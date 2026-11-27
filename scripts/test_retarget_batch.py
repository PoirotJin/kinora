from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from retarget_batch import create,step,job_id
from review_retarget import Retarget

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        from review_auth import Auth
        Auth(self.root/'review').create('jfc','temporary-test-password','admin')
        self.q=Retarget(self.root)
        with sqlite3.connect(self.root/'prompts.sqlite3') as c:
            c.execute('CREATE TABLE motion_links(number INTEGER PRIMARY KEY,task_id TEXT,text_id TEXT,topic TEXT,seed INTEGER)')
            c.executemany('INSERT INTO motion_links VALUES(?,?,?,?,?)',[(i,'t'+str(i),'text'+str(i),'walking',1) for i in range(1,7)])
        p=self.root/'topics/walking';p.mkdir(parents=True)
        with sqlite3.connect(p/'index.sqlite3') as c:
            c.execute('CREATE TABLE records(id TEXT PRIMARY KEY,shard INTEGER,offset INTEGER,bytes INTEGER,sha256 TEXT)')
            c.execute('CREATE TABLE tasks(id TEXT,state TEXT,error TEXT,spec TEXT)')
            c.executemany('INSERT INTO records VALUES(?,0,0,100,?)',[('t'+str(i),'sha'+str(i)) for i in range(1,6)])
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as c:
            c.execute('CREATE TABLE removed(motion_id TEXT PRIMARY KEY,at REAL,username TEXT,reason TEXT)')
            c.execute("INSERT INTO removed VALUES('m00000005',0,'jfc','test')")
        self.folder=self.root/'review/retarget/batches/test-batch'
        self.mock=patch('retarget_batch.fingerprint',return_value={'version':'one'});self.mock.start()
        self.manifest=create(self.root,self.folder,'jfc')
        self.disk=patch('retarget_batch.shutil.disk_usage',return_value=type('Space',(),{'free':100*1024**3})());self.disk.start()
        self.addCleanup(self.disk.stop)
    def tearDown(self):self.mock.stop();self.tmp.cleanup()
    def test_low_disk_waits_without_submitting(self):
        with patch('retarget_batch.shutil.disk_usage',return_value=type('Space',(),{'free':10*1024**3})()):
            self.assertEqual(step(self.root,self.folder,self.manifest)['state'],'waiting')
        with closing(self.q.db()) as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
    def test_snapshot_excludes_removed_and_uncommitted(self):
        self.assertEqual(self.manifest['total'],4)
    def test_two_inflight_only_and_idempotent_resume(self):
        step(self.root,self.folder,self.manifest);step(self.root,self.folder,self.manifest)
        with closing(self.q.db()) as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
    def test_failed_motion_is_recorded_and_next_is_fed(self):
        step(self.root,self.folder,self.manifest)
        self.q.update(job_id('test-batch','m00000001'),status='failed',message='quality gate')
        result=step(self.root,self.folder,self.manifest)
        self.assertEqual(result['counts']['failed'],1);self.assertEqual(result['counts']['queued'],2)
    def test_version_change_pauses_feeding(self):
        with patch('retarget_batch.fingerprint',return_value={'version':'two'}):
            self.assertEqual(step(self.root,self.folder,self.manifest)['state'],'paused')
        with closing(self.q.db()) as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
    def test_stable_distinct_request_ids(self):
        self.assertEqual(job_id('b','m1'),job_id('b','m1'));self.assertNotEqual(job_id('b','m1'),job_id('b','m2'))
    def test_new_motions_do_not_expand_snapshot(self):
        with sqlite3.connect(self.root/'prompts.sqlite3') as c:c.execute("INSERT INTO motion_links VALUES(99,'t99','text99','walking',1)")
        self.assertEqual(json.loads((self.folder/'manifest.json').read_text())['total'],4)

if __name__=='__main__':unittest.main()
