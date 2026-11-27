"""Accelerated Kinora retargeting engine for built-in humanoid robots with Holosoma fallback."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / 'scripts') not in sys.path:
    sys.path.insert(0, str(PROJECT / 'scripts'))
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from retarget_limits import MAX_FRAMES


def _load_accel():
    try:
        import kinora_retarget_accel as accel
        return accel if getattr(accel, 'is_available', lambda: False)() else None
    except Exception:
        return None


def _within_joint_limits(model, candidate: np.ndarray) -> bool:
    joints = candidate[:, model.jnt_qposadr[1:]]
    lower, upper = model.jnt_range[1:].T
    return not (np.any(joints < lower - 0.02) or np.any(joints > upper + 0.02))


def _fallback_to_standard(root, key, mid, frame_limit, robot, reason: str):
    print(f'Fast engine fallback ({reason}); invoking standard solver', flush=True)
    from retarget_engine import run as standard_run
    return standard_run(root, key, mid, frame_limit, robot)


def run(root, key, mid, frame_limit=None, robot='unitree_g1_23dof'):
    import mujoco
    from g2h.farm_index import read_motion
    from g2h.human_rig import motion_rig
    from review_access import check
    from review_retarget import Retarget
    from retarget_clearance import validate as validate_clearance
    from retarget_engine import world_points
    from retarget_parameters import validate as validate_params
    from retarget_pose import fit_head_joints, head_angles, head_rotations
    from retarget_quality import collision_pairs
    from retarget_robots import model_path as robot_model_path, robot_spec
    from robot_diagnostics import temporal

    spec = robot_spec(robot)
    root = Path(root)
    queue = Retarget(root)
    folder = queue.folder / key
    folder.mkdir(exist_ok=True)

    with closing(queue.db()) as db:
        job = db.execute('SELECT execution FROM jobs WHERE id=?', (key,)).fetchone()
    execution = json.loads(job['execution']) if job else {}
    parameters = validate_params(execution.get('parameters', {}))

    no_fallback = os.environ.get('KINORA_ACCEL_NO_FALLBACK') == '1'
    accel = _load_accel()
    if accel is None:
        if no_fallback:
            raise RuntimeError('kinora_retarget_accel is not installed')
        return _fallback_to_standard(root, key, mid, frame_limit, robot, 'runtime_unavailable')

    if spec['family'] not in accel.SUPPORTED_FAMILIES:
        if no_fallback:
            raise ValueError(f"Unsupported family: {spec['family']}")
        return _fallback_to_standard(root, key, mid, frame_limit, robot, 'unsupported_family')

    # Custom parameter overrides or preset tuning use the parametric Holosoma solver
    if (parameters != validate_params({}) or execution.get('preset') or execution.get('parent_job_id')) and not no_fallback:
        return _fallback_to_standard(root, key, mid, frame_limit, robot, 'custom_parameters')

    check(root, mid)
    with closing(sqlite3.connect(root / 'prompts.sqlite3')) as db:
        record = read_motion(db, root, mid)
    if record.get('scene_pipeline') == 'g1_native':
        raise ValueError('原生 G1 轨迹需要匹配的 29 自由度 G1 自定义机器人配置')

    motion = record['motion']
    fps = float(motion['fps'])
    if fps != 30:
        raise ValueError('当前重定向适配器要求 30 FPS，避免静默改变动作速度')

    template, _ = motion_rig(motion)
    world = world_points(motion, template)
    if frame_limit:
        world = world[:frame_limit]
    t_frames = len(world)
    if not 2 <= t_frames <= MAX_FRAMES:
        raise ValueError(f'重定向支持 2–{MAX_FRAMES} 帧 SMPL-H 动作')

    height = float(np.ptp(template[:22, 1]) + 0.15)
    if not 1.2 < height < 2.2:
        raise ValueError('人体模板身高异常')

    queue.update(key, done=0, total=t_frames, message='正在快速求解重定向')

    package = PROJECT / 'third_party/holosoma/holosoma_retargeting'
    prev_cwd = os.getcwd()
    os.chdir(package)
    try:
        mpath = robot_model_path(robot)
        model = mujoco.MjModel.from_xml_path(str(mpath))
    finally:
        os.chdir(prev_cwd)

    if model.nq != spec['dof'] + 7 or model.nv != spec['dof'] + 6:
        raise ValueError('机器人自由度不匹配')

    kp = np.asarray(motion['native_keypoints3d'], dtype=np.float64)[:t_frames]
    tr = np.asarray(motion['native_transl'], dtype=np.float64)[:t_frames]
    device = os.environ.get('KINORA_ACCEL_DEVICE') or None

    t0 = time.perf_counter()
    res = accel.retarget_human52(
        kp,
        tr,
        spec['family'],
        src_fps=fps,
        device=device,
        return_canonical=False,
    )
    solve_s = time.perf_counter() - t0

    joint_names = [model.joint(i).name for i in range(1, model.njnt)]
    src_name_to_idx = {name: idx for idx, name in enumerate(res['joint_names'])}

    qpos = np.zeros((t_frames, model.nq), dtype=np.float64)
    qpos[:, 0:3] = np.asarray(res['root_pos'], dtype=np.float64)
    # Match retarget_engine world_points frame-0 template[0] XY offset
    tpl_zup_xy = np.array([float(template[0, 0]), -float(template[0, 2])], dtype=np.float64)
    qpos[:, 0:2] += tpl_zup_xy[None, :]
    qpos[:, 3:7] = np.asarray(res['root_quat_wxyz'], dtype=np.float64)

    q_src = np.asarray(res['q_full'], dtype=np.float64)
    for jid in range(1, model.njnt):
        name = model.joint(jid).name
        adr = int(model.jnt_qposadr[jid])
        lo, hi = model.jnt_range[jid]
        if name in src_name_to_idx:
            qpos[:, adr] = np.clip(q_src[:, src_name_to_idx[name]], lo, hi)
        else:
            qpos[:, adr] = float(np.clip(0.0, lo, hi))

    # Fit head joints identically to retarget_engine.py (if present)
    head_joints = [
        (i, model.joint(name))
        for i, name in enumerate(('head_yaw_joint', 'head_pitch_joint'))
        if name in joint_names
    ]
    head_method = 'fixed'
    if head_joints:
        poses = np.asarray(motion['smpl_poses'], dtype=float).reshape(-1, 52, 3)[:t_frames]
        if False:
            if len(head_joints) != 2:
                raise ValueError('头部关节配置不完整')
            limits = np.asarray([joint.range for _, joint in head_joints])
            angles = fit_head_joints(head_rotations(poses), limits)
            head_method = 'quaternion-slerp-bounded-yaw-pitch-v1'
        else:
            angles = head_angles(poses)
            head_method = 'SMPL neck/head local yaw/pitch'
        for i, joint in head_joints:
            qpos[:, joint.qposadr[0]] = np.clip(angles[:, i], *joint.range)

    if qpos.shape != (t_frames, model.nq) or not np.isfinite(qpos).all():
        if not no_fallback:
            return _fallback_to_standard(root, key, mid, frame_limit, robot, 'non_finite_output')
        raise ValueError('求解结果形状或数值异常')

    norms = np.linalg.norm(qpos[:, 3:7], axis=1)
    if np.any(norms < 0.5):
        if not no_fallback:
            return _fallback_to_standard(root, key, mid, frame_limit, robot, 'invalid_root_quat')
        raise ValueError('无效根姿态四元数')
    qpos[:, 3:7] /= norms[:, None]

    # Verify Kinora hard gates before accepting fast candidate
    rejection = None if _within_joint_limits(model, qpos) else '关节越界超过容差'
    clearance_pairs = collision_pairs(model, robot)
    clearance = None
    if clearance_pairs and rejection is None:
        clearance = validate_clearance(model, qpos, clearance_pairs)
        (folder / 'clearance.json').write_text(json.dumps(clearance, ensure_ascii=False, indent=2))
        if not clearance['passed']:
            rejection = '自碰撞/净空检查未通过'

    timing_check = temporal(model, qpos.astype(np.float32), fps)
    if rejection is None and not timing_check['passed']:
        rejection = '时间连续性检查未通过'

    if rejection is not None:
        if not no_fallback:
            return _fallback_to_standard(root, key, mid, frame_limit, robot, rejection)
        from retarget_quality import reject_draft
        metadata = dict(
            robot=robot,
            robot_label=spec['label'],
            dof=spec['dof'],
            robot_note=spec['note'],
            motion_id=mid,
            text_id=record.get('text_id'),
            text=record['text'],
            adapter_version=accel.ENGINE_VERSION,
            created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            robot_sha256=hashlib.sha256(mpath.read_bytes()).hexdigest(),
            source_sha256=hashlib.sha256(world.astype('<f8').tobytes()).hexdigest(),
            coordinates=f"Z up, metres, root quaternion wxyz; qpos=[xyz,wxyz,{spec['dof']} joints]",
            head_method=head_method,
            validation='Offline kinematic retargeting, not dynamics or hardware validation',
            height=height,
            travel_scale=float(res.get('leg_scale', 1.0)),
            clearance=clearance,
            execution=execution,
        )
        reject_draft(folder, qpos, fps, joint_names, metadata, rejection)

    metadata = dict(
        robot=robot,
        robot_label=spec['label'],
        dof=spec['dof'],
        robot_note=spec['note'],
        motion_id=mid,
        text_id=record.get('text_id'),
        text=record['text'],
        adapter_version=accel.ENGINE_VERSION,
        created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        robot_sha256=hashlib.sha256(mpath.read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(world.astype('<f8').tobytes()).hexdigest(),
        coordinates=f"Z up, metres, root quaternion wxyz; qpos=[xyz,wxyz,{spec['dof']} joints]",
        head_method=head_method,
        validation='Offline kinematic retargeting, not dynamics or hardware validation',
        height=height,
        pose_method='accelerated kinematic constraint solver with anatomical leg scaling',
        travel_scale=float(res.get('leg_scale', 1.0)),
        solve_seconds=round(float(solve_s), 4),
        execution=execution,
    )
    metadata['hand_method'] = 'No hand/finger retargeting; wrist joints fixed at neutral where present'
    metadata['clearance'] = clearance

    data = mujoco.MjData(model)
    points = []
    for pose in qpos:
        data.qpos[:] = pose
        mujoco.mj_forward(model, data)
        points.append(data.xpos[1:].copy())
    points = np.asarray(points)[:, :, [0, 2, 1]] * np.array([1, 1, -1])
    parents = (model.body_parentid[1:] - 1).tolist()
    names = [model.body(i).name for i in range(1, model.nbody)]

    costs = np.zeros(t_frames, dtype=np.float64)
    tmp = folder / 'result.tmp.npz'
    np.savez_compressed(
        tmp,
        qpos=qpos.astype(np.float32),
        fps=np.array(fps),
        joint_names=np.array(joint_names),
        metadata=np.array(json.dumps(metadata, ensure_ascii=False)),
        costs=costs,
    )
    os.replace(tmp, folder / 'result.npz')

    preview = dict(
        joints=points.round(5).tolist(),
        parents=parents,
        names=names,
        fps=fps,
        frames=len(points),
        duration_s=len(points) / fps,
        metadata=metadata,
    )
    tmp_p = folder / 'preview.tmp'
    tmp_p.write_text(json.dumps(preview, ensure_ascii=False))
    os.replace(tmp_p, folder / 'preview.json')

    queue.update(key, done=t_frames, total=t_frames, message='结果已保存')
    print('RETARGET_OK', qpos.shape, f'({solve_s * 1000.0:.1f} ms)', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--motion', required=True)
    parser.add_argument('--frames', type=int)
    parser.add_argument('--robot', default='unitree_g1_23dof')
    args = parser.parse_args()
    run(args.root, args.job, args.motion, args.frames, args.robot)
