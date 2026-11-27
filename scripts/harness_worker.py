#!/usr/bin/env python3
"""Advance one explicitly selected motion task within strict execution bounds.

This is an opt-in foreground executor, not a queue scanner or a daemon. It does
not create tasks, configure models, start GPU workers or replay uncertain work.
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))


STOP_STATES = {'waiting_user', 'awaiting_review', 'accepted', 'cancelled', 'failed',
               'needs_attention', 'budget_exhausted'}


class WorkerDeadline(BaseException):
    """Interrupt a blocking tick without disguising it as a retryable tool error."""


def _bounds(max_ticks, max_seconds, poll_seconds):
    if type(max_ticks) is not int or not 1 <= max_ticks <= 1000:
        raise ValueError('max_ticks must be an integer from 1 to 1000')
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 3600:
        raise ValueError('max_seconds must be greater than zero and at most 3600')
    if type(poll_seconds) not in (int, float) or not 10 <= poll_seconds <= 3600:
        raise ValueError('poll_seconds must be from 10 to 3600')


class Runner:
    def __init__(self, service, check_account, *, clock=time.monotonic, sleep=time.sleep):
        self.service = service
        self.check_account = check_account
        self.clock = clock
        self.sleep = sleep

    def run(self, owner, task_id, *, max_ticks=24, max_seconds=300, poll_seconds=10):
        _bounds(max_ticks, max_seconds, poll_seconds)
        if not isinstance(owner, str) or not owner or not isinstance(task_id, str) or not task_id:
            raise ValueError('Explicit owner and task_id are required')
        started = self.clock(); deadline = started + max_seconds
        ticks = 0; task = None; first_observation = True; next_tick = started

        def report(reason, error=None):
            value = {'owner': owner, 'task_id': task_id, 'ticks': ticks,
                     'elapsed_seconds': max(0., self.clock() - started),
                     'stop_reason': reason,
                     'status': task.get('status') if task else None,
                     'status_source': 'last_observed_snapshot'}
            if error:
                value['error'] = error
            if reason in {'max_ticks', 'max_seconds', 'interrupted'}:
                value['resume'] = 'Read the existing task and resume with the same task_id; submitted jobs are preserved.'
            return value

        try:
            while ticks < max_ticks:
                now = self.clock()
                if now >= deadline:
                    return report('max_seconds')
                if next_tick > now:
                    # Stop early if the remaining wall-clock budget cannot accommodate
                    # the minimum safe poll interval. Never shorten that interval.
                    if next_tick >= deadline:
                        return report('max_seconds')
                    self.sleep(next_tick - now)
                    if self.clock() < next_tick:
                        return report('interrupted')
                    if self.clock() >= deadline:
                        return report('max_seconds')
                try:
                    role = self.check_account(owner)
                except PermissionError:
                    return report('account_revoked')
                except Exception as exc:
                    return report('authorization_unavailable', type(exc).__name__)
                if role not in {'user', 'admin'}:
                    return report('account_revoked')
                user = {'username': owner, 'role': role}
                try:
                    task = self.service.detail(user, task_id)
                except Exception as exc:
                    return report('task_unavailable', type(exc).__name__)
                status = task.get('status')
                if status in STOP_STATES:
                    return report(status)
                if status not in {'ready', 'waiting'}:
                    return report('unsupported_task_status')
                pending = any(op.get('status') in {'submitting', 'waiting', 'unknown'}
                              for op in task.get('operations', []))
                if first_observation:
                    first_observation = False
                    if status == 'waiting' or pending:
                        # On resume there may have been a poll just before this
                        # process started. Wait a full interval conservatively.
                        next_tick = self.clock() + poll_seconds
                        continue
                if self.clock() >= deadline:
                    return report('max_seconds')
                ticks += 1  # A failed/uncertain call still consumes a worker tick.
                try:
                    task = self.service.advance(user, task_id)
                except PermissionError:
                    return report('account_revoked')
                except Exception as exc:
                    # Do not retry a failed call here. The persisted task/outbox
                    # decides recovery when an operator resumes the same task.
                    return report('execution_error', type(exc).__name__)
                if self.clock() >= deadline:
                    return report('max_seconds')
                status = task.get('status')
                if status in STOP_STATES:
                    return report(status)
                if status not in {'ready', 'waiting'}:
                    return report('unsupported_task_status')
                next_tick = self.clock() + poll_seconds if status == 'waiting' else self.clock()
            return report('max_ticks')
        except WorkerDeadline:
            return report('max_seconds')
        except KeyboardInterrupt:
            return report('interrupted')


@contextmanager
def hard_deadline(seconds):
    """POSIX foreground deadline also bounds a blocking model/network operation."""
    if not hasattr(signal, 'setitimer') or threading.current_thread() is not threading.main_thread():
        raise RuntimeError('The foreground worker requires POSIX main-thread deadline support')
    if signal.getitimer(signal.ITIMER_REAL)[0] != 0:
        raise RuntimeError('Refusing to replace an existing process deadline')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise WorkerDeadline()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--max-ticks', type=int, default=24)
    parser.add_argument('--max-seconds', type=float, default=300)
    parser.add_argument('--poll-seconds', type=float, default=10)
    args = parser.parse_args(argv)
    try:
        _bounds(args.max_ticks, args.max_seconds, args.poll_seconds)
        from harness_service import HarnessService
        from review_access import active_account
        runner = Runner(HarnessService(args.root), lambda owner: active_account(args.root, owner))
        with hard_deadline(args.max_seconds):
            result = runner.run(args.owner, args.task_id, max_ticks=args.max_ticks,
                                max_seconds=args.max_seconds, poll_seconds=args.poll_seconds)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    except WorkerDeadline:
        result = {'owner': args.owner, 'task_id': args.task_id, 'stop_reason': 'max_seconds',
                  'status_source': 'unknown; read persisted task before resuming'}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result['stop_reason'] in {'account_revoked', 'authorization_unavailable',
                'task_unavailable', 'execution_error', 'unsupported_task_status', 'failed', 'needs_attention'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
