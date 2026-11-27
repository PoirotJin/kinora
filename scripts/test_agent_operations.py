import contextlib
import datetime
import fcntl
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
import MotionFarm as farm
import qwen_motion_agent as agent
from review_production import Production
from g2h import agent_learning as learning
from g2h.motion_traits import explicit_differences
from test_qwen_motion_agent import TOPICS,STRAIGHT,BACKWARD,prompt,response,review,AgentTests

class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=self.root/'topics.json';self.config.write_text(json.dumps({'topics':TOPICS}))
        self.p=Production(self.root,self.config,worker=True)
        self.settings=dict(topics=['walking'],per_topic=1,k=2,rounds=1,duration=8)
    def tearDown(self):self.tmp.cleanup()
    def job(self):
        with patch.object(self.p,'_spawn'):return self.p.launch(self.settings,'test')['job_id']
    def queue(self,n):
        folder=self.root/'topics/walking';folder.mkdir(parents=True)
        with sqlite3.connect(folder/'index.sqlite3') as db:
            db.executescript('CREATE TABLE tasks(state);CREATE TABLE shards(number,bytes);');db.executemany('INSERT INTO tasks VALUES(?)',[('pending',)]*n)
        return folder
    def test_mirror_and_path_regression(self):
        for a,b in [('walk forward','walk backward'),('turn left','turn right'),('run straight','run in a circle'),('turn clockwise','turn counterclockwise'),('walk slowly','walk fast')]:
            self.assertTrue(explicit_differences(a,b),(a,b))
        self.assertFalse(explicit_differences('walk','walk forward'))
    def test_checkpoint_manifest_replay_no_api(self):
        args=AgentTests().args(self.root);args.work_key='stable_step';args.enqueue=True
        replies=[response({'prompts':[prompt(STRAIGHT)]}),response(review(True))]
        with patch.object(farm,'call_qwen',side_effect=replies) as api,patch.object(farm,'enqueue_manifest') as enqueue,contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.run_agent(args,TOPICS));self.assertTrue(agent.run_agent(args,TOPICS))
        self.assertEqual(api.call_count,2);self.assertEqual(enqueue.call_count,2)
        with contextlib.closing(farm.catalog(self.root)) as db:self.assertEqual(db.execute('SELECT count(*) FROM prompts').fetchone()[0],1)
    def test_partial_api_success_reused(self):
        args=AgentTests().args(self.root);args.work_key='partial'
        with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(STRAIGHT)]}),farm.TemporaryAPIError('timeout')]),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(farm.TemporaryAPIError):agent.run_agent(args,TOPICS)
        with patch.object(farm,'call_qwen',return_value=response(review(True))) as api,contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(agent.run_agent(args,TOPICS))
        self.assertEqual(api.call_count,1)
    def test_backlog_skips_qwen_and_drains(self):
        folder=self.queue(20);job=self.job();calls=[]
        def run(j,args,*rest):
            calls.append(args[0])
            with sqlite3.connect(folder/'index.sqlite3') as db:db.execute("UPDATE tasks SET state='done'")
            return True
        with patch.object(self.p,'command',side_effect=run):self.p.execute(job)
        self.assertEqual([Path(x).name for x in calls],['motion_collector.py'])
    def test_resume_starts_at_motion_not_prompt(self):
        folder=self.queue(2);job=self.job()
        with contextlib.closing(self.p.db()) as db,db:db.execute("UPDATE runtime SET phase='motions' WHERE job=?",(job,))
        def run(j,args,*rest):
            self.assertEqual(Path(args[0]).name,'motion_collector.py')
            with sqlite3.connect(folder/'index.sqlite3') as db:db.execute("UPDATE tasks SET state='done'")
            return True
        with patch.object(self.p,'command',side_effect=run) as run:self.p.execute(job);self.assertEqual(run.call_count,1)
    def test_orphan_lock_blocks_recovery(self):
        job=self.job()
        with contextlib.closing(self.p.db()) as db,db:db.execute("UPDATE jobs SET status='running',updated=0 WHERE id=?",(job,))
        with (self.root/'gpu_worker.lock').open('a') as lock,patch.object(self.p,'_spawn') as spawn:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.p.recover();spawn.assert_not_called()
        with contextlib.closing(self.p.db()) as db:
            self.assertEqual(db.execute('SELECT status,stage FROM jobs WHERE id=?',(job,)).fetchone()[:],('running','recovering'))
            self.assertEqual(db.execute('SELECT restarts FROM runtime WHERE job=?',(job,)).fetchone()[0],0)
    def test_crash_storm_stops(self):
        job=self.job()
        with contextlib.closing(self.p.db()) as db,db:
            db.execute("UPDATE jobs SET status='running',updated=0 WHERE id=?",(job,));db.execute('UPDATE runtime SET restarts=3,restarted=?',(time.time(),))
        with patch.object(self.p,'_spawn') as spawn:self.p.recover();spawn.assert_not_called()
        with contextlib.closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'failed')
    def test_user_stop_never_auto_restarts(self):
        job=self.job()
        with contextlib.closing(self.p.db()) as db,db:db.execute("UPDATE jobs SET status='running' WHERE id=?",(job,))
        self.p.cancel()
        with contextlib.closing(self.p.db()) as db,db:db.execute('UPDATE jobs SET updated=0 WHERE id=?',(job,))
        with patch.object(self.p,'_spawn') as spawn:self.p.recover();spawn.assert_not_called()
        with contextlib.closing(self.p.db()) as db:
            self.assertEqual(db.execute('SELECT status,stage,stop FROM jobs WHERE id=?',(job,)).fetchone()[:],('stopped','stopped',1))
            self.assertEqual(db.execute('SELECT restarts FROM runtime WHERE job=?',(job,)).fetchone()[0],0)
    def record(self,mid,sign=1):
        trans=np.zeros((60,3));trans[:,2]=sign*np.arange(60)/30
        joints=np.zeros((60,52,3));joints[:,:,1]=1;joints[:,[7,8,10,11],1]=0
        return dict(motion_id=mid,text_id='text',topic='walking',text='A person walks backward.',
          created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
          motion=dict(fps=30,native_transl=trans,native_keypoints3d=joints,native_root_rotations_mat=np.tile(np.eye(3),(60,1,1))))
    def test_screening_duplicate_and_review_feedback(self):
        a=self.record('m1');learning.index_record(self.root,a);learning.index_record(self.root,a)
        learning.index_record(self.root,self.record('m2'))
        screen=learning.screening(self.root,'m2');self.assertIn('possible_duplicate',screen['flags']);self.assertIn('possible_direction_mismatch',screen['flags'])
        body=dict(motion_id='m2',status='accepted',semantic='match',quality='good',issues=[],revision=1)
        # Derived feedback may only reflect a persisted human review.
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:
            db.execute('CREATE TABLE latest(motion_id TEXT PRIMARY KEY,revision INTEGER,status TEXT,body TEXT)')
            db.execute('INSERT INTO latest VALUES(?,?,?,?)',('m2',1,'accepted',json.dumps(body)))
        learning.reviewed(self.root,body)
        out=learning.overview(self.root);self.assertEqual(out['hour']['motions'],2);self.assertEqual(out['hour']['accepted_seconds'],2)
        self.assertIsNone(out['hour']['tokens_per_accepted'])
    def test_signed_motion_descriptor_retains_direction(self):
        _,_,a=learning.analyze(self.record('a',1));_,_,b=learning.analyze(self.record('b',-1))
        self.assertGreater(np.linalg.norm(np.asarray(a)-np.asarray(b)),.5)
    def test_coverage_bounded_and_free_exploration(self):
        with contextlib.closing(farm.catalog(self.root)) as db:
            for i in range(520):
                item=prompt(STRAIGHT+str(i))
                with db:db.execute('INSERT INTO prompts VALUES(?,?,?,?)',(str(i),'walking',str(i),json.dumps(item)))
        m=learning.memory(self.root,'walking',0);self.assertEqual(m['sampled_texts'],512);self.assertTrue(m['free_exploration'])

if __name__=='__main__':unittest.main()
