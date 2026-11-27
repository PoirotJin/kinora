"""Two-clip splice: rigid transforms, placeholder join, seam gates and task flow."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from g2h import motion_splice as ms
from g2h.kimodo_adapter import fk
from g2h.kimodo_edit import window, compose
from g2h.motion_assets import validate_motion
from test_kimodo_adapter import fixture as rig_fixture
from test_motion_edits import QueueFixture


def walker(frames=90, swing=.3):
    """Canonical SMPL-H walk along +Z with a swinging left shoulder."""
    _, _, rest, parents = rig_fixture()
    local = np.broadcast_to(np.eye(3), (frames, 52, 3, 3)).copy()
    local[:, 16] = Rotation.from_euler('z', swing * np.sin(np.arange(frames) / 6)).as_matrix()
    points, _ = fk(local, rest, parents)
    root = np.column_stack((np.zeros(frames), np.ones(frames), np.arange(frames) / 30)).astype('float32')
    poses = Rotation.from_matrix(local.reshape(-1, 3, 3)).as_rotvec().reshape(frames, 156)
    arrays = {'fps': np.array(30), 'native_keypoints3d': points.astype('float32'), 'native_transl': root,
              'smpl_trans': root.copy(), 'smpl_poses': poses.astype('float32'), 'smpl_Rh': poses[:, :3].astype('float32'),
              'native_root_rotations_mat': local[:, 0].astype('float32'),
              'native_rot6d': local[..., :2].reshape(frames, 52, 6).astype('float32'),
              'kimodo_posed_joints': points.astype('float32')}
    return arrays, rest, parents


class TransformTests(unittest.TestCase):
    def test_turn_right_rotates_root_only_about_first_kept_frame(self):
        arrays, rest, parents = walker()
        out = ms.transform(arrays, 30, 90, 90.0, .5, -.25)
        self.assertEqual(len(out['native_transl']), 60)
        self.assertNotIn('kimodo_posed_joints', out)
        np.testing.assert_allclose(out['native_transl'][0], arrays['native_transl'][30] + [.5, 0, -.25], atol=1e-6)
        travel = out['native_transl'][-1] - out['native_transl'][0]
        self.assertLess(travel[0], -1.9); self.assertAlmostEqual(float(travel[2]), 0, places=5)
        self.assertAlmostEqual(ms.heading(out, 0), -np.pi / 2, places=5)
        np.testing.assert_array_equal(out['smpl_poses'][:, 3:], arrays['smpl_poses'][30:90, 3:])
        validate_motion(dict(out, rest_joints=rest, parents=parents))
        np.testing.assert_array_equal(arrays['native_transl'][:, 2], np.arange(90, dtype='float32') / 30)

    def test_bounds(self):
        arrays, _, _ = walker()
        for args in ((0, 5), (50, 40), (0, 91)):
            with self.assertRaises(ValueError): ms.transform(arrays, *args)
        with self.assertRaises(ValueError): ms.transform(arrays, 0, 90, float('nan'))
        with self.assertRaises(ValueError): ms.transform(arrays, 0, 90, 0.0, 500.0)


class JoinTests(unittest.TestCase):
    def parts(self, gap_z=1.0, turn=0.0):
        arrays, rest, parents = walker()
        a = ms.transform(arrays, 0, 60)
        b = ms.transform(arrays, 0, 60, turn, 0.0, float(a['native_transl'][-1, 2]) + gap_z)
        return a, b, rest, parents

    def test_join_is_continuous_and_valid(self):
        a, b, rest, parents = self.parts()
        joined, report = ms.join(a, b, 30, rest, parents)
        self.assertEqual(report['selection'], {'start_frame': 60, 'end_frame': 90})
        self.assertEqual(len(joined['native_transl']), 150)
        np.testing.assert_array_equal(joined['smpl_poses'][:60], a['smpl_poses'])
        np.testing.assert_array_equal(joined['smpl_poses'][90:], b['smpl_poses'])
        world = joined['native_keypoints3d'] - joined['native_keypoints3d'][:, :1] + joined['native_transl'][:, None]
        self.assertLess(float(np.linalg.norm(np.diff(world, axis=0), axis=-1).max()), .2)

    def test_legacy_hand_template_is_rederived_but_body_mismatch_rejected(self):
        a, b, rest, parents = self.parts()
        legacy = dict(a, native_keypoints3d=a['native_keypoints3d'].copy())
        legacy['native_keypoints3d'][:, 30:] += np.float32(.05)
        joined, report = ms.join(legacy, b, 30, rest, parents)
        self.assertAlmostEqual(report['rig_conformance'][0]['max_hand_shift_m'], np.sqrt(3) * .05, places=4)
        self.assertLess(report['rig_conformance'][0]['body_shift_max_m'], 1e-5)
        np.testing.assert_allclose(joined['native_keypoints3d'][:60], a['native_keypoints3d'], atol=1e-5)
        np.testing.assert_array_equal(joined['smpl_poses'][:60], a['smpl_poses'])
        broken = dict(a, native_keypoints3d=a['native_keypoints3d'].copy()); broken['native_keypoints3d'][:, 5] += np.float32(.05)
        with self.assertRaisesRegex(ValueError, '身体骨架'): ms.join(broken, b, 30, rest, parents)

    def test_feasibility_gate(self):
        a, b, rest, parents = self.parts(gap_z=5.0)
        with self.assertRaisesRegex(ValueError, '至少需要'): ms.join(a, b, 30, rest, parents)
        a, b, rest, parents = self.parts(turn=170.0)
        with self.assertRaisesRegex(ValueError, '转向'): ms.join(a, b, 30, rest, parents)
        ms.join(a, b, 60, rest, parents)
        with self.assertRaises(ValueError): ms.join(a, b, 5, rest, parents)

    def test_cubic_splice_join_records_speed_warning_instead_of_blocking(self):
        from g2h import cubic_splice
        a, b, rest, parents = self.parts(gap_z=5.0)
        with self.assertRaisesRegex(ValueError, '至少需要'):
            cubic_splice.check(a, b, 30)
        joined, report = cubic_splice.join(a, b, 30)
        self.assertEqual(len(joined['native_transl']), 150)
        self.assertIn('speed_warning', report)
        self.assertIn('至少需要', report['speed_warning'])

    def test_regenerated_gap_passes_existing_edit_seam_gates(self):
        a, b, rest, parents = self.parts(turn=45.0)
        joined, report = ms.join(a, b, 45, rest, parents)
        start, end = report['selection'].values()
        plan = window(len(joined['native_transl']), start, end)
        n = plan['frames']; local = np.broadcast_to(np.eye(3), (n, 52, 3, 3)).copy()
        local[:, 0] = Rotation.from_euler('y', np.linspace(0, -np.pi / 4, n)).as_matrix()
        local[:, 17] = Rotation.from_euler('z', .3 * np.sin(np.arange(n) / 5)).as_matrix()
        points, _ = fk(local, rest, parents)
        poses = Rotation.from_matrix(local.reshape(-1, 3, 3)).as_rotvec().reshape(n, 156)
        generated = {'fps': np.array(30), 'native_keypoints3d': points.astype('float32'),
                     'native_transl': joined['native_transl'][plan['start']:plan['end']].copy(),
                     'smpl_poses': poses.astype('float32')}
        result, seam = compose(joined, generated, start, end, plan, rest, parents)
        self.assertTrue(seam['passed'], seam)
        np.testing.assert_array_equal(result['smpl_poses'][:start], joined['smpl_poses'][:start])
        np.testing.assert_array_equal(result['smpl_poses'][end:], joined['smpl_poses'][end:])


class WalkPromptTests(unittest.TestCase):
    def place(self, dx, dz, turn=0.0):
        arrays, _, _ = walker()
        a = ms.transform(arrays, 0, 60)
        end = a['native_transl'][-1]
        return a, ms.transform(arrays, 0, 60, turn, float(end[0]) + dx, float(end[2]) + dz)

    def test_direction_is_relative_to_a_facing(self):
        cases = {(0, 2): 'forward,', (-2, 0): 'to the right,', (2, 0): 'to the left,',
                 (-1.5, 1.5): 'forward and to the right,', (0, -2): 'destination initially back, then walks naturally about 2.0 meters forward,'}
        for (dx, dz), words in cases.items():
            text, info = ms.walk_prompt(*self.place(dx, dz), 60)
            self.assertIn(words, text, (dx, dz)); self.assertAlmostEqual(info['distance_m'], np.hypot(dx, dz), places=4)
        # A already turned right 90 deg: world -X is now "forward" for the walker.
        arrays, _, _ = walker(); a = ms.transform(arrays, 0, 60, 90.0); end = a['native_transl'][-1]
        b = ms.transform(arrays, 0, 60, 90.0, float(end[0]) - 2, float(end[2]))
        self.assertIn('2.0 meters forward,', ms.walk_prompt(a, b, 60)[0])

    def test_turn_in_place_and_next_action(self):
        text, info = ms.walk_prompt(*self.place(0, 1, turn=-88.0), 60, 'A person waves both hands. Then sits down.')
        self.assertIn('turning about 90 degrees to the left', text); self.assertGreater(info['turn_left_deg'], 80)
        self.assertNotIn('waves', text); self.assertEqual(info['next_action'], 'waves both hands')
        text, _ = ms.walk_prompt(*self.place(0, .05), 30, '一个人跳舞')
        self.assertIn('a few natural steps almost in place', text); self.assertIn('keeping roughly the same facing', text)
        self.assertTrue(text.endswith('ready to begin the next movement.'))
        self.assertTrue(all(' ' <= c <= '~' for c in text))
        self.assertEqual(ms._next_action('Someone ' + 'x ' * 100), ' '.join(['x'] * 40))
        self.assertEqual(ms._next_action(None), '')


class SpliceServiceTests(QueueFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        arrays, self.rest, self.parents = walker()
        self.source = self.assets.register('alice', arrays)
        self.rig = patch('g2h.motion_splice.rig', return_value=(self.rest, self.parents)); self.rig.start(); self.addCleanup(self.rig.stop)

    def body(self, **kw):
        return dict({'request_id': 'splice_request_123', 'transition_frames': 150, 'clips': [
            {'motion_id': self.source['motion_id'], 'start_frame': 0, 'end_frame': 60,
             'transform': {'turn_right_deg': 0, 'offset_x_m': 0, 'offset_z_m': 0}},
            {'motion_id': self.source['motion_id'], 'start_frame': 10, 'end_frame': 70,
             'transform': {'turn_right_deg': 30.0, 'offset_x_m': 0.2, 'offset_z_m': 2.4}}]}, **kw)

    def test_splice_creates_direct_candidate_once_and_keeps_sources(self):
        from harness_service import HarnessService
        from g2h import cubic_splice
        service = HarnessService(self.root)
        user = {'username': 'alice', 'role': 'user'}
        body = self.body(transition_frames=18)
        body['clips'][1]['transform'] = {'turn_right_deg': 0, 'offset_x_m': 0, 'offset_z_m': 1.63}
        with patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_EDITING': '0'}), patch.object(
                service, 'repair', side_effect=AssertionError('GPU edit submitted')):
            first = service.splice(user, body)
            with patch.object(cubic_splice, 'join', side_effect=AssertionError('splice recomputed')):
                again = service.splice(user, body)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(first['candidates'], again['candidates'])
        self.assertEqual(first['selection'], {'start_frame': 60, 'end_frame': 78})
        self.assertEqual(first['operations'], [])
        self.assertEqual(first['splice']['planner'], cubic_splice.VERSION)
        self.assertTrue(first['splice']['quality']['passed'])
        joined, _ = self.assets.load('alice', first['splice']['joined_motion_id'])
        original, _ = self.assets.load('alice', self.source['motion_id'])
        self.assertEqual(len(joined['native_transl']), 138)
        np.testing.assert_array_equal(joined['smpl_poses'][:60], original['smpl_poses'][:60])
        np.testing.assert_array_equal(joined['native_transl'][:60], original['native_transl'][:60])
        self.assertEqual(self.assets.get('alice', self.source['motion_id'])['sha256'], self.source['sha256'])
        restarted = HarnessService(self.root)
        expected = first['splice']
        self.assertEqual(restarted.detail(user, first['id'])['splice'], expected)
        self.assertEqual(restarted.repair_status(user, first['id'])['splice'], expected)
        accepted = restarted.accept(user, first['id'], {'candidate_id': first['candidates'][0]['id']})
        self.assertEqual(accepted['splice'], expected)
        replayed = restarted.splice(user, body)
        self.assertEqual(replayed['status'], 'accepted')
        self.assertEqual(replayed['splice'], expected)
        with self.queue.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, '时长'):
            service.splice(user, dict(body, transition_frames=8))
        with self.assertRaisesRegex(ValueError, '该请求编号已用于其他拼接'):
            service.splice(user, dict(body, transition_frames=19))

    def test_boundary_preview_is_transformed_and_read_only(self):
        from harness_service import HarnessService
        service=HarnessService(self.root);user={'username':'alice','role':'user'}
        r=service.splice_preview(user,self.body())
        self.assertTrue(r['preview_only']);self.assertEqual(len(r['poses']),2);self.assertEqual(len(r['poses'][0]),52)
        arrays,_=self.assets.load('alice',self.source['motion_id']);clip=self.body()['clips'][1];t=clip['transform']
        transformed=ms.transform(arrays,10,70,t['turn_right_deg'],t['offset_x_m'],t['offset_z_m'])
        expected=(transformed['native_keypoints3d'][0]-transformed['native_keypoints3d'][0,:1]
                  +self.rest[0]+transformed['native_transl'][0])
        np.testing.assert_allclose(r['poses'][1],expected)
        self.assertEqual(self.assets.get('alice',self.source['motion_id'])['sha256'],self.source['sha256'])
        with self.queue.db() as d:self.assertEqual(d.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        for n in (None,True,8,541):
            with self.assertRaises(ValueError):service.splice_preview(user,self.body(transition_frames=n))

    def test_boundary_preview_and_result_share_world_coordinates_for_offset_rest_origins(self):
        from harness_service import HarnessService

        user = {'username': 'alice', 'role': 'user'}
        service = HarnessService(self.root)
        arrays, _ = self.assets.load('alice', self.source['motion_id'])
        first = self.assets.register('alice', dict(arrays, rest_joints=self.rest, parents=self.parents))['id']
        shift = np.array([.7, .25, -.35], dtype=np.float32)
        shifted = dict(arrays, rest_joints=self.rest + shift, parents=self.parents,
                       native_keypoints3d=arrays['native_keypoints3d'] + shift,
                       native_transl=arrays['native_transl'] - shift,
                       smpl_trans=arrays['smpl_trans'] - shift)
        second = self.assets.register('alice', shifted)['id']
        body = self.body(transition_frames=24)
        for clip in body['clips']:
            clip['motion_id'] = first
        body['clips'][1]['transform'] = {'turn_right_deg': 0, 'offset_x_m': 0, 'offset_z_m': 2.1}
        original = service.splice_preview(user, body)
        body['clips'][1]['motion_id'] = second
        actual = service.splice_preview(user, body)
        np.testing.assert_allclose(actual['poses'], original['poses'], atol=1e-6)
        self.assertAlmostEqual(actual['gap']['distance_m'], original['gap']['distance_m'], places=6)
        result = service.splice(user, body)
        rendered = service.preview(user, result['splice']['joined_motion_id'])
        seam = result['splice']['selection']
        np.testing.assert_allclose(actual['poses'], [rendered['joints'][seam['start_frame'] - 1],
                                                    rendered['joints'][seam['end_frame']]], atol=1e-6)
        self.assertAlmostEqual(actual['gap']['distance_m'], result['splice']['gap']['distance_m'], places=6)

    def test_disabled_editor_and_route_permissions(self):
        from harness_service import HarnessService
        from review_access import allowed
        service = HarnessService(self.root); user = {'username': 'alice', 'role': 'user'}
        with patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_EDITING': '0'}):
            self.assertFalse(service.capabilities(user)['editing_enabled'])
            self.assertTrue(service.capabilities(user)['splicing_enabled'])
        self.assertTrue(allowed({'role': 'user'}, '/api/harness/splice', True))
        self.assertFalse(allowed({'role': 'user'}, '/api/harness/splice', False))
        self.assertFalse(allowed({'role': 'viewer'}, '/api/harness/splice', True))
        for bad in ({'clips': []}, {'clips': [{}, {}]}, {'extra': 1}):
            with self.assertRaises(ValueError): ms.validate_request(dict(self.body(), **bad))


if __name__ == '__main__':
    unittest.main()
