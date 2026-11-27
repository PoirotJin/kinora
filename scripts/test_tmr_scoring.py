"""Coordinate adapter tests; run with numpy, no weights needed."""
import unittest
import numpy as np
from g2h.tmr_scoring import world_joints


class AdapterTests(unittest.TestCase):
    def record(self):
        p=np.zeros((61,52,3));p[:,:,1]=np.arange(52)[None,:]*.01
        t=np.zeros((61,3));t[:,0]=np.arange(61)/30
        return dict(motion=dict(native_keypoints3d=p,native_transl=t,fps=30))

    def test_resample_preserves_duration_laterality_and_translation(self):
        r=self.record();world=world_joints(r)
        self.assertEqual(world.shape,(41,22,3))
        np.testing.assert_allclose(world[:,0,0],np.arange(41)/20)
        np.testing.assert_allclose(world[:,1,1]-world[:,2,1],-.01)
        np.testing.assert_allclose(r['motion']['native_keypoints3d'][:,:,0],0)

    def test_native_global_root_is_not_added_twice(self):
        r=self.record();expected=world_joints(r)
        r['motion']['native_keypoints3d']+=r['motion']['native_transl'][:,None,:]
        np.testing.assert_allclose(world_joints(r),expected)

    def test_invalid_or_unsupported_inputs_do_not_score(self):
        for kind in ('nan','fps','shape'):
            r=self.record()
            if kind=='nan':r['motion']['native_transl'][0,0]=np.nan
            if kind=='fps':r['motion']['fps']=0
            if kind=='shape':r['motion']['native_keypoints3d']=np.zeros((61,22,3))
            with self.assertRaises(ValueError):world_joints(r)


if __name__=='__main__':unittest.main()
