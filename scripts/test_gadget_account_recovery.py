"""Real owner-scoped Gadget recovery handlers, Auth/Keys/SQLite; no network/GPU."""
from contextlib import closing
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'gadget'))
from test_gadget_round2_integration import Fixture,DirectClient
from review_access import Forbidden
from review_custom import Custom
from gadget_imports import Imports
import motionfarm_mcp as mcp
from workflow_journal import Journal

class AccountServer(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.f=Fixture(self.root);self.c=DirectClient(self.f)
  env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name+'/local'});env.start();self.addCleanup(env.stop)
 def account(self,name,role='user',scopes=None):
  self.f.auth.create(name,'initial-test-password',role);self.f.auth.change_password(name,'initial-test-password','current-test-password')
  c=DirectClient(self.f);c.key=self.f.keys.create(name,{} if scopes is None else {'scopes':scopes})['key'];return c
 def seed(self,owner,key,kind='retarget',created=1):
  if kind=='source':
   with closing(Imports(self.root).db()) as db,db:db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?)',('mimp'+key,owner,'fixture','Private text',created,16))
  elif kind=='generate':
   with closing(Custom(self.root).db()) as db,db:db.execute('INSERT INTO requests(id,owner,created,updated,status) VALUES(?,?,?,?,?)',(key,owner,created,created,'queued'))
  else:
   with closing(self.f.queue.db()) as db,db:db.execute('INSERT INTO jobs(id,owner,motion_id,robot,status,created,updated) VALUES(?,?,?,?,?,?,?)',(key,owner,self.f.mid,'unitree_g1_23dof','queued',created,created))
 def test_identity_stable_across_keys_password_and_distinct_accounts(self):
  first=self.c.request('identity');self.assertNotIn('role',first)
  second=DirectClient(self.f);second.key=self.f.keys.create('alice',{})['key'];self.assertEqual(first,second.request('identity'))
  bob=self.account('bob');self.assertNotEqual(first['account_id'],bob.request('identity')['account_id'])
  self.f.auth.change_password('alice','current-test-password','changed-test-password')
  with self.assertRaises(Forbidden):self.c.request('identity')
  replacement=DirectClient(self.f);replacement.key=self.f.keys.create('alice',{})['key'];self.assertEqual(first,replacement.request('identity'))
 def test_discovery_scopes_revocation_disabled_and_read_only(self):
  c=DirectClient(self.f);c.key=self.f.keys.create('alice',{'scopes':['generate']})['key'];self.assertEqual(c.request('identity')['username'],'alice')
  with self.assertRaises(Forbidden):c.request('recovery?kind=retarget')
  row=self.f.keys.create('alice',{'scopes':['read']});read=DirectClient(self.f);read.key=row['key'];self.assertEqual(read.request('recovery?kind=retarget')['records'],[])
  self.assertEqual(read.request('identity',{})['error'],'Read-only Gadget endpoint')
  self.assertEqual(read.request('recovery',{})['error'],'Read-only Gadget endpoint')
  self.f.keys.revoke('alice',row['id'])
  with self.assertRaises(Forbidden):read.request('recovery?kind=retarget')
  self.f.auth.set_disabled('alice',True)
  with self.assertRaises(Forbidden):self.c.request('identity')
  with self.assertRaises(Forbidden):self.c.request('recovery?kind=retarget')
 def test_all_kinds_owner_only_even_admin_and_bounded_pagination(self):
  admin=self.account('boss','admin');bob=self.account('bob')
  with closing(Imports(self.root).db()) as db,db:db.execute('DELETE FROM imports')
  for kind in ('retarget','generate','source'):
   def key(i):return 'generated-'+str(i) if kind=='generate' else str(i)*32
   for owner,i in [('alice',1),('alice',2),('bob',3),('boss',4)]:self.seed(owner,key(i),kind)
   one=self.c.request('recovery?kind='+kind+'&limit=1');self.assertEqual(len(one['records']),1);self.assertIsNotNone(one['next_after'])
   from urllib.parse import quote
   two=self.c.request('recovery?kind='+kind+'&limit=1&after='+quote(one['next_after']));self.assertIsNone(two['next_after'])
   self.assertEqual({r['id'] for r in one['records']+two['records']},{key(1),key(2)})
   self.assertEqual([r['id'] for r in admin.request('recovery?kind='+kind)['records']],[key(4)])
   self.assertEqual([r['id'] for r in bob.request('recovery?kind='+kind)['records']],[key(3)])
   self.assertNotIn('Private text',json.dumps(one))
 def test_new_client_rotation_finds_same_journal_without_new_job(self):
  args=dict(id='8'*32,motion_id=self.f.mid,robot='unitree_g1_23dof');mcp.invoke('retarget_motion',args,self.c)
  prior=Journal(self.c).root;self.f.auth.change_password('alice','current-test-password','changed-test-password')
  new=DirectClient(self.f);new.key=self.f.keys.create('alice',{})['key']
  result=mcp.invoke('workflow_recovery',{'verify_identity':True},new)
  self.assertEqual(result['records'][0]['id'],args['id']);self.assertEqual(Journal(new).root,prior)
  mcp.invoke('retarget_motion',args,new)
  with closing(self.f.queue.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
 def test_expired_unbound_v08_history_discovered_without_reading_other_directories(self):
  self.seed('alice','9'*32);old=Journal(self.c);old.update('retarget','9'*32,submission='uncertain',arguments_sha256='a'*64);old_path=old.path('retarget','9'*32);before=old_path.read_bytes()
  self.account('bob');self.seed('bob','b'*32)
  self.f.auth.change_password('alice','current-test-password','changed-test-password')
  new=DirectClient(self.f);new.key=self.f.keys.create('alice',{})['key']
  self.assertEqual(mcp.invoke('workflow_recovery',{'verify_identity':True},new)['records'],[])
  result=mcp.invoke('workflow_recovery',{'discover':True,'kind':'retarget'},new)
  self.assertEqual([r['id'] for r in result['records']],['9'*32]);self.assertNotIn('arguments_sha256',result['records'][0])
  self.assertEqual(old_path.read_bytes(),before);self.assertNotIn(old.root.name,Journal(new).legacy_scopes())
  self.assertEqual(mcp.invoke('get_job',{'kind':'retarget','id':'9'*32},new)['status'],'queued')
  self.assertTrue(all(value is None for endpoint,value in new.calls))
 def test_recreated_username_does_not_inherit_old_account_identity(self):
  prior=self.c.request('identity')
  with closing(self.f.auth.connect()) as db,db:db.execute("DELETE FROM users WHERE username='alice'")
  new=self.account('alice');current=new.request('identity')
  self.assertEqual(prior['server_id'],current['server_id']);self.assertNotEqual(prior['account_id'],current['account_id'])
 def test_invalid_discovery_inputs_and_other_job_still_denied(self):
  for query in ('kind=other','kind=retarget&limit=101','kind=retarget&limit=0','kind=retarget&after=bad','kind=retarget&after=[true,"x"]'):
   with self.subTest(query=query),self.assertRaises(ValueError):self.c.request('recovery?'+query)
  bob=self.account('bob');self.seed('alice','7'*32)
  with self.assertRaises(Forbidden):bob.request('retarget?id='+'7'*32)
  self.assertEqual(bob.request('recovery?kind=retarget')['records'],[])

if __name__=='__main__':unittest.main()
