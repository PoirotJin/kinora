import json,tempfile,unittest,sqlite3,contextlib,io,subprocess,sys,time
from pathlib import Path
from unittest.mock import patch
import MotionFarm as farm
from production_resources import profile,replenish

class ResourcesTests(unittest.TestCase):
 def test_recovery_keeps_live_producer_without_process_identity(self):
  from review_production import Production
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);production=Production(root,root/'topics.json',worker=True)
   with subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']) as child:
    try:
     with contextlib.closing(production.db()) as db,db:
      now=time.time()-25
      db.execute('INSERT INTO jobs(id,created,updated,status,pid) VALUES(?,?,?,?,?)',('old',now,now,'running',child.pid))
     production.recover()
     with contextlib.closing(production.db()) as db:
      self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'running')
    finally:
     child.terminate();child.wait(timeout=5)
   production.recover()
   with contextlib.closing(production.db()) as db:
    self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'failed')

 def setup_root(self,root):
  (root/'review/production').mkdir(parents=True)
  (root/'review/production/resource_profile.json').write_text(json.dumps(dict(batch_size=8,backfill_variants=8)))
  db=farm.catalog(root)
  for i,verdict in [(1,'distinct'),(2,'duplicate')]:
   item=dict(id='text'+str(i),topic_id='walking',text='A person walks forward with short steps.',duration_s=8,source={'semantic_review':{'verdict':verdict}})
   db.execute('insert into prompts values(?,?,?,?)',(item['id'],'walking',str(i),json.dumps(item)))
  db.execute('insert into motion_links(task_id,text_id,topic,seed) values(?,?,?,?)',('old','text1','walking',42));db.commit();db.close()
 def test_disabled_by_default_and_invalid_fails_closed(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);self.assertEqual(profile(root)['backfill_variants'],0)
   self.setup_root(root);(root/'review/production/resource_profile.json').write_text('{"batch_size":64}')
   with self.assertRaises(ValueError):profile(root)
 def test_only_reviewed_text_missing_seeds_and_restart_uses_manifest(self):
  with tempfile.TemporaryDirectory() as t,contextlib.redirect_stdout(io.StringIO()):
   root=Path(t);self.setup_root(root)
   with patch.object(farm,'enqueue_manifest') as enqueue:
    self.assertEqual(replenish(root,'walking','job_0_walking',8),7)
    self.assertEqual(replenish(root,'walking','job_0_walking',8),7)
    self.assertEqual(enqueue.call_args_list[0],enqueue.call_args_list[1])
   saved=json.loads((root/'agent_runs/job_0_walking_variants/manifest.json').read_text())
   self.assertEqual(saved['prompts'][0]['id'],'text1');self.assertNotIn(42,saved['variant_seeds'])
 def test_completed_prompt_not_regenerated(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);self.setup_root(root);db=farm.catalog(root)
   for seed in range(7):db.execute('insert into motion_links(task_id,text_id,topic,seed) values(?,?,?,?)',('old'+str(seed),'text1','walking',seed))
   db.commit();db.close();self.assertEqual(replenish(root,'walking','job_0_walking',8),0)
 def test_history_older_than_512_is_reached_across_calls(self):
  with tempfile.TemporaryDirectory() as t,contextlib.redirect_stdout(io.StringIO()):
   root=Path(t);self.setup_root(root);db=farm.catalog(root)
   for i in range(513):
    item=dict(id='skip'+str(i),topic_id='walking',text='A person walks forward with short steps.',duration_s=8,source={'semantic_review':{'verdict':'duplicate'}})
    db.execute('insert into prompts values(?,?,?,?)',(item['id'],'walking','skip'+str(i),json.dumps(item)))
   db.commit();db.close()
   with patch.object(farm,'enqueue_manifest'):
    self.assertEqual(replenish(root,'walking','page1',8),0)
    self.assertEqual(replenish(root,'walking','page2',8),7)
   saved=json.loads((root/'agent_runs/page2_variants/manifest.json').read_text());self.assertEqual(saved['prompts'][0]['id'],'text1')
 def test_cursor_persists_and_sweep_wrap_is_bounded(self):
  with tempfile.TemporaryDirectory() as t,contextlib.redirect_stdout(io.StringIO()):
   root=Path(t);self.setup_root(root)
   with patch.object(farm,'enqueue_manifest'):
    self.assertEqual(replenish(root,'walking','cursor_first',8),7)
    self.assertEqual(replenish(root,'walking','cursor_next',8),0)
    self.assertEqual(replenish(root,'walking','cursor_wait',8),0)
   state=sqlite3.connect(root/'review/production/resource_cursor.sqlite3')
   before,wait=state.execute('select before_rowid,wait_until from cursors').fetchone();state.close()
   self.assertIsNone(before);self.assertGreater(wait,0)
 def test_old_human_rejection_is_checked_by_motion_id(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);self.setup_root(root)
   from g2h.farm_index import short_id
   (root/'review').mkdir(exist_ok=True);r=sqlite3.connect(root/'review/reviews.sqlite3')
   r.execute('create table latest(motion_id text primary key,status text,body text)')
   db=farm.catalog(root);number=db.execute('select number from motion_links where task_id=?',('old',)).fetchone()[0];db.close()
   r.execute('insert into latest values(?,?,?)',(short_id(number),'rejected','{}'));r.commit();r.close()
   self.assertEqual(replenish(root,'walking','rejected_old',8),0)
if __name__=='__main__':unittest.main()
