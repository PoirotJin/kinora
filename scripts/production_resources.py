"""Hot-readable, bounded production priority; custom GPU turns remain unchanged."""
from contextlib import closing
import fcntl,json,os,secrets,sqlite3,time
from pathlib import Path

DEFAULT=dict(batch_size=4,backfill_variants=0)

def profile(root):
    p=Path(root)/'review/production/resource_profile.json'
    if not p.exists():return dict(DEFAULT)
    data=json.loads(p.read_text())
    if not isinstance(data,dict):raise ValueError('Invalid production resource profile')
    result=dict(DEFAULT)
    for key,allowed in [('batch_size',(2,4,8)),('backfill_variants',(0,8))]:
        value=data.get(key,result[key])
        if type(value) is not int or value not in allowed:raise ValueError('Invalid '+key)
        result[key]=value
    return result


def batch_size(root):return profile(root)['batch_size']


def replenish(root,topic,work_key,duration):
    """Add missing variants for one text-reviewed prompt only, capped at 8 total.

    Idempotent manifest, exclusive catalog lock, indexed seed counts, no model
    requests. Existing source motions/reviews/catalog entries remain immutable.
    """
    root=Path(root);limit=profile(root)['backfill_variants']
    if not limit:return 0
    if Path(work_key).name!=work_key:raise ValueError('Unsafe variant work key')
    import MotionFarm as farm
    with (root/'variant_generation.lock').open('a') as gate:
        try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        folder=root/'agent_runs'/(work_key+'_variants');manifest=folder/'manifest.json'
        if manifest.exists():
            saved=json.loads(manifest.read_text());farm.enqueue_manifest(root,manifest,saved['variant_seeds']);return len(saved['variant_seeds'])
        state_path=root/'review/production/resource_cursor.sqlite3'
        with closing(sqlite3.connect(state_path,timeout=2)) as state, closing(sqlite3.connect((root/'prompts.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            state_path.chmod(0o600)
            state.execute('CREATE TABLE IF NOT EXISTS cursors(topic TEXT,duration REAL,before_rowid INTEGER,wait_until REAL DEFAULT 0,updated REAL,PRIMARY KEY(topic,duration))')
            cursor=state.execute('SELECT before_rowid,wait_until FROM cursors WHERE topic=? AND duration=?',(topic,duration)).fetchone()
            if cursor and cursor[1]>time.time():return 0
            before=cursor[0] if cursor else None
            def save(value,wait=0):
                with state:state.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?)',(topic,duration,value,wait,time.time()))
            rows=(db.execute('SELECT rowid,id,item FROM prompts WHERE topic=? ORDER BY rowid DESC LIMIT 512',(topic,)) if before is None else db.execute('SELECT rowid,id,item FROM prompts WHERE topic=? AND rowid<? ORDER BY rowid DESC LIMIT 512',(topic,before))).fetchall()
            if not rows:
                # One complete sweep finished. A bounded new sweep will pick up
                # newer descriptions; do not spin over an exhausted catalog.
                save(None,time.time()+300);return 0
            reviews=root/'review/reviews.sqlite3'
            reviewdb=sqlite3.connect(reviews.resolve().as_uri()+'?mode=ro',uri=True) if reviews.exists() else None
            try:
                for rowid,text_id,raw in rows:
                    item=json.loads(raw)
                    if (item.get('source',{}).get('semantic_review',{}).get('verdict')!='distinct'
                        or item.get('topic_id')!=topic or item.get('duration_s')!=duration):continue
                    links=db.execute('SELECT number,seed FROM motion_links WHERE text_id=?',(text_id,)).fetchall()
                    existing={r[1] for r in links}
                    if len(existing)>=limit:continue
                    if reviewdb:
                        from g2h.farm_index import short_id
                        rejected=False
                        for number,_ in links:
                            reviewed=reviewdb.execute('SELECT status FROM latest WHERE motion_id=?',(short_id(number),)).fetchone()
                            if reviewed and reviewed[0]=='rejected':rejected=True;break
                        if rejected:continue
                    seeds=[]
                    while len(existing)+len(seeds)<limit:
                        seed=secrets.randbelow(2**32)
                        if seed not in existing and seed not in seeds:seeds.append(seed)
                    folder.mkdir(parents=True,exist_ok=True)
                    from g2h.generation_backend import current_recipe
                    data=dict(schema_version=1,generation=current_recipe(),prompts=[item],variant_seeds=seeds,
                        provenance=dict(kind='approved_prompt_variant_backfill',text_id=text_id,
                            total_variants_cap=limit,physical_quality='unverified; normal motion checks still required'))
                    temp=manifest.with_suffix('.tmp')
                    with temp.open('w') as f:json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
                    os.replace(temp,manifest)
                    farm.enqueue_manifest(root,manifest,seeds)
                    save(rowid)
                    return len(seeds)
                save(rows[-1][0])
            finally:
                if reviewdb:reviewdb.close()
    return 0
