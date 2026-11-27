"""Small relational indexes. Motion arrays remain in the original PKL shards."""
import hashlib
import json
from pathlib import Path
import pickle
import sqlite3
from g2h.cold_storage import archive_path, read_payload


def initialize(db):
    db.executescript("""
        CREATE INDEX IF NOT EXISTS prompts_topic ON prompts(topic);
        CREATE TABLE IF NOT EXISTS topic_counts(topic TEXT PRIMARY KEY, total INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS family_counts(
          topic TEXT, family TEXT, total INTEGER NOT NULL, example_id TEXT NOT NULL,
          PRIMARY KEY(topic,family));
        CREATE INDEX IF NOT EXISTS family_rank ON family_counts(topic,total DESC);
        CREATE TABLE IF NOT EXISTS motion_links(
          number INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL,
          text_id TEXT NOT NULL REFERENCES prompts(id), topic TEXT NOT NULL, seed INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS motions_text ON motion_links(text_id,number);
        CREATE INDEX IF NOT EXISTS motions_topic ON motion_links(topic,number);
        CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS prompt_added AFTER INSERT ON prompts BEGIN
          INSERT INTO topic_counts VALUES(new.topic,1)
            ON CONFLICT(topic) DO UPDATE SET total=total+1;
          INSERT INTO family_counts VALUES(new.topic,
            COALESCE(json_extract(new.item,'$.source.semantic_review.family'),'unreviewed'),1,new.id)
            ON CONFLICT(topic,family) DO UPDATE SET total=total+1;
        END;
    """)
    # One-time migration; normal rounds use indexed counters and bounded SELECTs.
    if not db.execute("SELECT 1 FROM index_meta WHERE key='summary_v1'").fetchone():
        with db:
            db.execute("DELETE FROM topic_counts")
            db.execute("DELETE FROM family_counts")
            db.execute("INSERT INTO topic_counts SELECT topic,COUNT(*) FROM prompts GROUP BY topic")
            db.execute("""INSERT INTO family_counts SELECT topic,
                COALESCE(json_extract(item,'$.source.semantic_review.family'),'unreviewed'),
                COUNT(*),MIN(id) FROM prompts GROUP BY 1,2""")
            db.execute("INSERT INTO index_meta VALUES('summary_v1','1')")


def short_id(number):
    alphabet, value = '0123456789abcdefghijklmnopqrstuvwxyz', ''
    while number:
        number, digit = divmod(number, 36)
        value = alphabet[digit] + value
    return 'm' + value.rjust(8, '0')


def motion_number(value):
    if not isinstance(value, str) or not value.startswith('m') or len(value) < 9:
        raise ValueError('Invalid motion ID')
    try:
        number = int(value[1:], 36)
    except ValueError:
        raise ValueError('Invalid motion ID') from None
    if number < 1 or short_id(number) != value:
        raise ValueError('Invalid motion ID')
    return number


def register_motion(db, text_id, topic, task_id, seed):
    if not db.execute('SELECT 1 FROM prompts WHERE id=? AND topic=?', (text_id, topic)).fetchone():
        raise ValueError('Motion refers to a missing text or mismatched topic')
    with db:
        db.execute('INSERT OR IGNORE INTO motion_links(task_id,text_id,topic,seed) VALUES(?,?,?,?)',
                   (task_id, text_id, topic, seed))
    row = db.execute('SELECT number,text_id,topic,seed FROM motion_links WHERE task_id=?', (task_id,)).fetchone()
    if row[1:] != (text_id, topic, seed):
        raise ValueError('Conflicting motion identity')
    return short_id(row[0])


def history_snapshot(db, topics, limit):
    result = {}
    for topic in topics:
        key = topic['id']
        row = db.execute('SELECT total FROM topic_counts WHERE topic=?', (key,)).fetchone()
        total = row[0] if row else 0
        families = db.execute('SELECT family,total,example_id FROM family_counts WHERE topic=? '
                              'ORDER BY total DESC,family LIMIT 60', (key,)).fetchall()
        count = limit if total <= limit else max(1, limit // 2)
        rows = db.execute('SELECT item FROM prompts WHERE topic=? ORDER BY rowid DESC LIMIT ?', (key, count)).fetchall()
        examples, seen = [], set()
        for (raw,) in rows:
            item = json.loads(raw)
            examples.append({k: item[k] for k in ('id', 'text', 'attributes')})
            seen.add(item['id'])
        for _, _, example_id in families:
            if len(examples) >= limit:
                break
            if example_id not in seen:
                item = json.loads(db.execute('SELECT item FROM prompts WHERE id=?', (example_id,)).fetchone()[0])
                examples.append({k: item[k] for k in ('id', 'text', 'attributes')})
                seen.add(example_id)
        result[key] = dict(total_saved_prompts=total, family_counts={f: n for f,n,_ in families},
            other_family_count=total-sum(n for _,n,_ in families),
            family_count_note='Model-assigned text families, not verified motion classes; labels may overlap.',
            history_is_sampled=len(examples)<total, examples=examples)
    return result


def motion_info(root, row):
    number, task, text_id, topic, seed = row
    result = dict(motion_id=short_id(number), task_id=task, text_id=text_id, topic=topic, seed=seed,
                  state='not_enqueued', location=None)
    path = Path(root) / 'topics' / topic / 'index.sqlite3'
    if not path.exists():
        return result
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        state = db.execute('SELECT state,error,spec FROM tasks WHERE id=?', (task,)).fetchone()
        record = db.execute('SELECT shard,offset,bytes,sha256 FROM records WHERE id=?', (task,)).fetchone()
        if state:
            result.update(state=state[0], error=state[1])
            variant=json.loads(state[2]).get('farm',{}).get('source',{}).get('production_variant')
            if isinstance(variant,dict):result['production_variant']=variant
        if record:
            shard, offset, size, digest = record
            result['location'] = dict(file=str(path.parent / ('motions_%06d.pkl' % shard)),
                                      offset=offset, bytes=size, sha256=digest,shard=shard)
            if archive_path(path.parent,shard).exists():
                result['storage_tier']='cold'
                result['location'].update(file=str(archive_path(path.parent,shard)),member='motions/'+task+'.pkl')
            else: result['storage_tier']='hot'
    finally:
        db.close()
    return result


def lookup_motion(db, root, motion_id):
    row = db.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links WHERE number=?',
                     (motion_number(motion_id),)).fetchone()
    if not row:
        raise ValueError('Unknown motion ID')
    return motion_info(root, row)


def read_motion(db, root, motion_id):
    """Direct seek into a trusted local shard; no scan of other motions."""
    info = lookup_motion(db, root, motion_id)
    location = info['location']
    if location is None:
        raise ValueError('Motion is not committed yet')
    payload=read_payload(Path(root)/'topics'/info['topic'],location['shard'],location['offset'],location['bytes'],location['sha256'],info['task_id'])
    try:
        record = pickle.loads(payload)
    except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, IndexError, ValueError) as exc:
        raise ValueError('无法解析已保存的源动作记录') from exc
    if record['id'] != info['task_id']:
        raise ValueError('Motion ID mismatch')
    return {**record, 'motion_id': motion_id, 'text_id': info['text_id']}


def sync_legacy(db, root, topics, limit=1000):
    """Incremental metadata-only migration; never deserialize motion arrays."""
    added = 0
    for topic in topics:
        path = Path(root) / 'topics' / topic['id'] / 'index.sqlite3'
        if not path.exists():
            continue
        meta = 'queue_cursor:' + topic['id']
        cursor = db.execute('SELECT value FROM index_meta WHERE key=?', (meta,)).fetchone()
        queue = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            rows = queue.execute('SELECT rowid,id,spec FROM tasks WHERE rowid>? ORDER BY rowid LIMIT ?',
                                  (int(cursor[0]) if cursor else 0, limit)).fetchall()
        finally:
            queue.close()
        for seq, task, raw in rows:
            spec = json.loads(raw)
            text_id = spec.get('farm', {}).get('prompt_id')
            if not text_id:
                raise ValueError('Legacy task lacks prompt_id; explicit import required: ' + task)
            register_motion(db, text_id, topic['id'], task, spec['seed'])
            with db:
                db.execute('INSERT OR REPLACE INTO index_meta VALUES(?,?)', (meta, str(seq)))
            added += 1
    return added
