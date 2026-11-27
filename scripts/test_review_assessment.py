import unittest
from review_assessment import summarize
class Scores(unittest.TestCase):
 def machine(self,**kwargs):
  d={'source_sha256':'sha','score':.8,'version':'v','state':'needs_review','evidence':{'body_quality':{'checks':{'self_collision_proxy':True,'stance_slide':False,'root_gait_consistency':None}}}};d.update(kwargs);return d
 def test_raw_similarity_not_probability_or_pass(self):
  d=summarize(self.machine(),source_sha='sha');self.assertEqual(d['dimensions'][0]['score'],80);self.assertEqual(d['dimensions'][0]['state'],'uncertain');self.assertEqual(d['state'],'needs_review')
 def test_stale_detector_never_counts_as_pass(self):
  d=summarize(self.machine(state='auto_passed'),source_sha='new');self.assertTrue(all(x['score'] is None for x in d['dimensions']))
 def test_physical_failure_not_offset_by_semantic(self):
  d=summarize(self.machine(score=.99,state='auto_passed'),source_sha='sha');self.assertEqual(d['state'],'needs_review');self.assertEqual(d['dimensions'][2]['score'],0)
 def test_conflicts_and_uncertainty_retained(self):
  opinions=[{'check_name':'penetration','source_sha256':'sha','verdict':'defect'},{'check_name':'semantic','source_sha256':'sha','verdict':'disputed'}]
  d=summarize(self.machine(),opinions,'sha');self.assertTrue(any('冲突' in r for r in d['reasons']));self.assertTrue(any('争议' in r for r in d['reasons']));self.assertTrue(d['human_status_unchanged'])
 def test_old_source_opinion_ignored(self):
  d=summarize(self.machine(),[{'check_name':'semantic','source_sha256':'old','verdict':'normal'}],'sha');self.assertIsNone(d['dimensions'][0]['human_verdict'])
 def test_complete_labels_do_not_write_final_review(self):
  from review_assessment import CHECKS
  opinions=[{'check_name':c,'source_sha256':'sha','verdict':'normal'} for c in CHECKS]
  d=summarize({'source_sha256':'sha','evidence':{}},opinions,'sha');self.assertEqual(d['state'],'human_dimensions_complete');self.assertTrue(d['human_status_unchanged'])
 def test_unknown_not_zero(self):
  d=summarize({},source_sha='sha');self.assertTrue(all(x['score'] is None for x in d['dimensions']));self.assertEqual(d['state'],'needs_review')
if __name__=='__main__':unittest.main()

class QueueTests(unittest.TestCase):
 def test_diversity_privacy_and_human_final_preserved(self):
  import tempfile,pathlib,sqlite3,json,types
  from unittest.mock import patch
  import review_assessment as a
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);(root/'review').mkdir()
   with sqlite3.connect(root/'review/auto_review.sqlite3') as db:
    db.executescript('CREATE TABLE runtime(key,value);CREATE TABLE scores(motion_id,text_id,topic,score,flags,source_sha256,created,version);')
    db.execute("INSERT INTO runtime VALUES('version','v')")
    db.executemany('INSERT INTO scores VALUES(?,?,?,?,?,?,?,?)',[(mid,tid,'walking',.7,'[]','sha',i,'v') for i,(mid,tid) in enumerate([('removed','t0'),('final','t1'),('one','t2'),('two','t2'),('three','t3')])])
   def fake(store,user,mid):
    if mid=='removed':raise ValueError('removed')
    return {'source_sha256':'sha','human_review':{'status':'accepted','motion_sha256':'sha'} if mid=='final' else {},'state':'needs_review','dimensions':[],'reasons':['unknown']}
   with patch.object(a,'detail',side_effect=fake):
    result=a.queue(types.SimpleNamespace(root=root),{'username':'u'},{'limit':20})
   ids=[x['motion_id'] for x in result['items']];self.assertNotIn('removed',ids);self.assertNotIn('final',ids);self.assertEqual(len(ids),2);self.assertTrue(result['human_status_unchanged'])
