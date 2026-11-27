"""Deterministic screening probes; these are not physical-validation benchmarks."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import numpy as np
from g2h.agent_learning import analyze, memory, ground_profile
from review_server import validate_review


def sample(topic='ground_get_up', start=.25, end=1.):
    n=60
    tr=np.zeros((n,3));tr[:,1]=np.linspace(start,end,n)
    p=np.repeat(tr[:,None,:],22,axis=1)
    p[:,[7,8,10,11],1]=0
    return dict(topic=topic,text='A person rises from lying on the floor to standing.',
        motion=dict(fps=30,native_transl=tr,native_keypoints3d=p,
                    native_root_rotations_mat=np.repeat(np.eye(3)[None],n,axis=0)))


class GroundQualityTests(unittest.TestCase):
    def test_rise_and_descent_have_distinct_expectations(self):
        m,flags,_=analyze(sample())
        self.assertNotIn('possible_rise_posture_mismatch',flags)
        self.assertEqual(m['start_posture_proxy'],'low')
        self.assertEqual(m['end_posture_proxy'],'upright_height')
        _,flags,_=analyze(sample('standing_to_ground',1.,.25))
        self.assertNotIn('possible_descent_posture_mismatch',flags)
        _,flags,_=analyze(sample('ground_get_up',.25,.25))
        self.assertIn('possible_rise_posture_mismatch',flags)

    def test_ground_uses_body_floor_and_does_not_flag_intentional_foot_slide(self):
        r=sample('crawling',.25,.25);m=r['motion']
        m['native_transl'][:,0]=np.linspace(0,2,60)
        # Hands below raised feet: foot-only floor would be wrong.
        m['native_keypoints3d'][:,[7,8,10,11],1]=.15
        m['native_keypoints3d'][:,20,1]=0
        metrics,flags,_=analyze(r)
        self.assertEqual(metrics['floor_proxy_source'],'whole_body_joint_minimum_p05')
        self.assertAlmostEqual(metrics['floor_proxy_m'],0)
        self.assertNotIn('possible_foot_slide',flags)
        self.assertFalse(metrics['foot_slide_applicable'])
        self.assertIn('support contacts and balance',metrics['unverified'])

    def test_ordinary_foot_slide_remains(self):
        r=sample('walking',1,1);r['text']='A person walks forward.'
        r['motion']['native_transl'][:,0]=np.linspace(0,2,60)
        m,flags,_=analyze(r)
        self.assertIn('possible_foot_slide',flags)
        self.assertTrue(m['foot_slide_applicable'])

    def test_outlier_detects_brief_floor_excursion_and_joint_speed(self):
        r=sample('floor_movements',.25,.25)
        r['motion']['native_keypoints3d'][30,20,1]=-1
        m,flags,_=analyze(r)
        self.assertIn('possible_floor_excursion',flags)
        self.assertGreater(m['floor_excursion_proxy_m'],.9)
        r=sample('crawling',.25,.25)
        r['motion']['native_transl'][30,1]+=1
        self.assertIn('possible_ground_speed_outlier',analyze(r)[1])

    def test_invalid_arrays_and_nonfinite_are_not_screened(self):
        for field in ('native_transl','native_keypoints3d','native_root_rotations_mat'):
            r=sample();r['motion'][field].flat[0]=np.nan
            with self.assertRaises(ValueError):analyze(r)
        r=sample();r['motion']['fps']=float('nan')
        with self.assertRaises(ValueError):analyze(r)

    def test_screen_does_not_mutate_source_and_feature_shape_stays_compatible(self):
        r=sample();before=r['motion']['native_keypoints3d'].copy()
        _,_,features=analyze(r)
        np.testing.assert_array_equal(before,r['motion']['native_keypoints3d'])
        self.assertEqual(len(features),137)
        self.assertEqual(ground_profile(dict(topic='floor_movements',text='A person does push-ups.')),'floor')

    def test_feedback_preserves_actor_revision_and_motion_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'review').mkdir()
            with sqlite3.connect(root/'prompts.sqlite3') as db:
                db.execute('CREATE TABLE prompts(id TEXT,topic TEXT,item TEXT)')
                db.execute('INSERT INTO prompts VALUES(?,?,?)',('t1','crawling',json.dumps({'text':'A person crawls.'})))
            body=dict(text_id='t1',motion_id='m1',status='rejected',issues=['support_contact'],
                      semantic='partial',reviewer='alice',revision=2,updated_at='2026-09-27',motion_sha256='abc',notes='Hands float')
            with sqlite3.connect(root/'review/reviews.sqlite3') as db:
                db.execute('CREATE TABLE latest(body TEXT)');db.execute('INSERT INTO latest VALUES(?)',(json.dumps(body),))
            result=memory(root,'crawling');example=result['failure_examples'][0]
            self.assertEqual(example['provenance']['reviewer'],'alice')
            self.assertEqual(example['provenance']['revision'],2)
            self.assertEqual(example['provenance']['motion_id'],'m1')
            self.assertIn('untrusted review data',result['instruction'])

    def test_review_reasons_are_supported_and_cannot_claim_good_quality(self):
        body=dict(revision=0,status='rejected',semantic='mismatch',quality='issues',
                  issues=['support_contact','start_end_pose','action_mismatch'],corrected_text='',notes='',reviewer='alice')
        validate_review(body)
        with self.assertRaises(ValueError):validate_review({**body,'quality':'good'})
        with self.assertRaises(ValueError):validate_review({**body,'semantic':'match'})


if __name__=='__main__':unittest.main()
