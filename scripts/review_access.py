"""Default-deny role policy, reversible library removals, and motion authorship."""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import time

class Forbidden(Exception):pass
class Removed(ValueError):pass


def active_account(root, username):
    """Read current authority at a work boundary; unavailable authority fails closed."""
    path=Path(root)/'review/accounts.sqlite3'
    if not username or not path.is_file():
        raise PermissionError('账户不存在或账户数据库暂不可用')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        row=db.execute('SELECT role,disabled FROM users WHERE username=?',(username,)).fetchone()
    if not row or row[1]:
        raise PermissionError('账户已停用或不存在；停止后续作业，已生成结果保留')
    return row[0]


def cancel_owner_jobs(root,username):
    """Revoke queued work immediately; an active GPU quantum commits safely first."""
    for name,table,active in (('custom','requests',('queued','preparing','ready','running')),
                              ('motion_edits','jobs',('queued','preparing','ready','running')),
                              ('retarget','jobs',('queued','running','stopping'))):
        path=Path(root)/'review'/name/'jobs.sqlite3'
        if not path.is_file():continue
        with closing(sqlite3.connect(path,timeout=3)) as db,db:
            if name in ('custom','motion_edits'):
                db.execute("UPDATE "+table+" SET cancel=1,status=CASE WHEN status IN ('preparing','running') THEN status ELSE 'cancelled' END,message=?,updated=? WHERE owner=? AND status IN ('queued','preparing','ready','running')",
                           ('账户已停用；当前小批次完成后停止，已生成结果保留',time.time(),username))
            else:
                db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'stopping' END,message=?,updated=? WHERE owner=? AND status IN ('queued','running','stopping')",
                           ('账户已停用，停止重定向作业',time.time(),username))

PUBLIC_FIELDS=('motion_id','topic','text','state','review','owner','request_id','storage_tier','tags')


def allowed(user,path,write):
    if user.get('role')=='admin':return True
    if user.get('role')!='user':return False
    if path in ('/api/harness/capabilities','/api/harness/export','/api/harness/results'):return not write
    asset = path.removeprefix('/api/harness/personal-assets/')
    if asset != path:
        if asset in ('rigs', 'poses', 'objects', 'snapshots'): return True
        if asset in ('inspect-bvh', 'import-bvh', 'apply-pose'): return write
        if asset in ('motions', 'history', 'snapshot-source'): return not write
        return not write and bool(re.fullmatch(r'(?:(?:rigs|poses|objects|snapshots|snapshot-jobs)/[0-9a-f]{32}|objects/[0-9a-f]{32}/mesh|rigs/[0-9a-f]{32}/calibration|snapshots/[0-9a-f]{32}/download)', asset))
    if path=='/api/harness/robots':return True
    if re.fullmatch(r'/api/harness/robots/robot_[0-9a-f]{32}/preview',path):return not write
    if re.fullmatch(r'/api/harness/robots/robot_[0-9a-f]{32}/profiles',path):return True
    if path=='/api/harness/tasks':return True
    if path=='/api/harness/video-motion':return True
    if re.fullmatch(r'/api/harness/video-motion/[0-9a-f]{64}',path):return not write
    if re.fullmatch(r'/api/harness/tasks/[A-Za-z0-9_-]{1,96}/(?:paired-preview|multi-preview)',path):return not write
    if path in ('/api/harness/splice','/api/harness/splice-preview','/api/harness/robot-edits','/api/harness/dynamics-validation','/api/harness/scene','/api/harness/paired-scene','/api/harness/multi-scene','/api/harness/object-response'):return write
    if re.fullmatch(r'/api/harness/robot-edits/[a-f0-9]{32}',path):return not write
    if re.fullmatch(r'/api/harness/robot-edits/[a-f0-9]{32}/(?:preview|model|download)',path):return not write
    if re.fullmatch(r'/api/harness/robot-edits/[a-f0-9]{32}/(?:edit|history|import-repair)',path):return write
    if re.fullmatch(r'/api/harness/tasks/[A-Za-z0-9_-]{1,96}',path):return not write
    if re.fullmatch(r'/api/harness/tasks/[A-Za-z0-9_-]{1,96}/(feedback|advance|cancel|accept|import-patch|repair|repair-status|scene-status|scene-resume)',path):return write
    if re.fullmatch(r'/api/harness/motions/[A-Za-z0-9_-]{1,96}/(?:preview|download)',path):return not write
    if path in ('/api/storage','/api/storage/download','/api/storage/archive'):return not write
    if re.fullmatch('/api/motions/m[0-9a-z]{8,}/review',path):return write
    if re.fullmatch('/api/motions/m[0-9a-z]{8,}/remove',path):return write
    if re.fullmatch('/api/motions/m[0-9a-z]{8,}/presence',path):return True
    if path in ('/api/library/collections','/api/library/saved-searches'):return True
    if path=='/api/library/export':return not write
    if path in ('/api/library/search','/api/library/find','/api/library/aliases'):return not write
    if path in ('/api/research/report','/api/research/retarget','/api/research/preview','/api/research/assessment','/api/research/active-queue'):return not write
    if path=='/api/research/calibration':return True
    if re.fullmatch('/api/motions/m[0-9a-z]{8,}/review-history',path):return not write
    if path=='/api/personal/presets':return True
    if path=='/api/personal/motions':return not write
    if path=='/api/tags':return not write
    if re.fullmatch('/api/motions/m[0-9a-z]{8,}/tags',path):return True
    if path=='/api/gadget/keys':return True
    if path=='/api/gadget/keys/revoke':return write
    if path=='/api/retarget':return True
    if path in ('/api/retarget/model','/api/retarget/mesh-data','/api/retarget/mesh-preview'):return not write
    if path=='/api/retarget/cancel':return write
    if path in ('/api/retarget/preview','/api/retarget/download','/api/retarget/repair-download'):return not write
    if path=='/api/rewrite/models':return not write
    if path=='/api/custom':return True
    if path=='/api/custom/cancel':return write
    if path=='/api/custom/results':return not write
    if not write and path in ('/api/topics','/api/motions'):return True
    return not write and bool(re.fullmatch(r'/api/motions/m[0-9a-z]{8,}(/preview(?:\.bin)?|/thumbnail)?',path))


def private_file(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    # Closing any raw fd for an existing database can release POSIX locks
    # held by a different SQLite connection in this process. Only create it.
    try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    except FileExistsError:pass
    else:os.close(fd)
    os.chmod(path,0o600)
    return path


def setup(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS removed(
      motion_id TEXT PRIMARY KEY,removed_at REAL,username TEXT,reason TEXT);
      CREATE TABLE IF NOT EXISTS removal_events(
      id INTEGER PRIMARY KEY,motion_id TEXT,action TEXT,username TEXT,at REAL,reason TEXT);
      CREATE TABLE IF NOT EXISTS motion_owners(
      motion_id TEXT PRIMARY KEY,username TEXT NOT NULL,request_id TEXT,source TEXT NOT NULL,created REAL);
      CREATE INDEX IF NOT EXISTS owners_user ON motion_owners(username,motion_id);''')


def hidden(root,ids):
    path=root/'review/reviews.sqlite3'
    if not ids or not path.exists():return set()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='removed'").fetchone():return set()
        return {r[0] for r in db.execute('SELECT motion_id FROM removed WHERE motion_id IN ('+','.join('?' for _ in ids)+')',ids)}


def check(root,mid):
    if hidden(root,[mid]):raise Removed('动作已被管理员移入回收站')


def require_motion_access(store,mid,user):
    """Farm motions are shared; authorship does not make them private.

    Numeric local imports keep their existing owner-only boundary. Job status,
    cancellation and removal permissions remain separate from source access.
    """
    if not user.get('username'):raise Forbidden('请登录后访问动作')
    if isinstance(mid,str) and mid.startswith('mimp'):
        from gadget_imports import Imports
        return Imports(store.root).get(mid,user['username'])
    if isinstance(mid,str) and mid.startswith('ma_'):
        from g2h.motion_assets import MotionAssets, ID_PATTERN
        if not ID_PATTERN.fullmatch(mid):raise ValueError('无效动作编号')
        try:return MotionAssets(store.root).get(user['username'],mid)
        except (KeyError,ValueError) as exc:raise Forbidden('动作不存在或当前不可访问') from exc
    from g2h.farm_index import motion_number
    number=motion_number(mid)
    check(store.root,mid)
    with closing(store.catalog()) as db:
        if not db.execute('SELECT 1 FROM motion_links WHERE number=?',(number,)).fetchone():
            raise Forbidden('动作不存在或当前不可访问')
    return None


def owners_map(root,ids):
    path=root/'review/reviews.sqlite3'
    if not ids or not path.exists():return {}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='motion_owners'").fetchone():return {}
        rows=db.execute('SELECT motion_id,username,request_id FROM motion_owners WHERE motion_id IN ('
                        +','.join('?' for _ in ids)+')',ids).fetchall()
    return {r[0]:dict(owner=r[1],request_id=r[2]) for r in rows}


def owner_of(root,mid):
    return owners_map(root,[mid]).get(mid,{})


def claim_owners(root,ids,username,request_id='',source='custom'):
    if not ids or not username:return
    path=private_file(root/'review/reviews.sqlite3')
    now=time.time()
    with closing(sqlite3.connect(path,timeout=5)) as db, db:
        setup(db)
        db.executemany('INSERT OR IGNORE INTO motion_owners VALUES(?,?,?,?,?)',
                       [(mid,username,request_id,source,now) for mid in ids])


def backfill_custom_owners(root):
    jobs=root/'review/custom/jobs.sqlite3'
    catalog=root/'prompts.sqlite3'
    if not jobs.exists() or not catalog.exists():return
    from g2h.farm_index import short_id
    with closing(sqlite3.connect(jobs.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        rows=db.execute('SELECT id,owner,seed,n FROM requests').fetchall()
    if not rows:return
    claimed=[]
    with closing(sqlite3.connect(catalog.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        for key,owner,seed,n in rows:
            for number, in db.execute('SELECT number FROM motion_links WHERE topic=? AND seed>=? AND seed<?',
                                      ('custom',seed,seed+n)).fetchall():
                claimed.append((short_id(number),owner,key))
    for mid,owner,key in claimed:
        claim_owners(root,[mid],owner,key,'custom')


def public_motion(info, admin=False):
    review=info.get('review')
    if review and not admin:
        review={'status':review.get('status')}
    item={key:info.get(key) for key in PUBLIC_FIELDS}
    item['review']=review
    item['direction_status']=info.get('direction_status','not_checked')
    item['state']='done' if info.get('location') or info.get('state')=='done' else info.get('state')
    if admin:
        for key in ('task_id','text_id','seed','error','location'):
            if key in info: item[key]=info[key]
    return item


def remove(store,mid,user,reason='',restore=False):
    if user.get('role')!='admin':
        if restore:raise Forbidden('仅管理员可恢复动作')
        if user.get('role')!='user' or not user.get('username') or owner_of(store.root,mid).get('owner')!=user['username']:
            raise Forbidden('只能删除自己生成的动作')
    from g2h.farm_index import motion_number,motion_info
    if not isinstance(reason,str) or len(reason)>200:raise ValueError('删除原因最长 200 字符')
    number=motion_number(mid)
    result={'ok':True,'motion_id':mid,'removed':not restore}
    with closing(store.reviews()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        exists=db.execute('SELECT 1 FROM removed WHERE motion_id=?',(mid,)).fetchone()
        if restore and exists:
            # A removal record is sufficient to restore visibility while its
            # source index is offline.
            db.execute('DELETE FROM removed WHERE motion_id=?',(mid,))
        elif not restore and exists:
            return result
        elif restore and db.execute(
                "SELECT 1 FROM removal_events WHERE motion_id=? AND action='restore' LIMIT 1", (mid,)).fetchone():
            return result
        else:
            with closing(store.catalog()) as catalog:
                row=catalog.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links WHERE number=?',(number,)).fetchone()
                if not row or not motion_info(store.root,row)['location']:raise ValueError('只能删除已生成的动作')
            if restore:return result
            db.execute('INSERT INTO removed VALUES(?,?,?,?)',(mid,time.time(),user['username'],reason))
        db.execute('INSERT INTO removal_events(motion_id,action,username,at,reason) VALUES(?,?,?,?,?)',
                   (mid,'restore' if restore else 'remove',user['username'],time.time(),reason))
    return result
