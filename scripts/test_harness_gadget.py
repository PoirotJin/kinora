"""Real HTTP/Gadget/task integration with synthetic assets; no model or GPU."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error

import numpy as np

from test_gadget_v2 import GadgetTests
from gadget_keys import Keys
from harness_service import HarnessService


class HarnessGadgetTests(unittest.TestCase):
    setUp = GadgetTests.setUp
    tearDown = GadgetTests.tearDown
    call = GadgetTests.call

    def task(self):
        service = HarnessService(self.root, self.store)
        arrays = dict(fps=np.asarray(30), native_keypoints3d=np.zeros((12, 52, 3), dtype=np.float32),
                      native_transl=np.zeros((12, 3), dtype=np.float32),
                      smpl_poses=np.zeros((12, 156), dtype=np.float32))
        arrays['native_transl'][6, 0] = .05
        asset = service.assets.register('alice', arrays)
        value = dict(request_id='gadget-task-test', goal='平滑选区内的根轨迹抖动，保持其他帧',
                     source_motion_id=asset['motion_id'], selection=dict(start_frame=3, end_frame=9))
        return value, self.call('harness_create', value)

    def test_stable_task_and_step_ids_prevent_repeated_side_effects(self):
        value, task = self.task()
        self.assertEqual(self.call('harness_create', value)['id'], task['id'])
        decision = dict(task_id=task['id'], decision_id='decision-0001',
                        action=dict(kind='tool', tool='repair_root_jitter', arguments={}))
        first = self.call('harness_step', decision)
        self.assertEqual(len(first['operations']), 1)
        self.assertEqual(first['operations'][0]['status'], 'succeeded')
        self.assertEqual(len(first['candidates']), 1)
        again = self.call('harness_step', decision)
        self.assertEqual(len(again['operations']), 1)
        self.assertEqual(len(again['candidates']), 1)
        self.assertEqual(again['decision_receipt']['state'], 'recorded')
        changed = dict(decision, action=dict(kind='finish', message='done'))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call('harness_step', changed)
        self.assertEqual(error.exception.code, 400)

    def test_account_and_scope_boundaries(self):
        _, task = self.task()
        bob = Keys(self.root).create('bob', {})['key']
        with self.assertRaises(urllib.error.HTTPError):
            self.call('harness_task?id=' + task['id'], key=bob)
        read_only = Keys(self.root).create('alice', {'scopes': ['read']})['key']
        self.assertEqual(self.call('harness_task?id=' + task['id'], key=read_only)['id'], task['id'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call('harness_cancel', {'task_id': task['id']}, key=read_only)
        self.assertEqual(error.exception.code, 403)

    def test_capabilities_and_external_driver_do_not_require_server_model(self):
        caps = self.call('harness_capabilities')
        self.assertFalse(next(b for b in caps['backends'] if b['id'] == 'kimodo')['available'])
        _, task = self.task()
        result = self.call('harness_step', dict(task_id=task['id'], decision_id='inspect-0001',
                           action=dict(kind='tool', tool='inspect_motion', arguments={})))
        self.assertEqual(result['operations'][0]['status'], 'succeeded')
        self.assertTrue(result['evidence'])
        self.assertNotEqual(result['status'], 'accepted')
        page = self.call('harness_tasks')
        self.assertEqual(page['items'][0]['id'], task['id'])


class MCPTaskTools(unittest.TestCase):
    def test_local_agent_routes_preserve_decision_ids(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gadget'))
        import motionfarm_mcp as mcp
        class Client:
            def request(self, endpoint, value=None):
                return {'endpoint': endpoint, 'value': value}
        payload = {'task_id': 'task-test', 'decision_id': 'decision-stable',
                   'action': {'kind': 'tool', 'tool': 'inspect_motion', 'arguments': {}}}
        result = mcp.invoke('motion_task_step', payload, Client())
        self.assertEqual(result, {'endpoint': 'harness_step', 'value': payload})
        self.assertEqual(mcp.invoke('get_motion_task', {'task_id': 'task-test'}, Client())['endpoint'],
                         'harness_task?id=task-test')
        self.assertFalse(any(t['name'] == 'accept_motion_task' for t in mcp.TOOLS))


if __name__ == '__main__':
    unittest.main()
