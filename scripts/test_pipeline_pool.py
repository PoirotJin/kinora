import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import closing
from pipeline_pool import Stage,Pipeline,status


class PoolTests(unittest.TestCase):
    def test_bounded_admission_dedup_and_independent_stages(self):
        gate=threading.Event();started=threading.Event();other=threading.Event()
        a=Stage('test-api',1);b=Stage('test-cpu',1)
        def block():started.set();gate.wait(2)
        try:
            self.assertTrue(a.submit('one',block));self.assertTrue(started.wait(1))
            self.assertFalse(a.submit('one',block));self.assertFalse(a.submit('two',block))
            self.assertTrue(b.submit('independent',other.set));self.assertTrue(other.wait(1))
            self.assertEqual(a.snapshot()['running'],1)
        finally:gate.set();a.close();b.close()
        self.assertFalse(a.submit('closed',lambda:None))

    def test_exception_releases_slot_and_has_backoff(self):
        stage=Stage('failure',1)
        def fail():raise RuntimeError('fixture')
        stage.submit('bad',fail)
        deadline=time.monotonic()+2
        while stage.snapshot()['failed']==0 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(stage.snapshot()['failed'],1)
        self.assertFalse(stage.submit('bad',fail))
        self.assertTrue(stage.submit('good',lambda:None));stage.close()

    def test_atomic_snapshot_and_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            pipe=Pipeline(folder)
            try:
                pipe.publish();data=status(folder)
                self.assertFalse(data['stale']);self.assertEqual(sum(s['capacity'] for s in data['stages']),5)
                file=Path(folder)/'review/pipeline/status.json';data['sampled_at']=0;file.write_text(json.dumps(data))
                self.assertTrue(status(folder)['stale'])
            finally:pipe.close()


class FlowTests(unittest.TestCase):
    def setUp(self):
        from review_production import Production
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        config=self.root/'topics.json';config.write_text(json.dumps({'topics':[{'id':'walking','name':'Walking'}]}))
        self.p=Production(self.root,config)
        self.job=self.p.launch(dict(topics=['walking'],per_topic=1,k=1,rounds=1,duration=8),'tester')['job_id']
        folder=self.root/'topics/walking';folder.mkdir(parents=True)
        self.index=folder/'index.sqlite3'
        with sqlite3.connect(self.index) as db:db.execute('CREATE TABLE tasks(state TEXT)')
        self.storage=self.root/'storage.sqlite3'
        with sqlite3.connect(self.storage) as db:db.execute('CREATE TABLE jobs(status TEXT)')
        storage=self.storage
        class FakeStorage:
            def __init__(self,*args):pass
            def db(self):return sqlite3.connect(storage)
            def recover(self):pass
            def inventory(self):return []
            def automatic(self,*args):pass
        self.patches=[patch('review_storage.Storage',FakeStorage),patch('g2h.capacity.status',return_value={'ready':True,'stale':False}),patch('g2h.cold_storage.guard'),patch('review_custom.Custom.run_batch')]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def job_status(self):
        with closing(self.p.db()) as db:return db.execute('SELECT status FROM jobs WHERE id=?',(self.job,)).fetchone()[0]

    def test_api_unlocked_gpu_exclusive_and_durable_progress(self):
        calls=[]
        def command(job,args,fd,folder=None):
            calls.append(Path(args[0]).name)
            with (self.root/'gpu_worker.lock').open('a') as probe:
                if fd is None:
                    fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with sqlite3.connect(self.index) as db:db.execute("INSERT INTO tasks VALUES('pending')")
                else:
                    with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with sqlite3.connect(self.index) as db:db.execute("UPDATE tasks SET state='done'")
            return True
        with patch.object(self.p,'command',side_effect=command):self.p.execute(self.job)
        self.assertEqual(calls,['qwen_motion_agent.py','motion_collector.py'])
        self.assertEqual(self.job_status(),'completed')
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT round FROM runtime').fetchone()[0],1)

    def test_api_can_prepare_while_other_job_owns_gpu(self):
        def command(job,args,fd,folder=None):
            self.assertIsNone(fd);self.p.cancel();return False
        with (self.root/'gpu_worker.lock').open('a') as other:
            fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(self.p,'command',side_effect=command) as call:self.p.execute(self.job)
            call.assert_called_once()
        self.assertEqual(self.job_status(),'stopped')

    def test_failed_api_retains_prompt_cursor_and_releases_gpu(self):
        with patch.object(self.p,'command',side_effect=RuntimeError('fixture')):self.p.execute(self.job)
        self.assertEqual(self.job_status(),'failed')
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT phase FROM runtime').fetchone()[0],'prompts')
        with (self.root/'gpu_worker.lock').open('a') as probe:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)

if __name__=='__main__':unittest.main()
