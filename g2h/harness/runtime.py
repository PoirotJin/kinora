"""One bounded decision or reconciliation per invocation; no hidden retry loop."""
from .contracts import BudgetExceeded, Conflict, HarnessError, LeaseLost, TERMINAL_TASKS, new_id, validate_schema


class Harness:
    def __init__(self, store, registry, driver, worker_id=None, driver_id=None):
        self.store, self.registry, self.driver = store, registry, driver
        self.worker_id = worker_id or new_id('worker')
        self.driver_id = driver_id or getattr(driver, 'identity', driver.__class__.__name__)

    def advance(self, owner, task_id, *, reconcile_only=False, before_first_operation=False):
        task = self.store.get_task(owner, task_id)
        if task['status'] in TERMINAL_TASKS or task['status'] in {'waiting_user', 'awaiting_review', 'budget_exhausted'}:
            return task
        lease = self.store.claim(owner, task_id, self.worker_id, decision_owner=self.driver_id)
        try:
            task = self.store.get_task(owner, task_id)
            if before_first_operation and (task['status'] != 'ready' or task['operations']):
                return task
            pending = [op for op in task['operations'] if op['status'] in {'submitting', 'waiting', 'unknown'}]
            context = {'owner': owner, 'task': task, 'store': self.store}
            if pending:
                op = pending[0]
                tool = self.registry.get(op['tool'])
                if tool.reconcile is None:
                    result = {'state': 'unknown', 'result': {'error': 'Interrupted operation has no reconciler; manual confirmation required'}}
                else:
                    try:
                        result = tool.reconcile(context, op['arguments'], op['id'], op['handle'])
                    except Exception as exc:
                        result = {'state': 'unknown', 'handle': op['handle'], 'result': {'error': type(exc).__name__, 'message': 'Reconciliation unavailable; operation was not resubmitted'}}
                self.store.settle_operation(lease, op['id'], result)
                return self.store.get_task(owner, task_id)
            if reconcile_only:
                return self.store.get_task(owner, task_id)
            self.store.reserve_step(lease, getattr(self.driver, 'cost_upper_bound_usd', 0.0))
            task = self.store.get_task(owner, task_id)
            context['task'] = task
            try:
                action = self.driver.next_action(task, self.registry.schemas())
            except Exception as exc:
                self.store.set_runtime_status(lease, 'needs_attention', 'Model driver failed: ' + type(exc).__name__)
                return self.store.get_task(owner, task_id)
            if not isinstance(action, dict):
                raise HarnessError('Driver action must be an object')
            kind = action.get('kind')
            if kind == 'wait':
                self.store.set_runtime_status(lease, 'waiting_user', action.get('message', 'User input required'))
            elif kind == 'finish':
                # A model can offer a result for review, but cannot accept on the user's behalf.
                self.store.set_runtime_status(lease, 'awaiting_review', action.get('message', 'Review the candidate results'), finished=True)
            elif kind == 'tool':
                tool = self.registry.get(action.get('tool'))
                args = action.get('arguments', {})
                validate_schema(args, tool.input_schema)
                op = self.store.reserve_operation(lease, tool, args)
                try:
                    value = tool.execute(context, args, op['id'])
                    self.store.settle_operation(lease, op['id'], value)
                except LeaseLost:
                    raise
                except Exception as exc:
                    # The call may already have reached an external system. Do not repeat it.
                    value = {'state': 'unknown' if tool.side_effect else 'failed',
                             'result': {'error': type(exc).__name__, 'message': 'Tool failed; reconcile before any new submission'}}
                    self.store.settle_operation(lease, op['id'], value)
            else:
                raise HarnessError('Unknown driver action kind')
        except BudgetExceeded as exc:
            self.store.set_runtime_status(lease, 'budget_exhausted', str(exc))
        except HarnessError as exc:
            if not isinstance(exc, LeaseLost):
                self.store.set_runtime_status(lease, 'needs_attention', str(exc))
            else:
                raise
        finally:
            self.store.release(lease)
        return self.store.get_task(owner, task_id)
