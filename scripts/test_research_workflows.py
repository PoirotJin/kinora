"""Bounded private saved-search and collection export tests; no services or GPU."""
import io,json,sqlite3,tempfile,unittest,zipfile,types,sys
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from review_collections import setup,saved_searches,export_collection
from review_access import Forbidden
class Workflows(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'reviews.sqlite3'
  self.store=types.SimpleNamespace(root=self.root,reviews=lambda:sqlite3.connect(self.path))
  with closing(self.store.reviews()) as db:setup(db)
  self.alice={'username':'alice'};self.bob={'username':'bob'}
 def tearDown(self):self.tmp.cleanup()
 def test_saved_search_private_update_delete(self):
  saved_searches(self.store,self.alice,value={'name':'walk','filters':{'q':'walk','tagged_by':'alice'}})
  self.assertEqual(saved_searches(self.store,self.bob)['items'],[])
  saved_searches(self.store,self.alice,value={'name':'walk','filters':{'q':'run'}})
  self.assertEqual(saved_searches(self.store,self.alice)['items'][0]['filters']['q'],'run')
  saved_searches(self.store,self.bob,value={'name':'walk','action':'delete'})
  self.assertEqual(len(saved_searches(self.store,self.alice)['items']),1)
  saved_searches(self.store,self.alice,value={'name':'walk','action':'delete'})
  self.assertEqual(saved_searches(self.store,self.alice)['items'],[])
 def test_invalid_search_fields(self):
  for filters in ({'owner':'bob'},{'q':['walk']},{'q':'a'*201}):
   with self.assertRaises(ValueError):saved_searches(self.store,self.alice,value={'name':'x','filters':filters})
 def seed(self,count):
  with closing(self.store.reviews()) as db,db:
   db.execute('INSERT INTO motion_collections VALUES(?,?,?,0)',('alice','test','Test'))
   db.executemany('INSERT INTO collection_items VALUES(?,?,?,0)',[('alice','test',f'm{i:08d}') for i in range(count)])
 def test_export_manifest_never_silently_omits(self):
  self.seed(3)
  def detail(store,mid,user):
   if mid=='m00000002':raise Forbidden('private')
   return {'motion_id':mid,'tags':[{'tag':'walk','username':'alice'}],'owner':'alice','review':{'status':'accepted'}}
  def source(store,mid,user):
   if mid=='m00000001':raise ValueError('missing')
   return b'source'
  with patch.dict(sys.modules,{'gadget_library':types.SimpleNamespace(detail=detail,source_npz=source)}):
   raw=export_collection(self.store,self.alice,{'id':['test']})
   with self.assertRaises(Forbidden):export_collection(self.store,self.bob,{'id':['test']})
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   manifest=json.loads(z.read('manifest.json'));self.assertFalse(manifest['complete']);self.assertEqual(len(manifest['entries']),3)
   self.assertEqual([e['asset_status'] for e in manifest['entries']],['included','unavailable','unavailable'])
   self.assertEqual(z.read('motions/m00000000.npz'),b'source')
 def test_collection_bound_before_asset_work(self):
  self.seed(51)
  with patch.dict(sys.modules,{'gadget_library':types.SimpleNamespace(detail=None,source_npz=None)}):
   with self.assertRaises(ValueError):export_collection(self.store,self.alice,{'id':['test']})
class QualityEvidence(unittest.TestCase):
 def test_real_frame_indices_and_unknown_robot_checks(self):
  try:import numpy as np
  except ImportError:self.skipTest('numpy unavailable in local Python')
  from review_research import source_quality
  motion=dict(native_keypoints3d=np.zeros((4,52,3)),native_transl=np.array([[0.,0.,0.],[0.,-.1,0.],[0.,0.,0.],[0.,0.,0.]]),fps=30)
  result=source_quality(motion,[0,0,0])
  self.assertEqual(result['issue_frames']['floor_joint_proxy'],[1])
  self.assertEqual(result['frame_index_base'],0)
  self.assertIsNone(result['checks']['surface_collision'])
  self.assertIn(result['status'],['issues','incomplete'])
if __name__=='__main__':unittest.main()
