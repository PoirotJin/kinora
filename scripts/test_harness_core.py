"""Bounded CPU tests for task ownership, durable effects and human acceptance."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from g2h.harness import (BudgetExceeded, Conflict, Harness, HarnessError, LeaseLost,
                         OpenAICompatibleDriver, Store, Tool, ToolRegistry)


class Driver:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = 0

    def next_action(self, context, tools):
        self.calls += 1
        if not self.actions:
            raise AssertionError('Unexpected model call')
        return self.actions.pop(0)


class SimulatedCrash(BaseException):
    pass


EMPTY = {'type': 'object', 'properties': {}, 'additionalProperties': False}


class HarnessCoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(self.folder.name)
        self.registry = ToolRegistry()
        self.task = self.store.create_task('alice', 'request-1', 'Walk then stop', ['Keep forward direction'])

    def tool(self, execute, reconcile=None, **extra):
        self.registry.register(Tool('generate', 'Create a motion', EMPTY, execute, reconcile, **extra))

    def advance(self, driver, task=None):
        return Harness(self.store, self.registry, driver).advance('alice', (task or self.task)['id'])

    def test_same_request_is_idempotent_but_changed_arguments_are_rejected(self):
        same = self.store.create_task('alice', 'request-1', 'Walk then stop', ['Keep forward direction'])
        self.assertEqual(same['id'], self.task['id'])
        with self.assertRaises(Conflict):
            self.store.create_task('alice', 'request-1', 'Run')
        other = self.store.create_task('bob', 'request-1', 'Run')
        self.assertNotEqual(other['id'], self.task['id'])

    def test_all_owner_mutations_and_reads_hide_foreign_tasks(self):
        attempts = [lambda: self.store.get_task('bob', self.task['id']),
                    lambda: self.store.add_feedback('bob', self.task['id'], 'Change'),
                    lambda: self.store.cancel_task('bob', self.task['id']),
                    lambda: self.store.register_candidate('bob', self.task['id'], 'm1'),
                    lambda: self.store.accept_candidate('bob', self.task['id'], 'candidate_missing'),
                    lambda: self.store.claim('bob', self.task['id'], 'worker')]
        for attempt in attempts:
            with self.assertRaises(KeyError):
                attempt()
        self.assertEqual(self.store.list_tasks('bob'), [])

    def test_selection_requires_existing_source_reference_and_half_open_frames(self):
        with self.assertRaises(HarnessError):
            self.store.create_task('alice', 'invalid', 'Edit', selection={'start_frame': 10, 'end_frame': 20})
        with self.assertRaises(HarnessError):
            self.store.create_task('alice', 'invalid', 'Edit', source_motion_id='m1', selection={'start_frame': 10, 'end_frame': 10})
        task = self.store.create_task('alice', 'valid', 'Edit', source_motion_id='m1', selection={'start_frame': 10, 'end_frame': 20})
        self.assertEqual(task['selection'], {'start_frame': 10, 'end_frame': 20})

    def test_finish_never_accepts_a_candidate(self):
        candidate = self.store.register_candidate('alice', self.task['id'], 'm1')
        task = self.advance(Driver({'kind': 'finish', 'message': 'Ready to review'}))
        self.assertEqual(task['status'], 'awaiting_review')
        self.assertEqual(task['candidates'][0]['status'], 'pending_review')
        accepted = self.store.accept_candidate('alice', self.task['id'], candidate['id'])
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['events'][-1]['kind'], 'candidate_accepted_by_user')

    def test_quarantined_candidates_are_preserved_but_not_accepted(self):
        candidate = self.store.register_candidate('alice', self.task['id'], 'm1', {'quarantined': True})
        with self.assertRaises(Conflict):
            self.store.accept_candidate('alice', self.task['id'], candidate['id'])
        self.assertEqual(len(self.store.get_task('alice', self.task['id'])['candidates']), 1)

    def test_feedback_appends_requirements_instead_of_forgetting_accepted_ones(self):
        self.advance(Driver({'kind': 'wait', 'message': 'Which interval?'}))
        task = self.store.add_feedback('alice', self.task['id'], 'Slow down', [{'text': 'Slow down', 'scope': 'selection'}])
        self.assertEqual(task['status'], 'ready')
        self.assertEqual(task['revision'], 2)
        self.assertEqual([r['text'] for r in task['requirements']], ['Keep forward direction', 'Slow down'])

    def test_long_job_is_reconciled_after_restart_without_resubmission(self):
        submitted, reconciled = [], []
        def submit(ctx, args, op):
            submitted.append(op)
            return {'state': 'waiting', 'handle': {'job_id': op}}
        def reconcile(ctx, args, op, handle):
            reconciled.append(handle['job_id'])
            return {'state': 'succeeded', 'candidates': [{'motion_id': 'm1', 'parent_id': 'source1'}],
                    'evidence': [{'kind': 'semantic', 'status': 'unknown', 'body': {'reason': 'Human review required'}}]}
        self.tool(submit, reconcile)
        first = self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        self.assertEqual(first['status'], 'waiting')
        driver = Driver()
        fresh = Harness(Store(self.folder.name), self.registry, driver)
        second = fresh.advance('alice', self.task['id'])
        self.assertEqual(len(submitted), 1)
        self.assertEqual(reconciled, submitted)
        self.assertEqual(driver.calls, 0)
        self.assertEqual(second['candidates'][0]['status'], 'pending_review')
        self.assertEqual(second['evidence'][0]['status'], 'unknown')

    def test_crash_after_external_acceptance_keeps_outbox_and_reconciles(self):
        calls = []
        def crash(ctx, args, op):
            calls.append(op)
            persisted = Store(self.folder.name).get_task('alice', self.task['id'])
            self.assertEqual(persisted['operations'][0]['status'], 'submitting')
            raise SimulatedCrash()
        def reconcile(ctx, args, op, handle):
            self.assertEqual(op, calls[0])
            self.assertIsNone(handle)
            return {'state': 'succeeded', 'result': {'job_id': op}}
        self.tool(crash, reconcile)
        with self.assertRaises(SimulatedCrash):
            self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        task = self.advance(Driver())
        self.assertEqual(len(calls), 1)
        self.assertEqual(task['operations'][0]['status'], 'succeeded')

    def test_unknown_side_effect_without_reconciler_is_never_replayed(self):
        calls = []
        def submit(ctx, args, op):
            calls.append(op)
            raise TimeoutError('External receipt lost')
        self.tool(submit)
        task = self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        self.assertEqual(task['status'], 'needs_attention')
        for _ in range(2):
            task = self.advance(Driver())
        self.assertEqual(len(calls), 1)
        self.assertEqual(task['operations'][0]['status'], 'unknown')

    def test_failed_attempts_consume_budget_and_do_not_execute_again(self):
        self.tool(lambda *a: {'state': 'failed', 'result': {'error': 'Bad candidate'}})
        task = self.store.create_task('alice', 'budget', 'Walk', budget={'max_attempts': 1})
        action = {'kind': 'tool', 'tool': 'generate', 'arguments': {}}
        self.advance(Driver(action), task)
        result = self.advance(Driver(action), task)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(len(result['operations']), 1)

    def test_cost_is_reserved_before_external_call(self):
        calls = []
        self.tool(lambda *a: calls.append(1), max_cost_usd=0.1)
        task = self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        self.assertEqual(task['status'], 'budget_exhausted')
        self.assertEqual(calls, [])

    def test_candidate_overflow_preserves_failure_evidence(self):
        self.tool(lambda *a: {'state': 'succeeded', 'candidates': [{'motion_id': 'm1'}, {'motion_id': 'm2'}]})
        task = self.store.create_task('alice', 'candidate-budget', 'Walk', budget={'max_candidates': 1})
        result = self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}), task)
        self.assertEqual(result['operations'][0]['status'], 'failed')
        self.assertIn('uncommitted_result', result['operations'][0]['result'])
        self.assertEqual(result['candidates'], [])

    def test_invalid_model_arguments_do_not_reach_tools(self):
        calls = []
        self.tool(lambda *a: calls.append(1))
        task = self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {'extra': True}}))
        self.assertEqual(task['status'], 'needs_attention')
        self.assertEqual(calls, [])
        self.assertEqual(task['operations'], [])

    def test_lease_excludes_another_writer_and_fences_expired_owner(self):
        lease = self.store.claim('alice', self.task['id'], 'worker1')
        with self.assertRaises(Conflict):
            self.store.claim('alice', self.task['id'], 'worker2')
        with self.store._db(True) as db:
            db.execute('UPDATE runs SET lease_until=0 WHERE id=?', (lease['run_id'],))
        new = self.store.claim('alice', self.task['id'], 'worker2')
        with self.assertRaises(LeaseLost):
            self.store.reserve_step(lease)
        self.store.release(lease)
        self.store.reserve_step(new)
        self.store.release(new)

    def test_cancel_fences_live_work_and_does_not_erase_operations(self):
        self.tool(lambda *a: {'state': 'waiting', 'handle': {'job_id': 'job1'}})
        self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        lease = self.store.claim('alice', self.task['id'], 'worker', decision_owner='Driver')
        task = self.store.cancel_task('alice', self.task['id'])
        with self.assertRaises(LeaseLost):
            self.store.reserve_step(lease)
        self.assertEqual(task['operations'][0]['handle'], {'job_id': 'job1'})
        self.assertEqual(self.advance(Driver())['status'], 'cancelled')

    def test_driver_configuration_is_explicit_and_remote_https_only(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(HarnessError):
                OpenAICompatibleDriver.from_env()
        with self.assertRaises(HarnessError):
            OpenAICompatibleDriver('http://example.com/v1', 'model', 'secret')
        OpenAICompatibleDriver('http://127.0.0.1:8080/v1', 'model', 'secret')

    def test_external_and_server_drivers_cannot_alternate_ownership(self):
        first = Harness(self.store, self.registry, Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}), driver_id='external-local')
        self.tool(lambda *a: {'state': 'succeeded'})
        first.advance('alice', self.task['id'])
        with self.assertRaises(Conflict):
            Harness(self.store, self.registry, Driver(), driver_id='server-agent').advance('alice', self.task['id'])

    def test_feedback_fences_a_driver_started_before_new_requirements(self):
        lease = self.store.claim('alice', self.task['id'], 'old-driver')
        self.store.add_feedback('alice', self.task['id'], 'Also stop smoothly')
        with self.assertRaises(LeaseLost):
            self.store.reserve_step(lease)

    def test_model_cost_reservation_survives_request_failure(self):
        class FailingDriver:
            cost_upper_bound_usd = 0.1
            def next_action(self, *a):
                raise TimeoutError('No response')
        task = self.store.create_task('alice', 'model-cost', 'Walk', budget={'max_cost_usd': 0.1})
        result = self.advance(FailingDriver(), task)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['reserved_cost'], 0.1)
        result = self.advance(FailingDriver(), task)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['steps'], 1)

    def test_remote_model_requires_explicit_cost_bound(self):
        with self.assertRaises(HarnessError):
            OpenAICompatibleDriver('https://example.com/v1', 'model', 'secret')
        driver = OpenAICompatibleDriver('https://example.com/v1', 'model', 'secret', cost_upper_bound_usd=0.02)
        self.assertEqual(driver.cost_upper_bound_usd, 0.02)

    def test_candidate_registration_is_idempotent_without_operation_id(self):
        first = self.store.register_candidate('alice', self.task['id'], 'm1', {'version': 'v1'})
        second = self.store.register_candidate('alice', self.task['id'], 'm1', {'version': 'v1'})
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(Conflict):
            self.store.register_candidate('alice', self.task['id'], 'm1', {'version': 'v2'})

    def test_unknown_evidence_cannot_be_converted_into_passed_by_acceptance(self):
        candidate = self.store.register_candidate('alice', self.task['id'], 'm1')
        self.store.add_evidence('alice', self.task['id'], 'semantics', 'unknown', {}, candidate['id'])
        task = self.store.accept_candidate('alice', self.task['id'], candidate['id'])
        self.assertEqual(task['evidence'][0]['status'], 'unknown')

    def test_manual_candidate_path_obeys_count_and_terminal_guards(self):
        task = self.store.create_task('alice', 'manual-budget', 'Edit', budget={'max_candidates': 1})
        self.store.register_candidate('alice', task['id'], 'm1')
        with self.assertRaises(BudgetExceeded):
            self.store.register_candidate('alice', task['id'], 'm2')
        self.store.cancel_task('alice', task['id'])
        with self.assertRaises(Conflict):
            self.store.register_candidate('alice', task['id'], 'm2')

    def test_feedback_during_tool_execution_leaves_intent_for_reconciliation(self):
        calls = []
        def submit(ctx, args, operation_id):
            calls.append(operation_id)
            self.store.add_feedback('alice', self.task['id'], 'Also keep direction')
            return {'state': 'waiting', 'handle': {'job_id': operation_id}}
        def reconcile(ctx, args, operation_id, handle):
            return {'state': 'succeeded', 'result': {'job_id': operation_id}}
        self.tool(submit, reconcile)
        with self.assertRaises(LeaseLost):
            self.advance(Driver({'kind': 'tool', 'tool': 'generate', 'arguments': {}}))
        current = self.store.get_task('alice', self.task['id'])
        self.assertEqual(current['operations'][0]['status'], 'submitting')
        self.assertEqual(current['revision'], 2)
        current = self.advance(Driver())
        self.assertEqual(current['operations'][0]['status'], 'succeeded')
        self.assertEqual(len(calls), 1)

    def test_accepted_version_becomes_next_edit_base_without_changing_original(self):
        task = self.store.create_task('alice', 'successive-edits', 'Edit the selected movement',
            source_motion_id='original', selection={'start_frame': 2, 'end_frame': 10},
            requirements=['Keep direction'])
        self.assertEqual(task['current_motion_id'], 'original')
        first = self.store.register_candidate('alice', task['id'], 'version1', parent_id='original')
        task = self.store.accept_candidate('alice', task['id'], first['id'])
        task = self.store.add_feedback('alice', task['id'], 'Also reduce arm swing')
        self.assertEqual(task['source_motion_id'], 'original')
        self.assertEqual(task['current_motion_id'], 'version1')
        self.assertEqual(task['status'], 'ready')
        self.assertEqual([x['text'] for x in task['requirements']], ['Keep direction', 'Also reduce arm swing'])
        observed = []
        self.registry.register(Tool('edit', 'Edit current version', EMPTY,
            lambda ctx, args, op: observed.append(ctx['task']['current_motion_id']) or {
                'state': 'succeeded', 'candidates': [{'motion_id': 'version2', 'parent_id': ctx['task']['current_motion_id']}]}))
        result = self.advance(Driver({'kind': 'tool', 'tool': 'edit', 'arguments': {}}), task)
        self.assertEqual(observed, ['version1'])
        second = next(c for c in result['candidates'] if c['motion_id'] == 'version2')
        self.assertEqual(second['parent_id'], 'version1')
        result = self.store.accept_candidate('alice', task['id'], second['id'])
        self.assertEqual(result['current_motion_id'], 'version2')
        self.assertEqual(result['source_motion_id'], 'original')

    def test_current_motion_migration_recovers_latest_accepted_candidate(self):
        candidate = self.store.register_candidate('alice', self.task['id'], 'latest-version')
        self.store.register_candidate('alice', self.task['id'], 'newer-unselected-version')
        self.store.accept_candidate('alice', self.task['id'], candidate['id'])
        with self.store._db(True) as db:
            # Recreate the schema shape from before current_motion_id was introduced.
            db.execute('ALTER TABLE tasks DROP COLUMN current_motion_id')
            db.execute('ALTER TABLE runs DROP COLUMN decision_owner')
            db.execute('PRAGMA user_version=0')
        workers = 6
        start = Barrier(workers)

        def read_current_motion(_):
            start.wait(timeout=5)
            reopened = Store(self.folder.name)
            return reopened.get_task('alice', self.task['id'])['current_motion_id']

        with ThreadPoolExecutor(max_workers=workers) as pool:
            self.assertEqual(list(pool.map(read_current_motion, range(workers))), ['latest-version'] * workers)

    def test_migration_resumes_if_an_older_startup_added_the_column_without_backfilling(self):
        edited = self.store.create_task('alice', 'resume-edit', 'Edit', source_motion_id='original')
        selected = self.store.register_candidate('alice', edited['id'], 'chosen-version')
        self.store.register_candidate('alice', edited['id'], 'unselected-version')
        self.store.accept_candidate('alice', edited['id'], selected['id'])
        source_only = self.store.create_task('alice', 'resume-source', 'Edit another', source_motion_id='source-only')
        with self.store._db(True) as db:
            db.execute('UPDATE tasks SET current_motion_id=NULL WHERE id IN (?,?)', (edited['id'], source_only['id']))
            db.execute('PRAGMA user_version=0')

        for _ in range(2):
            reopened = Store(self.folder.name)
            self.assertEqual(reopened.get_task('alice', edited['id'])['current_motion_id'], 'chosen-version')
            self.assertEqual(reopened.get_task('alice', source_only['id'])['current_motion_id'], 'source-only')
            self.assertIsNone(reopened.get_task('alice', self.task['id'])['current_motion_id'])

    def test_warm_ledger_can_reopen_while_a_task_has_a_write_transaction(self):
        with self.store._db(True):
            reopened = Store(self.folder.name)
            self.assertEqual(reopened.get_task('alice', self.task['id'])['id'], self.task['id'])


if __name__ == '__main__':
    unittest.main()
