"""Recovery of a completed upload after the user accepts its result."""
import base64
import io
import tempfile
from pathlib import Path
import unittest

import numpy as np
from harness_service import HarnessService


class UploadReceipts(unittest.TestCase):
    def test_accepted_upload_replay_returns_receipt_without_new_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            service = HarnessService(Path(directory))
            user = {'username': 'alice', 'role': 'user'}
            source = dict(fps=np.asarray(30), native_keypoints3d=np.zeros((12, 52, 3), dtype=np.float32),
                          native_transl=np.zeros((12, 3), dtype=np.float32),
                          smpl_poses=np.zeros((12, 156), dtype=np.float32))
            original = service.assets.register('alice', source)
            task = service.create(user, {'request_id': 'accepted-upload', 'goal': '修改中间片段',
                'source_motion_id': original['motion_id'], 'selection': {'start_frame': 3, 'end_frame': 7},
                'budget': {'max_candidates': 1}})
            patch = {k: v[3:7].copy() if v.ndim else v.copy() for k, v in source.items()}
            def payload(arrays):
                buffer = io.BytesIO(); np.savez_compressed(buffer, **arrays)
                return {'request_id': 'same-upload-id', 'npz_base64': base64.b64encode(buffer.getvalue()).decode()}
            value = payload(patch)
            first = service.import_patch(user, task['id'], value)
            accepted = service.accept(user, task['id'], {'candidate_id': first['candidates'][0]['id']})
            before = service.assets.list('alice')
            replay = service.import_patch(user, task['id'], value)
            self.assertEqual(replay['status'], 'accepted')
            self.assertEqual(replay['current_motion_id'], accepted['current_motion_id'])
            self.assertEqual(service.assets.list('alice'), before)
            patch['native_transl'][1, 0] = .01
            with self.assertRaises(ValueError):
                service.import_patch(user, task['id'], payload(patch))
            self.assertEqual(service.assets.list('alice'), before)


if __name__ == '__main__':
    unittest.main()
