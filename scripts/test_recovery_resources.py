"""Actual child-process cancellation/timeout and bounded restore verification."""
from contextlib import closing, ExitStack, nullcontext
import fcntl,json,os,sqlite3,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from g2h.capacity import Reconciler, Reservation
import test_review_retarget as fixture
from review_recovery import restore
import review_retarget


class FailTerminalRead(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.startswith('SELECT status,message FROM jobs WHERE id='):
            raise sqlite3.OperationalError('database is locked during terminal read')
        return super().execute(sql, *args, **kwargs)


def terminal_read_failure(queue):
    def connect():
        db = sqlite3.connect(queue.path, timeout=5, factory=FailTerminalRead)
        db.row_factory = sqlite3.Row
        return db
    return patch.object(queue, 'db', connect)


def storage_scan_failure(folder, active, fault):
    stack = ExitStack()
    temporary = folder/'transient.bin'
    if fault == 'disappeared':
        is_file, stat = Path.is_file, Path.stat

        def is_file_at_listing(path, *args, **kwargs):
            return path == temporary or is_file(path, *args, **kwargs)

        def stat_after_removal(path, *args, **kwargs):
            if path == temporary and active():
                path.unlink(missing_ok=True)
            return stat(path, *args, **kwargs)

        stack.enter_context(patch.object(Path, 'is_file', is_file_at_listing))
        stack.enter_context(patch.object(Path, 'stat', stat_after_removal))
    else:
        for method in ('iterdir', 'glob'):
            original = getattr(Path, method)

            def unreadable(path, *args, original=original, **kwargs):
                if path == folder and active():
                    raise OSError('job directory could not be read')
                return original(path, *args, **kwargs)

            stack.enter_context(patch.object(Path, method, unreadable))
    return stack


class Resources(unittest.TestCase):
    setUp=fixture.Tests.setUp
    tearDown=fixture.Tests.tearDown
    submit=fixture.Tests.submit
    def exercise(self,cancel,fail_timing=False,fail_capacity=False,fail_status=False,scan_fault=None):
        key=self.submit()['id'];folder=self.queue.folder/key
        lock=(self.queue.folder/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
        original=subprocess.Popen;children=[]
        write_text=Path.write_text
        def write_output(path,*args,**kwargs):
            if fail_timing and path==folder/'timing.tmp':
                raise OSError('timing file is not writable')
            return write_text(path,*args,**kwargs)
        def launch(command,**kwargs):
            if fail_status:
                (folder/'input.npz').write_bytes(b'incomplete solver input')
            if scan_fault == 'disappeared':
                (folder/'transient.bin').write_bytes(b'x'*30000)
            child=original([sys.executable,'-c','import time; time.sleep(60)'],**kwargs);children.append(child)
            if cancel:threading.Timer(.3,lambda:self.queue.cancel(key,self.a)).start()
            return child
        capacity_commit = (patch.object(Reservation, 'commit', side_effect=sqlite3.OperationalError('database is locked'))
                           if fail_capacity else nullcontext())
        try:
            # The queue fixture has no motion catalog; exercise the real child
            # lifetime after source preflight has accepted a motion.
            with patch('semantic_retarget.preflight', return_value=True), \
                 patch.object(review_retarget.subprocess,'Popen',side_effect=launch), \
                 patch.object(review_retarget,'RETARGET_TIMEOUT_SECONDS',2 if cancel else .1), \
                 patch.object(review_retarget.os,'nice'), \
                 patch.object(Path,'write_text',write_output), \
                 capacity_commit, \
                 (terminal_read_failure(self.queue) if fail_status else nullcontext()), \
                 (storage_scan_failure(folder, lambda: bool(children), scan_fault) if scan_fault else nullcontext()), \
                 patch.object(sys,'argv',['test',str(self.root),str(lock.fileno())]):
                self.queue.work()
        finally:lock.close()
        self.assertEqual(self.queue.get(key,self.a)['status'],'cancelled' if cancel else 'failed')
        self.assertTrue(children and children[0].poll() is not None)
        if fail_status:
            self.assertEqual((folder/'quarantine/input.npz').read_bytes(), b'incomplete solver input')
            self.assertEqual(json.loads((folder/'quality.json').read_text())['status'], 'cancelled')
        with closing(sqlite3.connect(self.root/'review/resource_quota.sqlite3')) as db:
            charges = {kind:(state,charged) for kind,state,charged in db.execute('SELECT kind,state,charged FROM reservations')}
        self.assertEqual(charges['retarget_seconds'][0], 'settled')
        if scan_fault == 'unreadable':
            self.assertEqual(charges['storage_bytes'], ('reserved',20_000_000))
        else:
            self.assertEqual(charges['storage_bytes'][0], 'settled')
            if scan_fault == 'disappeared':
                self.assertFalse((folder/'transient.bin').exists())
                self.assertLess(charges['storage_bytes'][1],30000)
        capacity_abandoned = fail_capacity or scan_fault == 'unreadable'
        with closing(sqlite3.connect(self.root/'review/storage/capacity.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT abandoned FROM reservations').fetchall(), [(1,)] if capacity_abandoned else [])
        if capacity_abandoned:
            scan = Reconciler(self.root)
            try:
                self.assertTrue(scan.step(seconds=5))
            finally:
                scan.close()
            with closing(sqlite3.connect(self.root/'review/storage/capacity.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM reservations').fetchone()[0], 0)
        with (self.queue.folder/'worker.lock').open('a') as f:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def test_cancel_reaps_actual_process_and_settles(self):self.exercise(True)
    def test_timeout_reaps_actual_process_and_settles(self):self.exercise(False)
    def test_cancel_settles_quota_when_timing_write_fails(self):self.exercise(True,fail_timing=True)
    def test_cancel_settles_account_quota_when_capacity_commit_fails(self):self.exercise(True,fail_capacity=True)
    def test_cancel_quarantines_and_settles_when_terminal_read_fails(self):self.exercise(True,fail_status=True)
    def test_cancel_settles_when_temporary_file_disappears_during_scan(self):self.exercise(True,scan_fault='disappeared')
    def test_cancel_settles_time_and_preserves_unknown_storage(self):self.exercise(True,scan_fault='unreadable')

    def test_invalid_job_directory_does_not_block_the_next_job(self):
        key = self.submit()['id']
        next_key = self.submit(2)['id']
        (self.queue.folder / key).write_text('unexpected regular file')
        with (self.queue.folder/'worker.lock').open('a') as lock, patch.object(review_retarget.os, 'nice'):
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.queue.work()
            failed = self.queue.get(key, self.a)
            self.assertEqual(failed['status'], 'failed')
            self.assertIn('作业目录不可用', failed['message'])
            self.assertEqual(self.queue.claim(0)['id'], next_key)

    def test_finished_result_is_preserved_when_cache_recheck_cannot_read_the_catalog(self):
        key = self.submit()['id']
        folder = self.queue.folder / key
        script = ("from pathlib import Path; import sys; root = Path(sys.argv[1]); "
                  "(root/'result.npz').write_bytes(b'completed'); (root/'preview.json').write_text('{}')")
        launch = subprocess.Popen
        earlier = {'key': 'initial-cache-identity'}
        with (self.queue.folder / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch('semantic_retarget.preflight', return_value=True), \
                 patch('retarget_cache.identity', side_effect=[earlier, sqlite3.OperationalError('database is locked')]), \
                 patch.object(review_retarget.subprocess, 'Popen', side_effect=lambda command, **options:
                              launch([sys.executable, '-c', script, str(folder)], **options)), \
                 patch.object(review_retarget.os, 'nice'), \
                 patch.object(sys, 'argv', ['test', str(self.root), str(lock.fileno())]):
                self.queue.work()
        self.assertEqual(self.queue.get(key, self.a)['status'], 'done')
        self.assertEqual((folder / 'result.npz').read_bytes(), b'completed')
        self.assertFalse((folder / 'reuse-manifest.json').exists())

    def test_restore_hash_and_quick_check(self):
        import hashlib
        folder=self.root/'backup';folder.mkdir();f=folder/'test.sqlite3'
        with closing(sqlite3.connect(f)) as db, db:db.execute('CREATE TABLE test(x)');db.execute('INSERT INTO test VALUES(42)')
        (folder/'manifest.json').write_text(json.dumps(dict(id='test',databases=[dict(path='test.sqlite3',sha256=hashlib.sha256(f.read_bytes()).hexdigest())])))
        self.assertEqual(restore(folder,self.root/'restored')['verified'],1)
        with self.assertRaises(ValueError):restore(folder,self.root/'restored')
        with f.open('ab') as out:out.write(b'corrupt')
        with self.assertRaises(ValueError):restore(folder,self.root/'bad-restore')

if __name__=='__main__':unittest.main()
