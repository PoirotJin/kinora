"""Crash-recoverable, size-limited streams of independently pickled records.

Read with iter_records(), not a single pickle.load(). Only load trusted files.
One collector owns the advisory lock; enqueue operations use SQLite separately.
"""
import fcntl
import hashlib
import json
import os
import pickle
import sqlite3
from pathlib import Path
from g2h.cold_storage import initialize as initialize_cold, archive_path, read_payload, guard


def connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "index.sqlite3", timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            error TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS shards (
            number INTEGER PRIMARY KEY, bytes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
            id TEXT PRIMARY KEY, shard INTEGER NOT NULL, offset INTEGER NOT NULL,
            bytes INTEGER NOT NULL, sha256 TEXT NOT NULL);
    """)
    initialize_cold(db)
    from g2h.db_stats import collection
    collection(db)
    return db


def task_id(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class CollectionStore:
    def __init__(self, root, max_bytes=1_000_000_000):
        self.root = Path(root)
        self.db = connect(root)
        self.lock = (self.root / "writer.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.db.close()
            raise RuntimeError("Another collector owns this database") from None
        self.max_bytes = max_bytes
        if max_bytes < 1:
            self.close()
            raise ValueError("max_bytes must be positive")
        # Discard bytes written before a crash but not committed in SQLite.
        for number, committed in self.db.execute("SELECT number, bytes FROM shards"):
            path = self.path(number)
            if self.db.execute('SELECT 1 FROM cold_shards WHERE number=?',(number,)).fetchone():
                if not archive_path(self.root,number).exists():
                    self.close();raise RuntimeError('Missing cold archive')
                continue
            if not path.exists() and committed:
                self.close()
                raise RuntimeError(f"Missing committed shard: {path}")
            if path.exists():
                if path.stat().st_size < committed:
                    self.close()
                    raise RuntimeError(f"Truncated committed shard: {path}")
                if path.stat().st_size > committed:
                    with path.open("r+b") as f:
                        f.truncate(committed)
                        f.flush()
                        os.fsync(f.fileno())

    def path(self, number):
        return self.root / f"motions_{number:06d}.pkl"

    def contains(self, key):
        return self.db.execute("SELECT 1 FROM records WHERE id=?", (key,)).fetchone() is not None

    def append(self, record):
        key = record["id"]
        if self.contains(key):
            return False
        payload = pickle.dumps(record, protocol=5)
        if len(payload) > self.max_bytes:
            raise ValueError(f"Single record exceeds shard limit: {len(payload)} > {self.max_bytes}")
        farm_root=self.root.parent.parent
        if (farm_root/'prompts.sqlite3').exists():
            from g2h.capacity import reservation
            # Include a conservative SQL/WAL metadata allowance. Reconciliation
            # replaces the allowance with measured bytes without a per-write walk.
            with reservation(farm_root,len(payload)+65536,'motion:'+key) as budget:
                result=self._append_payload(key,payload)
                budget.commit(len(payload)+65536)
                return result
        return self._append_payload(key,payload)

    def _append_payload(self,key,payload):
        last = self.db.execute("SELECT number, bytes FROM shards ORDER BY number DESC LIMIT 1").fetchone()
        number, offset = last if last else (0, 0)
        if last and self.db.execute('SELECT 1 FROM cold_shards WHERE number=?',(number,)).fetchone():
            number,offset=number+1,0
        if offset + len(payload) > self.max_bytes:
            number, offset = number + 1, 0
        # Register the new shard before touching its file, enabling crash recovery.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO shards VALUES (?, 0)", (number,))
        with self.path(number).open("ab") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            with self.db:
                self.db.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                                (key, number, offset, len(payload), hashlib.sha256(payload).hexdigest()))
                self.db.execute("UPDATE shards SET bytes=? WHERE number=?", (offset + len(payload), number))
                self.db.execute("UPDATE tasks SET state='done', error=NULL WHERE id=?", (key,))
        except BaseException:
            with self.path(number).open("r+b") as f:
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
            raise
        return True

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def iter_records(path):
    """Iterate a finished/trusted shard. Corrupt partial records raise an error."""
    with Path(path).open("rb") as f:
        while True:
            if not f.read(1):
                return
            f.seek(-1, 1)
            yield pickle.load(f)


def iter_committed(root):
    """Read a consistent index snapshot, including from a running collector."""
    db = connect(root)
    try:
        rows = db.execute("SELECT id, shard, offset, bytes, sha256 FROM records ORDER BY shard, offset").fetchall()
    finally:
        db.close()
    for key, shard, offset, size, digest in rows:
        data=read_payload(root,shard,offset,size,digest,key)
        yield pickle.loads(data)
