"""Bridge stage budgeting and model-facing global constraints without a GPU."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from g2h.kimodo_bridge import stages, conditioning, anchor_report, forward_path, forward_report
from g2h.kimodo_edit import window, valid_request, compose
from test_kimodo_edit import fixture, generated


class BridgeTests(unittest.TestCase):
    def test_budget_and_legacy_flags(self):
        for count in (120, 121, 150, 269, 270, 450, 540):
            plan = window(count+60, 30, 30+count, staged_bridge=True)
            parts = stages(plan)
            self.assertEqual(len(parts), 4)
            self.assertEqual(sum(p['frames'] for p in parts), plan['frames'])
            self.assertTrue(all(30 <= p['frames'] <= 270 for p in parts))
        with self.assertRaises(ValueError): stages(window(150, 15, 105))
        edit = dict(source='x', source_sha256='x', start_frame=30, end_frame=180)
        self.assertTrue(valid_request(edit))
        self.assertTrue(valid_request(dict(edit, follow_root=True, staged_bridge=True)))
        for extra in ({'staged_bridge': True}, {'staged_bridge': 1}, {'other': True}):
            self.assertFalse(valid_request(dict(edit, **extra)))

    def test_sparse_endpoints_stands_path_and_world_origin(self):
        source, rest, parents = fixture(240)
        source['native_transl'][:, 0] += 4
        stand, _, _ = fixture(60)
        stand['native_transl'][:] = [0, 1, 0]
        plan = window(240, 30, 210)
        class Constraint:
            def __init__(self, *args, **kwargs): self.args = args; self.kwargs = kwargs
        torch = SimpleNamespace(as_tensor=lambda x, **kw: np.asarray(x), long='long', float32='float32')
        skeleton = SimpleNamespace(root_idx=0)
        pipeline = SimpleNamespace(skeleton=skeleton, device='cpu')
        def soma(arrays, *args):
            pos = arrays['native_keypoints3d'][:, :22] - arrays['native_keypoints3d'][:, :1]
            pos = pos + arrays['native_transl'][:, None] + rest[0]
            rot = np.broadcast_to(np.eye(3), (*pos.shape[:2], 3, 3)).copy()
            return pos, rot
        with patch.dict(sys.modules, {'torch': torch, 'kimodo.constraints': SimpleNamespace(
                FullBodyConstraintSet=Constraint, Root2DConstraintSet=Constraint, EndEffectorConstraintSet=Constraint)}), \
                patch('g2h.kimodo_edit.canonical_to_soma', side_effect=soma):
            sets, origin, parts, targets = conditioning(source, stand, plan, pipeline, rest, parents)
            indices = sets[0].args[1]
            self.assertEqual(len(indices), 14)
            np.testing.assert_array_equal(indices[:3], np.arange(plan['left']-3, plan['left']))
            np.testing.assert_array_equal(indices[-3:], np.arange(plan['right'], plan['right']+3))
            np.testing.assert_allclose(sets[0].args[2] + origin, targets['positions'])
            free = sets[1].args[1]
            facing = sets[1].kwargs['global_root_heading']
            self.assertEqual(facing.shape, (len(free),2))
            np.testing.assert_allclose(np.linalg.norm(facing,axis=1),1)
            self.assertEqual(len(set(indices) & set(free)), 0)
            self.assertEqual(len(indices)+len(free), plan['frames'])
            cuts = np.cumsum([p['frames'] for p in parts])
            for lo, hi in zip([0, *cuts[:-1]], cuts):
                self.assertTrue(np.any((indices >= lo) & (indices < hi)), 'Kimodo crop must retain a full-body constraint in every stage')
            for cut in (cuts[0], cuts[2]):
                selected = targets['positions'][np.isin(indices, np.arange(cut-3, cut))]
                np.testing.assert_allclose(selected, np.broadcast_to(selected[0], selected.shape))
            # Numerical rejection exposes endpoint misses instead of claiming success.
            shifted = {k: v.copy() for k, v in source.items()}
            shifted['native_transl'][:, 0] += 10
            report = anchor_report(shifted, targets, pipeline, rest, parents)
            self.assertFalse(report['passed'])
            self.assertIn('关键帧', report['failures'][0])

    def test_forward_curve_and_backwards_detection(self):
        curve, angles = forward_path(np.array([0.,0.]), np.array([0.,6.]), .7, .7, 301)
        np.testing.assert_allclose(curve[[0,-1]], [[0,0],[0,6]], atol=1e-12)
        self.assertGreater(curve[:,0].max(),.1)
        self.assertLess(curve[:,0].min(),-.1)
        self.assertTrue(np.all(np.diff(curve[:,1])>0))
        tangent=np.gradient(curve,axis=0)
        forward=np.column_stack((np.sin(angles),np.cos(angles)))
        self.assertTrue(np.all(np.sum(tangent*forward,axis=1)>0))
        straight, facing = forward_path(np.array([0.,0.]),np.array([0.,6.]),0,0,301)
        np.testing.assert_allclose(straight[:,0],0)
        # Rearward destination forces a turn; the generated route still faces travel.
        rear, rear_angles = forward_path(np.array([0.,0.]),np.array([0.,-6.]),0,0,301)
        self.assertTrue(np.all(np.cos(rear_angles)<0))
        arrays,_,_=fixture(301)
        arrays['native_transl'][:,[0,2]]=curve
        points=arrays['native_keypoints3d']
        points[:,1,0]=np.cos(angles);points[:,1,2]=-np.sin(angles)
        points[:,2,0]=-np.cos(angles);points[:,2,2]=np.sin(angles)
        report=forward_report(arrays,{'walk_range':(0,300)})
        self.assertTrue(report['passed'],report)
        points[:,[1,2]]=points[:,[2,1]].copy()
        report=forward_report(arrays,{'walk_range':(0,300)})
        self.assertFalse(report['passed'])
        self.assertAlmostEqual(report['backward_distance_fraction'],1)

    def test_bridge_replaces_gap_and_preserves_exterior(self):
        source, rest, parents = fixture(240)
        plan = window(240, 30, 210); plan['staged_bridge'] = True
        candidate = generated(source, plan, rest, parents, angle=.01)
        result, report = compose(source, candidate, 30, 210, plan, rest, parents)
        self.assertTrue(report['passed'], report)
        for key in source:
            if source[key].ndim and len(source[key]) == 240:
                np.testing.assert_array_equal(result[key][:30], source[key][:30])
                np.testing.assert_array_equal(result[key][210:], source[key][210:])
        self.assertFalse(np.array_equal(result['smpl_poses'][30], source['smpl_poses'][30]))
        self.assertFalse(report['boundaries']['start']['cut_unchanged_through_acceleration'])


if __name__ == '__main__': unittest.main()
