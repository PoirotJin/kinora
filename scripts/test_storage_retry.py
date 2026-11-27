"""Bounded storage retry regression using disposable, real shard archives."""
from contextlib import closing
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from review_storage import Storage
from review_access import allowed
import test_cold_storage as cold_fixture


class StorageRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=cold_fixture.ColdTests();self.fixture.setUp()
        self.root=self.fixture.root
        self.config=self.root/'topics.json'
        self.config.write_text(json.dumps({'topics':[{'id':'walking','name':'Walking','definition':'walking'}]}))
        self.storage=Storage(self.root,self.config)

    def tearDown(self):self.fixture.tearDown()

    def job(self,key='first',status='running',message='准备打包'):
        now=time.time()
        with closing(self.storage.db()) as db,db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,'walking',0,'export',status,now,now,None,None,None,message))
        return key

    def execute(self,key):
        with patch('review_storage.os.nice'),patch('review_storage.capacity_status',return_value={'total':0}):
            self.storage.execute(key)

    def retry(self,key):
        child=Mock(pid=123456)
        with patch('review_storage.subprocess.Popen',return_value=child),patch('review_storage.threading.Thread.start'),patch('review_production.Production.identity',return_value='fixture'):
            return self.storage.retry({'job_id':key})['job_id']

    def test_writer_busy_then_explicit_retry_completes_without_mutating_source(self):
        key=self.job()
        with (self.fixture.folder/'writer.lock').open('a') as writer:
            fcntl.flock(writer,fcntl.LOCK_EX)
            self.execute(key)
        failed=self.storage.status()['jobs'][0]
        self.assertEqual(failed['status'],'failed')
        self.assertTrue(failed['retryable'])
        self.assertIn('本次打包未执行',failed['message'])
        self.assertNotIn('将在',failed['message'])
        new_key=self.retry(key)
        self.assertNotEqual(new_key,key)
        self.execute(new_key)
        with closing(self.storage.db()) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(key,)).fetchone()[0],'failed')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(new_key,)).fetchone()[0],'completed')
        self.assertTrue(self.storage.download(new_key).is_file())
        self.assertEqual(self.fixture.raw.read_bytes(),self.fixture.original)
        self.assertFalse(next(j for j in self.storage.status()['jobs'] if j['id']==new_key)['retryable'])

    def test_retry_rejects_missing_active_completed_and_concurrent_attempts(self):
        self.job('failed','failed');self.job('done','completed');self.job('running','running')
        for key in ('missing','done','running'):
            with self.subTest(key=key),self.assertRaises(ValueError):self.retry(key)
        with self.assertRaisesRegex(ValueError,'已有打包任务'):self.retry('failed')
        with closing(self.storage.db()) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],3)

    def test_legacy_failed_job_does_not_claim_automatic_recovery(self):
        self.job(status='failed',message='该主题正在生成，分包将在生成器空闲后处理')
        row=self.storage.status()['jobs'][0]
        self.assertTrue(row['retryable']);self.assertIn('不会自动恢复',row['message'])

    def test_inventory_reads_cold_after_hot_file_disappears(self):
        from g2h.cold_storage import package

        archived = package(self.storage.root, 'walking', 0)
        archive = Path(archived['path'])
        expected = archive.stat()
        hot = self.storage.root/'topics/walking/motions_000000.pkl'
        exists = Path.exists

        def exists_before_hot_unlink(path):
            return True if path == hot else exists(path)

        with patch.object(Path, 'exists', exists_before_hot_unlink):
            item, = self.storage.inventory()
        self.assertEqual((item['tier'], item['bytes'], item['modified']),
                         ('cold', expected.st_size, expected.st_mtime))

    def test_inventory_labels_missing_shard_without_dropping_other_rows(self):
        from g2h.collection_store import CollectionStore

        with CollectionStore(self.fixture.folder, max_bytes=self.fixture.raw.stat().st_size) as store:
            store.append(dict(id='b'*64, motion=b'second motion'))
        self.fixture.raw.unlink()
        missing, present = self.storage.inventory()
        self.assertEqual((missing['shard'], missing['tier'], missing['bytes'], missing['modified']), (0, 'missing', None, None))
        self.assertEqual((present['shard'], present['tier']), (1, 'hot'))

    def test_recovery_keeps_live_archive_without_process_identity(self):
        from review_auth import Auth
        from review_server import Handler, Server

        key = self.job()
        with Server(('127.0.0.1', 0), Handler) as server:
            server.auth = Auth(self.root/'review')
            server.auth.create('admin', 'temporary-test-password', 'admin')
            token = server.auth.change_password('admin', 'temporary-test-password', 'changed-test-password')
            server.storage = self.storage
            server.web = self.root
            origin = 'http://127.0.0.1:'+str(server.server_port)
            headers = {'Cookie': 'mf_session='+token, 'Origin': origin,
                       'X-MotionFarm-Request': '1', 'Content-Type': 'application/json'}
            package = Request(origin+'/api/storage/package',
                              data=b'{"topic":"walking","shard":0,"kind":"export"}', headers=headers)
            status = Request(origin+'/api/storage', headers=headers)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) as child:
                    try:
                        with closing(self.storage.db()) as db, db:
                            db.execute('UPDATE jobs SET pid=?, updated=? WHERE id=?', (child.pid, time.time()-25, key))
                        with self.assertRaises(HTTPError) as error:
                            urlopen(package, timeout=3)
                        with error.exception as response:
                            self.assertEqual(response.code, 400)
                            self.assertIn('已有打包任务', json.load(response)['error'])
                        with urlopen(status, timeout=3) as response:
                            job, = json.load(response)['jobs']
                        self.assertEqual((job['id'], job['status'], job['retryable']), (key, 'running', False))
                    finally:
                        child.terminate()
                        child.wait(timeout=5)
                with urlopen(package, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                    replacement = json.load(response)['job_id']
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    with urlopen(status, timeout=3) as response:
                        jobs = {job['id']: job for job in json.load(response)['jobs']}
                    if jobs[replacement]['status'] != 'running':
                        break
                    time.sleep(.05)
                self.assertEqual(jobs[key]['status'], 'failed')
                self.assertEqual(jobs[replacement]['status'], 'completed', jobs[replacement]['message'])
                with urlopen(Request(origin+'/api/storage/download?id='+replacement, headers=headers), timeout=3) as response:
                    self.assertTrue(response.read().startswith(b'PK\x03\x04'))
                self.assertEqual(self.fixture.raw.read_bytes(), self.fixture.original)
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_retry_stays_admin_only(self):
        self.assertFalse(allowed({'role':'user'},'/api/storage/retry',True))
        self.assertTrue(allowed({'role':'admin'},'/api/storage/retry',True))


if __name__=='__main__':unittest.main()
