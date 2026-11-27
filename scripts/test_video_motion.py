"""Regression tests for owner-scoped video-motion jobs, quotas, cleanup, and 404 handling."""
import base64
from contextlib import closing, nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from g2h import human_rig, resource_quota
from g2h.kimodo_adapter import fk
from review_auth import Auth
from review_server import Store
from scipy.spatial.transform import Rotation
import video_motion


def _fixture_arrays():
    rest, parents, _ = human_rig.definition()
    poses = np.zeros((10, 52, 3), dtype=np.float32)
    mats = Rotation.from_rotvec(poses.reshape(-1, 3)).as_matrix().reshape(10, 52, 3, 3)
    kp3d, _ = fk(mats, rest, parents)
    return dict(
        native_keypoints3d=kp3d.astype(np.float32),
        smpl_poses=poses.reshape(10, 156),
        native_transl=np.zeros((10, 3), dtype=np.float32),
        fps=np.asarray(30),
        parents=parents,
        rest_joints=rest,
    )


class VideoMotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'topics.json'
        self.config.write_text(json.dumps({'topics': []}))
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.store = Store(self.root, self.config, self.assets)
        auth = Auth(self.root / 'review')
        auth.create('alice', 'fixture-password-123')
        with closing(auth.connect()) as db, db:
            db.execute("UPDATE users SET must_change=0 WHERE username='alice'")
        from harness_service import HarnessService
        self.service = HarnessService(self.root, legacy_store=self.store)
        self.user = {'username': 'alice', 'role': 'user'}

    def test_missing_job_raises_key_error_and_returns_http_404(self):
        from test_review_access import AccessTests

        missing = 'a' * 64
        with self.assertRaises(KeyError):
            video_motion.status(self.service, self.user, missing)

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        status, body = fixture.call('/api/harness/video-motion/' + missing)
        self.assertEqual(status, 404, body)
        self.assertIn('不存在', body['error'])

    def test_submit_enqueues_once_checks_quota_and_get_status_does_not_submit_pool(self):
        raw = b'fake-video-bytes-0123456789'
        payload = {'request_id': 'vid-req-0001', 'video_base64': base64.b64encode(raw).decode()}
        before = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['storage_bytes']['used']
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit') as submit_pool, \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            job = video_motion.submit(self.service, self.user, payload)
            submit_pool.assert_called_once()
            # Polling status must not re-submit into the worker pool.
            polled = video_motion.status(self.service, self.user, job['id'])
            self.assertEqual(polled['id'], job['id'])
            submit_pool.assert_called_once()
        after = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['storage_bytes']['used']
        self.assertGreater(after, before)

        # Exhausted quota fails before writing input.video for a new request_id.
        cap = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['storage_bytes']['limit']
        fill = cap - after
        resource_quota.reserve(self.root, 'fill-video', 'alice', {'storage_bytes': fill}, 'user')
        resource_quota.settle(self.root, 'fill-video', {'storage_bytes': fill})
        rejected_key = hashlib.sha256(b'vid-req-0002').hexdigest()
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit') as submit_pool:
            with self.assertRaises(resource_quota.QuotaExceeded):
                video_motion.submit(self.service, self.user, {'request_id': 'vid-req-0002', 'video_base64': payload['video_base64']})
            submit_pool.assert_not_called()
        self.assertFalse((video_motion.owner_folder(self.service, self.user) / rejected_key / 'input.video').exists())

    def test_run_cleans_intermediate_files_on_done_and_failed_and_assemble_charges_quota(self):
        raw = b'fake-video-bytes-abcdef'
        payload = {'request_id': 'vid-clean-0001', 'video_base64': base64.b64encode(raw).decode()}
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit'), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            job = video_motion.submit(self.service, self.user, payload)
        folder = video_motion.owner_folder(self.service, self.user) / job['id']
        arrays = _fixture_arrays()
        np.savez_compressed(folder / 'observed.npz', smpl_poses=arrays['smpl_poses'], confidence=np.ones(10, dtype=np.float32))
        (folder / 'reference.json').write_text(json.dumps({
            'frames': 10,
            'shots': [{'start_frame': 0, 'end_frame': 10}],
            'segments': [{'start': 0, 'end': 10, 'request': {'text': 'stand', 'duration': 1, 'scene': {}}}],
        }))
        seg_asset = self.service.assets.register('alice', arrays, description='seg 0')
        before = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['storage_bytes']['used']
        with patch.object(self.service, 'scene', return_value={'id': 't1', 'operations': [], 'candidates': [{'motion_id': seg_asset['id']}]}), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            video_motion.run(self.service, self.user, folder)
        done_job = video_motion.status(self.service, self.user, job['id'])
        self.assertEqual(done_job['status'], 'done', done_job)
        self.assertFalse((folder / 'input.video').exists())
        self.assertFalse((folder / 'observed.npz').exists())
        after = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['storage_bytes']['used']
        self.assertEqual(after - before, self.service.assets.artifact_size('alice', done_job['motion_id']))

        # Failure path must also clean input.video and observed.npz.
        payload_fail = {'request_id': 'vid-clean-0002', 'video_base64': base64.b64encode(raw).decode()}
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit'), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            fail_job = video_motion.submit(self.service, self.user, payload_fail)
        fail_folder = video_motion.owner_folder(self.service, self.user) / fail_job['id']
        (fail_folder / 'observed.npz').write_bytes(b'obs')
        with patch('video_motion.subprocess.run', return_value=Mock(returncode=1)):
            video_motion.run(self.service, self.user, fail_folder)
        self.assertEqual(video_motion.status(self.service, self.user, fail_job['id'])['status'], 'failed')
        self.assertFalse((fail_folder / 'input.video').exists())
        self.assertFalse((fail_folder / 'observed.npz').exists())

    def test_discontinuous_shots_soft_quality_and_clean_error_stripping(self):
        raw = b'fake-video-bytes-discontinuous'
        payload = {'request_id': 'vid-soft-0001', 'video_base64': base64.b64encode(raw).decode()}
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit'), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            job = video_motion.submit(self.service, self.user, payload)
        folder = video_motion.owner_folder(self.service, self.user) / job['id']
        arrays = _fixture_arrays()
        # Shift observed pose far (> 0.15m MPJPE) to ensure soft warning still completes & publishes.
        shifted_poses = arrays['smpl_poses'].reshape(10, 52, 3).copy()
        shifted_poses[:, 1:, 2] = 1.5
        np.savez_compressed(folder / 'observed.npz', smpl_poses=shifted_poses.reshape(10, 156), confidence=np.ones(10, dtype=np.float32))
        (folder / 'reference.json').write_text(json.dumps({
            'frames': 10,
            'shots': [{'start_frame': 0, 'end_frame': 5}, {'start_frame': 5, 'end_frame': 10}],
            'segments': [
                {'start': 0, 'end': 5, 'overlap': 0, 'keep_frames': 5, 'request': {'text': 'shot 1', 'duration': 1, 'scene': {}}},
                {'start': 5, 'end': 10, 'overlap': 0, 'keep_frames': 5, 'request': {'text': 'shot 2', 'duration': 1, 'scene': {}}},
            ],
        }))
        seg_asset = self.service.assets.register('alice', arrays, description='seg')
        with patch.object(self.service, 'scene', return_value={'id': 't1', 'operations': [], 'candidates': [{'motion_id': seg_asset['id']}]}), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            video_motion.run(self.service, self.user, folder)
        done = video_motion.status(self.service, self.user, job['id'])
        self.assertEqual(done['status'], 'done', done)
        self.assertFalse(done['quality']['passed'])
        self.assertTrue(done['quality'].get('warnings'))

        # Verify exception prefixes in observations.log tail are stripped cleanly.
        payload2 = {'request_id': 'vid-err-0002', 'video_base64': base64.b64encode(raw).decode()}
        with patch.object(video_motion, 'available', return_value=True), \
             patch.object(self.service, '_scene_enabled', return_value=True), \
             patch.object(video_motion.POOL, 'submit'), \
             patch('g2h.capacity.reservation', side_effect=lambda *a, **k: nullcontext(Mock())):
            err_job = video_motion.submit(self.service, self.user, payload2)
        err_folder = video_motion.owner_folder(self.service, self.user) / err_job['id']
        def write_noisy_log(*args, **kwargs):
            (err_folder / 'observations.log').write_text('Traceback (most recent call last):\nRuntimeError: ValueError: 姿态跟踪关键点不足\n')
            return Mock(returncode=1)
        with patch('video_motion.subprocess.run', side_effect=write_noisy_log):
            video_motion.run(self.service, self.user, err_folder)
        failed = video_motion.status(self.service, self.user, err_job['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error'], '姿态跟踪关键点不足')


if __name__ == '__main__':
    unittest.main()
