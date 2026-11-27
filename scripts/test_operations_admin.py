"""Bounded admin-flow regressions with real SQLite queues, no solver or server."""
from contextlib import closing
import json
import sqlite3
import types
import unittest
from unittest.mock import patch
import test_retarget_batch as fixtures
import motion_operations as ops
import retarget_batch as batch
from review_auth import Auth
from retarget_parameters import validate

class AdminFlows(unittest.TestCase):
 def setUp(self):
  self.space=patch('shutil.disk_usage',return_value=types.SimpleNamespace(free=100*1024**3));self.space.start()
  self.f=fixtures.Tests();self.f.setUp();self.root=self.f.root;self.folder=self.f.folder;self.q=self.f.q
  self.user=dict(username='jfc',role='admin');self.other=dict(username='adminb',role='admin')
  Auth(self.root/'review').create('adminb','temporary-test-password','admin')
 def tearDown(self):self.f.tearDown();self.space.stop()
 def finish(self,folder,status='failed'):
  with closing(batch.connect(folder/'batch.sqlite3')) as db,db:db.execute('UPDATE items SET status=?',(status,))
  batch.publish(folder,json.loads((folder/'manifest.json').read_text()),'complete_with_failures')
 def new(self,name):
  folder=self.folder.parent/name;batch.create(self.root,folder,'jfc');return folder
 def output(self,folder,mid='m00000001'):
  with closing(batch.connect(folder/'batch.sqlite3')) as db,db:
   db.execute("UPDATE items SET status='done' WHERE motion_id=?",(mid,));jid=db.execute('SELECT job_id FROM items WHERE motion_id=?',(mid,)).fetchone()[0]
  self.q.submit(dict(id=jid,motion_id=mid,robot=batch.ROBOT,force_new=True,parameters={}),self.user,batch.Store(self.root));self.q.update(jid,status='done')
  p=self.q.folder/jid;p.mkdir(exist_ok=True);(p/'result.npz').write_bytes(b'fixture only');(p/'quality.json').write_text('{"export_allowed":true}');(p/'preview.json').write_text('{"metadata":{}}')
  return jid
 def test_toggle_preserves_owner_and_parameters(self):
  initial=ops.configure_incremental(self.root,self.user,{'enabled':True});initial['parameters']=validate({'smooth_weight':3})
  with closing(ops.connect(self.root)) as db,db:db.execute("UPDATE settings SET value=? WHERE key='incremental'",(json.dumps(initial),))
  changed=ops.configure_incremental(self.root,self.other,{'enabled':False})
  self.assertEqual(changed['owner'],'jfc');self.assertEqual(changed['parameters'],initial['parameters']);self.assertEqual(changed['updated_by'],'adminb')
 def test_ready_release_bypasses_waiter_and_cancel_preserves_batch(self):
  ready=self.new('ready');self.finish(ready);self.output(ready)
  ops.request_release(self.root,self.user,dict(id='waiting',batch=self.folder.name));ops.request_release(self.root,self.user,dict(id='ready',batch=ready.name))
  ops.release_tick(self.root)
  with closing(ops.connect(self.root)) as db:self.assertEqual(dict(db.execute('SELECT id,status FROM releases')),{'waiting':'waiting_batch','ready':'complete'})
  ops.cancel_release(self.root,self.user,{'id':'waiting'})
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT count(*) FROM items WHERE status='pending'").fetchone()[0],4)
  with self.assertRaises(ValueError):ops.cancel_release(self.root,self.user,{'id':'ready'})
 def test_owner_rejected_before_release_queued(self):
  with self.assertRaises(PermissionError):ops.request_release(self.root,self.other,dict(id='wrong-owner',batch=self.folder.name))
  with closing(ops.connect(self.root)) as db:self.assertEqual(db.execute('SELECT count(*) FROM releases').fetchone()[0],0)
 def test_older_than_thirty_still_scheduled_and_paginated(self):
  for i in range(31):self.finish(self.new('new-%02d'%i))
  ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='resume'))
  with patch.object(ops,'descriptor_backfill'):
   ops.maintenance(self.root)
  with closing(self.q.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
  page=ops.batches(self.root);self.assertEqual(len(page),30);self.assertNotIn(self.folder.name,[x['id'] for x in page]);self.assertIn(self.folder.name,[x['id'] for x in ops.batches(self.root,page[-1]['id'])])
 def test_scheduler_round_robin_has_bounded_work(self):
  for i in range(18):self.new('pending-%02d'%i)
  seen=[]
  with patch.object(ops,'descriptor_backfill'),patch.object(batch,'step',side_effect=lambda root,folder,m:seen.append(folder.name)):
   for i in range(3):
    before=len(seen);ops.maintenance(self.root);self.assertLessEqual(len(seen)-before,8)
  self.assertEqual(len(set(seen)),19)
 def test_uncommitted_prefix_does_not_starve_later_committed(self):
  self.finish(self.folder)
  with closing(sqlite3.connect(self.root/'prompts.sqlite3')) as db,db:
   db.execute('DELETE FROM motion_links');db.executemany('INSERT INTO motion_links VALUES(?,?,?,?,?)',[(i,'t'+str(i),'text'+str(i),'walking',1) for i in range(1,502)])
  with closing(sqlite3.connect(self.root/'topics/walking/index.sqlite3')) as db,db:
   db.execute('DELETE FROM records');db.execute("INSERT INTO records VALUES('t501',0,0,100,'sha501')")
  ops.configure_incremental(self.root,self.user,{'enabled':True});ops.incremental_tick(self.root);ops.incremental_tick(self.root)
  from g2h.farm_index import short_id
  created=list(self.folder.parent.glob('incremental-*'));self.assertEqual(len(created),1)
  with closing(batch.connect(created[0]/'batch.sqlite3')) as db:self.assertEqual(db.execute('SELECT motion_id FROM items').fetchone()[0],short_id(501))
 def test_migration_keeps_success_and_old_manifest_and_is_idempotent(self):
  self.finish(self.folder);self.output(self.folder)
  before=(self.folder/'manifest.json').read_bytes()
  with patch.object(batch,'fingerprint',return_value={'version':'new'}):result=ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'))
  target=self.folder.parent/result['batch'];self.assertEqual(result['total'],3);self.assertEqual(json.loads((target/'manifest.json').read_text())['versions'],{'version':'new'})
  self.assertEqual((self.folder/'manifest.json').read_bytes(),before)
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertEqual(dict(db.execute('SELECT status,count(*) FROM items GROUP BY status')),{'done':1,'migrated':3})
  again=ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'));self.assertEqual(again['batch'],result['batch']);self.assertTrue(again['already_migrated'])
  with closing(self.q.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
 def test_migration_checks_real_jobs_even_stale_batch_state(self):
  row={'id':batch.job_id(self.folder.name,'m00000001'),'motion_id':'m00000001','robot':batch.ROBOT,'parameters':{},'force_new':True}
  self.q.submit(row,self.user,batch.Store(self.root))
  with self.assertRaisesRegex(ValueError,'仍有排队'):ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'))
  self.assertEqual(list(self.folder.parent.glob('migration-*')),[])
 def test_migration_recovers_after_new_manifest_write_interrupted(self):
  self.finish(self.folder) # Terminal failed sources must also be recoverable.
  with patch.object(ops,'_finish_migration',side_effect=OSError('simulated crash after durable plan')):
   with self.assertRaises(OSError):ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'))
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='operator_paused'").fetchone()[0],'1')
  with patch.object(ops,'descriptor_backfill'):ops.maintenance(self.root)
  migrated=list(self.folder.parent.glob('migration-*'));self.assertEqual(len(migrated),1)
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT count(*) FROM items WHERE status='migrated'").fetchone()[0],4)
 def test_migration_recovers_after_target_manifest_is_visible(self):
  import os
  replace=os.replace
  def crash(source,target):
   replace(source,target)
   if target.name=='manifest.json' and target.parent.name.startswith('migration-'):raise OSError('simulated crash after target manifest')
  with patch('os.replace',side_effect=crash):
   with self.assertRaises(OSError):ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'))
  target=list(self.folder.parent.glob('migration-*'))[0]
  with closing(batch.connect(target/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='migration_hold'").fetchone()[0],'1')
  with patch.object(ops,'descriptor_backfill'):ops.maintenance(self.root)
  with closing(batch.connect(target/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='migration_hold'").fetchone()[0],'0')
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertEqual(db.execute("SELECT count(*) FROM items WHERE status='migrated'").fetchone()[0],4)
 def test_migration_rejects_when_maintenance_holds_gate(self):
  import fcntl
  with (self.root/'review/operations.lock').open('a') as gate:
   fcntl.flock(gate,fcntl.LOCK_EX)
   with self.assertRaisesRegex(ValueError,'维护正在运行'):ops.control_batch(self.root,self.user,dict(batch=self.folder.name,action='migrate_remaining'))
  with closing(batch.connect(self.folder/'batch.sqlite3')) as db:self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='migration_plan'").fetchone())
 def test_cancel_does_not_cancel_processing(self):
  ops.request_release(self.root,self.user,dict(id='packing',batch=self.folder.name))
  with closing(ops.connect(self.root)) as db,db:db.execute("UPDATE releases SET status='processing' WHERE id='packing'")
  with self.assertRaises(ValueError):ops.cancel_release(self.root,self.user,{'id':'packing'})
 def test_maintenance_error_does_not_block_batch(self):
  with patch.object(ops,'descriptor_backfill',side_effect=ValueError('bad descriptor')):ops.maintenance(self.root)
  with closing(self.q.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
  with closing(ops.connect(self.root)) as db:self.assertIn('bad descriptor',db.execute("SELECT value FROM settings WHERE key='maintenance_errors'").fetchone()[0])

if __name__=='__main__':unittest.main()
