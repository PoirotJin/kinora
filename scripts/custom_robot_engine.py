"""Bounded CPU humanoid IK using pinned owner-private robot profiles."""
from retarget_limits import MAX_FRAMES, MAX_SECONDS
from pathlib import Path
import json
import sys
import time
import numpy as np
from scipy.spatial.transform import Rotation
PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'scripts'),str(PROJECT)]
VERSION='custom-humanoid-ik-v2'

def tracking_targets(model,profile,motion):
    import mujoco as mj
    from custom_robot_profiles import JOINTS
    from retarget_pose import body_quaternions,SEGMENTS
    points=np.asarray(motion['native_keypoints3d'],float)
    translation=np.asarray(motion['native_transl'],float)
    if points.ndim!=3 or points.shape[1:]!=(52,3) or translation.shape!=(len(points),3) or not 2<=len(points)<=MAX_FRAMES or float(motion['fps'])!=30 or not np.isfinite(points).all() or not np.isfinite(translation).all():raise ValueError(f'需要 2–{MAX_FRAMES} 帧、30 FPS 的有限 SMPL-H 数据')
    world=(points-points[:,:1]+translation[:,None])[:,:,[0,2,1]]*np.array([1,-1,1])
    rotations=Rotation.from_quat(body_quaternions(world),scalar_first=True)
    mapping=profile['configuration']['mapping'];labels=list(JOINTS);ids=[model.body(mapping[n]).id for n in labels]
    data=mj.MjData(model);mj.mj_forward(model,data);rest={k:data.xpos[model.body(v).id].copy() for k,v in mapping.items()}
    human_leg=sum(np.linalg.norm(world[0,JOINTS[b]]-world[0,JOINTS[a]]) for a,b in (('L_Hip','L_Knee'),('L_Knee','L_Ankle')))
    robot_leg=sum(np.linalg.norm(rest[b]-rest[a]) for a,b in (('L_Hip','L_Knee'),('L_Knee','L_Ankle')))
    if human_leg<.05:raise ValueError('源动作腿长退化')
    scale=robot_leg/human_leg
    root=(world[:,0]-world[0,0])*scale+rest['Pelvis']
    targets={k:root+rotations.apply(np.broadcast_to(rest[k]-rest['Pelvis'],root.shape)) for k in labels}
    for a,b in SEGMENTS+[('L_Ankle','L_Foot'),('R_Ankle','R_Foot')]:
        delta=world[:,JOINTS[b]]-world[:,JOINTS[a]];norm=np.linalg.norm(delta,axis=1)
        if (norm<1e-8).any():raise ValueError('源动作肢体长度退化')
        targets[b]=targets[a]+delta/norm[:,None]*np.linalg.norm(rest[b]-rest[a])
    return root, rotations, labels, ids, rest, np.stack([targets[k] for k in labels],axis=1), scale


def tracking_report(rms, max_errors, scale):
    total_rms = float(np.sqrt(np.mean(np.square(rms))))
    maximum = float(max(max_errors))
    return dict(rms_m=total_rms, max_m=maximum, per_frame_rms_m=rms, per_frame_max_m=max_errors,
                limits=dict(rms_m=.15,max_m=.35), passed=total_rms<=.15 and maximum<=.35,
                target='Morphology-adjusted mapped body positions',scale=scale)


def measure_tracking(model, profile, motion, trajectory):
    import mujoco as mj

    _, _, _, ids, _, targets, scale = tracking_targets(model, profile, motion)
    if len(trajectory) != len(targets):
        raise ValueError('复检轨迹与源动作帧数不匹配')
    data = mj.MjData(model)
    rms, maximum = [], []
    for pose, target in zip(trajectory, targets):
        data.qpos[:] = pose
        mj.mj_forward(model, data)
        errors = np.linalg.norm(data.xpos[ids] - target, axis=1)
        rms.append(float(np.sqrt(np.mean(errors**2))))
        maximum.append(float(errors.max()))
    return tracking_report(rms, maximum, scale)


def solve(model,profile,motion,parameters,progress=lambda *_:None):
    import mujoco as mj
    from scipy.optimize import least_squares
    from retarget_pose import SEGMENTS
    from retarget_clearance import geom_pairs,configure_collision_backend
    from retarget_quality import bottom
    configure_collision_backend(model)
    root, rotations, labels, ids, rest, targets, scale = tracking_targets(model, profile, motion)
    data = mj.MjData(model)
    locked=set(profile['locked_joints']);active=[j for j in range(1,model.njnt) if model.joint(j).name not in locked]
    addresses=[int(model.jnt_qposadr[j]) for j in active]
    lower=np.r_[[-.25]*3,[-.5]*3,[model.jnt_range[j,0] for j in active]]
    upper=np.r_[[.25]*3,[.5]*3,[model.jnt_range[j,1] for j in active]]
    pairs=geom_pairs(model,profile['collision_pairs'])
    geoms=[g for g in range(model.ngeom) if model.geom_bodyid[g] and (model.geom_contype[g] or model.geom_conaffinity[g])]
    qprev=model.qpos0.copy();out=[];rms=[];max_errors=[];costs=[];evaluations=[]
    previous=np.r_[np.zeros(6),qprev[addresses]]
    for frame in range(len(targets)):
        def pose(x):
            q=model.qpos0.copy();q[:3]=root[frame]+x[:3]
            q[3:7]=(Rotation.from_rotvec(x[3:6])*rotations[frame]).as_quat(scalar_first=True)
            q[addresses]=x[6:];return q
        def residual(x):
            q=pose(x);data.qpos[:]=q;mj.mj_forward(model,data)
            delta=data.xpos[ids]-targets[frame]
            # Quadratic + quartic position loss: an isolated foot/hand error
            # must not be cheaply traded against many small landmark errors.
            # 15 cm is the existing RMS budget; export gates stay independent.
            tail=np.sqrt(1+2*(np.linalg.norm(delta,axis=1)/.15)**2)
            match=(delta*tail[:,None]).ravel()*np.sqrt(parameters['laplacian_weight'])
            limb=[]
            for a,b in SEGMENTS:
                ia,ib=labels.index(a),labels.index(b)
                length=max(np.linalg.norm(rest[b]-rest[a]),.01)
                limb.extend(((data.xpos[ids[ib]]-data.xpos[ids[ia]])-(targets[frame,ib]-targets[frame,ia]))/length)
            collision=[max(0,.01-mj.mj_geomDistance(model,data,a,b,.1,None))*50 for a,b,_,_ in pairs]
            floor=[max(0,-bottom(model,data,g))*50 for g in geoms]
            return np.r_[match,np.asarray(limb)*np.sqrt(parameters['limb_direction_weight']),
                x[:6]*np.sqrt(parameters['root_orientation_weight']),
                (x[6:]-previous[6:])*np.sqrt(parameters['smooth_weight']),collision,floor]
        pad=np.minimum(1e-8,0.25*np.maximum(0.0,upper-lower))
        fit=least_squares(residual,np.clip(previous,lower+pad,upper-pad),bounds=(lower,upper),max_nfev=60,ftol=1e-5,xtol=1e-5,gtol=1e-5)
        q=pose(fit.x);data.qpos[:]=q;mj.mj_forward(model,data)
        errors=np.linalg.norm(data.xpos[ids]-targets[frame],axis=1)
        out.append(q);rms.append(float(np.sqrt(np.mean(errors**2))));max_errors.append(float(errors.max()));costs.append(float(fit.cost));evaluations.append(int(fit.nfev))
        previous=fit.x.copy();qprev=q;progress(frame+1,len(targets))
    tracking=tracking_report(rms,max_errors,scale)
    tracking['evaluations']=evaluations
    return np.asarray(out),np.asarray(costs),tracking

def run(root,key,mid,robot):
    from contextlib import closing
    import mujoco as mj
    from review_retarget import Retarget
    from custom_robot_profiles import read
    from review_access import check
    root=Path(root);queue=Retarget(root)
    with closing(queue.db()) as db:row=dict(db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone())
    if (row['motion_id'],row['robot'],row['status'])!=(mid,robot,'running'):raise ValueError('Job identity mismatch')
    execution=json.loads(row['execution']);pinned=execution['custom_robot']
    profile,path=read(root,{'username':row['owner']},robot,pinned['version'])
    if profile['model_sha256']!=pinned['model_sha256']:raise ValueError('Pinned model changed')
    if profile['mujoco_version']!=mj.__version__:raise ValueError('MuJoCo 版本变化，请创建新机器人配置')
    check(root,mid)
    if mid.startswith('mimp'):
        from gadget_imports import Imports
        record=Imports(root).read(mid,row['owner'])
    elif mid.startswith('ma_'):
        from g2h.motion_assets import MotionAssets
        arrays,meta=MotionAssets(root).load(row['owner'],mid)
        record=dict(motion_id=mid,text=meta.get('description',''),duration_s=float(len(arrays['native_keypoints3d'])/30.0),motion=arrays)
    else:
        from semantic_retarget import source_record
        record=source_record(root,mid)
    model=mj.MjModel.from_binary_path(str(path));folder=queue.folder/key;folder.mkdir(exist_ok=True)
    qpos,costs,tracking=solve(model,profile,record['motion'],execution['parameters'],
        lambda done,total:queue.update(key,done=done,total=total,message=f'自定义机器人逐帧求解 {done}/{total}'))
    (folder/'tracking.json').write_text(json.dumps(tracking))
    metadata=dict(robot=robot,robot_label=robot,motion_id=mid,text=record.get('text',''),adapter_version=VERSION,
        robot_sha256=profile['model_sha256'],custom_robot=pinned,execution=execution,tracking=tracking,
        coordinates='Z up, metres, root quaternion wxyz',validation='Offline kinematics only; dynamics unverified',created=time.time())
    if not tracking['passed']:
        metadata['engine_warning'] = '机器人跟踪误差偏大'
    np.savez_compressed(folder/'result.npz',qpos=qpos.astype(np.float32),fps=np.array(30),joint_names=np.array(profile['joint_names']),costs=costs,metadata=np.array(json.dumps(metadata)))
    data=mj.MjData(model);frames=[]
    for q in qpos:
        data.qpos[:]=q;mj.mj_forward(model,data);frames.append(data.xpos[1:].copy()[:,[0,2,1]]*np.array([1,1,-1]))
    (folder/'preview.json').write_text(json.dumps(dict(joints=np.round(frames,5).tolist(),parents=(model.body_parentid[1:]-1).tolist(),names=[model.body(i).name for i in range(1,model.nbody)],fps=30,frames=len(qpos),duration_s=len(qpos)/30,metadata=metadata)))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    for name in ('root','job','motion','robot'):p.add_argument('--'+name,required=True)
    a=p.parse_args();run(a.root,a.job,a.motion,a.robot)
