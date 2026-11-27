"""API key validity agrees across authentication, listing and admission."""
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from review_auth import Auth
from review_access import Forbidden
from gadget_keys import Keys

class KeyLifecycle(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.auth=Auth(self.root/'review');self.auth.create('alice','initial-test-password')
  self.auth.change_password('alice','initial-test-password','current-test-password');self.keys=Keys(self.root)
 def test_password_change_frees_effective_key_capacity_and_explains_history(self):
  old=[self.keys.create('alice',{'name':'fixture-'+str(i)}) for i in range(10)]
  with self.assertRaisesRegex(ValueError,'10个有效密钥'):self.keys.create('alice',{})
  self.auth.change_password('alice','current-test-password','changed-test-password')
  for key in old:
   with self.assertRaises(Forbidden):self.keys.authenticate(key['key'],'read')
  history=self.keys.list('alice');self.assertEqual(len(history),10)
  self.assertTrue(all(row['status']=='password_changed' and row['active'] is False for row in history))
  self.assertTrue(all('account_salt' not in row and 'digest' not in row and 'key' not in row for row in history))
  new=self.keys.create('alice',{'name':'replacement'})
  self.assertEqual(self.keys.authenticate(new['key'],'read')['username'],'alice')
  self.assertEqual(sum(row['active'] for row in self.keys.list('alice')),1)
 def test_revoked_expired_and_disabled_are_distinct_and_denied(self):
  active=self.keys.create('alice',{});revoked=self.keys.create('alice',{});expired=self.keys.create('alice',{})
  self.keys.revoke('alice',revoked['id'])
  with closing(self.keys.db()) as db,db:db.execute('UPDATE keys SET expires=? WHERE id=?',(time.time()-1,expired['id']))
  listed={row['id']:row for row in self.keys.list('alice')}
  self.assertEqual(listed[active['id']]['status'],'active');self.assertEqual(listed[revoked['id']]['status'],'revoked');self.assertEqual(listed[expired['id']]['status'],'expired')
  self.auth.set_disabled('alice',True)
  self.assertEqual({row['id']:row for row in self.keys.list('alice')}[active['id']]['status'],'account_unavailable')
  for key in [active,revoked,expired]:
   with self.assertRaises(Forbidden):self.keys.authenticate(key['key'],'read')
  with self.assertRaises(Forbidden):self.keys.create('alice',{})

if __name__=='__main__':unittest.main()
