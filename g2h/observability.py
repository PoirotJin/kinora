"""Bounded production events; failures are visible but never stop production.

Stage spans measure real executions (including failed attempts), not replayed
checkpoints. Stages are leaf operations; totals across concurrent jobs represent
service work seconds, not a partition of elapsed wall time. Motion facts travel
through a topic-local outbox whose commit timestamp is set by the records INSERT
transaction. Browsing/catalog backfills do not call this module.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

STAGES = {'qwen_generation', 'qwen_review', 'embedding', 'retrieval',
          'inference', 'storage', 'storage_maintenance', 'gpu_wait'}
_initialized = set()
_warned = {}


def failure(root, operation, error):
    """Never include exception messages, requests, credentials or motion text."""
    try:
        root = Path(root)
        now = time.time()
        key = (str(root), operation, type(error).__name__)
        if now - _warned.get(key, 0) < 30:
            return
        _warned[key] = now
        print('Telemetry unavailable: %s (%s); production continues' %
              (operation, type(error).__name__), file=sys.stderr, flush=True)
        db = None
        try:
            # _db never calls failure(), so a locked/broken telemetry database
            # cannot recurse. The file marker remains available when this fails.
            db = _db(root)
            with db:
                db.execute('INSERT INTO errors(ts,operation,error_type) VALUES(?,?,?)',
                           (now, operation, type(error).__name__))
        except Exception:
            pass
        finally:
            if db is not None:
                db.close()
        folder = root / 'review'
        folder.mkdir(exist_ok=True, parents=True)
        temp = folder / ('telemetry-error.%d.tmp' % os.getpid())
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
            stream.write(json.dumps({'time': now, 'operation': operation,
                                    'error_type': type(error).__name__}))
        os.replace(temp, folder / 'telemetry-error.json')
    except Exception:
        pass


def _db(root):
    path = Path(root) / 'review' / 'telemetry.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=.1)
    try:
        if str(path) not in _initialized:
            path.chmod(0o600)
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spans(
                  id TEXT PRIMARY KEY,started REAL NOT NULL,finished REAL,
                  stage TEXT NOT NULL,source TEXT NOT NULL,job TEXT NOT NULL,
                  topic TEXT NOT NULL,seconds REAL,status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS spans_finished ON spans(finished);
                CREATE INDEX IF NOT EXISTS spans_started ON spans(started);
                CREATE TABLE IF NOT EXISTS motions(
                  motion_id TEXT PRIMARY KEY,saved REAL NOT NULL,duration REAL NOT NULL,
                  topic TEXT NOT NULL,source TEXT NOT NULL,bytes INTEGER NOT NULL,
                  frames INTEGER NOT NULL,fps REAL NOT NULL,job TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS motions_saved ON motions(saved);
                CREATE TABLE IF NOT EXISTS errors(
                  id INTEGER PRIMARY KEY,ts REAL NOT NULL,operation TEXT NOT NULL,error_type TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS errors_ts ON errors(ts);
            ''')
            with db:
                db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
                db.execute("INSERT OR IGNORE INTO meta VALUES('started_at',?)", (str(time.time()),))
            _initialized.add(str(path))
        return db
    except Exception:
        db.close()
        raise


def _write(root, operation, statements):
    db = None
    try:
        db = _db(root)
        with db:
            for sql, values in statements:
                db.execute(sql, values)
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_success',?)", (str(time.time()),))
        return True
    except Exception as exc:
        failure(root, operation, exc)
        return False
    finally:
        if db is not None:
            db.close()


def initialize(root):
    return _write(root, 'initialize', [])


def context(source=None, job=None):
    source = source or os.environ.get('MOTIONFARM_TELEMETRY_SOURCE', 'manual')
    source = source if source in ('production', 'custom', 'manual') else 'manual'
    return source, str(job if job is not None else os.environ.get('MOTIONFARM_TELEMETRY_JOB', ''))[:120]


class Span:
    def __init__(self, root, stage, topic='', source=None, job=None):
        self.root, self.stage, self.topic = root, stage, str(topic)[:120]
        self.source, self.job = context(source, job)
        self.id, self.status = uuid.uuid4().hex, 'ok'
        self.started = self.clock = None

    def __enter__(self):
        self.started, self.clock = time.time(), time.monotonic()
        if self.stage not in STAGES:
            failure(self.root, 'span_stage', ValueError())
            return self
        _write(self.root, 'span_start', [(
            'INSERT INTO spans VALUES(?,?,NULL,?,?,?,?,NULL,?)',
            (self.id, self.started, self.stage, self.source, self.job, self.topic, 'running'))])
        return self

    def __exit__(self, kind, value, traceback):
        if self.stage in STAGES:
            status = 'failed' if kind is not None else self.status
            _write(self.root, 'span_finish', [(
                'INSERT OR REPLACE INTO spans VALUES(?,?,?,?,?,?,?,?,?)',
                (self.id, self.started, time.time(), self.stage, self.source, self.job,
                 self.topic, max(0., time.monotonic() - self.clock), status))])
        return False


def span(root, stage, topic='', source=None, job=None):
    return Span(root, stage, topic, source, job)


@contextmanager
def process_context(source, job):
    """Only use in dedicated worker processes; inherited by child processes."""
    keys = ('MOTIONFARM_TELEMETRY_SOURCE', 'MOTIONFARM_TELEMETRY_JOB')
    old = {key: os.environ.get(key) for key in keys}
    os.environ[keys[0]], os.environ[keys[1]] = context(source, job)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def record_context(record):
    source = record.get('farm', {}).get('source', {})
    custom = source.get('custom_request_id')
    if custom or record.get('topic') == 'custom':
        return context('custom', custom or '')
    return context()


@contextmanager
def _bounded_topic(db):
    previous = int(db.execute('PRAGMA busy_timeout').fetchone()[0])
    db.execute('PRAGMA busy_timeout=100')
    try:
        yield
    finally:
        db.execute('PRAGMA busy_timeout=%d' % previous)


def initialize_outbox(root, db):
    """Collector owns this connection; no web read path creates these rows."""
    try:
        with _bounded_topic(db):
            db.executescript('''
                CREATE TABLE IF NOT EXISTS telemetry_outbox(
                  task_id TEXT PRIMARY KEY,motion_id TEXT NOT NULL,saved REAL,
                  frames INTEGER NOT NULL,fps REAL NOT NULL,topic TEXT NOT NULL,
                  source TEXT NOT NULL,job TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS telemetry_motion_commit AFTER INSERT ON records
                BEGIN UPDATE telemetry_outbox SET saved=(julianday('now')-2440587.5)*86400
                  WHERE task_id=NEW.id; END;
            ''')
        return True
    except Exception as exc:
        failure(root, 'outbox_initialize', exc)
        return False


def prepare_motion(root, db, record):
    """Prepare before append; only records transaction authorizes counting."""
    try:
        quality = record['quality']
        frames, fps = int(quality['frames']), float(quality['fps'])
        if frames <= 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError('invalid duration')
        source, job = record_context(record)
        with _bounded_topic(db), db:
            db.execute('''INSERT OR IGNORE INTO telemetry_outbox
                SELECT ?,?,NULL,?,?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM records WHERE id=?)''',
                (record['id'], record.get('motion_id') or record['id'], frames, fps,
                 str(record.get('topic', ''))[:120], source, job, record['id']))
        return True
    except Exception as exc:
        failure(root, 'outbox_prepare', exc)
        return False


def flush_motions(root, db, limit=32):
    """At most one small batch; retries are safe across either DB commit."""
    try:
        with _bounded_topic(db):
            rows = db.execute('''SELECT o.task_id,o.motion_id,o.saved,o.frames,o.fps,
                o.topic,o.source,o.job,r.bytes FROM telemetry_outbox o
                JOIN records r ON r.id=o.task_id WHERE o.saved IS NOT NULL LIMIT ?''',
                (min(128, max(1, int(limit))),)).fetchall()
        if not rows:
            return True
        statements = [('INSERT OR IGNORE INTO motions VALUES(?,?,?,?,?,?,?,?,?)',
            (row[1], row[2], row[3]/row[4], row[5], row[6], row[8], row[3], row[4], row[7])) for row in rows]
        if not _write(root, 'motion_commit', statements):
            return False
        with _bounded_topic(db), db:
            db.executemany('DELETE FROM telemetry_outbox WHERE task_id=?', [(row[0],) for row in rows])
        return True
    except Exception as exc:
        failure(root, 'outbox_flush', exc)
        return False
