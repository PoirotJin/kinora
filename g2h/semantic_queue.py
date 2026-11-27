"""Durable bounded retries: failed task/motion IDs are never reused as corrections."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
from g2h.collection_store import task_id
from g2h.motion_semantics import message, VERSION

MAX_ATTEMPTS = 3


def reject(store, job, report, max_attempts=MAX_ATTEMPTS):
    spec = job['spec']; attempt = spec.get('semantic_attempt', 1)
    reason = message(report)
    folder = store.root/'quarantine'/job['id']
    folder.mkdir(parents=True, exist_ok=True)
    next_spec = None
    if attempt < max_attempts:
        next_spec = copy.deepcopy(spec)
        next_spec.update(semantic_attempt=attempt+1, semantic_parent_task=job['id'],
                         semantic_root_task=spec.get('semantic_root_task',job['id']),
                         semantic_version=VERSION,
                         inference_seed=(spec.get('inference_seed',spec['seed'])+104729) % 2**32)
        if 'farm' in next_spec:
            next_spec['farm'].pop('motion_id',None)
        identity = task_id(next_spec)
        farm = next_spec.get('farm')
        if farm:
            from g2h.farm_index import register_motion
            with closing(sqlite3.connect(store.root.parent.parent/'prompts.sqlite3', timeout=15)) as db, db:
                farm['motion_id'] = register_motion(db,farm['prompt_id'],spec['topic'],identity,spec['seed'])
    report = dict(report, task_id=job['id'], motion_id=spec.get('farm',{}).get('motion_id'),
                  attempt=attempt, max_attempts=max_attempts,
                  next_task_id=identity if next_spec else None,
                  next_motion_id=next_spec.get('farm',{}).get('motion_id') if next_spec else None)
    # Preserve output and its provenance before committing queue state. A replay
    # after a crash uses the same next ID and cannot create an unbounded chain.
    for src in (Path(job['motion']), Path(str(job['motion'])+'.meta.json')):
        if src.exists():
            dest=folder/src.name
            if not dest.exists():
                src.replace(dest)
    (folder/'semantic.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (folder/'spec.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2))
    reason += f'；第 {attempt}/{max_attempts} 次，'+('已排队自动重试' if next_spec else '重试已耗尽，候选已隔离')
    with store.db:
        if next_spec:
            store.db.execute('INSERT OR IGNORE INTO tasks(id,spec) VALUES (?,?)',(identity,json.dumps(next_spec,ensure_ascii=False)))
        store.db.execute("UPDATE tasks SET state='failed',error=? WHERE id=?",(reason,job['id']))
    return report


def batch_state(folder, seeds):
    """Latest candidate per logical output slot; old failed attempts are retained."""
    states=[]
    with closing(sqlite3.connect(Path(folder)/'index.sqlite3',timeout=5)) as db:
        for seed in seeds:
            row=db.execute("""
                SELECT candidate.state,candidate.error,candidate.spec,parent.error
                FROM tasks AS candidate
                LEFT JOIN tasks AS parent ON parent.id=json_extract(candidate.spec,'$.semantic_parent_task')
                WHERE json_extract(candidate.spec,'$.seed')=?
                ORDER BY candidate.rowid DESC LIMIT 1
            """,(seed,)).fetchone()
            if row:
                spec=json.loads(row[2])
                states.append(dict(state=row[0],reason=row[1] or '',attempt=spec.get('semantic_attempt',1),
                                   previous_reason=row[3] or ''))
    return states


def quarantine_bytes(folder, seeds):
    folder=Path(folder);total=0
    if not (folder/'index.sqlite3').exists():return 0
    with closing(sqlite3.connect(folder/'index.sqlite3',timeout=5)) as db:
        for seed in seeds:
            rows=db.execute("SELECT id FROM tasks WHERE json_extract(spec,'$.seed')=? AND state='failed' ORDER BY rowid DESC LIMIT 3",(seed,))
            for (key,) in rows:
                try:
                    for path in (folder/'quarantine'/key).iterdir():
                        try:
                            info=path.stat()
                        except FileNotFoundError:
                            continue
                        if stat.S_ISREG(info.st_mode):total+=info.st_size
                except FileNotFoundError:
                    continue
    return total
