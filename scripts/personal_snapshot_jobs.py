"""Durable collection snapshot requests with one bounded background writer."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import json
import threading
import time

from g2h.capacity import CapacityReconciliationPending, CapacityUnavailable
from personal_assets import ID, Library, encoded, fields, label
from review_access import Forbidden, active_account
from review_collections import CollectionMembershipChanged
import re
import sqlite3

POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asset-snapshot')
LOCK = threading.Lock()
RUNNING = {}
MAX_SCHEDULED = 8


def setup(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS personal_snapshot_jobs(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, collection_id TEXT NOT NULL,
            basis TEXT NOT NULL, source TEXT, status TEXT NOT NULL, error TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS personal_snapshot_jobs_owner
            ON personal_snapshot_jobs(owner, status, created DESC);
        CREATE INDEX IF NOT EXISTS personal_snapshot_jobs_queued
            ON personal_snapshot_jobs(created, id) WHERE status='queued';
    """)


def get(library, sid):
    with closing(library.store.reviews()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,name,collection_id,status,error,created FROM personal_snapshot_jobs '
                         'WHERE owner=? AND id=?', (library.owner, sid)).fetchone()
    if row is None:
        raise Forbidden('请求不存在或不可访问')
    if row['status'] == 'queued':
        schedule(library, sid)
    return dict(row)


def submit(library, value):
    names = ('id','collection_id','name','expected_basis')
    fields(value, names, names)
    sid, cid, name = value['id'], value['collection_id'], label(value['name'])
    basis = value['expected_basis']
    if not isinstance(sid, str) or re.fullmatch(ID, sid) is None:
        raise ValueError('快照编号无效')
    with closing(library.store.reviews()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT owner,collection_id,name,basis FROM personal_snapshot_jobs WHERE id=?',
                         (sid,)).fetchone()
        if old is not None:
            if tuple(old) != (library.owner, cid, name, basis):
                raise ValueError('快照编号已被使用')
        else:
            source = library.read_collection_basis(db, cid)
            if source[2] != basis:
                raise CollectionMembershipChanged('集合成员已变化，请确认更新后的成员再保存快照。')
            pending = db.execute("SELECT count(*) FROM personal_snapshot_jobs WHERE owner=? AND status='queued'",
                                 (library.owner,)).fetchone()[0]
            if pending >= 2:
                raise ValueError('请等待当前快照完成后再提交')
            db.execute('INSERT INTO personal_snapshot_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                       (sid, library.owner, name, cid, basis, encoded(source).decode(), 'queued', '', time.time()))
    return get(library, sid)


def list_pending(db, owner, collection=None):
    scope = ' AND job.collection_id=?' if collection else ''
    args = (owner, collection) if collection else (owner,)
    rows = db.execute(
        "SELECT job.id,job.name,job.collection_id,job.status,job.error,job.created "
        "FROM personal_snapshot_jobs AS job WHERE job.owner=? AND job.status!='done'" + scope +
        " AND NOT EXISTS (SELECT 1 FROM personal_asset_snapshots AS snapshot "
        "WHERE snapshot.owner=job.owner AND snapshot.id=job.id)"
        " ORDER BY (job.status='queued') DESC, job.created DESC, job.id DESC LIMIT 20", args).fetchall()
    return [dict(row) for row in rows]


def resume_pending(store, stop):
    from harness_service import HarnessService

    service = None
    start = (-float('inf'), '')
    cursor = start
    end = None
    while not stop.is_set():
        try:
            with closing(store.reviews()) as db:
                if end is None:
                    # Bound each pass so new arrivals cannot delay retrying older jobs.
                    end = db.execute("SELECT created,id FROM personal_snapshot_jobs WHERE status='queued' "
                                     'ORDER BY created DESC,id DESC LIMIT 1').fetchone()
                rows = db.execute("SELECT id,owner,created FROM personal_snapshot_jobs WHERE status='queued' "
                                  'AND (created,id)>(?,?) AND (created,id)<=(?,?) '
                                  'ORDER BY created,id LIMIT ?', (*cursor, *end, MAX_SCHEDULED)).fetchall() if end else []
            if rows and service is None:
                service = HarnessService(store.root, legacy_store=store)
            for sid, owner, created in rows:
                if stop.is_set():
                    return
                with LOCK:
                    if len(RUNNING) >= MAX_SCHEDULED:
                        break
                    scheduled = (store.root, owner, sid) in RUNNING
                cursor = (created, sid)
                if not scheduled:
                    try:
                        schedule(Library(service, {'username': owner}), sid)
                    except (OSError, sqlite3.Error):
                        continue  # Retry this owner on the next pass.
            else:
                if len(rows) < MAX_SCHEDULED:
                    cursor, end = start, None
        except (OSError, sqlite3.Error):
            pass  # A later scan can retry a transient storage failure.
        stop.wait(1)


def schedule(library, sid):
    key = (library.store.root, library.owner, sid)
    reservation = object()
    with LOCK:
        if key in RUNNING or len(RUNNING) >= MAX_SCHEDULED:
            return
        RUNNING[key] = reservation
    try:
        POOL.submit(_run_scheduled, library, sid, key, reservation)
    except RuntimeError:
        _release(key, reservation)


def _release(key, reservation):
    with LOCK:
        if RUNNING.get(key) is reservation:
            del RUNNING[key]


def _run_scheduled(library, sid, key, reservation):
    # An executor may enqueue work before failing to start a worker.
    with LOCK:
        if RUNNING.get(key) is not reservation:
            return
    try:
        run(library, sid)
    finally:
        _release(key, reservation)


def _is_sqlite_contention(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    name = getattr(error, 'sqlite_errorname', '')
    if name:
        return name.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED'))
    # The deployed Python 3.10 runtime does not expose SQLite error names.
    return str(error).partition(':')[0] in ('database is locked', 'database table is locked',
                                             'database schema is locked')


def run(library, sid):
    with (library.root / '.snapshot.lock').open('a+b') as guard:
        # Multiple review workers can serve the same account. Only one can
        # materialize or resume a snapshot for this owner concurrently.
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with closing(library.store.reviews()) as db:
            row = db.execute("SELECT name,collection_id,basis,source FROM personal_snapshot_jobs "
                             "WHERE owner=? AND id=? AND status='queued'", (library.owner,sid)).fetchone()
        if row is None:
            return
        name, cid, basis, source = row
        try:
            role = active_account(library.store.root, library.owner)
            library.user = {**library.user, 'role': role}
            library.create_snapshot(dict(id=sid,collection_id=cid,name=name,expected_basis=basis),
                                    source=json.loads(source))
            result = ('done','')
        except CapacityReconciliationPending:
            return
        except CapacityUnavailable as exc:
            result = ('failed',str(exc))
        except (ValueError, PermissionError, Forbidden) as exc:
            result = ('failed',str(exc))
        except (OSError,KeyError,sqlite3.Error) as exc:
            if _is_sqlite_contention(exc):
                return
            result = ('failed','读取动作或写入快照失败，请检查后重新创建。')
        try:
            with closing(library.store.reviews()) as db, db:
                db.execute("UPDATE personal_snapshot_jobs SET status=?,error=?,source=NULL "
                           "WHERE owner=? AND id=? AND status='queued'", (*result,library.owner,sid))
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_contention(exc):
                raise
