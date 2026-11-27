"""Real Store + motion-domain integration, without providers or GPU work."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_service import HarnessService
from g2h.harness import HarnessError
from test_motion_tools import motion


class FakeDriver:
    identity = 'test_driver'
    cost_upper_bound_usd = 0.0
    def __init__(self, actions):
        self.actions = iter(actions)
        self.contexts = []
    def next_action(self, context, tools):
        self.contexts.append(context)
        return next(self.actions)


def encoded(arrays):
    data = io.BytesIO()
    np.savez_compressed(data, **arrays)
    return base64.b64encode(data.getvalue()).decode()


class HarnessServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = {'username': 'alice'}
        self.other = {'username': 'bob'}
        self.service = HarnessService(self.root)
        arrays = motion(aliases=False)
        arrays['native_transl'][6, 0] = 0.5
        self.source = self.service.assets.register('alice', arrays)

    def test_download_preserves_source_and_owner_boundary(self):
        raw = self.service.download(self.user, self.source['id'])
        with np.load(io.BytesIO(raw), allow_pickle=False) as saved:
            self.assertIn('native_transl', saved.files)
        with self.assertRaises((PermissionError, KeyError, ValueError)):
            self.service.download(self.other, self.source['id'])
        for invalid in ('../secret', '/etc/passwd', '', 'a'*97):
            with self.assertRaises(ValueError):
                self.service.download(self.user, invalid)
        self.assertEqual(raw, self.service.download(self.user, self.source['id']))

    def create(self, request_id='task-1', service=None):
        return (service or self.service).create(self.user, {
            'request_id': request_id, 'goal': 'Smooth the selected root translation jitter',
            'source_motion_id': self.source['id'],
            'selection': {'start_frame': 3, 'end_frame': 10},
            'requirements': ['Keep the unselected frames unchanged'],
            'budget': {'max_steps': 10, 'max_attempts': 4, 'max_candidates': 3}})

    def test_real_loop_inspection_repair_review_and_accept(self):
        driver = FakeDriver([
            {'kind': 'tool', 'tool': 'inspect_motion', 'arguments': {}},
            {'kind': 'tool', 'tool': 'repair_root_jitter', 'arguments': {'max_displacement_m': 0.04}},
            {'kind': 'finish', 'message': 'A numerical repair candidate is ready for your review'},
        ])
        service = HarnessService(self.root, driver=driver)
        task = self.create(service=service)
        original, _ = service.assets.load('alice', self.source['id'])
        task = service.advance(self.user, task['id'])
        self.assertEqual(task['operations'][0]['status'], 'succeeded')
        self.assertEqual(task['evidence'][0]['status'], 'unknown')
        task = service.advance(self.user, task['id'])
        self.assertEqual(len(task['candidates']), 1)
        candidate = task['candidates'][0]
        self.assertEqual(candidate['status'], 'pending_review')
        self.assertEqual(candidate['metadata']['semantic_status'], 'unknown')
        patched, metadata = service.assets.load('alice', candidate['motion_id'])
        self.assertEqual(metadata['parent_id'], self.source['id'])
        self.assertEqual(metadata['selection'], {'start_frame': 3, 'end_frame': 10})
        self.assertLess(patched['native_transl'][6, 0], original['native_transl'][6, 0])
        for key in ('native_keypoints3d', 'native_transl', 'smpl_poses'):
            self.assertEqual(patched[key][:3].tobytes(), original[key][:3].tobytes())
            self.assertEqual(patched[key][10:].tobytes(), original[key][10:].tobytes())
        task = service.advance(self.user, task['id'])
        self.assertEqual(task['status'], 'awaiting_review')
        task = service.accept(self.user, task['id'], {'candidate_id': candidate['id']})
        self.assertTrue(next(c for c in service.results(self.user)['items'] if c['motion_id']==candidate['motion_id'])['accepted'])
        self.assertEqual(service.results(self.other)['items'], [])
        self.assertEqual(task['status'], 'accepted')
        unchanged, _ = service.assets.load('alice', self.source['id'])
        for key in original:
            self.assertEqual(unchanged[key].tobytes(), original[key].tobytes())
        with self.assertRaises(KeyError):
            service.detail(self.other, task['id'])

    def test_import_full_candidate_and_retry_are_idempotent(self):
        task = self.create()
        arrays, _ = self.service.assets.load('alice', self.source['id'])
        arrays['native_keypoints3d'][3:10, 20, 0] += 0.1
        value = {'request_id': 'candidate-upload', 'npz_base64': encoded(arrays)}
        first = self.service.import_patch(self.user, task['id'], value)
        second = self.service.import_patch(self.user, task['id'], value)
        self.assertEqual(len(first['candidates']), 1)
        self.assertEqual(first['candidates'], second['candidates'])
        patched, metadata = self.service.assets.load('alice', first['candidates'][0]['motion_id'])
        self.assertEqual(metadata['selection'], {'start_frame': 3, 'end_frame': 10})
        self.assertEqual(patched['native_keypoints3d'][2, 20, 0], 0)
        self.assertAlmostEqual(float(patched['native_keypoints3d'][3, 20, 0]), 0.1)
        self.assertEqual(patched['native_keypoints3d'][10, 20, 0], 0)
        arrays['native_keypoints3d'][5, 20, 0] = 0.2
        with self.assertRaises(ValueError):
            self.service.import_patch(self.user, task['id'], {'request_id': value['request_id'], 'npz_base64': encoded(arrays)})

    def test_import_segment_candidate_uses_half_open_selection(self):
        task = self.create()
        arrays, _ = self.service.assets.load('alice', self.source['id'])
        segment = {key: value.copy() if key == 'fps' else value[3:10].copy() for key, value in arrays.items()}
        segment['native_keypoints3d'][:, 20, 0] += 0.2
        result = self.service.import_patch(self.user, task['id'], {'request_id': 'segment-upload', 'npz_base64': encoded(segment)})
        patched, _ = self.service.assets.load('alice', result['candidates'][0]['motion_id'])
        self.assertEqual(patched['native_keypoints3d'].shape[0], 12)
        self.assertEqual(patched['native_keypoints3d'][10, 20, 0], 0)
        self.assertAlmostEqual(float(patched['native_keypoints3d'][9, 20, 0]), 0.2)

    def test_import_rejects_changes_outside_selection_and_other_owner(self):
        task = self.create()
        arrays, _ = self.service.assets.load('alice', self.source['id'])
        arrays['native_keypoints3d'][0, 20, 0] += 0.1
        value = {'request_id': 'outside-upload', 'npz_base64': encoded(arrays)}
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.service.import_patch(self.user, task['id'], value)
        self.assertEqual(self.service.detail(self.user, task['id'])['candidates'], [])
        with self.assertRaises(KeyError):
            self.service.import_patch(self.other, task['id'], value)

    def test_disabled_kimodo_editor_and_unknown_tool_never_call_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            caps = self.service.capabilities(self.user)
            models = {x['id']: x for x in caps['backends']}
            self.assertFalse(models['kimodo']['available'])
            self.assertFalse(models['local_editor']['available'])
            self.assertFalse(caps['driver']['available'])
            task = self.create()
            with mock.patch('urllib.request.build_opener', side_effect=AssertionError('Network must not run')):
                with self.assertRaises(HarnessError):
                    self.service.advance(self.user, task['id'])
                unknown = self.service.execute(self.user, task['id'], {'kind': 'tool', 'tool': 'kimodo', 'arguments': {}}, 'unknown-decision')
                self.assertEqual(unknown['status'], 'needs_attention')
                edit = self.service.execute(self.user, task['id'], {'kind': 'tool', 'tool': 'edit_motion', 'arguments': {'prompt':'A person walks forward.'}}, 'edit-decision')
                self.assertEqual(edit['operations'][-1]['status'], 'failed')
                self.assertEqual(edit['operations'][-1]['result']['code'], 'editor_unavailable')

    def test_legacy_source_reads_through_authorized_library_bridge(self):
        service = HarnessService(self.root, legacy_store=object())
        buffer = io.BytesIO(); np.savez_compressed(buffer, **motion(aliases=False))
        with mock.patch('gadget_library.source_npz', return_value=buffer.getvalue()) as reader:
            task = service.create(self.user, {'request_id': 'legacy', 'goal': 'Inspect existing motion',
                                             'source_motion_id': 'motion123'})
            reader.assert_called_once_with(service.legacy, 'motion123', self.user)
            self.assertTrue(task['source_motion_id'].startswith('ma_'))
        from review_access import Forbidden
        with mock.patch('gadget_library.source_npz', side_effect=Forbidden('not allowed')):
            with self.assertRaises(Forbidden):
                service.create(self.other, {'request_id': 'denied', 'goal': 'Inspect', 'source_motion_id': 'motion123'})

    def test_preview_uses_existing_root_relative_coordinate_convention(self):
        rig = self.root / 'third_party/HY-Motion-1.0/retargeting/smplh'
        rig.mkdir(parents=True)
        np.asarray([-1] + [0] * 51, dtype=np.int32).tofile(rig / 'kintree.bin')
        template = np.zeros((52, 3), dtype=np.float32); template[0, 1] = 1
        template.tofile(rig / 'j_template.bin')
        (rig / 'joint_names.json').write_text(json.dumps(['joint' + str(i) for i in range(52)]))
        arrays = motion(aliases=False)
        arrays['native_keypoints3d'][:, :, 0] += np.arange(12)[:, None] * 4
        source = self.service.assets.register('alice', arrays)
        with mock.patch.dict(os.environ, {'MOTIONFARM_PREVIEW_PACKER': ''}):
            preview = self.service.preview(self.user, source['id'])
        world = np.asarray(preview['joints'])
        np.testing.assert_allclose(world[:, 0], arrays['native_transl'] + template[0], atol=1e-6)
        self.assertEqual(preview['quality']['semantic'], 'unknown')


if __name__ == '__main__':
    unittest.main()
