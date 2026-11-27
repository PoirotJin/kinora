"""Bounded motion evidence, immutable releases and operator controls.

Similarity and body screening are proxies: never delete or auto-pass from them.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

DESCRIPTOR_VERSION='resampled-root-relative-v1'
CHECKS=('semantic','penetration','sliding','root_gait')

def connect(root):
    db=sqlite3.connect(Path(root)/'review/operations.sqlite3',timeout=5)
    db.row_factory=sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY,motion_id TEXT,source_sha256 TEXT,topic TEXT,check_name TEXT,verdict TEXT,reviewer TEXT,created REAL,notes TEXT);
    CREATE INDEX IF NOT EXISTS label_motion ON labels(motion_id,check_name,id);
    CREATE TABLE IF NOT EXISTS descriptors(motion_id TEXT PRIMARY KEY,source_sha256 TEXT,topic TEXT,version TEXT,signature TEXT,vector TEXT,duration REAL,distance REAL,speed REAL,created REAL);
    CREATE INDEX IF NOT EXISTS descriptor_topic ON descriptors(topic,created);
    CREATE INDEX IF NOT EXISTS descriptor_hash ON descriptors(signature);
    CREATE TABLE IF NOT EXISTS similar(motion_id TEXT,other_id TEXT,distance REAL,version TEXT,created REAL,PRIMARY KEY(motion_id,other_id));
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,body TEXT,created REAL);
    CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY,status TEXT,body TEXT,created REAL,error TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS batch_catalog(id TEXT PRIMARY KEY,created REAL,owner TEXT,active INTEGER DEFAULT 1,checked REAL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS batch_scheduling ON batch_catalog(active,checked);
    ''')
    if 'checked' not in {r[1] for r in db.execute('PRAGMA table_info(releases)')}:
        try:db.execute('ALTER TABLE releases ADD COLUMN checked REAL DEFAULT 0');db.commit()
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc):raise
    return db

def event(db,user,action,body):
    db.execute('INSERT INTO events(actor,action,body,created) VALUES(?,?,?,?)',(user['username'],action,json.dumps(body),time.time()))

def label(store,user,value):
    from review_calibration import label as save_label
    return save_label(store,user,value)

def evidence(root,mid,source_sha256=None):
    from review_calibration import evidence as read_evidence
    return read_evidence(root,mid,source_sha256)

def quality_levels(info,quality):
    machine=info.get('machine_review') or {}
    return [dict(level='生成',status='完成' if info.get('state')=='generated' or info.get('location') else '未确认'),
            dict(level='语义匹配',status=machine.get('state','待校准'),note='TMR 相似度不是通过概率'),
            dict(level='人体几何',status='需复核' if any(v is False for v in quality.get('checks',{}).values()) else ('代理检查完成' if quality.get('checks') else '未检查'),note='骨架体积代理，不等于精确表面验证'),
            dict(level='机器人运动学',status='查看具体机器人重定向版本'),
            dict(level='动力学 / 可跟踪性',status='未验证')]

def descriptor(record):
    import numpy as np
    m=record['motion'];p=np.asarray(m['native_keypoints3d'],dtype=float);tr=np.asarray(m['native_transl'],dtype=float);fps=float(m['fps'])
    if p.ndim!=3 or p.shape[1:]!=(52,3) or len(p)<2 or tr.shape!=(len(p),3) or not np.isfinite(p).all() or not np.isfinite(tr).all():raise ValueError('Invalid motion')
    idx=np.linspace(0,len(p)-1,16).astype(int)
    # Keep heading and root trajectory: do not merge opposite-direction motions.
    body=(p-p[:,:1])[idx,:22]
    root=tr[idx]-tr[0]
    vector=np.concatenate((body.reshape(-1),root.reshape(-1)))
    distance=float(np.linalg.norm(np.diff(tr[:,[0,2]],axis=0),axis=1).sum())
    duration=len(p)/fps
    signature=hashlib.sha256(np.round(vector,3).astype('<f4').tobytes()+str(round(duration,2)).encode()).hexdigest()
    return dict(vector=vector.tolist(),signature=signature,duration=duration,distance=distance,speed=distance/duration)

def save_descriptor(root,record,sha):
    import numpy as np
    d=descriptor(record);mid=record['motion_id'];topic=record['topic']
    with closing(connect(root)) as db,db:
        # Bounded approximate neighbours, explicitly not an exhaustive global search.
        rows=db.execute('SELECT motion_id,vector,duration FROM descriptors WHERE topic=? AND version=? AND motion_id!=? ORDER BY created DESC LIMIT 256',(topic,DESCRIPTOR_VERSION,mid)).fetchall()
        matches=[];v=np.asarray(d['vector'])
        for row in rows:
            if abs(row['duration']-d['duration'])>0.5:continue
            distance=float(np.sqrt(np.mean((v-np.asarray(json.loads(row['vector'])))**2)))
            if distance<.045:matches.append((distance,row['motion_id']))
        db.execute('INSERT OR REPLACE INTO descriptors VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,sha,topic,DESCRIPTOR_VERSION,d['signature'],json.dumps(d['vector']),d['duration'],d['distance'],d['speed'],time.time()))
        db.execute('DELETE FROM similar WHERE motion_id=?',(mid,))
        for distance,other in sorted(matches)[:5]:db.execute('INSERT OR REPLACE INTO similar VALUES(?,?,?,?,?)',(mid,other,distance,DESCRIPTOR_VERSION,time.time()))
    return d

def coverage(root):
    with closing(connect(root)) as db:
        rows=db.execute('SELECT topic,count(*) AS sampled,avg(duration) AS duration_s,avg(speed) AS speed_mps,sum(distance<0.25) AS stationary,sum(speed>=0.25 AND speed<1.5) AS moderate,sum(speed>=1.5) AS fast FROM descriptors GROUP BY topic').fetchall()
    return [dict(r) for r in rows]

def coverage_cue(root,topic):
    with closing(connect(root)) as db:
        rows=db.execute('SELECT speed,distance,duration FROM descriptors WHERE topic=? ORDER BY created DESC LIMIT 256',(topic,)).fetchall()
    if not rows:return dict(scope='No measured motion coverage yet; follow topic definition.')
    bins={'stationary':0,'slow_travel':0,'moderate_travel':0,'fast_travel':0}
    for r in rows:
        key='stationary' if r['distance']<.25 else 'slow_travel' if r['speed']<.25 else 'moderate_travel' if r['speed']<1.5 else 'fast_travel';bins[key]+=1
    return dict(measured_samples=len(rows),root_motion_bins=bins,underrepresented=min(bins,key=bins.get),instruction='Use measured gaps only when compatible with this topic. Vary meaningful direction, transition and rhythm, not synonyms. Do not request running for floor/get-up topics. Root speed alone does not certify correct motion.')

def calibration_summary(root):
    from review_calibration import calibration_summary as summarize
    return summarize(root)

def admin(user):
    if user.get('role')!='admin':raise PermissionError('需要管理员权限')

def register_batch(root,folder,manifest,active=True):
    with closing(connect(root)) as db,db:
        db.execute('INSERT INTO batch_catalog(id,created,owner,active) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active',
                   (manifest['id'],manifest.get('created',folder.stat().st_mtime),manifest['owner'],int(active)))

def discover_batches(root,limit=16):
    """Read at most `limit` old manifests per tick; never discover from a web request."""
    import heapq,os
    folder=Path(root)/'review/retarget/batches'
    if not folder.exists():return
    with closing(connect(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='batch_discovery_cursor'").fetchone()
        cursor=row[0] if row else ''
    def page(after):
        with os.scandir(folder) as entries:
            return heapq.nsmallest(limit,(p.name for p in entries if p.name>after and p.is_dir(follow_symlinks=False) and re.fullmatch('[A-Za-z0-9-]{1,80}',p.name)))
    names=page(cursor) or page('')
    for name in names:
        with closing(connect(root)) as db:
            known=db.execute('SELECT 1 FROM batch_catalog WHERE id=?',(name,)).fetchone()
        if known:continue
        path=folder/name/'manifest.json'
        if not path.exists():continue
        try:
            m=json.loads(path.read_text())
            if m.get('id')==name:register_batch(root,path.parent,m)
        except (OSError,ValueError,KeyError):continue
    if names:
        with closing(connect(root)) as db,db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('batch_discovery_cursor',names[-1]))

def batches(root,cursor=None,limit=30):
    from retarget_batch import connect as batch_db
    folder=Path(root)/'review/retarget/batches';result=[]
    with closing(connect(root)) as db:
        boundary=db.execute('SELECT created,id FROM batch_catalog WHERE id=?',(cursor,)).fetchone() if cursor else None
        if cursor and not boundary:raise ValueError('批次分页游标已失效，请返回最新页')
        rows=db.execute('SELECT id FROM batch_catalog '+('WHERE (created,id)<(?,?) ' if boundary else '')+'ORDER BY created DESC,id DESC LIMIT ?',(*(tuple(boundary) if boundary else ()),min(31,max(1,limit)))).fetchall()
    for row in rows:
        path=folder/row['id']/'manifest.json'
        if not path.exists():continue
        m=json.loads(path.read_text());report=json.loads((path.parent/'status.json').read_text()) if (path.parent/'status.json').exists() else {}
        with closing(batch_db(path.parent/'batch.sqlite3')) as db:
            report['counts']=dict(db.execute('SELECT status,count(*) FROM items GROUP BY status'))
            report['failures']=[dict(r) for r in db.execute("SELECT motion_id,job_id,status,message FROM items WHERE status IN ('failed','version_mismatch','source_changed') ORDER BY position LIMIT 30")]
            control=db.execute("SELECT value FROM meta WHERE key='operator_paused'").fetchone();report['operator_paused']=bool(control and control[0]=='1')
            migration=db.execute("SELECT value FROM meta WHERE key='migration_plan'").fetchone()
            report['migration_target']=json.loads(migration[0])['target'] if migration else None
            completed=db.execute("SELECT count(*),min(updated),max(updated) FROM items WHERE status='done'").fetchone()
        n,start,end=completed
        elapsed=max(0,(end or 0)-(start or 0));rate=(n-1)/elapsed*3600 if n>1 and elapsed>0 else None
        remaining=sum(report['counts'].get(k,0) for k in ('pending','submitted','queued','running','stopping'))
        report.update(id=m['id'],total=m['total'],throughput_per_hour=rate,eta_seconds=remaining/rate*3600 if rate and remaining and report.get('state')=='running' else None,owner=m['owner'],remaining=remaining,versions=m.get('versions'),parameters=m.get('parameters'),migration_source=m.get('migration_source'))
        result.append(report)
    return result

def control_batch(root,user,value):
    admin(user)
    batch=value.get('batch','');action=value.get('action')
    if not re.fullmatch('[A-Za-z0-9-]{1,80}',batch):raise ValueError('无效批次')
    folder=Path(root)/'review/retarget/batches'/batch
    if not (folder/'manifest.json').is_file():raise ValueError('批次不存在')
    from retarget_batch import connect as batch_db
    if action=='migrate_remaining':return migrate_remaining(root,user,folder)
    if action not in ('pause','resume','retry_failed'):raise ValueError('无效操作')
    # Serialize operator mutations with the feeder, not just with other HTTP calls.
    import fcntl
    with (folder/'step.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with closing(batch_db(folder/'batch.sqlite3')) as db,db:
            if db.execute("SELECT 1 FROM meta WHERE key='migration_plan' AND NOT EXISTS(SELECT 1 FROM meta WHERE key='migration_finished')").fetchone():raise ValueError('迁移正在恢复中，请稍后刷新')
            if action in ('pause','resume'):
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('operator_paused','1' if action=='pause' else '0'))
            else:
                import secrets
                rows=db.execute("SELECT motion_id,job_id FROM items WHERE status='failed' LIMIT 100").fetchall()
                db.execute('CREATE TABLE IF NOT EXISTS attempts(motion_id TEXT,job_id TEXT,created REAL)')
                for mid,old in rows:
                    db.execute('INSERT INTO attempts VALUES(?,?,?)',(mid,old,time.time()))
                    db.execute("UPDATE items SET job_id=?,status='pending',message='人工请求重试；旧作业保留',updated=? WHERE motion_id=?",(secrets.token_hex(16),time.time(),mid))
    with closing(connect(root)) as db,db:event(db,user,'batch_'+action,value)
    register_batch(root,folder,json.loads((folder/'manifest.json').read_text()))
    return dict(ok=True)

def _finish_migration(root,folder,plan):
    """Recoverable handover under the source step lock. New work stays held first."""
    from retarget_batch import connect as batch_db,publish
    import os
    target=folder.parent/plan['target'];target.mkdir(exist_ok=True)
    m=plan['manifest']
    with closing(batch_db(target/'batch.sqlite3')) as db,db:
        db.executescript('CREATE TABLE IF NOT EXISTS items(position INTEGER PRIMARY KEY,motion_id TEXT UNIQUE,topic TEXT,source_sha256 TEXT,job_id TEXT UNIQUE,status TEXT,message TEXT,updated REAL);CREATE INDEX IF NOT EXISTS items_status ON items(status,position);CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);')
        db.execute("INSERT OR IGNORE INTO meta VALUES('migration_hold','1')")
        db.executemany('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?,?)',plan['items'])
    manifest=target/'manifest.json'
    if not manifest.exists():
        tmp=target/'manifest.tmp';tmp.write_text(json.dumps(m,ensure_ascii=False,indent=2));os.replace(tmp,manifest)
    elif json.loads(manifest.read_text())!=m:raise ValueError('迁移目标清单不一致；停止以保留旧结果')
    # Both complete new metadata files exist before retiring source work.
    with closing(batch_db(folder/'batch.sqlite3')) as db,db:
        for row in plan['items']:
            db.execute("UPDATE items SET status='migrated',message=?,updated=? WHERE motion_id=? AND status!='done'",('迁移到 '+plan['target'],time.time(),row[1]))
    with closing(batch_db(target/'batch.sqlite3')) as db,db:
        db.execute("UPDATE meta SET value='0' WHERE key='migration_hold'")
    publish(target,m,'ready','从 '+folder.name+' 迁移未完成项；旧结果保留')
    old=json.loads((folder/'manifest.json').read_text())
    with closing(connect(root)) as db,db:
        # A deterministic action body lets recovery avoid duplicate audit events.
        body=json.dumps(dict(source=folder.name,target=plan['target'],total=m['total']),sort_keys=True)
        if not db.execute("SELECT 1 FROM events WHERE action='batch_migrated' AND body=?",(body,)).fetchone():
            db.execute('INSERT INTO events(actor,action,body,created) VALUES(?,?,?,?)',(plan['actor'],'batch_migrated',body,time.time()))
    with closing(batch_db(folder/'batch.sqlite3')) as db,db:
        db.execute("INSERT OR REPLACE INTO meta VALUES('migration_finished',?)",(plan['target'],))
    publish(folder,old,'complete_with_migration','未完成项已迁移到 '+plan['target'])
    return dict(ok=True,batch=plan['target'],source=folder.name,total=m['total'],recovered=True)

def migrate_remaining(root,user,folder):
    import fcntl
    # Same order as maintenance: operations gate, then source step lock.
    with (root/'review/operations.lock').open('a') as gate:
        try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('维护正在运行，请稍后重试迁移；原批次未改变')
        return _migrate_remaining_locked(root,user,folder)

def _migrate_remaining_locked(root,user,folder):
    import fcntl,secrets
    from retarget_batch import connect as batch_db,fingerprint,job_id,validate_result
    from review_retarget import Retarget
    from retarget_parameters import validate
    from review_access import active_account
    old=json.loads((folder/'manifest.json').read_text())
    if old['owner']!=user['username']:raise PermissionError('只能为自己拥有的批次创建新版本')
    if active_account(root,old['owner'])!='admin':raise PermissionError('批次执行账户已停用')
    with (folder/'step.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with closing(batch_db(folder/'batch.sqlite3')) as db:
            saved=db.execute("SELECT value FROM meta WHERE key='migration_plan'").fetchone()
            finished=db.execute("SELECT value FROM meta WHERE key='migration_finished'").fetchone()
        if saved:
            plan=json.loads(saved[0])
            if finished:return dict(ok=True,batch=plan['target'],source=folder.name,total=plan['manifest']['total'],already_migrated=True)
            return _finish_migration(root,folder,plan)
        with closing(batch_db(folder/'batch.sqlite3')) as db,closing(Retarget(root).db()) as jobs,db:
            items=[dict(r) for r in db.execute('SELECT * FROM items ORDER BY position LIMIT 30001')]
            if len(items)>30000:raise ValueError('单次迁移最多 30000 个动作，请拆分批次')
            for item in items:
                job=jobs.execute('SELECT * FROM jobs WHERE id=?',(item['job_id'],)).fetchone()
                if job and job['status'] in ('queued','running','stopping'):raise ValueError('仍有排队或运行中的作业。请先暂停提交并等待它们结束，再迁移。')
                if item['status'] in ('submitted','queued','running','stopping') and not job:raise ValueError('有尚未完成提交的作业，请等待队列同步后再迁移')
                if job and job['status']=='done' and item['status']!='done':
                    try:valid=validate_result(root,item,job,old)
                    except (OSError,ValueError,KeyError):valid=False
                    item['status']='done' if valid else 'version_mismatch'
                    db.execute('UPDATE items SET status=? WHERE motion_id=?',(item['status'],item['motion_id']))
            remaining=[r for r in items if r['status'] not in ('done','removed','migrated')]
            if not remaining:raise ValueError('没有可迁移的未完成项；成功结果会保留在原批次')
            params=validate({});versions=fingerprint(root,remaining[0]['motion_id'],params)
            target='migration-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4);now=time.time()
            m=dict(id=target,created=now,owner=old['owner'],robot=old['robot'],parameters=params,versions=versions,total=len(remaining),concurrency=16,migration_source=folder.name,scope='Explicit migration of unfinished items to current solver and default parameters; source batch and successful outputs remain immutable.')
            plan=dict(target=target,actor=user['username'],manifest=m,items=[(i,r['motion_id'],r['topic'],r['source_sha256'],job_id(target,r['motion_id']),'pending','迁移自 '+folder.name,now) for i,r in enumerate(remaining)])
            # A terminal failed batch must re-enter the recovery scheduler BEFORE
            # the durable plan commits. A crash before the plan is harmless;
            # after it commits this source remains discoverable for recovery.
            register_batch(root,folder,old,active=True)
            db.execute("INSERT OR REPLACE INTO meta VALUES('operator_paused','1')")
            db.execute("INSERT INTO meta VALUES('migration_plan',?)",(json.dumps(plan),))
        return _finish_migration(root,folder,plan)

def overview(root,batches_cursor=None,releases_cursor=None,user=None):
    from review_retarget import Retarget
    with closing(connect(root)) as db:
        incremental=db.execute("SELECT value FROM settings WHERE key='incremental'").fetchone()
        maintenance_errors=db.execute("SELECT value FROM settings WHERE key='maintenance_errors'").fetchone()
        boundary=db.execute('SELECT created,id FROM releases WHERE id=?',(releases_cursor,)).fetchone() if releases_cursor else None
        if releases_cursor and not boundary:raise ValueError('版本分页游标已失效，请返回最新页')
        clauses=[];args=[]
        if user:clauses.append("json_extract(body,'$.owner')=?");args.append(user['username'])
        if boundary:clauses.append('(created,id)<(?,?)');args.extend(boundary)
        rows=db.execute('SELECT id,status,created,error,body FROM releases '+('WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY created DESC,id DESC LIMIT 21',args).fetchall()
        releases=[dict(id=r['id'],status=r['status'],created=r['created'],error=r['error'],**json.loads(r['body'])) for r in rows[:20]]
        releases_next=releases[-1]['id'] if len(rows)>20 else None
    with closing(Retarget(root).db()) as db:
        active=[dict(r) for r in db.execute("SELECT id,motion_id,robot,worker_slot,status FROM jobs WHERE status IN ('running','stopping') LIMIT 16")]
    # Slots are probed by their OS locks, not inferred from historical configuration.
    import fcntl
    occupied=[]
    for slot in range(16):
        path=Path(root)/'review/retarget'/('worker.lock' if slot==0 else 'worker-%d.lock'%slot)
        if not path.exists():continue
        with path.open('a') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:occupied.append(slot)
    page=batches(root,batches_cursor,31)
    return dict(batches=page[:30],batches_next=page[29]['id'] if len(page)>30 else None,releases_next=releases_next,active=active,occupied_slots=occupied,slot_limit=16,incremental=json.loads(incremental[0]) if incremental else {'enabled':False},maintenance_errors=json.loads(maintenance_errors[0]) if maintenance_errors else [],coverage=coverage(root),calibration=calibration_summary(root),releases=releases)

def configure_incremental(root,user,value):
    admin(user)
    if type(value.get('enabled')) is not bool:raise ValueError('enabled 必须为布尔值')
    from retarget_parameters import validate
    with closing(connect(root)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute("SELECT value FROM settings WHERE key='incremental'").fetchone()
        settings=json.loads(old[0]) if old else dict(owner=user['username'],robot='unitree_g1_23dof',parameters=validate({}))
        settings.update(enabled=value['enabled'],updated=time.time(),updated_by=user['username'])
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('incremental',json.dumps(settings)))
        event(db,user,'incremental_configure',settings)
    return settings

def incremental_tick(root):
    """Metadata-only scan capped at 500 candidates. A daily bounded new batch."""
    from retarget_batch import ROBOT, fingerprint, publish, job_id, connect as batch_db
    from retarget_parameters import validate
    from review_access import active_account
    from review_retarget import Retarget
    from g2h.farm_index import short_id
    with closing(connect(root)) as db:
        row=db.execute("SELECT value FROM settings WHERE key='incremental'").fetchone()
    if not row:return
    settings=json.loads(row[0])
    if not settings['enabled']:return
    if active_account(root,settings['owner'])!='admin':raise PermissionError('增量生产账户已停用')
    with closing(Retarget(root).db()) as jobs,jobs:
        jobs.execute('CREATE INDEX IF NOT EXISTS jobs_motion_robot_owner ON jobs(motion_id,robot,owner)')
    # Persistent active catalog is independent of the visible history page.
    with closing(connect(root)) as db:
        if db.execute("SELECT 1 FROM batch_catalog WHERE active=1 AND (id LIKE 'incremental-%' OR id LIKE 'migration-%') LIMIT 1").fetchone():return
        row=db.execute("SELECT value FROM settings WHERE key='incremental_scan_cursor'").fetchone();cursor=int(row[0]) if row else 0
    with closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True)) as db,closing(Retarget(root).db()) as jobs:
        db.create_function('motion_id',1,short_id)
        db.execute('ATTACH DATABASE ? AS reviews',(str(root/'review/reviews.sqlite3'),))
        db.execute('ATTACH DATABASE ? AS retarget',(str(root/'review/retarget/jobs.sqlite3'),))
        rows=db.execute('''SELECT m.number,m.topic,m.task_id FROM motion_links m
        WHERE NOT EXISTS(SELECT 1 FROM reviews.removed d WHERE d.motion_id=motion_id(m.number))
        AND NOT EXISTS(SELECT 1 FROM retarget.jobs j WHERE j.motion_id=motion_id(m.number) AND j.robot=? AND j.owner=? )
        AND m.number>? ORDER BY m.number LIMIT 500''',(ROBOT,settings['owner'],cursor)).fetchall()
    with closing(connect(root)) as db,db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('incremental_scan_cursor',str(rows[-1][0] if rows else 0)))
    selected=[]
    for number,topic,task in rows:
        path=root/'topics'/topic/'index.sqlite3'
        if not path.exists():continue
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            source=db.execute('SELECT sha256 FROM records WHERE id=?',(task,)).fetchone()
        if source:selected.append((short_id(number),topic,source[0]))
    if not selected:return
    import secrets
    batch='incremental-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(2)
    folder=root/'review/retarget/batches'/batch;folder.mkdir()
    params=validate(settings['parameters']);now=time.time()
    m=dict(id=batch,created=now,owner=settings['owner'],robot=ROBOT,parameters=params,total=len(selected),scope='New committed gallery motions with no previous owner/robot attempt; prior failures require explicit retry.',versions=fingerprint(root,selected[0][0],params),concurrency=16)
    with closing(batch_db(folder/'batch.sqlite3')) as db,db:
        db.executescript('CREATE TABLE items(position INTEGER PRIMARY KEY,motion_id TEXT UNIQUE,topic TEXT,source_sha256 TEXT,job_id TEXT UNIQUE,status TEXT,message TEXT,updated REAL);CREATE INDEX items_status ON items(status,position);CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);')
        db.executemany('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)',[(i,mid,topic,sha,job_id(batch,mid),'pending','',now) for i,(mid,topic,sha) in enumerate(selected)])
    (folder/'manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2));publish(folder,m,'ready')

def request_release(root,user,value):
    admin(user)
    rid=value.get('id','');batch=value.get('batch','');base=value.get('base') or None
    if not re.fullmatch('[A-Za-z0-9-]{1,80}',rid) or not re.fullmatch('[A-Za-z0-9-]{1,80}',batch):raise ValueError('版本和批次 ID 仅支持字母、数字、短横线')
    path=root/'review/retarget/batches'/batch/'manifest.json'
    if not path.is_file():raise ValueError('批次不存在')
    manifest=json.loads(path.read_text())
    if manifest.get('owner')!=user['username']:raise PermissionError('只能发布自己拥有的批次；其他管理员的批次仅可查看')
    body=dict(batch=batch,base=base,owner=user['username'],scope='Successful outputs only; complete failure manifest included; no dynamics claim.')
    with closing(connect(root)) as db,db:
        if base:
            old=db.execute("SELECT status,body FROM releases WHERE id=?",(base,)).fetchone()
            if not old or old[0]!='complete':raise ValueError('增量基线必须是已完成版本')
            if json.loads(old['body'])['owner']!=user['username']:raise PermissionError('只能使用自己的发布版本作为增量基线')
        if db.execute('SELECT 1 FROM releases WHERE id=?',(rid,)).fetchone():raise ValueError('版本号已存在；发布版本不可覆盖')
        db.execute('INSERT INTO releases(id,status,body,created) VALUES(?,?,?,?)',(rid,'queued',json.dumps(body),time.time()))
        event(db,user,'release_requested',dict(id=rid,**body))
    return dict(id=rid,status='queued')

def cancel_release(root,user,value):
    admin(user)
    with closing(connect(root)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM releases WHERE id=?',(value.get('id',''),)).fetchone()
        if not row or json.loads(row['body'])['owner']!=user['username']:raise PermissionError('发布请求不存在或不属于当前账户')
        if row['status']=='cancelled':return dict(ok=True,status='cancelled')
        if row['status'] not in ('queued','waiting_batch'):raise ValueError('只能取消排队或等待批次的发布；正在打包或已完成版本不可取消')
        db.execute("UPDATE releases SET status='cancelled',error='用户取消；批次和结果保留' WHERE id=?",(row['id'],))
        event(db,user,'release_cancelled',dict(id=row['id']))
    return dict(ok=True,status='cancelled')

def release_tick(root):
    """One release per maintenance tick, hardlink existing data, capped at 30k metadata rows.

    Does not compress or duplicate payloads. Manifest is the immutable export unit.
    """
    import os
    from retarget_batch import connect as batch_db
    row=None
    # Fair, bounded readiness checks; a waiting batch never owns the queue head.
    with closing(connect(root)) as db:
        candidates=db.execute("SELECT * FROM releases WHERE status IN ('queued','waiting_batch','processing') ORDER BY checked,created LIMIT 20").fetchall()
    for candidate in candidates:
        request=json.loads(candidate['body']);batch=root/'review/retarget/batches'/request['batch']
        try:
            with closing(batch_db(batch/'batch.sqlite3')) as db:
                remaining=db.execute("SELECT count(*) FROM items WHERE status IN ('pending','submitted','queued','running','stopping')").fetchone()[0]
            with closing(connect(root)) as db,db:
                if remaining:
                    db.execute("UPDATE releases SET status='waiting_batch',checked=?,error=? WHERE id=? AND status IN ('queued','waiting_batch')",(time.time(),'等待批次 '+request['batch']+' 完成，剩余 '+str(remaining)+' 项',candidate['id']))
                else:
                    claim=db.execute("UPDATE releases SET status='processing',checked=?,error='' WHERE id=? AND status IN ('queued','waiting_batch','processing')",(time.time(),candidate['id']))
                    if claim.rowcount:row=candidate
        except (OSError,sqlite3.Error) as exc:
            with closing(connect(root)) as db,db:db.execute("UPDATE releases SET status='failed',error=?,checked=? WHERE id=? AND status IN ('queued','waiting_batch','processing')",(str(exc)[:500],time.time(),candidate['id']))
        if row:break
    if not row:return
    rid=row['id'];request=json.loads(row['body']);folder=root/'review/releases'/rid
    try:
        from review_access import active_account
        if active_account(root,request['owner'])!='admin':raise PermissionError('发布账户已停用')
        batch=root/'review/retarget/batches'/request['batch']
        m=json.loads((batch/'manifest.json').read_text())
        with closing(batch_db(batch/'batch.sqlite3')) as db:
            if db.execute("SELECT 1 FROM items WHERE status IN ('pending','submitted','queued','running','stopping') LIMIT 1").fetchone():
                with closing(connect(root)) as state,state:state.execute("UPDATE releases SET status='waiting_batch',error='批次重新开始处理，等待完成' WHERE id=?",(rid,))
                return
            rows=[dict(r) for r in db.execute('SELECT * FROM items ORDER BY position LIMIT 30001')]
        if len(rows)>30000:raise ValueError('单版本上限 30000；请拆分批次')
        base_ids=set()
        if request['base']:
            base=json.loads((root/'review/releases'/request['base']/'manifest.json').read_text())
            base_ids=set(base.get('included_job_ids',[])) or {r['job_id'] for r in base['items']}
        import shutil
        if shutil.disk_usage(root).free<20*1024**3:raise ValueError('发布需要至少 20 GiB 剩余空间')
        folder.mkdir(parents=True,exist_ok=True)
        items=[];failures=[]
        from review_retarget import Retarget
        with closing(Retarget(root).db()) as jobs:
            for r in rows:
                if r['status']!='done':failures.append(r);continue
                from review_access import hidden
                if r['motion_id'] in hidden(root,[r['motion_id']]):
                    failures.append(dict(r,status='removed',message='发布时已移入回收站'));continue
                if r['job_id'] in base_ids:continue
                source=root/'review/retarget'/r['job_id']
                j=jobs.execute('SELECT owner,status FROM jobs WHERE id=?',(r['job_id'],)).fetchone()
                if not j or j['owner']!=request['owner'] or j['status']!='done':raise ValueError('发布批次包含其他用户或未完成结果')
                quality=json.loads((source/'quality.json').read_text())
                if quality.get('export_allowed') is not True:raise ValueError('输出质量门禁未通过')
                target=folder/'motions'/r['topic'];target.mkdir(parents=True,exist_ok=True)
                filename=target/(r['motion_id']+'.npz')
                if not filename.exists():os.link(source/'result.npz',filename)
                # Read at most one motion at a time; no full-dataset in-memory archive.
                digest=hashlib.sha256(filename.read_bytes()).hexdigest()
                metadata=json.loads((source/'preview.json').read_text()).get('metadata',{})
                items.append(dict(motion_id=r['motion_id'],job_id=r['job_id'],source_sha256=r['source_sha256'],topic=r['topic'],file=str(filename.relative_to(folder)),sha256=digest,bytes=filename.stat().st_size,algorithm=metadata.get('engine'),execution=metadata.get('execution'),quality_level='robot_kinematics',dynamics='unverified'))
        manifest=dict(id=rid,created=row['created'],batch=m,base=request['base'],included_job_ids=sorted(base_ids|{r['job_id'] for r in items}),items=items,failures=failures,scope=request['scope'])
        tmp=folder/'manifest.tmp';tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2));os.replace(tmp,folder/'manifest.json')
        (folder/'SHA256SUMS').write_text(''.join(r['sha256']+'  '+r['file']+'\n' for r in items))
        import zipfile
        required=sum(r['bytes'] for r in items)
        if shutil.disk_usage(root).free<required+20*1024**3:raise ValueError('发布压缩包空间不足，清单保留')
        with zipfile.ZipFile(folder/'dataset.tmp','w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for r in items:archive.write(folder/r['file'],r['file'])
            archive.write(folder/'manifest.json','manifest.json')
            archive.write(folder/'SHA256SUMS','SHA256SUMS')
        os.replace(folder/'dataset.tmp',folder/'dataset.zip')
        with closing(connect(root)) as db,db:db.execute("UPDATE releases SET status='complete' WHERE id=?",(rid,))
    except Exception as exc:
        with closing(connect(root)) as db,db:db.execute("UPDATE releases SET status='failed',error=? WHERE id=?",(str(exc)[:500],rid))

def maintenance(root):
    import fcntl
    from retarget_batch import step
    with (root/'review/operations.lock').open('a') as gate:
        try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        errors=[]
        for name,fn in [('discover_batches',lambda:discover_batches(root)),('descriptor_backfill',lambda:descriptor_backfill(root,16)),('incremental_tick',lambda:incremental_tick(root))]:
            try:fn()
            except Exception as exc:errors.append(dict(component=name,error=str(exc)[:300],at=time.time()))
        folder=root/'review/retarget/batches'
        with closing(connect(root)) as db:
            scheduled=db.execute('SELECT id FROM batch_catalog WHERE active=1 ORDER BY checked,id LIMIT 8').fetchall()
        for row in scheduled:
            path=folder/row['id']/'manifest.json'
            with closing(connect(root)) as db,db:db.execute('UPDATE batch_catalog SET checked=? WHERE id=?',(time.time(),row['id']))
            # Existing standalone controller, if active, owns this batch.
            try:
                with (path.parent/'controller.lock').open('a') as lock:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:continue
                    with (path.parent/'step.lock').open('a') as step_lock:
                        fcntl.flock(step_lock,fcntl.LOCK_EX)
                        with closing(sqlite3.connect(path.parent/'batch.sqlite3')) as db:
                            plan=db.execute("SELECT value FROM meta WHERE key='migration_plan' AND NOT EXISTS(SELECT 1 FROM meta WHERE key='migration_finished')").fetchone()
                        if plan:_finish_migration(root,path.parent,json.loads(plan[0]));continue
                        with closing(sqlite3.connect(path.parent/'batch.sqlite3')) as db:
                            pending=db.execute("SELECT 1 FROM items WHERE status IN ('pending','submitted','queued','running','stopping') LIMIT 1").fetchone()
                    if pending:step(root,path.parent,json.loads(path.read_text()))
                    else:
                        with closing(connect(root)) as db,db:db.execute('UPDATE batch_catalog SET active=0 WHERE id=?',(row['id'],))
            except Exception as exc:errors.append(dict(component='batch:'+row['id'],error=str(exc)[:300],at=time.time()))
        try:release_tick(root)
        except Exception as exc:errors.append(dict(component='release_tick',error=str(exc)[:300],at=time.time()))
        with closing(connect(root)) as db,db:db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('maintenance_errors',json.dumps(errors)))

def descriptor_backfill(root,limit=16):
    from g2h.farm_index import lookup_motion,read_motion
    import pickle
    with closing(connect(root)) as db:
        db.execute('ATTACH DATABASE ? AS agent',(str(root/'review/agent.sqlite3'),))
        db.execute('ATTACH DATABASE ? AS human',(str(root/'review/reviews.sqlite3'),))
        mids=[r[0] for r in db.execute("SELECT c.motion_id FROM agent.checks c LEFT JOIN descriptors d ON d.motion_id=c.motion_id WHERE d.motion_id IS NULL AND NOT EXISTS(SELECT 1 FROM human.removed h WHERE h.motion_id=c.motion_id) ORDER BY c.created DESC LIMIT ?",(limit,))]
    with closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True)) as catalog:
        for mid in mids:
            info=lookup_motion(catalog,root,mid)
            if not info.get('location'):continue
            try:
                record=read_motion(catalog,root,mid);record.setdefault('topic',info['topic']);record.setdefault('motion_id',mid)
                save_descriptor(root,record,info['location']['sha256'])
            except (ValueError,KeyError,EOFError,pickle.UnpicklingError):continue

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    maintenance(args.root.resolve())
