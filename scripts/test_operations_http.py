import unittest
from test_review_access import AccessTests
class HTTP(unittest.TestCase):
 def setUp(self):self.f=AccessTests();self.f.setUp()
 def tearDown(self):self.f.tearDown()
 def test_admin_status_and_user_denial(self):
  self.assertEqual(self.f.call('/api/operations','alice')[0],403)
  code,data=self.f.call('/api/operations','jfc');self.assertEqual(code,200,data);self.assertEqual(data['slot_limit'],16)
 def test_label_is_attributed_and_versioned(self):
  sha=self.f.fixture.store.detail(self.f.fixture.mid)['location']['sha256']
  value=dict(motion_id=self.f.fixture.mid,check='penetration',verdict='normal',notes='fixture',revision=0,source_sha256=sha)
  code,data=self.f.call('/api/research/calibration','alice',value);self.assertEqual(code,200,data)
  from motion_operations import evidence
  labels=evidence(self.f.root,self.f.fixture.mid)['labels'];self.assertEqual(labels[0]['reviewer'],'alice');self.assertTrue(labels[0]['source_sha256'])
 def test_incremental_and_invalid_release(self):
  code,data=self.f.call('/api/operations/incremental','jfc',{'enabled':False});self.assertEqual(code,200,data)
  self.assertEqual(self.f.call('/api/operations/releases','jfc',{'id':'../x','batch':'x'})[0],400)
if __name__=='__main__':unittest.main()
