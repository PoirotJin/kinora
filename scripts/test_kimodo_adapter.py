"""Kinematic consistency and invalid-input tests, without GPU or model weights."""
import copy
import unittest
import numpy as np
from scipy.spatial.transform import Rotation
from g2h.kimodo_adapter import BODY_MAP, convert, fk


def fixture():
    parents = np.array([-1,0,0,0,1,2,3,4,5,6,7,8,9,9,9,12,13,14,16,17,18,19] + [20]*15 + [21]*15)
    joints = np.zeros((52,3))
    offsets = {1:(.1,-.05,0),2:(-.1,-.05,0),3:(0,.1,0),4:(0,-.4,0),5:(0,-.4,0),
               6:(0,.1,0),7:(0,-.4,0),8:(0,-.4,0),9:(0,.1,0),10:(0,0,.15),11:(0,0,.15),
               12:(0,.1,0),13:(.1,.05,0),14:(-.1,.05,0),15:(0,.1,0),
               16:(.1,0,0),17:(-.1,0,0),18:(.3,0,0),19:(-.3,0,0),20:(.25,0,0),21:(-.25,0,0)}
    for i in range(1,52):
        joints[i] = joints[parents[i]] + offsets.get(i,(0,0,.01))
    local = np.broadcast_to(np.eye(3),(12,52,3,3)).copy()
    local[:,0] = Rotation.from_euler('y',np.linspace(0,.5,12)).as_matrix()
    local[:,4] = Rotation.from_euler('x',np.linspace(0,.2,12)).as_matrix()
    points, glob = fk(local,joints,parents)
    root = np.column_stack((np.linspace(0,1,12),np.ones(12),np.linspace(0,.5,12)))
    source = {'local_rot_mats':local[:,:22].copy(),'global_rot_mats':glob[:,:22].copy(),
              'root_positions':root.copy(),'posed_joints':points[:,:22]+root[:,None]}
    return source,local,joints,parents


class AdapterTests(unittest.TestCase):
    def test_rotation_transfer_and_export_reconstruct_same_fk(self):
        source,expected,joints,parents = fixture()
        original=copy.deepcopy(source)
        output,meta=convert(source,list(BODY_MAP),joints[:22],joints,parents)
        local=Rotation.from_rotvec(output['smpl_poses'].reshape(-1,3)).as_matrix().reshape(12,52,3,3)
        np.testing.assert_allclose(local,expected,atol=1e-6)
        points,glob=fk(local,joints,parents)
        np.testing.assert_allclose(points,output['native_keypoints3d'],atol=1e-6)
        np.testing.assert_allclose(glob[:,0],output['native_root_rotations_mat'],atol=1e-6)
        np.testing.assert_allclose(output['native_transl'][:,[0,2]],source['root_positions'][:,[0,2]],atol=1e-6)
        self.assertEqual(output['native_keypoints3d'].shape,(12,52,3))
        self.assertFalse(meta['dynamics_verified'])
        for k in source:
            np.testing.assert_array_equal(source[k],original[k])
            np.testing.assert_array_equal(output['kimodo_'+k],original[k])

    def test_mapping_uses_names_not_assumed_source_indices(self):
        source,_,joints,parents=fixture()
        order=np.arange(22)[::-1]
        for key in ('local_rot_mats','global_rot_mats','posed_joints'):
            source[key]=source[key][:,order]
        output,_=convert(source,[BODY_MAP[i] for i in order],joints[:22][order],joints,parents)
        np.testing.assert_allclose(output['native_root_rotations_mat'],source['global_rot_mats'][:,-1],atol=1e-6)

    def test_reflection_and_nonfinite_inputs_are_rejected(self):
        for corruption in ('reflection','nan'):
            source,_,joints,parents=fixture()
            source['global_rot_mats'][0,0,0,0]=-1 if corruption=='reflection' else np.nan
            with self.assertRaises(ValueError):
                convert(source,list(BODY_MAP),joints[:22],joints,parents)


if __name__ == '__main__':
    unittest.main()
