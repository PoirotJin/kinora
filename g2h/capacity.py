"""Conservative shared disk accounting; SQLite serializes space reservations.

Large writers reserve before touching disk. A bounded background scan repairs
crash leftovers and accounts for logs/metadata; incomplete scans never lower the
ledger. The 10 GB production reserve remains separate from metadata headroom.
"""
from contextlib import closing
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time

METADATA_HEADROOM = 256 * 1024 * 1024
MAX_SNAPSHOT_AGE = 300
SNAPSHOT_RESTORE_PURPOSE = 'snapshot-source-restore:'
SNAPSHOT_RESTORE_TEMP = '.snapshot-restore-'
PENDING_MOTION_ASSET = '.pending-registration'
SNAPSHOT_MANIFEST_TEMP = '.snapshot-manifest-'
SNAPSHOT_MANIFEST = re.compile(r'([0-9a-f]{32})\.json\.gz')
PERSONAL_ASSET_ACCOUNT = re.compile(r'[0-9a-f]{64}')


class CapacityUnavailable(RuntimeError):
    pass


class CapacityReconciliationPending(CapacityUnavailable):
    pass


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return ''


def worker_exited(pid, saved_identity):
    if pid is None or pid <= 0:
        return True
    current_identity = identity(pid) if saved_identity else ''
    if current_identity:
        return current_identity != saved_identity
    # macOS and some containers have no readable procfs. Keep the reservation
    # until the PID disappears, including when a different user owns it.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def connect(root):
    path = Path(root) / 'review/storage/capacity.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS capacity(id INTEGER PRIMARY KEY CHECK(id=1),
        total INTEGER NOT NULL, growth INTEGER NOT NULL, sampled REAL NOT NULL,
        hot INTEGER NOT NULL, cold INTEGER NOT NULL, exports INTEGER NOT NULL, files INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,bytes INTEGER NOT NULL,
        purpose TEXT NOT NULL,pid INTEGER NOT NULL,identity TEXT NOT NULL,created REAL NOT NULL,
        abandoned INTEGER NOT NULL DEFAULT 0);
    ''')
    return db


def ensure(root):
    with closing(connect(root)) as db:
        if db.execute('SELECT 1 FROM capacity').fetchone():
            return
        # One bounded initial inventory while production is not yet admitted.
        # Existing deployments seed this before enabling jobs.
        from g2h.cold_storage import usage
        snapshot = usage(root)
        with db:
            db.execute('INSERT OR IGNORE INTO capacity VALUES(1,?,0,?,?,?,?,?)',
                       (snapshot['total'], snapshot['sampled_at'], snapshot['hot'],
                        snapshot['cold'], snapshot['exports'], snapshot['files']))


def _snapshot(db):
    row = db.execute('SELECT total,growth,sampled,hot,cold,exports,files FROM capacity WHERE id=1').fetchone()
    pending = db.execute('SELECT coalesce(sum(bytes),0) FROM reservations').fetchone()[0]
    from g2h.cold_storage import LIMIT_BYTES, RESERVE_BYTES
    return dict(ready=True,total=row[0] + pending, accounted_bytes=row[0], reserved_bytes=pending,
                growth=row[1], sampled_at=row[2], hot=row[3], cold=row[4], exports=row[5],
                files=row[6], limit=LIMIT_BYTES, reserve=RESERVE_BYTES,
                metadata_headroom=METADATA_HEADROOM, stale=time.time()-row[2] > MAX_SNAPSHOT_AGE)


def status(root, initialize=True):
    if initialize:ensure(root)
    with closing(connect(root)) as db:
        if not db.execute('SELECT 1 FROM capacity').fetchone():
            from g2h.cold_storage import LIMIT_BYTES,RESERVE_BYTES
            return dict(ready=False,total=None,hot=None,cold=None,exports=None,files=None,
                        limit=LIMIT_BYTES,reserve=RESERVE_BYTES,sampled_at=None,stale=True,reserved_bytes=0)
        return _snapshot(db)


def _check(root, snapshot, additional, reclaim=False):
    if snapshot['stale']:
        raise CapacityReconciliationPending('容量账本校准已过期，等待后台存储核对后重试')
    quota_reserve=0 if reclaim else snapshot['reserve']
    if snapshot['total'] + additional + METADATA_HEADROOM >= snapshot['limit'] - quota_reserve:
        raise CapacityUnavailable('归档临时空间将超过 800 GB 上限' if reclaim else
                                  '已达到 790 GB 生产保护线（含正在写入的空间预留）；请归档或迁出数据')
    # Pending reservations can already be partially materialized: subtracting
    # their full amount again is conservative, never grants extra disk space.
    if shutil.disk_usage(root).free - snapshot['reserved_bytes'] - additional < snapshot['reserve']:
        raise CapacityUnavailable('实际数据盘剩余空间不足 10 GB 加本次写入预留')


def check(root, additional=0):
    snapshot = status(root)
    _check(root, snapshot, max(0, int(additional)))
    return snapshot


class Reservation:
    def __init__(self, root, additional, purpose, reclaim=False):
        self.root = Path(root)
        self.bytes = max(0, int(additional))
        self.key = secrets.token_hex(16)
        self.done = False
        ensure(self.root)
        with closing(connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                snapshot=_snapshot(db)
                _check(self.root, snapshot, self.bytes, reclaim=reclaim)
                self.baseline=snapshot['sampled_at']
                db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,0)',
                           (self.key, self.bytes, str(purpose)[:160], os.getpid(), identity(), time.time()))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def __enter__(self):
        return self

    def commit(self, actual_delta):
        """Account this operation's physical byte delta, not its final directory size."""
        if self.done:
            return
        delta = int(actual_delta)
        with closing(connect(self.root)) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM reservations WHERE id=?', (self.key,)).fetchone():
                sampled=db.execute('SELECT sampled FROM capacity WHERE id=1').fetchone()[0]
                # A new baseline may already contain part of this operation's
                # files, while replacements/deletions can precede publication.
                # Carry the peak reservation when reconciliation overlapped us.
                peak=max(self.bytes,delta,0)
                charged=peak if delta<0 or sampled!=self.baseline else delta
                db.execute('UPDATE capacity SET total=max(0,total+?),growth=growth+? WHERE id=1',
                           (charged, peak))
                db.execute('DELETE FROM reservations WHERE id=?', (self.key,))
            db.commit()
        self.done = True
        # Retain the actual accounting even when a buggy writer exceeded its
        # estimate. Do not raise after an already committed motion and invite a retry.

    def __exit__(self, *exc):
        if not self.done:
            # We cannot assume an exception means no partial file was written.
            with closing(connect(self.root)) as db, db:
                db.execute('UPDATE reservations SET abandoned=1 WHERE id=?', (self.key,))


def reservation(root, additional, purpose, reclaim=False):
    return Reservation(root, additional, purpose, reclaim=reclaim)


def release(root, removed_bytes):
    """Deletion space is reclaimed by the next complete background scan.

    Subtracting immediately can double-apply a deletion already seen by a
    concurrent scan. A conservative delay (normally <=30 seconds) is safe.
    """
    if int(removed_bytes)<0:raise ValueError('removed_bytes must be nonnegative')


class Reconciler:
    """Incremental metadata scan: no more than a small time/entry slice per call."""
    def __init__(self, root):
        self.root = Path(root)
        self.stack = []
        self.data = None

    def close(self):
        for _, iterator in self.stack:
            iterator.close()
        self.stack = []
        self.data = None

    def _remove_abandoned_restore(self, entry):
        if not entry.name.startswith(SNAPSHOT_RESTORE_TEMP):
            return False
        key = entry.name.removeprefix(SNAPSHOT_RESTORE_TEMP)
        parts = Path(entry.path).relative_to(self.root).parts
        if (len(parts) != 5 or parts[:2] != ('harness_assets', 'objects')
                or self.orphans.get(key) != SNAPSHOT_RESTORE_PURPOSE + parts[-2]):
            return False
        try:
            os.unlink(entry.path)
        except FileNotFoundError:
            pass
        except OSError:
            # Keep the reservation and count the bytes until cleanup succeeds.
            del self.orphans[key]
            return False
        return True

    def _remove_unpublished_motion(self, folder):
        parts = folder.relative_to(self.root).parts
        if len(parts) != 4 or parts[:2] != ('harness_assets', 'objects'):
            return False
        if not folder.name.startswith(('ma_', '.writing-')):
            return False
        marker = folder/PENDING_MOTION_ASSET
        if not marker.is_file() or marker.is_symlink():
            return False
        database = self.root/'harness_assets/assets.sqlite3'
        if database.is_symlink():
            return False
        try:
            uri = database.absolute().as_uri() + '?mode=rw'
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as db:
                # Registration holds the same writer lock from staging through
                # publication. A busy database leaves cleanup for a later scan.
                db.execute('BEGIN IMMEDIATE')
                if not marker.is_file() or marker.is_symlink():
                    return False
                if db.execute('SELECT 1 FROM assets WHERE id=?', (folder.name,)).fetchone():
                    marker.unlink(missing_ok=True)
                    return False
                shutil.rmtree(folder)
                return True
        except (OSError, sqlite3.Error):
            return False

    def _remove_unpublished_snapshot(self, entry):
        manifest = SNAPSHOT_MANIFEST.fullmatch(entry.name)
        staged = entry.name.startswith(SNAPSHOT_MANIFEST_TEMP)
        if not (manifest or staged):
            return False
        parts = Path(entry.path).relative_to(self.root).parts
        if (len(parts) != 4 or parts[:2] != ('review', 'personal-assets')
                or not PERSONAL_ASSET_ACCOUNT.fullmatch(parts[2])):
            return False
        sid = manifest[1] if manifest else None
        database = self.root/'review/reviews.sqlite3'
        if database.is_symlink():
            return False
        try:
            uri = database.absolute().as_uri() + '?mode=rw'
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as db:
                registered = 'SELECT 1 FROM personal_asset_snapshots WHERE id=?'
                if db.execute(registered, (sid,)).fetchone():
                    return False
                # Publication holds the review database writer lock through
                # rename and commit. Confirm the file is still unregistered.
                db.execute('BEGIN IMMEDIATE')
                if sid is not None and db.execute(registered, (sid,)).fetchone():
                    return False
                os.unlink(entry.path)
                return True
        except FileNotFoundError:
            return True
        except (OSError, sqlite3.Error):
            return False

    def step(self, seconds=.15, max_entries=4000):
        if self.data is None:
            with closing(connect(self.root)) as db:
                row=db.execute('SELECT growth FROM capacity WHERE id=1').fetchone()
                self.start=row[0] if row else 0
                self.orphans = {key:purpose for key, pid, ident, abandoned, purpose in db.execute(
                    'SELECT id,pid,identity,abandoned,purpose FROM reservations')
                    if abandoned or worker_exited(pid, ident)}
            self.data = dict(total=0, hot=0, cold=0, exports=0, files=0)
            self.stack = [(self.root, os.scandir(self.root))]
        deadline = time.monotonic() + seconds
        count = 0
        try:
            while self.stack and count < max_entries and time.monotonic() < deadline:
                folder, entries = self.stack[-1]
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close(); self.stack.pop(); continue
                count += 1
                try:
                    if entry.is_symlink():
                        raise RuntimeError('数据目录包含符号链接，容量核对已停止')
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        if not self._remove_unpublished_motion(child):
                            self.stack.append((child, os.scandir(child)))
                        continue
                    if self._remove_abandoned_restore(entry) or self._remove_unpublished_snapshot(entry):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                self.data['total'] += size; self.data['files'] += 1
                export = 'exports' in Path(entry.path).relative_to(self.root).parts
                if entry.name.startswith('motions_') and entry.name.endswith('.pkl'):
                    self.data['hot'] += size
                if entry.name.endswith('.mf.zip') and not export:
                    self.data['cold'] += size
                if export:
                    self.data['exports'] += size
            if self.stack:
                return False
            with closing(connect(self.root)) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO capacity VALUES(1,0,0,0,0,0,0,0)')
                growth = db.execute('SELECT growth FROM capacity WHERE id=1').fetchone()[0]
                # A write after its directory was scanned may not be visible in
                # data. Add peak bounds of settlements since scan start. Double
                # counting a concurrent write is safe and fixed next quiet scan.
                total = self.data['total'] + max(0, growth-self.start)
                # Only retire orphans already quiescent before the scan began.
                # A writer that failed during this scan may have created bytes
                # in an already visited directory; keep its reservation for now.
                db.executemany('DELETE FROM reservations WHERE id=?', ((key,) for key in self.orphans))
                db.execute('UPDATE capacity SET total=?,sampled=?,hot=?,cold=?,exports=?,files=? WHERE id=1',
                           (total, time.time(), self.data['hot'], self.data['cold'], self.data['exports'], self.data['files']))
                db.commit()
            self.data = None
            return True
        except BaseException:
            self.close()
            with closing(connect(self.root)) as db, db:
                db.execute('UPDATE capacity SET sampled=0 WHERE id=1')
            raise
