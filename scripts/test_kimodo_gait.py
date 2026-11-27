import unittest
import numpy as np
from g2h.kimodo_gait import review

class GaitTests(unittest.TestCase):
    def motion(self):
        points=np.zeros((30,52,3));points[:,1,0]=.1;points[:,2,0]=-.1
        points[:,[7,10],0]=.15;points[:,[8,11],0]=-.15
        return {'native_keypoints3d':points,'native_transl':np.zeros((30,3)),'fps':np.array(30)}

    def test_stationary_and_slide_not_filtered_out_by_speed(self):
        a=self.motion();r=review(a,0,30)
        self.assertEqual(r['max_support_drift_proxy_m'],0)
        a['native_transl'][:,0]=np.linspace(0,.3,30)
        r=review(a,0,30)
        self.assertAlmostEqual(r['max_support_drift_proxy_m'],.3)
        self.assertTrue(r['warnings'])
        self.assertFalse(r['sole_contact_verified'])

    def test_crossing_and_wrap_safe_turn(self):
        a=self.motion();a['native_keypoints3d'][5:15,[7,8]]=a['native_keypoints3d'][5:15,[8,7]].copy()
        r=review(a,0,30);self.assertEqual(r['crossing_intervals'],[{'start_frame':5,'end_frame':15}])
        angles=np.linspace(170,190,30)*np.pi/180
        a=self.motion();a['native_keypoints3d'][:,1,0]=np.cos(angles);a['native_keypoints3d'][:,1,2]=-np.sin(angles)
        a['native_keypoints3d'][:,2]=-a['native_keypoints3d'][:,1]
        self.assertLess(review(a,0,30)['pelvis_yaw_step_max_deg'],1)

    def test_insufficient_contact_is_not_a_clean_bill(self):
        a=self.motion();a['native_keypoints3d'][:,:,1]=np.arange(30)[:,None]/30
        r=review(a,0,30)
        self.assertIsNone(r['max_support_drift_proxy_m']);self.assertTrue(r['warnings'])

if __name__=='__main__':unittest.main()
