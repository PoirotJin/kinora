"""MotionFarm SMPL-H to Holosoma Humanoid adapter; CPU, offline only."""
from retarget_limits import MAX_FRAMES, MAX_SECONDS
import argparse
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
CONTINUITY_RETRY_WEIGHT = 4.0
CONTINUITY_RETRY_TASK = 'continuity-retry'


def install_cvxpy_fallback():
    import cvxpy as cp
    if getattr(cp.Problem.solve, '_motionfarm_fallback', False):
        return
    original_solve = cp.Problem.solve
    good_statuses = {cp.OPTIMAL, cp.OPTIMAL_INACCURATE}

    def fallback_solve(self, *args, **kwargs):
        requested = kwargs.get('solver')
        candidates = [cp.CLARABEL, cp.OSQP, cp.SCS]
        if requested in candidates:
            order = [requested] + [solver for solver in candidates if solver != requested]
        elif requested is not None:
            order = [requested] + candidates
        else:
            order = candidates
        last_exc = None
        for idx, solver in enumerate(order):
            call_kwargs = dict(kwargs, solver=solver)
            try:
                value = original_solve(self, *args, **call_kwargs)
            except cp.error.SolverError as exc:
                last_exc = exc
                continue
            if self.status in good_statuses or idx == len(order) - 1:
                return value
        if last_exc is not None:
            raise last_exc
        return original_solve(self, *args, **kwargs)

    fallback_solve._motionfarm_fallback = True
    cp.Problem.solve = fallback_solve

def world_points(motion,template):
    import numpy as np
    p=np.asarray(motion['native_keypoints3d'],dtype=float)
    t=np.asarray(motion['native_transl'],dtype=float)
    if p.ndim!=3 or p.shape[1:]!=(52,3) or not 2<=len(p)<=MAX_FRAMES or t.shape!=(len(p),3):
        raise ValueError(f'重定向支持 2–{MAX_FRAMES} 帧 SMPL-H 动作')
    world=p[:,:22]-p[:,0:1]+template[0]+t[:,None]
    if not np.isfinite(world).all():raise ValueError('动作包含非有限数值')
    # Proper rotation (det=+1): Human +Y up -> Holosoma +Z up.
    return world[:,:,[0,2,1]]*np.array([1,-1,1])


def transform_scene_objects(objects, travel_scale=1.0, offset_xz=(0.0, 0.0)):
    """Copy human_interaction_v1 objects and apply X/Z retarget alignment, keeping Y and heading intact."""
    import copy
    dx, dz = float(offset_xz[0]), float(offset_xz[1])
    scale = float(travel_scale)
    result = copy.deepcopy(objects)
    if scale == 1.0 and dx == 0.0 and dz == 0.0:
        return result
    for obj in result:
        cx, cy, cz = obj['center_m']
        obj['center_m'] = [float(cx) * scale + dx, float(cy), float(cz) * scale + dz]
        if 'trajectory' in obj:
            for pt in obj['trajectory']:
                px, py, pz = pt['center_m']
                pt['center_m'] = [float(px) * scale + dx, float(py), float(pz) * scale + dz]
    return result

def run(root,key,mid,frame_limit=None,robot='unitree_g1_23dof'):
    print('Loading CPU retarget dependencies',flush=True)
    import numpy as np
    import mujoco
    sys.path.insert(0,str(PROJECT))
    sys.path.insert(0,str(PROJECT/'third_party/holosoma'))
    install_cvxpy_fallback()
    from g2h.farm_index import read_motion
    from review_retarget import Retarget
    from review_access import check
    from holosoma_retargeting.config_types.retargeting import RetargetingConfig
    from holosoma_retargeting.config_types.robot import RobotConfig
    from holosoma_retargeting.config_types.data_type import MotionDataConfig
    from holosoma_retargeting.config_types.task import TaskConfig
    from holosoma_retargeting.config_types.retargeter import RetargeterConfig, SelfCollisionConfig
    from holosoma_retargeting.src.interaction_mesh_retargeter import InteractionMeshRetargeter
    from holosoma_retargeting.examples.robot_retarget import main
    from retarget_robots import robot_spec, model_path as robot_model_path, joint_mapping
    spec=robot_spec(robot)
    root=Path(root);queue=Retarget(root);folder=queue.folder/key
    folder.mkdir(exist_ok=True)
    from retarget_parameters import validate
    with closing(queue.db()) as db: job=db.execute('SELECT execution FROM jobs WHERE id=?',(key,)).fetchone()
    execution=json.loads(job['execution']) if job else {}
    parameters=validate(execution.get('parameters',{}))
    check(root,mid)
    with closing(sqlite3.connect(root/'prompts.sqlite3')) as db:record=read_motion(db,root,mid)
    motion=record['motion'];fps=float(motion['fps'])
    if fps!=30:raise ValueError('当前 Holosoma 适配器要求 30 FPS，避免静默改变动作速度')
    from g2h.human_rig import motion_rig
    template,_=motion_rig(motion)
    world=world_points(motion,template)
    if frame_limit:world=world[:frame_limit]
    queue.update(key,total=len(world),message='正在逐帧求解')
    # Fixed rest-pose height, never estimate height from a crouched animation.
    height=float(np.ptp(template[:22,1])+.15)
    if not 1.2<height<2.2:raise ValueError('人体模板身高异常')
    package=PROJECT/'third_party/holosoma/holosoma_retargeting'
    os.chdir(package)
    model_path=robot_model_path(robot)
    model=mujoco.MjModel.from_xml_path(str(model_path))
    if model.nq!=spec['dof']+7 or model.nv!=spec['dof']+6:raise ValueError('机器人自由度不匹配')
    clearance_pairs=[]
    if True:
        from retarget_clearance import body_pairs, SOLVER_CLEARANCE_M, validate as validate_clearance, install_normal_fallback, install_clearance_refinement, configure_collision_backend, install_collision_backend, install_clearance_objective
        configure_collision_backend(model)
        install_collision_backend(InteractionMeshRetargeter)
        from retarget_quality import collision_pairs
        clearance_pairs=collision_pairs(model,robot)
        install_normal_fallback(InteractionMeshRetargeter)
        install_clearance_refinement(InteractionMeshRetargeter)
        from retarget_clearance import install_bounded_contact_recovery
        install_bounded_contact_recovery(InteractionMeshRetargeter)
    joint_names=[model.joint(i).name for i in range(1,model.njnt)]
    from retarget_pose import fit_head_joints, head_angles, head_rotations, install_pose_targets, morphology_targets
    from holosoma_retargeting.config_types.data_type import JOINTS_MAPPINGS, SMPLX_DEMO_JOINTS
    mapping=joint_mapping(spec,JOINTS_MAPPINGS)
    adapted,travel_scale=morphology_targets(world,model,mapping,SMPLX_DEMO_JOINTS)
    target_scene = None
    scene_objects = None
    if 'scene_spec_json' in motion:
        from g2h.human_interaction import decode, measure_arrivals
        scene = decode(motion['scene_spec_json'], len(motion['native_transl']))
        offset_xz = np.zeros(2, dtype=float)
        if any('tolerance_m' in point for point in scene['waypoints']):
            target_scene = scene
            # A rigid translation preserves gait when one anchor suffices. Several
            # distinct anchors require the original metric travel between them.
            pins = [point for point in scene['waypoints'] if 'tolerance_m' in point]
            coords = np.array([point['xz_m'] for point in pins])
            if np.allclose(coords, coords[0], atol=1e-6, rtol=0):
                adapted[:, :, :2] += coords[0] * [1., -1.] * (1. - travel_scale)
                offset_xz = np.asarray(coords[0], dtype=float) * (1. - travel_scale)
            else:
                adapted[:, :, :2] += world[:, :1, :2] - adapted[:, :1, :2]
                travel_scale = 1.
        scene_objects = transform_scene_objects(scene['objects'], travel_scale, offset_xz)
    warm={};cost={};lower={str(i):-1. for i in range(3,7)};upper={str(i):1. for i in range(3,7)}
    for jid in range(1,model.njnt):
        name=model.joint(jid).name;adr=int(model.jnt_qposadr[jid]);lo,hi=model.jnt_range[jid]
        value=.25 if 'knee' in name else (-.12 if 'ankle_pitch' in name else 0.)
        if 'elbow' in name:value=-.4 if hi<=.01 else .4
        if clearance_pairs and 'shoulder_roll' in name:value=.30 if name.startswith('left') else -.30
        if clearance_pairs and 'pelvic_roll' in name:value=.06 if name.startswith('left') else -.06
        if clearance_pairs and 'ankle_roll' in name:value=-.06 if name.startswith('left') else .06
        warm[adr]=float(np.clip(value,lo,hi))
        weight=.8 if 'ankle_pitch' in name else (.3 if 'ankle_roll' in name else (.2 if 'waist' in name else (5. if 'head_' in name else (.05 if 'wrist' in name else 0.))))
        if weight:cost[str(adr)]=weight
        if 'wrist' in name:lower[str(adr)]=upper[str(adr)]=float(np.clip(0.,lo,hi))
    install_pose_targets(InteractionMeshRetargeter,world,warm_start=warm if robot!='unitree_g1_23dof' else None,root_orientation_weight=parameters['root_orientation_weight'],limb_direction_weight=parameters['limb_direction_weight'])
    if clearance_pairs:
        batch_path=root/'exports/amp70_unitree_g1_23dof_clearance_v2/batch.json'
        separate_tracks=batch_path.exists() and mid in {r['motion_id'] for r in json.loads(batch_path.read_text())['motions']}
        install_clearance_objective(InteractionMeshRetargeter,separate_tracks=separate_tracks)
    if os.environ.get('MOTIONFARM_CLEARANCE_DIAGNOSTIC')=='1':
        solve=InteractionMeshRetargeter.solve_single_iteration
        def diagnostic_solve(self,**kwargs):
            try:return solve(self,**kwargs)
            except RuntimeError:
                q=kwargs['q_locked'].copy();q[self.q_a_indices]=kwargs['q_a_n_last']
                np.savez(folder/'failed_pose.npz',qpos=q,previous=kwargs['q_t_last'])
                ground_j,ground_d=self._update_jacobians_and_phis_from_q(q)
                self_j,self_d=self._compute_self_collision_constraints(kwargs['frame_idx'])
                details={'frame':kwargs['frame_idx'],'foot_sticking':str(kwargs['foot_sticking']),
                    'self':[(str(k),v,float(np.linalg.norm(self_j[k]))) for k,v in sorted(self_d.items(),key=lambda x:x[1])[:20]],
                    'ground':[(str(k),v,float(np.linalg.norm(ground_j[k]))) for k,v in sorted(ground_d.items(),key=lambda x:x[1])[:20]]}
                (folder/'failed_constraints.json').write_text(json.dumps(details,indent=2))
                raise
        InteractionMeshRetargeter.solve_single_iteration=diagnostic_solve
    retry = None

    def progress(self,done,total):
        if done==1 or done==total or done%5==0:
            stage = '连续性重算' if retry is not None else '逐帧求解'
            queue.update(key,done=done,total=total,message=f'{stage} {done}/{total}')
    InteractionMeshRetargeter.progress_callback=progress
    config=RetargetingConfig(task_type='robot_only',robot=spec['family'],data_format='smplx',
        task_name='input',data_path=folder,save_dir=folder,
        robot_config=RobotConfig(robot_type=spec['family'],robot_dof=spec['dof'],robot_height=spec['height'],robot_urdf_file=str(model_path),
            manual_cost=cost,manual_lb=lower,manual_ub=upper),
        motion_data_config=MotionDataConfig(data_format='smplx',robot_type=spec['family'],joints_mapping=mapping),
        retargeter=RetargeterConfig(smooth_weight=parameters['smooth_weight'],laplacian_weight=parameters['laplacian_weight'],self_collision=SelfCollisionConfig(enable=bool(clearance_pairs),pairs=clearance_pairs,tolerance=SOLVER_CLEARANCE_M if clearance_pairs else 0.02)),
        task_config=TaskConfig(ground_range=(-3.,3.)))
    head_joints = [(i, model.joint(name)) for i, name in enumerate(('head_yaw_joint', 'head_pitch_joint'))
                   if name in joint_names]
    head_method = 'fixed'
    if head_joints:
        # Joint positions alone do not identify head yaw. Use local neck/head rotations.
        poses = np.asarray(motion['smpl_poses'], dtype=float).reshape(-1, 52, 3)[:len(world)]
        if robot == 'unitree_g1_23dof':
            if len(head_joints) != 2:
                raise ValueError('Humanoid 头部关节配置不完整')
            limits = np.asarray([joint.range for _, joint in head_joints])
            angles = fit_head_joints(head_rotations(poses), limits)
            head_method = 'quaternion-slerp-bounded-yaw-pitch-v1'
        else:
            angles = head_angles(poses)
            head_method = 'SMPL neck/head local yaw/pitch'

    def solve(solver_config):
        result_path = solver_config.save_dir / f'{solver_config.task_name}.npz'
        # Coordinates already use robot link lengths. Holosoma replaces this
        # input file with its robot-only result.
        np.savez(result_path, global_joint_positions=adapted, height=spec['height'])
        main(solver_config)
        with np.load(result_path, allow_pickle=False) as result:
            candidate = np.asarray(result['qpos'], dtype=float)
            costs = np.asarray(result['costs'])
        if candidate.shape != (len(world), model.nq) or not np.isfinite(candidate).all():
            raise ValueError('求解结果形状或数值异常')
        for i, joint in head_joints:
            candidate[:, joint.qposadr[0]] = np.clip(angles[:, i], *joint.range)
        if not np.isfinite(candidate).all():
            raise ValueError('头部映射产生无效数值')
        norms = np.linalg.norm(candidate[:, 3:7], axis=1)
        if np.any(norms < .5):
            raise ValueError('无效根姿态四元数')
        candidate[:, 3:7] /= norms[:, None]
        return candidate, costs

    def within_joint_limits(candidate):
        joints=candidate[:,model.jnt_qposadr[1:]]
        lower,upper=model.jnt_range[1:].T
        return not (np.any(joints < lower-.02) or np.any(joints > upper+.02))

    qpos, costs = solve(config)
    if (robot in ('unitree_g1_23dof', 'unitree_g1_23dof')
            and parameters == validate({}) and not execution.get('preset')
            and not execution.get('parent_job_id') and within_joint_limits(qpos)):
        from robot_diagnostics import temporal

        # Check the same float32 trajectory the quality gate will receive.
        initial_timing = temporal(model, qpos.astype(np.float32), fps)
        if not initial_timing['passed']:
            retry = dict(smooth_weight=CONTINUITY_RETRY_WEIGHT, selected=False,
                         initial_temporal=initial_timing)
            queue.update(key, done=0, total=len(world), message='时间连续性未通过，正在提高时间平滑权重重新求解')
            retry_config = replace(
                config, task_name=CONTINUITY_RETRY_TASK,
                retargeter=replace(config.retargeter, smooth_weight=CONTINUITY_RETRY_WEIGHT),
            )
            try:
                candidate, candidate_costs = solve(retry_config)
                if not within_joint_limits(candidate):
                    raise ValueError('关节越界超过容差')
            except (RuntimeError, ValueError) as exc:
                retry['error'] = str(exc)[:500]
            else:
                retry['temporal'] = temporal(model, candidate.astype(np.float32), fps)
                if retry['temporal']['passed']:
                    qpos, costs = candidate, candidate_costs
                    retry['selected'] = True
            finally:
                (folder / f'{CONTINUITY_RETRY_TASK}.npz').unlink(missing_ok=True)
    rejection=None if within_joint_limits(qpos) else '关节越界超过容差'
    clearance=None
    if clearance_pairs:
        clearance=validate_clearance(model,qpos,clearance_pairs)
        (folder/'clearance.json').write_text(json.dumps(clearance,ensure_ascii=False,indent=2))
        if not clearance['passed']:
            rejection='自碰撞/净空检查未通过'
    metadata=dict(robot=robot,robot_label=spec['label'],dof=spec['dof'],robot_note=spec['note'],motion_id=mid,text_id=record.get('text_id'),text=record['text'],
        adapter_version='holosoma-multirobot-v4-clearance',created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        robot_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(world.astype('<f8').tobytes()).hexdigest(),
        coordinates=f"Z up, metres, root quaternion wxyz; qpos=[xyz,wxyz,{spec['dof']} joints]",
        head_method=head_method,
        validation='Offline kinematic retargeting, not dynamics or hardware validation',height=height,
        pose_method='robot segment lengths and hip/shoulder offsets; source torso orientation and limb direction objectives',
        travel_scale=travel_scale,pose_weights={'root_quaternion':parameters['root_orientation_weight'],'segment_direction':parameters['limb_direction_weight']},execution=execution)
    if scene_objects is not None:
        metadata['scene_objects'] = scene_objects
    if target_scene is not None:
        xz = qpos[:, :2].astype(np.float32) * [1., -1.]
        arrivals = measure_arrivals(xz, target_scene)
        metadata['scene_arrivals'] = arrivals
        if any(not row['passed'] for row in arrivals):
            rejection = rejection or '机器人轨迹偏离场景目标点'
    metadata['hand_method']='No hand/finger retargeting; wrist joints fixed at neutral where present'
    metadata['clearance']=clearance
    if retry is not None:
        metadata['continuity_retry'] = retry
    if rejection:
        metadata['engine_warning'] = rejection
    data=mujoco.MjData(model);points=[]
    for pose in qpos:
        data.qpos[:]=pose;mujoco.mj_forward(model,data)
        points.append(data.xpos[1:].copy())
    # Web viewers use +Y up, reverse the source conversion.
    points=np.asarray(points)[:,:,[0,2,1]]*np.array([1,1,-1])
    parents=(model.body_parentid[1:]-1).tolist()
    names=[model.body(i).name for i in range(1,model.nbody)]
    tmp=folder/'result.tmp.npz'
    np.savez_compressed(tmp,qpos=qpos.astype(np.float32),fps=np.array(fps),joint_names=np.array(joint_names),
        metadata=np.array(json.dumps(metadata,ensure_ascii=False)),costs=costs)
    os.replace(tmp,folder/'result.npz')
    preview=dict(joints=points.round(5).tolist(),parents=parents,names=names,fps=fps,frames=len(points),duration_s=len(points)/fps,metadata=metadata)
    tmp=folder/'preview.tmp';tmp.write_text(json.dumps(preview,ensure_ascii=False));os.replace(tmp,folder/'preview.json')
    (folder/'input.npz').unlink(missing_ok=True)
    queue.update(key,done=len(world),total=len(world),message='结果已保存')
    print('RETARGET_OK',qpos.shape,flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--job',required=True)
    parser.add_argument('--motion',required=True);parser.add_argument('--frames',type=int)
    parser.add_argument('--robot',default='unitree_g1_23dof')
    args=parser.parse_args();run(args.root,args.job,args.motion,args.frames,args.robot)
