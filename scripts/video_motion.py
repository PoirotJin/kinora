"""Owner-scoped video jobs with durable observations and idempotent Kimodo segments."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
MAX_BYTES = 32*1024*1024
POOL = ThreadPoolExecutor(max_workers=2,thread_name_prefix='video-motion')
RUNNING = set()
LOCK = threading.Lock()


def runtime():
    return (Path(os.environ.get('MOTIONFARM_VIDEO_PYTHON',PROJECT/'.video-venv/bin/python')),
            Path(os.environ.get('MOTIONFARM_VIDEO_MODEL',PROJECT/'ckpts/video/pose_landmarker_heavy.task')))


def available():
    return all(path.is_file() for path in runtime())


def owner_folder(service,user):
    owner = hashlib.sha256(service.owner(user).encode()).hexdigest()
    return service.root/'review/video-motion'/owner


def write_json(path,value):
    with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as stream:
        json.dump(value,stream,ensure_ascii=False,allow_nan=False)
    os.replace(stream.name,path)


def _enqueue(service,user,folder):
    with LOCK:
        if folder not in RUNNING:
            RUNNING.add(folder)
            try:
                POOL.submit(run,service,user,folder)
            except Exception:
                RUNNING.discard(folder)
                raise


def submit(service,user,value):
    key = value.get('request_id')
    data = value.get('video_base64')
    if not isinstance(key,str) or not re.fullmatch('[a-zA-Z0-9_-]{8,80}',key) or not isinstance(data,str):
        raise ValueError('视频请求编号或文件无效')
    if not available() or not service._scene_enabled():
        raise ValueError('服务端未启用视频动作提取')
    if not 0<len(data)<=4*((MAX_BYTES+2)//3):
        raise ValueError('视频大小须在 32 MiB 以内')
    raw=base64.b64decode(data,validate=True)
    if not 0<len(raw)<=MAX_BYTES:
        raise ValueError('视频大小须在 32 MiB 以内')
    from g2h import resource_quota
    from g2h.capacity import reservation
    from review_access import active_account
    owner=service.owner(user)
    folder=owner_folder(service,user)/hashlib.sha256(key.encode()).hexdigest()
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    digest=hashlib.sha256(raw).hexdigest()
    with (folder/'submit.lock').open('a') as fp:
        fcntl.flock(fp,fcntl.LOCK_EX)
        path=folder/'job.json'
        if path.exists():
            job=json.loads(path.read_text())
            if job['source_sha256']!=digest:
                raise ValueError('此请求编号已用于其他视频')
        else:
            role=active_account(service.root,owner)
            ticket='video-upload:'+folder.name
            allowance=len(raw)+65536
            resource_quota.reserve(service.root,ticket,owner,{'storage_bytes':allowance},role)
            charged=0
            staged=None
            try:
                with reservation(service.root,allowance,ticket) as capacity:
                    with tempfile.NamedTemporaryFile(dir=folder,prefix='.video-',delete=False) as stream:
                        staged=Path(stream.name)
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staged,folder/'input.video')
                    staged=None
                    write_json(path,dict(id=folder.name,source_sha256=digest,status='queued'))
                    charged=len(raw)+path.stat().st_size
                    capacity.commit(charged)
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)
                resource_quota.settle(service.root,ticket,{'storage_bytes':charged})
            job=json.loads(path.read_text())
    if job['status'] not in ('done','failed'):
        _enqueue(service,user,folder)
    return job


def status(service,user,key):
    if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key):
        raise ValueError('无效视频任务编号')
    folder=owner_folder(service,user)/key
    path=folder/'job.json'
    if not path.is_file():
        raise KeyError('视频任务不存在')
    return json.loads(path.read_text())


def _clean_error(text, fallback='视频姿态提取失败'):
    cleaned = re.sub(r'^(?:Traceback.*\|)?(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*)+', '', str(text or '').strip()).strip()
    return cleaned or fallback


def run(service,user,folder):
    try:
        with (folder/'worker.lock').open('a') as fp:
            try:
                fcntl.flock(fp,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return
            job=json.loads((folder/'job.json').read_text())
            if job['status'] in ('done','failed'):
                return
            def update(**fields):
                job.update(fields)
                write_json(folder/'job.json',job)
            try:
                path=folder/'reference.json'
                if not path.exists():
                    update(status='tracking')
                    python,model=runtime()
                    with (folder/'observations.log').open('w') as log:
                        result=subprocess.run([str(python),str(PROJECT/'scripts/video_observations.py'),
                                               str(folder),str(model)],stdout=log,stderr=log,timeout=1200)
                    if result.returncode:
                        lines=[line.strip() for line in (folder/'observations.log').read_text(errors='replace').splitlines() if line.strip()]
                        raise ValueError(_clean_error(lines[-1] if lines else '', '视频姿态提取失败')[:300])
                manifest=json.loads(path.read_text())
                motions=[]
                for index,part in enumerate(manifest['segments']):
                    update(status='reconstructing',segment=index+1,segments=len(manifest['segments']),
                           frames=manifest['frames'],shots=manifest['shots'])
                    key=hashlib.sha256(f'{folder.name}:{index}'.encode()).hexdigest()
                    task=service.scene(user,dict(part['request'],request_id=key))
                    while not task['candidates']:
                        if any(op['status'] in ('failed','cancelled') for op in task['operations']):
                            raise ValueError(f'Kimodo 第 {index+1} 段重建失败')
                        time.sleep(4)
                        task=service.scene_status(user,task['id'])
                    motions.append(task['candidates'][0]['motion_id'])
                asset,quality=assemble(service,user,folder,manifest,motions)
                update(status='done',motion_id=asset['motion_id'],quality=quality)
            except Exception as exc:
                update(status='failed',error=_clean_error(exc, '视频动作提取失败')[:500])
            finally:
                if job.get('status') in ('done','failed'):
                    for artifact in ('input.video','observed.npz'):
                        (folder/artifact).unlink(missing_ok=True)
    finally:
        with LOCK:
            RUNNING.discard(folder)


def assemble(service,user,folder,manifest,motions):
    from g2h import resource_quota
    from g2h.capacity import reservation
    from g2h.human_rig import load
    from g2h.kimodo_adapter import fk
    from review_access import active_account
    from scipy.spatial.transform import Rotation
    rest,parents=load()
    pieces=[]
    for i,(part,mid) in enumerate(zip(manifest['segments'],motions)):
        arrays,_=service.assets.load(service.owner(user),mid)
        overlap=part.get('overlap',int(i>0))
        keep=part.get('keep_frames',part['end']-part['start'])
        pieces.append({key:arrays[key][overlap:overlap+keep]
                       for key in ('native_keypoints3d','smpl_poses','native_transl')})
    joined={k:np.concatenate([p[k] for p in pieces]) for k in pieces[0]}
    joined.update(fps=np.asarray(30),parents=parents,rest_joints=rest)
    if len(joined['native_transl'])!=manifest['frames']:
        raise ValueError('视频重建帧数与源时间轴不一致')
    with np.load(folder/'observed.npz') as obs:
        matrices=Rotation.from_rotvec(obs['smpl_poses'].reshape(-1,3)).as_matrix().reshape(-1,52,3,3)
        reference,_=fk(matrices,rest,parents)
        tracked=obs['confidence']>=.45
    if not tracked.any():
        raise ValueError('没有足够可靠的观测可验证视频重建')
    joints=joined['native_keypoints3d'][:,:22]
    error=np.linalg.norm((joints-joints[:,:1])-(reference[:,:22]-reference[:,:1]),axis=2)
    quality=dict(observed_mpjpe_m=float(error[tracked].mean()),reliable_fraction=float(tracked.mean()),
                 cut_steps_m=[float(np.linalg.norm(joints[s['start_frame']]-joints[s['start_frame']-1],axis=1).max())
                              for s in manifest['shots'][1:]])
    warnings=[]
    if quality['observed_mpjpe_m']>.15:
        warnings.append(f"视频姿态误差偏大（{quality['observed_mpjpe_m']:.3f}m > 0.15m）")
    max_step=max(quality['cut_steps_m'],default=0.)
    if max_step>.22:
        warnings.append(f"跨镜头跳变偏大（{max_step:.3f}m > 0.22m）")
    quality['passed']=not warnings
    if warnings:
        quality['warnings']=warnings
    owner=service.owner(user)
    role=active_account(service.root,owner)
    estimate=sum(int(np.asarray(array).nbytes) for array in joined.values())+32768
    ticket='video-asset:'+folder.name
    resource_quota.reserve(service.root,ticket,owner,{'storage_bytes':estimate},role)
    charged=0
    try:
        with reservation(service.root,estimate,ticket) as capacity:
            meta=service.assets.register(owner,joined,description='视频人体动作提取',
                     request_id='video:'+folder.name,provenance=dict(kind='kimodo_video',segments=motions,
                     shots=manifest['shots'],source_sha256=json.loads((folder/'job.json').read_text())['source_sha256'],quality=quality))
            charged=meta['artifact_bytes']
            capacity.commit(charged)
            return meta,quality
    finally:
        resource_quota.settle(service.root,ticket,{'storage_bytes':charged})
