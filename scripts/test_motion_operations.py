import json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import numpy as np
import motion_operations as ops
from production_health import topic_outcome,topic_wait,classify_step

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'review').mkdir()
 def tearDown(self):self.tmp.cleanup()
 def record(self,mid='m00000001',shift=0):
  p=np.zeros((60,52,3));p[:,:,1]=np.arange(52)[None,:]*.01;p+=shift
  return dict(motion_id=mid,topic='walking',motion=dict(native_keypoints3d=p,native_transl=np.zeros((60,3)),fps=30))
 def test_descriptor_translation_invariance(self):
  self.assertEqual(ops.descriptor(self.record())['signature'],ops.descriptor(self.record(shift=5))['signature'])
 def test_opposite_root_trajectories_not_identical(self):
  a=self.record();b=self.record();a['motion']['native_transl'][:,0]=np.arange(60)*.03;b['motion']['native_transl'][:,0]=-np.arange(60)*.03
  self.assertNotEqual(ops.descriptor(a)['signature'],ops.descriptor(b)['signature'])
 def test_similar_and_coverage(self):
  ops.save_descriptor(self.root,self.record(),'a');ops.save_descriptor(self.root,self.record('m00000002'),'b')
  self.assertEqual(ops.evidence(self.root,'m00000002')['similar'][0]['other_id'],'m00000001')
  self.assertEqual(ops.coverage(self.root)[0]['sampled'],2)
 def test_no_automatic_safety_claim(self):
  levels=ops.quality_levels({'state':'generated'},dict(checks={}))
  self.assertEqual(levels[-1]['status'],'未验证')
 def test_admin_only(self):
  with self.assertRaises(PermissionError):ops.configure_incremental(self.root,dict(role='user'),{'enabled':True})
 def test_invalid_release_path(self):
  with self.assertRaises(ValueError):ops.request_release(self.root,dict(role='admin',username='jfc'),dict(id='../bad',batch='test'))
 def test_topic_cooldown_isolated_and_persistent(self):
  class P:
   def db(inner):return sqlite3.connect(self.root/'guards.sqlite3')
  p=P()
  for _ in range(3):topic_outcome(p,'walking','duplicate')
  self.assertGreater(topic_wait(p,'walking'),3500);self.assertEqual(topic_wait(p,'dancing'),0)
  topic_outcome(p,'walking','success');self.assertEqual(topic_wait(p,'walking'),0)
 def test_reason_classification(self):
  p=self.root/'agent_runs/job_1_walking/attempt_01';p.mkdir(parents=True);(p/'outcome.json').write_text(json.dumps({'rejected':[{'reason':'Matches neighbor foo'}]}))
  self.assertEqual(classify_step(self.root,['--work-key','job_1_walking']),'duplicate')

 def test_release_failure_is_recorded_not_published(self):
  batch=self.root/'review/retarget/batches/t';batch.mkdir(parents=True)
  (batch/'manifest.json').write_text('{"owner":"jfc"}')
  ops.request_release(self.root,dict(role='admin',username='jfc'),dict(id='v1',batch='t'))
  with patch('review_access.active_account',return_value='user'):ops.release_tick(self.root)
  with closing(ops.connect(self.root)) as db:self.assertEqual(db.execute("SELECT status FROM releases WHERE id='v1'").fetchone()[0],'failed')
 def test_release_immutable_id(self):
  p=self.root/'review/retarget/batches/t';p.mkdir(parents=True);(p/'manifest.json').write_text('{"owner":"jfc"}')
  ops.request_release(self.root,dict(role='admin',username='jfc'),dict(id='v1',batch='t'))
  with self.assertRaises(ValueError):ops.request_release(self.root,dict(role='admin',username='jfc'),dict(id='v1',batch='t'))
 def test_paused_batch_does_not_submit(self):
  from retarget_batch import step
  p=self.root/'batch';p.mkdir();(p/'batch.sqlite3').touch()
  with sqlite3.connect(p/'batch.sqlite3') as db:
   db.execute('CREATE TABLE items(status TEXT)');db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute("INSERT INTO meta VALUES('operator_paused','1')")
  with patch('retarget_batch.publish',return_value={'state':'paused'}),patch('retarget_batch._step') as worker:
   self.assertEqual(step(self.root,p,{})['state'],'paused');worker.assert_not_called()

if __name__=='__main__':unittest.main()
