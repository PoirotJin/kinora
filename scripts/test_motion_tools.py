"""CPU-only tests for scoped motion versions, patch insertion and model slots."""
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np

from g2h.motion_assets import MotionAssets, load_numeric_npz, validate_motion
from g2h.motion_backends import BackendUnavailable, MotionBackends
from g2h.motion_tools import MotionTools


def motion(frames=12, aliases=True):
    points = np.zeros((frames, 52, 3), dtype=np.float32)
    points[:, :, 1] = np.linspace(0.1, 1.5, 52)
    root = np.zeros((frames, 3), dtype=np.float32)
    root[:, 2] = np.arange(frames) * 0.02
    result = {'fps': np.asarray(30), 'native_keypoints3d': points,
              'native_transl': root, 'smpl_poses': np.zeros((frames, 156), dtype=np.float32)}
    if aliases:
        matrices = np.broadcast_to(np.eye(3, dtype=np.float32), (frames, 52, 3, 3))
        result.update(smpl_trans=root.copy(), smpl_Rh=np.zeros((frames, 3), dtype=np.float32),
                      native_root_rotations_mat=matrices[:, 0].copy(),
                      native_rot6d=matrices[..., :2].reshape(frames, 52, 6).copy(),
                      smpl_num_frames=np.asarray(frames), smpl_mocap_framerate=np.asarray(30),
                      smpl_betas=np.zeros((1, 16), dtype=np.float32))
    return result


class MotionDomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assets = MotionAssets(self.temp.name)
        self.owner = 'alice'
        self.source = self.assets.register(self.owner, motion(), description='Walk forward')
        self.tools = MotionTools(self.assets)

    def test_existing_four_field_import_is_supported(self):
        value = validate_motion(motion(aliases=False))
        self.assertEqual(len(value), 4)

    def test_npz_object_array_rejected_without_pickle(self):
        path = Path(self.temp.name) / 'untrusted.npz'
        arrays = motion()
        arrays['extra'] = np.asarray([{'untrusted': 'payload'}], dtype=object)
        np.savez(path, **arrays)
        with self.assertRaises(ValueError):
            load_numeric_npz(path)

    def test_non_archive_payload_is_a_validation_error(self):
        path = Path(self.temp.name) / 'not-an-archive.npz'
        path.write_bytes(b'not a zip archive')
        with self.assertRaises(ValueError):
            load_numeric_npz(path)

    def test_forged_numpy_header_rejected_before_allocation(self):
        import io
        path = Path(self.temp.name) / 'forged.npz'
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, {'descr': '<f8', 'fortran_order': False, 'shape': (10**12, 3)})
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('native_transl.npy', header.getvalue())
        with self.assertRaisesRegex(ValueError, 'allocation'):
            load_numeric_npz(path)

    def test_native_joint_root_drift_does_not_change_root_trajectory_measurement(self):
        arrays = motion()
        arrays['native_keypoints3d'][:, :, 0] += np.arange(12)[:, None] * 3
        source = self.assets.register(self.owner, arrays)
        report = self.tools.inspect(self.owner, source['id'])
        self.assertAlmostEqual(report['metrics']['mean_root_speed_m_s'], 0.6, places=5)
        self.assertEqual(report['metrics']['coordinate_origin'], 'unspecified_rest_root')

    def test_zip_path_and_duplicate_fields_rejected(self):
        path = Path(self.temp.name) / 'traversal.npz'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../x.npy', b'not arrays')
        with self.assertRaises(ValueError):
            load_numeric_npz(path)

    def test_integrity_and_account_isolation(self):
        with self.assertRaises(KeyError):
            self.assets.load('bob', self.source['id'])
        self.assertEqual(self.assets.list('bob'), [])
        for motion_id in ('../private', '/etc/passwd', 'ma_' + '0' * 32 + '/x'):
            with self.assertRaises(ValueError):
                self.assets.get(self.owner, motion_id)
        path = self.assets.artifact_path(self.owner, self.source['id'])
        path.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.assets.load(self.owner, self.source['id'])

    def test_input_is_copied_and_source_remains_immutable(self):
        arrays = motion()
        stored = self.assets.register(self.owner, arrays)
        arrays['native_transl'][:] = 100
        saved, _ = self.assets.load(self.owner, stored['id'])
        self.assertLess(saved['native_transl'].max(), 1)

    def test_duplicate_request_and_argument_mismatch(self):
        first = self.assets.register(self.owner, motion(), request_id='same')
        second = self.assets.register(self.owner, motion(), request_id='same')
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(ValueError):
            self.assets.register(self.owner, motion(), description='different', request_id='same')
        other = self.assets.register('bob', motion(), request_id='same')
        self.assertNotEqual(first['id'], other['id'])

    def test_patch_reinsertion_preserves_all_other_bytes_and_lineage(self):
        arrays, _ = self.assets.load(self.owner, self.source['id'])
        original = {key: value.copy() for key, value in arrays.items()}
        arrays['native_transl'][4:8, 0] = 0.1
        arrays['smpl_trans'][4:8, 0] = 0.1
        arrays['native_keypoints3d'][4:8, 20, 0] = 0.2
        edited = self.assets.apply_patch(self.owner, self.source['id'], 4, 8, arrays,
                                         expected_sha256=self.source['sha256'], request_id='edit-1')
        candidate, _ = self.assets.load(self.owner, edited['id'])
        self.assertEqual(edited['parent_id'], self.source['id'])
        self.assertEqual(edited['parent_sha256'], self.source['sha256'])
        self.assertEqual(edited['semantic_status'], 'unknown')
        for key, before in original.items():
            if before.ndim and before.shape[0] == 12:
                self.assertEqual(before[:4].tobytes(), candidate[key][:4].tobytes())
                self.assertEqual(before[8:].tobytes(), candidate[key][8:].tobytes())
        saved_source, _ = self.assets.load(self.owner, self.source['id'])
        self.assertEqual(saved_source['native_transl'].tobytes(), original['native_transl'].tobytes())

    def test_segment_only_patch_supported_with_unchanged_static_fields(self):
        arrays, _ = self.assets.load(self.owner, self.source['id'])
        static = {'fps', 'smpl_num_frames', 'smpl_mocap_framerate', 'smpl_betas'}
        patch = {key: value.copy() if key in static else value[2:5].copy() for key, value in arrays.items()}
        patched = self.assets.apply_patch(self.owner, self.source['id'], 2, 5, patch, expected_sha256=self.source['sha256'])
        self.assertEqual(patched['frames'], 12)

    def test_outside_change_rejected_including_negative_zero(self):
        arrays, _ = self.assets.load(self.owner, self.source['id'])
        arrays['native_keypoints3d'][0, 0, 0] = -0.
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.assets.apply_patch(self.owner, self.source['id'], 4, 8, arrays, expected_sha256=self.source['sha256'])

    def test_static_change_missing_field_or_hash_rejected(self):
        arrays, _ = self.assets.load(self.owner, self.source['id'])
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.assets.apply_patch(self.owner, self.source['id'], 4, 8, arrays, expected_sha256='wrong')
        arrays['smpl_betas'][0, 0] = 0.1
        with self.assertRaisesRegex(ValueError, 'static'):
            self.assets.apply_patch(self.owner, self.source['id'], 4, 8, arrays, expected_sha256=self.source['sha256'])
        del arrays['fps']
        with self.assertRaises(ValueError):
            self.assets.apply_patch(self.owner, self.source['id'], 4, 8, arrays, expected_sha256=self.source['sha256'])

    def test_shape_finite_fps_aliases_and_rotation_validation(self):
        for key, change in [
            ('fps', lambda a: np.asarray(29)),
            ('native_transl', lambda a: np.zeros((11, 3), np.float32)),
            ('native_keypoints3d', lambda a: a * np.nan),
            ('smpl_trans', lambda a: a + 1),
            ('smpl_Rh', lambda a: a + 1),
            ('native_root_rotations_mat', lambda a: a * 2),
            ('native_rot6d', lambda a: a * 2),
            ('smpl_num_frames', lambda a: np.asarray(100)),
        ]:
            arrays = motion()
            arrays[key] = change(arrays[key])
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_motion(arrays)

    def test_joint_pose_consistency_is_unknown_without_rig_and_checked_with_rig(self):
        self.assertEqual(self.source['joint_pose_consistency_status'], 'unknown')
        arrays = motion()
        arrays['parents'] = np.asarray([-1] + [0] * 51, dtype=np.int32)
        arrays['rest_joints'] = arrays['native_keypoints3d'][0].copy()
        source = self.assets.register(self.owner, arrays)
        self.assertEqual(source['joint_pose_consistency_status'], 'passed')
        arrays['native_keypoints3d'][5, 20, 0] = 0.1
        with self.assertRaisesRegex(ValueError, 'rest rig'):
            self.assets.register(self.owner, arrays)

    def test_interval_validation(self):
        arrays, _ = self.assets.load(self.owner, self.source['id'])
        for interval in [(-1, 5), (5, 5), (0, 13), (True, 5), (0., 5)]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.assets.apply_patch(self.owner, self.source['id'], *interval, arrays, expected_sha256=self.source['sha256'])

    def test_unknown_extra_frame_channel_is_protected(self):
        arrays = motion()
        arrays['aux_contacts'] = np.zeros((12, 4), dtype=np.float32)
        source = self.assets.register(self.owner, arrays)
        arrays['aux_contacts'][10, 0] = 1
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.assets.apply_patch(self.owner, source['id'], 4, 8, arrays, expected_sha256=source['sha256'])

    def test_readonly_context_copies(self):
        context = self.assets.context(self.owner, self.source['id'], 4, 8, padding=2)
        self.assertEqual(context['window_start_frame'], 2)
        self.assertEqual(context['window_end_frame'], 10)
        self.assertTrue(context['read_only'])
        with self.assertRaises(ValueError):
            context['arrays']['native_transl'][0, 0] = 1

    def test_inspect_reports_numbers_but_unknown_semantics_and_dynamics(self):
        report = self.tools.inspect(self.owner, self.source['id'], 3, 9)
        self.assertAlmostEqual(report['metrics']['mean_root_speed_m_s'], 0.6, places=5)
        evidence = {value['kind']: value['status'] for value in report['evidence']}
        self.assertEqual(evidence['numeric_integrity'], 'passed')
        for key in ('semantic_alignment', 'dynamics', 'boundary_quality'):
            self.assertEqual(evidence[key], 'unknown')

    def test_root_jitter_repair_is_bounded_and_synchronizes_aliases(self):
        arrays = motion()
        arrays['native_transl'][6, 0] = 0.5
        arrays['smpl_trans'] = arrays['native_transl'].copy()
        source = self.assets.register(self.owner, arrays)
        edited = self.tools.repair_root_jitter(self.owner, source['id'], 3, 10,
                                               expected_sha256=source['sha256'], strength=0.5,
                                               passes=3, max_displacement_m=0.04)
        after, _ = self.assets.load(self.owner, edited['id'])
        self.assertLess(after['native_transl'][6, 0], 0.5)
        self.assertLessEqual(float(np.linalg.norm(after['native_transl'] - arrays['native_transl'], axis=-1).max()), 0.040001)
        np.testing.assert_array_equal(after['native_transl'], after['smpl_trans'])
        for key in ('native_keypoints3d', 'smpl_poses', 'native_rot6d'):
            np.testing.assert_array_equal(after[key], arrays[key])
        for first, last in ((0, 4), (9, 12)):
            self.assertEqual(after['native_transl'][first:last].tobytes(), arrays['native_transl'][first:last].tobytes())
        np.testing.assert_array_equal(after['native_transl'][:, 1], arrays['native_transl'][:, 1])

    def test_root_repair_rejects_unknown_dynamic_coordinates(self):
        arrays = motion()
        arrays['aux_world_positions'] = np.zeros((12, 2, 3), np.float32)
        source = self.assets.register(self.owner, arrays)
        with self.assertRaisesRegex(ValueError, 'unknown dynamic'):
            self.tools.repair_root_jitter(self.owner, source['id'], 3, 10, expected_sha256=source['sha256'])

    def test_disabled_kimodo_and_semantic_editor_are_explicit(self):
        backends = MotionBackends()
        caps = {value['name']: value for value in backends.capabilities()}
        self.assertFalse(caps['kimodo']['available'])
        self.assertFalse(caps['local_editor']['available'])
        for name in ('kimodo', 'local_editor'):
            with self.assertRaises(BackendUnavailable):
                backends.invoke(name, text='test')
        seen = []
        configured = MotionBackends(generator=lambda **kw: seen.append(kw) or {'job_id': 'existing'})
        self.assertEqual(configured.invoke('hy_motion', request_id='stable')['job_id'], 'existing')
        self.assertEqual(seen, [{'request_id': 'stable'}])


if __name__ == '__main__':
    unittest.main()
