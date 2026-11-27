"""Owner-private, immutable robot edits with explicit review and bounded IK workers."""
from pathlib import Path
import hashlib,io,json,os,re,secrets,sqlite3,subprocess,sys,time
from contextlib import closing
from functools import lru_cache
from zipfile import BadZipFile
from zlib import error as CompressionError
P=Path(__file__).resolve().parents[1];sys.path[:0]=[str(P/'scripts'),str(P)]
from g2h.capacity import worker_exited
VERSION='robot-editor-v1'
IDENT=re.compile(r'[a-f0-9]{32}')

class EditorVersionConflict(ValueError):
    """The operation refers to a version that is no longer current."""

class EditorTrajectoryMissing(ValueError):
    def __init__(self, message='候选轨迹文件已丢失，请撤销到可用版本后重新生成'):
        super().__init__(message)

class EditorTrajectoryDamaged(EditorTrajectoryMissing):
    def __init__(self):
        super().__init__('候选轨迹文件损坏或无法读取，请撤销到可用版本后重新生成')


class EditorTrajectoryMismatch(EditorTrajectoryMissing):
    def __init__(self):
        super().__init__('候选轨迹文件已损坏或与验收记录不一致，请撤销到可用版本后重新生成')


def _candidate_proof(version):
    if version['trajectory_sha256']:
        return ('file', version['trajectory_sha256'])
    # Earlier workers stored the candidate's identity and quality proof inside the archive.
    return ('quality', hashlib.sha256((version['quality'] or '{}').encode()).hexdigest())


def _candidate_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise EditorTrajectoryMissing() from None
    except OSError:
        raise EditorTrajectoryDamaged() from None


def _read_candidate(raw, session_id, version_id, proof):
    import numpy as np

    proof_type, expected = proof
    if proof_type == 'file' and hashlib.sha256(raw).hexdigest() != expected:
        raise EditorTrajectoryMismatch()
    try:
        with np.load(io.BytesIO(raw), allow_pickle=False) as archive:
            arrays = {name: archive[name] for name in archive.files}
        poses = arrays['qpos']
        joints = arrays['joint_names']
        fps = float(arrays['fps'])
        metadata = json.loads(str(arrays['metadata']))
        if (poses.ndim != 2 or not 1 <= len(poses) <= 3600 or not poses.shape[1]
                or poses.dtype.kind not in 'iuf' or not np.isfinite(poses).all()
                or joints.ndim != 1 or joints.dtype.kind not in 'SU'
                or not 1 <= fps <= 240 or not isinstance(metadata, dict)):
            raise ValueError('无效候选轨迹')
        json.dumps(metadata, allow_nan=False)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, BadZipFile, CompressionError, RecursionError, OverflowError):
        raise EditorTrajectoryDamaged() from None
    identity = metadata.get('robot_edit')
    if not isinstance(identity, dict) or (identity.get('session'), identity.get('candidate')) != (session_id, version_id):
        raise EditorTrajectoryMismatch()
    if proof_type == 'quality':
        legacy_quality = hashlib.sha256(encoded(metadata.get('quality', {})).encode()).hexdigest()
        if legacy_quality != expected:
            raise EditorTrajectoryMismatch()
    return arrays


@lru_cache(maxsize=256)
def _cached_candidate_error(path, file_identity, proof):
    try:
        _read_candidate(_candidate_bytes(path), path.parent.name, path.stem, proof)
    except EditorTrajectoryMissing as error:
        return type(error)
    return None


def _candidate_error(path, proof):
    try:
        info = path.stat()
    except FileNotFoundError:
        return EditorTrajectoryMissing
    except OSError:
        return EditorTrajectoryDamaged
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return _cached_candidate_error(path, identity, proof)


def encoded(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
def ident(x):
    if not isinstance(x,str) or not IDENT.fullmatch(x):raise ValueError('无效编辑编号')
    return x


def export_verified(quality):
    if 'repair_recheck' in quality:
        from robot_repair import export_verified as repair_verified
        return repair_verified(quality)
    from robot_dynamics import export_verified as dynamics_verified
    from robot_edit_smoothness import verified as smoothness_verified
    return dynamics_verified(quality) and smoothness_verified(quality)


class Editor:
    def __init__(self,root):
        from review_access import private_file
        self.root=Path(root);self.folder=self.root/'review/robot_edits';self.db_path=private_file(self.folder/'index.sqlite3')
        with closing(self.db()) as d:
            d.executescript('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,owner TEXT,job TEXT,source_sha TEXT,head TEXT,redo TEXT,accepted TEXT,created REAL); CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,session TEXT,parent TEXT,request TEXT,digest TEXT,status TEXT,body TEXT,quality TEXT,error TEXT,created REAL,pid INTEGER,process_identity TEXT,trajectory_sha256 TEXT,UNIQUE(session,request));')
            if 'trajectory_sha256' not in {column['name'] for column in d.execute('PRAGMA table_info(versions)')}:
                with d:
                    d.execute('BEGIN IMMEDIATE')
                    if 'trajectory_sha256' not in {column['name'] for column in d.execute('PRAGMA table_info(versions)')}:
                        d.execute('ALTER TABLE versions ADD COLUMN trajectory_sha256 TEXT')
    def db(self):
        d=sqlite3.connect(self.db_path,timeout=5);d.row_factory=sqlite3.Row;return d
    def trajectory_path(self,sid,version):
        return self.folder/sid/(version+'.npz')
    def session(self,user,sid):
        from review_access import active_account,Forbidden
        active_account(self.root,user['username']);ident(sid)
        with closing(self.db()) as d:r=d.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        if not r or r['owner']!=user['username']:raise Forbidden('编辑不存在或不属于当前账户')
        return dict(r)
    def source(self,user,job):
        from review_retarget import Retarget
        from review_access import Forbidden,check
        ident(job);q=Retarget(self.root);row=q.get(job,user)
        if row['owner']!=user['username']:raise Forbidden('只能编辑自己的重定向结果')
        check(self.root,row['motion_id'])
        if row['status'] not in ('done','failed'):raise ValueError('请等待重定向完成')
        if row.get('result_corrupt'):raise ValueError(row['export_reason'])
        folder=q.folder/job;path=folder/'result.npz'
        if row['status']=='failed':
            from robot_repair import repair_source
            repair=repair_source(folder,row,user)
            if repair is None:raise ValueError('此作业没有可精修的完整机器人轨迹')
            path=repair[0]
        if not path.is_file():raise ValueError('此作业没有可修整轨迹')
        return row,path
    def checked_source(self,user,session):
        row,path=self.source(user,session['job'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=session['source_sha']:raise ValueError('原始轨迹已改变，请重新建立编辑')
        return row,path
    def load(self,user,sid,version=None):
        import numpy as np
        s=self.session(user,sid);row,path=self.checked_source(user,s)
        version=version or s['head']
        if version!='original':
            ident(version)
            with closing(self.db()) as d:v=d.execute('SELECT * FROM versions WHERE id=? AND session=?',(version,sid)).fetchone()
            if not v or v['status'] in ('pending','running','failed'):raise ValueError('候选尚不可读取')
            path=self.trajectory_path(sid,version)
            a = _read_candidate(_candidate_bytes(path), sid, version, _candidate_proof(v))
        else:
            with np.load(path,allow_pickle=False) as z:a={k:z[k].copy() for k in z.files}
        if len(a['qpos'])>3600 or a['qpos'].ndim!=2:raise ValueError('轨迹最多支持 3600 帧')
        return s,row,a
    def model(self,user,row,a):
        import mujoco as mj
        meta=json.loads(str(a['metadata']));profile=None
        if row['robot'].startswith('robot_'):
            from custom_robot_profiles import read
            pinned=json.loads(row['execution'])['custom_robot'];profile,path=read(self.root,user,row['robot'],pinned['version'])
            if profile['mujoco_version']!=mj.__version__:raise ValueError('机器人运行库不一致')
            model=mj.MjModel.from_binary_path(str(path))
        else:
            from retarget_robots import model_path
            path=model_path(row['robot'])
            if meta.get('robot_sha256')!=hashlib.sha256(path.read_bytes()).hexdigest():raise ValueError('机器人模型版本已改变')
            model=mj.MjModel.from_xml_path(str(path))
        if a['qpos'].shape[1]!=model.nq:raise ValueError('机器人轨迹维度错误')
        return model,profile
    def create(self,user,value):
        if set(value)!={'job_id','request_id'}:raise ValueError('编辑参数无效')
        request=ident(value['request_id']);row,path=self.source(user,value['job_id']);sid=hashlib.sha256((user['username']+':'+request).encode()).hexdigest()[:32]
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with closing(self.db()) as d,d:
            old=d.execute('SELECT job,source_sha FROM sessions WHERE id=?',(sid,)).fetchone()
            if old and tuple(old)!=(row['id'],digest):raise ValueError('请求编号已用于其他轨迹')
            d.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?,?,?,?)',(sid,user['username'],row['id'],digest,'original','[]',None,time.time()))
        return self.detail(user,sid)
    def reap(self):
        with closing(self.db()) as d:rows=list(d.execute("SELECT id,session,pid,process_identity,created FROM versions WHERE status IN ('pending','running')"))
        for v in rows:
            if time.time() - v['created'] > 10 and worker_exited(v['pid'], v['process_identity']):
                self.mark_interrupted(v)

    @staticmethod
    def _valid_ancestor(d, sid, target):
        seen = set()
        cur = target
        while cur and cur != 'original' and cur not in seen:
            seen.add(cur)
            row = d.execute('SELECT parent, status FROM versions WHERE id=? AND session=?', (cur, sid)).fetchone()
            if row is None:
                return 'original'
            if row['status'] != 'failed':
                return cur
            cur = row['parent']
        return 'original'

    @classmethod
    def _heal_session_head(cls, d, sid):
        s = d.execute('SELECT head, redo FROM sessions WHERE id=?', (sid,)).fetchone()
        if s is None:
            return None
        head = cls._valid_ancestor(d, sid, s['head'])
        redo = json.loads(s['redo'])
        clean_redo = []
        for vid in redo:
            row = d.execute('SELECT status FROM versions WHERE id=? AND session=?', (vid, sid)).fetchone()
            if row is not None and row['status'] != 'failed':
                clean_redo.append(vid)
        if head != s['head'] or clean_redo != redo:
            d.execute('UPDATE sessions SET head=?, redo=? WHERE id=?', (head, encoded(clean_redo), sid))
        return head

    @classmethod
    def _rollback_failed_head(cls, d, sid, vid):
        s = d.execute('SELECT head, redo FROM sessions WHERE id=?', (sid,)).fetchone()
        if s is None:
            return
        redo = [r for r in json.loads(s['redo']) if r != vid]
        if s['head'] == vid:
            new_head = cls._valid_ancestor(d, sid, vid)
            d.execute('UPDATE sessions SET head=?, redo=? WHERE id=?', (new_head, encoded(redo), sid))
        else:
            cls._heal_session_head(d, sid)

    def mark_interrupted(self, version):
        from g2h import resource_quota

        with closing(self.db()) as d, d:
            d.execute('BEGIN IMMEDIATE')
            changed = d.execute(
                "UPDATE versions SET status='failed',error='编辑进程中断，请新建候选' "
                "WHERE id=? AND status IN ('pending','running') AND pid IS ? AND process_identity IS ?",
                (version['id'], version['pid'], version['process_identity']),
            ).rowcount
            if changed:
                self._rollback_failed_head(d, version['session'], version['id'])
        if changed:
            (self.folder/version['session']/(version['id']+'.upload.npy')).unlink(missing_ok=True)
            elapsed = min(125, max(0, time.time() - version['created']))
            resource_quota.settle(self.root,'robot-edit:'+version['id'],{'retarget_seconds':elapsed,'storage_bytes':20_000_000})

    def watch_worker(self, proc, vid):
        proc.wait()
        try:
            if not self.db_path.is_file():
                return
            with closing(self.db()) as d:
                version = d.execute("SELECT id,session,pid,process_identity,created FROM versions "
                                    "WHERE id=? AND pid=? AND status IN ('pending','running')", (vid, proc.pid)).fetchone()
            if version:
                self.mark_interrupted(version)
        except sqlite3.OperationalError:
            if self.db_path.is_file():
                raise
    def detail(self,user,sid):
        s=self.session(user,sid);row,_=self.source(user,s['job']);self.reap()
        with closing(self.db()) as d, d:
            d.execute('BEGIN IMMEDIATE')
            self._heal_session_head(d, sid)
            s = dict(d.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
            rows=[dict(v) for v in d.execute('SELECT id,parent,request AS request_id,status,quality,error,created,trajectory_sha256 FROM versions WHERE session=? ORDER BY created',(sid,))]
        for v in rows:
            if v['status'] in ('ready','quarantined'):
                problem = _candidate_error(self.trajectory_path(sid,v['id']), _candidate_proof(v))
                v['trajectory_available'] = problem is None
                if problem is not None:
                    v['trajectory_error'] = str(problem())
            v.pop('trajectory_sha256')
            v['quality']=json.loads(v['quality']) if v['quality'] else None
        accepted=next((v for v in rows if v['id']==s['accepted']),None)
        accepted_exportable=bool(accepted and accepted['status']=='ready' and accepted['trajectory_available']
                                 and export_verified(accepted['quality'] or {}))
        return dict(id=sid,job_id=s['job'],repair_available=row['status']=='failed',head=s['head'],
                    accepted=s['accepted'],accepted_exportable=accepted_exportable,
                    can_undo=s['head']!='original',can_redo=bool(json.loads(s['redo'])),versions=rows)
    def preview(self,user,sid,version=None):
        import numpy as np,mujoco as mj
        s,row,a=self.load(user,sid,version);m,p=self.model(user,row,a);q=a['qpos'];d=mj.MjData(m);frames=[]
        for pose in q:d.qpos[:]=pose;mj.mj_forward(m,d);frames.append(d.xpos[1:].copy()[:,[0,2,1]]*np.array([1,1,-1]))
        meta=json.loads(str(a['metadata']));tracking=meta.get('tracking') or {}
        # Preview must not run a minute-long simulation on every seek/version switch.
        # Only immutable worker proof can authorize export; old reports remain unverified.
        import copy
        original=(version or s['head'])=='original'
        quality=copy.deepcopy(meta.get('quality') or {})
        if original and row['status']=='failed':
            from robot_repair import repair_source
            repair=repair_source(self.root/'review/retarget'/row['id'],row,user)
            if repair is None:raise ValueError('此作业没有可精修的完整机器人轨迹')
            quality=repair[1]
        quality.setdefault('events',[]);quality.setdefault('checks',{})
        current=__import__('robot_dynamics').export_verified(quality) if original else export_verified(quality)
        quality['export_allowed']=bool(current)
        if not current:quality['status']='unverified' if quality.get('status')!='failed' else 'failed'
        quality['preview_scope']='Stored immutable acceptance report; previews never bypass current export gates.'
        smooth=meta.get('quality',{}).get('edit_smoothness')
        if smooth:
            quality['edit_smoothness']=smooth;quality['export_allowed']=quality['export_allowed'] and smooth['passed']
        accuracy=meta.get('quality',{}).get('edit_accuracy')
        if accuracy:
            quality['edit_accuracy']=accuracy;quality['export_allowed']=bool(quality['export_allowed'] and accuracy['passed'])
            if not quality['export_allowed']:quality['status']='failed'
        from robot_diagnostics import tracking_events
        quality['events'].extend(tracking_events(tracking))
        if tracking.get('passed') is False:quality['export_allowed']=False;quality['status']='failed'
        quality['source_tracking']=quality.get('tracking',tracking) if original or 'repair_recheck' in quality else dict(status='not_recomputed',reason='机器人编辑改变了原目标，保留原始报告，不冒充重新验证')
        locked=set(p['locked_joints']) if p else {m.joint(j).name for j in range(1,m.njnt) if 'wrist' in m.joint(j).name}
        joints=[dict(name=m.joint(j).name,index=int(m.jnt_qposadr[j]),min=float(m.jnt_range[j,0]),max=float(m.jnt_range[j,1]),locked=m.joint(j).name in locked,type='radian' if int(m.jnt_type[j])==3 else 'metre') for j in range(1,m.njnt)]
        return dict(joints=np.round(frames,6).tolist(),parents=(m.body_parentid[1:]-1).tolist(),names=[m.body(i).name for i in range(1,m.nbody)],fps=float(a['fps']),frames=len(q),qpos=q.tolist(),joint_controls=joints,quality=quality,version=version or s['head'],mapping=p['configuration']['mapping'] if p else {},coordinates='Preview Y up; edit offsets world X forward, Y left, Z up')
    def render_model(self,user,sid):
        _,row,a=self.load(user,sid,'original');m,_=self.model(user,row,a)
        from robot_editor_model import export
        return export(m)
    def validate_existing(self,user,value):
        # A zero-offset immutable candidate evaluates every frame without changing the source.
        session=self.create(user,value);sid=session['id'];_,_,arrays=self.load(user,sid,'original')
        if len(arrays['qpos'])<12:raise ValueError('完整验收至少需要 12 帧')
        return self.edit(user,sid,dict(request_id=value['request_id'],parent='original',start_frame=0,end_frame=12,
            keyframes=[dict(frame=6,joint_offsets={},body_offsets={},root_offset=[0,0,0])],pinned_bodies=[],frozen_joints=[]))
    def edit(self,user,sid,value):
        if not isinstance(value,dict):raise ValueError('编辑参数必须是对象')
        if set(value)-{'request_id','parent','start_frame','end_frame','keyframes','pinned_bodies','frozen_joints'}:raise ValueError('不支持的编辑参数')
        request=ident(value.get('request_id'));body={k:v for k,v in value.items() if k!='request_id'}
        # Validate everything before starting a worker; solve repeats validation.
        _,row,a=self.load(user,sid,value.get('parent'));m,p=self.model(user,row,a);validate_edit(m,p,a['qpos'],body)
        return self.enqueue(user,sid,request,body)

    def import_repair(self,user,sid,value):
        from robot_repair import imported_trajectory
        if not isinstance(value,dict) or set(value)!={'request_id','parent','npz_base64'}:raise ValueError('待复检参数不完整')
        request=ident(value['request_id'])
        parent=value['parent'] if value['parent']=='original' else ident(value['parent'])
        session,row,original=self.load(user,sid,'original')
        if row['status']!='failed':raise ValueError('仅待修复作业可导入复检')
        trajectory=imported_trajectory(value['npz_base64'],session,original)
        body=dict(parent=parent,imported_sha256=hashlib.sha256(trajectory.tobytes()).hexdigest())
        return self.enqueue(user,sid,request,body,trajectory)

    def enqueue(self,user,sid,request,body,trajectory=None):
        from review_access import active_account
        from g2h import resource_quota
        self.session(user,sid);self.reap()
        digest=hashlib.sha256(encoded(body).encode()).hexdigest()
        vid=secrets.token_hex(16);ticket='robot-edit:'+vid
        folder=self.folder/sid
        upload=folder/(vid+'.upload.npy')
        reserved=False
        try:
            with closing(self.db()) as d,d:
                d.execute('BEGIN IMMEDIATE')
                self._heal_session_head(d, sid)
                old=d.execute('SELECT digest FROM versions WHERE session=? AND request=?',(sid,request)).fetchone()
                if old:
                    if old['digest']!=digest:raise ValueError('重试必须使用同一编辑参数')
                    d.commit()
                    return self.detail(user,sid)
                if d.execute('SELECT head FROM sessions WHERE id=?',(sid,)).fetchone()[0]!=body['parent']:raise EditorVersionConflict('当前版本已改变，请刷新后再编辑')
                if d.execute("SELECT count(*) FROM versions WHERE status IN ('pending','running')").fetchone()[0]>=2:raise ValueError('编辑任务繁忙，请稍后重试')
                resource_quota.reserve(self.root,ticket,user['username'],{'retarget_seconds':125,'storage_bytes':20_000_000},active_account(self.root,user['username']))
                reserved=True
                folder.mkdir(mode=0o700,exist_ok=True)
                if trajectory is not None:
                    import numpy as np
                    np.save(upload,trajectory,allow_pickle=False)
                d.execute('INSERT INTO versions(id,session,parent,request,digest,status,body,quality,error,created,pid,process_identity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(vid,sid,body['parent'],request,digest,'pending',encoded(body),None,None,time.time(),None,None))
                d.execute('UPDATE sessions SET head=?,redo=? WHERE id=?',(vid,'[]',sid))
        except Exception:
            upload.unlink(missing_ok=True)
            if reserved:resource_quota.settle(self.root,ticket,{'retarget_seconds':0,'storage_bytes':0})
            raise
        try:
            with open(folder/(vid+'.log'),'w') as log:
                proc=subprocess.Popen([sys.executable,str(Path(__file__)),'--worker',str(self.root),sid,vid],stdout=log,stderr=log,start_new_session=True,env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
        except OSError:
            upload.unlink(missing_ok=True)
            resource_quota.settle(self.root,ticket,{'retarget_seconds':0,'storage_bytes':0})
            with closing(self.db()) as d,d:
                d.execute('BEGIN IMMEDIATE')
                d.execute("UPDATE versions SET status='failed',error='编辑进程启动失败' WHERE id=?",(vid,))
                self._rollback_failed_head(d, sid, vid)
            return self.detail(user,sid)
        import threading
        from g2h.capacity import identity
        try:
            with closing(self.db()) as d,d:
                d.execute("UPDATE versions SET pid=?,process_identity=? WHERE id=? AND status='pending' AND pid IS NULL",
                          (proc.pid,identity(proc.pid),vid))
        finally:
            threading.Thread(target=self.watch_worker,args=(proc,vid),daemon=True).start()
        return self.detail(user,sid)
    def move(self,user,sid,value):
        s=self.session(user,sid)
        if set(value)!={'head','action'} or value['action'] not in ('undo','redo','accept'):raise ValueError('无效版本操作')
        with closing(self.db()) as d,d:
            d.execute('BEGIN IMMEDIATE')
            self._heal_session_head(d, sid)
            s=dict(d.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
            if s['head']!=value['head']:raise EditorVersionConflict('版本已改变，请刷新')
            v=d.execute('SELECT * FROM versions WHERE id=? AND session=?',(s['head'],sid)).fetchone()
            if v and v['status'] in ('pending','running'):raise ValueError('请等待编辑完成')
            redo=json.loads(s['redo'])
            if value['action']=='accept':
                if not v or v['status']!='ready':raise ValueError('只能接受检查通过的候选')
                problem = _candidate_error(self.trajectory_path(sid,v['id']), _candidate_proof(v))
                if problem is not None:
                    raise problem()
                if not export_verified(json.loads(v['quality'])):raise ValueError('只能接受检查通过的候选')
                d.execute('UPDATE sessions SET accepted=? WHERE id=?',(s['head'],sid))
            elif value['action']=='undo':
                if not v:raise ValueError('已在原始版本')
                if v['status']!='failed':redo.append(s['head'])
                parent = self._valid_ancestor(d, sid, v['parent'])
                d.execute('UPDATE sessions SET head=?,redo=? WHERE id=?',(parent,encoded(redo),sid))
            else:
                head = None
                while redo:
                    candidate = redo.pop()
                    row_r = d.execute('SELECT status FROM versions WHERE id=? AND session=?', (candidate, sid)).fetchone()
                    if row_r is not None and row_r['status'] != 'failed':
                        head = candidate
                        break
                if head is None:raise ValueError('没有可重做的版本')
                d.execute('UPDATE sessions SET head=?,redo=? WHERE id=?',(head,encoded(redo),sid))
        return self.detail(user,sid)
    def download(self,user,sid,version):
        s=self.session(user,sid);ident(version)
        if s['accepted']!=version:raise ValueError('请先检查并接受此版本')
        self.checked_source(user,s)
        with closing(self.db()) as d:r=d.execute('SELECT quality,status,trajectory_sha256 FROM versions WHERE id=? AND session=?',(version,sid)).fetchone()
        if not r or r['status']!='ready' or not export_verified(json.loads(r['quality'])):raise ValueError('候选未通过检查，禁止导出')
        raw = _candidate_bytes(self.trajectory_path(sid,version))
        _read_candidate(raw, sid, version, _candidate_proof(r))
        return raw

def validate_edit(m,p,q,b):
    import numpy as np
    if set(b)!={'parent','start_frame','end_frame','keyframes','pinned_bodies','frozen_joints'}:raise ValueError('编辑参数不完整')
    a,z=b['start_frame'],b['end_frame']
    if type(a)!=int or type(z)!=int or not 0<=a<z<=len(q) or not 12<=z-a<=300:raise ValueError('选区需为 12–300 帧')
    names={m.joint(j).name:j for j in range(1,m.njnt)};bodies={m.body(j).name:j for j in range(1,m.nbody)}
    locked=set(p['locked_joints']) if p else {n for n in names if 'wrist' in n or any(x in n for x in ('finger','thumb','pinky'))}
    if any(int(m.jnt_type[j]) not in (2,3) for j in names.values()):raise ValueError('仅支持单轴机器人关节')
    for key,available in (('pinned_bodies',bodies),('frozen_joints',names)):
        if not isinstance(b[key],list) or len(b[key])>64 or any(not isinstance(n,str) for n in b[key]) or len(set(b[key]))!=len(b[key]) or any(n not in available for n in b[key]):raise ValueError('锁定部位无效')
    keys=b['keyframes']
    if not isinstance(keys,list) or not 1<=len(keys)<=16:raise ValueError('请设置 1–16 个关键帧')
    seen=set()
    for k in keys:
        if set(k)!={'frame','joint_offsets','body_offsets','root_offset'}:raise ValueError('关键帧字段无效')
        f=k['frame']
        if type(f)!=int or not a+3<=f<z-3 or f in seen:raise ValueError('关键帧需在选区内部，边界保留 3 帧过渡保护')
        seen.add(f)
        if not isinstance(k['joint_offsets'],dict) or not isinstance(k['body_offsets'],dict):raise ValueError('关键帧参数无效')
        for n,v in k['joint_offsets'].items():
            if n not in names or n in locked or n in b['frozen_joints'] or type(v) not in (int,float) or not np.isfinite(v) or abs(v)>(.5236 if int(m.jnt_type[names[n]])==3 else .1):raise ValueError('关节调整超出范围或关节已锁定')
        for n,v in list(k['body_offsets'].items())+[('__root__',k['root_offset'])]:
            if n!='__root__' and (n not in bodies or n in b['pinned_bodies']):raise ValueError('目标部位无效或已锁定')
            if not isinstance(v,list) or len(v)!=3 or any(type(x) not in (float,int) or not np.isfinite(x) for x in v) or np.linalg.norm(v)>.5:raise ValueError('位移不得超过 0.5 米')
    return locked

def solve_edit(m,p,q,fps,b):
    import numpy as np,mujoco as mj
    from scipy.optimize import least_squares
    from retarget_clearance import geom_pairs,configure_collision_backend
    from retarget_quality import bottom,collision_pairs
    locked=validate_edit(m,p,q,b)|set(b['frozen_joints']);a,z=b['start_frame'],b['end_frame'];out=q.copy();data=mj.MjData(m);configure_collision_backend(m)
    keys=sorted(b['keyframes'],key=lambda k:k['frame']);times=[a+2]+[k['frame'] for k in keys]+[z-3]
    joint_names=set().union(*(k['joint_offsets'] for k in keys));body_names=set().union(*(k['body_offsets'] for k in keys))
    def curve(field,name=None):
        values=[np.zeros(3) if field!='joint_offsets' else 0]+[(k[field].get(name,[0,0,0] if field=='body_offsets' else 0) if name else k[field]) for k in keys]+[np.zeros(3) if field!='joint_offsets' else 0]
        result=[]
        for f in range(a,z):
            i=min(max(np.searchsorted(times,f,side='right')-1,0),len(times)-2);t=np.clip((f-times[i])/(times[i+1]-times[i]),0,1);t=t*t*t*(10+t*(-15+6*t))
            result.append(np.asarray(values[i])*(1-t)+np.asarray(values[i+1])*t)
        return np.asarray(result)
    offsets={n:curve('joint_offsets',n) for n in joint_names};body_offsets={n:curve('body_offsets',n) for n in body_names};root=curve('root_offset')
    active=[j for j in range(1,m.njnt) if m.joint(j).name not in locked|joint_names];addresses=[int(m.jnt_qposadr[j]) for j in active]
    lower=np.array([m.jnt_range[j,0] for j in active]);upper=np.array([m.jnt_range[j,1] for j in active])
    data.qpos[:]=q[a];mj.mj_forward(m,data);pins={n:data.xpos[m.body(n).id].copy() for n in b['pinned_bodies']}
    pairs=geom_pairs(m,p['collision_pairs'])
    geoms=[g for g in range(m.ngeom) if m.geom_bodyid[g] and (m.geom_contype[g] or m.geom_conaffinity[g])];errors=[];events=[]
    for f in range(a+3,z-3):
        desired=q[f].astype(float).copy();desired[:3]+=root[f-a]
        for n,vs in offsets.items():desired[int(m.jnt_qposadr[m.joint(n).id])]+=vs[f-a]
        data.qpos[:]=q[f];mj.mj_forward(m,data);targets={n:data.xpos[m.body(n).id].copy()+vs[f-a] for n,vs in body_offsets.items()};targets.update(pins)
        if targets and addresses:
            def residual(x):
                data.qpos[:]=desired;data.qpos[addresses]=x;mj.mj_forward(m,data)
                return np.r_[np.concatenate([(data.xpos[m.body(n).id]-v)*100 for n,v in targets.items()]),(x-desired[addresses])*.1,
                    [max(0,.005-mj.mj_geomDistance(m,data,g,h,.1,None))*100 for g,h,_,_ in pairs],
                    [max(0,-bottom(m,data,g))*100 for g in geoms]]
            pad=np.minimum(1e-8,0.25*np.maximum(0.0,upper-lower))
            fit=least_squares(residual,np.clip(desired[addresses],lower+pad,upper-pad),bounds=(lower,upper),max_nfev=40)
            desired[addresses]=fit.x
        out[f]=desired;data.qpos[:]=out[f];mj.mj_forward(m,data)
        for n,v in targets.items():
            e=float(np.linalg.norm(data.xpos[m.body(n).id]-v));errors.append(e)
            if e>.01:events.append(dict(kind='edit_target',frame=f,end_frame=f+1,part=n,value=e,limit=.01,severity='error'))
    # Pinning also checks protected boundary frames; incompatible locks fail visibly.
    for f in list(range(a,a+3))+list(range(z-3,z)):
        data.qpos[:]=out[f];mj.mj_forward(m,data)
        for n,v in pins.items():
            e=float(np.linalg.norm(data.xpos[m.body(n).id]-v));errors.append(e)
            if e>.01:events.append(dict(kind='pinned_body',frame=f,end_frame=f+1,part=n,value=e,limit=.01,severity='error'))
    return out,dict(passed=not events,max_target_error_m=max(errors,default=0),limit_m=.01,events=events[:1000],outside_unchanged=bool(np.array_equal(q[:a],out[:a]) and np.array_equal(q[z:],out[z:])))

def worker(root,sid,vid):
    import signal,numpy as np
    from g2h import resource_quota
    from g2h.capacity import identity, reservation
    from retarget_quality import analyze,collision_pairs
    from retarget_clearance import validate
    from robot_repair import repair_source, recheck
    e=Editor(root);started=time.monotonic();charged=0
    upload=e.folder/sid/(vid+'.upload.npy')
    with closing(e.db()) as d, d:
        claimed = d.execute(
            "UPDATE versions SET status='running',pid=?,process_identity=? "
            "WHERE id=? AND session=? AND status='pending'", (os.getpid(), identity(), vid, sid),
        ).rowcount
        if not claimed:
            return
        s = dict(d.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
        v = dict(d.execute('SELECT * FROM versions WHERE id=? AND session=?',(vid,sid)).fetchone())
    def timeout(*_):raise TimeoutError('机器人编辑超过两分钟，已停止')
    signal.signal(signal.SIGALRM,timeout);signal.alarm(120)
    try:
        user=dict(username=s['owner'],role='user');body=json.loads(v['body'])
        imported='imported_sha256' in body
        _,row,a=e.load(user,sid,'original' if imported else v['parent'])
        m,profile=e.model(user,row,a)
        meta=json.loads(str(a['metadata']))
        repair=repair_source(e.root/'review/retarget'/row['id'],row,user)
        if row['status']=='failed' and repair is None:raise ValueError('待修复作业已不可用')
        fps=float(a['fps'])
        edit_profile=profile or dict(locked_joints=[m.joint(j).name for j in range(1,m.njnt) if 'wrist' in m.joint(j).name],collision_pairs=collision_pairs(m,row['robot']))
        with reservation(e.root,20_000_000,'robot-edit:'+vid) as space:
            accuracy=None
            if imported:
                if repair is None:raise ValueError('仅待修复作业可导入复检')
                q=np.load(upload,allow_pickle=False)
                if q.shape!=a['qpos'].shape or q.dtype!=np.float32 or hashlib.sha256(q.tobytes()).hexdigest()!=body['imported_sha256']:
                    raise ValueError('复检上传文件已改变')
            else:
                q,accuracy=solve_edit(m,edit_profile,a['qpos'],fps,body)
            if repair:
                quality=recheck(e.root,row,m,profile,q,fps,'upload' if imported else 'editor',
                                upright_required='upright_quality' in meta)
                # A correction may need a sharp delta to cancel a jump in the parent.
                # The repaired trajectory itself must pass absolute continuity.
                a.pop('costs',None)
            else:
                quality=analyze(m,q,fps,validate(m,q,edit_profile['collision_pairs']),profile=profile,robot=row['robot'])
                smooth=__import__('robot_edit_smoothness').analyze(m,a['qpos'],q,fps)
                quality['edit_smoothness']=smooth;quality['events'].extend(smooth['events'])
                quality['export_allowed']=bool(quality['export_allowed'] and smooth['passed'])
            if accuracy:
                quality['edit_accuracy']=accuracy;quality['events'].extend(accuracy['events'])
                quality['export_allowed']=bool(quality['export_allowed'] and accuracy['passed'] and accuracy['outside_unchanged'])
            if not quality['export_allowed']:quality['status']='failed'
            if 'original_tracking' not in meta:meta['original_tracking']=meta.get('tracking')
            meta['tracking']=quality.get('tracking',dict(status='not_recomputed',passed=None))
            meta['quality']=quality
            meta['robot_edit']=dict(version=VERSION,session=sid,candidate=vid,parent=v['parent'],
                                    source_job=s['job'],source_sha256=s['source_sha'],request=body)
            if repair:
                meta['validation']='Kinora repaired trajectory; see the new quality report.'
                meta['clearance']=quality['clearance']
                if 'upright_quality' in quality:meta['upright_quality']=quality['upright_quality']
            a['qpos']=q;a['metadata']=np.array(encoded(meta));path=e.folder/sid/(vid+'.npz')
            temp=e.folder/sid/(vid+'.tmp.npz')
            try:
                from retarget_cache import digest
                np.savez_compressed(temp,**a)
                trajectory_sha256 = digest(temp)
                os.replace(temp,path)
            finally:
                temp.unlink(missing_ok=True)
            charged=path.stat().st_size;space.commit(charged)
        with closing(e.db()) as d,d:d.execute('UPDATE versions SET status=?,quality=?,trajectory_sha256=? WHERE id=?',
                          ('ready' if quality['export_allowed'] else 'quarantined',encoded(quality),trajectory_sha256,vid))
    except BaseException as error:
        with closing(e.db()) as d,d:
            d.execute('BEGIN IMMEDIATE')
            d.execute("UPDATE versions SET status='failed',error=? WHERE id=?",(str(error)[:500],vid))
            Editor._rollback_failed_head(d,sid,vid)
    finally:
        signal.alarm(0);upload.unlink(missing_ok=True)
        resource_quota.settle(e.root,'robot-edit:'+vid,{'retarget_seconds':time.monotonic()-started,'storage_bytes':charged})

if __name__=='__main__':
    if len(sys.argv)==5 and sys.argv[1]=='--worker':worker(*sys.argv[2:])
