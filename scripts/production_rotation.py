"""Daily bounded stochastic resampling of reviewed descriptions, not new semantics."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


def calendar(now=None):
    now=time.time() if now is None else now
    local=time.localtime(now)
    start=time.mktime((local.tm_year,local.tm_mon,local.tm_mday,0,0,0,0,0,-1))
    return time.strftime('%Y-%m-%d',local),time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(start))


def replenish(root,topic,work_key,duration,now=None):
    from production_supply import policy
    rules=policy(root)
    if not rules.get('rolling_variants',False):return 0
    root=Path(root);cap=rules['daily_variants_per_description']
    if Path(work_key).name!=work_key:raise ValueError('Unsafe rotation work key')
    day,since=calendar(now)
    import MotionFarm as farm
    with (root/'variant_generation.lock').open('a') as gate:
        try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        manifest=root/'agent_runs'/(work_key+'_rolling')/'manifest.json'
        if manifest.exists():
            saved=json.loads(manifest.read_text())
            # Yesterday's interrupted work must not generate extra samples today.
            if saved['provenance']['day']!=day:return 0
            farm.enqueue_manifest(root,manifest,saved['variant_seeds']);return len(saved['variant_seeds'])
        with closing(sqlite3.connect(root/'review/production/resource_cursor.sqlite3',timeout=2)) as state, closing(sqlite3.connect((root/'prompts.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            state.execute('CREATE TABLE IF NOT EXISTS rotation(topic TEXT,duration REAL,day TEXT,before_rowid INTEGER,PRIMARY KEY(topic,duration))')
            cursor=state.execute('SELECT day,before_rowid FROM rotation WHERE topic=? AND duration=?',(topic,duration)).fetchone()
            before=cursor[1] if cursor and cursor[0]==day else None
            def save(value):
                with state:state.execute('INSERT OR REPLACE INTO rotation VALUES(?,?,?,?)',(topic,duration,day,value))
            queue=root/'topics'/topic/'index.sqlite3'
            if not queue.exists():return 0
            db.execute('ATTACH DATABASE ? AS queue',(queue.resolve().as_uri()+'?mode=ro',))
            review=root/'review/reviews.sqlite3';has_review=review.exists()
            if has_review:
                from g2h.farm_index import short_id
                db.create_function('motion_short_id',1,short_id,deterministic=True)
                db.execute('ATTACH DATABASE ? AS reviews',(review.resolve().as_uri()+'?mode=ro',))
            rows=(db.execute('SELECT rowid,id,item FROM prompts WHERE topic=? ORDER BY rowid DESC LIMIT 512',(topic,)) if before is None else db.execute('SELECT rowid,id,item FROM prompts WHERE topic=? AND rowid<? ORDER BY rowid DESC LIMIT 512',(topic,before))).fetchall()
            if not rows:save(None);return 0
            for rowid,text_id,raw in rows:
                item=json.loads(raw)
                if item.get('duration_s')!=duration or item.get('source',{}).get('semantic_review',{}).get('verdict')!='distinct':continue
                if has_review and db.execute("SELECT 1 FROM motion_links l JOIN reviews.latest r ON r.motion_id=motion_short_id(l.number) WHERE l.text_id=? AND r.status='rejected' LIMIT 1",(text_id,)).fetchone():continue
                # Includes fresh prompt, historical backfill, failures and pending
                # reservations today. The queue timestamps are UTC, not local dates.
                total,used=db.execute('SELECT count(*),coalesce(sum(t.created>=?),0) FROM motion_links l JOIN queue.tasks t ON t.id=l.task_id WHERE l.text_id=?',(since,text_id)).fetchone()
                # A new prompt can be catalogued before its original queue
                # transaction commits. Never race that first enqueue of 8 seeds.
                if not total or used>=cap:continue
                seeds=[]
                while len(seeds)<cap-used:
                    seed=secrets.randbelow(2**32)
                    if seed in seeds or db.execute('SELECT 1 FROM motion_links WHERE text_id=? AND seed=? LIMIT 1',(text_id,seed)).fetchone():continue
                    seeds.append(seed)
                # Do not mutate the catalog item. Each output retains a separate
                # explicit provenance marker and its original semantic review.
                origin=dict(kind='reviewed_description_random_variant',day=day,text_id=text_id,
                            daily_description_cap=cap,new_semantic_description=False,
                            physical_quality='unverified; normal motion checks required')
                item={**item,'source':{**item['source'],'production_variant':origin}}
                from g2h.generation_backend import current_recipe
                data=dict(schema_version=1,generation=current_recipe(),prompts=[item],variant_seeds=seeds,provenance=origin)
                manifest.parent.mkdir(parents=True,exist_ok=True)
                temp=manifest.with_suffix('.tmp')
                with temp.open('w') as f:json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
                os.replace(temp,manifest)
                farm.enqueue_manifest(root,manifest,seeds);save(rowid)
                return len(seeds)
            save(rows[-1][0])
    return 0
