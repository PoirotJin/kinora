"""Route a blocked wrist approach around the rim of a top-contact object."""
import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation, Slerp

from .human_interaction import MAX_PENETRATION_M, object_centers, report, skeleton_samples
from .kimodo_adapter import fk
from .motion_continuity import rotation_steps

VERSION = 'human-interaction-top-approach-v1'
ARMS = {20: (13, 16, 18, 20), 21: (14, 17, 19, 21)}


def repair_top_approach(arrays, scene, rest, parents):
    """Fit wrist paths over a box edge, leaving the contact and pose frames intact."""
    before = report(arrays, scene, rest, parents, frame_distances=True)
    if any(not item['passed'] for item in before['contacts']):
        return arrays, None
    translation = np.asarray(arrays['native_transl'], dtype=float)
    poses = np.asarray(arrays['smpl_poses'], dtype=float).reshape(-1, 52, 3)
    frames = len(poses)
    boxes = {obj['id']: (object_centers(obj, frames),
                        Rotation.from_euler('y', obj['heading_deg'], degrees=True).as_matrix(),
                        np.asarray(obj['size_m']) / 2) for obj in scene['objects']}
    groups = {}
    for item in scene['contacts']:
        obj = item['object_id']
        half = boxes[obj][2]
        if item['joint_index'] in ARMS and abs(item['anchor_m'][1] - half[1]) <= .005:
            groups.setdefault((obj, item['start_frame']), set()).add(item['joint_index'])
    if not groups:
        return arrays, None

    matrices = Rotation.from_rotvec(poses.reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
    points = np.asarray(arrays['native_keypoints3d'], dtype=float)
    world = points - points[:, :1] + translation[:, None] + rest[0]
    _, global_rotations = fk(matrices, rest, parents)

    def depths(positions, frame):
        samples = skeleton_samples(positions, parents)
        return np.stack([np.maximum(0., np.min(half - np.abs((samples - centers[frame]) @ axes), axis=-1))
                         for centers, axes, half in boxes.values()])

    protected = {key['frame'] for key in scene['keyframes']} | {0, frames-1}
    for item in scene['contacts']:
        protected.update(range(item['start_frame'], item['end_frame']))
    bad = np.array([f for f in range(frames) if depths(world[f], f).max() > MAX_PENETRATION_M])
    if not len(bad):
        return arrays, None

    for (obj, contact_frame), wrists in sorted(groups.items(), key=lambda group: group[0][1]):
        if bad[-1] >= contact_frame:
            continue
        start_limit = max((f for f in protected if f < bad[0]), default=0)
        end = min(f for f in protected if f >= bad[-1])
        if bad[0] <= start_limit or bad[-1] >= end or end > contact_frame:
            continue
        centers, axes, half = boxes[obj]
        wrists = sorted(wrists)
        local = (world[:, wrists] - centers[:, None]) @ axes
        # Pick the entry face using the original clear incoming wrist positions.
        incoming = local[start_limit][:, [0, 2]]
        excess = np.abs(incoming) - half[[0, 2]]
        side = excess.argmax(axis=1)
        if np.any(excess[np.arange(len(wrists)), side] < 0):
            continue
        axis = 2 * side
        sign = np.sign(incoming[np.arange(len(wrists)), side])
        start = max(start_limit, int(bad[0]) - 18)
        if end - start < 8:
            continue
        turn = start + (end-start) // 2
        corner = (local[start] + local[end]) / 2
        corner[:, 1] = half[1] + .07
        corner[np.arange(len(wrists)), axis] = sign * (half[axis] + .075)
        targets = PchipInterpolator([start, turn, end],
                                    np.stack([local[start], corner, local[end]]), axis=0)
        joints = sorted({joint for wrist in wrists for joint in ARMS[wrist]})
        rotations = [Slerp([start, turn, end], Rotation.from_matrix(
            global_rotations[[start, end, end], wrist])) for wrist in wrists]
        updated = poses.copy()
        for frame in range(start+1, end):
            desired = targets(frame) @ axes.T + centers[frame]
            facing = np.stack([interpolation(frame).as_matrix() for interpolation in rotations])
            previous = updated[frame-1, joints]
            original = poses[frame, joints]

            def evaluate(values):
                frame_pose = poses[frame].copy()
                frame_pose[joints] = values.reshape(-1, 3)
                local_rot = Rotation.from_rotvec(frame_pose).as_matrix()[None]
                position, rotation = fk(local_rot, rest, parents)
                return position[0] + translation[frame], rotation[0]

            def residual(values):
                position, rotation = evaluate(values)
                facing_error = Rotation.from_matrix(facing.transpose(0, 2, 1) @ rotation[wrists]).as_rotvec()
                return np.r_[65 * (position[wrists]-desired).ravel(), .48 * facing_error.ravel(),
                             120 * np.maximum(0., depths(position, frame).ravel() - .006),
                             .15 * (values-original.ravel()), .85 * (values-previous.ravel())]

            bounds = (original.ravel()-1.3, original.ravel()+1.3)
            fit = least_squares(residual, np.clip(previous.ravel(), *bounds),
                                bounds=bounds, max_nfev=32, ftol=2e-4)
            updated[frame, joints] = fit.x.reshape(-1, 3)
        selected = slice(start+1, end)
        new_rot = Rotation.from_rotvec(updated[selected].reshape(-1, 3)).as_matrix().reshape(-1, 52, 3, 3)
        new_points, _ = fk(new_rot, rest, parents)
        new_world = world.copy()
        new_world[selected] = new_points + translation[selected, None]
        old_step = np.linalg.norm(np.diff(world, axis=0), axis=-1).max()
        old_accel = np.linalg.norm(np.diff(world, n=2, axis=0), axis=-1).max()
        old_turn = rotation_steps(matrices[:-1], matrices[1:]).max()
        checked_rot = matrices.copy()
        checked_rot[selected] = new_rot
        if (np.linalg.norm(np.diff(new_world, axis=0), axis=-1).max() > max(.20, old_step + 1e-5)
                or np.linalg.norm(np.diff(new_world, n=2, axis=0), axis=-1).max() > max(120/900, old_accel + 1e-5)
                or rotation_steps(checked_rot[:-1], checked_rot[1:]).max() > max(np.pi/4, old_turn + 1e-5)):
            continue
        result = dict(arrays)
        for field, values in {'native_keypoints3d': new_points,
                              'smpl_poses': updated[selected].reshape(-1, 156),
                              'native_rot6d': new_rot[..., :2].reshape(-1, 52, 6)}.items():
            if field in arrays:
                result[field] = arrays[field].copy()
                result[field][selected] = values
        after = report(result, scene, rest, parents)
        if after['passed']:
            return result, {'version': VERSION, 'object_id': obj, 'start_frame': start+1, 'end_frame': end-1,
                            'rerouted_joints': joints, 'max_penetration_m': after['max_skeleton_penetration_m']}
    return arrays, None
