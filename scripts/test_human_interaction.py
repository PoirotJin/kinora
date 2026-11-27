import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import warnings

import numpy as np

from g2h import human_interaction as scene
from g2h.generation_backend import KIMODO_MODEL, current_recipe, validate_spec


def fixture():
    return {
        'kind': scene.VERSION,
        'objects': [{'id': 'box', 'center_m': [2, .5, 3], 'size_m': [1, 1, 1], 'heading_deg': 90}],
        'waypoints': [{'frame': 0, 'xz_m': [1, 3], 'heading_deg': 90}],
        'keyframes': [{'frame': 29, 'object_id': 'box', 'pelvis_offset_m': [0, .5, -1],
                       'heading_deg': 0, 'body_rotvecs': [[0, 0, 0]] * 21}],
        'contacts': [{'start_frame': 20, 'end_frame': 30, 'object_id': 'box',
                      'joint_index': 20, 'anchor_m': [0, .5, 0], 'tolerance_m': .02}],
    }


class HumanInteraction(unittest.TestCase):
    def test_distant_frame_zero_pose_matches_its_track_start(self):
        data = fixture()
        key = dict(data['keyframes'][0], frame=0, pelvis_offset_m=[0, .415, -8])
        data['waypoints'][0]['xz_m'] = [-6, 3]
        data['keyframes'].insert(0, key)
        scene.check(data, 30)

        data['waypoints'][0]['xz_m'][0] = -5
        with self.assertRaisesRegex(ValueError, '路径点.*冲突'):
            scene.check(data, 30)

    def test_terminal_two_foot_hold_gets_full_pose_constraints(self):
        data = fixture()
        final = data['keyframes'][0]
        data['keyframes'].insert(0, dict(final, frame=20))
        data['contacts'] = [
            dict(data['contacts'][0], joint_index=joint) for joint in (10, 11)
        ]
        rest, parents = np.zeros((52, 3)), np.zeros(52)

        with patch('g2h.kimodo_edit.canonical_to_soma',
                   side_effect=lambda arrays, *_: (arrays['native_transl'][:, None, :], None)), \
             patch('g2h.kimodo_scene.make_constraints') as constraints:
            scene.conditioning(data, 30, SimpleNamespace(skeleton=None), rest, parents)

        _, route, poses, _, _, _, targets, _ = constraints.call_args.args
        np.testing.assert_array_equal(route, np.arange(20))
        np.testing.assert_array_equal(poses, np.arange(20, 30))
        np.testing.assert_allclose(targets, np.repeat(targets[-1:], 10, axis=0))

    def test_desktop_scene_endpoint_queues_once_and_reconciles(self):
        from contextlib import closing
        from harness_service import HarnessService
        from review_access import allowed
        from review_auth import Auth
        from review_custom import Custom
        from review_harness import handle

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Auth(root/'review').create('alice','sample-test-password','user')
            service = HarnessService(root,legacy_store=SimpleNamespace(root=root))
            user = {'username':'alice','role':'user'}
            handler = SimpleNamespace(server=SimpleNamespace(harness_service=service),
                                      send=lambda status,body:(status,body))
            value = {'request_id':'example-scene-id','duration':1,'scene':fixture(),
                     'text':'A person steps slowly toward a small box and reaches one hand toward its top.'}
            handler.body = lambda **_: value
            path = '/api/harness/scene'
            self.assertTrue(allowed(user,path,True))
            self.assertFalse(allowed(user,path,False))
            with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL':KIMODO_MODEL,
                                         'MOTIONFARM_HARNESS_ENABLE_GENERATION':'1'}), \
                 patch('harness_service.kimodo_readiness',return_value={'available':True}), \
                 patch('g2h.generation_backend.kimodo_readiness',return_value={'available':True}):
                self.assertTrue(service.capabilities(user)['scene_interaction'])
                status, task = handle(handler,urlsplit(path),True,user)
                self.assertEqual(status,202)
                self.assertEqual(task['operations'][0]['status'],'waiting')
                _, retry = handle(handler,urlsplit(path),True,user)
                self.assertEqual(retry['id'],task['id'])
                different = dict(value, duration=2)
                handler.body = lambda **_: different
                with self.assertRaisesRegex(ValueError,'决策编号'):
                    handle(handler,urlsplit(path),True,user)
                handler.body = lambda **_: {}
                route = '/api/harness/tasks/'+task['id']+'/scene-status'
                self.assertTrue(allowed(user,route,True))
                status, reconciled = handle(handler,urlsplit(route),True,user)
                self.assertEqual(status,200)
                self.assertEqual(reconciled['operations'][0]['status'],'waiting')
                with closing(Custom(root).db()) as db:
                    queued, = db.execute('SELECT scene FROM requests').fetchall()
                    self.assertEqual(json.loads(queued['scene']),fixture())
            lease = service.store.claim('alice', task['id'], 'reconcile-test', decision_owner='external-local')
            service.store.settle_operation(lease, task['operations'][0]['id'], {
                'state': 'failed', 'result': {'message': 'Test generation failed'},
            })
            service.store.release(lease)
            handler.server.harness_service = HarnessService(root, legacy_store=SimpleNamespace(root=root))
            status, restored = handle(handler, urlsplit('/api/harness/tasks'), False, user)
            self.assertEqual(status, 200)
            summary, = restored['items']
            self.assertEqual(summary['id'], task['id'])
            self.assertTrue(summary.get('scene'))
            self.assertEqual(summary['last_operation_status'], 'failed')
            self.assertNotIn('operations', summary)
            with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL':KIMODO_MODEL,
                                         'MOTIONFARM_HARNESS_ENABLE_GENERATION':'0'}):
                self.assertFalse(service.capabilities(user)['scene_interaction'])

    def test_marker_without_pose_and_drift_during_hold(self):
        data = fixture()
        data.update(keyframes=[], contacts=[], waypoints=[
            dict(frame=0, xz_m=[0,0], heading_deg=0),
            dict(frame=15, xz_m=[1,0], heading_deg=0, tolerance_m=.12, through_frame=29)])
        scene.check(data, 30)
        _, _, _, path, _ = scene.world_targets(data, 30)
        root = np.zeros((30, 3))
        root[:, [0, 2]] = path
        arrays = dict(native_transl=root, native_keypoints3d=np.zeros((30, 52, 3)))
        rest = np.zeros((52, 3))
        parents = np.arange(52)-1
        preview = scene.preview(arrays, data, rest, parents)
        self.assertEqual(preview['targets'], [])
        self.assertTrue(preview['quality']['passed'])
        root[24, 2] += .15
        preview = scene.preview(arrays, data, rest, parents)
        self.assertFalse(preview['quality']['passed'])
        self.assertEqual(preview['events'][0]['frame'], 24)

    def test_rotated_anchor_and_route_survive_spec_validation(self):
        request = fixture()
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL':KIMODO_MODEL}):
            spec = dict(current_recipe(), text='Reach toward the box', duration_s=1., seed=17, scene=request)
        validate_spec(spec)
        indices, roots, headings, path, _ = scene.world_targets(request, 30)
        np.testing.assert_array_equal(indices, [29])
        np.testing.assert_allclose(roots, [[1, 1, 3]])
        np.testing.assert_allclose(path, np.tile([1, 3], (30, 1)))
        self.assertEqual(headings, [90])
        self.assertEqual(json.loads(bytes(scene.encode(request))), request)
        with self.assertRaisesRegex(ValueError, 'Human52'):
            validate_spec(dict(spec, scene_pipeline='g1_native'))

    def test_scene_roundtrips_through_numeric_npz_validation(self):
        from g2h.motion_assets import load_numeric_npz

        arrays = {'fps': np.asarray(30), 'native_keypoints3d': np.zeros((30, 52, 3)),
                  'native_transl': np.zeros((30, 3)), 'smpl_poses': np.zeros((30, 156)),
                  'scene_spec_json': scene.encode(fixture())}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scene.npz'
            np.savez_compressed(path, **arrays)
            loaded = load_numeric_npz(path)
        self.assertEqual(scene.decode(loaded['scene_spec_json'], 30), fixture())
        with self.assertRaises(ValueError):
            scene.decode(np.array([0], dtype=np.uint8), 30)

    def test_rejects_conflicting_targets_and_anchors_inside_geometry(self):
        data = fixture()
        data['waypoints'].append({'frame': 29, 'xz_m': [9, 3], 'heading_deg': 90})
        with self.assertRaisesRegex(ValueError, '冲突'):
            scene.check(data, 30)
        data = fixture()
        data['contacts'][0]['anchor_m'] = [0, 0, 0]
        with self.assertRaisesRegex(ValueError, '物体表面'):
            scene.check(data, 30)
        data = fixture()
        data['contacts'].append(dict(data['contacts'][0],anchor_m=[.5,.5,0]))
        with self.assertRaisesRegex(ValueError, '接触目标冲突'):
            scene.check(data,30)
        data = fixture()
        data['contacts'][0]['end_frame'] = 31
        with self.assertRaisesRegex(ValueError, '接触区间'):
            scene.check(data,30)

    def test_moving_object_targets_and_contacts_are_frame_relative(self):
        data = fixture()
        data['objects'][0]['trajectory'] = [
            {'frame': 0, 'center_m': [2, .5, 3]},
            {'frame': 29, 'center_m': [3, .5, 3]},
        ]
        scene.check(data, 30)
        _, roots, _, _, _ = scene.world_targets(data, 30)
        np.testing.assert_allclose(roots, [[2, 1, 3]])
        rest = np.zeros((52, 3))
        centers = scene.object_centers(data['objects'][0], 30)
        arrays = {'native_transl': centers + [-1, .5, 0],
                  'native_keypoints3d': np.zeros((30, 52, 3))}
        arrays['native_keypoints3d'][:, 20, 0] = 1
        result = scene.report(arrays, data, rest, [-1] + [0] * 51)
        self.assertTrue(result['passed'], result)
        self.assertAlmostEqual(result['contacts'][0]['max_error_m'], 0)

    def test_personal_side_contact_fit_keeps_route_and_rejects_unsafe_results(self):
        from g2h.human_interaction_repair import personal_wrist_misses, repair
        from g2h.human_rig import load
        from g2h.kimodo_adapter import fk
        from scipy.spatial.transform import Rotation

        rest, parents = load()
        frames = 60
        poses = np.zeros((frames, 52, 3))
        poses[:, 0, 1] = -np.pi / 2
        poses[:, 16, 2], poses[:, 17, 2] = -1.4, 1.4
        matrices = Rotation.from_rotvec(poses.reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
        points, _ = fk(matrices, rest, parents)
        obj = {'id': 'prop', 'asset_version': 'a' * 32, 'size_m': [2, 2, 1.2],
               'center_m': [0, 1, 0], 'heading_deg': 0}
        contact = {'start_frame': 24, 'end_frame': frames, 'object_id': 'prop',
                   'joint_index': 21, 'anchor_m': [1, .25, 0], 'tolerance_m': .08}
        for x, locked_frame, repaired in ((1.52, 0, True), (1.52, 24, False),
                                          (1.41, 0, False), (1.62, 0, False)):
            with self.subTest(x=x, locked_frame=locked_frame):
                root = np.array([x, .915, 0])
                data = {
                    'kind': scene.VERSION, 'objects': [obj], 'contacts': [contact],
                    'waypoints': [{'frame': 0, 'xz_m': [x, 0], 'heading_deg': -90}],
                    'keyframes': [{'frame': locked_frame, 'object_id': 'prop',
                                   'pelvis_offset_m': (root - obj['center_m']).tolist(),
                                   'heading_deg': -90, 'body_rotvecs': poses[0, 1:22].tolist()}],
                }
                arrays = {'smpl_poses': poses.reshape(frames, 156), 'native_keypoints3d': points,
                          'native_transl': np.tile(root - rest[0], (frames, 1))}
                before = scene.report(arrays, data, rest, parents)
                self.assertEqual(before['max_skeleton_penetration_m'], 0)
                self.assertEqual(personal_wrist_misses(data, before), [contact])
                updated, audit = repair(arrays, data, rest, parents, quality=before)
                if repaired:
                    self.assertEqual(audit['version'], 'personal-wrist-fit-v1')
                    self.assertTrue(scene.report(updated, data, rest, parents)['passed'])
                    np.testing.assert_array_equal(updated['native_transl'], arrays['native_transl'])
                    np.testing.assert_array_equal(updated['smpl_poses'][:16], arrays['smpl_poses'][:16])
                    np.testing.assert_array_equal(updated['smpl_poses'][:, :17 * 3], arrays['smpl_poses'][:, :17 * 3])
                else:
                    self.assertIs(updated, arrays)

    def test_repair_clears_approach_but_preserves_key_pose_and_contact(self):
        from g2h.human_interaction_repair import repair

        rest = np.zeros((52, 3))
        rest[20] = [1, 0, 0]
        parents = np.array([-1] + [0] * 51)
        data = fixture()
        # The wrist touches the top of the rotating box at the final key pose.
        # A small object crosses the pelvis during the approach.
        data['objects'].append({
            'id': 'crossing', 'center_m': [1, 1, 2], 'size_m': [.1, .1, .1],
            'heading_deg': 0,
            'trajectory': [{'frame': f, 'center_m': [1, 1, z]}
                           for f, z in ((0, 2), (12, 2.8), (15, 3), (18, 3.2))],
        })
        arrays = {'native_keypoints3d': np.tile(rest, (30, 1, 1)),
                  'native_transl': np.tile([1., 1., 3.], (30, 1)),
                  'smpl_poses': np.zeros((30, 156))}
        self.assertFalse(scene.report(arrays, data, rest, parents)['passed'])
        updated, audit = repair(arrays, data, rest, parents)
        self.assertTrue(scene.report(updated, data, rest, parents)['passed'])
        self.assertGreater(audit['modified_frames'], 0)
        for field in arrays:
            np.testing.assert_array_equal(updated[field][20:], arrays[field][20:])

    def test_preflight_recomputes_colliding_wrists_and_preserves_root_and_contact(self):
        from g2h.human_interaction_preflight import CONTACT_LOCK_M, preflight
        from g2h.human_rig import load
        from g2h.kimodo_adapter import fk
        from scipy.spatial.transform import Rotation

        rest, parents = load()
        pose = np.zeros((52, 3))
        pose[[16, 17]] = [[0, -1.05, -.22], [0, 1.05, .22]]
        root = np.array([0, .945, 0])
        joints, _ = fk(Rotation.from_rotvec(pose).as_matrix()[None], rest, parents)
        hands = joints[0, [20, 21]] + root - rest[0]
        center = hands.mean(axis=0)
        center[1] -= .08
        obj = dict(id='desk', center_m=center.tolist(), size_m=[1.15, .16, .48], heading_deg=0)
        key = dict(frame=29, object_id='desk', pelvis_offset_m=(root-center).tolist(),
                   heading_deg=0, body_rotvecs=pose[1:22].tolist())
        data = dict(kind=scene.VERSION, objects=[obj], keyframes=[key],
                    waypoints=[dict(frame=0, xz_m=[0, 0], heading_deg=0)],
                    contacts=[dict(start_frame=29, end_frame=30, object_id='desk', joint_index=i,
                                   anchor_m=(hand-center).tolist(), tolerance_m=.02)
                              for i, hand in zip((20, 21), hands)])
        valid, audit = preflight(data, 30, rest, parents)
        row = audit['keyframes'][0]
        self.assertTrue(audit['passed'])
        self.assertGreater(row['before_m'], scene.MAX_PENETRATION_M)
        self.assertLess(row['after_m'], scene.MAX_PENETRATION_M)
        self.assertEqual(valid['contacts'], data['contacts'])
        self.assertTrue(all(c['shift_m'] <= CONTACT_LOCK_M for c in row['contacts']))
        self.assertEqual(len(row['contacts']), 2)
        self.assertEqual(valid['keyframes'][0]['pelvis_offset_m'], key['pelvis_offset_m'])
        self.assertEqual(valid['keyframes'][0]['body_rotvecs'][0:12], key['body_rotvecs'][0:12])
        self.assertEqual(key['body_rotvecs'], pose[1:22].tolist())
        second, unchanged = preflight(valid, 30, rest, parents)
        self.assertEqual(second, valid)
        self.assertEqual(unchanged['keyframes'][0]['changed_joints'], [])

    def test_contact_window_measured_in_world_and_penetration_is_rejected(self):
        data = fixture()
        rest = np.zeros((52, 3))
        parents = [-1] + [0] * 51
        arrays = {'native_transl': np.tile([1., 1., 3.], (30, 1)),
                  'native_keypoints3d': np.zeros((30, 52, 3))}
        arrays['native_keypoints3d'][:, 20, 0] = 1
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            report = scene.report(arrays, data, rest, parents, frame_distances=True)
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['contacts'][0]['hit_fraction'], 1.)
        self.assertEqual(len(report['contacts'][0]['distance_m']), 10)
        arrays['native_keypoints3d'][21, 20, 1] = -.15
        report = scene.report(arrays, data, rest, parents)
        view = scene.preview(arrays,data,rest,parents)
        self.assertEqual(view['targets'][0]['frame'],29)
        self.assertEqual(view['events'][0]['frame'],21)
        self.assertFalse(report['passed'])
        self.assertAlmostEqual(report['max_skeleton_penetration_m'], .15)
        self.assertEqual(report['contacts'][0]['hit_fraction'], .9)

    def test_ground_sole_alignment_and_initial_foot_clearance_check(self):
        import sys
        from g2h.human_rig import load
        from g2h.kimodo_adapter import BODY_MAP, convert, fk
        from scipy.spatial.transform import Rotation

        rest, parents = load()
        # Construct a SOMA skeleton whose legs are 3.6 cm longer than Human52.
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

        class Recorder:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        fake_torch = SimpleNamespace(
            as_tensor=lambda a, dtype=None, device=None: np.asarray(a),
            long='long', float32='f32',
        )
        fake_constraints = SimpleNamespace(FullBodyConstraintSet=Recorder, Root2DConstraintSet=Recorder)
        skeleton = SimpleNamespace(
            root_idx=0,
            bone_order_names=soma_names,
            joint_parents=Tensor(soma_parents),
            neutral_joints=Tensor(soma_rest),
        )
        pipeline = SimpleNamespace(skeleton=skeleton, device='cpu')

        data = {
            'kind': scene.VERSION,
            'objects': [{'id': 'marker', 'center_m': [0, 0.005, 0], 'size_m': [0.32, 0.01, 0.32], 'heading_deg': 0}],
            'waypoints': [{'frame': 0, 'xz_m': [0, 0], 'heading_deg': 0}],
            'keyframes': [{
                'frame': 0, 'object_id': 'marker', 'pelvis_offset_m': [0, 0.905, 0],
                'heading_deg': 0, 'body_rotvecs': [[0, 0, 0]] * 21,
            }],
            'contacts': [],
        }
        with patch.dict(sys.modules, {'torch': fake_torch, 'kimodo': SimpleNamespace(), 'kimodo.constraints': fake_constraints}):
            sets, origin, heading = scene.conditioning(data, 30, pipeline, rest, parents)
        self.assertEqual(heading, 0.0)
        full_body = sets[1]
        soma_positions = full_body.args[2]
        foot_ids = [soma_names.index(n) for n in ('LeftFoot', 'LeftToeBase', 'RightFoot', 'RightToeBase')]
        self.assertAlmostEqual(float(soma_positions[0, foot_ids, 1].min()), 0.0, places=6)

        # Roundtrip back through convert(..., preserve_root=True): soles must land at Y=0.
        local_soma = np.broadcast_to(np.eye(3), (30, len(soma_names), 3, 3)).copy()
        soma_fk, soma_glob = fk(local_soma, soma_rest, soma_parents)
        root_pos = np.tile(soma_positions[0, 0], (30, 1))
        output = {
            'local_rot_mats': local_soma,
            'global_rot_mats': soma_glob,
            'root_positions': root_pos,
            'posed_joints': soma_fk + root_pos[:, None, :],
        }
        arrays, meta = convert(output, soma_names, soma_rest, rest, parents, fps=30, preserve_root=True)
        for key in ('native_transl', 'smpl_trans'):
            arrays[key] += origin
        self.assertAlmostEqual(meta['vertical_shift_m'], -0.036, places=6)
        rep = scene.report(arrays, data, rest, parents)
        self.assertTrue(rep['passed'], rep)
        self.assertAlmostEqual(rep['initial_foot_clearance_m'], 0.0, places=5)

        # Floating by +0.12 m at frame 0 must fail initial_foot_clearance_m > 0.08 m.
        floating = {k: v.copy() for k, v in arrays.items()}
        floating['native_transl'][:, 1] += 0.12
        bad = scene.report(floating, data, rest, parents)
        self.assertFalse(bad['passed'])
        self.assertAlmostEqual(bad['initial_foot_clearance_m'], 0.12, places=5)

        # Standing on a supporting object under root[0, [0, 2]] measures clearance relative to object top.
        step_scene = {
            'kind': scene.VERSION,
            'objects': [{'id': 'step1', 'center_m': [0, 0.08, 0], 'size_m': [1.0, 0.16, 0.42], 'heading_deg': 0}],
            'waypoints': [{'frame': 0, 'xz_m': [0, 0], 'heading_deg': 0}],
            'keyframes': [{'frame': 0, 'object_id': 'step1', 'pelvis_offset_m': [0, 0.99, 0],
                           'heading_deg': 0, 'body_rotvecs': [[0, 0, 0]] * 21}],
            'contacts': [],
        }
        on_step = {k: v.copy() for k, v in arrays.items()}
        on_step['native_transl'][:, 1] += 0.16
        step_rep = scene.report(on_step, step_scene, rest, parents)
        self.assertTrue(step_rep['passed'], step_rep)
        self.assertAlmostEqual(step_rep['initial_foot_clearance_m'], 0.0, places=5)

    def test_attachments_validation_and_bounds(self):
        data = fixture()
        data['attachments'] = [{
            'object_id': 'box', 'start_frame': 10, 'end_frame': 25,
            'mode': 'carry', 'joints': [20, 21],
            'anchors_m': [[0.5, 0.0, 0.0], [-0.5, 0.0, 0.0]],
        }]
        scene.check(data, 30)
        encoded = scene.encode(data)
        self.assertEqual(scene.decode(encoded, 30), data)

        # Reject invalid attachment variants.
        bad_cases = [
            dict(data, attachments=[dict(data['attachments'][0], anchors_m=[[0.4, 0.0, 0.0], [-0.5, 0.0, 0.0]])]),
            dict(data, attachments=[dict(data['attachments'][0], mode='throw')]),
            dict(data, attachments=[dict(data['attachments'][0], start_frame=25, end_frame=25)]),
            dict(data, attachments=[dict(data['attachments'][0], end_frame=31)]),
            dict(data, attachments=[dict(data['attachments'][0], joints=[20, 20])]),
            dict(data, attachments=[dict(data['attachments'][0], joints=[19], anchors_m=[[0.5, 0, 0]])]),
            dict(data, attachments=[dict(data['attachments'][0], object_id='missing')]),
            dict(data, attachments=[dict(data['attachments'][0]) for _ in range(9)]),
        ]
        for bad in bad_cases:
            with self.assertRaises(ValueError):
                scene.check(bad, 30)

    def test_video_pose_fast_travel_stays_within_pelvis_offset_bounds(self):
        from g2h import video_pose
        from g2h.human_rig import load

        rest, _ = load()
        arr = {
            'smpl_poses': np.zeros((150, 156)),
            'native_transl': np.column_stack([np.zeros(150), np.zeros(150), np.linspace(0.0, 12.0, 150)]),
        }
        cond = video_pose.scene_conditions(arr, np.ones(150), [{'start_frame': 0, 'end_frame': 150}], 0, 150)
        sc = cond['scene']
        scene.check(sc, 150)
        for kf in sc['keyframes']:
            if kf['frame'] > 0:
                self.assertTrue(all(abs(v) <= 5.0 for v in kf['pelvis_offset_m']), kf)
        indices, roots, _, _, _ = scene.world_targets(sc, 150)
        expected = (arr['native_transl'] + rest[0])[indices]
        np.testing.assert_allclose(roots, expected, atol=1e-4)

    def test_video_multi_person_occlusion_and_discontinuous_observations(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        import video_observations
        from g2h import video_pose, video_shots

        # unit() zero-norm fallback & degenerate torso_frame fallback
        np.testing.assert_allclose(video_pose.unit([0.0, 0.0, 0.0]), [0.0, 0.0, 1.0])
        pts = np.zeros((16, 33, 3))
        mats = video_pose.torso_frame(pts)
        self.assertEqual(mats.shape, (16, 3, 3))
        self.assertTrue(np.isfinite(mats).all())

        # Heavy occlusion: early/late gap > 5 frames, distal joint completely missing, sparse anchors
        frames = 45
        world = np.full((frames, 33, 3), np.nan, dtype=np.float32)
        conf = np.zeros((frames, 33), dtype=np.float32)
        for t in (8, 22, 36):
            world[t, 11] = [0.2, 1.4, 0.0]
            world[t, 12] = [-0.2, 1.4, 0.0]
            world[t, 23] = [0.1, 0.9, 0.0]
            world[t, 24] = [-0.1, 0.9, 0.0]
            world[t, 25] = [0.1, 0.5, 0.0]
            world[t, 27] = [0.1, 0.1, 0.0]
            conf[t, [11, 12, 23, 24, 25, 27]] = 0.9
        # Shot 1: [0, 15), Shot 2: [15, 45) with discontinuous pose jump
        world[22, 27] = [0.8, 0.9, 0.6]
        shots = [{'start_frame': 0, 'end_frame': 15}, {'start_frame': 15, 'end_frame': 45}]
        arr, rel, reports = video_pose.fit_observations(world, conf, shots)
        self.assertTrue(np.isfinite(arr['smpl_poses']).all())
        self.assertTrue(np.isfinite(arr['native_transl']).all())
        cond = video_pose.scene_conditions(arr, rel * 0.1, shots, 0, 20)
        self.assertGreaterEqual(len(cond['scene']['keyframes']), 3)

        # Multi-person tracking selection in video_shots.tracked_landmarks
        def make_person(cx, cy, size, vis):
            world_lms = [SimpleNamespace(x=0.0, y=-1.0, z=0.0) for _ in range(33)]
            screen_lms = [SimpleNamespace(x=cx, y=cy, z=0.0, visibility=vis, presence=vis) for _ in range(33)]
            for idx, (dx, dy) in ((11, (size, -size)), (12, (-size, -size)), (23, (size, size)), (24, (-size, size))):
                screen_lms[idx] = SimpleNamespace(x=cx + dx, y=cy + dy, z=0.0, visibility=vis, presence=vis)
            return world_lms, screen_lms

        p_big_w, p_big_s = make_person(0.2, 0.5, 0.2, 0.95)
        p_bg_w, p_bg_s = make_person(0.8, 0.5, 0.05, 0.95)
        p_big_w[0] = SimpleNamespace(x=0.25, y=-1.0, z=0.0)
        p_bg_w[0] = SimpleNamespace(x=0.85, y=-1.0, z=0.0)
        frame_result = SimpleNamespace(pose_world_landmarks=[p_bg_w, p_big_w], pose_landmarks=[p_bg_s, p_big_s])
        tracker = MagicMock()
        tracker.__enter__.return_value = tracker
        tracker.detect_for_video.return_value = frame_result

        def fake_reader(*args, **kwargs):
            yield {'size': (4, 4)}
            yield bytes(4 * 4 * 3)

        shot_list = [{'start_frame': 0, 'end_frame': 1, 'path': 'dummy.mp4'}]
        fake_ffmpeg = SimpleNamespace(read_frames=fake_reader)
        fake_mp = SimpleNamespace(
            Image=lambda **kw: kw,
            ImageFormat=SimpleNamespace(SRGB='SRGB'),
            tasks=SimpleNamespace(
                BaseOptions=lambda **kw: kw,
                vision=SimpleNamespace(
                    RunningMode=SimpleNamespace(VIDEO='VIDEO'),
                    PoseLandmarkerOptions=lambda **kw: kw,
                    PoseLandmarker=SimpleNamespace(create_from_options=lambda opts: tracker),
                ),
            ),
        )
        with patch.dict(sys.modules, {'imageio_ffmpeg': fake_ffmpeg, 'mediapipe': fake_mp}):
            tracked_w, _, _ = video_shots.tracked_landmarks(shot_list, Path('pose.task'))
        self.assertEqual(shot_list[0]['multi_person_frames'], 1)
        self.assertAlmostEqual(float(tracked_w[0, 0, 0]), 0.25, places=5)

        # Discontinuous 2-shot preparation pads short shot to >= 30 frames & preserves sum(keep_frames)
        with tempfile.TemporaryDirectory() as tmp:
            fake_shots = [{'start_frame': 0, 'end_frame': 15}, {'start_frame': 15, 'end_frame': 45}]
            fake_reports = [
                {'start_frame': 0, 'end_frame': 15, 'cut_joint_jump_m': 0.0, 'continuous': True},
                {'start_frame': 15, 'end_frame': 45, 'cut_joint_jump_m': 0.8, 'continuous': False},
            ]
            with patch.object(video_observations, 'prepare_shots', return_value=fake_shots), \
                 patch.object(video_observations, 'tracked_landmarks', return_value=(world, world, conf)), \
                 patch.object(video_observations, 'fit_observations', return_value=(arr, np.ones(frames), fake_reports)):
                manifest = video_observations.prepare(Path('in.mp4'), Path('pose.task'), Path(tmp))
            self.assertEqual(len(manifest['segments']), 2)
            self.assertEqual(manifest['segments'][0]['overlap'], 0)
            self.assertEqual(manifest['segments'][1]['overlap'], 0)
            self.assertEqual(sum(s['keep_frames'] for s in manifest['segments']), frames)
            for seg in manifest['segments']:
                scene.check(seg['request']['scene'], seg['request']['duration'] * 30)

    def test_preflight_returns_best_effort_when_penetration_reduces_but_exceeds_limit(self):
        from unittest.mock import patch
        from g2h import human_interaction_preflight as pre
        from g2h.human_rig import load
        from g2h.kimodo_adapter import fk
        from scipy.spatial.transform import Rotation

        rest, parents = load()
        pose = np.zeros((52, 3))
        pose[[16, 17]] = [[0, -1.05, -.22], [0, 1.05, .22]]
        root = np.array([0, .945, 0])
        joints, _ = fk(Rotation.from_rotvec(pose).as_matrix()[None], rest, parents)
        hands = joints[0, [20, 21]] + root - rest[0]
        center = hands.mean(axis=0)
        center[1] -= .08
        obj = dict(id='desk', center_m=center.tolist(), size_m=[1.15, .16, .48], heading_deg=0)
        key = dict(frame=29, object_id='desk', pelvis_offset_m=(root-center).tolist(),
                   heading_deg=0, body_rotvecs=pose[1:22].tolist())
        data = dict(kind=scene.VERSION, objects=[obj], keyframes=[key],
                    waypoints=[dict(frame=0, xz_m=[0, 0], heading_deg=0)],
                    contacts=[])
        with patch.object(pre, 'MAX_PENETRATION_M', 1e-4):
            valid, audit = pre.preflight(data, 30, rest, parents)
        row = audit['keyframes'][0]
        self.assertEqual(row['status'], 'best_effort')
        self.assertLess(row['after_m'], row['before_m'])
        self.assertFalse(audit['passed'])
        self.assertNotEqual(valid['keyframes'][0]['body_rotvecs'], key['body_rotvecs'])


if __name__ == '__main__':
    unittest.main()
