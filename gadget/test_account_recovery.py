"""Account-bound journals with explicit read-only discovery; offline by default."""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import motionfarm_mcp as mcp
from workflow_journal import Journal

class Client:
 url='https://example.invalid'
 def __init__(self,key='key-a',account='a'*32,username='alice'):
  self.key=key;self.account=account;self.username=username;self.calls=[];self.failure=None;self.invalid=None;self.page=[];self.cursor=None;self.recovery_identity=None
 def identity(self):return dict(schema_version='gadget-account-v1',server_id='1'*32,account_id=self.account,username=self.username)
 def request(self,endpoint,value=None,binary=False):
  self.calls.append((endpoint,value))
  if self.failure:raise self.failure
  if endpoint=='identity':return self.invalid if self.invalid is not None else self.identity()
  if endpoint.startswith('recovery?'):return dict(identity=self.recovery_identity or self.identity(),records=self.page,next_after=self.cursor)
  return {'motion_id':'mimp'+value['id']} if endpoint=='import' else {'status':'queued'}

class AccountRecovery(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
  env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name});env.start();self.addCleanup(env.stop)
 def test_new_submission_verifies_once_then_rotated_key_binds_same_account(self):
  a=Client();args=dict(id='b'*32,motion_id='m00000001',robot='unitree_g1_23dof')
  mcp.invoke('retarget_motion',args,a);self.assertEqual([x[0] for x in a.calls],['identity','retarget'])
  b=Client('key-b');before=mcp.invoke('workflow_recovery',{},b)
  self.assertEqual(before['records'],[]);self.assertTrue(before['context']['verify_identity_required']);self.assertEqual(b.calls,[])
  connected=mcp.invoke('workflow_recovery',{'verify_identity':True},b)
  self.assertEqual([r['id'] for r in connected['records']],['b'*32]);self.assertTrue(connected['context']['online_authorization_checked'])
  mcp.invoke('retarget_motion',args,b);self.assertEqual([x[0] for x in b.calls],['identity','retarget'])
  with self.assertRaisesRegex(ValueError,'different arguments'):mcp.invoke('retarget_motion',dict(args,force_new=True),b)
 def test_different_account_cannot_see_or_associate_other_credential(self):
  a=Client();j=Journal(a);j.update('generate','alice-uncertain',submission='uncertain');a_legacy=j.root
  j.verify_account();b=Client('bob-key','b'*32,'bob')
  result=mcp.invoke('workflow_recovery',{'verify_identity':True},b)
  self.assertEqual(result['records'],[]);self.assertNotEqual(Journal(b).root,j.root)
  self.assertNotIn(a_legacy.name,Journal(b).legacy_scopes())
 def test_verified_cache_is_offline_and_not_claimed_live_authorization(self):
  c=Client();j=Journal(c);j.verify_account();j.update('retarget','c'*32,server_status='queued')
  offline=Client();offline.failure=TimeoutError('offline')
  result=mcp.invoke('workflow_recovery',{},offline)
  self.assertEqual(result['records'][0]['id'],'c'*32);self.assertEqual(offline.calls,[])
  self.assertEqual(result['context']['identity_source'],'previously_verified_local_cache');self.assertFalse(result['context']['online_authorization_checked'])
  with self.assertRaises(TimeoutError):mcp.invoke('workflow_recovery',{'verify_identity':True},offline)
  self.assertEqual(mcp.invoke('workflow_recovery',{},offline)['records'][0]['id'],'c'*32)
 def test_unverified_failure_never_reads_other_accounts_or_submits(self):
  a=Client();Journal(a).verify_account();Journal(a).update('generate','owned-old')
  for failure in (TimeoutError('offline'),PermissionError('revoked')):
   c=Client('unknown-key');c.failure=failure
   with self.assertRaises(type(failure)):mcp.invoke('retarget_motion',dict(id='d'*32,motion_id='m00000001',robot='unitree_g1_23dof'),c)
   self.assertEqual([x[0] for x in c.calls],['identity']);self.assertFalse(Journal(c).identity)
   self.assertEqual(mcp.invoke('workflow_recovery',{},c)['records'],[])
 def test_invalid_identity_and_changed_identity_fail_closed(self):
  c=Client();c.invalid={'username':'alice'}
  with self.assertRaises(ValueError):Journal(c).verify_account()
  self.assertFalse(Journal(c).identity);c.invalid=None;j=Journal(c);j.verify_account();j.update('generate','stable-old')
  c.account='d'*32
  with self.assertRaisesRegex(ValueError,'identity changed'):Journal(c).verify_account()
  self.assertEqual(Journal(c).read('generate','stable-old')['id'],'stable-old')
 def test_verified_legacy_links_preserve_bytes_ids_hashes_and_import_cue(self):
  a=Client();old=Journal(a);key='a'*32
  old.update('source',key,submission='uncertain',arguments_sha256='f'*64)
  raw=old.path('source',key).read_bytes();old_root=old.root
  result=mcp.invoke('workflow_recovery',{'verify_identity':True},a)
  row=result['records'][0];self.assertEqual(row['id'],key);self.assertEqual(row['arguments_sha256'],'f'*64)
  self.assertEqual(row['resume'],dict(tool='get_motion',arguments={'motion_id':'mimp'+key}))
  b=Client('new-key');mcp.invoke('workflow_recovery',{'verify_identity':True},b)
  self.assertEqual(Journal(b).read('source',key)['arguments_sha256'],'f'*64)
  self.assertEqual((old_root/old.path('source',key).name).read_bytes(),raw)
 def test_conflicting_legacy_hashes_are_not_silently_overwritten(self):
  a=Client();Journal(a).update('generate','conflict-1',arguments_sha256='a'*64);Journal(a).verify_account()
  b=Client('new-key');Journal(b).update('generate','conflict-1',arguments_sha256='b'*64)
  with self.assertRaisesRegex(ValueError,'Conflicting original'):mcp.invoke('workflow_recovery',{'verify_identity':True},b)
  self.assertEqual(len([p for p in (self.root/'.gadget-journal').glob('*/*.json') if p.parent.name!='bindings']),2)
 def test_explicit_server_discovery_paginates_without_posting(self):
  c=Client();c.page=[dict(kind='source',id='e'*32,server_status='done',source_status='missing')];c.cursor='[1,"mimpeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"]'
  result=mcp.invoke('workflow_recovery',dict(discover=True,kind='source',limit=1),c)
  self.assertEqual([x[0].split('?')[0] for x in c.calls],['identity','recovery']);self.assertTrue(all(v is None for _,v in c.calls))
  self.assertEqual(result['next_after'],c.cursor);self.assertEqual(result['records'][0]['server_motion_id'],'mimp'+'e'*32)
  before=len(c.calls);local=mcp.invoke('workflow_recovery',{},c)
  self.assertEqual(len(c.calls),before);self.assertTrue(local['records'][0]['server_observed'])
  self.assertNotIn('arguments_sha256',local['records'][0])
  self.assertEqual(local['records'][0]['source_status'],'missing')
  del c.page[0]['source_status']
  compatible=mcp.invoke('workflow_recovery',dict(discover=True,kind='source',limit=1),c)
  self.assertEqual(compatible['records'][0]['source_status'],'unverified')
  c.page=[];c.cursor=None;mcp.invoke('workflow_recovery',dict(discover=True,kind='source',after=result['next_after']),c)
  self.assertEqual(parse_qs(urlsplit(c.calls[-1][0]).query)['after'],[result['next_after']])
 def test_discovery_cannot_mix_accounts_or_write_malformed_page(self):
  c=Client();c.page=[dict(kind='retarget',id='f'*32,server_status='queued')]
  c.recovery_identity=dict(c.identity(),account_id='b'*32)
  with self.assertRaisesRegex(ValueError,'differs'):mcp.invoke('workflow_recovery',dict(discover=True,kind='retarget'),c)
  self.assertEqual(Journal(c).recover()['records'],[])
  c.recovery_identity=None;c.page.append(dict(kind='source',id='a'*32,server_status='done'))
  with self.assertRaisesRegex(ValueError,'Invalid owned'):mcp.invoke('workflow_recovery',dict(discover=True,kind='retarget'),c)
  self.assertEqual(Journal(c).recover()['records'],[])
 def test_bindings_never_contain_raw_key_or_payload_and_symlinks_fail(self):
  c=Client('mfg_not-a-real-credential-secret');Journal(c).verify_account()
  for p in (self.root/'.gadget-journal').rglob('*'):
   if p.is_file():self.assertNotIn(c.key,p.read_text())
  b=Client('different-key');j=Journal(b);j.binding_path.symlink_to(Journal(c).binding_path)
  with self.assertRaisesRegex(ValueError,'Unsafe journal'):Journal(b)

if __name__=='__main__':unittest.main()
