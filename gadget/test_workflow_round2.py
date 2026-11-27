"""v0.8 operator contracts; fake transport only, no network or model calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import motionfarm as cli
import motionfarm_mcp as mcp
from workflow_journal import Journal

class Client:
 url='https://example.invalid'
 key='fixture-key-not-a-real-credential'
 def __init__(self):self.calls=[];self.import_error=None;self.import_reply=None
 def request(self,endpoint,value=None,binary=False):
  if endpoint=='identity':return dict(schema_version='gadget-account-v1',server_id='1'*32,account_id='2'*32,username='alice')
  self.calls.append((endpoint,value))
  if binary:return b'synthetic-fixture'
  if endpoint=='import':
   if self.import_error:raise self.import_error
   return self.import_reply or {'motion_id':'mimp'+value['id']}
  if endpoint.startswith('motion?'):return {'motion_id':endpoint.split('=',1)[1],'text':'Fixture source motion','state':'done'}
  return {'ok':True}
 def wait(self,*args):return {'status':'done'}

class WorkflowRound2(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
  env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name});env.start();self.addCleanup(env.stop)
 def test_mcp_force_new_explicit_and_false_keeps_legacy_journal_hash(self):
  c=Client();args=dict(id='1'*32,motion_id='m00000001',robot='unitree_g1_23dof',parameters={'smooth_weight':4})
  mcp.invoke('retarget_motion',args,c)
  mcp.invoke('retarget_motion',dict(args,force_new=False),c)
  self.assertNotIn('force_new',c.calls[-1][1])
  fresh=dict(args,id='2'*32,force_new=True);mcp.invoke('retarget_motion',fresh,c);self.assertIs(c.calls[-1][1]['force_new'],True)
  for bad in (1,'true',None):
   with self.assertRaises(ValueError):mcp.invoke('retarget_motion',dict(args,force_new=bad),c)
 def test_cli_force_new_forwarded_only_when_requested(self):
  for fresh in (False,True):
   c=Client();argv=['motionfarm.py','retarget','--id','1'*32,'--motion','m00000001','--robot','unitree_g1_23dof']+(['--force-new'] if fresh else [])
   with patch.object(cli,'Client',return_value=c),patch('sys.argv',argv),patch('sys.stdout',new=io.StringIO()):cli.main()
   self.assertEqual(c.calls[-1][1].get('force_new',False),fresh)
   if not fresh:self.assertNotIn('force_new',c.calls[-1][1])
 def manifest(self,items):
  p=self.root/'manifest.json';p.write_text(json.dumps({'workflow':'retarget','robot':'unitree_g1_23dof','items':items}));return p
 def test_variants_distinct_stable_ids_and_explicit_computation(self):
  items=[dict(id=name,motion_id='m00000001',parameters={'smooth_weight':weight},force_new=True) for name,weight in [('baseline',.2),('variant',4)]]
  p=self.manifest(items);c=Client();cli.run_manifest(c,p,self.root/'out',1)
  first=[v for e,v in c.calls if e=='retarget'];self.assertEqual(len({v['id'] for v in first}),2);self.assertTrue(all(v['force_new'] is True for v in first))
  cli.run_manifest(c,p,self.root/'out',1);all_posts=[v for e,v in c.calls if e=='retarget'];self.assertEqual([v['id'] for v in all_posts[:2]],[v['id'] for v in all_posts[2:]])
 def test_existing_state_uses_exact_legacy_retarget_id(self):
  item=dict(id='old-item',motion_id='m00000001');p=self.manifest([item]);out=self.root/'out';out.mkdir();run_id=uuid.uuid4().hex;old=uuid.uuid5(uuid.UUID(run_id),'m00000001:unitree_g1_23dof').hex
  state=dict(manifest_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),run_id=run_id,items={'old-item':{'motions':{'m00000001':{'retarget_id':old}}}})
  (out/'workflow-state.json').write_text(json.dumps(state));c=Client();cli.run_manifest(c,p,out,1)
  self.assertEqual(next(v['id'] for e,v in c.calls if e=='retarget'),old)
  self.assertNotEqual(old,cli.retarget_identity(run_id,'old-item','m00000001','unitree_g1_23dof',{}))
 def test_already_conflicting_legacy_state_fails_before_sending(self):
  items=[dict(id=name,motion_id='m00000001',parameters={'smooth_weight':weight}) for name,weight in [('baseline',.2),('variant',4)]]
  p=self.manifest(items);out=self.root/'out';out.mkdir();state=dict(manifest_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),run_id=uuid.uuid4().hex,items={i['id']:{'motions':{'m00000001':{'retarget_id':'a'*32}}} for i in items})
  raw=json.dumps(state);(out/'workflow-state.json').write_text(raw);c=Client()
  with self.assertRaisesRegex(ValueError,'Legacy workflow state'):cli.run_manifest(c,p,out,1)
  self.assertEqual(c.calls,[]);self.assertEqual((out/'workflow-state.json').read_text(),raw)
 def test_import_lost_ack_contains_exact_resume_before_send(self):
  c=Client();key='a'*32;p=self.root/'source.npz';p.write_bytes(b'fixture');original=c.request
  def check(endpoint,value=None,binary=False):
   if endpoint=='import':
    record=Journal(c).read('source',key);self.assertEqual(record['server_motion_id'],'mimp'+key);self.assertEqual(record['submission'],'uncertain')
    raise TimeoutError('lost acknowledgement')
   return original(endpoint,value,binary)
  c.request=check
  with self.assertRaises(TimeoutError):mcp.invoke('import_motion',dict(id=key,file=str(p),text='Fixture'),c)
  record=mcp.invoke('workflow_recovery',dict(kind='source',id=key),c)['record'];self.assertEqual(record['resume'],dict(tool='get_motion',arguments={'motion_id':'mimp'+key}))
  mcp.invoke(record['resume']['tool'],record['resume']['arguments'],c)
  self.assertTrue(Journal(c).read('source',key)['resource_observed'])
 def test_import_ack_identity_checked_without_changing_request_id(self):
  c=Client();key='a'*32;p=self.root/'source.npz';p.write_bytes(b'fixture')
  args=dict(id=key,file=str(p),text='Fixture');c.import_reply={'motion_id':'mimp'+'b'*32}
  with self.assertRaisesRegex(ValueError,'acknowledgement motion_id differs'):mcp.invoke('import_motion',args,c)
  self.assertEqual(Journal(c).read('source',key)['submission'],'uncertain')
  c.import_reply=None;mcp.invoke('import_motion',args,c);record=Journal(c).read('source',key)
  self.assertEqual(record['id'],key);self.assertTrue(record['resource_identity_verified']);self.assertEqual(record['submission'],'acknowledged')
 def test_old_import_records_get_cues_offline_without_rewrite(self):
  c=Client();j=Journal(c);key='c'*32;p=j.path('source',key);raw=json.dumps(dict(kind='source',id=key,submission='uncertain'));p.write_text(raw)
  direct=j.recover('source',key)['record'];listed=j.recover()['records'][0]
  self.assertEqual(direct['server_motion_id'],'mimp'+key);self.assertEqual(direct['resume'],listed['resume']);self.assertEqual(c.calls,[]);self.assertEqual(p.read_text(),raw)

if __name__=='__main__':unittest.main()
