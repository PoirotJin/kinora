"""Conservative skeleton diagnostics, not sole contact or robot collision proof."""
import numpy as np

VERSION = 'skeleton-gait-review-v1'


def runs(mask, minimum=5):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    return [(int(a), int(b)) for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)) if b-a >= minimum]


def review(arrays, start, end):
    fps = float(arrays['fps'])
    w = arrays['native_keypoints3d'].astype(np.float64) + arrays['native_transl'][:, None]
    if not 0 <= start < end <= len(w) or end-start < 6:
        raise ValueError('Gait review requires at least six frames')
    w = w[start:end]
    lateral = (w[:, 1]-w[:, 2])[:, [0, 2]]
    lateral /= np.maximum(np.linalg.norm(lateral, axis=1, keepdims=True), 1e-9)
    separation = np.sum((w[:, 7]-w[:, 8])[:, [0, 2]] * lateral, axis=1)
    yaw = np.unwrap(np.arctan2(-lateral[:, 1], lateral[:, 0]))
    angular_step = np.abs(np.diff(yaw)) * 180/np.pi
    angular_speed = angular_step * fps
    episodes = []
    for side, ankle, toe in (('left', 7, 10), ('right', 8, 11)):
        # Height and vertical speed only: do not use horizontal speed to decide
        # contact, which would circularly exclude the very sliding we measure.
        height = w[:, [ankle, toe], 1]
        low = np.percentile(height, 5, axis=0) + .065
        contact = np.all(height <= low, axis=1) & np.all(np.abs(np.gradient(height, axis=0)*fps) < .2, axis=1)
        for a, b in runs(contact):
            foot = w[a:b][:, [ankle, toe]][:, :, [0, 2]]
            drifts = np.linalg.norm(foot-foot[0], axis=-1).max(axis=0)
            # Heel/toe roll can move one joint legitimately; flag only when both drift.
            drift = float(drifts.min())
            episodes.append({'side':side, 'start_frame':start+a, 'end_frame':start+b,
                             'duration_s':(b-a)/fps, 'support_drift_proxy_m':drift})
    worst = max((e['support_drift_proxy_m'] for e in episodes), default=None)
    crossings = [{'start_frame':start+a,'end_frame':start+b} for a,b in runs(separation < -.02, 3)]
    warnings = []
    if worst is None: warnings.append('未检测到足够的疑似支撑区间，无法评估滑步')
    elif worst > .08: warnings.append('疑似支撑脚漂移 %.1f 厘米，需复核脚底接触' % (worst*100))
    if crossings: warnings.append('检测到左右踝横向次序交叉，需检查交叉步')
    if angular_step.max() > 10: warnings.append('骨盆单帧转向超过 10°，需检查突然扭转')
    return {'version':VERSION, 'scope':'Skeleton proxies only; sole contact, collisions and robot dynamics unverified.',
            'start_frame':start,'end_frame':end,'contact_method':{'height_band_m':.065,'vertical_speed_max_m_s':.2,'minimum_frames':5},
            'support_episodes':episodes,'max_support_drift_proxy_m':worst,
            'ankle_signed_separation_min_m':float(separation.min()),'crossing_intervals':crossings,
            'pelvis_yaw_step_max_deg':float(angular_step.max()),'pelvis_yaw_speed_max_deg_s':float(angular_speed.max()),
            'warnings':warnings, 'sole_contact_verified':False,'robot_collision_verified':False}
