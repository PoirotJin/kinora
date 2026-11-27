"""Collision-check sparse Human52 targets before diffusion."""
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .human_interaction import (BONE_SAMPLE_FRACTIONS, MAX_PENETRATION_M,
                                object_centers, skeleton_samples, world_targets)
from .kimodo_adapter import fk

VERSION = 'human-interaction-preflight-v1'
CONTACT_LOCK_M = .0005
_SAMPLE_JOINTS = np.r_[np.arange(52), np.tile(np.arange(1, 52), len(BONE_SAMPLE_FRACTIONS))]
_LIMB_ROOTS = {1, 2, 13, 14}


def preflight(scene, frames, rest, parents):
    """Return effective conditioning targets and measurements, leaving the request intact."""
    indices, roots, headings, _, _ = world_targets(scene, frames)
    objects = {obj['id']: (object_centers(obj, frames),
                          Rotation.from_euler('y', obj['heading_deg'], degrees=True).as_matrix(),
                          np.asarray(obj['size_m']) / 2) for obj in scene['objects']}
    effective = []
    measurements = []
    for key, frame, root, heading in zip(scene['keyframes'], indices, roots, headings):
        pose = np.zeros((52, 3))
        pose[0] = Rotation.from_euler('y', heading, degrees=True).as_rotvec()
        pose[1:22] = key['body_rotvecs']

        def evaluate(angles):
            positions, rotations = fk(Rotation.from_rotvec(angles).as_matrix()[None], rest, parents)
            return positions[0] + (root - rest[0]), rotations[0]

        def depths(positions):
            samples = skeleton_samples(positions, parents)
            return np.stack([np.maximum(0., np.min(half - np.abs((samples - centers[frame]) @ axes), axis=-1))
                             for centers, axes, half in objects.values()])

        points, orientations = evaluate(pose)
        original_depth = depths(points)
        row = {'frame': int(frame), 'before_m': float(original_depth.max()),
               'after_m': float(original_depth.max()), 'changed_joints': [], 'contacts': []}
        measurements.append(row)
        if row['before_m'] <= MAX_PENETRATION_M:
            effective.append(key)
            continue

        # An in-window contact is authoritative. Also lock a repeated target
        # holding a contact on the same object after the annotated window.
        locks = {}
        protected_targets = []
        for item in scene['contacts']:
            joint = item['joint_index']
            center, axes, _ = objects[item['object_id']]
            anchor = center[frame] + axes @ np.asarray(item['anchor_m'])
            in_window = item['start_frame'] <= frame < item['end_frame']
            if (in_window or item['object_id'] == key['object_id']) and (
                    np.linalg.norm(points[joint] - anchor) <= item['tolerance_m']):
                locks[joint] = points[joint]
                protected_targets.append((joint, anchor, item['tolerance_m']))

        joints = set()
        for hit in _SAMPLE_JOINTS[np.any(original_depth > .006, axis=0)]:
            # A joint rotation moves descendants. Include the wrist when its
            # fingers hit the object and the collar when the forearm needs room.
            while hit > 0:
                if hit <= 21:
                    joints.add(int(hit))
                if hit in _LIMB_ROOTS:
                    break
                hit = parents[hit]
        local_joints = sorted(joints)
        locked_joints = list(locks)
        locked_positions = points[locked_joints]

        def modified(delta):
            angles = pose.copy()
            angles[joints] += delta.reshape(-1, 3)
            return angles

        def residual(delta):
            candidate, rotated = evaluate(modified(delta))
            contact_shift = candidate[locked_joints] - locked_positions
            relative = orientations[locked_joints].transpose(0, 2, 1) @ rotated[locked_joints]
            turn = (Rotation.from_matrix(relative).as_rotvec().ravel() if locked_joints else np.empty(0))
            return np.r_[100 * np.maximum(0., depths(candidate).ravel() - .004),
                         450 * contact_shift.ravel(), .65 * turn, joint_weights * delta]

        best = None
        joint_sets = [local_joints]
        spine = {3, 6, 9}
        if set(local_joints) & {13, 14} and not spine <= set(local_joints):
            joint_sets.append(sorted(set(local_joints) | spine))
        for joints in filter(None, joint_sets):
            bound = np.repeat([.35 if joint in spine else 1.1 for joint in joints], 3)
            joint_weights = np.repeat([.23 if joint in spine else .065 for joint in joints], 3)
            for start in (0., .10, -.10):
                initial = np.zeros((len(joints), 3))
                for i, joint in enumerate(joints):
                    initial[i, 0 if joint in spine else 1] = start
                fit = least_squares(residual, initial.ravel(), bounds=(-bound, bound),
                                    max_nfev=100, ftol=1e-6)
                angles = modified(fit.x)
                candidate, rotated = evaluate(angles)
                max_depth = float(depths(candidate).max())
                drift = np.linalg.norm(candidate[locked_joints] - locked_positions, axis=1)
                if (np.any(drift > CONTACT_LOCK_M)
                        or any(np.linalg.norm(candidate[joint] - anchor) > tolerance
                               for joint, anchor, tolerance in protected_targets)):
                    continue
                if max_depth > MAX_PENETRATION_M and max_depth >= row['before_m']:
                    continue
                cost = (max_depth > MAX_PENETRATION_M, max_depth > .008,
                        max_depth if max_depth > MAX_PENETRATION_M else 0.,
                        np.linalg.norm(fit.fun))
                if best is None or cost < best[0]:
                    best = cost, angles, rotated, drift, max_depth
                if max_depth <= .008:
                    break
            if best is not None and best[4] <= .008:
                break
        if best is None:
            effective.append(key)
            row['status'] = 'unresolved'
            continue

        _, angles, rotated, drift, max_depth = best
        changed = np.linalg.norm(angles[1:22] - pose[1:22], axis=1) > 1e-9
        angles = Rotation.from_rotvec(angles[1:22]).as_rotvec()
        body = [angles[i].tolist() if change else original
                for i, (change, original) in enumerate(zip(changed, key['body_rotvecs']))]
        effective.append(dict(key, body_rotvecs=body))
        row.update(status='repaired' if max_depth <= MAX_PENETRATION_M else 'best_effort',
                   after_m=max_depth, changed_joints=(np.flatnonzero(changed) + 1).tolist(),
                   contacts=[dict(joint=joint, shift_m=float(displacement),
                                  rotation_change_deg=float(np.rad2deg(Rotation.from_matrix(
                                      orientations[joint].T @ rotated[joint]).magnitude())))
                             for joint, displacement in zip(locked_joints, drift)])

    audit = {'version': VERSION, 'keyframes': measurements,
             'passed': all(row['after_m'] <= MAX_PENETRATION_M for row in measurements)}
    return dict(scene, keyframes=effective), audit
