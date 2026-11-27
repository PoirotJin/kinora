"""Per-reviewer, source-bound calibration opinions and paginated audit trails."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

CHECKS = ('semantic', 'penetration', 'sliding', 'root_gait', 'naturalness', 'ground_contact')


class CalibrationConflict(ValueError):
    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


def connect(root):
    from motion_operations import connect as operations_connect
    db = operations_connect(root)
    db.execute('CREATE INDEX IF NOT EXISTS label_reviewer_source ON labels(motion_id,source_sha256,check_name,reviewer,id)')
    return db


def _source(store, user, mid):
    from gadget_library import detail
    detail(store, mid, user)
    if mid.startswith('mimp'):
        raise ValueError('当前校准仅支持公共动作库')
    source = store.detail(mid)
    sha = (source.get('location') or {}).get('sha256')
    if not sha:
        raise ValueError('动作尚未生成')
    return source, sha


def _current(db, mid, sha=None):
    args = [mid]
    where = 'motion_id=?'
    if sha is not None:
        where += ' AND source_sha256=?'
        args.append(sha)
    rows = db.execute('''SELECT l.* FROM labels l JOIN
        (SELECT max(id) AS id FROM labels WHERE ''' + where + '''
         GROUP BY source_sha256,check_name,reviewer) x ON x.id=l.id
         ORDER BY l.id DESC''', args).fetchall()
    groups = {}
    for row in rows:
        value = dict(row)
        groups.setdefault((value['source_sha256'], value['check_name']), []).append(value)
    result = []
    for (source_sha, check), opinions in groups.items():
        verdicts = {x['verdict'] for x in opinions}
        verdict = ('disputed' if {'normal', 'defect'} <= verdicts else
                   'uncertain' if 'uncertain' in verdicts else next(iter(verdicts)))
        result.append(dict(check_name=check, source_sha256=source_sha, verdict=verdict,
                           trusted=verdict in ('normal', 'defect'), opinions=opinions))
    return result


def label(store, user, value):
    mid = value.get('motion_id', '')
    source, sha = _source(store, user, mid)
    check, verdict = value.get('check'), value.get('verdict')
    if check not in CHECKS or verdict not in ('defect', 'normal', 'uncertain'):
        raise ValueError('无效审核维度或结论')
    revision = value.get('revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('请刷新专项结论后再保存（缺少审核版本）')
    if value.get('source_sha256') != sha:
        raise CalibrationConflict('动作源版本已变化，请重新打开动作复核', dict(source_sha256=sha))
    notes = value.get('notes', '')
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValueError('专项审核备注不能超过 2000 字')
    with closing(connect(store.root)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            prior = db.execute('''SELECT * FROM labels WHERE motion_id=? AND source_sha256=?
                AND check_name=? AND reviewer=? ORDER BY id DESC LIMIT 1''',
                (mid, sha, check, user['username'])).fetchone()
            if (prior['id'] if prior else 0) != revision:
                raise CalibrationConflict('你已在其他窗口修改这一项；草稿保留，请载入最新意见后再提交', dict(prior) if prior else None)
            cursor = db.execute('''INSERT INTO labels(motion_id,source_sha256,topic,check_name,
                verdict,reviewer,created,notes) VALUES(?,?,?,?,?,?,?,?)''',
                (mid, sha, source['topic'], check, verdict, user['username'], time.time(), notes))
            event = dict(db.execute('SELECT * FROM labels WHERE id=?', (cursor.lastrowid,)).fetchone())
            db.execute('INSERT INTO events(actor,action,body,created) VALUES(?,?,?,?)',
                (user['username'], 'calibration_label', json.dumps(event), time.time()))
            current = _current(db, mid, sha)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return dict(ok=True, motion_id=mid, check=check, verdict=verdict, event=event,
                source_sha256=sha, current=current)


def evidence(root, mid, source_sha256=None):
    with closing(connect(root)) as db:
        rows = db.execute('SELECT * FROM labels WHERE motion_id=? ORDER BY id DESC LIMIT 41', (mid,)).fetchall()
        matches = db.execute('SELECT other_id,distance,version FROM similar WHERE motion_id=? ORDER BY distance LIMIT 5', (mid,)).fetchall()
        current = _current(db, mid, source_sha256)
    return dict(labels=[dict(r) for r in rows[:40]], next_after=rows[39]['id'] if len(rows) > 40 else None,
                current=current, source_sha256=source_sha256, similar=[dict(r) for r in matches],
                scope='各人保留最新意见；不同审核员意见冲突时标记有争议，不计入可信校准样本。')


def history(store, user, value):
    def first(key, default=None):
        item = value.get(key, default)
        return item[0] if isinstance(item, list) else item
    mid = first('motion_id', '')
    _, sha = _source(store, user, mid)
    after = int(first('after', '0') or 0)
    limit = int(first('limit', '20'))
    if after < 0 or not 1 <= limit <= 100:
        raise ValueError('无效历史分页参数')
    with closing(connect(store.root)) as db:
        rows = db.execute('SELECT * FROM labels WHERE motion_id=?' + (' AND id<?' if after else '') +
                          ' ORDER BY id DESC LIMIT ?', ([mid, after, limit + 1] if after else [mid, limit + 1])).fetchall()
        current = _current(db, mid, sha)
    return dict(motion_id=mid, source_sha256=sha, labels=[dict(r) for r in rows[:limit]],
                next_after=rows[limit - 1]['id'] if len(rows) > limit else None, current=current)


def review_history(store, mid, after=None, limit=20, user=None):
    if user is not None:
        from gadget_library import detail
        detail(store, mid, user)
    limit = int(limit)
    after = int(after or 0)
    if not 1 <= limit <= 100 or after < 0:
        raise ValueError('无效审核历史分页参数')
    with closing(store.reviews()) as db:
        rows = db.execute('SELECT revision,body FROM events WHERE motion_id=?' +
                          (' AND revision<?' if after else '') + ' ORDER BY revision DESC LIMIT ?',
                          ([mid, after, limit + 1] if after else [mid, limit + 1])).fetchall()
    return dict(items=[json.loads(r[1]) for r in rows[:limit]],
                next_after=rows[limit - 1][0] if len(rows) > limit else None)


def calibration_summary(root):
    # Bounded maintenance: select complete opinions for at most 5000 recent motion/source/check groups.
    with closing(connect(root)) as db:
        keys = db.execute('''SELECT motion_id,source_sha256,check_name FROM labels
            GROUP BY motion_id,source_sha256,check_name ORDER BY max(id) DESC LIMIT 5001''').fetchall()
        states = []
        for key in keys[:5000]:
            states.extend(x for x in _current(db, key['motion_id'], key['source_sha256']) if x['check_name'] == key['check_name'])
    flags = {'penetration':'possible_self_penetration', 'sliding':'possible_stance_sliding', 'root_gait':'possible_root_gait_mismatch'}
    machine = Path(root) / 'review/auto_review.sqlite3'
    groups = {}
    with closing(sqlite3.connect(machine.resolve().as_uri() + '?mode=ro', uri=True)) if machine.exists() else closing(sqlite3.connect(':memory:')) as scores:
        version = scores.execute("SELECT value FROM runtime WHERE key='version'").fetchone() if machine.exists() else None
        for state in states:
            row = state['opinions'][0]
            key = (row['topic'], row['check_name'], state['verdict'])
            group = groups.setdefault(key, dict(topic=key[0], check_name=key[1], verdict=key[2], n=0, scored=0, machine_flagged=0, trusted=0))
            group['n'] += 1
            if version and row['check_name'] in flags:
                scored = scores.execute('SELECT source_sha256,flags FROM scores WHERE motion_id=? AND version=?', (row['motion_id'], version[0])).fetchone()
                if scored and scored[0] == row['source_sha256']:
                    group['scored'] += 1
                    # Disagreement/uncertainty is visible but excluded from reliable calibration counts.
                    if state['trusted']:
                        group['trusted'] += 1
                        group['machine_flagged'] += int(flags[row['check_name']] in json.loads(scored[1]))
    return dict(groups=list(groups.values()), limit=5000, truncated=len(keys)>5000,
                policy='按每人当前源版本的最新意见合并；正常与缺陷并存为有争议，无法确定和争议均不计入可信校准。仅匹配当前检测版本和源校验值；不会因此自动放宽阈值。')
