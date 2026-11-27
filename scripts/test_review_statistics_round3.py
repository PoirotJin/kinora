"""Synthetic DB/motion regression for latest-review-only derived statistics."""
from contextlib import closing
import datetime,json,sqlite3,threading,unittest
from unittest.mock import patch
import numpy as np
import MotionFarm as farm
from g2h import agent_learning as learning
from g2h.collection_store import CollectionStore
from g2h.farm_index import register_motion,read_motion
import test_review_server as fixtures


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReviewTests();self.f.setUp();self.root=self.f.root
        with closing(farm.catalog(self.root)) as db:self.mid=register_motion(db,'a'*64,'walking','summary-fixture',43)
        with CollectionStore(self.root/'topics/walking') as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('summary-fixture','{}'))
            store.append(dict(id='summary-fixture',topic='walking',text='A person walks forward.',created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),motion=dict(fps=np.array(30),native_keypoints3d=np.zeros((60,52,3)),native_transl=np.zeros((60,3)),native_root_rotations_mat=np.tile(np.eye(3),(60,1,1)))))
        np.array([-1]+[0]*51,dtype=np.int32).tofile(self.f.assets/'kintree.bin')
        np.zeros((52,3),dtype=np.float32).tofile(self.f.assets/'j_template.bin')
        (self.f.assets/'joint_names.json').write_text(json.dumps(['joint'+str(n) for n in range(52)]))
        with closing(self.f.store.catalog()) as db:learning.index_record(self.root,read_motion(db,self.root,self.mid))
    def tearDown(self):self.f.tearDown()
    def row(self):
        with closing(learning.connect(self.root)) as db:return dict(db.execute('SELECT * FROM checks WHERE motion_id=?',(self.mid,)).fetchone())
    def second(self):return self.f.store.save(self.mid,{**self.f.value,'revision':1,'reviewer':'bob','status':'rejected','semantic':'mismatch','quality':'issues','issues':['action_mismatch']})
    def test_stale_postcommit_callback_cannot_revert_summary(self):
        paused=threading.Event();resume=threading.Event();errors=[];original=learning.reviewed
        def delayed(root,body):
            if body.get('revision')==1:
                paused.set()
                if not resume.wait(4):raise RuntimeError('callback test timeout')
            return original(root,body)
        def alice():
            try:self.f.store.save(self.mid,{**self.f.value,'reviewer':'alice'})
            except BaseException as e:errors.append(e)
        with patch.object(learning,'reviewed',side_effect=delayed):
            worker=threading.Thread(target=alice);worker.start()
            try:
                self.assertTrue(paused.wait(4));self.second()
            finally:resume.set();worker.join(4)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        row=self.row();self.assertEqual((row['review_revision'],row['status'],row['semantic'],row['quality']),(2,'rejected','mismatch','issues'))
        self.assertEqual(json.loads(row['issues']),['action_mismatch'])
        self.assertEqual(learning.overview(self.root)['hour']['accepted_seconds'],0)
        self.assertEqual(len(self.f.store.detail(self.mid)['history']),2)
    def test_derived_transaction_locks_authoritative_review_until_commit(self):
        self.f.store.save(self.mid,self.f.value)
        entered=threading.Event();release=threading.Event();errors=[];original=learning._sync_review
        def hold(db,mid,apply=True):
            result=original(db,mid,apply);entered.set()
            if not release.wait(4):raise RuntimeError('lock test timeout')
            return result
        def work():
            try:learning.reviewed(self.root,{'motion_id':self.mid})
            except BaseException as e:errors.append(e)
        with patch.object(learning,'_sync_review',side_effect=hold):
            worker=threading.Thread(target=work);worker.start()
            try:
                self.assertTrue(entered.wait(4))
                with closing(sqlite3.connect(self.root/'review/reviews.sqlite3',timeout=.05)) as db:
                    with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):db.execute('BEGIN IMMEDIATE')
            finally:release.set();worker.join(4)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.second();self.assertEqual(self.row()['review_revision'],2)
    def test_cached_preview_repairs_existing_inconsistency_without_payload_read(self):
        self.f.store.save(self.mid,self.f.value);self.second();self.f.store.preview(self.mid)
        with closing(learning.connect(self.root)) as db,db:db.execute("UPDATE checks SET status='accepted',semantic='match',review_revision=1 WHERE motion_id=?",(self.mid,))
        with patch('review_server.read_motion',side_effect=AssertionError('warm cache reread payload')):self.f.store.preview(self.mid)
        self.assertEqual((self.row()['status'],self.row()['review_revision']),('rejected',2))
    def test_busy_agent_worker_does_not_hold_review_lock_while_waiting(self):
        first=self.f.store.save(self.mid,self.f.value)
        waiting=threading.Event();errors=[];original=learning.connect
        def announced(root):
            waiting.set();return original(root)
        def work():
            try:learning.reviewed(self.root,first)
            except BaseException as e:errors.append(e)
        with closing(sqlite3.connect(self.root/'review/agent.sqlite3',timeout=1)) as worker_db:
            worker_db.execute('BEGIN IMMEDIATE')
            worker_db.execute('UPDATE checks SET priority=priority+1 WHERE motion_id=?',(self.mid,))
            with patch.object(learning,'connect',side_effect=announced):
                worker=threading.Thread(target=work);worker.start()
                try:
                    self.assertTrue(waiting.wait(4))
                    # A fresh human revision can commit while the summary worker is busy.
                    with closing(self.f.store.reviews()) as db,db:
                        db.execute('BEGIN IMMEDIATE')
                        newer={**first,'revision':2,'status':'rejected','semantic':'mismatch','quality':'issues','issues':['action_mismatch']}
                        raw=json.dumps(newer)
                        db.execute('INSERT INTO events(motion_id,revision,body) VALUES(?,?,?)',(self.mid,2,raw))
                        db.execute('INSERT OR REPLACE INTO latest VALUES(?,?,?,?)',(self.mid,2,'rejected',raw))
                finally:worker_db.commit();worker.join(4)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual((self.row()['status'],self.row()['review_revision']),('rejected',2))
    def test_index_created_after_review_synchronizes_latest(self):
        self.f.store.save(self.mid,self.f.value);self.second()
        with closing(learning.connect(self.root)) as db,db:db.execute('DELETE FROM checks WHERE motion_id=?',(self.mid,))
        with closing(self.f.store.catalog()) as db:learning.index_record(self.root,read_motion(db,self.root,self.mid))
        self.assertEqual((self.row()['status'],self.row()['review_revision']),('rejected',2))
    def test_stale_metadata_at_same_revision_is_repaired(self):
        body=self.f.store.save(self.mid,self.f.value)
        with closing(learning.connect(self.root)) as db,db:db.execute("UPDATE checks SET status='rejected',quality='issues' WHERE motion_id=?",(self.mid,))
        learning.reviewed(self.root,{**body,'status':'rejected','quality':'issues'})
        self.assertEqual((self.row()['status'],self.row()['quality']),('accepted','good'))
    def test_unsaved_callback_cannot_fabricate_accepted_summary(self):
        learning.reviewed(self.root,{'motion_id':self.mid,**self.f.value})
        self.assertEqual((self.row()['status'],self.row()['review_revision']),('unreviewed',0))
    def test_bounded_dry_run_and_idempotent_repair_preserve_history(self):
        self.f.store.save(self.mid,self.f.value);self.second()
        with closing(self.f.store.reviews()) as db:history=db.execute('SELECT * FROM events').fetchall()
        with closing(learning.connect(self.root)) as db,db:
            db.execute("UPDATE checks SET status='accepted',review_revision=0 WHERE motion_id=?",(self.mid,))
            for mid in ['m00000020','m00000021','m00000022']:
                db.execute("INSERT INTO checks(motion_id,status,review_revision) VALUES(?,'accepted',7)",(mid,))
        dry=learning.reconcile_reviews(self.root,limit=2,dry_run=True)
        self.assertEqual((dry['scanned'],dry['changed']),(2,2));self.assertIsNotNone(dry['next_after']);self.assertEqual(self.row()['status'],'accepted')
        first=learning.reconcile_reviews(self.root,limit=2);second=learning.reconcile_reviews(self.root,after=first['next_after'],limit=2)
        self.assertEqual(first['changed']+second['changed'],4);self.assertIsNone(second['next_after'])
        self.assertEqual(learning.reconcile_reviews(self.root,limit=20)['changed'],0)
        with closing(self.f.store.reviews()) as db:self.assertEqual(db.execute('SELECT * FROM events').fetchall(),history)
        for limit in [0,501,True]:
            with self.assertRaises(ValueError):learning.reconcile_reviews(self.root,limit=limit)
        with self.assertRaises(ValueError):learning.reconcile_reviews(self.root,after='bad cursor')
    def test_existing_schema_migrates_without_losing_rows(self):
        with closing(learning.connect(self.root)) as db,db:db.execute('ALTER TABLE checks DROP COLUMN review_revision')
        self.assertEqual(self.row()['review_revision'],0)
        self.assertEqual(self.row()['motion_id'],self.mid)


if __name__=='__main__':unittest.main()
