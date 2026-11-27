"""Rewrite selection/provenance tests; no external APIs or GPU."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
from review_custom import Custom
from review_auth import Auth
from rewrite_models import public_models,resolve
class RewriteTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);Auth(self.root/'review').create('alice','test-password-123');self.custom=Custom(self.root)
 def tearDown(self):self.tmp.cleanup()
 def body(self,**values):return dict(request_id='rewrite_test_123',description='A person walks forward slowly while swinging both arms freely beside the body.',n=1,duration=2,**values)
 def test_default_and_private_registry(self):
  result=public_models(self.root);self.assertEqual(result['models'][0]['id'],'qwen-default');self.assertNotIn('key_file',result['models'][0]);self.assertNotIn('base_url',result['models'][0])
  with self.assertRaises(ValueError):resolve(self.root,'arbitrary-user-model')
 def test_selection_is_part_of_idempotency(self):
  self.custom.submit(self.body(),'alice')
  with self.assertRaises(ValueError):self.custom.submit(self.body(prompt_mode='exact_english'),'alice')
  with closing(self.custom.db()) as db:self.assertEqual(db.execute('SELECT rewrite_model FROM requests').fetchone()[0],'qwen-default')
 def test_local_original_and_agent_are_recorded(self):
  value=self.body(prompt_mode='exact_english',original_description='向前走',rewrite_agent={'name':'Codex','model':'chosen'})
  self.custom.submit(value,'alice',source='local_agent');self.custom.submit(value,'alice',source='local_agent')
  with closing(self.custom.db()) as db:meta=json.loads(db.execute('SELECT rewrite_meta FROM requests').fetchone()[0])
  self.assertEqual(meta['source'],'local_agent');self.assertEqual(meta['original_description'],'向前走')
  value['rewrite_agent']['model']='changed'
  with self.assertRaises(ValueError):self.custom.submit(value,'alice',source='local_agent')
 def test_legacy_exact_request_can_resume_through_gadget(self):
  value=self.body(prompt_mode='exact_english')
  self.custom.submit(value,'alice')
  self.assertEqual(self.custom.submit(value,'alice',source='local_agent')['request_id'],value['request_id'])
 def test_disabled_model_and_bad_metadata(self):
  with self.assertRaises(ValueError):self.custom.submit(self.body(rewrite_model='missing'),'alice')
  with self.assertRaises(ValueError):self.custom.submit(self.body(rewrite_agent={'key':'secret'}),'alice')
if __name__=='__main__':unittest.main()
