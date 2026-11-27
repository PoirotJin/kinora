from test_retarget_batch import Tests as Base
from unittest.mock import patch
from contextlib import closing
import json,sqlite3,zipfile
import motion_operations as ops

class Integration(Base):
 def test_incremental_excludes_prior_attempts_and_removed(self):
  # One explicit old attempt, regardless of success/failure, is not auto-retried.
  self.q.submit(dict(id='a'*32,motion_id='m00000001',robot='unitree_g1_23dof',parameters={},force_new=True),dict(username='jfc',role='admin'),__import__('retarget_batch').Store(self.root))
  ops.configure_incremental(self.root,dict(username='jfc',role='admin'),{'enabled':True})
  with patch('retarget_batch.fingerprint',return_value={'version':'one'}):ops.incremental_tick(self.root)
  folders=list((self.root/'review/retarget/batches').glob('incremental-*'))
  self.assertEqual(len(folders),1)
  with sqlite3.connect(folders[0]/'batch.sqlite3') as db:
   ids={r[0] for r in db.execute('SELECT motion_id FROM items')}
  self.assertEqual(ids,{'m00000002','m00000003','m00000004'})
  ops.incremental_tick(self.root)
  self.assertEqual(len(list((self.root/'review/retarget/batches').glob('incremental-*'))),1)
 def test_release_has_checksum_and_failure_manifest(self):
  with sqlite3.connect(self.folder/'batch.sqlite3') as db:
   db.execute("UPDATE items SET status='failed',message='fixture' WHERE motion_id!='m00000001'")
   db.execute("UPDATE items SET status='done' WHERE motion_id='m00000001'")
   jid=db.execute("SELECT job_id FROM items WHERE motion_id='m00000001'").fetchone()[0]
  self.q.submit(dict(id=jid,motion_id='m00000001',robot='unitree_g1_23dof',parameters={},force_new=True),dict(username='jfc',role='admin'),__import__('retarget_batch').Store(self.root))
  self.q.update(jid,status='done');folder=self.q.folder/jid;folder.mkdir(exist_ok=True)
  (folder/'result.npz').write_bytes(b'test payload');(folder/'quality.json').write_text('{"export_allowed":true}');(folder/'preview.json').write_text('{"metadata":{}}')
  ops.request_release(self.root,dict(username='jfc',role='admin'),dict(id='v1',batch='test-batch'))
  with patch('shutil.disk_usage',return_value=type('D',(),{'free':100*1024**3})()):ops.release_tick(self.root)
  with closing(ops.connect(self.root)) as db:self.assertEqual(db.execute("SELECT status FROM releases WHERE id='v1'").fetchone()[0],'complete')
  out=self.root/'review/releases/v1'
  manifest=json.loads((out/'manifest.json').read_text());self.assertEqual(len(manifest['items']),1);self.assertEqual(len(manifest['failures']),3)
  with zipfile.ZipFile(out/'dataset.zip') as z:self.assertIsNone(z.testzip());self.assertIn('SHA256SUMS',z.namelist())
  ops.request_release(self.root,dict(username='jfc',role='admin'),dict(id='v2',batch='test-batch',base='v1'))
  with patch('shutil.disk_usage',return_value=type('D',(),{'free':100*1024**3})()):ops.release_tick(self.root)
  self.assertEqual(json.loads((self.root/'review/releases/v2/manifest.json').read_text())['items'],[])
 def test_manual_retry_preserves_original_attempt(self):
  with sqlite3.connect(self.folder/'batch.sqlite3') as db:
   old=db.execute('SELECT job_id FROM items WHERE position=0').fetchone()[0]
   db.execute("UPDATE items SET status='failed' WHERE position=0")
  ops.control_batch(self.root,dict(username='jfc',role='admin'),dict(batch='test-batch',action='retry_failed'))
  with sqlite3.connect(self.folder/'batch.sqlite3') as db:
   new,status=db.execute('SELECT job_id,status FROM items WHERE position=0').fetchone()
   self.assertNotEqual(new,old);self.assertEqual(status,'pending');self.assertEqual(db.execute('SELECT job_id FROM attempts').fetchone()[0],old)
