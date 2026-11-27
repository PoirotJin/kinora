"""Numerical measurements shared by motion editors and timeline screening."""
import numpy as np
from scipy.spatial.transform import Rotation

# Timeline screening of the 22 body joints on canonical 30 fps motion.
BODY_JOINTS = 22
TIMELINE_STEP_M = .19
TIMELINE_ROTATION_DEG = 55.


def position_steps(previous, current):
    """Per-joint displacement in metres for paired samples."""
    return np.linalg.norm(current - previous, axis=-1)


def rotation_steps(previous, current):
    """Per-joint shortest relative rotation, in radians."""
    relative = previous.swapaxes(-1, -2) @ current
    return Rotation.from_matrix(relative.reshape(-1, 3, 3)).magnitude().reshape(relative.shape[:-2])


def body_steps(world, local):
    """Largest step per frame, assigned to the later sample of each pair."""
    points = world[:, :BODY_JOINTS]
    rotations = local[:, :BODY_JOINTS]
    step = position_steps(points[:-1], points[1:]).max(axis=1)
    angle = rotation_steps(rotations[:-1], rotations[1:]).max(axis=1)
    return np.r_[0., step], np.r_[0., np.rad2deg(angle)]
