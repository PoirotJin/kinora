"""Fail-closed model-based inverse dynamics and continuous torque-bounded joint-PD replay.

No floating-base wrench, state resets, invented motors or contact forces are used.
A pass is conditional on the supplied model/scene; it is not hardware certification.
"""
import copy,hashlib,json
import numpy as np
VERSION='robot-dynamics-v1'
def policy_fingerprint():
    from pathlib import Path
    from importlib.metadata import version
    path=Path(__file__).resolve();cfg=path.parents[1]/'data/dynamics/unitree_g1_23dof.json'
    return hashlib.sha256(path.read_bytes()+(cfg.read_bytes() if cfg.exists() else b'')+version('mujoco').encode()+np.__version__.encode()).hexdigest()
LIMITS=dict(root_force_bodyweight_ratio=.01,root_torque_bodyweight_m_ratio=.01,
            root_position_error_m=.02,root_rotation_error_rad=.05,joint_position_error=.05,
            timestep_seconds=.002)

def contract(model,profile=None):
    """Require named, explicit speed limits and their source; never infer hardware ratings."""
    cfg=(profile or {}).get('configuration',{}).get('dynamics')
    if not isinstance(cfg,dict):raise ValueError('缺少已注明来源的动力学配置和关节速度上限')
    speeds=cfg.get('joint_velocity_limits');names=[model.joint(j).name for j in range(1,model.njnt)]
    if not isinstance(speeds,dict) or set(speeds)!=set(names):raise ValueError('必须提供每个关节的速度上限，包括锁定关节')
    if any(type(v) not in (int,float) or not np.isfinite(v) or v<=0 for v in speeds.values()):raise ValueError('关节速度上限必须为正有限数值')
    if not isinstance(cfg.get('source'),str) or not 5<=len(cfg['source'])<=1000:raise ValueError('必须记录动力学参数来源')
    efforts=cfg.get('joint_effort_limits')
    if efforts is not None and (not isinstance(efforts,dict) or set(efforts)!=set(names) or any(type(v) not in (int,float) or not np.isfinite(v) or v<=0 for v in efforts.values())):raise ValueError('关节力矩/力上限无效')
    return cfg

def evaluate(model,qpos,fps,profile=None):
    import mujoco as mj
    q=np.asarray(qpos,float);fps=float(fps)
    result=dict(version=VERSION,policy_sha256=policy_fingerprint(),status='unverified',passed=False,events=[],thresholds=LIMITS.copy(),
        scope='Model-conditioned inverse dynamics and continuous inverse-feedforward + joint-PD simulation; no hardware/controller/thermal certification.')
    try:
        cfg=contract(model,profile)
        if q.ndim!=2 or q.shape[1]!=model.nq or not 3<=len(q)<=3600 or not np.isfinite(q).all() or not 1<=fps<=240 or len(q)/fps>120:raise ValueError('动力学轨迹格式或时长无效')
        if model.nq!=model.nv+1 or model.jnt_type[0]!=0 or np.any(model.jnt_type[1:]<2):raise ValueError('仅支持单浮动根和单轴关节')
        if np.any(np.abs(np.linalg.norm(q[:,3:7],axis=1)-1)>.01):raise ValueError('根四元数无效')
        if model.neq or model.ntendon or model.nmocap or model.nplugin:raise ValueError('暂不验证约束驱动、肌腱、外部插件或 mocap 施力')
        if not np.allclose(model.opt.gravity,[0,0,-9.81],atol=1e-6):raise ValueError('验收场景需使用标准重力 Z 向上')
        if np.any(model.body_gravcomp):raise ValueError('验收不能使用人工重力补偿')
        # NATIVECCD selects libccd rather than disabling collisions (existing geometry policy).
        if model.opt.disableflags & ~int(mj.mjtDisableBit.mjDSBL_NATIVECCD):raise ValueError('验收不能禁用接触、重力或其他物理约束')
        result['collision_backend']='libccd' if model.opt.disableflags & int(mj.mjtDisableBit.mjDSBL_NATIVECCD) else 'nativeccd'
        ground=[g for g in range(model.ngeom) if model.geom_bodyid[g]==0 and model.geom_type[g]==mj.mjtGeom.mjGEOM_PLANE and model.geom_contype[g] and model.geom_conaffinity[g]]
        if not ground:raise ValueError('模型缺少明确的碰撞地面，不能假定支撑力')
        if any(not np.isfinite(model.geom_friction[g]).all() or model.geom_friction[g,0]<=0 for g in ground):raise ValueError('地面摩擦参数无效')
        mass=float(model.body_mass.sum())
        if mass<=0 or not np.isfinite(model.body_inertia).all():raise ValueError('质量或惯性无效')
        if model.nu!=model.nv-6:raise ValueError('每个关节必须有一个真实定义的执行器，包括锁定关节')
        slots=[];bounds=[];seen=set()
        for a in range(model.nu):
            j=int(model.actuator_trnid[a,0]);gear=float(model.actuator_gear[a,0])
            if model.actuator_trntype[a]!=0 or j<=0 or j in seen or model.actuator_dyntype[a]!=0 or model.actuator_gaintype[a]!=0 or model.actuator_biastype[a]!=0 or model.actuator_gainprm[a,0]!=1 or not gear or np.any(model.actuator_gear[a,1:]):raise ValueError('目前只验证无耦合、固定增益为 1 的直接力矩/力执行器')
            seen.add(j);lo,hi=-np.inf,np.inf
            for limited,ranges in ((model.actuator_ctrllimited,model.actuator_ctrlrange),(model.actuator_forcelimited,model.actuator_forcerange)):
                if limited[a]:lo=max(lo,float(ranges[a,0]));hi=min(hi,float(ranges[a,1]))
            if model.jnt_actfrclimited[j]:
                r=sorted(model.jnt_actfrcrange[j]/gear);lo=max(lo,float(r[0]));hi=min(hi,float(r[1]))
            if cfg.get('joint_effort_limits'):
                limit=cfg['joint_effort_limits'][model.joint(j).name]/abs(gear);lo=max(lo,-limit);hi=min(hi,limit)
            if not np.isfinite([lo,hi]).all() or not lo<0<hi:raise ValueError('执行器缺少有限的双向输出限制：'+model.joint(j).name)
            slots.append((j,int(model.jnt_dofadr[j]),gear));bounds.append((lo,hi))
        if seen!=set(range(1,model.njnt)):raise ValueError('执行器未完整覆盖关节')
    except (ValueError,TypeError,IndexError) as exc:
        result['reason']=str(exc);return result
    m=copy.copy(model);m.opt.timestep=min(float(m.opt.timestep),LIMITS['timestep_seconds'])
    dt=1/fps;v=np.empty((len(q),m.nv));interval=np.empty((len(q)-1,m.nv))
    for i in range(len(q)-1):mj.mj_differentiatePos(m,interval[i],dt,q[i],q[i+1])
    v[0]=interval[0];v[-1]=interval[-1];v[1:-1]=(interval[:-1]+interval[1:])/2
    acc=np.gradient(v,dt,axis=0,edge_order=2);d=mj.MjData(m);controls=np.zeros((len(q),m.nu));events=[];failures=0
    metrics=dict(replay_actuator_utilization=0.,actuator_saturation_steps=0,root_force_ratio=0.,root_torque_ratio=0.,actuator_utilization=0.,velocity_utilization=0.,root_position_error_m=0.,root_rotation_error_rad=0.,joint_position_error=0.)
    def check(kind,frame,part,value,limit):
        nonlocal failures
        if not np.isfinite(value) or value>limit:
            failures+=1
            if len(events)<1000:events.append(dict(kind=kind,frame=int(frame),end_frame=int(frame)+1,part=part,value=float(value) if np.isfinite(value) else None,limit=float(limit),severity='error'))
    for f,pose in enumerate(q):
        mj.mj_resetData(m,d);d.qpos[:]=pose;d.qvel[:]=v[f];d.qacc[:]=acc[f];mj.mj_inverse(m,d)
        force=float(np.linalg.norm(d.qfrc_inverse[:3])/(mass*9.81));torque=float(np.linalg.norm(d.qfrc_inverse[3:6])/(mass*9.81))
        metrics['root_force_ratio']=max(metrics['root_force_ratio'],force);metrics['root_torque_ratio']=max(metrics['root_torque_ratio'],torque)
        check('dynamics_root_force',f,'root',force,LIMITS['root_force_bodyweight_ratio']);check('dynamics_root_torque',f,'root',torque,LIMITS['root_torque_bodyweight_m_ratio'])
        for a,(j,adr,gear) in enumerate(slots):
            name=m.joint(j).name;u=float(d.qfrc_inverse[adr]/gear);lo,hi=bounds[a];ratio=u/hi if u>=0 else u/lo
            controls[f,a]=np.clip(u,lo,hi);metrics['actuator_utilization']=max(metrics['actuator_utilization'],ratio)
            check('dynamics_actuator_limit',f,name,ratio,1.000001)
            speed=max(abs(float(v[f,adr])),abs(float(interval[max(0,f-1),adr])),abs(float(interval[min(f,len(interval)-1),adr])))/cfg['joint_velocity_limits'][name];metrics['velocity_utilization']=max(metrics['velocity_utilization'],speed);check('dynamics_velocity_limit',f,name,speed,1.000001)
    # Only replay an inverse-feasible reference. Never use resets or external root forces.
    steps=0
    if not failures:
        d=mj.MjData(m);d.qpos[:]=q[0];d.qvel[:]=v[0];mj.mj_forward(m,d)
        error=np.empty(m.nv);target=np.empty(m.nq);servo=np.empty(m.nv);matrix=np.empty((m.nv,m.nv));mj.mj_fullM(m,matrix,d.qM)
        inertia=np.maximum(np.diag(matrix),1e-8);kp=[];kd=[]
        for a,(j,adr,gear) in enumerate(slots):
            gain=min(abs(x) for x in bounds[a])*abs(gear)/.05;kp.append(gain);kd.append(2*np.sqrt(gain*inertia[adr]))
        result['controller']=dict(version='bounded-inverse-spd-v1',kp=kp,kd=kd,root_actuation=False,state_resets=False)
        for f in range(len(q)-1):
            n=int(np.ceil(dt/LIMITS['timestep_seconds']));m.opt.timestep=dt/n
            for k in range(n):
                alpha=k/n;target[:]=q[f];mj.mj_integratePos(m,target,interval[f],alpha*dt);mj.mj_differentiatePos(m,servo,1.,d.qpos,target)
                mj.mj_fullM(m,matrix,d.qM);feedback=np.zeros(m.nv);implicit=np.zeros(m.nv);feed=np.zeros(m.nv)
                for a,(j,adr,gear) in enumerate(slots):
                    feedback[adr]=kp[a]*(servo[adr]-m.opt.timestep*d.qvel[adr])+kd[a]*(interval[f,adr]-d.qvel[adr]);implicit[adr]=kd[a]*m.opt.timestep+kp[a]*m.opt.timestep**2;feed[adr]=((1-alpha)*controls[f,a]+alpha*controls[f+1,a])*gear
                acceleration=np.linalg.solve(matrix+np.diag(implicit),feed+feedback-d.qfrc_bias+d.qfrc_passive+d.qfrc_constraint)
                for a,(j,adr,gear) in enumerate(slots):
                    command=(feed[adr]+feedback[adr]-implicit[adr]*acceleration[adr])/gear;d.ctrl[a]=np.clip(command,*bounds[a]);metrics['actuator_saturation_steps']+=int(command!=d.ctrl[a]);metrics['replay_actuator_utilization']=max(metrics['replay_actuator_utilization'],float(d.ctrl[a]/bounds[a][1] if d.ctrl[a]>=0 else d.ctrl[a]/bounds[a][0]))
                mj.mj_step(m,d);steps+=1
                target[:]=q[f];mj.mj_integratePos(m,target,interval[f],(k+1)*m.opt.timestep)
                mj.mj_differentiatePos(m,error,1.,target,d.qpos)
                for key,value,limit in [('root_position_error_m',np.linalg.norm(error[:3]),LIMITS['root_position_error_m']),('root_rotation_error_rad',np.linalg.norm(error[3:6]),LIMITS['root_rotation_error_rad']),('joint_position_error',np.max(np.abs(error[6:]),initial=0),LIMITS['joint_position_error'])]:
                    metrics[key]=max(metrics[key],float(value));check('dynamics_'+key,f+1,'robot',float(value),limit)
                for j,adr,_ in slots:check('dynamics_velocity_limit',f+1,m.joint(j).name,abs(float(d.qvel[adr]))/cfg['joint_velocity_limits'][m.joint(j).name],1.000001)
                for j,adr,_ in slots:
                    if m.jnt_limited[j]:
                        value=float(d.qpos[m.jnt_qposadr[j]]);check('dynamics_joint_limit',f+1,m.joint(j).name,max(m.jnt_range[j,0]-value,value-m.jnt_range[j,1]),.02)
                for contact in d.contact:
                    bodies=[int(m.geom_bodyid[g]) for g in contact.geom]
                    if all(bodies):check('dynamics_self_collision',f+1,'robot',max(0.,-float(contact.dist)),0.)
                    else:check('dynamics_ground_penetration',f+1,'robot',max(0.,-float(contact.dist)),.03)
                if any(w.number for w in d.warning):check('dynamics_simulation_warning',f+1,'robot',1,0)
                if failures:break
            if failures:break
    result.update(status='passed' if not failures else 'failed',passed=not failures,events=events,failures=failures,metrics=metrics,frames=len(q),replay_steps=steps,reference_seconds=(len(q)-1)/fps,replayed_seconds=steps*m.opt.timestep,parameter_source=cfg['source'],configuration_sha256=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest())
    return result


def builtin_profile(robot):
    """Only the exact pinned G1 model is covered by the checked source contract."""
    if robot!='unitree_g1_23dof':return None
    from pathlib import Path
    from retarget_robots import model_path
    path=Path(__file__).resolve().parents[1]/'data/dynamics/unitree_g1_23dof.json'
    cfg=json.loads(path.read_text())
    if hashlib.sha256(model_path(robot).read_bytes()).hexdigest()!=cfg['model_sha256']:raise ValueError('G1 模型已改变，动力学参数需重新核对')
    return {'configuration':{'dynamics':cfg}}

def export_verified(quality):
    return bool(quality.get('export_allowed') and quality.get('dynamics',{}).get('version')==VERSION and quality.get('dynamics',{}).get('passed') is True and quality.get('dynamics',{}).get('policy_sha256')==policy_fingerprint())
