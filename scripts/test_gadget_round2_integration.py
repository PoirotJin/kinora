"""Actual Gadget auth/handler/queue/cache integration; no solver/network/model calls."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'gadget'))
from review_auth import Auth
from gadget_keys import Keys
from gadget_imports import Imports
from review_retarget import Retarget
from review_gadget import handle_api
from retarget_cache import identity, seal
import motionfarm as cli
import motionfarm_mcp as mcp

def fixture_import(root,mid,text):
 import numpy as np
 imports=Imports(root);path=imports.folder/(mid+'.npz')
 np.savez_compressed(path,fps=np.array(30),native_keypoints3d=np.zeros((2,52,3)),native_transl=np.zeros((2,3)),smpl_poses=np.zeros((2,156)))
 raw=path.read_bytes()
 with closing(imports.db()) as db,db:db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?)',(mid,'alice',hashlib.sha256(raw).hexdigest(),text,time.time(),len(raw)))

class Fixture:
 def __init__(self,root):
  self.root=Path(root);self.auth=Auth(self.root/'review');self.auth.create('alice','initial-test-password')
  self.auth.change_password('alice','initial-test-password','current-test-password')
  self.keys=Keys(self.root);self.key=self.keys.create('alice',{})['key'];self.queue=Retarget(self.root)
  self.server=types.SimpleNamespace(store=types.SimpleNamespace(root=self.root),retarget=self.queue)
  self.mid='mimp'+'d'*32
  fixture_import(self.root,self.mid,'Synthetic imported motion')
 def complete(self,jid):
  row=self.queue.get(jid,{'username':'alice','role':'user'})
  if row['status']=='done':return
  self.queue.update(jid,status='done',done=2,total=2)
  p=self.queue.folder/jid;p.mkdir(exist_ok=True);(p/'result.npz').write_bytes(b'synthetic fixture; no motion calculation')
  (p/'quality.json').write_text(json.dumps(dict(version='robot-quality-v3',dynamics_required=False,export_allowed=True)))
  (p/'preview.json').write_text(json.dumps({'metadata':{'motion_id':row['motion_id'],'robot':row['robot'],'execution':json.loads(row['execution'])}}))
  seal(p,identity(self.root,row['motion_id'],row['robot'],row['execution']))

class DirectClient:
 url='https://motionfarm-fixture.invalid'
 def __init__(self,fixture):self.fixture=fixture;self.key=fixture.key;self.calls=[]
 def request(self,endpoint,value=None,binary=False):
  self.calls.append((endpoint,value))
  if endpoint.startswith('retarget_report?'):return {'quality':{'status':'fixture-unverified'}}
  class Handler:
   server=self.fixture.server
   headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'}
   def origin_allowed(inner):return True
   def body(inner,**kwargs):return value
   def send(inner,status,body,*args,**kwargs):return body
   def download(inner,path):return Path(path).read_bytes()
  return handle_api(Handler(),urlsplit('/api/gadget/v1/'+endpoint),value is not None)
 def wait(self,kind,jid,timeout):
  assert kind=='retarget';self.fixture.complete(jid);return self.request('retarget?id='+jid)

class Round2Integration(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
  env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name+'/local','KINORA_RETARGET_REQUIRE_DYNAMICS':'0'});env.start();self.addCleanup(env.stop)
  model=patch('retarget_cache.model_signature',return_value={'sha256':'fixture-robot-model'})
  model.start();self.addCleanup(model.stop)
  self.fixture=Fixture(tmp.name);self.client=DirectClient(self.fixture)
 def test_mcp_default_reuses_but_explicit_new_version_is_idempotent(self):
  f=self.fixture;c=self.client;old='1'*32
  c.request('retarget',dict(id=old,motion_id=f.mid,robot='unitree_g1_23dof',parameters={'smooth_weight':.2},force_new=True));f.complete(old)
  old_bytes=(f.queue.folder/old/'result.npz').read_bytes()
  args=dict(id='2'*32,motion_id=f.mid,robot='unitree_g1_23dof',parameters={'smooth_weight':4},parent_job_id=old)
  cached=mcp.invoke('retarget_motion',args,c)
  self.assertEqual(cached['status'],'done');self.assertEqual(cached['reused_from'],old)
  self.assertEqual(json.loads(cached['execution'])['parameters']['smooth_weight'],.2)
  self.assertEqual(json.loads(cached['requested_execution'])['parameters']['smooth_weight'],4)
  self.assertEqual(mcp.invoke('retarget_motion',args,c)['reused_from'],old)
  fresh=dict(args,id='3'*32,force_new=True)
  first=mcp.invoke('retarget_motion',fresh,c);again=mcp.invoke('retarget_motion',fresh,c)
  self.assertEqual(first['status'],'queued');self.assertEqual(first['id'],again['id'])
  self.assertEqual(json.loads(first['execution'])['parameters']['smooth_weight'],4)
  self.assertEqual((f.queue.folder/old/'result.npz').read_bytes(),old_bytes)
  with closing(f.queue.db()) as db:self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0],1)
 def test_cross_parameter_reuse_still_checks_current_robot_and_quality(self):
  f=self.fixture;c=self.client;old='1'*32
  args=dict(motion_id=f.mid,robot='unitree_g1_23dof',parameters={'smooth_weight':4})
  c.request('retarget',dict(id=old,motion_id=f.mid,robot=args['robot'],force_new=True))
  f.complete(old)
  with patch('retarget_cache.model_signature',return_value={'sha256':'other-robot-model'}):
   self.assertEqual(c.request('retarget',dict(args,id='2'*32))['status'],'queued')
  (f.queue.folder/old/'quality.json').write_text('{"export_allowed":true}')
  self.assertEqual(c.request('retarget',dict(args,id='3'*32))['status'],'queued')
 def test_manifest_variants_reach_real_queue_and_download_then_resume(self):
  f=self.fixture;c=self.client;p=self.root/'manifest.json';out=self.root/'out'
  p.write_text(json.dumps({'workflow':'retarget','robot':'unitree_g1_23dof','force_new':True,'items':[{'id':'baseline','motion_id':f.mid,'parameters':{'smooth_weight':.2}},{'id':'variant','motion_id':f.mid,'parameters':{'smooth_weight':4}}]}))
  cli.run_manifest(c,p,out,1)
  ids=[v['id'] for e,v in c.calls if e=='retarget'];self.assertEqual(len(set(ids)),2)
  for label in ('baseline','variant'):self.assertTrue((out/label/f.mid/'reference.npz').is_file())
  cli.run_manifest(c,p,out,1)
  self.assertEqual(ids,[v['id'] for e,v in c.calls if e=='retarget'][2:])
  with closing(f.queue.db()) as db:
   rows=db.execute('SELECT status,execution FROM jobs').fetchall()
  self.assertEqual(len(rows),2);self.assertTrue(all(r['status']=='done' for r in rows))
  self.assertEqual(sorted(json.loads(r['execution'])['parameters']['smooth_weight'] for r in rows),[.2,4])
 def test_uncertain_import_resume_reads_actual_owned_server_motion(self):
  f=self.fixture;c=self.client;identity='a'*32;p=self.root/'upload.npz';p.write_bytes(b'fixture only')
  original=c.request
  def lost_ack(endpoint,value=None,binary=False):
   if endpoint=='import':
    # Fixture models a successful import commit before transport loses its ACK.
    # Numeric NPZ validation and motion computation are not under test here.
    fixture_import(f.root,'mimp'+value['id'],value['text'])
    raise TimeoutError('simulated lost import acknowledgement')
   return original(endpoint,value,binary)
  c.request=lost_ack
  with self.assertRaises(TimeoutError):mcp.invoke('import_motion',dict(id=identity,file=str(p),text='Fixture source'),c)
  recovery=mcp.invoke('workflow_recovery',dict(kind='source',id=identity),c)['record']
  self.assertEqual(recovery['submission'],'uncertain')
  result=mcp.invoke(recovery['resume']['tool'],recovery['resume']['arguments'],c)
  self.assertEqual(result['motion_id'],'mimp'+identity);self.assertEqual(result['state'],'done')
  self.assertEqual(result['owner'],'alice')

if __name__=='__main__':unittest.main()
