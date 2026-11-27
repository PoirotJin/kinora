"""Selected-window invariants, rotation geometry and fail-closed seam checks."""
import copy
from pathlib import Path
import hashlib
import tempfile
import unittest
from types import SimpleNamespace
import numpy as np
from scipy.spatial.transform import Rotation
from g2h.kimodo_edit import window, compose, canonical_to_soma, read_source
from g2h.kimodo_adapter import fk, BODY_MAP
from g2h.motion_assets import validate_motion
from test_kimodo_adapter import fixture as rig_fixture


def fixture(frames=90):
    _, _, rest, parents = rig_fixture()
    local = np.broadcast_to(np.eye(3), (frames,52,3,3)).copy()
    points, _ = fk(local, rest, parents)
    root = np.column_stack((np.zeros(frames),np.ones(frames),np.arange(frames)*.005)).astype('float32')
    arrays = {'fps':np.array(30), 'native_keypoints3d':points.astype('float32'),
              'native_transl':root,'smpl_poses':np.zeros((frames,156),dtype='float32')}
    return arrays, rest, parents


def generated(source, plan, rest, parents, angle=.35):
    n=plan['frames'];local=np.broadcast_to(np.eye(3),(n,52,3,3)).copy()
    local[:,16]=Rotation.from_euler('z',np.full(n,angle)).as_matrix()
    points,_=fk(local,rest,parents)
    return {'fps':np.array(30),'native_keypoints3d':points.astype('float32'),
            'native_transl':source['native_transl'][plan['start']:plan['end']].copy(),
            'smpl_poses':Rotation.from_matrix(local.reshape(-1,3,3)).as_rotvec().reshape(n,156).astype('float32')}


class EditGeometryTests(unittest.TestCase):
    def test_exterior_and_cut_derivatives_are_exact(self):
        source,rest,parents=fixture();before=copy.deepcopy(source);plan=window(90,20,70)
        result,report=compose(source,generated(source,plan,rest,parents),20,70,plan,rest,parents)
        self.assertTrue(report['passed'],report)
        for key in ('native_keypoints3d','native_transl','smpl_poses'):
            self.assertEqual(result[key][:23].tobytes(),source[key][:23].tobytes())
            self.assertEqual(result[key][67:].tobytes(),source[key][67:].tobytes())
            np.testing.assert_array_equal(before[key],source[key])
        self.assertGreater(report['max_rotation_change_deg'],10)
        validate_motion(dict(result,rest_joints=rest,parents=parents))

    def test_start_end_and_full_selection(self):
        for start,end in ((0,45),(45,90),(0,90)):
            source,rest,parents=fixture();plan=window(90,start,end)
            result,report=compose(source,generated(source,plan,rest,parents),start,end,plan,rest,parents)
            self.assertTrue(report['passed'],report)
            self.assertEqual(report['boundaries']['start']['neighbor'],start>0)
            self.assertEqual(report['boundaries']['end']['neighbor'],end<90)

    def test_short_and_long_windows_have_explicit_bounds(self):
        self.assertEqual(window(90,40,52)['frames'],42)
        self.assertEqual(window(600,40,340)['frames'],300)
        for args in ((90,0,11),(400,0,301),(20,0,20),(90,True,20),(90,80,91)):
            with self.assertRaises(ValueError):window(*args)

    def test_teleport_is_quarantinable_even_when_exterior_is_exact(self):
        source,rest,parents=fixture();plan=window(90,20,70);patch=generated(source,plan,rest,parents)
        patch['native_transl'][:,0]+=20
        _,report=compose(source,patch,20,70,plan,rest,parents)
        self.assertFalse(report['passed'])
        self.assertTrue(report['outside_selection_bitwise_equal'])

    def test_noop_is_not_presented_as_semantic_repair(self):
        source,rest,parents=fixture();plan=window(90,20,70)
        _,report=compose(source,generated(source,plan,rest,parents,0),20,70,plan,rest,parents)
        self.assertFalse(report['passed'])

    def test_unknown_motion_fields_rejected_raw_evidence_retained(self):
        source,rest,parents=fixture();plan=window(90,20,70);patch=generated(source,plan,rest,parents)
        source['kimodo_original']=np.arange(90,dtype='float32')
        result,_=compose(source,patch,20,70,plan,rest,parents)
        np.testing.assert_array_equal(result['kimodo_original'],source['kimodo_original'])
        source['unknown_positions']=np.zeros((90,3),dtype='float32')
        with self.assertRaises(ValueError):compose(source,patch,20,70,plan,rest,parents)

    def test_three_dimensional_pose_arrays_and_aliases_stay_consistent(self):
        source,rest,parents=fixture();source['smpl_poses']=source['smpl_poses'].reshape(90,52,3)
        source['smpl_trans']=source['native_transl'].copy()
        source['native_root_rotations_mat']=np.broadcast_to(np.eye(3),(90,3,3)).astype('float32').copy()
        source['native_rot6d']=np.broadcast_to(np.eye(3)[:,:2].reshape(6),(90,52,6)).astype('float32').copy()
        plan=window(90,20,70);result,report=compose(source,generated(source,plan,rest,parents),20,70,plan,rest,parents)
        validate_motion(result);self.assertTrue(report['passed'])

    def test_source_file_hash_is_checked(self):
        source,_,_=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.npz';np.savez(path,**source)
            request={'source':str(path),'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'start_frame':20,'end_frame':70}
            self.assertEqual(read_source(request)[1]['frames'],80)
            request['source_sha256']='0'*64
            with self.assertRaises(ValueError):read_source(request)

    def test_inverse_mapping_uses_current_canonical_rotations(self):
        class Tensor:
            def __init__(self,a):self.a=a
            def detach(self):return self
            def cpu(self):return self
            def numpy(self):return self.a
        source,rest,parents=fixture();plan=window(90,0,90)
        source=generated(source,plan,rest,parents)
        skeleton=SimpleNamespace(bone_order_names=list(BODY_MAP),joint_parents=Tensor(parents[:22]),neutral_joints=Tensor(rest[:22]))
        positions,rotations=canonical_to_soma(source,rest,parents,skeleton)
        local=Rotation.from_rotvec(source['smpl_poses'].reshape(-1,3)).as_matrix().reshape(90,52,3,3)
        points,glob=fk(local,rest,parents)
        np.testing.assert_allclose(rotations,glob[:,:22],atol=1e-7)
        np.testing.assert_allclose(positions,points[:,:22]+source['native_transl'][:,None],atol=1e-7)



class FollowRootTests(unittest.TestCase):
    """Splice gaps add a Root2D path on the free frames; ordinary edits do not."""
    def build(self, follow):
        import sys
        from unittest.mock import patch
        from g2h.kimodo_edit import constraints
        class Recorder:
            def __init__(self, *args): self.args = args
        class FullBody(Recorder): pass
        class Root2D(Recorder): pass
        fake_torch = SimpleNamespace(as_tensor=lambda a, dtype=None, device=None: np.asarray(a), long='long', float32='f32')
        fake = SimpleNamespace(FullBodyConstraintSet=FullBody, Root2DConstraintSet=Root2D)
        source, rest, parents = fixture()
        source['native_transl'][:, 0] = np.sin(np.arange(90) / 20)  # a curved planned path
        plan = window(90, 30, 60)
        if follow: plan['follow_root'] = True
        skeleton = SimpleNamespace(root_idx=0)
        soma = lambda arrays, *a: (arrays['native_keypoints3d'][:, :22] - arrays['native_keypoints3d'][:, :1]
                                   + arrays['native_transl'][:, None] + rest[0], np.zeros((len(arrays['native_transl']), 22, 3, 3)))
        with patch.dict(sys.modules, {'torch': fake_torch, 'kimodo': SimpleNamespace(), 'kimodo.constraints': fake}), \
                patch('g2h.kimodo_edit.canonical_to_soma', side_effect=soma):
            sets = constraints(source, plan, SimpleNamespace(skeleton=skeleton, device='cpu'), rest, parents)[0]
        return sets, source, plan, rest

    def test_root_path_on_free_frames_only_when_requested(self):
        sets, _, _, _ = self.build(False)
        self.assertEqual([type(s).__name__ for s in sets], ['FullBody'])
        sets, source, plan, rest = self.build(True)
        self.assertEqual([type(s).__name__ for s in sets], ['FullBody', 'Root2D'])
        frozen, free = sets[0].args[1], sets[1].args[1]
        self.assertEqual(sorted(np.concatenate([frozen, free]).tolist()), list(range(plan['frames'])))
        self.assertEqual(free[0], plan['left'] + plan['guard_left']); self.assertEqual(free[-1], plan['right'] - plan['guard_right'] - 1)
        expected = (source['native_transl'][plan['start']:plan['end']] + rest[0])[free][:, [0, 2]]
        np.testing.assert_allclose(sets[1].args[2], expected, atol=1e-6)
        self.assertEqual(sets[1].args[2].shape, (len(free), 2))

    def test_read_source_accepts_only_a_true_follow_flag(self):
        source, _, _ = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.npz'; np.savez(path, **source)
            edit = {'source': str(path), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'start_frame': 20, 'end_frame': 70}
            self.assertNotIn('follow_root', read_source(edit)[1])
            self.assertTrue(read_source(dict(edit, follow_root=True))[1]['follow_root'])
            for bad in (dict(edit, follow_root=False), dict(edit, follow_root=1), dict(edit, other=True)):
                with self.assertRaises(ValueError): read_source(bad)


if __name__=='__main__':unittest.main()
