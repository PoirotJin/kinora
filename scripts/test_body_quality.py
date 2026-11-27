import unittest
import numpy as np
from g2h.body_quality import analyze,segment_distance,intent


def gait(root_moves=True,text='A person walks forward.'):
    n=180;fps=30;t=np.arange(n)/fps
    p=np.zeros((n,52,3));p[:,:,1]=1
    for side,hip,knee,ankle,toe,shoulder,elbow,wrist in [(-1,1,4,7,10,16,18,20),(1,2,5,8,11,17,19,21)]:
        p[:,[hip,knee,ankle,toe],0]=side*.14
        p[:,knee,1]=.55;p[:,[ankle,toe],1]=.05
        p[:,shoulder]=[side*.2,1.5,0];p[:,elbow]=[side*.4,1.3,0];p[:,wrist]=[side*.6,1.1,0]
        phase=(t+(0 if side<0 else .5))%1
        z=np.where(phase<.6,.3-phase,-.3+(phase-.6)*1.5)
        y=.05+np.where(phase<.6,0,.15*np.sin(np.pi*(phase-.6)/.4))
        p[:,ankle,2]=z;p[:,toe,2]=z+.14
        p[:,ankle,1]=y;p[:,toe,1]=y
    p[:,9,1]=1.3;p[:,12,1]=1.55
    tr=np.zeros((n,3));tr[:,2]=t if root_moves else 0
    return dict(topic='walking',text=text,motion=dict(native_keypoints3d=p,native_transl=tr,fps=fps))


class BodyTests(unittest.TestCase):
    def test_segment_cross_parallel_and_degenerate(self):
        a=np.array([[0.,0,0]]);b=np.array([[1.,0,0]])
        self.assertAlmostEqual(segment_distance(a,b,np.array([[.5,-1.,0]]),np.array([[.5,1.,0]]))[0],0)
        self.assertAlmostEqual(segment_distance(a,b,a+[0,1,0],b+[0,1,0])[0],1)
        self.assertAlmostEqual(segment_distance(a,a,b,b)[0],1)

    def test_stationary_root_with_travelling_stride(self):
        q=analyze(gait(False))
        self.assertIn('possible_root_gait_mismatch',q['flags'])
        self.assertIn('possible_stance_sliding',q['flags'])
        self.assertTrue(q['events']['root_gait_mismatch'])

    def test_consistent_root_does_not_slide(self):
        r=gait();copy=r['motion']['native_keypoints3d'].copy();q=analyze(r)
        self.assertNotIn('possible_root_gait_mismatch',q['flags'])
        self.assertNotIn('possible_stance_sliding',q['flags'])
        np.testing.assert_array_equal(copy,r['motion']['native_keypoints3d'])

    def test_treadmill_and_in_place_are_distinct(self):
        q=analyze(gait(False,'A person walks on a treadmill.'))
        self.assertNotIn('possible_root_gait_mismatch',q['flags'])
        self.assertIsNone(q['checks']['stance_slide'])
        q=analyze(gait(False,'A person walks in place.'))
        self.assertIsNone(q['checks']['root_gait_consistency'])
        self.assertEqual(intent('A person walks forward, not on a treadmill.','walking'),'travelling_gait')

    def test_foot_through_opposite_shin_reports_parts_and_frames(self):
        r=gait();p=r['motion']['native_keypoints3d']
        p[30:40,7]=(p[30:40,5]+p[30:40,8])/2
        p[30:40,10]=p[30:40,7]+[0,0,.14]
        q=analyze(r)
        self.assertIn('possible_self_penetration',q['flags'])
        self.assertTrue(any(x['parts']==['left_foot','right_shin'] for x in q['events']['self_collision']))

    def test_floor_actions_do_not_use_upright_gait_rules(self):
        r=gait(False);r['topic']='crawling';q=analyze(r)
        self.assertIsNone(q['checks']['root_gait_consistency'])
        self.assertIsNone(q['checks']['stance_slide'])


if __name__=='__main__':unittest.main()
