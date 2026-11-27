"""Owner-isolated SQLite task ledger and transactional external-operation outbox.

The ledger never interprets a process restart as permission to repeat a side effect.
All runtime writes are fenced with a renewable, monotonically versioned run lease.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from .contracts import (BudgetExceeded, Conflict, HarnessError, LeaseLost, RunBusy, TERMINAL_TASKS,
                        budget, digest, encode, identifier, new_id, requirements, selection, text)


class Store:
    LEDGER_PATH = Path('review/harness.sqlite3')
    SCHEMA_VERSION = 1

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / self.LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise HarnessError('Harness database must not be a symbolic link')
        try:
            self.path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            pass
        self.path.chmod(0o600)
        with self._db() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= self.SCHEMA_VERSION:
                return
            db.executescript('''
              BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, goal TEXT NOT NULL, requirements TEXT NOT NULL,
                source_motion_id TEXT, current_motion_id TEXT, selection TEXT, budget TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                attempts INTEGER NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0,
                reserved_cost REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner,request_id));
              CREATE INDEX IF NOT EXISTS task_owner ON tasks(owner,updated);
              CREATE TABLE IF NOT EXISTS paired_scenes(
                first_task TEXT PRIMARY KEY REFERENCES tasks(id),
                second_task TEXT UNIQUE NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS scene_groups(
                group_task TEXT NOT NULL REFERENCES tasks(id),
                actor_index INTEGER NOT NULL,
                actor_task TEXT NOT NULL REFERENCES tasks(id),
                meta TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(group_task, actor_index));
              CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                status TEXT NOT NULL, lease_owner TEXT, lease_token INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
                decision_owner TEXT);
              CREATE INDEX IF NOT EXISTS task_runs ON runs(task_id,created);
              CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                run_id TEXT NOT NULL REFERENCES runs(id), tool TEXT NOT NULL,
                arguments TEXT NOT NULL, arguments_hash TEXT NOT NULL,
                task_revision INTEGER NOT NULL, side_effect INTEGER NOT NULL,
                status TEXT NOT NULL, handle TEXT, result TEXT, max_cost REAL NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS task_operations ON operations(task_id,created);
              CREATE TABLE IF NOT EXISTS candidates(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                operation_id TEXT REFERENCES operations(id), motion_id TEXT NOT NULL,
                parent_id TEXT, metadata TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, UNIQUE(task_id,operation_id,motion_id));
              CREATE INDEX IF NOT EXISTS candidate_motion ON candidates(motion_id);
              CREATE TABLE IF NOT EXISTS evidence(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                operation_id TEXT REFERENCES operations(id), candidate_id TEXT REFERENCES candidates(id),
                kind TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
            ''')
            if db.execute('PRAGMA user_version').fetchone()[0] >= self.SCHEMA_VERSION:
                return
            columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            if 'decision_owner' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN decision_owner TEXT')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'current_motion_id' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN current_motion_id TEXT')
            # An older migration could create the column and stop before writing any values.
            # Restore the last explicit acceptance, with the source as the default.
            for task in db.execute('SELECT id,source_motion_id FROM tasks WHERE current_motion_id IS NULL'):
                event = db.execute("SELECT body FROM events WHERE task_id=? AND kind='candidate_accepted_by_user' ORDER BY sequence DESC LIMIT 1", (task['id'],)).fetchone()
                candidate = None
                if event:
                    cid = json.loads(event['body']).get('candidate_id')
                    candidate = db.execute('SELECT motion_id FROM candidates WHERE task_id=? AND id=?', (task['id'], cid)).fetchone()
                current = candidate['motion_id'] if candidate else task['source_motion_id']
                if current is not None:
                    db.execute('UPDATE tasks SET current_motion_id=? WHERE id=?', (current, task['id']))
            db.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _task(self, db, owner, task_id):
        identifier(owner, 'owner')
        row = db.execute('SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, owner)).fetchone()
        if row is None:
            raise KeyError('Task not found')
        return row

    @staticmethod
    def _event(db, task_id, kind, body):
        db.execute('INSERT INTO events(task_id,kind,body,created) VALUES(?,?,?,?)',
                   (task_id, kind, encode(body), time.time()))

    @staticmethod
    def _status(db, task_id, status):
        db.execute('UPDATE tasks SET status=?,updated=? WHERE id=?', (status, time.time(), task_id))

    def link_scene_pair(self, owner, first, second, kind):
        with self._db(True) as db:
            self._task(db, owner, first)
            self._task(db, owner, second)
            db.execute('INSERT INTO paired_scenes VALUES(?,?,?) ON CONFLICT(first_task) DO NOTHING',
                       (first, second, kind))
            stored = db.execute('SELECT second_task,kind FROM paired_scenes WHERE first_task=?', (first,)).fetchone()
            if tuple(stored) != (second, kind):
                raise Conflict('Paired scene already linked to a different task')

    def scene_partner(self, owner, task_id):
        with self._db() as db:
            row = db.execute('SELECT kind,second_task FROM paired_scenes JOIN tasks ON id=first_task '
                             'WHERE first_task=? AND owner=?', (task_id, owner)).fetchone()
            return dict(row) if row else None

    def link_scene_group(self, owner, group_task, actor_tasks, meta=None):
        """Persist an ordered multi-actor group with owner validation and idempotent checks."""
        if not isinstance(actor_tasks, (list, tuple)) or not actor_tasks:
            raise HarnessError('Scene group requires at least one actor task')
        if len(set(actor_tasks)) != len(actor_tasks):
            raise HarnessError('Duplicate actor task in scene group')
        meta_obj = {} if meta is None else meta
        if not isinstance(meta_obj, dict):
            raise HarnessError('Invalid scene group metadata')
        with self._db(True) as db:
            self._link_scene_group(db, owner, group_task, actor_tasks, meta_obj)

    def _link_scene_group(self, db, owner, group_task, actor_tasks, meta, actor_requests=None):
        self._task(db, owner, group_task)
        for tid in actor_tasks:
            self._task(db, owner, tid)
        existing = db.execute(
            'SELECT actor_index, actor_task, meta FROM scene_groups '
            'WHERE group_task=? ORDER BY actor_index', (group_task,),
        ).fetchall()
        public_meta = encode(meta)
        fingerprint = digest([actor_requests, meta]) if actor_requests is not None else None
        stored_json = encode({**meta, '_request_hash': fingerprint, '_actor_requests': actor_requests}) if fingerprint else public_meta
        if existing:
            stored = json.loads(existing[0]['meta'])
            stored_hash = stored.pop('_request_hash', None)
            stored_requests = stored.pop('_actor_requests', None)
            if ([(row['actor_index'], row['actor_task']) for row in existing] != list(enumerate(actor_tasks))
                    or encode(stored) != public_meta
                    or (fingerprint and stored_hash and fingerprint != stored_hash)):
                raise Conflict('该多角色场景编号已用于其他参数')
            if fingerprint and stored_requests is None:
                db.execute('UPDATE scene_groups SET meta=? WHERE group_task=? AND actor_index=0',
                           (stored_json, group_task))
            return
        db.executemany(
            'INSERT INTO scene_groups(group_task,actor_index,actor_task,meta) VALUES(?,?,?,?)',
            [(group_task, idx, tid, stored_json if idx == 0 else public_meta)
             for idx, tid in enumerate(actor_tasks)],
        )

    def admit_scene_group(self, owner, actor_requests, meta, *, budget):
        """Bind the ordered actor plans and task IDs before any queue side effects."""
        with self._db(True) as db:
            tasks = []
            for request_id, arguments in actor_requests:
                task_id = self._create_task(db, owner, request_id, arguments['text'], budget=budget)
                # Older servers may have submitted an actor before linking its group.
                existing = db.execute(
                    "SELECT arguments FROM operations WHERE task_id=? AND tool='generate_motion'", (task_id,),
                ).fetchall()
                if any(json.loads(row['arguments']) != arguments for row in existing):
                    raise Conflict('该多角色场景编号已用于其他参数')
                tasks.append(task_id)
            self._link_scene_group(db, owner, tasks[0], tasks, meta, actor_requests)
        return tasks

    def scene_group(self, owner, task_id, *, include_submission=False):
        """Return {'tasks': [...], 'meta': {...}} for an owner-verified group task, else None."""
        identifier(owner, 'owner')
        if not isinstance(task_id, str) or not task_id:
            return None
        with self._db() as db:
            rows = db.execute(
                'SELECT g.actor_task, g.meta '
                'FROM scene_groups g '
                'JOIN tasks gt ON gt.id = g.group_task '
                'JOIN tasks at ON at.id = g.actor_task '
                'WHERE g.group_task=? AND gt.owner=? AND at.owner=? '
                'ORDER BY g.actor_index',
                (task_id, owner, owner),
            ).fetchall()
            if not rows:
                return None
            total = db.execute(
                'SELECT COUNT(*) FROM scene_groups WHERE group_task=?',
                (task_id,),
            ).fetchone()[0]
            if len(rows) != total:
                return None
            meta = json.loads(rows[0]['meta'])
            fingerprint = meta.pop('_request_hash', None)
            requests = meta.pop('_actor_requests', None)
            group = {'tasks': [row['actor_task'] for row in rows], 'meta': meta,
                     'has_submission': requests is not None}
            if include_submission and requests is not None:
                if len(requests) != len(rows) or digest([requests, meta]) != fingerprint:
                    raise HarnessError('保存的场景提交参数校验失败')
                group['actor_requests'] = requests
            return group

    def create_task(self, owner, request_id, goal, requirements=None, source_motion_id=None,
                    selection=None, budget=None):
        with self._db(True) as db:
            task_id = self._create_task(db, owner, request_id, goal, requirements=requirements,
                                        source_motion_id=source_motion_id, selection=selection, budget=budget)
        return self.get_task(owner, task_id)

    def _create_task(self, db, owner, request_id, goal, *, requirements=None, source_motion_id=None,
                     selection=None, budget=None):
        identifier(owner, 'owner'); identifier(request_id, 'request id')
        goal = text(goal, 'goal')
        if source_motion_id is not None:
            identifier(source_motion_id, 'source motion')
        picked = globals()['selection'](selection)
        if picked is not None and source_motion_id is None:
            raise HarnessError('A selected interval requires a source motion')
        limits = globals()['budget'](budget)
        requested = dict(goal=goal, requirements=requirements or [], source_motion_id=source_motion_id,
                         selection=picked, budget=limits)
        fingerprint = digest(requested)
        reqs = globals()['requirements'](requirements)
        old = db.execute('SELECT id,request_hash FROM tasks WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        if old:
            if old['request_hash'] != fingerprint:
                raise Conflict('Request id already used with different arguments')
            task_id = old['id']
        else:
            task_id = new_id('task'); now = time.time()
            db.execute('''INSERT INTO tasks(id,owner,request_id,request_hash,goal,requirements,
              source_motion_id,current_motion_id,selection,budget,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (task_id, owner, request_id, fingerprint, goal, encode(reqs), source_motion_id, source_motion_id,
                        encode(picked), encode(limits), 'ready', now, now))
            self._event(db, task_id, 'task_created', dict(goal=goal, revision=1))
        return task_id

    @staticmethod
    def _decode(row, fields):
        item = dict(row)
        for field in fields:
            if item.get(field) is not None:
                item[field] = json.loads(item[field])
        return item

    def get_task(self, owner, task_id):
        with self._db() as db:
            result = self._decode(self._task(db, owner, task_id), ['requirements', 'selection', 'budget'])
            for key, fields in [('operations', ['arguments', 'handle', 'result']),
                                ('candidates', ['metadata']), ('evidence', ['body'])]:
                result[key] = [self._decode(row, fields) for row in db.execute(
                    'SELECT * FROM ' + key + ' WHERE task_id=? ORDER BY created,id', (task_id,))]
            result['events'] = [self._decode(row, ['body']) for row in db.execute(
                'SELECT * FROM events WHERE task_id=? ORDER BY sequence DESC LIMIT 100', (task_id,))][::-1]
            result['runs'] = [dict(row) for row in db.execute(
                'SELECT id,status,lease_token,created,updated FROM runs WHERE task_id=? ORDER BY created', (task_id,))]
            return result

    def candidate_results(self, owner, motion_ids):
        """Read the result provenance of the requested versions for one owner."""
        placeholders = ','.join('?' for _ in motion_ids)
        with self._db() as db:
            rows = db.execute(f'''
                SELECT c.motion_id, c.task_id, c.status,
                       c.status = 'accepted' AND t.status = 'accepted'
                           AND t.current_motion_id IS c.motion_id AS accepted
                FROM candidates c
                JOIN tasks t ON t.id = c.task_id
                WHERE t.owner = ? AND c.motion_id IN ({placeholders})
                ORDER BY c.status = 'quarantined' DESC, accepted DESC, c.created DESC, c.id DESC
            ''', (owner, *motion_ids))
            results = {}
            for row in rows:
                results.setdefault(row['motion_id'], {
                    'task_id': row['task_id'],
                    'status': row['status'],
                    'accepted': bool(row['accepted']),
                })
            return results

    def list_tasks(self, owner, limit=50, *, grouped=False):
        identifier(owner, 'owner')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HarnessError('Invalid limit')
        group_filter = '''
          AND t.id NOT IN (
              SELECT g.actor_task FROM tasks root JOIN scene_groups g ON g.group_task=root.id
              WHERE root.owner=:owner AND g.actor_task<>g.group_task
              UNION ALL
              SELECT p.second_task FROM tasks root JOIN paired_scenes p ON p.first_task=root.id
              WHERE root.owner=:owner
          )
        ''' if grouped else ''
        with self._db() as db:
            rows = db.execute(
                '''SELECT t.*,
                          (EXISTS(SELECT 1 FROM scene_groups WHERE group_task=t.id)
                           OR EXISTS(SELECT 1 FROM paired_scenes WHERE first_task=t.id)) AS scene_group,
                          EXISTS(SELECT 1 FROM operations
                                 WHERE task_id=t.id AND tool='generate_motion'
                                   AND json_type(arguments, '$.scene')='object') AS scene,
                          (SELECT status FROM operations WHERE task_id=t.id
                           ORDER BY created DESC, id DESC LIMIT 1) AS last_operation_status
                   FROM tasks t WHERE t.owner=:owner''' + group_filter +
                ' ORDER BY t.updated DESC, t.id DESC LIMIT :limit',
                {'owner': owner, 'limit': limit}).fetchall()
            tasks = []
            for row in rows:
                task = self._decode(row, ['budget', 'requirements', 'selection'])
                if task.pop('scene_group'):
                    task['scene_group'] = True
                if task.pop('scene'):
                    task['scene'] = True
                if task['last_operation_status'] is None:
                    del task['last_operation_status']
                tasks.append(task)
            return tasks

    def add_feedback(self, owner, task_id, message, requirements=None):
        message = text(message, 'feedback')
        additions = globals()['requirements'](requirements if requirements is not None else [message])
        with self._db(True) as db:
            row = self._task(db, owner, task_id)
            if row['status'] == 'cancelled':
                raise Conflict('Cancelled task cannot be resumed')
            current = json.loads(row['requirements'])
            if len(current) + len(additions) > 100:
                raise HarnessError('Too many requirements')
            db.execute('UPDATE tasks SET requirements=?,revision=revision+1,status=?,updated=? WHERE id=?',
                       (encode(current + additions), 'ready', time.time(), task_id))
            # A model call started on an earlier revision must not commit its decision.
            db.execute('UPDATE runs SET lease_token=lease_token+1,lease_owner=NULL,lease_until=0 WHERE task_id=?',
                       (task_id,))
            self._event(db, task_id, 'user_feedback', dict(message=message, requirements=additions))
        return self.get_task(owner, task_id)

    def cancel_task(self, owner, task_id, *, related=(), cancel_scene_jobs=None):
        task_ids = tuple(dict.fromkeys((task_id, *related)))
        with self._db(True) as db:
            tasks = [self._task(db, owner, tid) for tid in task_ids]
            if cancel_scene_jobs is not None:
                placeholders = ','.join('?' for _ in task_ids)
                operations = db.execute(f"""SELECT id FROM operations WHERE task_id IN ({placeholders})
                    AND tool='generate_motion' AND json_type(arguments, '$.scene')='object'""", task_ids)
                # Generation submits under the same ledger lock. Stop the existing
                # queue jobs before acknowledging the task cancellation.
                cancel_scene_jobs([op['id'] for op in operations])
            for task in tasks:
                if task['status'] == 'cancelled':
                    continue
                tid = task['id']
                self._status(db, tid, 'cancelled')
                db.execute('UPDATE runs SET status=?,lease_token=lease_token+1,lease_owner=NULL,lease_until=0 WHERE task_id=?',
                           ('cancelled', tid))
                self._event(db, tid, 'cancel_requested', {'external_operations': 'preserved; reconcile before resubmit'})
        return self.get_task(owner, task_id)

    def register_candidate(self, owner, task_id, motion_id, metadata=None, parent_id=None):
        with self._db(True) as db:
            task = self._task(db, owner, task_id)
            if task['status'] in TERMINAL_TASKS:
                raise Conflict('Reopen with feedback before adding a candidate to a terminal task')
            result = self._candidate(db, task_id, None, motion_id, metadata, parent_id)
            self._event(db, task_id, 'candidate_registered', {'candidate_id': result['id']})
        return result

    def task_status(self, owner, task_id):
        with self._db() as db:
            return self._task(db, owner, task_id)['status']

    @contextmanager
    def while_task_not_cancelled(self, owner, task_id):
        with self._db(True) as db:
            if self._task(db, owner, task_id)['status'] == 'cancelled':
                raise InterruptedError('Task was cancelled before publication')
            yield

    def _candidate(self, db, task_id, operation_id, motion_id, metadata=None, parent_id=None):
        identifier(motion_id, 'motion id')
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise HarnessError('Invalid candidate metadata')
        if parent_id is not None:
            identifier(parent_id, 'parent id')
        status = 'quarantined' if metadata.get('quarantined') else 'pending_review'
        old = db.execute('SELECT * FROM candidates WHERE task_id=? AND operation_id IS ? AND motion_id=?',
                         (task_id, operation_id, motion_id)).fetchone()
        if old:
            if json.loads(old['metadata']) != metadata or old['parent_id'] != parent_id:
                raise Conflict('Candidate idempotency mismatch')
            return self._decode(old, ['metadata'])
        task = db.execute('SELECT budget FROM tasks WHERE id=?', (task_id,)).fetchone()
        limit = json.loads(task['budget'])['max_candidates']
        count = db.execute('SELECT COUNT(*) FROM candidates WHERE task_id=?', (task_id,)).fetchone()[0]
        if count >= limit:
            raise BudgetExceeded('Candidate budget exhausted')
        cid = new_id('candidate')
        db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)',
                   (cid, task_id, operation_id, motion_id, parent_id, encode(metadata), status, time.time()))
        return self._decode(db.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone(), ['metadata'])

    def accept_candidate(self, owner, task_id, candidate_id):
        with self._db(True) as db:
            task = self._task(db, owner, task_id)
            if task['status'] == 'cancelled':
                raise Conflict('Cancelled task cannot accept candidates')
            row = db.execute('SELECT * FROM candidates WHERE id=? AND task_id=?', (candidate_id, task_id)).fetchone()
            if row is None:
                raise KeyError('Candidate not found')
            if row['status'] == 'quarantined':
                raise Conflict('Quarantined candidate cannot be accepted')
            db.execute('UPDATE candidates SET status=? WHERE id=?', ('accepted', candidate_id))
            db.execute('UPDATE tasks SET current_motion_id=? WHERE id=?', (row['motion_id'], task_id))
            self._status(db, task_id, 'accepted')
            db.execute('UPDATE runs SET status=?,lease_token=lease_token+1,lease_owner=NULL,lease_until=0 WHERE task_id=?',
                       ('accepted', task_id))
            self._event(db, task_id, 'candidate_accepted_by_user', {'candidate_id': candidate_id})
        return self.get_task(owner, task_id)

    def create_run(self, owner, task_id):
        with self._db(True) as db:
            task = self._task(db, owner, task_id)
            if task['status'] in TERMINAL_TASKS:
                raise Conflict('Task is terminal')
            row = db.execute("SELECT * FROM runs WHERE task_id=? AND status NOT IN ('accepted','cancelled','failed','finished') ORDER BY created DESC LIMIT 1", (task_id,)).fetchone()
            if row:
                return dict(row)
            run_id = new_id('run'); now = time.time()
            db.execute('INSERT INTO runs(id,task_id,status,created,updated) VALUES(?,?,?,?,?)',
                       (run_id, task_id, 'ready', now, now))
            self._event(db, task_id, 'run_created', {'run_id': run_id})
            return dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def claim(self, owner, task_id, worker, ttl=120, decision_owner='manual'):
        run = self.create_run(owner, task_id)
        identifier(decision_owner, 'decision owner')
        if not 1 <= ttl <= 600:
            raise HarnessError('Invalid lease duration')
        with self._db(True) as db:
            task = self._task(db, owner, task_id)
            if task['status'] in TERMINAL_TASKS:
                raise Conflict('Task is terminal')
            row = db.execute('SELECT * FROM runs WHERE id=?', (run['id'],)).fetchone()
            now = time.time()
            if row['lease_owner'] and row['lease_until'] > now:
                raise RunBusy('Run is already advancing')
            if row['decision_owner'] is not None and row['decision_owner'] != decision_owner:
                raise Conflict('Run belongs to another decision driver; explicit takeover required')
            token = row['lease_token'] + 1
            db.execute('UPDATE runs SET lease_owner=?,lease_token=?,lease_until=?,updated=?,decision_owner=? WHERE id=?',
                       (worker, token, now + ttl, now, decision_owner, run['id']))
            return dict(run_id=run['id'], task_id=task_id, owner=owner, worker=worker, token=token)

    def _fence(self, db, lease):
        task = self._task(db, lease['owner'], lease['task_id'])
        run = db.execute('SELECT * FROM runs WHERE id=? AND task_id=?', (lease['run_id'], lease['task_id'])).fetchone()
        if (run is None or run['lease_owner'] != lease['worker'] or run['lease_token'] != lease['token']
                or run['lease_until'] <= time.time() or task['status'] in TERMINAL_TASKS):
            raise LeaseLost('Run lease is no longer current')
        return task

    def release(self, lease):
        with self._db(True) as db:
            db.execute('UPDATE runs SET lease_owner=NULL,lease_until=0 WHERE id=? AND lease_owner=? AND lease_token=?',
                       (lease['run_id'], lease['worker'], lease['token']))

    def reserve_step(self, lease, max_cost_usd=0.0):
        from math import isfinite
        if type(max_cost_usd) not in (int, float) or not isfinite(max_cost_usd) or max_cost_usd < 0:
            raise HarnessError('Invalid model request cost bound')
        with self._db(True) as db:
            task = self._fence(db, lease); limits = json.loads(task['budget'])
            if task['steps'] >= limits['max_steps']:
                raise BudgetExceeded('Decision step budget exhausted')
            if task['reserved_cost'] + max_cost_usd > limits['max_cost_usd'] + 1e-12:
                raise BudgetExceeded('Model request cost reservation exceeds budget')
            db.execute('UPDATE tasks SET steps=steps+1,reserved_cost=reserved_cost+? WHERE id=?', (max_cost_usd, task['id']))
            self._event(db, task['id'], 'decision_started', {'step': task['steps'] + 1, 'max_cost_usd': max_cost_usd})

    def reserve_operation(self, lease, tool, arguments):
        with self._db(True) as db:
            task = self._fence(db, lease); limits = json.loads(task['budget'])
            if task['attempts'] >= limits['max_attempts']:
                raise BudgetExceeded('Tool attempt budget exhausted')
            if task['reserved_cost'] + tool.max_cost_usd > limits['max_cost_usd'] + 1e-12:
                raise BudgetExceeded('Tool cost reservation exceeds budget')
            count = db.execute('SELECT COUNT(*) FROM candidates WHERE task_id=?', (task['id'],)).fetchone()[0]
            if tool.side_effect and count >= limits['max_candidates']:
                raise BudgetExceeded('Candidate budget exhausted')
            op_id = new_id('op'); now = time.time()
            # This is the transactional outbox: durable intent precedes the external call.
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (op_id, task['id'], lease['run_id'], tool.name, encode(arguments), digest(arguments),
                        task['revision'], int(tool.side_effect), 'submitting', None, None, tool.max_cost_usd, now, now))
            db.execute('UPDATE tasks SET attempts=attempts+1,reserved_cost=reserved_cost+? WHERE id=?', (tool.max_cost_usd, task['id']))
            self._event(db, task['id'], 'operation_intent', {'operation_id': op_id, 'tool': tool.name})
            return self._decode(db.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone(), ['arguments', 'handle', 'result'])

    def settle_operation(self, lease, operation_id, value):
        if not isinstance(value, dict) or value.get('state') not in {'succeeded', 'waiting', 'failed', 'unknown'}:
            raise HarnessError('Tool returned an invalid result state')
        encode(value)
        with self._db(True) as db:
            task = self._fence(db, lease)
            op = db.execute('SELECT * FROM operations WHERE id=? AND task_id=?', (operation_id, task['id'])).fetchone()
            if op is None:
                raise KeyError('Operation not found')
            if op['status'] in {'succeeded', 'failed'}:
                raise Conflict('Operation has already settled')
            candidates = value.get('candidates', [])
            if not isinstance(candidates, list):
                raise HarnessError('Invalid candidate result')
            limits = json.loads(task['budget'])
            count = db.execute('SELECT COUNT(*) FROM candidates WHERE task_id=?', (task['id'],)).fetchone()[0]
            if count + len(candidates) > limits['max_candidates']:
                # Keep the external result as evidence; never hide or accept an oversized batch.
                value = dict(value, state='failed', result={'error': 'Candidate budget exceeded', 'uncommitted_result': value})
                candidates = []
            for item in candidates:
                self._candidate(db, task['id'], operation_id, item['motion_id'], item.get('metadata'), item.get('parent_id'))
            for item in value.get('evidence', []):
                self._add_evidence(db, task['id'], operation_id, item)
            state = value['state']
            db.execute('UPDATE operations SET status=?,handle=?,result=?,updated=? WHERE id=?',
                       (state, encode(value.get('handle')), encode(value.get('result', {})), time.time(), operation_id))
            self._status(db, task['id'], {'waiting': 'waiting', 'unknown': 'needs_attention'}.get(state, 'ready'))
            self._event(db, task['id'], 'operation_' + state, {'operation_id': operation_id, 'tool': op['tool']})

    def _add_evidence(self, db, task_id, operation_id, item):
        if not isinstance(item, dict) or item.get('status') not in {'passed', 'failed', 'unknown'}:
            raise HarnessError('Evidence status must be passed, failed or unknown')
        kind = text(item.get('kind'), 'evidence kind', 160)
        candidate_id = item.get('candidate_id')
        if candidate_id and not db.execute('SELECT id FROM candidates WHERE id=? AND task_id=?', (candidate_id, task_id)).fetchone():
            raise KeyError('Evidence candidate not found')
        db.execute('INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)',
                   (new_id('evidence'), task_id, operation_id, candidate_id, kind, item['status'], encode(item.get('body', {})), time.time()))

    def add_evidence(self, owner, task_id, kind, status, body, candidate_id=None):
        with self._db(True) as db:
            self._task(db, owner, task_id)
            self._add_evidence(db, task_id, None, dict(kind=kind, status=status, body=body, candidate_id=candidate_id))
        return self.get_task(owner, task_id)

    def set_runtime_status(self, lease, status, message, finished=False):
        if status not in {'ready', 'waiting_user', 'awaiting_review', 'needs_attention', 'budget_exhausted'}:
            raise HarnessError('Invalid runtime task status')
        with self._db(True) as db:
            task = self._fence(db, lease)
            self._status(db, task['id'], status)
            db.execute('UPDATE runs SET status=?,updated=? WHERE id=?', ('finished' if finished else status, time.time(), lease['run_id']))
            self._event(db, task['id'], 'agent_' + status, {'message': str(message)[:8000]})
