"""Versioned, discrete 30 Hz root continuity gate; no motion modification."""
import numpy as np

VERSION = 'root-continuity-v1'
# Engineering acceptance limits, not a physical-contact or dynamics certificate.
LIMITS = {'step_m': .10, 'velocity_change_m_s': .75}


def review(arrays, start, end):
    root = np.asarray(arrays['native_transl'], dtype=np.float64)
    if root.ndim != 2 or root.shape[1] != 3 or not 0 <= start < end <= len(root):
        raise ValueError('Invalid root continuity selection')
    # Include both cut edges and all velocity changes touching the selection.
    step_frames = np.arange(max(1, start), min(len(root), end+1))
    change_frames = np.arange(max(2, start), min(len(root), end+2))
    steps = np.linalg.norm(root[step_frames] - root[step_frames-1], axis=1)
    changes = np.linalg.norm(root[change_frames] - 2*root[change_frames-1] + root[change_frames-2], axis=1)*30
    failures, violations = [], []
    for name, frames, values, limit, label, unit in (
        ('step_m', step_frames, steps, LIMITS['step_m'], 'root 逐帧位置变化', 'm'),
        ('velocity_change_m_s', change_frames, changes, LIMITS['velocity_change_m_s'], 'root 单帧速度变化', 'm/s'),
    ):
        bad = ~np.isfinite(values) | (values > limit)
        for frame, value in zip(frames[bad], values[bad]):
            violations.append({'metric': name, 'frame': int(frame),
                               'value': float(value) if np.isfinite(value) else None})
        if bad.any():
            failures.append('%s超限（%d 处，首处第 %d 帧，限值 %.3f %s）' % (label, bad.sum(), frames[bad][0], limit, unit))
    return {'version': VERSION, 'passed': not failures, 'failures': failures,
            'fps': 30, 'limits': dict(LIMITS), 'violations': violations,
            'max_step_m': float(steps.max()) if len(steps) and np.isfinite(steps).all() else None,
            'max_velocity_change_m_s': float(changes.max()) if len(changes) and np.isfinite(changes).all() else None,
            'scope': '3D root translation, both seams and entire selection; no smoothing; not contact or dynamics validation.'}
