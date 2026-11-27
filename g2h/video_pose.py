"""Fit tracked landmarks to the canonical rig for shot-local Kimodo conditioning."""
import numpy as np
from scipy.spatial.transform import Rotation

from .human_rig import load
from .kimodo_adapter import fk


def unit(v):
    v = np.asarray(v, dtype=float)
    length = np.linalg.norm(v,axis=-1,keepdims=True)
    safe = length >= 1e-6
    out = np.divide(v, np.where(safe, length, 1.))
    if v.ndim == 1:
        return out if bool(safe) else np.array([0., 0., 1.], dtype=float)
    fallback = np.zeros_like(out)
    fallback[..., 2] = 1.
    return np.where(safe, out, fallback)


def torso_frame(points):
    left = unit(points[:,11]+points[:,23]-points[:,12]-points[:,24])
    up = unit(points[:,11]+points[:,12]-points[:,23]-points[:,24])
    cross = np.cross(left,up)
    bad = np.linalg.norm(cross,axis=-1) < 1e-6
    forward = unit(cross)
    if np.any(bad):
        alt = np.where(np.abs(left[:,0:1]) < .9, [1.,0.,0.], [0.,1.,0.])
        forward[bad] = unit(np.cross(left[bad], alt[bad]))
    return np.stack([left,unit(np.cross(forward,left)),forward],axis=-1)


def fit_observations(world, confidence, shots):
    """Reset camera yaw at cuts, retain within-shot turns, infer travel from support feet."""
    rest,parents = load()
    observed = [11,12,13,14,15,16,23,24,25,26,27,28,31,32]
    torso_joints = (11,12,23,24)
    local = np.zeros((len(world),52,3))
    root = np.zeros((len(world),3))
    reliability = np.zeros(len(world))
    reports = []
    last_pose = None
    for shot in shots:
        a,b = shot['start_frame'],shot['end_frame']
        points = world[a:b].astype(float).copy()
        scores = confidence[a:b]
        raw_valid = np.isfinite(points).all(axis=-1)
        has_joint = {}
        for joint in observed:
            good = np.flatnonzero(raw_valid[:,joint])
            has_joint[joint] = bool(len(good))
            if len(good):
                for k in range(3):
                    points[:,joint,k] = np.interp(np.arange(len(points)),good,points[good,joint,k])
        torso_valid = raw_valid[:,list(torso_joints)].all(axis=1)
        if all(has_joint[j] for j in torso_joints) and int(torso_valid.sum()) >= 3:
            torso = torso_frame(points)
        else:
            torso = np.broadcast_to(np.eye(3),(len(points),3,3)).copy()
        yaw = np.arctan2(torso[:,0,2],torso[:,2,2])
        # A monocular cut changes camera coordinates. Align the new heading to the
        # preceding one only when limb configuration supports continuous action.
        previous_heading = 0.
        if a:
            prev = Rotation.from_rotvec(local[a-1,0]).as_matrix()
            previous_heading = np.arctan2(prev[0,2],prev[2,2])
        alignment = Rotation.from_euler('y',previous_heading-yaw[0]).as_matrix()
        points = points @ alignment.T
        torso = alignment @ torso
        matrices = np.broadcast_to(np.eye(3),(len(points),52,3,3)).copy()
        matrices[:,0] = torso
        global_rot = matrices.copy()
        bones = {1:(4,23,25),2:(5,24,26),4:(7,25,27),5:(8,26,28),
                 7:(10,27,31),8:(11,28,32),16:(18,11,13),17:(19,12,14),
                 18:(20,13,15),19:(21,14,16)}
        for j in range(1,52):
            inherited = global_rot[:,parents[j]]
            if j in bones:
                child,first,last = bones[j]
                if has_joint[first] and has_joint[last]:
                    target = np.einsum('tji,tj->ti',inherited,unit(points[:,last]-points[:,first]))
                    source = unit(rest[child]-rest[j])
                    cross = np.cross(source,target)
                    sine = np.linalg.norm(cross,axis=1)
                    angle = np.arctan2(sine,target@source)
                    axis = cross/np.maximum(sine[:,None],1e-10)
                    opposite = (sine<1e-8) & (angle>1.)
                    if opposite.any():
                        fallback = np.eye(3)[np.argmin(abs(source))]
                        axis[opposite] = unit(np.cross(source,fallback))
                    matrices[:,j] = Rotation.from_rotvec(axis*angle[:,None]).as_matrix()
            global_rot[:,j] = inherited @ matrices[:,j]
        pose,_ = fk(matrices,rest,parents)
        feet = pose[:,[10,11]]
        horizontal = np.zeros((len(pose),2))
        weights = np.exp(-(feet[:,:,1]-feet[:,:,1].min(axis=1,keepdims=True))/.025)
        weights /= weights.sum(axis=1,keepdims=True)
        steps = -(np.diff(feet,axis=0)*weights[1:,:,None]).sum(axis=1)
        horizontal[1:] = np.cumsum(steps[:,[0,2]],axis=0)
        if a:
            horizontal += root[a-1,[0,2]]
        root[a:b,0],root[a:b,2] = horizontal.T
        root[a:b,1] = -feet[:,:,1].min(axis=1)
        local[a:b] = Rotation.from_matrix(matrices.reshape(-1,3,3)).as_rotvec().reshape(-1,52,3)
        frac_detected = raw_valid[:,observed].mean(axis=1)
        base_rel = np.quantile(scores[:,observed],.25,axis=1)
        reliability[a:b] = base_rel * np.where(frac_detected < 1., .5 * frac_detected, 1.)
        jump = 0. if last_pose is None else float(np.linalg.norm(pose[0,:22]-last_pose,axis=1).max())
        last_pose = pose[-1,:22]
        reports.append(dict(start_frame=a,end_frame=b,cut_joint_jump_m=jump,continuous=jump<=.22))
    return dict(smpl_poses=local.reshape(-1,156),native_transl=root-rest[0]),reliability,reports


def scene_conditions(arrays, reliability, shots, first, end):
    """Resample observed FK poses as world-space Kimodo keys for a bounded segment."""
    from .human_rig import load

    if not 0 <= first < end <= len(reliability) or end-first>150:
        raise ValueError('视频约束段须在 1–150 帧内')
    rest,_ = load()
    frames = end-first
    duration = (frames+29)//30
    pose = arrays['smpl_poses'].reshape(-1,52,3)
    roots = arrays['native_transl'] + rest[0]
    # Select reliable observations per time bin. Leave a short reconstruction
    # interval around each camera change instead of constraining its pose jump.
    cuts = [s['start_frame'] for s in shots[1:]]
    anchors = {first,end-1}
    for a in range(first+1,end-1,5):
        eligible = [i for i in range(a,min(a+5,end-1))
                    if reliability[i]>=.45 and all(abs(i-cut)>4 for cut in cuts)]
        if eligible:
            anchors.add(max(eligible,key=lambda i:reliability[i]))
    if len(anchors)<3 and frames>=15:
        candidates = [i for i in range(first+1,end-1)
                      if i not in anchors and all(abs(i-cut)>4 for cut in cuts)]
        if candidates:
            candidates.sort(key=lambda i: float(reliability[i]), reverse=True)
            anchors.update(candidates[:3-len(anchors)])
        if len(anchors)<3:
            anchors.add((first+end)//2)
            for i in sorted(range(first+1,end-1), key=lambda idx: abs(idx-(first+end)//2)):
                if len(anchors)>=3:
                    break
                anchors.add(i)
    sorted_anchors = sorted(anchors)
    chunks = []
    cur = []
    for i in sorted_anchors:
        cand = cur + [i]
        span = roots[cand].max(axis=0) - roots[cand].min(axis=0)
        if cur and float(span.max()) > 9.0:
            chunks.append(cur)
            cur = [i]
        else:
            cur = cand
    if cur:
        chunks.append(cur)

    chunk_refs = []
    for chunk in chunks:
        cmin, cmax = roots[chunk].min(axis=0), roots[chunk].max(axis=0)
        mid = (cmin + cmax) * .5
        ref_y = -1. if (-5.5 <= float(cmin[1]) and float(cmax[1]) <= 3.5) else round(float(mid[1]), 5)
        chunk_refs.append([round(float(mid[0]), 5), ref_y, round(float(mid[2]), 5)])

    reference = chunk_refs[0]
    obj = dict(id='video-reference', center_m=reference, size_m=[.01]*3, heading_deg=0)
    if len(chunks) > 1:
        track = []
        for chunk, ref in zip(chunks, chunk_refs):
            track.append(dict(frame=chunk[0] - first, center_m=ref))
            if chunk[-1] != chunk[0]:
                track.append(dict(frame=chunk[-1] - first, center_m=ref))
        obj['trajectory'] = track

    keys = []
    route = []
    for chunk, ref in zip(chunks, chunk_refs):
        ref_arr = np.asarray(ref, dtype=float)
        for i in chunk:
            rot = Rotation.from_rotvec(pose[i,0]).as_matrix()
            yaw = float(np.rad2deg(np.arctan2(rot[0,2],rot[2,2])))
            offset = roots[i] - ref_arr
            if i - first > 0:
                offset = np.clip(np.round(offset, 5), -5.0, 5.0)
            else:
                offset = np.round(offset, 5)
            pelvis = offset.tolist()
            body = pose[i,1:22].copy()
            # Redistribute residual root tilt to the three root children. The scene
            # constraint represents yaw alone at the pelvis.
            tilt = Rotation.from_euler('y',-yaw,degrees=True) * Rotation.from_matrix(rot)
            body[:3] = (tilt*Rotation.from_rotvec(body[:3])).as_rotvec()
            keys.append(dict(frame=i-first,object_id='video-reference',pelvis_offset_m=pelvis,
                             heading_deg=yaw,body_rotvecs=body.round(5).tolist()))
            route.append(dict(frame=i-first,xz_m=roots[i,[0,2]].round(5).tolist(),heading_deg=yaw))
    if frames<duration*30:
        keys.append(dict(keys[-1],frame=duration*30-1))
        route.append(dict(route[-1],frame=duration*30-1))
    return dict(duration=duration,
                text='A person performs the recorded movement, following the observed limb poses, body turns and foot timing.',
                scene=dict(kind='human_interaction_v1',
                           objects=[obj],
                           keyframes=keys,waypoints=route,contacts=[]))
