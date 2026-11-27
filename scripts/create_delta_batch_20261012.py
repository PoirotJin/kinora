import json, sqlite3, time
from collections import Counter
from pathlib import Path
from retarget_batch import connect, job_id, fingerprint, publish, ROBOT
from retarget_parameters import validate

root=Path('/home/jfc/MotionFarm/outputs/text_motion_farm')
export=root/'exports/human-smplh-delta-20261012'
selected=json.loads((export/'selection.json').read_text())
folder=root/'review/retarget/batches/incremental-delta-20261012'
folder.mkdir(exist_ok=False)
parameters=validate({});now=time.time()
jobs=sqlite3.connect(f'file:{root}/review/retarget/jobs.sqlite3?mode=ro',uri=True)
items=[];reused=[];linked=[]
for i,(mid,task,tid,topic,seed,loc) in enumerate(selected):
    jid=job_id(folder.name,mid);status='pending';message=''
    prior=jobs.execute("SELECT id,status FROM jobs WHERE motion_id=? AND robot=? AND owner='jfc' AND status IN ('done','queued','running','stopping') ORDER BY CASE WHEN status='done' THEN 0 ELSE 1 END,created DESC LIMIT 1",(mid,ROBOT)).fetchone()
    if prior:
        old,st=prior
        if st=='done':
            p=root/'review/retarget'/old
            try:
                meta=json.loads((p/'preview.json').read_text())['metadata']
                if (p/'result.npz').is_file() and meta.get('quality',{}).get('export_allowed') is True:
                    jid=old;status='done';message='Reuse immutable existing G1 result; algorithm/parameters retained in original metadata';reused.append({'motion_id':mid,'job_id':old})
            except (OSError,KeyError,ValueError):pass
        else:
            jid=old;status=st;message='Join existing active G1 job';linked.append({'motion_id':mid,'job_id':old})
    items.append((i,mid,topic,loc[3],jid,status,message,now))
manifest=dict(id=folder.name,created=now,owner='jfc',robot=ROBOT,parameters=parameters,
              snapshot_max_number=json.loads((export/'snapshot.json').read_text())['max_motion_number'],
              total=len(items),topics=dict(Counter(x[3] for x in selected)),concurrency=16,
              scope='Exact same newly exported SMPL-H snapshot; excludes prior exported IDs and recycle bin.',
              versions=fingerprint(root,selected[0][0],parameters),reused_results=len(reused),joined_active_jobs=len(linked))
with connect(folder/'batch.sqlite3') as c:
    c.executescript('CREATE TABLE items(position INTEGER PRIMARY KEY,motion_id TEXT UNIQUE,topic TEXT,source_sha256 TEXT,job_id TEXT UNIQUE,status TEXT,message TEXT,updated REAL);CREATE INDEX items_status ON items(status,position);CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);')
    c.executemany('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)',items)
    c.execute('INSERT INTO meta VALUES(?,?)',('state','ready'))
(folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
(folder/'reused.json').write_text(json.dumps(reused,indent=2))
(folder/'joined.json').write_text(json.dumps(linked,indent=2))
print(json.dumps(publish(folder,manifest,'ready'),ensure_ascii=False))
