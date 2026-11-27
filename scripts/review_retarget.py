"""Durable, owner-scoped CPU retarget queue with independently locked slots."""
from collections import defaultdict
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from review_access import Forbidden, private_file, check, active_account, cancel_owner_jobs, hidden
from retarget_limits import MAX_FRAMES, MAX_SECONDS

PROJECT=Path(__file__).resolve().parents[1]
ROBOT='unitree_g1_23dof'
from retarget_robots import robot_spec, catalog
ACTIVE=('queued','running','stopping')
RETARGET_TIMEOUT_SECONDS=900
SLOT_LIMIT=int(os.environ.get('MOTIONFARM_RETARGET_SLOT_LIMIT','16'))
if not 1<=SLOT_LIMIT<=64:raise ValueError('Retarget slot limit must be 1..64')


class MeshPreviewUnavailable(Exception):
    pass



def _read_mesh_preview(path, version, geom_count):
    import numpy as np

    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('version') != version:
            return None
        frames, fps = data.get('frames'), data.get('fps')
        if type(frames) is not int or not 2 <= frames <= MAX_FRAMES:
            return None
        if type(fps) not in (int, float) or not 1 <= fps <= 240 or frames / fps > MAX_SECONDS:
            return None
        poses, roots = np.asarray(data['poses']), np.asarray(data['roots'])
        if poses.shape != (frames, geom_count, 7) or roots.shape not in ((frames, 2), (frames, 3)):
            return None
        if any(values.dtype.kind not in 'iuf' or not np.isfinite(values).all() for values in (poses, roots)):
            return None
        # Every cached field is returned to clients via the strict JSON API.
        json.dumps(data, allow_nan=False)
        return data
    except (FileNotFoundError, ValueError, KeyError, TypeError, RecursionError):
        return None


def _candidate_ledger(root):
    from g2h.harness import Store
    if (Path(root) / Store.LEDGER_PATH).is_file():
        return Store(root)


def _job_storage_bytes(folder):
    total = 0
    for directory in (folder, folder/'quarantine'):
        try:
            for entry in directory.iterdir():
                try:
                    info = entry.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        except FileNotFoundError:
            if directory == folder:
                raise
    return total


def require_retargetable_source(root, owner, motion_id):
    if not motion_id.startswith('ma_'):
        return
    ledger = _candidate_ledger(root)
    if ledger is None:
        return
    result = ledger.candidate_results(owner, [motion_id]).get(motion_id)
    if result and result['status'] == 'quarantined':
        raise ValueError('隔离候选无法提交重定向，请先修正动作。')


def _clean_engine_error(tail):
    raw = '\n'.join(tail[-5:]) if tail else ''
    last = next((line.strip() for line in reversed(tail or []) if line.strip()), '')
    cleaned = re.sub(r'^(?:Traceback.*\|)?(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*)+', '', last).strip()
    if 'CLARABEL' in raw or 'SolverError' in raw or 'clarabel' in cleaned.lower():
        return '求解器未收敛，请调整参数后重试'
    return cleaned[:200] if cleaned else '求解器退出'


class Retarget:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.folder=self.root/'review/retarget'
        self.path=private_file(self.folder/'jobs.sqlite3')
        self._progress_times={}
        # Serialize initial WAL setup across the API and queue workers.
        with private_file(self.folder / 'schema.lock').open('rb') as gate, closing(self.db()) as db:
            fcntl.flock(gate, fcntl.LOCK_EX)
            db.executescript('''PRAGMA journal_mode=WAL;
              BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT,motion_id TEXT,
                robot TEXT,status TEXT,created REAL,updated REAL,done INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,message TEXT DEFAULT '',cancel INTEGER DEFAULT 0);
              CREATE INDEX IF NOT EXISTS jobs_active ON jobs(status,created);
              CREATE TABLE IF NOT EXISTS turns(owner TEXT PRIMARY KEY,served REAL);''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            migrations = {
                'retry_at': 'REAL NOT NULL DEFAULT 0',
                'worker_slot': 'INTEGER',
                'execution': "TEXT NOT NULL DEFAULT '{}'",
                'cache_key': "TEXT NOT NULL DEFAULT ''",
                'reused_from': "TEXT NOT NULL DEFAULT ''",
                'requested_execution': "TEXT NOT NULL DEFAULT ''",
                'reuse_prior_parameters': 'INTEGER NOT NULL DEFAULT 0',
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_reuse ON jobs(owner,motion_id,robot,status,created)')
            # Older workers could write 'queued' after the user's cancellation.
            db.execute("UPDATE jobs SET status='cancelled',message='已取消',retry_at=0,updated=? "
                       "WHERE status='queued' AND cancel=1", (time.time(),))
            db.commit()

    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        return db

    def _enrich_jobs(self, rows):
        removed = hidden(self.root, list({row['motion_id'] for row in rows}))
        managed = defaultdict(set)
        for row in rows:
            if row['status'] == 'done' and (row['motion_id'] or '').startswith('ma_'):
                managed[row['owner']].add(row['motion_id'])
        ledger = _candidate_ledger(self.root) if managed else None
        quarantined = set()
        if ledger is not None:
            for owner, ids in managed.items():
                quarantined.update((owner, mid) for mid, result in ledger.candidate_results(owner, ids).items()
                                   if result['status'] == 'quarantined')
        for row in rows:
            row['source_removed'] = row['motion_id'] in removed
            self._enrich_job(row, (row['owner'], row['motion_id']) in quarantined)

    def _enrich_job(self, row, source_quarantined):
        from retarget_cache import sealed_result_matches, version_info
        from retarget_quality import retarget_export_verified
        folder = self.folder / row['id']
        row['result_version'] = None
        row['source_quarantined'] = source_quarantined
        if row['status'] == 'done':
            row['result_missing'] = not (folder / 'result.npz').is_file()
            integrity = None if row['source_removed'] or row['result_missing'] else sealed_result_matches(folder)
            row['result_corrupt'] = not (row['source_removed'] or row['result_missing']) and (
                integrity is False or (integrity is None and bool(row['reused_from'])))
            if not (row['source_removed'] or row['result_missing'] or row['result_corrupt']):
                row['preview_corrupt'] = sealed_result_matches(folder, ('preview.json',)) is False
                if not row['preview_corrupt']:
                    row['result_version'] = version_info(folder)
            proof = folder / 'quality.json'
            verified = False
            if not (row['source_removed'] or row['result_missing'] or row['result_corrupt'] or source_quarantined) and proof.is_file():
                try:
                    verified = bool(retarget_export_verified(json.loads(proof.read_text())))
                except (OSError, ValueError, TypeError):
                    verified = False
            row['export_verified'] = verified
            if row['source_removed']:
                row['export_reason'] = '源动作已移入回收站'
            elif row['result_missing']:
                row['export_reason'] = '重定向结果文件缺失'
            elif row['result_corrupt']:
                row['export_reason'] = '重定向结果完整性校验失败，请重新提交重定向'
            elif source_quarantined:
                row['export_reason'] = '源动作已标记为隔离候选，无法正式导出此轨迹。'
            elif not verified:
                row['export_reason'] = '此历史轨迹尚未通过当前验收，请进入精修重新验收'
        else:
            row['export_verified'] = False
        mid = row.get('motion_id') or ''
        info = {'motion_id': mid}
        if mid.startswith('ma_'):
            try:
                from g2h.motion_assets import MotionAssets
                meta = MotionAssets(self.root).get(row['owner'], mid)
                info.update(parent_id=meta.get('parent_id'), description=meta.get('description') or '', sha256=meta.get('sha256'))
            except (KeyError, ValueError, OSError):
                pass
        row['source_info'] = info

    def get(self,key,user):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('无效作业 ID')
        with closing(self.db()) as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
        if row is None:raise ValueError('重定向作业不存在')
        if user['role']!='admin' and row['owner']!=user['username']:raise Forbidden('只能访问自己的重定向作业')
        result = dict(row)
        self._enrich_jobs([result])
        return result

    def submit(self,value,user,store,handoff_from=None,*,reuse_prior_parameters=False):
        active_account(self.root,user['username'])
        key=value.get('id');mid=value.get('motion_id');robot=value.get('robot',ROBOT)
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('无效作业 ID')
        custom = None
        if isinstance(robot,str) and robot.startswith('robot_'):
            from custom_robot_profiles import read
            profile,_=read(self.root,user,robot,value.get('robot_version'))
            custom=dict(version=profile['version'],model_sha256=profile['model_sha256'],solver=profile['solver'])
        else:
            robot_spec(robot)
            if 'robot_version' in value:raise ValueError('内置机器人不接受 robot_version')
        if not isinstance(mid,str) or not re.fullmatch(r'(?:m[0-9a-z]{8,}|ma_[0-9a-f]{32})',mid):raise ValueError('请输入有效动作 ID')
        from retarget_parameters import resolve, execution_matches
        if type(value.get('force_new',False)) is not bool:raise ValueError('force_new 必须是布尔值')
        resolved=resolve(self.root,user,robot,value)
        if custom:resolved['custom_robot']=custom
        if value.get('force_new'):resolved['force_new']=True
        execution=json.dumps(resolved,sort_keys=True)
        detail=store.detail(mid)
        if not detail.get('location'):raise ValueError('原始动作尚未生成')
        require_retargetable_source(self.root, user['username'], mid)
        from retarget_cache import identity, reuse
        # Hash assets outside the SQLite write transaction.
        context=None
        if not value.get('force_new'):
            try:context=identity(self.root,mid,robot,execution,detail)
            except (OSError,ValueError,sqlite3.Error,subprocess.SubprocessError):context=None
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
            if prior:
                if (prior['owner'],prior['motion_id'],prior['robot'])!=(user['username'],mid,robot) or not execution_matches(prior['requested_execution'] or prior['execution'],execution):raise ValueError('作业 ID 已使用')
                db.commit()
                return self.get(key,user)
            cached=None if value.get('force_new') else reuse(self,db,key,user['username'],mid,robot,execution,context,
                                                           reuse_prior_parameters=reuse_prior_parameters)
            if cached:
                now=time.time()
                db.execute('INSERT INTO jobs(id,owner,motion_id,robot,status,created,updated,done,total,message,execution,cache_key,reused_from,requested_execution) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key,user['username'],mid,robot,'done',now,now,cached['done'],cached['total'],'已复用已有重定向结果，无需重新计算',cached['execution'],cached['cache_key'],cached['source'],execution))
                db.commit()
                return self.get(key,user)
            if handoff_from is not None:
                parent=db.execute('SELECT owner,status,cancel FROM jobs WHERE id=?',(handoff_from,)).fetchone()
                if not parent or parent['owner']!=user['username'] or parent['cancel'] or parent['status'] not in ('queued','running'):
                    raise PermissionError('方向纠正父作业已取消或不可转交')
            excluded=handoff_from or ''
            total=db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','stopping') AND id<>?",(excluded,)).fetchone()[0]
            own=db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','running','stopping') AND id<>?",(user['username'],excluded)).fetchone()[0]
            own_limit=min(48,max(16,int(os.environ.get('MOTIONFARM_ADMIN_RETARGET_LIMIT','16')))) if user['role']=='admin' else 5
            if total>=50 or own>=own_limit:raise PermissionError('队列已满：当前账户最多 %d 个未完成作业，全局最多 50 个'%own_limit)
            now=time.time()
            db.execute('INSERT INTO jobs(id,owner,motion_id,robot,status,created,updated,message,execution,reuse_prior_parameters) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (key,user['username'],mid,robot,'queued',now,now,'等待 CPU 重定向资源',execution,reuse_prior_parameters))
            if context:db.execute('UPDATE jobs SET cache_key=? WHERE id=?',(context['key'],key))
            if handoff_from is not None:
                db.execute("UPDATE jobs SET status='failed',message=?,updated=? WHERE id=?",('方向语义不符；已转入纠正作业 '+key,now,handoff_from))
            db.commit()
        return self.get(key,user)

    def status(self,user,query=None):
        watch_id = (query or {}).get('watch', [''])[0]
        watched = None
        with closing(self.db()) as db:
            active=[dict(r) for r in db.execute("SELECT j.*,COALESCE(t.served,0) AS served FROM jobs j LEFT JOIN turns t ON j.owner=t.owner WHERE status IN ('queued','running','stopping') ORDER BY CASE WHEN status='queued' THEN 1 ELSE 0 END,served,created")]
            # Interleave users for projected queue order, just as claims do.
            order=[]
            while active:
                row=active.pop(0);order.append(row)
                same=[r for r in active if r['owner']==row['owner']]
                active=[r for r in active if r['owner']!=row['owner']]+same
            positions={r['id']:i+1 for i,r in enumerate(order)}
            from review_job_pages import job_page
            rows,after=job_page(db,'jobs',user,('queued','running','stopping'),query)
            if re.fullmatch('[a-f0-9]{32}', watch_id) and all(row['id'] != watch_id for row in rows):
                match = db.execute("SELECT * FROM jobs WHERE id=? AND (owner=? OR ?='admin')",
                                   (watch_id, user['username'], user['role'])).fetchone()
                if match is not None:
                    watched = dict(match)
        from robot_repair import repair_source
        visible_rows = [*rows, watched] if watched is not None else rows
        self._enrich_jobs(visible_rows)
        for row in visible_rows:
            row['queue_position']=positions.get(row['id'])
            repair=repair_source(self.folder/row['id'],row,user) if not row['source_removed'] else None
            row['repair_available']=repair is not None
            if repair:
                from retarget_quality import CHECK_LABELS
                checks=repair[1].get('checks') or {}
                failed=[CHECK_LABELS.get(name,name) for name,passed in checks.items() if passed is False]
                row['repair_warning']='、'.join(failed)+'未通过，可下载后修复' if failed else '轨迹待复检，可下载后修复'
        visible=[r for r in order if user['role']=='admin' or r['owner']==user['username']]
        queued=sum(r['status']=='queued' for r in visible)
        from retarget_scheduler import health, DISPATCH_NOTICES
        notice=DISPATCH_NOTICES.get(health(self.root)) if queued else None
        if notice:
            notice='排队作业已保存。'+notice+'请勿重复提交。'
        return dict(items=rows,watched=watched,next_after=after,queued=queued,running=len(visible)-queued,
                    robot=ROBOT,robots=catalog(),dispatch_notice=notice)

    def cancel(self,key,user):
        row=self.get(key,user)
        with closing(self.db()) as db:
            db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'stopping' END,"
                       "updated=?,message=CASE WHEN status='queued' THEN '已取消' ELSE '取消请求已接收' END,"
                       "retry_at=0 WHERE id=? AND status IN ('queued','running','stopping')",(time.time(),key));db.commit()
        cancelled = self.get(key,user)
        if cancelled['cancel']:
            from semantic_retarget import cancel_recovery
            cancel_recovery(self, row)
            return self.get(key,user)
        return cancelled

    def result(self,key,user,name,require_dynamic=False):
        row=self.get(key,user);check(self.root,row['motion_id'])
        if row['status']!='done':raise ValueError('结果尚未完成')
        if name not in ('result.npz','preview.json'):raise ValueError('无效结果')
        if row['result_corrupt']:raise ValueError(row['export_reason'])
        if name=='preview.json' and row.get('preview_corrupt'):
            raise ValueError('重定向骨架预览完整性校验失败')
        path=self.folder/key/name
        if row['result_missing'] or not path.is_file():raise ValueError('结果文件不存在')
        if name=='result.npz' and require_dynamic and not row['export_verified']:
            if row['source_quarantined']:
                raise ValueError(row['export_reason'])
            raise ValueError('此历史轨迹尚未通过当前验收，不能作为已验证机器人轨迹导出；请重新验收')
        return path

    def repair_download(self,key,user):
        from robot_repair import draft_npz,repair_source
        row=self.get(key,user)
        check(self.root,row['motion_id'])
        repair=repair_source(self.folder/key,row,user)
        if repair is None:raise ValueError('此作业没有可下载的待修复轨迹')
        return draft_npz(repair[0],row,repair[1])

    @staticmethod
    def _finish_pending_cancellation(db, key, now):
        return db.execute("UPDATE jobs SET status='cancelled',message='已取消',retry_at=0,updated=? "
                          "WHERE id=? AND cancel=1 AND status IN ('queued','running','stopping')",
                          (now, key)).rowcount > 0

    def update(self,key,**values):
        if not set(values)<= {'done','total','message','status','retry_at'}:raise ValueError('Invalid update')
        # Intermediate progress is presentation-only; terminal updates always commit.
        if 'done' in values and 0<values['done']<values.get('total',0) and set(values)<= {'done','total','message'}:
            now=time.monotonic()
            if now-self._progress_times.get(key,-float('inf'))<1:return
            self._progress_times[key]=now
        now = time.time()
        assignments = ','.join(k+'=?' for k in values)
        status = values.get('status')
        if status == 'done' and 'done' not in values:
            assignments += ',done=total'
        with closing(self.db()) as db, db:
            if status in ('queued', 'failed', 'done'):
                db.execute(f'UPDATE jobs SET {assignments},updated=? WHERE id=? AND cancel=0',
                           (*values.values(), now, key))
                self._finish_pending_cancellation(db, key, now)
            else:
                guard = ' AND cancel=0' if set(values) <= {'done', 'total', 'message'} else ''
                db.execute(f'UPDATE jobs SET {assignments},updated=? WHERE id=?{guard}',
                           (*values.values(), now, key))

    def mesh_preview(self,key,user):
        from zipfile import BadZipFile

        import numpy as np
        from retarget_cache import digest

        result = self.result(key,user,'result.npz')
        robot = self.get(key,user)['robot']
        if robot.startswith('robot_'):
            raise ValueError('自定义机器人请读取该作业的 preview.json 骨架预览')
        spec = robot_spec(robot)
        model = PROJECT/'web/review/robot'/(spec['asset']+'.json')
        try:
            manifest = json.loads(model.read_text())
            version, geoms = manifest['version'], manifest['geoms']
            if not isinstance(version, str) or not re.fullmatch('[A-Za-z0-9_-]+', version) or not isinstance(geoms, list) or not geoms:
                raise ValueError('无效预览模型')
        except FileNotFoundError as exc:
            raise MeshPreviewUnavailable(f"{spec['label']} 的预览资源缺失，请联系管理员恢复。") from exc
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise MeshPreviewUnavailable(f"{spec['label']} 的预览资源损坏，请联系管理员恢复。") from exc
        output = result.parent/('mesh-'+version[:12]+'-'+digest(result)[:16]+'.json')
        data = _read_mesh_preview(output, version, len(geoms))
        if data is None:
            with open(self.folder/'mesh-export.lock','a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('实体预览正在准备，请稍后再试')
                data = _read_mesh_preview(output, version, len(geoms))
                if data is None:
                    env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',CUDA_VISIBLE_DEVICES='')
                    try:
                        subprocess.run([str(PROJECT/'.retarget-venv/bin/python'),str(PROJECT/'scripts/retarget_mesh.py'),
                            '--result',str(result),'--output',str(output)],env=env,timeout=30,check=True,capture_output=True)
                    except FileNotFoundError as exc:
                        raise MeshPreviewUnavailable('机器人预览转换服务不可用，请联系管理员恢复。') from exc
                    except subprocess.TimeoutExpired:raise ValueError('实体预览准备超时，请稍后再试')
                    except subprocess.CalledProcessError:raise ValueError('实体预览转换失败，请管理员检查机器人模型文件与关节顺序')
                    data = _read_mesh_preview(output, version, len(geoms))
                    if data is None:
                        raise MeshPreviewUnavailable('生成的机器人预览无法读取，请联系管理员检查预览转换服务。')
        metadata = data.get('metadata')
        scene_objects = metadata.get('scene_objects') if isinstance(metadata, dict) else None
        if not isinstance(scene_objects, list):
            # Older caches may omit the scene even when the result archive records it.
            try:
                with np.load(result, allow_pickle=False) as archive:
                    source_metadata = json.loads(str(archive['metadata']))
                    if isinstance(source_metadata, dict):
                        scene_objects = source_metadata.get('scene_objects')
                        json.dumps(scene_objects, allow_nan=False)
            except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile, RecursionError):
                scene_objects = None
        if isinstance(scene_objects, list):
            data['scene'] = {'objects': scene_objects}
            if not isinstance(metadata, dict):
                metadata = data['metadata'] = {}
            metadata['scene_objects'] = scene_objects
        return data

    def tick(self,slot=0,wait=False,stop=None,custom_only=False):
        if type(slot) is not int or not 0<=slot<SLOT_LIMIT:raise ValueError('Unsupported worker slot')
        self.revoke_inactive()
        with closing(self.db()) as db:
            if not db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping') AND retry_at<=? AND (?=0 OR substr(robot,1,6)='robot_') LIMIT 1",(time.time(),int(custom_only))).fetchone():return
        lock=open(self.folder/('worker.lock' if slot==0 else 'worker-%d.lock'%slot),'a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:lock.close();return
        env=os.environ.copy();env.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',RAYON_NUM_THREADS='1',CUDA_VISIBLE_DEVICES='')
        try:
            with open(self.folder/('worker.log' if slot==0 else 'worker-%d.log'%slot),'w') as log:
                process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(self.root),str(lock.fileno()),str(slot),str(int(custom_only))],env=env,stdout=log,stderr=log,pass_fds=(lock.fileno(),),start_new_session=True)
            if wait:
                from pipeline_pool import wait_process
                wait_process(process,stop)
            else:threading.Thread(target=process.wait,daemon=True).start()
        finally:lock.close()

    def start(self):
        self.stop=threading.Event()
        def loop():
            while not self.stop.is_set():
                try:self.tick()
                except (OSError,sqlite3.Error):pass
                self.stop.wait(3)
        threading.Thread(target=loop,daemon=True).start()

    def claim(self,slot,custom_only=False):
        """Caller holds this slot's inherited lock, including in its solver child."""
        if type(slot) is not int or not 0<=slot<SLOT_LIMIT:raise ValueError('Unsupported worker slot')
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            # Recover only this slot; pre-migration NULL belongs to legacy slot 0.
            db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'cancelled' ELSE 'queued' END,"
                       "message=CASE WHEN cancel=1 THEN '已取消' ELSE message END "
                       "WHERE status IN ('running','stopping') AND (worker_slot=? OR (worker_slot IS NULL AND ?=0)) "
                       "AND (?=0 OR substr(robot,1,6)='robot_')",(slot,slot,int(custom_only)))
            row=db.execute("SELECT j.* FROM jobs j LEFT JOIN turns t ON j.owner=t.owner WHERE j.status='queued' AND j.cancel=0 AND j.retry_at<=? AND (?=0 OR substr(j.robot,1,6)='robot_') ORDER BY COALESCE(t.served,0),j.created LIMIT 1",(time.time(),int(custom_only))).fetchone()
            if row is None:db.commit();return
            active_account(self.root,row['owner'])
            db.execute("UPDATE jobs SET status='running',worker_slot=?,done=0,message='准备原始动作',updated=? WHERE id=?",(slot,time.time(),row['id']))
            db.execute('INSERT OR REPLACE INTO turns VALUES(?,?)',(row['owner'],time.time()));db.commit()
        return row

    def _asset_detail(self, row):
        if not (row['motion_id'] or '').startswith('ma_'):
            return None
        from g2h.motion_assets import MotionAssets
        meta = MotionAssets(self.root).get(row['owner'], row['motion_id'])
        return {'location': {'sha256': meta['sha256']}}

    def work(self,slot=0,custom_only=False):
        os.nice(10)
        self.revoke_inactive()
        row=self.claim(slot,custom_only)
        if row is None:return
        key=row['id'];folder=self.folder/key
        try:
            folder.mkdir(exist_ok=True)
            before=_job_storage_bytes(folder)
        except OSError as exc:
            print(f'Retarget job directory unavailable for {key}: {exc}',flush=True)
            self.update(key,status='failed',message='作业目录不可用：'+(exc.strerror or str(exc))[:210])
            return
        sys.path.insert(0,str(PROJECT))
        from g2h import resource_quota
        reservation=None;capacity_budget=None;started=None;process=None;cache_lock=None
        work_started=time.monotonic(); timing={}
        confirmed_cancellation=None
        try:
            check(self.root,row['motion_id'])
            require_retargetable_source(self.root, row['owner'], row['motion_id'])
            sys.path.insert(0,str(PROJECT))
            from g2h.capacity import reservation as capacity_reservation
            role=active_account(self.root,row['owner'])
            from semantic_retarget import preflight
            if not row['robot'].startswith('robot_') and not preflight(self,row,folder):return
            if not row['robot'].startswith('robot_') and (folder/'semantic.json').exists():
                pending_semantic=json.loads((folder/'semantic.json').read_text())
                if pending_semantic.get('status')=='fail':
                    from semantic_retarget import retry_robot
                    retry_robot(self,row,pending_semantic)
                    return
            from retarget_cache import identity, reuse, seal
            try:context=identity(self.root,row['motion_id'],row['robot'],row['execution'],self._asset_detail(row))
            except (OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError):context=None
            timing['identity_seconds']=time.monotonic()-work_started
            if context:
                import hashlib
                lock_key=hashlib.sha256((row['owner']+':'+context['key']).encode()).hexdigest()
                cache_lock=open(self.folder/('reuse-'+lock_key+'.lock'),'a')
                try:fcntl.flock(cache_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    self.update(key,status='queued',retry_at=time.time()+10,message='相同动作与参数正在重定向，完成后将复用结果')
                    return
            with closing(self.db()) as db:
                db.execute('BEGIN IMMEDIATE')
                if self._finish_pending_cancellation(db, key, time.time()):
                    db.commit()
                    return
                cached=None if json.loads(row['execution']).get('force_new') else reuse(
                    self,db,key,row['owner'],row['motion_id'],row['robot'],row['execution'],context,
                    reuse_prior_parameters=bool(row['reuse_prior_parameters']))
                if cached:
                    db.execute("UPDATE jobs SET status='done',done=?,total=?,message='已复用已有重定向结果，无需重新计算',reused_from=?,cache_key=?,execution=?,requested_execution=?,updated=? WHERE id=?",
                               (cached['done'],cached['total'],cached['source'],cached['cache_key'],cached['execution'],row['execution'],time.time(),key));db.commit()
                    return
                db.commit()
            ticket=key+':'+secrets.token_hex(8)
            resource_quota.reserve(self.root,ticket,row['owner'],{'retarget_seconds':905,'storage_bytes':20_000_000},role)
            reservation=ticket
            capacity_context=capacity_reservation(self.root,20_000_000,'retarget:'+key)
            capacity_budget=capacity_context.__enter__()
            python=PROJECT/'.retarget-venv/bin/python'
            engine='gadget_retarget_engine.py' if row['motion_id'].startswith(('mimp','ma_')) else 'retarget_engine.py'
            if row['robot'].startswith('robot_'):engine='custom_robot_engine.py'
            if engine=='retarget_engine.py' and os.environ.get('KINORA_DISABLE_FAST_RETARGET') != '1':
                _exec=json.loads(row['execution'])
                if not _exec.get('preset') and not _exec.get('parent_job_id') and _exec.get('parameters',{}) == __import__('retarget_parameters').validate({}):
                    from retarget_cache import has_fast_engine
                    if has_fast_engine():
                        engine='fast_retarget_engine.py'
            command=[str(python),str(PROJECT/'scripts/retarget_dispatch.py'),engine,'--root',str(self.root),'--job',key,'--motion',row['motion_id'],'--robot',row['robot']]
            with open(folder/'engine.log','w') as log:
                active_account(self.root,row['owner'])
                started=time.monotonic()
                process=subprocess.Popen(command,stdout=log,stderr=log,pass_fds=(int(sys.argv[2]),))
                deadline=time.monotonic()+RETARGET_TIMEOUT_SECONDS
                while process.poll() is None:
                    with closing(self.db()) as db:cancel=db.execute('SELECT cancel FROM jobs WHERE id=?',(key,)).fetchone()[0]
                    try:active_account(self.root,row['owner'])
                    except PermissionError:cancel=True
                    if cancel or time.monotonic()>deadline:
                        process.terminate()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:process.kill();process.wait()
                        self.update(key,status='cancelled' if cancel else 'failed',message='已取消' if cancel else '单作业超过 15 分钟，已停止；自动生产不受影响')
                        if cancel:confirmed_cancellation='已取消'
                        return
                    time.sleep(.5)
            timing['solver_process_seconds']=time.monotonic()-started
            if process.returncode:
                with open(folder/'engine.log','rb') as log:
                    log.seek(max(0,os.fstat(log.fileno()).st_size-2000));tail=log.read().decode('utf-8','replace').strip().splitlines()
                semantic_path=folder/'semantic.json'
                semantic=json.loads(semantic_path.read_text()) if semantic_path.exists() else {}
                if semantic.get('status')=='fail' and not row['robot'].startswith('robot_'):
                    from semantic_retarget import retry_robot
                    retry_robot(self,row,semantic)
                else:
                    self.update(key,status='failed',message='重定向失败：'+_clean_engine_error(tail))
            elif not (folder/'result.npz').is_file() or not (folder/'preview.json').is_file():
                self.update(key,status='failed',message='结果文件不完整')
            else:
                if context:
                    try:
                        current=identity(self.root,row['motion_id'],row['robot'],row['execution'],self._asset_detail(row))
                        if current and current['key']==context['key']:seal(folder,context)
                    except (OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError):pass
                from retarget_quality import retarget_export_verified
                quality_path=folder/'quality.json'
                quality=json.loads(quality_path.read_text()) if quality_path.is_file() else {}
                if quality and not retarget_export_verified(quality):
                    message=(quality.get('reason') or '重定向已完成，部分指标待精修确认')[:220]
                else:
                    message='重定向完成；基础方向已检查，完整语义待审核'
                self.update(key, status='done', message=message)
        except resource_quota.QuotaExceeded as exc:
            self.update(key,status='queued',retry_at=time.time()+60,message=str(exc)[:240])
        except PermissionError as exc:
            message=str(exc)[:240]
            self.update(key,status='cancelled',message=message)
            confirmed_cancellation=message
        except sqlite3.OperationalError:
            self.update(key,status='queued',retry_at=time.time()+60,message='账户或配额数据库暂时不可用，保留作业，稍后重试')
        except Exception as exc:
            cleaned=re.sub(r'^(?:Traceback.*\|)?(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*)+','',str(exc).strip()).strip()
            self.update(key,status='failed',message=(cleaned or '重定向执行异常')[:240] if isinstance(exc,(ValueError,RuntimeError)) else '重定向执行异常，请检查日志')
        finally:
            if cache_lock is not None:cache_lock.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            try:
                with closing(self.db()) as db:
                    final=db.execute('SELECT status,message FROM jobs WHERE id=?',(key,)).fetchone()
            except (OSError,sqlite3.Error) as exc:
                print(f'Retarget final status unavailable for {key}: {exc}',flush=True)
                final=(dict(status='cancelled',message=confirmed_cancellation)
                       if confirmed_cancellation is not None else None)
            if final and final['status'] in ('failed','cancelled'):
                from retarget_quality import quarantine
                try:quarantine(folder,final['message'],final['status'])
                except (OSError,ValueError):print('Quarantine metadata unavailable for failed/cancelled job',key,flush=True)
            timing.update(total_seconds=time.monotonic()-work_started,solver_started=started is not None,version=1)
            temp=folder/'timing.tmp'
            try:
                temp.write_text(json.dumps(timing))
                os.replace(temp,folder/'timing.json')
            except OSError as exc:
                print(f'Retarget timing unavailable for {key}: {exc}',flush=True)
            if reservation:
                actual={'retarget_seconds':0 if started is None else time.monotonic()-started}
                raw_delta=None
                try:
                    raw_delta=_job_storage_bytes(folder)-before
                    actual['storage_bytes']=max(0,raw_delta)
                except OSError as exc:
                    # Keep the conservative storage charge when the bytes cannot be measured.
                    print(f'Retarget storage measurement unavailable for {key}: {exc}',flush=True)
                try:
                    if capacity_budget is not None:
                        try:
                            if raw_delta is not None:capacity_budget.commit(raw_delta)
                        finally:capacity_context.__exit__(None,None,None)
                except (OSError,sqlite3.Error) as exc:
                    print(f'Retarget capacity settlement unavailable for {key}: {exc}',flush=True)
                finally:
                    resource_quota.settle(self.root,reservation,actual)

    def revoke_inactive(self):
        with closing(self.db()) as db:
            owners=[r[0] for r in db.execute("SELECT DISTINCT owner FROM jobs WHERE status IN ('queued','running','stopping')")]
        for owner in owners:
            try:active_account(self.root,owner)
            except PermissionError:cancel_owner_jobs(self.root,owner)

if __name__=='__main__':
    # FD inherited from the web process and held until this worker exits.
    lock_fd=int(sys.argv[2]);fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    Retarget(sys.argv[1]).work(int(sys.argv[3]) if len(sys.argv)>3 else 0,custom_only=len(sys.argv)>4 and sys.argv[4]=='1')
