"""Owner-checked, versioned motion evidence. Unknown checks never count as passed."""
from contextlib import closing
import datetime,hashlib,json,time
from zipfile import BadZipFile
from zlib import error as CompressionError
import numpy as np
VERSION='motion-quality-v3'

def setup(db):
 db.executescript('''CREATE TABLE IF NOT EXISTS quality_reports(motion_id TEXT NOT NULL,version TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(motion_id,version));
 CREATE INDEX IF NOT EXISTS quality_status ON quality_reports(status,motion_id);''')

def source_quality(motion,root_offset,record=None):
 p=np.asarray(motion['native_keypoints3d'],float);tr=np.asarray(motion['native_transl'],float);fps=float(motion['fps'])
 if p.ndim!=3 or p.shape[1:]!=(52,3) or not 2<=len(p)<=3600 or tr.shape!=(len(p),3) or not 1<=fps<=240:raise ValueError('动作形状不支持质量检查')
 finite=bool(np.isfinite(p).all() and np.isfinite(tr).all())
 if not finite:return dict(version=VERSION,status='failed',checks={'finite':False},metrics={},scope='Human skeleton screening only')
 world=p-p[:,:1]+np.asarray(root_offset)+tr[:,None]
 feet=world[:,[7,8,10,11]];ground=feet[:,:,1].min(axis=1);contact=feet[:-1,:,1]<.04
 speed=np.linalg.norm(np.diff(feet[:,:,[0,2]],axis=0),axis=2)*fps
 slide=float(speed[contact].mean()) if contact.any() else None
 acceleration=np.diff(tr,n=2,axis=0)*fps*fps
 metrics=dict(frames=len(p),fps=fps,duration_s=len(p)/fps,floor_joint_penetration_m=float(max(0,-ground.min())),contact_slide_proxy_mps=slide,root_acceleration_max_mps2=float(np.linalg.norm(acceleration,axis=1).max()) if len(acceleration) else 0.)
 checks=dict(finite=True,floor_joint_proxy=metrics['floor_joint_penetration_m']<=.03,foot_slide_proxy=None if slide is None else slide<=.35,root_acceleration_proxy=metrics['root_acceleration_max_mps2']<=25.,robot_joint_limits=None,surface_collision=None,dynamics=None)
 issue_frames={
  'floor_joint_proxy':np.flatnonzero(ground<-.03).tolist(),
  'foot_slide_proxy':np.flatnonzero(np.any(contact & (speed>.35),axis=1)).tolist(),
  'root_acceleration_proxy':(np.flatnonzero(np.linalg.norm(acceleration,axis=1)>25)+1).tolist(),
 }
 from g2h.body_quality import analyze as analyze_body
 body=analyze_body(record or dict(motion=motion,text='',topic=''))
 if body.get('metrics',{}).get('intent') in ('moving_surface','non_locomotion'):
  checks['foot_slide_proxy']=None;issue_frames['foot_slide_proxy']=[]
 checks.update(body['checks']);issue_frames.update(body.get('issue_frames',{}));metrics['body_quality']=body
 return dict(issue_frames=issue_frames,frame_index_base=0,version=VERSION,status='issues' if False in checks.values() else 'incomplete',checks=checks,metrics=metrics,thresholds=dict(ground_m=0,contact_height_m=.04,joint_penetration_m=.03,slide_mps=.35,root_acceleration_mps2=25),scope='Human skeleton proxies in HY Y-up coordinates. Joint centres are not collision surfaces. Screened does not mean robot-safe or human-approved.')

def record_for(store,mid,user):
 from gadget_library import detail
 detail(store,mid,user)
 if mid.startswith('mimp'):
  from gadget_imports import Imports
  return Imports(store.root).read(mid,user['username'])
 from g2h.farm_index import read_motion
 with closing(store.catalog()) as db:return read_motion(db,store.root,mid)

def report(store,mid,user):
 from gadget_library import detail
 info=detail(store,mid,user)
 with closing(store.reviews()) as db:
  setup(db);row=db.execute('SELECT body FROM quality_reports WHERE motion_id=? AND version=?',(mid,VERSION)).fetchone()
 if row:quality=json.loads(row[0]);record=None
 else:
  record=record_for(store,mid,user)
  template=np.fromfile(store.assets/'j_template.bin',dtype=np.float32).reshape(-1,3)
  quality=source_quality(record['motion'],template[0],record)
  with closing(store.reviews()) as db,db:db.execute('INSERT OR REPLACE INTO quality_reports VALUES(?,?,?,?,?)',(mid,VERSION,quality['status'],json.dumps(quality,allow_nan=False),time.time()))
 if record is None:record=record_for(store,mid,user)
 lineage=dict(motion_id=mid,owner=info.get('owner'),request_id=info.get('request_id'),created_utc=record.get('created_utc'),seed=record.get('inference_seed',record.get('seed')),allocation_seed=record.get('seed'),effective_prompt=record.get('text',info.get('text')),generator=record.get('provenance',{}),coordinates=record.get('coordinates'))
 # Hash numeric source arrays, explicitly distinguished from serialized record/file hashes.
 digest=hashlib.sha256()
 for key in sorted(record['motion']):
  a=np.asarray(record['motion'][key])
  if a.dtype.kind not in 'fiu':continue
  digest.update(key.encode());digest.update(str(a.dtype).encode());digest.update(str(a.shape).encode());digest.update(a.tobytes())
 lineage['numeric_motion_sha256']=digest.hexdigest()
 request=info.get('request_id')
 if request:
  from review_custom import Custom
  with closing(Custom(store.root).db()) as db:
   row=db.execute('SELECT description,prompt,prompt_mode,seed,lineage,inference_seed,rewrite_model,rewrite_meta FROM requests WHERE id=? AND owner=?',(request,user['username'])).fetchone()
  if row:lineage.update(original_prompt=json.loads(row[7]).get('original_description') or row[0],effective_prompt=row[1],prompt_mode=row[2],request_seed=row[5] if row[5] is not None else row[3],parent=json.loads(row[4]),rewrite=dict(mode=row[2],performed=row[2]=='translate',model=row[6],metadata=json.loads(row[7])))
 from motion_operations import quality_levels,evidence
 info_for_levels=store.detail(mid) if not mid.startswith('mimp') else info
 return dict(levels=quality_levels(info_for_levels,quality),calibration=evidence(store.root,mid),motion_id=mid,generation_status=info['state'],quality=quality,human_review=info.get('review'),provenance=lineage,reproducibility='A matching seed/parameters provides a replay recipe; exact numerical identity also requires the same checkpoint and runtime.')

def _retarget_evidence_object(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('重定向验收资料格式无效')
    json.dumps(value, allow_nan=False)
    return value


def retarget_report(queue, key, user):
    row = queue.get(key, user)
    folder = queue.folder / key
    quality = {'status': 'not_checked', 'version': None}
    provenance = None
    provenance_reason = None
    if row.get('result_corrupt'):
        reason = row['export_reason']
        quality = {'status': 'unavailable', 'version': None, 'reason': reason}
        provenance_reason = reason
    else:
        try:
            quality = _retarget_evidence_object((folder / 'quality.json').read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            quality = {'status': 'unavailable', 'version': None, 'reason': '重定向验收文件无法读取'}
        result = folder / 'result.npz'
        if result.is_file():
            try:
                with np.load(result, allow_pickle=False) as data:
                    provenance = _retarget_evidence_object(str(data['metadata']))
            except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile, CompressionError, RecursionError):
                provenance_reason = '重定向结果的来源信息无法读取'
    return dict(job=row, quality=quality, provenance=provenance, provenance_reason=provenance_reason,
                execution=json.loads(row.get('execution', '{}')))


def imported_view(store,mid,user,mode='detail'):
 """Owner-only imported sources use the same bounded numerical preview cache."""
 from gadget_imports import Imports
 imports=Imports(store.root);item=imports.get(mid,user['username'])
 if mode=='detail':
  return dict(motion_id=mid,topic='本地导入',state='done',owner=item['owner'],location=True,
              storage_tier='import',prompt={'text':item['text']},review=None,history=[],
              history_next_after=None,screening=None,machine_review=None,read_only=True)
 def load():
  record=imports.read(mid,user['username']);record['created_utc']=datetime.datetime.fromtimestamp(item['created'],datetime.timezone.utc).isoformat();return record
 meta,world,wire=store.preview_cache.get((mid,item['sha256']),mid,load)
 if mode=='binary':return wire
 if mode=='thumbnail':return {**meta,'joints':world[len(world)//2].round(5).tolist()}
 return {**meta,'joints':world.round(5).tolist()}
