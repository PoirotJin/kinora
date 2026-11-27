"""Deterministic short transition with independent pelvis and heading tangents."""
import numpy as np
from scipy.spatial.transform import Rotation

from . import kimodo_root, motion_splice as splice
from .human_rig import motion_rig
from .motion_assets import STATIC, validate_motion

VERSION = 'cubic-hermite-splice-v3'
LIMITS = dict(splice.LIMITS, min_transition_frames=9)


def duration(frames):
    if type(frames) is not int or not LIMITS['min_transition_frames'] <= frames <= LIMITS['max_transition_frames']:
        raise ValueError('过渡时长需为 0.3–18 秒')


def check(a, b, frames):
    duration(frames)
    gap = splice.gap(a, b)
    seconds = frames / 30
    needed = max(gap['distance_m'] / LIMITS['max_speed_m_s'],
                 gap['turn_deg'] / LIMITS['max_turn_deg_s'])
    if needed > seconds:
        raise ValueError('当前间距或转向至少需要 %.2f 秒' % needed)
    return dict(gap, seconds=seconds, mean_speed_m_s=gap['distance_m'] / seconds)


def endpoint(values, at_end):
    samples = values[-9:][::-1] if at_end else values[:9]
    times = np.arange(1, len(samples)) / (-30 if at_end else 30)
    design = np.column_stack((times, .5 * times**2))
    delta = samples[1:] - samples[0]
    weights = np.ones(len(times))
    for _ in range(6):
        scale = np.sqrt(weights)
        velocity, acceleration = np.linalg.lstsq(design * scale[:, None], delta * scale[:, None], rcond=None)[0]
        residual = np.linalg.norm(design @ np.stack((velocity, acceleration)) - delta, axis=1)
        median = np.median(residual)
        mad = 1.4826 * np.median(np.abs(residual - median))
        weights = np.minimum(1., max(1e-6, median + 1.345 * mad) / np.maximum(residual, 1e-12))
    return samples[0].copy(), velocity


def heading(points):
    across = points[:, 1] - points[:, 2]
    return np.unwrap(np.arctan2(-across[:, 2], across[:, 0]))


def transform(arrays, start, end, yaw, x, z):
    out = splice.transform(arrays, start, end, yaw, x, z)
    # Both legacy and current transform implementations use the same rotation
    # on the body. Preserve the original fixed template pivot on either.
    pivot = arrays['native_keypoints3d'][start:end, :1].astype(np.float64)
    original = arrays['native_keypoints3d'][start:end].astype(np.float64)
    out['native_keypoints3d'] = ((original-pivot) @ splice._yaw(yaw).T + pivot).astype(
        arrays['native_keypoints3d'].dtype)
    return out


def _with_pivot(arrays, old, new):
    offset = old - new
    if np.max(np.abs(offset)) <= 1e-7:
        return arrays
    out = dict(arrays)
    out['native_keypoints3d'] = (arrays['native_keypoints3d'] - offset).astype(arrays['native_keypoints3d'].dtype)
    for key in ('native_transl', 'smpl_trans'):
        if key in arrays:
            out[key] = (arrays[key] + offset).astype(arrays[key].dtype)
    return out


def bounded_floor_lift(heights, u):
    # A full-width sine hump under-corrects dips near either boundary. Keep a
    # plateau over the middle half, with zero-derivative quintic ramps at both
    # ends, so asymmetric foot dips can use the same bounded lift.
    ramp = np.minimum(1., np.minimum(u, 1. - u) * 4.)
    envelope = ramp**3 * (10. - 15. * ramp + 6. * ramp**2)
    required = np.maximum(0., .035 - heights)
    amplitude = float(np.max(required / np.maximum(envelope, 1e-12)))
    return min(amplitude, .08) * envelope


def align_rest_origins(a, b):
    """Use a shared rest origin for compatible rigs while preserving their world poses."""
    rest, parents = motion_rig(a)
    rest_b, parents_b = motion_rig(b)
    if not np.array_equal(parents, parents_b) or not np.allclose(
            rest - rest[0], rest_b - rest_b[0], atol=5e-3, rtol=0):
        raise ValueError('两段素材骨架不一致，无法拼接')
    return _with_pivot(b, rest_b[0], rest[0]), rest, parents


def join(a, b, frames):
    b, rest, parents = align_rest_origins(a, b)
    duration(frames)
    gap = splice.gap(a, b)
    seconds = frames / 30
    needed = max(gap['distance_m'] / LIMITS['max_speed_m_s'],
                 gap['turn_deg'] / LIMITS['max_turn_deg_s'])
    report = dict(gap, seconds=seconds, mean_speed_m_s=gap['distance_m'] / seconds)
    if needed > seconds:
        report['speed_warning'] = '当前间距或转向至少需要 %.2f 秒' % needed
    shared = set(splice._channels(a)) & set(splice._channels(b))
    keys = sorted(shared)
    if 'smpl_betas' in a and 'smpl_betas' in b and not np.array_equal(a['smpl_betas'], b['smpl_betas']):
        raise ValueError('两段素材的体型参数不同，无法拼接')
    root_a = np.column_stack((a['native_transl'], heading(a['native_keypoints3d'])))
    root_b = np.column_stack((b['native_transl'], heading(b['native_keypoints3d'])))
    p, v = endpoint(root_a, True)
    q, w = endpoint(root_b, False)
    q[3] = p[3] + np.angle(np.exp(1j * (q[3] - p[3])))
    u = np.arange(1, frames + 1)[:, None] / (frames + 1)
    t = (frames + 1) / 30
    path = p + u * (v*t + u * (3*(q-p)-(2*v+w)*t + u * (-2*(q-p)+(v+w)*t)))
    first = Rotation.from_matrix(splice._local(a)[-1])
    last = Rotation.from_matrix(splice._local(b)[0])
    delta = (first.inv() * last).as_rotvec()
    ease = (u*u*(3-2*u)).ravel()
    local = np.stack([(first * Rotation.from_rotvec(alpha * delta)).as_matrix() for alpha in ease])
    from .kimodo_adapter import fk
    points, _ = fk(local, rest, parents)
    local[:, 0] = Rotation.from_euler('y', path[:, 3] - heading(points)).as_matrix() @ local[:, 0]
    dtypes = {key: (np.result_type(a[key], b[key]), a[key].shape[1:]) for key in keys}
    middle = splice._fill(local, path[:, :3], keys, dtypes, rest, parents)
    # Quaternion interpolation can dip the feet even when both boundary poses
    # clear the floor. Correct only the new transition with a smooth, bounded
    # root lift; exterior frames and all acceptance thresholds stay unchanged.
    world_middle = middle['native_keypoints3d'] + middle['native_transl'][:, None]
    floor_lift = bounded_floor_lift(world_middle[:, [10, 11], 1].min(axis=1), u.ravel())
    if np.any(floor_lift):
        path[:, 1] += floor_lift
        middle = splice._fill(local, path[:, :3], keys, dtypes, rest, parents)
    out = {}
    for key in keys:
        suffix = middle[key].shape[1:]
        out[key] = np.concatenate((a[key].reshape((-1,) + suffix), middle[key], b[key].reshape((-1,) + suffix)))
    for key in (STATIC & a.keys() & b.keys()) - {'smpl_num_frames', 'parents', 'rest_joints', 'root_origin'}:
        if np.array_equal(a[key], b[key]):
            out[key] = a[key]
    out.update(fps=np.asarray(30), rest_joints=rest, parents=parents, root_origin=rest[0].copy())
    out = validate_motion(out)
    left = len(a['native_transl'])
    right = left + frames
    root_report = kimodo_root.review(out, left, right)
    cuts = np.array([left, right])
    world = out['native_keypoints3d'] + out['native_transl'][:, None]
    rotations = splice._local(out)
    relative = rotations[cuts-1].swapaxes(-1, -2) @ rotations[cuts]
    rotation_step = np.rad2deg(Rotation.from_matrix(relative.reshape(-1, 3, 3)).magnitude().max())
    joint_step = np.linalg.norm(world[cuts] - world[cuts-1], axis=-1).max()
    failures = list(root_report['failures'])
    if joint_step > .20:
        failures.append('接缝关节点位移超过 0.20 m')
    if rotation_step > 30:
        failures.append('接缝关节旋转超过 30°')
    toe_penetration = max(0., .07 - float(world[left:right, [10, 11], 1].min()))
    body_joints = [3, 4, 5, 6, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    body_penetration = max(0., -float(world[left:right, body_joints, 1].min()))
    if toe_penetration > .04:
        failures.append('足端穿透代理超过 0.04 m')
    if body_penetration > .03:
        failures.append('身体关节点穿透超过 0.03 m')
    report.update(toe_penetration_m=toe_penetration, body_point_penetration_m=body_penetration,
                  transition_floor_lift_m=float(floor_lift.max()))
    report.update(version=VERSION, frames=len(world), selection={'start_frame': left, 'end_frame': right},
                  root_continuity=root_report, joint_step_m=float(joint_step),
                  joint_rotation_deg=float(rotation_step), passed=not failures, failures=failures)
    return out, report
