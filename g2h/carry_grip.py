"""Fit a generated actor's arms to a moving two-handed grip, retaining its footwork."""
import numpy as np
from scipy.spatial.transform import Rotation

from .human_interaction import object_centers
from .kimodo_adapter import fk


def unit(v):
    return v / np.maximum(np.linalg.norm(v,axis=1,keepdims=True),1e-10)


def align(before, after):
    before, after = unit(before), unit(after)
    cross = np.cross(before,after)
    sine = np.linalg.norm(cross,axis=1)
    cosine = (before*after).sum(axis=1)
    axis = unit(cross)
    opposite = (sine<1e-8) & (cosine<0)
    if opposite.any():
        v = before[opposite]
        axis[opposite] = unit(np.cross(v,np.eye(3)[np.argmin(np.abs(v),axis=1)]))
    return Rotation.from_rotvec(axis*np.arctan2(sine,cosine)[:,None]).as_matrix()


def _window_weights(frames, start_frame, end_frame, ramp_frames):
    weights = np.zeros(frames, dtype=np.float64)
    weights[start_frame:end_frame] = 1.0
    if ramp_frames > 0 and start_frame > 0:
        a0 = max(0, start_frame - ramp_frames)
        n_in = start_frame - a0
        t = (np.arange(1, n_in + 1, dtype=np.float64) / (n_in + 1)
             if a0 > 0 else np.arange(n_in, dtype=np.float64) / max(n_in, 1))
        weights[a0:start_frame] = 6 * t**5 - 15 * t**4 + 10 * t**3
    if ramp_frames > 0 and end_frame < frames:
        r1 = min(frames, end_frame + ramp_frames)
        n_out = r1 - end_frame
        t = (1.0 - np.arange(1, n_out + 1, dtype=np.float64) / (n_out + 1)
             if r1 < frames else 1.0 - np.arange(1, n_out + 1, dtype=np.float64) / max(n_out, 1))
        weights[end_frame:r1] = 6 * t**5 - 15 * t**4 + 10 * t**3
    return weights


def _wrist_rotation(wrist, anchor, size_m, box_rot, direction):
    sign = 1. if wrist == 20 else -1.
    half = np.asarray(size_m, dtype=np.float64) / 2.
    n_frames = len(direction)
    if abs(anchor[2]) >= .005 and abs(anchor[0]) < half[0] - .005:
        palm_x = box_rot.apply([0., .22, -np.sign(anchor[2])]) * sign
        palm_x /= np.linalg.norm(palm_x)
        palm_z = np.cross(palm_x, [0., 1., 0.])
        palm_z /= np.linalg.norm(palm_z)
        return np.broadcast_to(
            np.column_stack((palm_x, np.cross(palm_z, palm_x), palm_z)), (n_frames, 3, 3)
        ).copy()
    if abs(anchor[0]) >= max(abs(anchor[2]), 1e-4) and abs(anchor[1]) < half[1] - .005:
        outward = box_rot.apply([np.sign(anchor[0]), 0., 0.])
        palm_y = np.broadcast_to(outward, (n_frames, 3)).copy()
        up = np.broadcast_to([0., 1., 0.], (n_frames, 3))
        palm_z = unit(up - palm_y * (palm_y * up).sum(axis=1, keepdims=True))
        palm_x = unit(np.cross(palm_y, palm_z))
        return np.stack((palm_x, palm_y, palm_z), axis=-1)
    palm_x = direction * sign
    up = np.broadcast_to([0., 1., 0.], (n_frames, 3))
    palm_z = np.cross(palm_x, up)
    degenerate = np.linalg.norm(palm_z, axis=1) < 1e-4
    if degenerate.any():
        palm_z[degenerate] = np.cross(palm_x[degenerate], np.array([0., 0., 1.]))
    palm_z = unit(palm_z)
    palm_y = unit(np.cross(palm_z, palm_x))
    return np.stack((palm_x, palm_y, palm_z), axis=-1)


def fit_window(arrays, obj, start_frame, end_frame, anchors_m, rest, parents, ramp_frames=8, joints=(20, 21)):
    """Fit arm pose over [start_frame, end_frame) with quintic ramp_frames blending, preserving legs/root."""
    frames = len(arrays['native_transl'])
    if not 0 <= int(start_frame) < int(end_frame) <= frames:
        raise ValueError('无效的抓取帧区间')
    joints = tuple(int(j) for j in joints)
    anchors = np.asarray(anchors_m, dtype=np.float64)
    if not joints or len(joints) != len(anchors) or any(j not in (20, 21) for j in joints) or len(set(joints)) != len(joints):
        raise ValueError('腕关节与锚点配置不匹配')
    weights = _window_weights(frames, int(start_frame), int(end_frame), int(ramp_frames))
    active = np.flatnonzero(weights > 0.)
    if not len(active):
        return {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in arrays.items()}
    poses = np.asarray(arrays['smpl_poses'], dtype=np.float64).reshape(frames, 52, 3)
    matrices = Rotation.from_rotvec(poses.reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
    xyz, orientations = fk(matrices, rest, parents)
    world = xyz + np.asarray(arrays['native_transl'], dtype=np.float64)[:, None, :]
    center = object_centers(obj, frames)
    box_rot = Rotation.from_euler('y', obj['heading_deg'], degrees=True)
    alpha = weights[active]
    selected = []
    for wrist, anchor in zip(joints, anchors):
        elbow, shoulder = int(parents[wrist]), int(parents[parents[wrist]])
        selected.extend((shoulder, elbow, wrist))
        target = center[active] + box_rot.apply(anchor)
        start = world[active, shoulder]
        direction = unit(target - start)
        upper = np.linalg.norm(rest[shoulder] - rest[elbow])
        lower = np.linalg.norm(rest[elbow] - rest[wrist])
        reach = np.clip(np.linalg.norm(target - start, axis=1), abs(upper - lower) + 1e-4, upper + lower - 1e-4)
        along = (upper**2 - lower**2 + reach**2) / (2 * reach)
        lateral = np.broadcast_to([1. if wrist == 20 else -1., 0., 0.], direction.shape)
        pole = (orientations[active, 0] @ lateral[:, :, None])[:, :, 0]
        pole[:, 1] -= .8
        pole = unit(pole - direction * (direction * pole).sum(axis=1, keepdims=True))
        new_elbow = start + along[:, None] * direction + np.sqrt(np.maximum(0., upper**2 - along**2))[:, None] * pole
        new_wrist = start + reach[:, None] * direction
        shoulder_rot = align(world[active, elbow] - start, new_elbow - start) @ orientations[active, shoulder]
        elbow_rot = align(world[active, wrist] - world[active, elbow], new_wrist - new_elbow) @ orientations[active, elbow]
        wrist_rot = _wrist_rotation(wrist, anchor, obj['size_m'], box_rot, direction)
        for joint, parent_rot, rotation in (
            (shoulder, orientations[active, parents[shoulder]], shoulder_rot),
            (elbow, shoulder_rot, elbow_rot),
            (wrist, elbow_rot, wrist_rot),
        ):
            ik_local = parent_rot.swapaxes(-1, -2) @ rotation
            orig_local = matrices[active, joint]
            delta = Rotation.from_matrix(orig_local.swapaxes(-1, -2) @ ik_local).as_rotvec()
            matrices[active, joint] = orig_local @ Rotation.from_rotvec(delta * alpha[:, None]).as_matrix()
    xyz_active, _ = fk(matrices[active], rest, parents)
    updated = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in arrays.items()}
    selected = sorted(set(selected))
    updated_pose = poses.copy()
    updated_pose[np.ix_(active, selected)] = (
        Rotation.from_matrix(matrices[np.ix_(active, selected)].reshape(-1, 3, 3))
        .as_rotvec()
        .reshape(len(active), len(selected), 3)
    )
    updated['smpl_poses'] = updated_pose.reshape(arrays['smpl_poses'].shape).astype(arrays['smpl_poses'].dtype)
    updated['native_keypoints3d'][active] = xyz_active.astype(arrays['native_keypoints3d'].dtype)
    if 'native_rot6d' in arrays:
        updated['native_rot6d'] = arrays['native_rot6d'].copy()
        updated['native_rot6d'][np.ix_(active, selected)] = (
            matrices[np.ix_(active, selected)][..., :2].reshape(len(active), len(selected), 6)
            .astype(arrays['native_rot6d'].dtype)
        )
    return updated


def fit(arrays, scene, rest, parents):
    """Fit wrist contacts in scene across their contact windows and arbitrary box sizes."""
    objects = {obj['id']: obj for obj in scene['objects']}
    groups = {}
    for contact in scene.get('contacts', []):
        joint = contact['joint_index']
        if joint not in (20, 21):
            continue
        key = (contact['object_id'], contact['start_frame'], contact['end_frame'])
        groups.setdefault(key, []).append((joint, contact['anchor_m']))
    updated = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in arrays.items()}
    for (obj_id, start_frame, end_frame), pairs in sorted(groups.items(), key=lambda x: (x[0][1], x[0][2], x[0][0])):
        joints = tuple(j for j, _ in pairs)
        anchors = [a for _, a in pairs]
        updated = fit_window(updated, objects[obj_id], start_frame, end_frame, anchors, rest, parents,
                             ramp_frames=8, joints=joints)
    return updated
