"""Versioned executable parameters; solver safety constraints are not user configurable."""
from contextlib import closing
import hashlib,json,math
VERSION='retarget-parameters-v1'
FIELDS={
 'smooth_weight':dict(default=.2,minimum=0.,maximum=10.,label='时间平滑权重'),
 'laplacian_weight':dict(default=10.,minimum=.1,maximum=100.,label='形状匹配权重'),
 'root_orientation_weight':dict(default=40.,minimum=1.,maximum=200.,label='躯干朝向权重'),
 'limb_direction_weight':dict(default=2.,minimum=.1,maximum=20.,label='肢体方向权重')}

def schema():return dict(version=VERSION,parameters=FIELDS,locked=['joint_limits','self_collision','wrist_neutral','head_mapping'],note='Only these numeric objective weights execute; limits/collision/wrist policy stay enforced.')

def validate(value):
 if not isinstance(value,dict) or set(value)-set(FIELDS):raise ValueError('不支持的可执行参数：'+', '.join(sorted(set(value)-set(FIELDS))) if isinstance(value,dict) else '参数必须为对象')
 result={}
 for key,spec in FIELDS.items():
  v=value.get(key,spec['default'])
  if type(v) not in (int,float) or not math.isfinite(v) or not spec['minimum']<=v<=spec['maximum']:raise ValueError(key+' 超出允许范围')
  result[key]=float(v)
 return result

def resolve(root,user,robot,value):
 from review_personal import identifier
 from review_access import Forbidden
 if 'preset_id' in value and not value['preset_id']:raise ValueError('方案 ID 不能为空')
 if 'preset_version' in value and 'preset_id' not in value:raise ValueError('方案版本必须指定方案 ID')
 if value.get('preset_id') and 'parameters' in value:raise ValueError('方案与直接参数不能同时提供')
 preset=None;parameters=value.get('parameters',{})
 if value.get('preset_id'):
  import sqlite3
  key=identifier(value['preset_id']);version=value.get('preset_version')
  if type(version) is not int or version<1:raise ValueError('请选择明确的方案版本')
  with closing(sqlite3.connect(str(root/'review/reviews.sqlite3'))) as db:
   row=db.execute('SELECT owner FROM personal_presets WHERE id=?',(key,)).fetchone()
   if not row or row[0]!=user['username']:raise Forbidden('方案不存在或不属于当前用户')
   row=db.execute('SELECT body FROM personal_preset_versions WHERE preset_id=? AND version=?',(key,version)).fetchone()
  if not row:raise ValueError('方案版本不存在')
  p=json.loads(row[0])
  if p['robot']!=robot:raise ValueError('方案机器人与目标机器人不一致')
  parameters=p['parameters'];preset=dict(id=key,version=version,name=p['name'],owner=user['username'])
 effective=validate(parameters)
 result=dict(schema_version=VERSION,parameters=effective,preset=preset)
 parent=value.get('parent_job_id')
 if parent is not None:
  from review_retarget import Retarget
  prior=Retarget(root).get(parent,user)
  if prior['owner']!=user['username']:raise Forbidden('只能复现自己的作业')
  result['parent_job_id']=parent
 result['sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 return result


def execution_matches(previous,current):
    old=json.loads(previous);new=json.loads(current)
    if not old:
        return new.get('preset') is None and new.get('parameters')==validate({}) and not new.get('parent_job_id')
    return old==new
