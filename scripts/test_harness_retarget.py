"""Retarget bridge tests: fake durable queues only, no solver or worker."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g2h.harness import ToolRegistry
from harness_service import HarnessService
from harness_retarget import register_retarget_tool, _import_id, _job_id
from test_motion_tools import motion


class FakeImports:
    records = {}
    calls = []
    def __init__(self, root):
        self.root = root
    def save(self, owner, key, text, raw):
        mid = 'mimp' + key
        digest = hashlib.sha256(raw).hexdigest()
        current = (owner, text, digest, raw)
        self.calls.append((owner, key, text, raw))
        if mid in self.records and self.records[mid] != current:
            raise ValueError('Import identity mismatch')
        self.records[mid] = current
        return {'motion_id': mid, 'sha256': digest}
    def get(self, mid, owner):
        if mid not in self.records or self.records[mid][0] != owner:
            raise PermissionError('Import is not owned')
        return {'motion_id': mid, 'owner': owner}


class FakeRetarget:
    rows = {}
    calls = []
    lookups = []
    cache = {}
    def __init__(self, root):
        self.root = root
    def submit(self, value, user, store):
        self.calls.append(copy.deepcopy(value))
        store.detail(value['motion_id'])
        key = value['id']
        if key in self.rows:
            return copy.deepcopy(self.rows[key])
        cached = None if value['force_new'] else self.cache.get((user['username'], value['motion_id'], value['robot']))
        self.rows[key] = {
            'id': key, 'owner': user['username'], 'motion_id': value['motion_id'],
            'robot': value['robot'], 'status': 'done' if cached else 'queued',
            'execution': json.dumps(cached['execution'] if cached else {'parameters': value.get('parameters', {})}),
            'requested_execution': json.dumps({'parameters': value.get('parameters', {})}),
            'reused_from': cached['id'] if cached else '',
            'result_version': {'sha256': 'known-result'} if cached else None,
        }
        return copy.deepcopy(self.rows[key])
    def get(self, key, user):
        self.lookups.append(key)
        if key not in self.rows:
            raise ValueError('Job not found')
        row = self.rows[key]
        if user['role'] != 'admin' and row['owner'] != user['username']:
            raise PermissionError('Job is not owned')
        return copy.deepcopy(row)


class HarnessRetargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = HarnessService(Path(self.temp.name), legacy_store=object())
        self.user = {'username': 'alice', 'role': 'user'}
        self.source = self.service.assets.register('alice', motion(aliases=False))
        self.args = {'motion_id': self.source['id'], 'robot': 'test_robot'}
        self.context = {'owner': 'alice', 'task': {'requirements': [
            {'text': 'Retarget for the selected robot', 'kind': 'hard',
             'check': {'type': 'retarget', 'robot': 'test_robot'}}]}}
        FakeImports.records = {}; FakeImports.calls = []
        FakeRetarget.rows = {}; FakeRetarget.calls = []; FakeRetarget.lookups = []; FakeRetarget.cache = {}
        self.patches = [
            mock.patch('gadget_imports.Imports', FakeImports),
            mock.patch('review_retarget.Retarget', FakeRetarget),
            mock.patch('retarget_robots.robot_spec', return_value={'name': 'test_robot'}),
        ]
        for patch in self.patches:
            patch.start(); self.addCleanup(patch.stop)
        registry = ToolRegistry()
        self.tool = register_retarget_tool(registry, self.service, self.user)
        self.assertIs(registry.get('retarget_motion'), self.tool)

    def test_disabled_does_not_import_or_submit(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = self.tool.execute(self.context, self.args, 'op_1')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['code'], 'retarget_disabled')
        self.assertEqual(FakeImports.calls, [])
        self.assertEqual(FakeRetarget.calls, [])

    def test_quarantined_source_is_rejected_before_import_or_submit(self):
        task = self.service.store.create_task('alice', 'isolated-source', 'Review a failed motion')
        self.service.store.register_candidate('alice', task['id'], self.source['id'], {'quarantined': True})
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            result = self.tool.execute(self.context, self.args, 'op_1')
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['code'], 'retarget_preflight_rejected')
        self.assertIn('隔离候选', result['result']['message'])
        self.assertEqual(FakeImports.calls, [])
        self.assertEqual(FakeRetarget.calls, [])

    def test_typed_robot_grant_required_and_no_text_fallback(self):
        for task in ({'goal': 'Please retarget to test_robot'}, {'requirements': []},
                     {'requirements': [{'check': {'type': 'retarget', 'robot': 'other_robot'}}]}):
            with self.subTest(task=task):
                result = self.tool.execute({'owner': 'alice', 'task': task}, self.args, 'op_1')
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['result']['code'], 'retarget_preflight_rejected')
        self.assertEqual(FakeRetarget.calls, [])

    def test_stable_import_bytes_and_job_ids_on_repeated_operation(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            first = self.tool.execute(self.context, self.args, 'op_1')
            second = self.tool.execute(self.context, self.args, 'op_1')
        self.assertEqual(first['handle'], second['handle'])
        self.assertEqual(first['state'], 'waiting')
        self.assertEqual(len(FakeRetarget.rows), 1)
        self.assertEqual(FakeImports.calls[0][3], FakeImports.calls[1][3])
        self.assertEqual(FakeRetarget.calls[0]['id'], _job_id('alice', 'op_1'))
        self.assertEqual(FakeRetarget.calls[0]['motion_id'], 'mimp' + _import_id('alice', self.source['sha256']))
        self.assertIs(FakeRetarget.calls[0]['force_new'], False)

    def test_cross_operation_reuses_identical_import_and_original_cached_parameters(self):
        import_mid = 'mimp' + _import_id('alice', self.source['sha256'])
        FakeRetarget.cache[('alice', import_mid, 'test_robot')] = {
            'id': 'cached-original', 'execution': {'parameters': {'smooth_weight': 0.2}}}
        args = dict(self.args, parameters={'smooth_weight': 5.})
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            first = self.tool.execute(self.context, args, 'op_1')
            second = self.tool.execute(self.context, args, 'op_2')
        self.assertEqual(FakeRetarget.calls[0]['motion_id'], FakeRetarget.calls[1]['motion_id'])
        self.assertNotEqual(FakeRetarget.calls[0]['id'], FakeRetarget.calls[1]['id'])
        self.assertEqual(first['state'], 'succeeded')
        self.assertEqual(second['result']['execution']['parameters']['smooth_weight'], 0.2)
        self.assertEqual(second['result']['requested_execution']['parameters']['smooth_weight'], 5.)
        self.assertEqual(second['result']['reused_from'], 'cached-original')
        self.assertNotIn('candidates', second)
        self.assertEqual(second['result']['artifacts'][0]['kind'], 'retarget')
        self.assertEqual(second['evidence'][0]['status'], 'unknown')

    def test_force_new_needs_its_own_user_grant(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            refusal = self.tool.execute(self.context, dict(self.args, force_new=True), 'op_1')
            self.assertEqual(refusal['state'], 'failed')
            self.assertEqual(refusal['result']['code'], 'retarget_preflight_rejected')
            self.context['task']['requirements'][0]['check']['force_new'] = True
            result = self.tool.execute(self.context, dict(self.args, force_new=True), 'op_1')
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(FakeRetarget.calls[0]['force_new'])

    def test_reconcile_after_disable_does_not_reimport_or_resubmit(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            result = self.tool.execute(self.context, self.args, 'op_1')
        key = result['handle']['job_id']
        FakeRetarget.rows[key].update(status='done', result_version={'sha256': 'actual'})
        submitted, imports = len(FakeRetarget.calls), len(FakeImports.calls)
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '0'}):
            done = self.tool.reconcile(self.context, self.args, 'op_1', result['handle'])
        self.assertEqual(done['state'], 'succeeded')
        self.assertEqual(len(FakeRetarget.calls), submitted)
        self.assertEqual(len(FakeImports.calls), imports)

    def test_unknown_submission_never_replays(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '0'}):
            result = self.tool.reconcile(self.context, self.args, 'never-submitted', None)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(FakeRetarget.calls, [])
        self.assertEqual(FakeImports.calls, [])

    def test_owner_isolation_including_admin_job_read(self):
        refusal = self.tool.execute(dict(self.context, owner='bob'), self.args, 'op_1')
        self.assertEqual(refusal['state'], 'failed')
        self.assertEqual(refusal['result']['code'], 'retarget_preflight_rejected')
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            result = self.tool.execute(self.context, self.args, 'op_1')
        self.user['role'] = 'admin'
        FakeRetarget.rows[result['handle']['job_id']]['owner'] = 'bob'
        with self.assertRaises(PermissionError):
            self.tool.reconcile(self.context, self.args, 'op_1', result['handle'])
        self.assertEqual(len(FakeRetarget.calls), 1)

    def test_source_or_export_preflight_failure_is_definitive(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            for lookup_error in (KeyError('Motion not found'), PermissionError('Source denied')):
                with mock.patch.object(self.service, '_source', side_effect=lookup_error):
                    result = self.tool.execute(self.context, self.args, 'op_1')
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['result']['code'], 'retarget_preflight_rejected')
            with mock.patch('harness_retarget._numeric_export', side_effect=ValueError('Unsupported source format')):
                result = self.tool.execute(self.context, self.args, 'op_1')
            self.assertEqual(result['state'], 'failed')
        self.assertEqual(FakeImports.calls, [])
        self.assertEqual(FakeRetarget.calls, [])

    def test_uncertainty_after_import_or_submission_is_not_swallowed(self):
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            with mock.patch.object(FakeImports, 'save', side_effect=OSError('Import acknowledgement lost')):
                with self.assertRaises(OSError):
                    self.tool.execute(self.context, self.args, 'op_import')
            with mock.patch.object(FakeRetarget, 'submit', side_effect=TimeoutError('Job acknowledgement lost')):
                with self.assertRaises(TimeoutError):
                    self.tool.execute(self.context, self.args, 'op_job')
        self.assertEqual(len(FakeImports.records), 1)

    def test_reconcile_grant_rejection_still_requires_manual_resolution(self):
        with self.assertRaises(PermissionError):
            self.tool.reconcile({'owner': 'alice', 'task': {'requirements': []}}, self.args, 'op_1', None)
        self.assertEqual(FakeRetarget.calls, [])

    def test_wrong_handle_and_invalid_parameters_fail_before_effect(self):
        with self.assertRaises(PermissionError):
            self.tool.reconcile(self.context, self.args, 'op_1', {'job_id': 'another'})
        with mock.patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_RETARGET': '1'}):
            refusal = self.tool.execute(self.context, dict(self.args, parameters={'unsafe_collision_bypass': 1}), 'op_1')
            self.assertEqual(refusal['state'], 'failed')
            self.assertEqual(refusal['result']['code'], 'retarget_preflight_rejected')
        self.assertEqual(FakeImports.calls, [])
        self.assertEqual(FakeRetarget.calls, [])


if __name__ == '__main__':
    unittest.main()
