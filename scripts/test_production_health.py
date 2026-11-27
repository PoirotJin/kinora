import json,sqlite3,tempfile,time,unittest
from unittest.mock import patch,Mock
from pathlib import Path
from production_health import read,outcome,setup
from review_production import Production

class HealthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  config=self.root/'topics.json';config.write_text(json.dumps({'topics':[{'id':'walking'}]}));self.p=Production(self.root,config)
  self.job=self.p.launch(dict(topics=['walking'],per_topic=1,k=1,rounds=1,duration=8),'test')['job_id']
 def test_failed_is_visible_even_no_active_job(self):
  self.p.update(self.job,status='failed',message='API failure')
  h=read(self.root);self.assertIn('production_failed',[a['key'] for a in h['alerts']])
 def test_custom_output_does_not_mask_stall(self):
  now=time.time()
  with self.p.db() as db:db.execute('UPDATE jobs SET status="running",created=?,updated=?',(now-4000,now))
  with sqlite3.connect(self.root/'review/telemetry.sqlite3') as db:
   db.execute('CREATE TABLE motions(saved REAL,job TEXT)');db.execute('INSERT INTO motions VALUES(?,?)',(now,'custom-id'))
  self.assertIn('production_no_output',[a['key'] for a in read(self.root)['alerts']])
  self.p.update(self.job,status='stopped')
  self.assertNotIn('production_no_output',[a['key'] for a in read(self.root)['alerts']])
 def test_persisted_counters_and_success_reset(self):
  for _ in range(24):counts=outcome(self.p,self.job,'skip','refused')
  self.assertEqual(counts['skips'],24)
  for _ in range(6):counts=outcome(self.p,self.job,'temporary','timeout')
  self.assertEqual(counts['retries'],6)
  outcome(self.p,self.job,'success');h=read(self.root)
  self.assertEqual(h['consecutive_skips'],0);self.assertEqual(h['retries'],0)
 def test_command_retry_budget_stops_at_six(self):
  with patch.object(self.p,'_command_once',return_value=75),patch('review_production.time.sleep'):
   with self.assertRaisesRegex(RuntimeError,'6'):self.p.command(self.job,[],None)
  self.assertEqual(read(self.root)['retries'],6)
 def test_actual_skip_path_circuit_breaker(self):
  for _ in range(23):outcome(self.p,self.job,'skip','refused')
  child=Mock();child.poll.return_value=3;child.returncode=3
  with patch('g2h.cold_storage.guard'),patch('review_production.subprocess.Popen',return_value=child):
   with self.assertRaisesRegex(RuntimeError,'24'):self.p._command_once(self.job,['qwen_motion_agent.py'],None)
if __name__=='__main__':unittest.main()
