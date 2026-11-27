"""Deterministic executor tests; no network, paid providers, or GPU work."""
import unittest
import time
from unittest import mock

from scripts.harness_worker import Runner, WorkerDeadline, _bounds, hard_deadline


class Clock:
    def __init__(self):
        self.now = 0.
        self.sleeps = []
    def __call__(self): return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Service:
    def __init__(self, clock, states, initial='ready', hook=None):
        self.clock = clock
        self.states = iter(states)
        self.task = {'id': 'task1', 'status': initial, 'operations': []}
        self.calls = []
        self.details = []
        self.hook = hook
    def detail(self, user, task_id):
        self.details.append((user.copy(), task_id))
        return dict(self.task)
    def advance(self, user, task_id):
        self.calls.append((self.clock(), user.copy(), task_id))
        if self.hook:
            self.hook()
        self.task['status'] = next(self.states)
        return dict(self.task)


class WorkerTests(unittest.TestCase):
    def runner(self, service, clock, check=None):
        return Runner(service, check or (lambda owner: 'user'), clock=clock, sleep=clock.sleep)

    def test_ready_steps_continue_until_review_without_manual_clicks(self):
        clock = Clock(); service = Service(clock, ['ready', 'ready', 'awaiting_review'])
        checked = []
        result = self.runner(service, clock, lambda owner: checked.append(owner) or 'user').run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'awaiting_review')
        self.assertEqual(result['ticks'], 3)
        self.assertEqual(len(checked), 3)
        self.assertEqual(clock.sleeps, [])
        self.assertTrue(all(call[1] == {'username': 'alice', 'role': 'user'} for call in service.calls))

    def test_external_job_queries_are_at_least_ten_seconds_apart(self):
        clock = Clock(); service = Service(clock, ['waiting', 'waiting', 'ready', 'awaiting_review'])
        result = self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'awaiting_review')
        self.assertEqual([call[0] for call in service.calls], [0., 10., 20., 20.])
        self.assertEqual(clock.sleeps, [10., 10.])

    def test_resuming_waiting_job_waits_before_first_poll(self):
        clock = Clock(); service = Service(clock, ['awaiting_review'], initial='waiting')
        result = self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(result['ticks'], 1)
        self.assertEqual(service.calls[0][0], 10.)

    def test_resuming_crash_intent_also_waits_before_reconciliation(self):
        clock = Clock(); service = Service(clock, ['awaiting_review'])
        service.task['operations'] = [{'status': 'submitting'}]
        self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(service.calls[0][0], 10.)

    def test_each_specified_stop_state_prevents_further_decisions(self):
        for status in ('waiting_user', 'awaiting_review', 'accepted', 'cancelled', 'failed',
                       'needs_attention', 'budget_exhausted'):
            clock = Clock(); service = Service(clock, [], initial=status)
            result = self.runner(service, clock).run('alice', 'task1')
            self.assertEqual(result['stop_reason'], status)
            self.assertEqual(service.calls, [])

    def test_tick_budget_never_runs_extra_action(self):
        clock = Clock(); service = Service(clock, ['ready'] * 5)
        result = self.runner(service, clock).run('alice', 'task1', max_ticks=2)
        self.assertEqual(result['stop_reason'], 'max_ticks')
        self.assertEqual(result['ticks'], 2)
        self.assertEqual(len(service.calls), 2)

    def test_wall_clock_admission_bound_stops_ready_loop(self):
        clock = Clock()
        def slow_tick(): clock.now += 2
        service = Service(clock, ['ready'] * 10, hook=slow_tick)
        result = self.runner(service, clock).run('alice', 'task1', max_seconds=5)
        self.assertEqual(result['stop_reason'], 'max_seconds')
        self.assertEqual(len(service.calls), 3)

    def test_remaining_budget_never_shortens_poll_interval(self):
        clock = Clock(); service = Service(clock, ['waiting'] * 10)
        result = self.runner(service, clock).run('alice', 'task1', max_seconds=5)
        self.assertEqual(result['stop_reason'], 'max_seconds')
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_revocation_is_checked_before_every_tick(self):
        clock = Clock(); service = Service(clock, ['ready'] * 10)
        checked = iter(['user', 'user', None])
        result = self.runner(service, clock, lambda owner: next(checked)).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'account_revoked')
        self.assertEqual(len(service.calls), 2)

    def test_unavailable_authority_fails_closed_without_reading_task(self):
        clock = Clock(); service = Service(clock, [])
        def unavailable(owner): raise OSError('No account database')
        result = self.runner(service, clock, unavailable).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'authorization_unavailable')
        self.assertEqual(service.details, [])
        self.assertEqual(service.calls, [])

    def test_failed_action_stops_without_hidden_retry(self):
        clock = Clock()
        def fail(): raise TimeoutError('Committed response may have been lost')
        service = Service(clock, ['ready'], hook=fail)
        result = self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'execution_error')
        self.assertEqual(result['ticks'], 1)
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(result['error'], 'TimeoutError')

    def test_deadline_interrupt_stops_without_claiming_operation_failure(self):
        clock = Clock()
        def expire(): raise WorkerDeadline()
        service = Service(clock, ['ready'], hook=expire)
        result = self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'max_seconds')
        self.assertEqual(result['ticks'], 1)
        self.assertIn('same task_id', result['resume'])

    def test_unknown_task_state_fails_closed(self):
        clock = Clock(); service = Service(clock, [], initial='mystery')
        result = self.runner(service, clock).run('alice', 'task1')
        self.assertEqual(result['stop_reason'], 'unsupported_task_status')
        self.assertEqual(service.calls, [])

    def test_bounds_reject_unlimited_or_aggressive_polling(self):
        for args in ((0, 300, 10), (1001, 300, 10), (24, 0, 10), (24, 3601, 10),
                     (24, 300, 9), (True, 300, 10), (24, float('nan'), 10)):
            with self.assertRaises(ValueError):
                _bounds(*args)

    def test_posix_deadline_restores_handler_and_does_not_replace_existing_alarm(self):
        with mock.patch('scripts.harness_worker.signal.getitimer', return_value=(0., 0.)), \
             mock.patch('scripts.harness_worker.signal.getsignal', return_value='original'), \
             mock.patch('scripts.harness_worker.signal.signal') as handler, \
             mock.patch('scripts.harness_worker.signal.setitimer') as timer:
            with hard_deadline(10):
                pass
            self.assertEqual(timer.call_args_list[-1].args[1], 0)
            self.assertEqual(handler.call_args_list[-1].args[1], 'original')
        with mock.patch('scripts.harness_worker.signal.getitimer', return_value=(2., 0.)):
            with self.assertRaises(RuntimeError):
                with hard_deadline(10):
                    pass

    def test_real_deadline_interrupts_a_blocking_action(self):
        completed = []
        def blocking():
            time.sleep(1)
            completed.append(True)
        service = Service(time.monotonic, ['ready'], hook=blocking)
        with hard_deadline(0.01):
            result = Runner(service, lambda owner: 'user').run('alice', 'task1', max_seconds=0.01)
        self.assertEqual(result['stop_reason'], 'max_seconds')
        self.assertEqual(completed, [])


if __name__ == '__main__':
    unittest.main()
