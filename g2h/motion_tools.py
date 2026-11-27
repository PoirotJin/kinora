"""Numerical observations and explicit, bounded temporal editing tools.

These tools do not infer a user's intent or certify semantic/physical quality.
"""
import math

import numpy as np

from .motion_assets import STATIC, selection_bounds
from .motion_backends import MotionBackends

CHECKER_VERSION = 'motion-observation-v1'


def _vector(value):
    return [float(x) for x in value]


class MotionTools:
    def __init__(self, assets, backends=None):
        self.assets = assets
        self.backends = backends or MotionBackends()

    def capabilities(self):
        return {'models': self.backends.capabilities(),
                'tools': [{'name': 'inspect_motion', 'available': True},
                          {'name': 'apply_motion_patch', 'available': True},
                          {'name': 'repair_root_jitter', 'available': True,
                           'scope': 'Explicit root translation smoothing only; no semantic editing'}]}

    def inspect(self, owner, motion_id, start_frame=None, end_frame=None):
        arrays, metadata = self.assets.load(owner, motion_id)
        frames = metadata['frames']
        start = 0 if start_frame is None else start_frame
        end = frames if end_frame is None else end_frame
        selection_bounds(start, end, frames)
        fps = metadata['fps']
        joints = arrays['native_keypoints3d'].astype(np.float64)
        root = arrays['native_transl'].astype(np.float64)
        # Same root-relative convention as the existing preview transport.
        # Without a rest template the global vertical origin is unknown.
        rest_origin = arrays['rest_joints'][0] if 'rest_joints' in arrays else np.zeros(3)
        world = joints - joints[:, :1] + root[:, None, :] + rest_origin
        velocity = np.diff(root[start:end], axis=0) * fps
        speed = np.linalg.norm(velocity, axis=-1)
        horizontal = np.linalg.norm(velocity[:, [0, 2]], axis=-1)
        displacement = root[end - 1] - root[start]
        moving = float(np.linalg.norm(displacement[[0, 2]]))
        boundary = {}
        for label, frame in [('start', start), ('end', end)]:
            if not 0 < frame < frames:
                boundary[label] = {'status': 'unknown', 'reason': 'No neighboring frame at this boundary'}
                continue
            step = np.linalg.norm(world[frame] - world[frame - 1], axis=-1)
            value = {'status': 'measured', 'frame': frame,
                     'joint_step_max_m': float(step.max()),
                     'root_step_m': float(np.linalg.norm(root[frame] - root[frame - 1]))}
            if 1 < frame < frames - 1:
                before = (root[frame - 1] - root[frame - 2]) * fps
                across = (root[frame] - root[frame - 1]) * fps
                after = (root[frame + 1] - root[frame]) * fps
                value['root_velocity_change_max_m_s'] = float(max(np.linalg.norm(across - before), np.linalg.norm(after - across)))
            boundary[label] = value
        metrics = {'frames': end - start, 'duration_s': (end - start) / fps,
                   'mean_root_speed_m_s': float(speed.mean()) if len(speed) else 0.,
                   'max_root_speed_m_s': float(speed.max()) if len(speed) else 0.,
                   'mean_horizontal_root_speed_m_s': float(horizontal.mean()) if len(horizontal) else 0.,
                   'root_displacement_m': _vector(displacement),
                   'travel_direction_xz': _vector(displacement[[0, 2]] / moving) if moving > 1e-8 else None,
                   'minimum_joint_y_relative_origin_m': float(world[start:end, :, 1].min()),
                   'coordinate_origin': 'rest_template' if 'rest_joints' in arrays else 'unspecified_rest_root',
                   'boundaries': boundary}
        return {'motion_id': motion_id, 'motion_sha256': metadata['sha256'],
                'checker_version': CHECKER_VERSION,
                'selection': {'start_frame': start, 'end_frame': end}, 'metrics': metrics,
                'evidence': [
                    {'kind': 'numeric_integrity', 'status': 'passed',
                     'body': {'version': CHECKER_VERSION, 'scope': 'Canonical arrays are finite, shaped and alias-consistent'}},
                    {'kind': 'joint_pose_consistency',
                     'status': 'passed' if {'parents', 'rest_joints'} <= set(arrays) else 'unknown',
                     'body': {'reason': 'Checked against the supplied rig' if {'parents', 'rest_joints'} <= set(arrays) else 'Legacy four-field source has no rest rig for FK verification'}},
                    {'kind': 'ground_clearance', 'status': 'unknown',
                     'body': {'reason': 'Joint Y values alone do not establish the floor, body surfaces or ground penetration'}},
                    {'kind': 'boundary_quality', 'status': 'unknown',
                     'body': {'reason': 'Measurements require task-specific thresholds and source comparison; natural motion also has steps'}},
                    {'kind': 'semantic_alignment', 'status': 'unknown',
                     'body': {'reason': 'No independent motion-language or human evaluation has been supplied'}},
                    {'kind': 'dynamics', 'status': 'unknown',
                     'body': {'reason': 'No robot-specific dynamic validation has been performed'}}]}

    def splice(self, owner, parent_id, start_frame, end_frame, patch, *,
               expected_sha256, description='', request_id=None):
        return self.assets.apply_patch(owner, parent_id, start_frame, end_frame, patch,
                                       expected_sha256=expected_sha256, description=description,
                                       request_id=request_id,
                                       provenance={'tool': 'apply_motion_patch', 'version': CHECKER_VERSION})

    def repair_root_jitter(self, owner, motion_id, start_frame, end_frame, *,
                           expected_sha256, strength=0.25, passes=1,
                           max_displacement_m=0.05, axes=('x', 'z'), request_id=None):
        """Smooth explicitly requested root jitter inside the selected interval.

        Fixed first/last selected frames protect the neighboring segments. This
        changes the selected trajectory, so callers must reject it when the task
        requires the original trajectory. Joint-relative poses stay untouched.
        """
        arrays, metadata = self.assets.load(owner, motion_id)
        selection_bounds(start_frame, end_frame, metadata['frames'])
        if expected_sha256 != metadata['sha256']:
            raise ValueError('Root repair requires the exact parent hash')
        if (type(strength) not in (int, float) or not math.isfinite(strength) or not 0 < strength <= 0.5
                or type(passes) is not int or not 1 <= passes <= 8
                or type(max_displacement_m) not in (int, float) or not math.isfinite(max_displacement_m)
                or not 0 < max_displacement_m <= 0.1):
            raise ValueError('Invalid bounded root repair parameters')
        if not isinstance(axes, (list, tuple)) or not axes or len(set(axes)) != len(axes) or any(x not in ('x', 'y', 'z') for x in axes):
            raise ValueError('Repair axes must be unique x/y/z coordinates')
        if end_frame - start_frame < 3:
            raise ValueError('Root repair requires at least three selected frames')
        supported = {'native_keypoints3d', 'native_transl', 'native_root_rotations_mat',
                     'native_rot6d', 'smpl_poses', 'smpl_trans', 'smpl_Rh'} | STATIC
        unknown = [key for key, value in arrays.items()
                   if key not in supported and value.ndim and value.shape[0] == metadata['frames']]
        if unknown:
            raise ValueError('Cannot synchronize unknown dynamic fields: ' + ', '.join(unknown))
        root = arrays['native_transl'][start_frame:end_frame].astype(np.float64)
        original = root.copy()
        indices = [('x', 'y', 'z').index(x) for x in axes]
        for _ in range(passes):
            previous = root.copy()
            root[1:-1, indices] += strength * ((previous[:-2, indices] + previous[2:, indices]) / 2 - previous[1:-1, indices])
        delta = root - original
        norm = np.linalg.norm(delta, axis=-1, keepdims=True)
        delta *= np.minimum(1., max_displacement_m / np.maximum(norm, 1e-30))
        for key in ('native_transl', 'smpl_trans'):
            if key in arrays:
                arrays[key][start_frame:end_frame] = (original + delta).astype(arrays[key].dtype)
        return self.assets.apply_patch(owner, motion_id, start_frame, end_frame, arrays,
                                       expected_sha256=expected_sha256,
                                       description='Explicit root translation jitter repair', request_id=request_id,
                                       provenance={'tool': 'repair_root_jitter', 'version': CHECKER_VERSION,
                                                   'strength': strength, 'passes': passes,
                                                   'axes': list(axes), 'max_displacement_m': max_displacement_m,
                                                   'semantic_status': 'unknown', 'dynamics_status': 'unknown'})
