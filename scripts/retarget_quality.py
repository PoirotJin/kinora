"""Versioned, bounded kinematic checks on robot collision surfaces."""
import os
from retarget_limits import MAX_FRAMES, MAX_SECONDS
VERSION='robot-quality-v3'
PAIR_VERSION='selected-surfaces-v1'


def retarget_requires_dynamics():
    return os.environ.get('KINORA_RETARGET_REQUIRE_DYNAMICS', '0') != '0'


def retarget_export_verified(quality):
    if not isinstance(quality, dict) or quality.get('version') != VERSION or quality.get('export_allowed') is not True:
        return False
    if not retarget_requires_dynamics() and quality.get('dynamics_required') is False:
        return True
    dynamics = quality.get('dynamics')
    if not isinstance(dynamics, dict) or dynamics.get('passed') is not True:
        return False
    from robot_dynamics import export_verified
    return export_verified(quality)


CHECK_LABELS = {
    'finite': '轨迹数值',
    'root_quaternion': '根姿态',
    'joint_limits': '关节限位',
    'wrist_neutral': '手腕中立位',
    'surface_collision': '自碰撞净空',
    'ground_surface': '地面穿透',
    'foot_slide_proxy': '支撑脚滑动',
    'temporal_continuity': '时间连续性',
    'dynamics': '动力学',
    'tracking': '姿态跟踪',
    'upright_profile': '直立姿态与足部约束',
    'direction_semantics': '动作方向',
}
TEMPORAL_LABELS = {
    'root_step': ('根位置单帧位移', 'm'),
    'root_acceleration': ('根位置加速度', 'm/s²'),
    'root_rotation': ('根姿态单帧旋转', 'rad'),
    'joint_step': ('关节单帧变化', 'rad'),
}


class DraftCandidateError(ValueError):
    """A complete engine result is available in quarantine for diagnosis."""


def reject_draft(folder, qpos, fps, joint_names, metadata, reason):
    import json
    import numpy as np

    target = folder / 'quarantine'
    target.mkdir(exist_ok=True)
    temporary = target / 'result.tmp.npz'
    try:
        np.savez_compressed(temporary, qpos=np.asarray(qpos, dtype=np.float32), fps=np.array(fps),
                            joint_names=np.array(joint_names), metadata=np.array(json.dumps(metadata, ensure_ascii=False)))
        os.replace(temporary, target / 'result.npz')
    finally:
        temporary.unlink(missing_ok=True)
    raise DraftCandidateError(reason)


def failure_summary(quality):
    failed = [
        CHECK_LABELS.get(name, name)
        for name, passed in quality['checks'].items() if passed is False
    ]
    if quality['checks'].get('tracking') is not False and quality.get('tracking', {}).get('passed') is False:
        failed.append(CHECK_LABELS['tracking'])
    events = quality.get('temporal', {}).get('events', [])
    if events and quality['checks'].get('temporal_continuity') is False:
        worst = max(events, key=lambda event: event['value'] / event['limit'])
        label, unit = TEMPORAL_LABELS[worst['kind']]
        part = f" {worst['part']}" if worst['kind'] == 'joint_step' else ''
        detail = (f"第 {worst['frame'] + 1} 帧{part} {label} "
                  f"{worst['value']:.3g} {unit}（上限 {worst['limit']:.3g} {unit}）")
    else:
        detail = ''
    if failed:
        reason = '、'.join(failed) + '未通过'
    else:
        reason = '质量验证未完成'
    if quality['checks'].get('dynamics') is None:
        reason += '，动力学未验证'
    if detail:
        reason += '。' + detail
    return reason + '。结果已隔离，禁止正式导出'


def collision_pairs(model,robot):
    if False:
        from retarget_clearance import body_pairs
        return body_pairs(model)
    if robot!='unitree_g1_23dof':raise ValueError('Unknown collision mapping')
    names={model.body(int(model.geom_bodyid[g])).name for g in range(model.ngeom) if model.geom_contype[g] or model.geom_conaffinity[g]}
    legs={side:{n for n in names if n.startswith(side+'_') and any(k in n for k in ('knee','ankle'))} for side in ('left','right')}
    pairs={(a,b) for a in legs['left'] for b in legs['right']}
    trunk={n for n in names if n in ('pelvis_contour_link','torso_link') or any(k in n for k in ('hip_','knee'))}
    for side,other in [('left','right'),('right','left')]:
        arm={n for n in names if n.startswith(side+'_') and any(k in n for k in ('elbow','hand'))}
        opposite={n for n in names if n.startswith(other+'_') and any(k in n for k in ('shoulder','elbow','hand'))}
        pairs.update((a,b) for a in arm for b in trunk|opposite)
    if not all(legs.values()) or len(pairs)<20:raise ValueError('Incomplete G1 collision surfaces')
    return sorted({tuple(sorted(p)) for p in pairs})

def bottom(model,data,g):
    """World Z minimum of the configured collision surface, not a joint centre."""
    import mujoco
    import numpy as np
    kind=model.geom_type[g];size=model.geom_size[g];rot=data.geom_xmat[g].reshape(3,3);z=data.geom_xpos[g,2]
    if kind==mujoco.mjtGeom.mjGEOM_MESH:
        mesh=int(model.geom_dataid[g]);v=model.mesh_vert[model.mesh_vertadr[mesh]:model.mesh_vertadr[mesh]+model.mesh_vertnum[mesh]]
        # Project each vertex onto world Z without a mixed-precision BLAS call.
        return float((np.einsum('ij,j->i', v, rot[2]) + z).min())
    if kind==mujoco.mjtGeom.mjGEOM_BOX:return float(z-np.abs(rot[2])@size)
    if kind==mujoco.mjtGeom.mjGEOM_SPHERE:return float(z-size[0])
    if kind==mujoco.mjtGeom.mjGEOM_CAPSULE:return float(z-size[0]-size[1]*abs(rot[2,2]))
    if kind==mujoco.mjtGeom.mjGEOM_ELLIPSOID:return float(z-np.linalg.norm(rot[2]*size))
    if kind==mujoco.mjtGeom.mjGEOM_CYLINDER:return float(z-size[1]*abs(rot[2,2])-size[0]*np.linalg.norm(rot[2,:2]))
    return None

def analyze(model,qpos,fps,clearance,profile=None,robot=None,*,require_dynamics=True):
    import mujoco
    import numpy as np
    qpos=np.asarray(qpos,float);fps=float(fps)
    if qpos.ndim!=2 or qpos.shape[1]!=model.nq or not 2<=len(qpos)<=MAX_FRAMES or not np.isfinite(qpos).all() or not np.isfinite(fps) or not 1<=fps<=240 or len(qpos)/fps>MAX_SECONDS:
        raise ValueError('Invalid robot trajectory')
    violation=0.;wrist_error=0.;events=[]
    for j in range(1,model.njnt):
        if model.jnt_type[j] not in (mujoco.mjtJoint.mjJNT_HINGE,mujoco.mjtJoint.mjJNT_SLIDE):raise ValueError('Unsupported articulated joint')
        q=qpos[:,model.jnt_qposadr[j]];lo,hi=model.jnt_range[j]
        if model.jnt_limited[j]:
            violation=max(violation,float(np.max(lo-q)),float(np.max(q-hi)))
            for frame in np.flatnonzero((q<lo-.02)|(q>hi+.02))[:50]:
                events.append(dict(kind='joint_limit',frame=int(frame),end_frame=int(frame)+1,
                                   part=model.joint(j).name,value=float(max(lo-q[frame],q[frame]-hi)),
                                   limit=.02,severity='error'))
        name=model.joint(j).name
        if profile and name in profile['locked_joints']:wrist_error=max(wrist_error,float(np.abs(q-model.qpos0[model.jnt_qposadr[j]]).max()))
        elif not profile and 'wrist' in name:wrist_error=max(wrist_error,float(np.abs(q-np.clip(0,lo,hi)).max()))
    geoms=[g for g in range(model.ngeom) if model.geom_bodyid[g] and (model.geom_contype[g] or model.geom_conaffinity[g])]
    feet=[g for g in geoms if any(s in (model.geom(g).name+' '+model.body(int(model.geom_bodyid[g])).name) for s in ('ankle','foot'))]
    if profile:
        foot_names={profile['configuration']['mapping'][k] for k in ('L_Foot','R_Foot')}
        feet=[g for g in geoms if model.body(int(model.geom_bodyid[g])).name in foot_names]
    data=mujoco.MjData(model);positions=[];heights=[];minimum=0.;complete=bool(geoms)
    for frame,q in enumerate(qpos):
        data.qpos[:]=q;mujoco.mj_forward(model,data)
        lows={g:bottom(model,data,g) for g in geoms}
        complete=complete and all(v is not None for v in lows.values())
        minimum=min(minimum,*[v for v in lows.values() if v is not None])
        if len(events)<1000:
            for g,v in lows.items():
                if v is not None and v<-.03:events.append(dict(kind='ground',frame=frame,end_frame=frame+1,part=model.body(int(model.geom_bodyid[g])).name,value=float(-v),limit=.03,severity='error'))
        positions.append([data.geom_xpos[g].copy() for g in feet]);heights.append([lows[g] if lows[g] is not None else float('nan') for g in feet])
    slide=None
    if feet:
        h=np.asarray(heights);contact=(h[:-1]<.03)&(h[1:]<.03)
        speed=np.linalg.norm(np.diff(np.asarray(positions)[:,:,:2],axis=0),axis=2)*fps
        if contact.any():
            slide=float(speed[contact].mean())
            for f,g in np.argwhere(contact&(speed>.35))[:100]:events.append(dict(kind='foot_slide',frame=int(f)+1,end_frame=int(f)+2,part=model.body(int(model.geom_bodyid[feet[g]])).name,value=float(speed[f,g]),limit=.35,severity='error' if slide>.35 else 'warning'))
    checks=dict(finite=True,root_quaternion=bool(np.all(np.abs(np.linalg.norm(qpos[:,3:7],axis=1)-1)<.01)),joint_limits=violation<=.02,wrist_neutral=wrist_error<=1e-5,
        surface_collision=clearance.get('passed') if clearance else None,ground_surface=minimum>=-.03 if complete else None,foot_slide_proxy=None if slide is None else slide<=.35,dynamics=None)
    from robot_diagnostics import temporal
    timing=temporal(model,qpos,fps);checks['temporal_continuity']=timing['passed'];events.extend(timing['events'])
    for e in (clearance or {}).get('examples',[]):events.append(dict(kind='collision',frame=e['frame'],end_frame=e['frame']+1,part=' / '.join(e['bodies']),value=e['distance_m'],limit=.005,severity='error'))
    from robot_dynamics import evaluate,builtin_profile
    dynamics=evaluate(model,qpos,fps,profile or builtin_profile(robot));checks['dynamics']=dynamics['passed'] if dynamics['status']!='unverified' else None;events.extend(dynamics['events'])
    required=('finite','root_quaternion','joint_limits','wrist_neutral','surface_collision','ground_surface','temporal_continuity')
    if require_dynamics:
        required += ('dynamics',)
    passed=all(checks[k] is True for k in required) and checks['foot_slide_proxy'] is not False
    status = ('model_dynamics_pass' if require_dynamics else 'kinematics_pass') if passed else ('unverified' if require_dynamics and dynamics['status']=='unverified' else 'failed')
    return dict(version=VERSION,dynamics=dynamics,dynamics_required=require_dynamics,events=sorted(events,key=lambda e:e['frame'])[:1000],temporal=timing,status=status,export_allowed=passed,checks=checks,
        metrics=dict(joint_limit_violation_rad=violation,wrist_error_rad=wrist_error,surface_penetration_m=-minimum,contact_slide_proxy_mps=slide),
        thresholds=dict(joint_limit_rad=.02,ground_penetration_m=.03,contact_height_m=.03,slide_proxy_mps=.35),
        clearance=clearance,scope='Selected-surface kinematics plus versioned model-conditioned dynamics and continuous feedforward replay. Not hardware/controller certification.')


def quarantine(folder,reason,status='failed'):
    import json,os
    from pathlib import Path
    folder=Path(folder);target=folder/'quarantine';target.mkdir(exist_ok=True)
    for name in ('result.npz','result.checked.npz','preview.json','input.npz','continuity-retry.npz','failed_pose.npz'):
        path=folder/name
        if path.exists():os.replace(path,target/name)
    path=folder/'quality.json'
    value=json.loads(path.read_text()) if path.exists() else dict(version=VERSION,checks={})
    value.update(status=status,export_allowed=False,reason=reason[:500])
    path.write_text(json.dumps(value,allow_nan=False,indent=2))
