"""HTTP/SQLite regressions for execution snapshots, isolation, quality and replay."""
from contextlib import closing
import json,unittest,urllib.error
import numpy as np
from unittest.mock import patch
import test_gadget_v2 as fixture
from review_personal import save
from review_access import claim_owners,Forbidden
from review_collections import change,collections,aliases
from review_tags import change as tag
from review_research import source_quality
from retarget_parameters import validate,execution_matches,schema

class Improvements(unittest.TestCase):
    setUp=fixture.GadgetTests.setUp
    tearDown=fixture.GadgetTests.tearDown
    call=fixture.GadgetTests.call
    user={'username':'alice','role':'user'}
    def preset(self,parameters=None):
        return save(self.store,self.user,dict(id='a'*32,version=0,name='QA',robot='unitree_g1_23dof',notes='',parameters=parameters or {'smooth_weight':1}))
    def test_schema_and_invalid_parameters(self):
        info = self.call('info')
        self.assertEqual(info['parameter_schema'],schema())
        for value in ({'wrist_lock':False},{'smooth_weight':True},{'smooth_weight':float('nan')},{'laplacian_weight':0},[]):
            with self.assertRaises(ValueError):validate(value)
    def test_version_snapshot_idempotency_and_private_preset(self):
        p=self.preset();body=dict(id='b'*32,motion_id=self.valid,robot=p['robot'],preset_id=p['id'],preset_version=1)
        first=self.call('retarget',body);self.assertEqual(json.loads(first['execution'])['parameters']['smooth_weight'],1)
        save(self.store,self.user,dict(id=p['id'],version=1,name='QA2',robot=p['robot'],notes='',parameters={'smooth_weight':2}))
        self.assertEqual(self.call('retarget',body)['execution'],first['execution'])
        with self.assertRaises(urllib.error.HTTPError):self.call('retarget',{**body,'preset_version':2})
        from gadget_keys import Keys
        bob=Keys(self.root).create('bob',{})['key']
        with self.assertRaises(urllib.error.HTTPError) as e:self.call('retarget',{**body,'id':'c'*32},key=bob)
        self.assertEqual(e.exception.code,403)
    def test_design_only_preserved_but_execution_rejected(self):
        p=self.preset({'unknown':4});self.assertEqual(p['execution_mode'],'design_only')
        with self.assertRaises(urllib.error.HTTPError):self.call('retarget',dict(id='b'*32,motion_id=self.valid,robot=p['robot'],preset_id=p['id'],preset_version=1))
    def test_legacy_idempotency_does_not_ignore_new_weights(self):
        default=json.dumps(dict(parameters=validate({}),preset=None))
        self.assertTrue(execution_matches('{}',default))
        self.assertFalse(execution_matches('{}',json.dumps(dict(parameters=validate({'smooth_weight':1}),preset=None))))
    def test_replay_seed_separate_ids_and_parent_permissions(self):
        from review_custom import Custom
        c=Custom(self.root);body=dict(request_id='qa_parent',description='Walk slowly',n=1,duration=2,seed=123)
        c.submit(body,'alice');c.submit({**body,'request_id':'qa_child','parent_request_id':'qa_parent'},'alice')
        with closing(c.db()) as db:rows=db.execute('SELECT seed,inference_seed,lineage FROM requests ORDER BY created').fetchall()
        self.assertNotEqual(rows[0]['seed'],rows[1]['seed']);self.assertEqual([r['inference_seed'] for r in rows],[123,123]);self.assertEqual(json.loads(rows[1]['lineage'])['parent_request_id'],'qa_parent')
        with self.assertRaises(Forbidden):c.submit({**body,'request_id':'qa_badchild','parent_request_id':'qa_parent'},'bob')
        with self.assertRaises(ValueError):c.submit({**body,'seed':124},'alice')
    def test_inference_seed_manifest_does_not_reuse_motion_id(self):
        import MotionFarm as farm
        from pathlib import Path
        from g2h.collection_store import CollectionStore
        item=dict(id='f'*64,topic_id='walking',text='walk',duration_s=2,attributes={},source={},inference_seed_offset=100)
        path=self.root/'manifest.json';path.write_text(json.dumps(dict(schema_version=1,prompts=[item])))
        farm.enqueue_manifest(self.root,path,[10]);item['inference_seed_offset']=99;path.write_text(json.dumps(dict(schema_version=1,prompts=[item])));farm.enqueue_manifest(self.root,path,[11])
        with CollectionStore(self.root/'topics/walking') as store:
            specs=[json.loads(r[0]) for r in store.db.execute("SELECT spec FROM tasks WHERE json_extract(spec,'$.inference_seed') IS NOT NULL")]
        self.assertEqual([s['inference_seed'] for s in specs],[110,110]);self.assertEqual({s['seed'] for s in specs},{10,11})
    def test_collections_private_membership_with_shared_authored_sources(self):
        change(self.store,self.user,dict(id='favorites',motion_id=self.valid))
        self.assertEqual(len(collections(self.store,self.user,{'id':['favorites']})['items']),1)
        self.assertEqual(collections(self.store,{'username':'bob'},{'id':['favorites']})['items'],[])
        claim_owners(self.root,[self.valid],'bob')
        self.assertEqual(collections(self.store,self.user,{'id':['favorites']})['items'][0]['owner'],'bob')
        change(self.store,self.user,dict(id='favorites',motion_id=self.valid))
    def test_combined_search_private_collection_author_quality(self):
        claim_owners(self.root,[self.valid],'alice');change(self.store,self.user,dict(id='favorites',motion_id=self.valid))
        page=self.call('search?author=alice&collection=favorites&quality=not_checked&duration_min=0')
        self.assertEqual([r['motion_id'] for r in page['items']],[self.valid])
        self.assertEqual(self.call('search?author=bob&collection=favorites')['items'],[])
        for query in ('duration_min=nan','duration_max=-1','robot=unknown','quality=passed'):
            with self.assertRaises(urllib.error.HTTPError):self.call('search?'+query)
    def test_alias_preserves_assignment_and_audit(self):
        tag(self.store,self.valid,self.user,dict(group='gait',tag='walking'))
        with self.assertRaises(Forbidden):aliases(self.store,self.user,dict(group='gait',alias='walk',canonical='walking'))
        aliases(self.store,{'role':'admin','username':'admin'},dict(group='gait',alias='walk',canonical='walking'))
        rows=self.call('search?tag_group=gait&tag=walk')['items'];self.assertEqual(rows[0]['tags'][0]['tag'],'walking')
        with closing(self.store.reviews()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tag_events').fetchone()[0],1);self.assertEqual(db.execute('SELECT count(*) FROM tag_alias_events').fetchone()[0],1)
    def test_quality_unknown_and_nonfinite(self):
        motion=dict(native_keypoints3d=np.zeros((3,52,3)),native_transl=np.zeros((3,3)),fps=30)
        report=source_quality(motion,[0,1,0]);self.assertEqual(report['status'],'incomplete');self.assertIsNone(report['checks']['surface_collision']);self.assertIsNone(report['checks']['foot_slide_proxy'])
        motion['native_transl'][0,0]=np.nan;self.assertEqual(source_quality(motion,[0,1,0])['status'],'failed')
    def test_reports_allow_shared_authored_sources(self):
        claim_owners(self.root,[self.valid],'bob')
        self.assertEqual(self.call('report?id='+self.valid)['provenance']['owner'],'bob')
    def test_task_envelope_preserves_original_fields(self):
        body=dict(id='b'*32,motion_id=self.valid,robot='unitree_g1_23dof');self.call('retarget',body)
        row=self.call('retarget?id='+body['id']);self.assertEqual(row['status'],'queued');self.assertFalse(row['task']['terminal']);self.assertEqual(row['task']['poll']['after_seconds'],10)
        self.call('cancel',{'id':body['id']});self.assertTrue(self.call('retarget?id='+body['id'])['task']['terminal'])

if __name__=='__main__':unittest.main()
