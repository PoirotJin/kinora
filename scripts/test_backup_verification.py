import json,sqlite3,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from backup_verification import audit_references,verify
class ReferenceTests(unittest.TestCase):
 def test_missing_and_cold_reference(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'topics/walking/index.sqlite3';p.parent.mkdir(parents=True)
   with sqlite3.connect(p) as db:db.execute('CREATE TABLE records(shard INTEGER)');db.executemany('INSERT INTO records VALUES(?)',[(0,),(0,),(1,)])
   report=audit_references(root,{'files':[{'tier':'cold','path':'topics/walking/motions_000000.mf.zip'}]})
   self.assertEqual(report['checked_shards'],2);self.assertEqual(report['missing'],[{'topic':'walking','shard':1}])
   limited=audit_references(root,{'files':[]},max_shards=1);self.assertTrue(limited['truncated']);self.assertFalse(limited['consistent_in_scope'])
 def test_verification_does_not_certify_stale_metadata(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);farm=root/'farm';farm.mkdir();payload=root/'payload';payload.mkdir();metadata=root/'metadata';metadata.mkdir()
   (farm/'payload_backup_config.json').write_text(json.dumps({'target':str(payload)}))
   (farm/'backup_config.json').write_text(json.dumps({'target':str(metadata)}))
   manifest=dict(id='snapshot',created=time.time(),complete=True,files=[dict(tier='hot',path='topics/walking/motions_000000.pkl',bytes=1,sha256='a'*64)])
   (payload/'latest.json').write_text(json.dumps(manifest))
   source=metadata/'db-test/topics/walking/index.sqlite3';source.parent.mkdir(parents=True)
   with sqlite3.connect(source) as db:db.execute('CREATE TABLE records(shard INTEGER)');db.execute('INSERT INTO records VALUES(0)')
   for created,expected in ((0,False),(time.time(),True)):
    (metadata/'latest.json').write_text(json.dumps(dict(id='db-test',created=created)))
    with patch('backup_verification.restore',return_value=dict(verified_files=1)):
     result=verify(farm)
    self.assertEqual(result['complete'],expected)
    self.assertEqual(result['metadata_fresh'],expected)

 def test_payload_verifies_its_pinned_metadata_not_a_newer_snapshot(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);farm=root/'farm';farm.mkdir();payload=root/'payload';payload.mkdir();metadata=root/'metadata';metadata.mkdir()
   (farm/'payload_backup_config.json').write_text(json.dumps({'target':str(payload)}));(farm/'backup_config.json').write_text(json.dumps({'target':str(metadata)}))
   manifest=dict(id='snapshot',created=time.time(),complete=True,files=[],metadata_snapshot=dict(id='db-pinned',created=time.time()))
   (payload/'latest.json').write_text(json.dumps(manifest));pinned=metadata/'db-pinned';pinned.mkdir()
   (pinned/'manifest.json').write_text(json.dumps(dict(id='db-pinned',created=time.time())))
   (metadata/'latest.json').write_text(json.dumps(dict(id='db-newer',created=time.time())))
   with patch('backup_verification.restore',return_value=dict(verified_files=0)):
    result=verify(farm)
   self.assertEqual(result['metadata_snapshot'],'db-pinned');self.assertTrue(result['complete'])

 def test_missing_pinned_metadata_records_incomplete_without_crashing(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);farm=root/'farm';farm.mkdir();payload=root/'payload';payload.mkdir();metadata=root/'metadata';metadata.mkdir()
   (farm/'payload_backup_config.json').write_text(json.dumps({'target':str(payload)}));(farm/'backup_config.json').write_text(json.dumps({'target':str(metadata)}))
   manifest=dict(id='snapshot',created=100.0,complete=True,files=[],metadata_snapshot=dict(id='db-rotated-out',created=50.0))
   (payload/'latest.json').write_text(json.dumps(manifest))
   with patch('backup_verification.restore',return_value=dict(verified_files=0)):
    result=verify(farm)
   self.assertEqual(result['metadata_snapshot'],'db-rotated-out')
   self.assertEqual(result['metadata_created'],50.0)
   self.assertEqual(result['payload_created'],100.0)
   self.assertTrue(result['metadata_missing'])
   self.assertFalse(result['metadata_fresh'])
   self.assertFalse(result['complete'])
   written=json.loads((payload/'verification.json').read_text())
   self.assertTrue(written['metadata_missing']);self.assertFalse(written['complete'])

if __name__=='__main__':unittest.main()
