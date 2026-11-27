"""Read-only review probes for recovery edge cases.

These use the real ledger and service logic with stubbed numeric assets and no
GPU work, credentials or external network calls.
"""
import base64
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

from g2h.harness import Tool, ToolRegistry
from harness_service import HarnessService

SCRIPTS = Path(__file__).resolve().parent


class Assets:
    def __init__(self, root):
        self.root = root
        self.requests = {}
    def get(self, owner, motion_id):
        return {'motion_id': motion_id, 'sha256': 'source-sha'}
    def load(self, owner, motion_id):
        return {}, self.get(owner, motion_id)
    def import_npz(self, owner, path, **kwargs):
        metadata = self.get(owner, 'ma_patch')
        self.requests[owner, kwargs['request_id']] = metadata
        return metadata
    def find_request(self, owner, request_id):
        return self.requests.get((owner, request_id))
    def apply_patch(self, owner, source_id, start, end, arrays, **kwargs):
        return {'motion_id': 'ma_result', 'parent_id': source_id, 'selection': {'start_frame': start, 'end_frame': end}}


class HarnessServiceEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = {'username': 'alice'}
        self.service = HarnessService(self.root, assets=Assets(self.root))

    def create(self, budget=None):
        return self.service.store.create_task('alice', 'source-task', 'Smooth root jitter',
            source_motion_id='ma_source', selection={'start_frame': 2, 'end_frame': 5}, budget=budget)

    def test_import_retry_at_candidate_limit_should_return_existing_candidate(self):
        task = self.create({'max_candidates': 1})
        value = {'request_id': 'stable-upload', 'npz_base64': base64.b64encode(b'fake-numeric-npz').decode()}
        first = self.service.import_patch(self.user, task['id'], value)
        second = self.service.import_patch(self.user, task['id'], value)
        self.assertEqual(first['candidates'], second['candidates'])

    def test_new_action_is_not_recorded_when_tick_only_reconciles_previous_job(self):
        task = self.create()
        registry = ToolRegistry()
        inspections = []
        registry.register(Tool('slow', 'Long operation', {'type': 'object', 'properties': {}},
            lambda *a: {'state': 'waiting', 'handle': {'job': 'one'}},
            reconcile=lambda *a: {'state': 'succeeded'}))
        registry.register(Tool('inspect', 'Observe', {'type': 'object', 'properties': {}},
            lambda *a: inspections.append(1) or {'state': 'succeeded'}, side_effect=False))
        with mock.patch.object(self.service, '_registry', return_value=registry):
            self.service.execute(self.user, task['id'], {'kind': 'tool', 'tool': 'slow', 'arguments': {}}, 'decision-one')
            action = {'kind': 'tool', 'tool': 'inspect', 'arguments': {}}
            result = self.service.execute(self.user, task['id'], action, 'decision-two')
            self.assertEqual(inspections, [])
            self.assertEqual(result['decision_receipt']['state'], 'deferred')
            self.assertFalse(result['decision_receipt']['action_applied'])
            result = self.service.execute(self.user, task['id'], action, 'decision-two')
            self.assertEqual(inspections, [1])
            self.assertEqual(result['decision_receipt']['state'], 'recorded')
            self.assertTrue(result['decision_receipt']['action_applied'])
            result = self.service.execute(self.user, task['id'], action, 'decision-two')
            self.assertEqual(inspections, [1])

    def test_disabling_new_generation_still_allows_existing_job_reconciliation(self):
        task = self.create()
        service = HarnessService(self.root, legacy_store=object(), assets=Assets(self.root))
        class Custom:
            def __init__(self, root):
                pass
            def db(self):
                db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
                db.execute('CREATE TABLE requests(id TEXT,owner TEXT,status TEXT,message TEXT)')
                db.execute('INSERT INTO requests VALUES(?,?,?,?)', ('hgen_op_existing', 'alice', 'failed', 'Finished with no usable result'))
                return db
        with mock.patch.dict(sys.modules, {'review_custom': types.SimpleNamespace(Custom=Custom)}), mock.patch.dict(
                'os.environ', {'MOTIONFARM_HARNESS_ENABLE_GENERATION': '0'}, clear=True):
            result = service._registry(self.user).get('generate_motion').reconcile(
                {'owner': 'alice', 'task': task}, {}, 'op_existing', {'request_id': 'hgen_op_existing'})
        self.assertEqual(result['state'], 'failed')

    def test_feedback_does_not_duplicate_after_lost_http_response(self):
        task = self.create()
        gadget = SCRIPTS.parent / 'gadget'
        spec = importlib.util.spec_from_file_location('_motionfarm_client_review_edges', gadget / 'motionfarm.py')
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(gadget))
        self.addCleanup(lambda: sys.path.remove(str(gadget)))
        spec.loader.exec_module(module)
        mcp_spec = importlib.util.spec_from_file_location('_motionfarm_mcp_review_edges', gadget / 'motionfarm_mcp.py')
        mcp = importlib.util.module_from_spec(mcp_spec)
        with mock.patch.dict(sys.modules, {'motionfarm': module}):
            mcp_spec.loader.exec_module(mcp)
        service = self.service
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, limit): return b'{}'
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                value = json.loads(request.data)
                service.feedback({'username': 'alice'}, value['task_id'], {'message': value['message']})
                if self.calls == 1:
                    raise TimeoutError('Committed response was lost')
                return Response()
        client = module.Client.__new__(module.Client)
        client.url = 'https://example.invalid'; client.key = 'test-only'; client.opener = Opener()
        with mock.patch.object(module.time, 'sleep'), self.assertRaises(TimeoutError):
            mcp.invoke('feedback_motion_task', {'task_id': task['id'], 'message': 'Move slower'}, client=client)
        result = self.service.detail(self.user, task['id'])
        self.assertEqual([r['text'] for r in result['requirements']], ['Move slower'])


if __name__ == '__main__':
    unittest.main()
