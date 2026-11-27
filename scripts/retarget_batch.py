"""Finite, resumable metadata snapshot feeding the existing two-slot retarget queue."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
ROBOT = 'unitree_g1_23dof'
CONCURRENCY=int(os.environ.get('MOTIONFARM_RETARGET_BATCH_CONCURRENCY','2'))
if not 1<=CONCURRENCY<=16:raise ValueError('Batch concurrency must be 1..16')

def connect(path):
    c = sqlite3.connect(path, timeout=5); c.row_factory = sqlite3.Row
    return c

def job_id(batch, mid):
    return hashlib.sha256(('retarget-batch:'+batch+':'+mid).encode()).hexdigest()[:32]

def fingerprint(root, mid, parameters):
    from retarget_cache import identity
    context = identity(root, mid, ROBOT, {'parameters':parameters})
    if not context: raise ValueError('Cannot identify current solver')
    return {k:v for k,v in context.items() if k not in ('motion_id','source_sha256','key')}

def create(root, folder, owner):
    from g2h.farm_index import short_id
    from retarget_parameters import validate
    from review_access import active_account
    if active_account(root, owner) != 'admin': raise PermissionError('Batch requires an active administrator')
    folder.mkdir(parents=True, exist_ok=False)
    rows=[]; counts={}; started=time.time()
    with closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True)) as c:
        c.create_function('motion_id',1,short_id)
        maximum=c.execute('SELECT max(number) FROM motion_links').fetchone()[0] or 0
        c.execute('ATTACH DATABASE ? AS reviews',(str(root/'review/reviews.sqlite3'),))
        topics=[r[0] for r in c.execute('SELECT DISTINCT topic FROM motion_links').fetchall()]
        order=['walking','dancing','running','ground_get_up','standing_to_ground','squatting','crawling','floor_movements','custom']
        topics.sort(key=lambda x:order.index(x) if x in order else len(order))
        for ti,topic in enumerate(topics):
            path=root/'topics'/topic/'index.sqlite3'
            if not path.is_file():continue
            c.execute('ATTACH DATABASE ? AS source',(str(path),))
            cursor=0; sequence=0
            while True:
                page=c.execute('SELECT m.number,r.sha256 FROM motion_links m JOIN source.records r ON r.id=m.task_id '
                    'LEFT JOIN reviews.removed d ON d.motion_id=motion_id(m.number) '
                    'WHERE m.topic=? AND m.number>? AND m.number<=? AND d.motion_id IS NULL ORDER BY m.number LIMIT 1000',
                    (topic,cursor,maximum)).fetchall()
                if not page:break
                for number,sha in page:
                    mid=short_id(number)
                    rows.append((sequence,ti,mid,topic,sha));sequence+=1
                cursor=page[-1][0]
            counts[topic]=sequence;c.execute('DETACH DATABASE source')
    if not rows:raise ValueError('No generated motions in snapshot')
    rows.sort();parameters=validate({})
    manifest=dict(id=folder.name,created=started,owner=owner,robot=ROBOT,parameters=parameters,
                  snapshot_max_number=maximum,total=len(rows),topics=counts,concurrency=CONCURRENCY,
                  scope='Generated gallery motions at snapshot; excludes recycle bin and private gadget uploads.',
                  versions=fingerprint(root,rows[0][2],parameters))
    with closing(connect(folder/'batch.sqlite3')) as c,c:
        c.executescript('CREATE TABLE items(position INTEGER PRIMARY KEY,motion_id TEXT UNIQUE,topic TEXT,source_sha256 TEXT,job_id TEXT UNIQUE,status TEXT,message TEXT,updated REAL);CREATE INDEX items_status ON items(status,position);CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);')
        c.executemany('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)',[(i,mid,topic,sha,job_id(folder.name,mid),'pending','',started) for i,(_,_,mid,topic,sha) in enumerate(rows)])
        c.execute('INSERT INTO meta VALUES(?,?)',('state','ready'))
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    publish(folder,manifest,'ready','Snapshot complete')
    return manifest

def publish(folder,manifest,state,message='',blocked_reason=None):
    with closing(connect(folder/'batch.sqlite3')) as c:
        counts=dict(c.execute('SELECT status,count(*) FROM items GROUP BY status').fetchall())
        active=[dict(r) for r in c.execute("SELECT motion_id,job_id,status,message FROM items WHERE status IN ('submitted','queued','running','stopping') ORDER BY position LIMIT 16")]
    report=dict(id=manifest['id'],robot=manifest['robot'],owner=manifest['owner'],total=manifest['total'],
                state=state,message=message,blocked_reason=blocked_reason,concurrency=CONCURRENCY,counts=counts,active=active,updated=time.time(),parameters=manifest['parameters'])
    tmp=folder/'status.tmp';tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2));os.replace(tmp,folder/'status.json')
    if folder.parent.name=='batches' and folder.parent.parent.name=='retarget':
        from motion_operations import register_batch
        register_batch(folder.parents[3],folder,manifest,any(counts.get(k,0) for k in ('pending','submitted','queued','running','stopping')))
    return report

def validate_result(root,item,row,manifest):
    from retarget_cache import effective,ADAPTERS
    p=root/'review/retarget'/row['id']
    if not (p/'result.npz').is_file():return False
    m=json.loads((p/'preview.json').read_text())['metadata']
    versions=manifest['versions']
    return (m.get('motion_id')==item['motion_id'] and m.get('robot')==ROBOT
            and m.get('quality',{}).get('export_allowed') is True
            and effective(m.get('execution',{}))['parameters']==manifest['parameters']
            and m.get('engine',{}).get('sha256')==versions['code'][versions['engine']]
            and all(m.get('adapter_sources',{}).get(n)==versions['code'][n] for n in ADAPTERS)
            and m.get('robot_collision_identity',{}).get('sha256')==versions['model']['sha256'])

class Store:
    def __init__(self,root):self.root=root
    def detail(self,mid):
        from review_access import check
        from g2h.farm_index import lookup_motion
        check(self.root,mid)
        with closing(sqlite3.connect((self.root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True)) as c:
            return lookup_motion(c,self.root,mid)

def step(root,folder,manifest):
    with (folder/'step.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with closing(connect(folder/'batch.sqlite3')) as db:
            hold=db.execute("SELECT value FROM meta WHERE key='migration_hold'").fetchone()
            if hold and hold[0]=='1':return publish(folder,manifest,'paused','新版本批次等待迁移完成；不会提前提交','migration')
            row=db.execute("SELECT value FROM meta WHERE key='operator_paused'").fetchone()
        if row and row[0]=='1':
            from review_retarget import Retarget
            with closing(connect(folder/'batch.sqlite3')) as db,closing(Retarget(root).db()) as jobs,db:
                for item in db.execute("SELECT * FROM items WHERE status IN ('submitted','queued','running','stopping')").fetchall():
                    job=jobs.execute('SELECT * FROM jobs WHERE id=?',(item['job_id'],)).fetchone()
                    if not job:continue
                    status=job['status']
                    if status=='done':
                        try:valid=validate_result(root,item,job,manifest)
                        except (OSError,ValueError,KeyError):valid=False
                        if not valid:status='version_mismatch'
                    db.execute('UPDATE items SET status=?,message=?,updated=? WHERE motion_id=?',(status,job['message'],time.time(),item['motion_id']))
            return publish(folder,manifest,'paused','已暂停新提交；已进入队列和运行中的作业继续完成','operator')
        return _step(root,folder,manifest)

def _step(root,folder,manifest):
    from review_retarget import Retarget
    from review_access import active_account,Removed
    q=Retarget(root);store=Store(root)
    user=dict(username=manifest['owner'],role=active_account(root,manifest['owner']))
    if user['role']!='admin':raise PermissionError('Batch owner is no longer an administrator')
    with closing(connect(folder/'batch.sqlite3')) as c,closing(q.db()) as jobs,c:
        submitted=c.execute("SELECT * FROM items WHERE status IN ('submitted','queued','running','stopping') ORDER BY position LIMIT 16").fetchall()
        for item in submitted:
            row=jobs.execute('SELECT * FROM jobs WHERE id=?',(item['job_id'],)).fetchone()
            if row is None:continue  # A crash between journal and submit is retried below.
            status=row['status'];message=row['message']
            if status=='done':
                try:valid=validate_result(root,item,row,manifest)
                except (OSError,ValueError,KeyError):valid=False
                if not valid:status='version_mismatch';message='Completed output does not match the pinned batch version'
            c.execute('UPDATE items SET status=?,message=?,updated=? WHERE motion_id=?',(status,message,time.time(),item['motion_id']))
        c.commit()
        if c.execute("SELECT 1 FROM items WHERE status='version_mismatch' LIMIT 1").fetchone():
            return publish(folder,manifest,'paused','结果版本与批次快照不一致。旧结果保留；待活跃作业结束后可显式迁移未完成项。','version')
        first=c.execute('SELECT motion_id FROM items ORDER BY position LIMIT 1').fetchone()[0]
        if fingerprint(root,first,manifest['parameters'])!=manifest['versions']:
            return publish(folder,manifest,'paused','算法或机器人资源已变化。恢复原版本环境后可继续，或显式迁移未完成项到当前版本。','version')
        if shutil.disk_usage(root).free < 20*1024**3:
            return publish(folder,manifest,'waiting','Less than 20 GiB free; waiting without deleting results')
        active=c.execute("SELECT count(*) FROM items WHERE status IN ('submitted','queued','running','stopping')").fetchone()[0]
        candidates=c.execute("SELECT * FROM items WHERE status='submitted' OR status='pending' ORDER BY CASE WHEN status='submitted' THEN 0 ELSE 1 END,position LIMIT ?",(max(0,CONCURRENCY-active)+sum(x['status']=='submitted' for x in submitted),)).fetchall()
        for item in candidates:
            if jobs.execute('SELECT 1 FROM jobs WHERE id=?',(item['job_id'],)).fetchone():continue
            own=jobs.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running','stopping')",(user['username'],)).fetchone()[0]
            if own>=CONCURRENCY:break  # Leave the usual per-user queue limit and fairness untouched.
            try:
                info=store.detail(item['motion_id'])
                if not info.get('location') or info['location']['sha256']!=item['source_sha256']:
                    c.execute("UPDATE items SET status='source_changed',message='Source differs from snapshot',updated=? WHERE motion_id=?",(time.time(),item['motion_id']));c.commit();continue
                c.execute("UPDATE items SET status='submitted',updated=? WHERE motion_id=?",(time.time(),item['motion_id']));c.commit()
                row=q.submit(dict(id=item['job_id'],motion_id=item['motion_id'],robot=ROBOT,
                                  parameters=manifest['parameters'],force_new=True),user,store)
                c.execute('UPDATE items SET status=?,message=?,updated=? WHERE motion_id=?',(row['status'],row['message'],time.time(),item['motion_id']));c.commit()
            except Removed:
                c.execute("UPDATE items SET status='removed',message='Moved to recycle bin after snapshot',updated=? WHERE motion_id=?",(time.time(),item['motion_id']));c.commit()
            except PermissionError as exc:
                return publish(folder,manifest,'waiting',str(exc))
            except ValueError as exc:
                c.execute("UPDATE items SET status='failed',message=?,updated=? WHERE motion_id=?",(str(exc)[:500],time.time(),item['motion_id']));c.commit()
        outstanding=c.execute("SELECT count(*) FROM items WHERE status IN ('pending','submitted','queued','running','stopping')").fetchone()[0]
        failures=c.execute("SELECT count(*) FROM items WHERE status NOT IN ('done','pending','submitted','queued','running','stopping')").fetchone()[0]
    return publish(folder,manifest,'running' if outstanding else ('complete_with_failures' if failures else 'complete'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=('create','run','status'))
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--batch',required=True);parser.add_argument('--owner',default='jfc')
    args=parser.parse_args();root=args.root.resolve()
    if not args.batch.replace('-','').isalnum():raise ValueError('Invalid batch ID')
    folder=root/'review/retarget/batches'/args.batch
    if args.command=='create':print(json.dumps(create(root,folder,args.owner),ensure_ascii=False,indent=2));return
    if args.command=='status':print((folder/'status.json').read_text());return
    manifest=json.loads((folder/'manifest.json').read_text());stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    with (folder/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=None
        while not stop.is_set():
            try:report=step(root,folder,manifest)
            except Exception as exc:
                report=publish(folder,manifest,'waiting',type(exc).__name__+': '+str(exc)[:300])
            signature=(report['state'],report['counts'],report['message'])
            if signature!=previous:print(json.dumps(report,ensure_ascii=False),flush=True);previous=signature
            if report['state'].startswith('complete'):break
            stop.wait(10 if report['state']=='running' else 60)

if __name__=='__main__':main()
