"""Build synchronized Kimodo conditions in one world frame for two Human52 actors."""
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .human_rig import load
from .kimodo_adapter import fk


def _arms_down_rotvecs():
    rotvecs = np.zeros((21, 3))
    rotvecs[15, 2] = -1.4
    rotvecs[16, 2] = 1.4
    return [[round(float(v), 6) if v else 0 for v in row] for row in rotvecs]


def _parse_actor_starts(actor_starts, defaults):
    if actor_starts is None:
        return defaults
    if not isinstance(actor_starts, (list, tuple)) or len(actor_starts) != 2:
        raise ValueError('双人起始位置需要包含 2 个角色')
    parsed = []
    for entry, (def_xz, def_heading) in zip(actor_starts, defaults):
        if isinstance(entry, dict):
            xz = entry.get('xz_m', entry.get('start_xz_m'))
            if xz is None and 'x' in entry and 'z' in entry:
                xz = [entry['x'], entry['z']]
            heading = float(entry.get('heading_deg', def_heading))
        else:
            xz = entry
            heading = def_heading
        arr = np.asarray(xz, dtype=float)
        if arr.shape != (2,) or not np.isfinite(arr).all():
            raise ValueError('无效的起始位置坐标')
        parsed.append((arr.tolist(), heading))
    return parsed


def interaction_plans(kind, frames, x=0., z=0., box_size_m=None, to_xz_m=None, actor_starts=None):
    """Return per-actor scene specs plus explicit joint-pair/prop contact windows."""
    if kind not in ('handshake', 'carry_box') or not 90 <= frames <= 3600:
        raise ValueError('请选择握手或合力搬箱子，时长至少 3 秒')
    rest, parents = load()
    start, end = round(.24*(frames-1)), round(.78*(frames-1))
    carrying = kind == 'carry_box'
    times = sorted({round(t) for t in np.linspace(start,end,15 if carrying else 7)})
    if box_size_m is not None:
        size = np.asarray(box_size_m, dtype=float)
        if size.shape != (3,) or not np.isfinite(size).all() or min(size) < .01:
            raise ValueError('无效箱子尺寸')
    else:
        size = np.array([.58,.38,.42]) if carrying else np.array([.01,.01,.01])
    if to_xz_m is not None:
        dest = np.asarray(to_xz_m, dtype=float)
        if dest.shape != (2,) or not np.isfinite(dest).all():
            raise ValueError('无效目标搬运坐标')
        delta_xz = dest - np.array([float(x), float(z)])
    else:
        delta_xz = np.array([1.1, 0.])
    center_y = max(float(size[1]/2), min(1.02, 1.25 - float(size[1]/2))) if carrying and box_size_m is not None else (1.02 if carrying else 1.16)
    center = np.array([float(x), center_y, float(z)])
    def displacement(frame):
        u = np.clip((frame-start)/(end-start), 0., 1.)
        if carrying:
            s = u*u*(3.-2.*u)
            return np.array([delta_xz[0]*s, .08*np.sin(np.pi*u), delta_xz[1]*s])
        return np.array([0., .045*np.sin(4*np.pi*u), 0.])
    prop_id = 'shared-box' if carrying else 'hand-contact'
    track_frames = [0,*times,frames-1]
    prop = dict(id=prop_id, center_m=center.tolist(), size_m=size.tolist(), heading_deg=0,
                trajectory=[dict(frame=t, center_m=(center+displacement(t)).tolist()) for t in track_frames])
    standoff = float(size[2]/2 + .37) if carrying else .35
    defaults = [([float(x), float(z) - standoff], 0.), ([float(x), float(z) + standoff], 180.)]
    starts = _parse_actor_starts(actor_starts, defaults)
    arms_down = _arms_down_rotvecs()
    plans = []
    for actor, heading in enumerate([0.,180.]):
        side = -1 if actor == 0 else 1
        yaw = Rotation.from_euler('y',heading,degrees=True)
        root = np.array([float(x),.92,float(z)+side*standoff])
        arms = [(20,16,18),(21,17,19)] if carrying else [(21,17,19)]
        ax = min(.23, max(.01, float(size[0]/2) - .02))
        anchors = {joint: ([(-side if joint==20 else side)*ax,float(size[1]/2),float(side*size[2]/2)]
                           if carrying else [0.,0.,side*.005]) for joint,_,_ in arms}
        def root_at(t):
            delta = displacement(t)
            return root + np.array([delta[0],0.,delta[2]])
        start_xz_i, start_heading_i = starts[actor]
        p0 = np.array([start_xz_i[0], .915, start_xz_i[1]])
        keys = [dict(frame=0,object_id=prop_id,pelvis_offset_m=(p0-center-displacement(0)).tolist(),
                     heading_deg=start_heading_i,body_rotvecs=arms_down)]
        for t in times:
            pelvis = root_at(t)
            angles = np.zeros((52,3))
            angles[0] = yaw.as_rotvec()
            angles[16,2], angles[17,2] = -1.4, 1.4
            for wrist, shoulder, elbow in arms:
                target = center+displacement(t)+anchors[wrist]
                # A slight elbow bend resolves the straight arm singularity.
                initial = np.r_[angles[shoulder], [0.,.3 if wrist==20 else -.3,0.]]
                def residual(v):
                    angles[[shoulder,elbow]] = v.reshape(2,3)
                    positions,_ = fk(Rotation.from_rotvec(angles).as_matrix()[None],rest,parents)
                    return np.r_[positions[0,wrist]+pelvis-rest[0]-target, .003*(v-initial)]
                solution = least_squares(residual,initial,max_nfev=80)
                error = np.linalg.norm(residual(solution.x)[:3])
                if carrying:
                    _, global_rot = fk(Rotation.from_rotvec(angles).as_matrix()[None],rest,parents)
                    # Keep the fingers along the top surface as the wrist meets the rim.
                    neutral_axis = np.array([1. if wrist==20 else -1.,0.,0.])
                    local_axis = global_rot[0,elbow].T @ np.array([0.,.08,-side])
                    local_axis /= np.linalg.norm(local_axis)
                    axis = np.cross(neutral_axis, local_axis)
                    axis_norm = np.linalg.norm(axis)
                    angle = np.arctan2(axis_norm, np.dot(neutral_axis, local_axis))
                    if axis_norm > 1e-8:
                        angles[wrist] = axis / axis_norm * angle
                    elif np.dot(neutral_axis, local_axis) < 0:
                        angles[wrist] = np.array([0., 1., 0.]) * np.pi
                if error > .018:
                    raise ValueError('共享接触点超出手臂可达范围')
            keys.append(dict(frame=t,object_id=prop_id,pelvis_offset_m=(pelvis-center-displacement(t)).tolist(),
                             heading_deg=heading,body_rotvecs=[[round(float(v),6) if v else 0 for v in row] for row in angles[1:22]]))
        p_end_xz = root_at(frames-1)[[0,2]]
        p_end = np.array([p_end_xz[0], .915, p_end_xz[1]])
        keys.append(dict(frame=frames-1,object_id=prop_id,pelvis_offset_m=(p_end-center-displacement(frames-1)).tolist(),
                         heading_deg=heading,body_rotvecs=arms_down))
        path = [dict(frame=0,xz_m=[float(start_xz_i[0]),float(start_xz_i[1])],heading_deg=start_heading_i)]
        path.extend(dict(frame=t,xz_m=root_at(t)[[0,2]].tolist(),heading_deg=heading) for t in times)
        path.append(dict(frame=frames-1,xz_m=p_end_xz.tolist(),heading_deg=heading))
        contacts = [dict(start_frame=start,end_frame=end+1,object_id=prop_id,joint_index=wrist,
                         anchor_m=anchors[wrist],tolerance_m=.05) for wrist,_,_ in arms]
        plan = dict(kind='human_interaction_v1',objects=[prop],waypoints=path,keyframes=keys,contacts=contacts)
        if carrying:
            plan['carry_grip'] = True
        plans.append(plan)
    return plans


def contact_report(kind, plans, joints):
    """Measure both generated tracks together on the shared absolute timeline."""
    from .human_interaction import object_centers

    if len(plans) != 2 or len(joints) != 2:
        raise ValueError('双人场景需要两个角色')
    a,b = (np.asarray(track,float) for track in joints)
    if a.shape != b.shape or a.ndim != 3 or a.shape[1:] != (52,3):
        raise ValueError('两个角色必须使用相同帧数和骨架')
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('双人骨架包含无效坐标')
    windows = []
    for idx,(plan,track) in enumerate(zip(plans,(a,b))):
        obj = plan['objects'][0]
        centers = object_centers(obj,len(track))
        for c in plan['contacts']:
            start,end = c['start_frame'],c['end_frame']
            distal = track[start:end,c['joint_index']]
            distances = np.linalg.norm(distal-centers[start:end]-c['anchor_m'],axis=1)
            windows.append(dict(actor=idx+1,joint=c['joint_index'],start_frame=start,end_frame=end,
                                maximum_m=float(distances.max()),passed=bool(np.all(distances<=c['tolerance_m']))))
    result = dict(contacts=windows,passed=all(w['passed'] for w in windows))
    if kind == 'handshake':
        start,end = windows[0]['start_frame'],windows[0]['end_frame']
        distance = np.linalg.norm(a[start:end,21]-b[start:end,21],axis=1)
        result['wrist_gap_max_m'] = float(distance.max())
        result['passed'] &= result['wrist_gap_max_m'] <= .075
    return result
