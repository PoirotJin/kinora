"""Read-only, immutable search snapshots; SQLite commits invalidate cached data.

Only shared catalog metadata is cached. Account/author/review filters are read
fresh by the search request, and no job or private import data enters this index.
"""
from collections import OrderedDict
from contextlib import closing
import atexit
import json
import itertools
from pathlib import Path
import sqlite3
import threading

import numpy as np

_LOCK = threading.RLock()
_INDEXES = OrderedDict()
_SERIAL = itertools.count(1)


class Watch:
    def __init__(self, path):
        self.path = Path(path)
        self.db = None
        self.identity = None
        self.revision = None

    def changed(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            identity = None
        replaced = identity != self.identity
        if replaced:
            self.close()
            self.identity = identity
        if identity is None:
            return replaced
        if self.db is None:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=5, check_same_thread=False)
        revision = self.db.execute('PRAGMA data_version').fetchone()[0]
        changed = replaced or revision != self.revision
        self.revision = revision
        return changed

    def close(self):
        if self.db is not None:
            self.db.close()
        self.db = None
        self.revision = None


def frozen(values, dtype=None):
    array = np.asarray(values, dtype=dtype)
    array.flags.writeable = False
    return array


class Index:
    def __init__(self, root):
        self.root = Path(root)
        self.catalog = Watch(self.root / 'prompts.sqlite3')
        self.topics = {}
        self.records = {}
        self.snapshot = None
        self.serial = 0

    def close(self):
        self.catalog.close()
        for watch in self.topics.values():
            watch.close()

    def get(self):
        changed = self.catalog.changed()
        snapshot = self.snapshot
        if changed or snapshot is None:
            db = self.catalog.db
            # One read transaction keeps prompt and motion mappings consistent.
            db.execute('BEGIN')
            try:
                prompts = json.loads(db.execute("SELECT json_group_array(json_array(id,"
                    "json_extract(item,'$.text'),json_extract(item,'$.duration_s'))) FROM prompts").fetchone()[0])
                motions = json.loads(db.execute('SELECT json_group_array(json_array(number,task_id,text_id,topic)) '
                    'FROM (SELECT number,task_id,text_id,topic FROM motion_links ORDER BY number)').fetchone()[0])
            finally:
                db.rollback()
            positions = {row[0]: i for i, row in enumerate(prompts)}
            motions = [row for row in motions if row[2] in positions]
            from g2h.farm_index import short_id
            ids = [short_id(row[0]) for row in motions]
            snapshot = dict(
                prompts=tuple(tuple(row) for row in prompts),
                motions=tuple(tuple(row) for row in motions),
                numbers=frozen([r[0] for r in motions], np.int64),
                prompt_index=frozen([positions[r[2]] for r in motions], np.int32),
                topic=frozen([r[3] for r in motions]),
                ids=frozen(ids), bare_ids=frozen([v[1:] for v in ids]),
                trimmed_ids=frozen([v[1:].lstrip('0') for v in ids]),
                durations=frozen([float(r[2]) if r[2] is not None else np.nan for r in prompts], np.float64),
            )
            from library_directions import prepare
            snapshot['text_directions'] = prepare(snapshot['prompts'])
        used = {row[3] for row in snapshot['motions']}
        for topic in used:
            watch = self.topics.setdefault(topic, Watch(self.root / 'topics' / topic / 'index.sqlite3'))
            if watch.changed() or topic not in self.records:
                records = set(json.loads(watch.db.execute(
                    'SELECT json_group_array(id) FROM records').fetchone()[0])) if watch.db else set()
                if records != self.records.get(topic):
                    changed = True
                self.records[topic] = records
        for topic in set(self.topics) - used:
            self.topics.pop(topic).close()
            self.records.pop(topic, None)
        if changed or self.snapshot is None:
            snapshot = dict(snapshot)
            snapshot['generated'] = frozen([r[1] in self.records[r[3]] for r in snapshot['motions']], bool)
            self.serial = next(_SERIAL)
            snapshot['serial'] = self.serial
            self.snapshot = snapshot
        return self.snapshot


def snapshot(store):
    key = str(Path(store.root).resolve())
    with _LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = _INDEXES[key] = Index(key)
        _INDEXES.move_to_end(key)
        while len(_INDEXES) > 2:
            _, old = _INDEXES.popitem(last=False)
            old.close()
        try:
            return index.get()
        except Exception:
            # Watch revisions can advance before all snapshot reads succeed.
            _INDEXES.pop(key)
            index.close()
            raise


def clear():
    with _LOCK:
        for index in _INDEXES.values():
            index.close()
        _INDEXES.clear()


atexit.register(clear)


def filters(store, data, params):
    """Fresh review/ownership state, including same-count updates and removals."""
    from g2h.farm_index import motion_number
    from g2h import direction_index
    with closing(store.reviews()) as db:
        db.execute('BEGIN')
        state = tuple(tuple(db.execute('SELECT motion_id' + (',' + field if field else '') +
            ' FROM ' + table + ' ORDER BY motion_id').fetchall()) for table, field in
            [('latest', 'status'), ('motion_owners', 'username'), ('removed', '')])
    def numbers(rows):
        result = []
        for row in rows:
            try:
                n = motion_number(row[0])
                if n <= 2**63-1:
                    result.append(n)
            except ValueError:
                pass
        return result
    latest, owners, removed = state
    mask = data['generated'].copy()
    mask &= ~np.isin(data['numbers'], numbers(removed))
    if params['topic']:
        mask &= data['topic'] == params['topic']
    if params['author']:
        mask &= np.isin(data['numbers'], numbers([r for r in owners if r[1] == params['author']]))
    status = params['status']
    direction = ()
    if status in direction_index.FILTERS:
        path = direction_index.path(store.root)
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
                sql = 'SELECT json_group_array(number) FROM results WHERE version=?'
                args = [direction_index.VERSION]
                if status != 'direction_not_checked':
                    sql += ' AND status=?'; args.append(direction_index.FILTERS[status])
                direction = tuple(json.loads(db.execute(sql, args).fetchone()[0]))
        checked = np.isin(data['numbers'], direction)
        mask &= ~checked if status == 'direction_not_checked' else checked
    elif status == 'pending':
        mask &= ~np.isin(data['numbers'], numbers(latest))
    elif status:
        mask &= np.isin(data['numbers'], numbers([r for r in latest if r[1] == status]))
    return mask, (state, direction)
