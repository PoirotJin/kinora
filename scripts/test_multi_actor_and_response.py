"""Tests for Contract A (object response & carry grip) and Contract B (multi-actor & paired scenes)."""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from g2h import carry_grip, human_interaction, multi_actor, object_response, paired_scene
from g2h.human_rig import load
from g2h.kimodo_adapter import BODY_MAP, fk
from g2h.motion_assets import validate_motion


def _standing_walk_motion(frames=90, walk_z=1.2, raise_arms_window=None):
    rest, parents = load()
    local = np.broadcast_to(np.eye(3), (frames, 52, 3, 3)).copy()
    local[:, 16] = Rotation.from_euler('z', -1.4).as_matrix()
    local[:, 17] = Rotation.from_euler('z', 1.4).as_matrix()
    if raise_arms_window is not None:
        s, e = raise_arms_window
        for f in range(s, e):
            local[f, 16] = Rotation.from_euler('y', -1.1).as_matrix() @ Rotation.from_euler('z', -1.2).as_matrix()
            local[f, 17] = Rotation.from_euler('y', 1.1).as_matrix() @ Rotation.from_euler('z', 1.2).as_matrix()
            local[f, 18] = Rotation.from_euler('y', 0.4).as_matrix()
            local[f, 19] = Rotation.from_euler('y', -0.4).as_matrix()
    xyz, _ = fk(local, rest, parents)
    root = np.zeros((frames, 3), dtype=np.float64)
    root[:, 1] = 0.915
    root[:, 2] = np.linspace(0.0, walk_z, frames)
    poses = Rotation.from_matrix(local.reshape(-1, 3, 3)).as_rotvec().reshape(frames, 156)
    arrays = {
        'fps': np.asarray(30),
        'native_keypoints3d': xyz.astype(np.float32),
        'native_transl': (root - rest[0]).astype(np.float32),
        'smpl_poses': poses.astype(np.float32),
        'native_rot6d': local[..., :2].reshape(frames, 52, 6).astype(np.float32),
        'native_root_rotations_mat': local[:, 0].astype(np.float32),
        'smpl_trans': (root - rest[0]).astype(np.float32),
        'smpl_Rh': poses[:, :3].astype(np.float32),
    }
    return arrays, rest, parents


class CarryGripWindowTests(unittest.TestCase):
    def test_fit_window_subwindow_quintic_blend_and_leg_preservation(self):
        arrays, rest, parents = _standing_walk_motion(frames=90, walk_z=0.0)
        obj = {
            'id': 'box-1',
            'center_m': [0.0, 1.0, 0.36],
            'size_m': [0.46, 0.32, 0.34],
            'heading_deg': 0.0,
        }
        anchors = [[0.23, 0.0, -0.05], [-0.23, 0.0, -0.05]]
        updated = carry_grip.fit_window(
            arrays, obj, 25, 65, anchors, rest, parents, ramp_frames=8, joints=(20, 21)
        )
        validate_motion(dict(updated, rest_joints=rest, parents=parents))

        # Outside [25 - 8, 65 + 8) must remain bitwise unchanged.
        for key in ('smpl_poses', 'native_keypoints3d', 'native_transl'):
            np.testing.assert_array_equal(updated[key][:17], arrays[key][:17])
            np.testing.assert_array_equal(updated[key][73:], arrays[key][73:])

        # Legs and root joints (0..15) must remain untouched everywhere.
        orig_poses = arrays['smpl_poses'].reshape(90, 52, 3)
        new_poses = updated['smpl_poses'].reshape(90, 52, 3)
        np.testing.assert_array_equal(new_poses[:, :16], orig_poses[:, :16])
        np.testing.assert_array_equal(updated['native_transl'], arrays['native_transl'])

        # Inside [25, 65), wrists meet anchors cleanly.
        pts = updated['native_keypoints3d'].astype(np.float64)
        world = pts - pts[:, :1] + updated['native_transl'][:, None, :] + rest[0]
        err_l = np.linalg.norm(world[25:65, 20] - (np.array(obj['center_m']) + anchors[0]), axis=1)
        err_r = np.linalg.norm(world[25:65, 21] - (np.array(obj['center_m']) + anchors[1]), axis=1)
        self.assertLess(float(err_l.max()), 1e-4)
        self.assertLess(float(err_r.max()), 1e-4)

        # Ramp transitions smoothly without velocity discontinuities.
        wrist_steps = np.linalg.norm(np.diff(world[16:74, 20], axis=0), axis=1)
        self.assertLess(float(wrist_steps.max()), 0.12)


class ObjectResponseTests(unittest.TestCase):
    def test_couple_grasped_objects_submillimeter_slip_and_place_lowering(self):
        arrays, rest, parents = _standing_walk_motion(frames=90, walk_z=1.5)
        box = {
            'id': 'crate',
            'center_m': [0.0, 0.95, 0.36],
            'size_m': [0.60, 0.40, 0.42],
            'heading_deg': 15.0,
        }
        anchors = [[0.30, 0.0, -0.05], [-0.30, 0.0, -0.05]]
        scene = {
            'kind': human_interaction.VERSION,
            'objects': [box],
            'waypoints': [{'frame': 0, 'xz_m': [0.0, 0.0], 'heading_deg': 0.0}],
            'keyframes': [],
            'contacts': [
                {'start_frame': 20, 'end_frame': 65, 'object_id': 'crate', 'joint_index': 20, 'anchor_m': anchors[0], 'tolerance_m': 0.01},
                {'start_frame': 20, 'end_frame': 65, 'object_id': 'crate', 'joint_index': 21, 'anchor_m': anchors[1], 'tolerance_m': 0.01},
            ],
            'attachments': [{
                'object_id': 'crate', 'start_frame': 20, 'end_frame': 65,
                'mode': 'place', 'joints': [20, 21], 'anchors_m': anchors,
            }],
        }
        updated, updated_scene, audit = object_response.couple_grasped_objects(arrays, scene, rest, parents)
        self.assertTrue(audit['attachments'][0]['passed'], audit)
        self.assertLess(audit['max_slip_m'], 0.001)
        human_interaction.check(updated_scene, 90)
        decoded = human_interaction.decode(updated['scene_spec_json'], 90)
        self.assertEqual(decoded, updated_scene)

        # Verify trajectory holds frame 0 before grasp, tracks hands during [20, 65), and lowers to floor (size_y/2) in [65, 90).
        centers = human_interaction.object_centers(updated_scene['objects'][0], 90)
        np.testing.assert_allclose(centers[0], box['center_m'], atol=1e-5)
        self.assertGreater(float(np.linalg.norm(centers[64] - centers[20])), 0.4)
        self.assertAlmostEqual(float(centers[-1, 1]), 0.20, places=4)

        # Verify < 1 mm wrist-to-anchor slip directly from FK world joints.
        pts = updated['native_keypoints3d'].astype(np.float64)
        world = pts - pts[:, :1] + updated['native_transl'][:, None, :] + rest[0]
        rot = Rotation.from_euler('y', 15.0, degrees=True)
        for j, a in zip((20, 21), anchors):
            err = np.linalg.norm(world[20:65, j] - (centers[20:65] + rot.apply(a)), axis=1)
            self.assertLess(float(err.max()), 0.001)

    def test_couple_grasped_objects_keeps_keyframe_and_waypoint_aligned_on_carry(self):
        # Regression test: when keyframes and waypoints share a frame (e.g. carry or multi_actor co_carry)
        # and generated motion deviates slightly from planned object trajectory, keyframes stay consistent.
        plans = multi_actor.build_multi_plans({
            'duration': 4,
            'objects': [{
                'id': 'crate-box', 'center_m': [0.0, 0.275, 0.0],
                'size_m': [0.65, 0.55, 0.50], 'heading_deg': 0.0,
            }],
            'actors': [
                {'id': 'r1', 'start_xz_m': [0.0, -1.0], 'heading_deg': 0.0},
                {'id': 'r2', 'start_xz_m': [0.0, 1.0], 'heading_deg': 180.0},
            ],
            'interactions': [{
                'kind': 'co_carry', 'actors': [0, 1], 'object_id': 'crate-box',
                'size_m': [0.65, 0.55, 0.50], 'to_xz_m': [1.0, 0.2], 'lift_m': 0.32,
                'start_frame': 25, 'end_frame': 95,
            }],
        })
        sc = plans[0]['scene']
        rest, parents = load()
        indices, roots, headings, _, _ = human_interaction.world_targets(sc, 120)
        full_roots = np.column_stack([np.interp(np.arange(120), indices, roots[:, ax]) for ax in range(3)])
        # Perturb generated root slightly (+3 cm) so coupled box center diverges from orig_centers.
        full_roots[30:95, 0] += 0.03
        full_roots[30:95, 2] -= 0.02
        local = np.zeros((120, 52, 3))
        interp_yaw = np.interp(np.arange(120), indices, np.unwrap(np.deg2rad(headings)))
        local[:, 0] = Rotation.from_rotvec(interp_yaw[:, None] * np.array([0.0, 1.0, 0.0])).as_rotvec()
        key_rot = np.array([k['body_rotvecs'] for k in sc['keyframes']], dtype=float)
        for j in range(1, 22):
            for ax in range(3):
                local[:, j, ax] = np.interp(np.arange(120), indices, key_rot[:, j - 1, ax])
        pts, _ = fk(Rotation.from_rotvec(local.reshape(-1, 3)).as_matrix().reshape(120, 52, 3, 3), rest, parents)
        arrays = {
            'fps': np.asarray(30),
            'native_keypoints3d': pts.astype(np.float32),
            'native_transl': (full_roots - rest[0]).astype(np.float32),
            'smpl_poses': local.reshape(120, 156).astype(np.float32),
        }
        updated, updated_scene, audit = object_response.couple_grasped_objects(arrays, sc, rest, parents)
        self.assertLess(audit['max_slip_m'], 0.001)
        human_interaction.check(updated_scene, 120)
        rep = human_interaction.report(updated, updated_scene, rest, parents)
        self.assertTrue(all(c['passed'] for c in rep['contacts']), rep)
        self.assertTrue(all(k['pelvis_error_m'] <= human_interaction.MAX_KEYFRAME_ERROR_M for k in rep['keyframes']), rep)
        # Verify repair clears release-ramp penetration while preserving coupled contacts and keyframes.
        from g2h.human_interaction_repair import repair
        repaired, _ = repair(updated, updated_scene, rest, parents)
        human_interaction.check(updated_scene, 120)
        rep2 = human_interaction.report(repaired, updated_scene, rest, parents)
        self.assertTrue(rep2['passed'], rep2)

    def test_attach_object_to_motion_auto_detect_and_manual_frames(self):
        # Motion with clear raised-arms grasp plateau [28, 72).
        arrays, rest, parents = _standing_walk_motion(frames=90, walk_z=0.6, raise_arms_window=(28, 72))
        objects = [{
            'id': 'parcel',
            'center_m': [0.0, 0.25, 0.0],  # Deliberately overlaps pelvis at frame 0 to test auto_place!
            'size_m': [0.50, 0.50, 0.44],
            'heading_deg': 0.0,
        }]
        updated, scene_dict, rep = object_response.attach_object_to_motion(
            arrays, objects,
            {'object_id': 'parcel', 'start_frame': None, 'end_frame': None,
             'auto_detect': True, 'mode': 'carry', 'auto_place': False},
            rest, parents,
        )
        validate_motion(updated)
        self.assertTrue(rep['passed'], rep)
        self.assertLess(rep['object_response']['max_slip_m'], 0.001)
        att = scene_dict['attachments'][0]
        self.assertGreaterEqual(att['start_frame'], 20)
        self.assertLessEqual(att['end_frame'], 80)
        # Auto-repositioned in front of pelvis because initial center overlapped pelvis.
        self.assertGreater(scene_dict['objects'][0]['center_m'][2], 0.35)

        # Fallback window when arms stay down + explicit manual frames.
        quiet_arrays, _, _ = _standing_walk_motion(frames=90, walk_z=0.0, raise_arms_window=None)
        _, fallback_scene, fallback_rep = object_response.attach_object_to_motion(
            quiet_arrays, objects,
            {'object_id': 'parcel', 'start_frame': None, 'end_frame': None,
             'auto_detect': True, 'mode': 'lift', 'auto_place': True},
            rest, parents,
        )
        self.assertTrue(fallback_rep['passed'], fallback_rep)
        self.assertEqual(fallback_scene['attachments'][0]['start_frame'], round(0.25 * 89))
        self.assertEqual(fallback_scene['attachments'][0]['end_frame'], round(0.80 * 89))

        _, manual_scene, manual_rep = object_response.attach_object_to_motion(
            quiet_arrays, objects,
            {'object_id': 'parcel', 'start_frame': 18, 'end_frame': 58,
             'auto_detect': False, 'mode': 'follow_hands', 'auto_place': True},
            rest, parents,
        )
        self.assertTrue(manual_rep['passed'], manual_rep)
        self.assertEqual(manual_scene['attachments'][0]['start_frame'], 18)
        self.assertEqual(manual_scene['attachments'][0]['end_frame'], 58)

    def test_collection_kimodo_wires_sole_alignment_and_scene_contact_repairs(self):
        from g2h.generation_backend import KIMODO_MODEL, current_recipe
        from scripts import collection_kimodo

        rest, parents = load()
        soma_names = list(BODY_MAP) + ['LeftToeEnd', 'RightToeEnd']
        soma_parents = np.r_[parents[:22], [10, 11]]
        soma_rest = np.zeros((24, 3), dtype=np.float64)
        soma_rest[:22] = rest[:22]
        for idx in (7, 8, 10, 11):
            soma_rest[idx, 1] -= 0.036
        soma_rest[22] = soma_rest[10] + [0, 0, 0.04]
        soma_rest[23] = soma_rest[11] + [0, 0, 0.04]

        class Tensor:
            def __init__(self, arr):
                self.arr = np.asarray(arr)
            def detach(self):
                return self
            def cpu(self):
                return self
            def numpy(self):
                return self.arr

        class FakeConstraint:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class FakeTorch:
            __version__ = '0.0-test'
            float32 = 'f32'
            long = 'long'
            @staticmethod
            def as_tensor(val, dtype=None, device=None):
                return np.asarray(val)
            @staticmethod
            def isfinite(val):
                return np.isfinite(val)
            @staticmethod
            def set_num_threads(_):
                pass
            @staticmethod
            def set_num_interop_threads(_):
                pass
            class cuda:
                @staticmethod
                def is_available():
                    return True
                @staticmethod
                def synchronize():
                    pass
                @staticmethod
                def manual_seed_all(_):
                    pass
            @staticmethod
            def manual_seed(_):
                pass
            @staticmethod
            def inference_mode():
                return tempfile.TemporaryDirectory()
            @staticmethod
            def load(path, **_):
                return {'encoded': np.zeros((1, 1, 4096), dtype=np.float32)}
            @staticmethod
            def cat(items):
                return items[0]

        frames = 60
        box = {'id': 'crate', 'center_m': [0.0, 0.98, 0.45], 'size_m': [0.46, 0.32, 0.34], 'heading_deg': 0.0}
        scene = {
            'kind': human_interaction.VERSION,
            'objects': [box],
            'waypoints': [{'frame': 0, 'xz_m': [0.0, 0.0], 'heading_deg': 0.0}],
            'keyframes': [{'frame': 0, 'object_id': 'crate', 'pelvis_offset_m': [0.0, -0.07, -0.45],
                           'heading_deg': 0.0, 'body_rotvecs': [[0.0, 0.0, 0.0]] * 21}],
            'contacts': [
                {'start_frame': 15, 'end_frame': 45, 'object_id': 'crate', 'joint_index': 20, 'anchor_m': [0.23, 0.0, -0.05], 'tolerance_m': 0.02},
                {'start_frame': 15, 'end_frame': 45, 'object_id': 'crate', 'joint_index': 21, 'anchor_m': [-0.23, 0.0, -0.05], 'tolerance_m': 0.02},
            ],
            'attachments': [{
                'object_id': 'crate', 'start_frame': 15, 'end_frame': 45,
                'mode': 'lift', 'joints': [20, 21],
                'anchors_m': [[0.23, 0.0, -0.05], [-0.23, 0.0, -0.05]],
            }],
        }

        local_soma = np.broadcast_to(np.eye(3), (frames, len(soma_names), 3, 3)).copy()
        soma_fk, soma_glob = fk(local_soma, soma_rest, soma_parents)
        # SOMA Hips at 0.910 + 0.036 = 0.946 so SOMA soles are at Y=0 during diffusion.
        root_pos = np.tile([0.0, 0.946, 0.0], (frames, 1))
        fake_skel = SimpleNamespace(
            root_idx=0, bone_order_names=soma_names,
            joint_parents=Tensor(soma_parents), neutral_joints=Tensor(soma_rest),
        )

        class FakePipeline:
            fps = 30
            device = 'cpu'
            skeleton = fake_skel
            output_skeleton = fake_skel
            seen_constraints = None
            def __call__(self, **kwargs):
                self.seen_constraints = kwargs.get('constraint_lst')
                return {
                    'local_rot_mats': local_soma[None],
                    'global_rot_mats': soma_glob[None],
                    'root_positions': root_pos[None],
                    'posed_joints': (soma_fk + root_pos[:, None, :])[None],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}):
                spec = dict(current_recipe(), text='A person lifts a crate smoothly with both hands.',
                            duration_s=2.0, seed=11, scene=scene)
            batch = root / 'batch.json'
            feat = root / 'feat.pt'
            motion_out = root / 'out.npz'
            feat.write_bytes(b'stub')
            batch.write_text(json.dumps([{'spec': spec, 'features': str(feat), 'motion': str(motion_out)}]))
            profile = {'model': KIMODO_MODEL}
            collection_kimodo.metadata_path(feat).write_text(json.dumps({'cache_profile': profile}))

            pipe = FakePipeline()
            fake_kimodo = SimpleNamespace(load_model=lambda *a, **kw: pipe)
            fake_sanitize = SimpleNamespace(sanitize_texts=lambda texts: texts)
            fake_constraints = SimpleNamespace(FullBodyConstraintSet=FakeConstraint, Root2DConstraintSet=FakeConstraint)
            def generate():
                with patch.dict(sys.modules, {
                    'torch': FakeTorch,
                    'kimodo': fake_kimodo,
                    'kimodo.sanitize': fake_sanitize,
                    'kimodo.constraints': fake_constraints,
                    'motion_correction': SimpleNamespace(),
                }), patch.object(collection_kimodo, 'cache_profile', return_value=profile), \
                     patch.object(collection_kimodo, 'file_identity', return_value={'name': 'model.safetensors'}):
                    collection_kimodo.run_batch(SimpleNamespace(batch=str(batch), stage='generate', persistent=False))

            generate()

            # Verify FullBodyConstraintSet placed SOMA soles at Y_soma = 0 during diffusion.
            full_body = pipe.seen_constraints[1]
            foot_ids = [soma_names.index(n) for n in ('LeftFoot', 'LeftToeBase', 'RightFoot', 'RightToeBase')]
            self.assertAlmostEqual(float(full_body.args[2][0, foot_ids, 1].min()), 0.0, places=6)

            meta = json.loads(collection_kimodo.metadata_path(motion_out).read_text())
            self.assertAlmostEqual(meta['conversion']['vertical_shift_m'], -0.036, places=6)
            self.assertAlmostEqual(meta['scene']['initial_foot_clearance_m'], 0.0, places=5)
            self.assertTrue(meta['scene']['passed'], meta['scene'])
            self.assertLess(meta['scene']['object_response']['max_slip_m'], 0.001)
            with np.load(motion_out, allow_pickle=False) as saved:
                decoded = human_interaction.decode(saved['scene_spec_json'], frames)
                self.assertGreater(len(decoded['objects'][0]['trajectory']), 1)

            # A model result can follow the route cleanly while missing a saved wrist target.
            side = {'id': 'prop', 'asset_version': 'a' * 32, 'size_m': [2, 2, 1.2],
                    'center_m': [0, 1, 0], 'heading_deg': 0}
            pose = np.zeros((len(soma_names), 3))
            pose[0, 1], pose[16, 2], pose[17, 2] = -np.pi / 2, -1.4, 1.4
            local_soma[:] = Rotation.from_rotvec(pose).as_matrix()
            soma_fk, soma_glob = fk(local_soma, soma_rest, soma_parents)
            side_scene = {
                'kind': human_interaction.VERSION, 'objects': [side],
                'waypoints': [{'frame': 0, 'xz_m': [1.52, 0], 'heading_deg': -90}],
                'keyframes': [{'frame': 0, 'object_id': 'prop', 'heading_deg': -90,
                               'pelvis_offset_m': [1.52, -.09, 0], 'body_rotvecs': pose[1:22].tolist()}],
                'contacts': [{'start_frame': 24, 'end_frame': frames, 'object_id': 'prop',
                              'joint_index': 21, 'anchor_m': [1, .25, 0], 'tolerance_m': .08}],
            }
            output = root / 'side.npz'
            spec.update(text='A person reaches for a handle on the side of a stationary prop.', scene=side_scene)
            batch.write_text(json.dumps([{'spec': spec, 'features': str(feat), 'motion': str(output)}]))
            generate()

            meta = json.loads(collection_kimodo.metadata_path(output).read_text())['scene']
            self.assertTrue(meta['passed'], meta)
            self.assertEqual(meta['repair']['version'], 'personal-wrist-fit-v1')
            with np.load(root / meta['repair']['original_npz'], allow_pickle=False) as original, \
                 np.load(output, allow_pickle=False) as repaired:
                self.assertFalse(human_interaction.report(original, side_scene, rest, parents)['contacts'][0]['passed'])
                np.testing.assert_array_equal(repaired['native_transl'], original['native_transl'])
                np.testing.assert_array_equal(repaired['smpl_poses'][0], original['smpl_poses'][0])

            # Existing .quarantine.npz on a sequence-gate failure must be preserved and candidate saved cleanly.
            bad_seq_out = root / 'bad_seq.npz'
            bad_seq_out.with_name('bad_seq.quarantine.npz').write_bytes(b'prior-quarantine')
            bad_spec = dict(spec, seed=12)
            batch.write_text(json.dumps([{'spec': bad_spec, 'features': str(feat), 'motion': str(bad_seq_out)}]))
            with patch('g2h.kimodo_long.seam_report', return_value={
                'passed': False,
                'boundaries': [{'frame': 30, 'joint_step_max_m': 0.5, 'joint_acceleration_max_m_s2': 200.0,
                                'rotation_step_max_rad': 1.0, 'passed': False}],
            }):
                generate()
            self.assertEqual(bad_seq_out.with_name('bad_seq.quarantine.npz').read_bytes(), b'prior-quarantine')
            self.assertTrue(bad_seq_out.is_file())
            bad_meta = json.loads(collection_kimodo.metadata_path(bad_seq_out).read_text())
            self.assertFalse(bad_meta['sequence']['passed'])
            self.assertIn('阶段衔接帧 30', bad_meta['sequence']['warning'])


class MultiActorAndPairedTests(unittest.TestCase):
    def test_invalid_later_actor_does_not_create_any_scene_tasks(self):
        from harness_service import HarnessService

        user = {'username': 'alice'}
        value = {'request_id': 'multi-invalid-actor', 'duration': 4, 'actors': [
            {'id': 'R1', 'text': 'Walk two short steps forward and rest with both feet flat.'},
            {'id': 'R2', 'text': '慢慢向前走两步。'},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            service = HarnessService(directory)
            with patch.object(service, '_scene_enabled', return_value=True), patch.object(
                service, 'execute', side_effect=lambda user, task_id, *_: service.detail(user, task_id)
            ) as execute:
                with self.assertRaisesRegex(ValueError, '英文原文'):
                    service.multi_scene(user, value)
                self.assertEqual(service.tasks(user)['items'], [])
                execute.assert_not_called()

                value['request_id'] = 'multi-corrected-actor'
                value['actors'][1]['text'] = 'Turn slowly to the right and hold a balanced standing pose.'
                group = service.multi_scene(user, value)
                self.assertEqual(len(group['actors_group']), 2)
                self.assertEqual(len(service.tasks(user)['items']), 2)
                self.assertEqual(execute.call_count, 2)

    def test_task_list_identifies_scene_group_roots(self):
        from harness_service import HarnessService

        with tempfile.TemporaryDirectory() as directory:
            service = HarnessService(directory)
            user = {'username': 'alice'}
            with patch.object(service, '_scene_enabled', return_value=True), patch.object(
                service, 'execute', side_effect=lambda user, task_id, *_: service.detail(user, task_id)
            ):
                result = service.multi_scene(user, {
                    'request_id': 'multi-group-root', 'duration': 4,
                    'actors': [{'id': 'R1'}, {'id': 'R2'}],
                })
            first = service.store.create_task('alice', 'paired-root', 'First partner')['id']
            second = service.store.create_task('alice', 'paired-second', 'Second partner')['id']
            service.store.link_scene_pair('alice', first, second, 'handshake')
            tasks = service.tasks(user)['items']
            self.assertEqual({task['id'] for task in tasks if task.get('scene_group')}, {result['id'], first})
            self.assertEqual(len(tasks), 4)
            grouped = service.tasks(user, {'grouped': ['1'], 'limit': ['2']})['items']
            self.assertEqual({task['id'] for task in grouped}, {result['id'], first})
            self.assertEqual(service.tasks({'username': 'bob'}, {'grouped': ['1']})['items'], [])

    def test_paired_scene_ground_keyframes_and_optional_params(self):
        rest, parents = load()
        for kind in ('handshake', 'carry_box'):
            plans = paired_scene.interaction_plans(
                kind, 90, x=1.0, z=2.0,
                box_size_m=[0.50, 0.34, 0.38] if kind == 'carry_box' else None,
                to_xz_m=[2.2, 2.5] if kind == 'carry_box' else None,
                actor_starts=[[0.2, 1.0], [1.8, 3.0]],
            )
            self.assertEqual(len(plans), 2)
            for p in plans:
                human_interaction.check(p, 90)
                self.assertEqual(p['keyframes'][0]['frame'], 0)
                self.assertEqual(p['keyframes'][-1]['frame'], 89)
                indices, roots, headings, _, _ = human_interaction.world_targets(p, 90)
                self.assertAlmostEqual(float(roots[0, 1]), 0.915, places=5)
                self.assertAlmostEqual(float(roots[-1, 1]), 0.915, places=5)
                np.testing.assert_allclose(roots[0, [0, 2]], p['waypoints'][0]['xz_m'], atol=1e-6)
                np.testing.assert_allclose(roots[-1, [0, 2]], p['waypoints'][-1]['xz_m'], atol=1e-6)

    def test_build_multi_plans_and_report_for_two_and_three_actors(self):
        rest, parents = load()
        # Reject invalid counts / durations.
        for bad in (
            {'duration': 0, 'actors': [{}]},
            {'duration': 121, 'actors': [{}]},
            {'duration': 3, 'actors': []},
            {'duration': 3, 'actors': [{} for _ in range(5)]},
        ):
            with self.assertRaises(ValueError):
                multi_actor.build_multi_plans(bad)

        # 3-actor scene: R1 and R2 co-carry a floor-level box with custom size & lift, R3 stands nearby.
        payload = {
            'duration': 4,
            'objects': [{
                'id': 'crate-box',
                'center_m': [0.0, 0.275, 0.0],
                'size_m': [0.65, 0.55, 0.50],
                'heading_deg': 0.0,
            }],
            'actors': [
                {'id': 'r1', 'start_xz_m': [-0.1, -1.2], 'heading_deg': 0.0},
                {'id': 'r2', 'start_xz_m': [0.1, 1.2], 'heading_deg': 180.0},
                {'id': 'r3', 'start_xz_m': [2.2, 0.0], 'heading_deg': -90.0, 'text': 'A person watches safely from the side.'},
            ],
            'interactions': [{
                'kind': 'co_carry',
                'actors': ['r1', 'r2'],
                'object_id': 'crate-box',
                'size_m': [0.65, 0.55, 0.50],
                'to_xz_m': [1.0, 0.4],
                'lift_m': 0.32,
                'start_frame': 25,
                'end_frame': 95,
            }],
        }
        plans = multi_actor.build_multi_plans(payload)
        self.assertEqual(len(plans), 3)

        joints_list = []
        for plan in plans:
            self.assertTrue(plan['text'].startswith('A person '))
            self.assertGreaterEqual(len(plan['text'].split()), 8)
            self.assertEqual(plan['duration'], 4)
            sc = plan['scene']
            human_interaction.check(sc, 120)
            self.assertEqual(sc['keyframes'][0]['frame'], 0)
            indices, roots, headings, _, _ = human_interaction.world_targets(sc, 120)
            self.assertAlmostEqual(float(roots[0, 1]), 0.915, places=5)
            np.testing.assert_allclose(roots[0, [0, 2]], sc['waypoints'][0]['xz_m'], atol=1e-6)

            # Synthesize exact FK tracks along keyframes to verify multi_actor_report passes.
            full_roots = np.column_stack([
                np.interp(np.arange(120), indices, roots[:, ax]) for ax in range(3)
            ])
            local = np.zeros((120, 52, 3))
            yaw_unwrapped = np.unwrap(np.deg2rad(headings))
            interp_yaw = np.interp(np.arange(120), indices, yaw_unwrapped)
            local[:, 0] = Rotation.from_rotvec(interp_yaw[:, None] * np.array([0.0, 1.0, 0.0])).as_rotvec()
            key_rot = np.array([k['body_rotvecs'] for k in sc['keyframes']], dtype=float)
            for j in range(1, 22):
                for ax in range(3):
                    local[:, j, ax] = np.interp(np.arange(120), indices, key_rot[:, j - 1, ax])
            pts, _ = fk(Rotation.from_rotvec(local.reshape(-1, 3)).as_matrix().reshape(120, 52, 3, 3), rest, parents)
            joints_list.append(pts - pts[:, :1] + full_roots[:, None, :])

        # Verify multi_actor_report works with both wrapper dicts and raw scene dicts.
        rep_wrap = multi_actor.multi_actor_report(plans, joints_list)
        rep_raw = multi_actor.multi_actor_report([p['scene'] for p in plans], joints_list)
        self.assertTrue(rep_wrap['passed'], rep_wrap)
        self.assertTrue(rep_raw['passed'], rep_raw)
        self.assertGreaterEqual(rep_raw['min_pelvis_clearance_m'], 0.32)
        self.assertTrue(rep_raw['clearance_passed'])
        self.assertLess(rep_raw['carry_gap_max_m'], 0.05)

        # Colliding actors (< 0.32 m pelvis clearance) must fail clearance check.
        colliding = [j.copy() for j in joints_list]
        colliding[2][:, :, 0] = colliding[0][:, :, 0] + 0.10
        colliding[2][:, :, 2] = colliding[0][:, :, 2]
        bad_rep = multi_actor.multi_actor_report(plans, colliding)
        self.assertFalse(bad_rep['passed'])
        self.assertFalse(bad_rep['clearance_passed'])
        self.assertLess(bad_rep['min_pelvis_clearance_m'], 0.32)

    def test_two_actor_handshake_arbitrary_start_and_report(self):
        rest, parents = load()
        plans = multi_actor.build_multi_plans({
            'duration': 3,
            'actors': [
                {'id': 'a1', 'start_xz_m': [-1.0, -0.8], 'heading_deg': 45.0},
                {'id': 'a2', 'start_xz_m': [1.0, 0.8], 'heading_deg': -135.0},
            ],
            'interactions': [{'kind': 'handshake', 'actors': [0, 1]}],
        })
        self.assertEqual(len(plans), 2)
        joints_list = []
        for plan in plans:
            sc = plan['scene']
            human_interaction.check(sc, 90)
            indices, roots, headings, _, _ = human_interaction.world_targets(sc, 90)
            np.testing.assert_allclose(roots[0, [0, 2]], sc['waypoints'][0]['xz_m'], atol=1e-6)
            full_roots = np.column_stack([np.interp(np.arange(90), indices, roots[:, ax]) for ax in range(3)])
            local = np.zeros((90, 52, 3))
            interp_yaw = np.interp(np.arange(90), indices, np.unwrap(np.deg2rad(headings)))
            local[:, 0] = Rotation.from_rotvec(interp_yaw[:, None] * np.array([0.0, 1.0, 0.0])).as_rotvec()
            key_rot = np.array([k['body_rotvecs'] for k in sc['keyframes']], dtype=float)
            for j in range(1, 22):
                for ax in range(3):
                    local[:, j, ax] = np.interp(np.arange(90), indices, key_rot[:, j - 1, ax])
            pts, _ = fk(Rotation.from_rotvec(local.reshape(-1, 3)).as_matrix().reshape(90, 52, 3, 3), rest, parents)
            joints_list.append(pts - pts[:, :1] + full_roots[:, None, :])

        rep = multi_actor.multi_actor_report(plans, joints_list)
        self.assertTrue(rep['passed'], rep)
        self.assertLessEqual(rep['wrist_gap_max_m'], 0.075)
        self.assertGreaterEqual(rep['min_pelvis_clearance_m'], 0.32)

    def test_single_and_four_actor_plans_with_custom_actor_scene(self):
        # 1 actor with empty objects -> builds grounded floor-anchor scene.
        solo = multi_actor.build_multi_plans({
            'duration': 2,
            'actors': [{'id': 'r1', 'start_xz_m': [0.5, -0.5], 'heading_deg': 30.0}],
        })
        self.assertEqual(len(solo), 1)
        human_interaction.check(solo[0]['scene'], 60)
        self.assertEqual(solo[0]['scene']['keyframes'][0]['frame'], 0)

        # 4 actors: R1/R2 handshake, R3 custom scene without frame-0 keyframe (auto-grounded), R4 idle.
        custom_scene = {
            'kind': human_interaction.VERSION,
            'objects': [{'id': 'goal', 'center_m': [3.0, 0.005, 0.0], 'size_m': [0.32, 0.01, 0.32], 'heading_deg': 0.0}],
            'waypoints': [
                {'frame': 0, 'xz_m': [2.0, 0.0], 'heading_deg': 90.0},
                {'frame': 89, 'xz_m': [3.0, 0.0], 'heading_deg': 90.0, 'tolerance_m': 0.12},
            ],
            'keyframes': [],
            'contacts': [],
        }
        quad = multi_actor.build_multi_plans({
            'duration': 3,
            'actors': [
                {'id': 'r1', 'start_xz_m': [-0.5, -0.5], 'heading_deg': 0.0},
                {'id': 'r2', 'start_xz_m': [-0.5, 0.5], 'heading_deg': 180.0},
                {'id': 'r3', 'start_xz_m': [2.0, 0.0], 'heading_deg': 90.0, 'scene': custom_scene},
                {'id': 'r4', 'start_xz_m': [-2.0, 0.0], 'heading_deg': 0.0},
            ],
            'interactions': [{'kind': 'handshake', 'actors': ['r1', 'r2']}],
        })
        self.assertEqual(len(quad), 4)
        for p in quad:
            human_interaction.check(p['scene'], 90)
            self.assertEqual(p['scene']['keyframes'][0]['frame'], 0)
            indices, roots, _, _, _ = human_interaction.world_targets(p['scene'], 90)
            self.assertAlmostEqual(float(roots[0, 1]), 0.915, places=5)
            np.testing.assert_allclose(roots[0, [0, 2]], p['scene']['waypoints'][0]['xz_m'], atol=1e-6)

    def test_paired_scene_parallel_wrist_axis_has_no_nan(self):
        real_fk = fk
        def parallel_fk(local, rest, parents):
            pts, glob = real_fk(local, rest, parents)
            # Construct elbow frame whose col-0 aligns with [0, 0.08, 1] so global_rot.T @ [0, 0.08, 1] == [1, 0, 0]
            # (hits parallel for wrist=20 and antiparallel for wrist=21 in actor 0).
            v = np.array([0.0, 0.08, 1.0]); v /= np.linalg.norm(v)
            u = np.array([1.0, 0.0, 0.0]); w = np.cross(v, u)
            mat = np.column_stack((v, u, w))
            glob = glob.copy()
            glob[:, 18] = mat
            glob[:, 19] = mat
            return pts, glob

        with patch('g2h.paired_scene.fk', side_effect=parallel_fk):
            plans = paired_scene.interaction_plans('carry_box', 90)
        for plan in plans:
            for kf in plan['keyframes']:
                self.assertTrue(np.isfinite(kf['body_rotvecs']).all())

    def test_bvh_convert_mismatched_calibration_channels_raises_value_error(self):
        from g2h import bvh_import

        bvh_raw = (
            b'HIERARCHY\nROOT Hips\n{\n  OFFSET 0 90 0\n'
            b'  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation\n'
            b'  JOINT LeftUpLeg\n  {\n    OFFSET 10 0 0\n    CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'    JOINT LeftLeg\n    {\n      OFFSET 0 -40 0\n      CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'      JOINT LeftFoot\n      {\n        OFFSET 0 -40 0\n        CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'        End Site { OFFSET 0 -5 10 }\n      }\n    }\n  }\n'
            b'  JOINT RightUpLeg\n  {\n    OFFSET -10 0 0\n    CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'    JOINT RightLeg\n    {\n      OFFSET 0 -40 0\n      CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'      JOINT RightFoot\n      {\n        OFFSET 0 -40 0\n        CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'        End Site { OFFSET 0 -5 10 }\n      }\n    }\n  }\n'
            b'  JOINT Spine\n  {\n    OFFSET 0 15 0\n    CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'    JOINT Head\n    {\n      OFFSET 0 35 0\n      CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'      End Site { OFFSET 0 15 0 }\n    }\n    JOINT LeftArm\n    {\n      OFFSET 15 20 0\n      CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'      JOINT LeftForeArm\n      {\n        OFFSET 25 0 0\n        CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'        JOINT LeftHand\n        {\n          OFFSET 25 0 0\n          CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'          End Site { OFFSET 10 0 0 }\n        }\n      }\n    }\n'
            b'    JOINT RightArm\n    {\n      OFFSET -15 20 0\n      CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'      JOINT RightForeArm\n      {\n        OFFSET -25 0 0\n        CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'        JOINT RightHand\n        {\n          OFFSET -25 0 0\n          CHANNELS 3 Zrotation Xrotation Yrotation\n'
            b'          End Site { OFFSET -10 0 0 }\n        }\n      }\n    }\n  }\n}\n'
            b'MOTION\nFrames: 2\nFrame Time: 0.0333333\n'
            + (b' '.join([b'0'] * 48) + b'\n') * 2
        )
        parsed = bvh_import.read(bvh_raw)
        mapped, missing = bvh_import.mapping(parsed[0])
        self.assertEqual(missing, [])
        with self.assertRaisesRegex(ValueError, '校准通道'):
            bvh_import.convert(parsed, {
                'signature': parsed[3], 'mapping': mapped, 'up': '+Y', 'forward': '+Z',
                'units': 0.01, 'calibration_channels': [0.0] * 5,
            })


if __name__ == '__main__':
    unittest.main()
