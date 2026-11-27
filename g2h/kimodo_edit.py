"""Constrained local resynthesis with exact exterior and measured seam gates."""
import hashlib
from pathlib import Path
import numpy as np
from scipy.spatial.transform import Rotation
from .kimodo_adapter import BODY_MAP, fk
from .motion_assets import STATIC, _same, load_numeric_npz, selection_bounds, validate_motion

VERSION = 'kimodo-local-edit-v1'
CANONICAL = {'native_keypoints3d', 'native_transl', 'smpl_poses',
             'native_rot6d', 'native_root_rotations_mat', 'smpl_trans', 'smpl_Rh'}


def window(frames, start, end, staged_bridge=False):
    selection_bounds(start, end, frames)
    limit = 540 if staged_bridge else 300
    if frames < 30 or not 12 <= end - start <= limit:
        raise ValueError('请在至少 1 秒的动作中选择 0.4–10 秒的片段')
    spare = 30 if staged_bridge else 300 - (end - start)
    first = max(0, start - min(15, spare // 2))
    last = min(frames, end + min(15, spare - (start - first)))
    if last - first < 30:
        first = max(0, min(first, last - 30))
        last = min(frames, max(last, first + 30))
    return {'start': first, 'end': last, 'frames': last - first,
            'left': start - first, 'right': end - first,
            'guard_left': 3 if start else 0, 'guard_right': 3 if end < frames else 0}


EDIT_KEYS = frozenset({'source', 'source_sha256', 'start_frame', 'end_frame'})


def valid_request(edit):
    return (isinstance(edit, dict) and EDIT_KEYS <= set(edit)
            and set(edit) <= EDIT_KEYS | {'follow_root', 'staged_bridge'}
            and all(edit.get(k, True) is True for k in ('follow_root', 'staged_bridge'))
            and (not edit.get('staged_bridge') or edit.get('follow_root') is True))


def read_source(edit):
    # follow_root (splice gaps only): the selection already holds the planned
    # ground path, and Kimodo is asked to keep its root on it.
    if not valid_request(edit):
        raise ValueError('Invalid pinned edit request')
    arrays = load_numeric_npz(edit['source'])
    if hashlib.sha256(Path(edit['source']).read_bytes()).hexdigest() != edit['source_sha256']:
        raise ValueError('修复来源版本校验不一致')
    plan = window(len(arrays['native_transl']), edit['start_frame'], edit['end_frame'], edit.get('staged_bridge', False))
    if edit.get('follow_root'):
        plan['follow_root'] = True
    if edit.get('staged_bridge'):
        from .kimodo_bridge import stages
        plan['staged_bridge'] = True
        stages(plan)
    return arrays, plan


def canonical_to_soma(arrays, rest, parents, skeleton):
    """Transfer named global body rotations back into SOMA's neutral frame.

    Always derive from the current canonical version, never from archived raw
    Kimodo arrays, which may predate an accepted edit.
    """
    local = Rotation.from_rotvec(arrays['smpl_poses'].reshape(-1, 3)).as_matrix().reshape(-1, 52, 3, 3)
    _, global_body = fk(local, rest, parents)
    names = skeleton.bone_order_names
    src_parents = skeleton.joint_parents.detach().cpu().numpy()
    src_rest = skeleton.neutral_joints.detach().cpu().numpy()
    frames = len(local)
    global_soma = np.empty((frames, len(names), 3, 3))
    local_soma = np.empty_like(global_soma)
    mapping = {name: i for i, name in enumerate(BODY_MAP)}
    for i, name in enumerate(names):
        p = int(src_parents[i])
        inherited = np.broadcast_to(np.eye(3), (frames, 3, 3)) if p < 0 else global_soma[:, p]
        global_soma[:, i] = global_body[:, mapping[name]] if name in mapping else inherited
        local_soma[:, i] = inherited.swapaxes(-1, -2) @ global_soma[:, i]
    positions, global_soma = fk(local_soma, src_rest, src_parents)
    root = arrays['native_transl'].astype(np.float64) + rest[0]
    positions += (root - src_rest[0])[:, None]
    return positions, global_soma


def constraints(arrays, plan, pipeline, rest, parents):
    import torch
    from kimodo.constraints import FullBodyConstraintSet, Root2DConstraintSet
    validate_motion(dict(arrays, parents=parents, rest_joints=rest))
    first, last = plan['start'], plan['end']
    sliced = {k: v[first:last] if k not in STATIC and v.ndim and v.shape[0] == len(arrays['native_transl']) else v
              for k, v in arrays.items()}
    indices = np.arange(plan['frames'])
    frozen = ((indices < plan['left'] + plan['guard_left']) |
              (indices >= plan['right'] - plan['guard_right']))
    follow = bool(plan.get('follow_root')) and not frozen.all()
    if not frozen.any() and not follow:
        return []
    positions, rotations = canonical_to_soma(sliced, rest, parents, pipeline.skeleton)
    device = pipeline.device
    sets = []
    if frozen.any():
        sets.append(FullBodyConstraintSet(pipeline.skeleton,
            torch.as_tensor(indices[frozen], dtype=torch.long, device=device),
            torch.as_tensor(positions[frozen], dtype=torch.float32, device=device),
            torch.as_tensor(rotations[frozen], dtype=torch.float32, device=device)))
    if follow:
        # Ground-plane (x, z) root path on every free frame, as FullBody
        # derives it for the frozen ones; height and heading stay free.
        path = positions[~frozen][:, pipeline.skeleton.root_idx][:, [0, 2]]
        sets.append(Root2DConstraintSet(pipeline.skeleton,
            torch.as_tensor(indices[~frozen], dtype=torch.long, device=device),
            torch.as_tensor(path, dtype=torch.float32, device=device)))
    return [sets]


def _weights(count, left, right):
    weights = np.ones(count)
    ramp = min(12, max(1, (count - left - right) // 3))
    t = np.arange(1, ramp + 1) / (ramp + 1)
    ease = 6*t**5 - 15*t**4 + 10*t**3
    if left:
        weights[:left] = 0
        weights[left:left+ramp] = ease
    if right:
        weights[-right:] = 0
        weights[-right-ramp:-right] = ease[::-1]
    return weights, ramp


def _angles(local):
    relative = local[:-1].swapaxes(-1, -2) @ local[1:]
    return Rotation.from_matrix(relative.reshape(-1, 3, 3)).magnitude().reshape(len(local)-1, 52)


def seam_report(before, after, start, end, rest, parents, ramp, staged_bridge=False):
    frames = len(before['native_transl'])
    for key, original in before.items():
        changed = after[key]
        if key not in STATIC and original.ndim and original.shape[0] == frames:
            if not _same(original[:start], changed[:start]) or not _same(original[end:], changed[end:]):
                raise ValueError('修复改变了选区外的数据：' + key)
        elif not _same(original, changed):
            raise ValueError('修复改变了静态数据：' + key)
    worlds = [a['native_keypoints3d'].astype(float) - a['native_keypoints3d'][:, :1] +
              a['native_transl'][:, None] + rest[0] for a in (before, after)]
    rotations = [Rotation.from_rotvec(a['smpl_poses'].reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
                 for a in (before, after)]
    checks = {}
    failures = []
    for label, cut in (('start', start), ('end', end)):
        if cut in (0, frames):
            checks[label] = {'neighbor': False}
            continue
        # Three unchanged inner frames preserve discrete position, velocity and
        # acceleration at the actual cut, in every stored temporal channel.
        low, high = max(0, cut-2), min(frames, cut+2)
        if not staged_bridge and not np.array_equal(worlds[0][low:high], worlds[1][low:high]):
            raise ValueError('边界位置、速度或加速度发生改变')
        step = float(np.linalg.norm(worlds[1][cut] - worlds[1][cut-1], axis=-1).max())
        if step > .20:
            failures.append(label + ': 边界关节跳变 %.3fm > 0.200m' % step)
        lo, hi = (max(0, start-2), min(frames, start+3+ramp+2)) if label == 'start' else (max(0, end-3-ramp-2), min(frames, end+2))
        positional = [float(np.linalg.norm(np.diff(w[lo:hi], axis=0), axis=-1).max()) for w in worlds]
        acceleration = [float(np.linalg.norm(np.diff(w[lo:hi], n=2, axis=0), axis=-1).max()*900) for w in worlds]
        angular = [float(_angles(r[lo:hi]).max()) for r in rotations]
        limits = {'step_m': max(.20, positional[0]*2+.01),
                  'acceleration_m_s2': max(120., acceleration[0]*2+10),
                  'rotation_step_rad': max(np.deg2rad(30), angular[0]*2+np.deg2rad(2))}
        passed = bool(positional[1] <= limits['step_m'] and acceleration[1] <= limits['acceleration_m_s2'] and angular[1] <= limits['rotation_step_rad'])
        if not passed:
            metrics = [('关节位移', positional[1], limits['step_m'], 'm'),
                       ('关节加速度', acceleration[1], limits['acceleration_m_s2'], 'm/s²'),
                       ('逐帧旋转', np.rad2deg(angular[1]), np.rad2deg(limits['rotation_step_rad']), '°')]
            failures.append(label + ': ' + '、'.join('%s %.2f%s > %.2f%s' % (name, value, unit, limit, unit)
                for name, value, limit, unit in metrics if value > limit))
        checks[label] = {'neighbor': True, 'cut_unchanged_through_acceleration': not staged_bridge,
                         'joint_step_max_m': positional[1], 'joint_acceleration_max_m_s2': acceleration[1],
                         'rotation_step_max_rad': angular[1], 'limits': limits, 'passed': passed}
    delta = rotations[0][start:end].swapaxes(-1, -2) @ rotations[1][start:end]
    max_angle = float(Rotation.from_matrix(delta.reshape(-1, 3, 3)).magnitude().max())
    max_root = float(np.linalg.norm(after['native_transl'][start:end] - before['native_transl'][start:end], axis=-1).max())
    if max_angle < np.deg2rad(.5) and max_root < .001:
        failures.append('候选没有产生足够的动作变化，请调整提示词')
    root_report = None
    if staged_bridge:
        from .kimodo_root import review as review_root
        root_report = review_root(after, start, end)
        failures.extend(root_report['failures'])
    scope = ('Exact exterior; measured bridge seams; semantic and dynamics review required.' if staged_bridge
             else 'Exact exterior; unchanged discrete cut derivatives; numerical seam checks only.')
    return {'version': VERSION, 'passed': not failures, 'failures': failures,
            'outside_selection_bitwise_equal': True, 'boundaries': checks,
            **({'root_continuity': root_report} if root_report is not None else {}),
            'max_rotation_change_deg': float(np.rad2deg(max_angle)), 'max_root_change_m': max_root,
            'transition_frames': ramp, 'scope': scope}


def compose(original, generated, start, end, plan, rest, parents):
    frames = len(original['native_transl'])
    validate_motion(dict(original, parents=parents, rest_joints=rest))
    unknown = [k for k, v in original.items() if k not in CANONICAL | STATIC and not k.startswith('kimodo_')
               and v.ndim and v.shape[0] == frames]
    if unknown:
        raise ValueError('无法同步动作扩展字段：' + ', '.join(unknown))
    result = {k: v.copy() for k, v in original.items()}
    a, b = plan['left'], plan['right']
    count = end-start
    weights, ramp = _weights(count, plan['guard_left'], plan['guard_right'])
    if plan.get('staged_bridge'):
        # Genuine constrained in-betweening replaces every gap frame.
        weights, ramp = np.ones(count), 12
    source_local = Rotation.from_rotvec(original['smpl_poses'][start:end].reshape(-1, 3)).as_matrix().reshape(count, 52, 3, 3)
    new_local = Rotation.from_rotvec(generated['smpl_poses'][a:b].reshape(-1, 3)).as_matrix().reshape(count, 52, 3, 3)
    delta = source_local[:, :22].swapaxes(-1, -2) @ new_local[:, :22]
    vectors = Rotation.from_matrix(delta.reshape(-1, 3, 3)).as_rotvec().reshape(count, 22, 3)
    blended = source_local.copy()
    blended[:, :22] = source_local[:, :22] @ Rotation.from_rotvec((vectors*weights[:, None, None]).reshape(-1, 3)).as_matrix().reshape(count, 22, 3, 3)
    points, _ = fk(blended, rest, parents)
    roots = original['native_transl'][start:end]*(1-weights[:, None]) + generated['native_transl'][a:b]*weights[:, None]
    poses = Rotation.from_matrix(blended.reshape(-1, 3, 3)).as_rotvec().reshape(count, 156)
    replacements = {'native_keypoints3d': points, 'native_transl': roots, 'smpl_trans': roots,
                    'smpl_poses': poses, 'smpl_Rh': poses[:, :3],
                    'native_root_rotations_mat': blended[:, 0],
                    'native_rot6d': blended[..., :2].reshape(count, 52, 6)}
    active = weights > 0
    for key, values in replacements.items():
        if key in result:
            values = values.reshape((count,) + result[key].shape[1:]).astype(result[key].dtype)
            result[key][start:end][active] = values[active]
    # kimodo_* channels retain immutable raw generator evidence. Current motion
    # consumers use canonical fields; the new asset's provenance declares this.
    validate_motion(dict(result, parents=parents, rest_joints=rest))
    report = seam_report(original, result, start, end, rest, parents, ramp, plan.get('staged_bridge', False))
    return result, report
