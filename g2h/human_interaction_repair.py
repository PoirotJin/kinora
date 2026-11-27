"""Repair sampled Human52/object penetration while retaining verified anchors."""
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .human_interaction import MAX_PENETRATION_M, object_centers, report

VERSION = 'human-interaction-repair-v2'
FRACTIONS = (.25, .5, .75)


def personal_wrist_misses(scene, quality):
    if scene.get('attachments') or scene.get('carry_grip'):
        return []
    assets = {obj['id'] for obj in scene['objects'] if 'asset_version' in obj}
    return [contact for contact, measurement in zip(scene['contacts'], quality['contacts'])
            if contact['object_id'] in assets and contact['joint_index'] in (20, 21)
            and not measurement['passed']]


def _fit_personal_contacts(arrays, scene, rest, parents, contacts):
    from .carry_grip import fit_window
    from .motion_continuity import body_steps

    objects = {obj['id']: obj for obj in scene['objects']}
    protected = {key['frame'] for key in scene['keyframes']}
    ramp = 8
    candidate = arrays
    for contact in contacts:
        start, end = contact['start_frame'], contact['end_frame']
        if any(start - ramp <= f < end + ramp for f in protected):
            return arrays
        candidate = fit_window(candidate, objects[contact['object_id']], start, end,
                               [contact['anchor_m']], rest, parents, ramp_frames=ramp,
                               joints=(contact['joint_index'],))
    if not report(candidate, scene, rest, parents)['passed']:
        return arrays

    def continuity(motion):
        points = motion['native_keypoints3d']
        world = points - points[:, :1] + motion['native_transl'][:, None]
        local = Rotation.from_rotvec(motion['smpl_poses'].reshape(-1, 3)).as_matrix().reshape(-1, 52, 3, 3)
        step, turn = body_steps(world, local)
        accel = np.linalg.norm(np.diff(world, n=2, axis=0), axis=-1)
        return np.array([step.max(), turn.max(), accel.max()])

    limits = np.maximum([.20, 45., 120/900], continuity(arrays) + 1e-5)
    return candidate if np.all(continuity(candidate) <= limits) else arrays


def repair(arrays, scene, rest, parents, *, quality=None):
    if quality is None:
        quality = report(arrays, scene, rest, parents)
    misses = personal_wrist_misses(scene, quality)
    if misses:
        fitted = _fit_personal_contacts(arrays, scene, rest, parents, misses)
        if fitted is not arrays:
            report_after = report(fitted, scene, rest, parents)
            changed = np.any(fitted['smpl_poses'] != arrays['smpl_poses'], axis=1)
            return fitted, dict(version='personal-wrist-fit-v1', status='repaired',
                                max_penetration_m=report_after['max_skeleton_penetration_m'],
                                modified_frames=int(changed.sum()))
    if scene.get('carry_grip'):
        from .carry_grip import fit
        gripped = fit(arrays, scene, rest, parents)
        after = report(gripped,scene,rest,parents)
        if after['passed']:
            return gripped, dict(version='two-hand-grip-v1',status='repaired',
                                 max_penetration_m=after['max_skeleton_penetration_m'])
        arrays = gripped
    first, audit, tried_over = _repair(arrays, scene, rest, parents, lift_feet=True)
    if tried_over and audit['penetrating_frames_after'] > len(audit['protected_penetrating_frame_indices']):
        second, alternate, _ = _repair(arrays, scene, rest, parents, lift_feet=False)
        if (alternate['penetrating_frames_after'], alternate['max_penetration_m']) < (
                audit['penetrating_frames_after'], audit['max_penetration_m']):
            first, audit = second, alternate
    if audit['penetrating_frames_after'] > len(audit['protected_penetrating_frame_indices']):
        from .human_interaction_approach import repair_top_approach

        updated, approach = repair_top_approach(first, scene, rest, parents)
        if approach is not None:
            audit.update(approach=approach, status='repaired',
                         penetrating_frames_after=0, max_penetration_m=approach['max_penetration_m'])
            changed = np.any(updated['smpl_poses'] != arrays['smpl_poses'], axis=1)
            changed |= np.any(updated['native_transl'] != arrays['native_transl'], axis=1)
            audit['modified_frames'] = int(changed.sum())
            first = updated
    return first, audit


def _repair(arrays, scene, rest, parents, *, lift_feet):
    from .kimodo_adapter import fk

    before = report(arrays, scene, rest, parents, frame_distances=True)
    original = np.asarray(arrays['native_keypoints3d'], dtype=float)
    translation = np.asarray(arrays['native_transl'], dtype=float)
    angles = np.asarray(arrays['smpl_poses'], dtype=float).reshape(-1,52,3)
    frames = len(translation)
    parent = np.asarray(parents)[1:]
    rest = np.asarray(rest)
    box_data = [(object_centers(obj, frames),
                 Rotation.from_euler('y',obj['heading_deg'],degrees=True).as_matrix(),
                 np.asarray(obj['size_m'])/2) for obj in scene['objects']]

    def samples(points):
        start = points[...,parent,:]
        delta = points[...,1:,:]-start
        return np.concatenate([points, *(start+f*delta for f in FRACTIONS)],axis=-2)

    def penetration(points, frame):
        shape = samples(points)
        depth = [np.maximum(0., np.min(half-np.abs((shape-centers[frame]) @ rotation),axis=-1))
                 for centers,rotation,half in box_data]
        return np.asarray(depth)

    world = original-original[:,:1]+translation[:,None]+rest[0]
    baseline = np.array([penetration(points,f).max() for f,points in enumerate(world)])
    violating = baseline > MAX_PENETRATION_M
    protected = np.zeros(frames,bool)
    protected[[k['frame'] for k in scene['keyframes']]] = True
    contact_anchors = {}
    for source,contact in zip(scene['contacts'],before['contacts']):
        start,end=contact['start_frame'],contact['end_frame']
        valid = np.asarray(contact['distance_m']) <= source['tolerance_m']
        # A valid wrist contact cannot freeze a penetrating elbow/forearm.
        # Keep clear contact frames bitwise fixed; constrain the contact point
        # while repairing only penetrating non-keyframes. Final contact gates
        # below must still pass, as must every original geometric threshold.
        protected[start:end] |= valid & ~violating[start:end]
        for frame in np.arange(start,end)[valid]:
            contact_anchors.setdefault(int(frame),set()).add(contact['joint_index'])
    # Keep terminal samples fixed when they are already clear.
    protected[[0,frames-1]] |= baseline[[0,frames-1]] <= MAX_PENETRATION_M
    bad = np.flatnonzero(violating & ~protected)
    summary = {'version':VERSION, 'protected_frames':int(protected.sum()),
               'protected_penetrating_frame_indices':np.flatnonzero(violating & protected).tolist(),
               'penetrating_frames_before':int(violating.sum()),
               'penetrating_frames_after':int(violating.sum()),
               'max_penetration_m':float(baseline.max()),
               'modified_frames':0, 'status':'unchanged'}
    if not len(bad):
        return arrays, summary, False

    # Map each sampled bone point to its child and the joints that can move it.
    sample_joint = np.r_[np.arange(52),np.tile(np.arange(1,52),len(FRACTIONS))]
    changes = np.zeros((frames,66))
    target = MAX_PENETRATION_M-.006
    solved = set()
    tried_over = False
    for f in bad:
        affected = set()
        active = penetration(world[f], f) > target
        for joint in sample_joint[np.any(active, axis=0)]:
            while joint>0:
                joint = int(parents[joint])
                if 0<joint<=21:
                    affected.add(joint)
        joints = sorted(affected)
        columns = np.r_[np.arange(3), np.array([joint*3+axis for joint in joints for axis in range(3)],dtype=int)]
        limit = np.r_[[.19,.16,.19],np.full(len(columns)-3,.48)]
        floor = min(0.,world[f,[7,8,10,11],1].min())
        previous = changes[f-1,columns] if f and f-1 in solved else np.zeros(len(columns))

        def candidate(delta):
            pose = angles[f].copy()
            pose[joints] += delta[3:].reshape(-1,3)
            positions,_ = fk(Rotation.from_rotvec(pose).as_matrix()[None],rest,parents)
            return positions[0]+translation[f]+delta[:3]

        def residual(delta):
            points = candidate(delta)
            anchors = sorted(contact_anchors.get(int(f), []))
            return np.r_[45*np.maximum(0.,penetration(points,f).ravel()-target),
                         18*np.maximum(0.,floor-points[[7,8,10,11],1]),
                         100*(points[anchors]-world[f,anchors]).ravel(),
                         np.r_[[1.5]*3,[.21]*(len(columns)-3)]*delta,
                         .45*(delta-previous)]

        initial = previous.copy()
        # An ankle or toe inside a low obstacle needs an upward escape path.
        # Starting at the nearest face can send a low foot under the obstacle.
        for touches, (centers, _, half) in zip(active, box_data):
            top = centers[f, 1] + half[1]
            feet = sample_joint[touches]
            if lift_feet and top < world[f, [4, 5], 1].mean() and np.isin(feet, [7, 8, 10, 11]).any():
                initial[1] = max(initial[1], min(.14, max(.05, top-world[f, feet, 1].min())))
                tried_over = True
        fit = least_squares(residual,initial,bounds=(-limit,limit),max_nfev=20,ftol=2e-4,diff_step=1e-3)
        if penetration(candidate(fit.x),f).max() < baseline[f]-1e-5:
            changes[f,columns]=fit.x
            solved.add(f)

    # Blend over adjacent clear frames with an explicit guard around anchors.
    smooth=gaussian_filter1d(changes,1.2,axis=0)
    frame_indices = np.arange(frames)
    distance=np.full(frames,5.)
    for f in np.flatnonzero(protected):
        distance=np.minimum(distance,np.abs(frame_indices-f))
    smooth *= np.minimum(distance/3,1)[:,None]
    # Extend the fitted correction into clear neighbors before fading out.
    # In particular, a sparse two-frame collision should not create a sharp
    # acceleration when the next frame returns to the unchanged trajectory.
    extended = changes.copy()
    if solved:
        fitted = np.array(sorted(solved))
        offsets = np.abs(frame_indices[:, None] - fitted)
        nearest = fitted[offsets.argmin(axis=1)]
        for f in np.flatnonzero(~protected & ~violating):
            source = nearest[f]
            if not protected[min(f, source):max(f, source)+1].any():
                extended[f] = changes[source] * max(0., 1 - abs(source-f)/4) * min(1., distance[f]/2)
    best = arrays
    best_cost = (int((baseline>MAX_PENETRATION_M).sum()),float(baseline.max()))
    old_step = float(np.linalg.norm(np.diff(world,axis=0),axis=-1).max())
    old_accel = float(np.linalg.norm(np.diff(world,n=2,axis=0),axis=-1).max())
    for delta in (.65*changes+.35*smooth, .30*changes+.70*smooth, changes,
                  extended, .80*extended+.20*gaussian_filter1d(extended, .85, axis=0),
                  .65*extended, .40*extended):
        delta = delta.copy()
        delta[protected]=0
        trial_pose=angles.copy()
        trial_pose[:,1:22]+=delta[:,3:].reshape(frames,21,3)
        trial_trans=translation+delta[:,:3]
        matrices=Rotation.from_rotvec(trial_pose.reshape(-1,3)).as_matrix().reshape(frames,52,3,3)
        xyz,_=fk(matrices,rest,parents)
        modified = np.any(delta!=0,axis=1)
        selected=np.flatnonzero(modified)
        xyz[~modified]=original[~modified]
        new_world=xyz-xyz[:,:1]+trial_trans[:,None]+rest[0]
        new_depth=np.array([penetration(p,f).max() for f,p in enumerate(new_world)])
        cost=(int((new_depth>MAX_PENETRATION_M).sum()),float(new_depth.max()))
        if (cost>=best_cost or new_depth.max()>baseline.max()+1e-6
                or np.any(~violating & (new_depth>MAX_PENETRATION_M))):
            continue
        if (np.linalg.norm(np.diff(new_world,axis=0),axis=-1).max()>max(.20,old_step+1e-5)
                or np.linalg.norm(np.diff(new_world,n=2,axis=0),axis=-1).max()>max(120/900,old_accel+1e-5)):
            continue
        updated=dict(arrays)
        for name,value in {
            'native_keypoints3d':xyz,'native_transl':trial_trans,'smpl_trans':trial_trans,
            'smpl_poses':trial_pose.reshape(arrays['smpl_poses'].shape),'smpl_Rh':trial_pose[:,0],
            'native_root_rotations_mat':matrices[:,0],'native_rot6d':matrices[...,:2].reshape(frames,52,6),
        }.items():
            if name in arrays:
                updated[name]=arrays[name].copy()
                updated[name][selected]=value[selected]
        after=report(updated,scene,rest,parents,frame_distances=True)
        if any(np.any((np.asarray(a['distance_m'])<=item['tolerance_m']) &
                      (np.asarray(b['distance_m'])>item['tolerance_m']))
               for item,a,b in zip(scene['contacts'],before['contacts'],after['contacts'])):
            continue
        best,best_cost=updated,cost
        summary.update(status='repaired', modified_frames=len(selected),
                       penetrating_frames_after=cost[0], max_penetration_m=cost[1])
    return best, summary, tried_over
