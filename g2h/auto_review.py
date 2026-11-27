"""Versioned machine review, separate from human decisions and source payloads.

Thresholds are frozen on historical labels, then validated on prospective,
hash-selected text groups. Unknown/uncertain labels never count as successes.
"""
from contextlib import closing
import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time


def connect(root):
    path = Path(root) / 'review/auto_review.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=3)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS scores(
        motion_id TEXT,version TEXT,topic TEXT,text_id TEXT,source_sha256 TEXT,
        created REAL,score REAL,flags TEXT,body TEXT,
        PRIMARY KEY(motion_id,version));
      CREATE INDEX IF NOT EXISTS scores_topic ON scores(version,topic,created);
      CREATE TABLE IF NOT EXISTS calibration(
        topic TEXT,version TEXT,created REAL,threshold REAL,training_groups TEXT,
        evidence TEXT,state TEXT,PRIMARY KEY(topic,version));
      CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS calibration_history(
        digest TEXT PRIMARY KEY,topic TEXT,version TEXT,at REAL,state TEXT,evidence TEXT);
    ''')
    return db


def sampled(text_id):
    return int(hashlib.sha256(('audit-v1:' + text_id).encode()).hexdigest()[:8], 16) % 5 == 0


def reviewed_at(body):
    try:
        value = str(body['updated_at']).strip()
        if value[-1:] in 'Zz':
            value = value[:-1] + '+00:00'  # Python 3.10 rejects a trailing Z
        return datetime.datetime.fromisoformat(value).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.


def wilson_upper(errors, n):
    if not n:
        return 1.
    z = 1.96
    p = errors / n
    return (p + z*z/(2*n) + z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)


def save(root, record, source_sha256, result, flags):
    score = result.get('score')
    if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1):
        raise ValueError('Invalid TMR score')
    with closing(connect(root)) as db, db:
        db.execute('INSERT OR IGNORE INTO scores VALUES(?,?,?,?,?,?,?,?,?)',
                   (record['motion_id'], result['version'], record['topic'], record['text_id'],
                    source_sha256, time.time(), score, json.dumps(flags), json.dumps(result)))


def calibrate(root, version):
    """Bounded per-topic calibration. Human DB is authoritative, never our output."""
    reviews = Path(root) / 'review/reviews.sqlite3'
    if not reviews.exists():
        return
    with closing(connect(root)) as db, closing(sqlite3.connect(reviews.resolve().as_uri()+'?mode=ro', uri=True)) as human:
        topics = [r[0] for r in db.execute('SELECT DISTINCT topic FROM scores WHERE version=? LIMIT 30', (version,))]
        for topic in topics:
            policy = db.execute('SELECT * FROM calibration WHERE topic=? AND version=?', (topic, version)).fetchone()
            if policy:
                rows=[dict(r) for r in db.execute('SELECT * FROM scores WHERE version=? AND topic=? AND created>? ORDER BY created LIMIT 5000',(version,topic,policy['created']))]
            else:
                rows = [dict(r) for r in db.execute('SELECT * FROM scores WHERE version=? AND topic=? ORDER BY created DESC LIMIT 5000', (version, topic))]
            for r in rows:
                h = human.execute('SELECT body FROM latest WHERE motion_id=?', (r['motion_id'],)).fetchone()
                r['human'] = json.loads(h[0]) if h else None
                if r['human'] and r['human'].get('motion_sha256') != r['source_sha256']:
                    r['human'] = None
            policy = db.execute('SELECT * FROM calibration WHERE topic=? AND version=?', (topic, version)).fetchone()
            if not policy:
                # One representative per prompt, preventing k variants inflating n.
                groups = {}
                for r in rows:
                    if r['score'] is not None and r['human'] and r['human']['status'] in ('accepted', 'rejected'):
                        groups.setdefault(r['text_id'], r)
                train = list(groups.values())
                if len(train) < 30 or sum(r['human']['status']=='rejected' for r in train) < 5:
                    continue
                threshold = None
                for cut in sorted({r['score'] for r in train}):
                    passing = [r for r in train if r['score'] >= cut and not json.loads(r['flags'])]
                    if len(passing) >= 10 and all(r['human']['status']=='accepted' for r in passing):
                        threshold = cut
                        break
                if threshold is None:
                    continue
                # Freeze once. Never optimize on the prospective validation set.
                with db:
                    db.execute('INSERT OR IGNORE INTO calibration VALUES(?,?,?,?,?,?,?)',
                               (topic, version, time.time(), threshold, json.dumps(list(groups)), '{}', 'validating'))
                continue
            excluded = set(json.loads(policy['training_groups']))
            candidates = {}
            # Oldest representative of each future hash-selected prompt group.
            for r in sorted(rows, key=lambda r:r['created']):
                if (r['created'] > policy['created'] and r['text_id'] not in excluded and
                    sampled(r['text_id']) and r['score'] is not None and
                    r['score'] >= policy['threshold'] and not json.loads(r['flags'])):
                    candidates.setdefault(r['text_id'], r)
            # Fixed first 75 groups; no repeated threshold search / optional
            # stopping as new validation examples arrive.
            validation = list(candidates.values())[:75]
            labelled = [r for r in validation if r['human'] and
                        reviewed_at(r['human']) > max(policy['created'], r['created'])]
            errors = sum(r['human']['status'] != 'accepted' for r in labelled)
            upper = wilson_upper(errors, len(labelled))
            # All selected groups must be reviewed: no cherry-picking easy labels.
            later = list(candidates.values())[75:]
            drift = any(r['human'] and r['human']['status']!='accepted' and
                        reviewed_at(r['human'])>r['created'] for r in later)
            ready = len(labelled) == 75 and upper <= .05 and not drift and len(rows)<5000
            evidence = dict(groups=len(labelled), selected=len(validation), errors=errors,drift_detected=drift,
                            error_upper_95=upper, max_error_upper=.05, minimum_groups=75,
                            scope='first 5000 scored motions after frozen threshold; prospective prompt groups',window_exhausted=len(rows)>=5000,
                            feedback=[dict(motion_id=r['motion_id'], revision=r['human'].get('revision'),
                                           reviewer=r['human'].get('reviewer')) for r in labelled])
            with db:
                db.execute('UPDATE calibration SET evidence=?,state=? WHERE topic=? AND version=?',
                           (json.dumps(evidence), 'eligible' if ready else 'validating', topic, version))
                snapshot=json.dumps(dict(threshold=policy['threshold'],state='eligible' if ready else 'validating',evidence=evidence),sort_keys=True)
                digest=hashlib.sha256((topic+version+snapshot).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO calibration_history VALUES(?,?,?,?,?,?)',
                           (digest,topic,version,time.time(),'eligible' if ready else 'validating',snapshot))


def verdict(row, policy):
    body = json.loads(row['body'])
    flags = json.loads(row['flags'])
    if body.get('integrity') == 'invalid':
        return 'quarantined', 'invalid_motion_data'
    if row['score'] is None:
        return 'needs_review', body.get('error', 'scoring_unavailable')
    if flags:
        return 'needs_review', 'geometry_or_duplicate_flags'
    if policy and policy['state']=='eligible' and row['score'] >= policy['threshold']:
        return 'auto_passed', 'calibrated_topic_threshold'
    return 'needs_review', 'below_threshold' if policy and row['score'] < policy['threshold'] else 'calibration_pending'


def detail(root, mid):
    path = Path(root) / 'review/auto_review.sqlite3'
    if not path.exists():
        return dict(state='pending', reason='not_scored')
    with closing(connect(root)) as db:
        active=db.execute("SELECT value FROM runtime WHERE key='version'").fetchone()
        if active:
            row=db.execute('SELECT * FROM scores WHERE motion_id=? AND version=?',(mid,active[0])).fetchone()
        else:
            row = db.execute('SELECT * FROM scores WHERE motion_id=? ORDER BY created DESC LIMIT 1', (mid,)).fetchone()
        if not row:
            return dict(state='pending', reason='not_scored')
        p = db.execute('SELECT * FROM calibration WHERE topic=? AND version=?', (row['topic'], row['version'])).fetchone()
        state, reason = verdict(row, p)
        return dict(state=state, reason=reason, score=row['score'], version=row['version'],
                    source_sha256=row['source_sha256'], flags=json.loads(row['flags']),
                    evidence=json.loads(row['body']), human_status_unchanged=True,
                    calibration={k:p[k] for k in ('state','threshold','created')} if p else None)


def overview(root):
    with closing(connect(root)) as db:
        rows = db.execute('''SELECT s.version,s.topic,count(*) n,sum(s.score IS NOT NULL) scored,
          sum(CASE WHEN json_extract(s.body,'$.integrity')='invalid' THEN 1 ELSE 0 END) quarantined,
          sum(CASE WHEN s.score IS NOT NULL AND s.flags='[]' AND p.state='eligible' AND s.score>=p.threshold THEN 1 ELSE 0 END) auto_passed
          FROM scores s LEFT JOIN calibration p ON p.version=s.version AND p.topic=s.topic
          GROUP BY s.version,s.topic LIMIT 60''').fetchall()
        policies = [dict(r) for r in db.execute('SELECT topic,version,threshold,state,evidence FROM calibration LIMIT 60')]
        runtime = dict(db.execute('SELECT key,value FROM runtime'))
        version = runtime.get('version', '')
        # Independent random sample plus flagged examples; bounded for UI.
        recent = [dict(r) for r in db.execute('SELECT motion_id,text_id,topic,score,flags FROM scores WHERE version=? ORDER BY created DESC LIMIT 500', (version,))]
        validation_queue=[]
        for p in db.execute('SELECT * FROM calibration WHERE version=? LIMIT 30',(version,)).fetchall():
            excluded=set(json.loads(p['training_groups'])); groups=set()
            for r in db.execute('SELECT * FROM scores WHERE topic=? AND version=? AND created>? ORDER BY created LIMIT 5000',
                                (p['topic'],version,p['created'])):
                if r['text_id'] in excluded or not sampled(r['text_id']) or r['score'] is None or r['score']<p['threshold'] or json.loads(r['flags']):continue
                if r['text_id'] not in groups:
                    groups.add(r['text_id'])
                    validation_queue.append(dict(r,selection='validation_sample'))
                if len(groups)>=75:break
    # Old detector versions remain in the audit database, not current counters.
    rows=[r for r in rows if not version or r['version']==version]
    policies=[p for p in policies if not version or p['version']==version]
    human_path = Path(root)/'review/reviews.sqlite3'
    done = set()
    if human_path.exists():
        with closing(sqlite3.connect(human_path.resolve().as_uri()+'?mode=ro',uri=True)) as h:
            for r in recent:
                if h.execute('SELECT 1 FROM latest WHERE motion_id=?',(r['motion_id'],)).fetchone():
                    done.add(r['motion_id'])
            for r in validation_queue:
                body=h.execute('SELECT body FROM latest WHERE motion_id=?',(r['motion_id'],)).fetchone()
                b=json.loads(body[0]) if body else {}
                if reviewed_at(b)>r['created'] and b.get('motion_sha256')==r['source_sha256']:
                    done.add(r['motion_id'])
                else:
                    done.discard(r['motion_id'])
    random_items = [dict(r,selection='random_sample') for r in recent if sampled(r['text_id']) and r['motion_id'] not in done]
    difficult = [dict(r,selection='flagged_sample') for r in recent if json.loads(r['flags']) and r['motion_id'] not in done]
    queue = []; seen=set()
    validation_queue=[r for r in validation_queue if r['motion_id'] not in done]
    for r in validation_queue[:10]+random_items[:10]+difficult[:10]:
        if r['text_id'] not in seen:
            queue.append(r); seen.add(r['text_id'])
        if len(queue)>=20:break
    return dict(topics=[dict(r) for r in rows], calibration=policies, runtime=runtime,
                queue=queue, mode='calibration_first',
                limitations='TMR is semantic similarity, not physical safety. Auto-pass is separate from human approval. Ground and robot dynamics remain unverified.')
