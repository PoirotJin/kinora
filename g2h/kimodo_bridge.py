"""Four-phase splice conditioning, separate from ordinary local repair."""
import numpy as np
from scipy.spatial.transform import Rotation

VERSION = 'step-turn-bridge-v3'
STAND_TEXT = ('A person stands upright and relaxed with feet hip-width apart, '
              'arms relaxed beside the body, maintaining balance without stepping.')
TEXTS = (
    'A person smoothly finishes their movement and turns by lifting and replanting alternating feet in small separated steps, shifting weight over the supporting foot without sliding or twisting a planted foot to face the walking path and comes to a relaxed upright standing pose, feet hip-width apart and arms clear of the torso.',
    'A person walks forward along a smooth curved path, turning the body to face the direction of travel, never walking backwards, with alternating hip-width steps on separate tracks, firm planted support feet and relaxed arm swing, following the ground path.',
    'A person slows their walk, keeps facing forward along the path, turns by lifting and replanting alternating feet in small separated steps, shifting weight over the supporting foot without sliding or twisting a planted foot and comes to a balanced relaxed stand at the destination.',
    'A person turns by lifting and replanting alternating feet in small separated steps, shifting weight over the supporting foot without sliding or twisting a planted foot from relaxed standing to face the target direction and smoothly enters the target movement, matching its body pose and direction of motion.',
)
LABELS = ('收势站立', '自然行走', '减速站稳', '接入后段')


def stages(plan):
    count = plan['right'] - plan['left']
    if not 120 <= count <= 540 or min(plan['left'], plan['frames'] - plan['right']) < 3:
        raise ValueError('四阶段过渡需要 4–18 秒，且前后素材至少保留 3 帧上下文')
    # Give walking half of the budget left after reserving one second per phase.
    extra = count - 120
    walk_extra = extra // 2
    other, remainder = divmod(extra - walk_extra, 3)
    counts = [30 + other + (remainder > 0), 30 + walk_extra,
              30 + other + (remainder > 1), 30 + other]
    counts[0] += plan['left']
    counts[-1] += plan['frames'] - plan['right']
    return [{'text': text, 'frames': n} for text, n in zip(TEXTS, counts)]


def forward_path(start, end, start_heading, end_heading, frames):
    """Bounded cubic curve; endpoint turns happen at rest, not by walking backwards.

    Tangents stay within 45 degrees of the displacement. Equal lateral tangent
    signs produce an S curve; aligned clips keep a straight, shorter path.
    Resample by arc length so curvature does not introduce speed spikes.
    """
    delta = np.asarray(end) - start
    distance = np.linalg.norm(delta)
    if distance < .15:
        u = np.linspace(0, 1, frames)
        angle = start_heading + np.angle(np.exp(1j*(end_heading-start_heading))) * (3*u*u-2*u*u*u)
        return np.asarray(start) + u[:, None]*delta, angle
    bearing = np.arctan2(delta[0], delta[1])
    angles = bearing + np.clip(np.angle(np.exp(1j*(np.array([start_heading,end_heading])-bearing))), -np.pi/4, np.pi/4)
    directions = np.column_stack((np.sin(angles),np.cos(angles)))
    control = np.array([start, start + distance*.32*directions[0], end - distance*.32*directions[1], end])
    u = np.linspace(0, 1, max(1001, frames*4))[:,None]
    curve = (1-u)**3*control[0]+3*(1-u)**2*u*control[1]+3*(1-u)*u*u*control[2]+u**3*control[3]
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(curve,axis=0),axis=1))]
    samples = np.linspace(0, arc[-1], frames)
    path = np.column_stack([np.interp(samples,arc,curve[:,j]) for j in range(2)])
    tangent = np.gradient(path,axis=0)
    return path, np.unwrap(np.arctan2(tangent[:,0],tangent[:,1]))


def forward_report(arrays, targets):
    """Measure travel vs pelvis facing during locomotion, excluding slow turns."""
    lo, hi = targets['walk_range']
    points = arrays['native_keypoints3d'].astype(np.float64)
    across = points[:,1]-points[:,2]
    facing = np.column_stack((-across[:,2],across[:,0]))
    facing /= np.maximum(np.linalg.norm(facing,axis=1,keepdims=True),1e-9)
    root = arrays['native_transl'][:,[0,2]].astype(np.float64)
    indices = np.arange(lo+3,hi-2)
    delta = root[indices+3]-root[indices-3]
    speed = np.linalg.norm(delta,axis=1)*5
    moving = speed > .15
    cosine = np.sum(delta*facing[indices],axis=1)/np.maximum(np.linalg.norm(delta,axis=1),1e-9)
    weights = speed[moving]/30
    total = float(weights.sum())
    forward = float(weights[cosine[moving]>=.5].sum()/total) if total else 1.
    backward = float(weights[cosine[moving]<0].sum()/total) if total else 0.
    passed = forward >= .9 and backward <= .05
    return {'passed':passed,'forward_distance_fraction':forward,'backward_distance_fraction':backward,
            'moving_distance_m':total,'speed_threshold_m_s':.15,'forward_cosine_min':.5,
            'limits':{'min_forward_fraction':.9,'max_backward_fraction':.05},
            'failures':[] if passed else ['行走朝向未通过：前进距离占比 %.1f%%，倒走占比 %.1f%%' % (forward*100,backward*100)]}


def conditioning(original, standing, plan, pipeline, rest, parents):
    """Sparse real endpoints and generated standing anchors in one coordinate frame."""
    import torch
    from kimodo.constraints import FullBodyConstraintSet, Root2DConstraintSet, EndEffectorConstraintSet
    from .kimodo_edit import canonical_to_soma
    from .motion_splice import heading
    from .motion_assets import STATIC
    parts = stages(plan)
    first, last = plan['start'], plan['end']
    sliced = {k: v[first:last] if k not in STATIC and v.ndim and len(v) == len(original['native_transl']) else v
              for k, v in original.items()}
    positions, rotations = canonical_to_soma(sliced, rest, parents, pipeline.skeleton)
    stand_pos, stand_rot = canonical_to_soma(standing, rest, parents, pipeline.skeleton)
    # Select the quietest interior frame of the generated standing reference.
    speed = np.linalg.norm(np.diff(stand_pos, axis=0), axis=-1).max(axis=1)
    index = 15 + int(np.argmin(speed[15:45]))
    if speed[index] * 30 > .3:
        raise ValueError('站立参考未稳定：关节速度超过 0.3 m/s，请重新生成')
    root = pipeline.skeleton.root_idx
    a, b = plan['left'], plan['right']
    cuts = np.cumsum([p['frames'] for p in parts])
    indices = list(range(a - 3, a)) + list(range(b, b + 3))
    pos = [positions[i].copy() for i in indices]
    rot = [rotations[i].copy() for i in indices]
    origin = positions[0, root].copy(); origin[1] = 0
    start = positions[a-1, root, [0, 2]] - origin[[0, 2]]
    end = positions[b, root, [0, 2]] - origin[[0, 2]]
    curve, curve_heading = forward_path(start, end, heading(sliced,a-1), heading(sliced,b), 1001)
    for cut, endpoint in ((cuts[0], a - 1), (cuts[2], b)):
        yaw = (curve_heading[0] if endpoint == a-1 else curve_heading[-1]) - heading(standing, index)
        turn = Rotation.from_euler('y', yaw).as_matrix()
        pose = (stand_pos[index] - stand_pos[index, root]) @ turn.T
        target = positions[endpoint, root].copy()
        target[1] = stand_pos[index, root, 1]
        pose += target
        for frame in range(int(cut) - 3, int(cut) + 1):
            indices.append(frame); pos.append(pose.copy()); rot.append(turn @ stand_rot[index])
    indices = np.asarray(indices)
    pos = np.asarray(pos) - origin
    rot = np.asarray(rot)
    order = np.argsort(indices)
    tensor = lambda x, dtype: torch.as_tensor(x, dtype=dtype, device=pipeline.device)
    full = FullBodyConstraintSet(pipeline.skeleton, tensor(indices[order], torch.long),
                                tensor(pos[order], torch.float32), tensor(rot[order], torch.float32))
    # Stop at the two standing anchors. The walk reaches 85% of the distance;
    # deceleration and turning use the remainder during phase three.
    times = [a-1, int(cuts[0])-3, int(cuts[1])-1, int(cuts[2])-3, b]
    points = np.asarray([start, start, start + .85*(end-start), end, end])
    path = positions[:, root][:, [0, 2]] - origin[[0, 2]]
    arriving = (end - start) * .3 / (times[3] - times[2])
    velocities = [path[a-1]-path[a-2], np.zeros(2), arriving, np.zeros(2), path[b+1]-path[b]]
    for i, (lo, hi) in enumerate(zip(times[:-1], times[1:])):
        t = np.linspace(0, 1, hi-lo+1)[:, None]
        path[lo:hi+1] = ((2*t**3-3*t*t+1)*points[i] + (t**3-2*t*t+t)*(hi-lo)*velocities[i]
                        + (-2*t**3+3*t*t)*points[i+1] + (t**3-t*t)*(hi-lo)*velocities[i+1])
    # Map the walking progress onto the curved route, with facing along its tangent.
    travel = np.linalg.norm(end-start)
    progress = np.zeros(plan['frames'])
    u = np.linspace(0,1,times[3]-times[1]+1)
    progress[times[1]:times[3]+1] = 10*u**3-15*u**4+6*u**5
    walk = np.arange(times[1], times[3]+1)
    for axis in range(2):
        path[walk,axis] = np.interp(progress[walk],np.linspace(0,1,len(curve)),curve[:,axis])
    angles = np.array([heading(sliced,i) for i in range(plan['frames'])])
    angles[walk] = np.interp(progress[walk],np.linspace(0,1,len(curve_heading)),curve_heading)
    for lo,hi,target in ((a-1,times[1],curve_heading[0]),(times[3],b,heading(sliced,b))):
        t = np.linspace(0,1,hi-lo+1)
        initial = angles[lo]
        angles[lo:hi+1] = initial + np.angle(np.exp(1j*(target-initial)))*(3*t*t-2*t*t*t)
    # Keep identical standing anchors through the first frame of the next phase.
    for cut,value in ((cuts[0],curve_heading[0]),(cuts[2],curve_heading[-1])):
        angles[int(cut)-3:int(cut)+1] = value
    headings = np.column_stack((np.cos(angles),np.sin(angles)))
    peak_speed = np.linalg.norm(np.diff(path[times[1]:times[3]+1], axis=0), axis=-1).max() * 30
    if peak_speed > 1.6:
        raise ValueError('四阶段行走路径峰值速度 %.2fm/s > 1.60m/s，请增加时长或缩短间距' % peak_speed)
    free = np.setdiff1d(np.arange(plan['frames']), indices)
    # Keep smooth facing while permitting small alternating weight shifts during turns.
    # Endpoint anchors still pin both ends; quintic progress eases walking in and out.
    controlled = free
    for lo, hi in ((a-1,times[1]),(times[3],b)):
        t = np.linspace(0,1,hi-lo+1)
        sway = .035*np.sin(6*np.pi*t)*np.sin(np.pi*t)**2
        path[lo:hi+1] += sway[:,None]*np.column_stack((np.cos(angles[lo:hi+1]),-np.sin(angles[lo:hi+1])))
    root_path = Root2DConstraintSet(pipeline.skeleton, tensor(controlled, torch.long), tensor(path[controlled], torch.float32),
                                    global_root_heading=tensor(headings[controlled],torch.float32))
    # Heading features alone do not fix pelvis rotation in the decoded motion.
    # Sparse native pelvis constraints guide actual facing while limbs and the
    # intervening frames remain generated by Kimodo.
    pelvis_frames = free[(free >= times[1]) & (free <= times[3])][::10]
    yaw = angles[pelvis_frames] - heading(standing, index)
    turn = Rotation.from_euler('y', yaw).as_matrix()
    pelvis_pos = np.einsum('tij,kj->tki', turn, stand_pos[index]-stand_pos[index,root])
    pelvis_pos[:,:,0] += path[pelvis_frames,0,None]
    pelvis_pos[:,:,2] += path[pelvis_frames,1,None]
    pelvis_pos[:,:,1] += stand_pos[index,root,1]
    pelvis_rot = turn[:,None] @ stand_rot[index]
    class DeviceEndEffector(EndEffectorConstraintSet):
        # Native crop_move rebuilds joint indices on CPU even for CUDA frames.
        # It preserves type(self), so normalize indices on every cropped copy.
        def update_constraints(self, data_dict, index_dict):
            self.pos_indices = self.pos_indices.to(self.frame_indices.device)
            self.rot_indices = self.rot_indices.to(self.frame_indices.device)
            return super().update_constraints(data_dict, index_dict)
    pelvis = DeviceEndEffector(pipeline.skeleton, tensor(pelvis_frames, torch.long),
        tensor(pelvis_pos, torch.float32), tensor(pelvis_rot, torch.float32),
        tensor(path[pelvis_frames], torch.float32), joint_names=['Hips'])
    targets = {'indices': indices[order], 'positions': pos[order] + origin, 'walk_range': (times[1],times[3])}
    return [full, root_path, pelvis], origin, parts, targets


def anchor_report(arrays, targets, pipeline, rest, parents):
    from .kimodo_edit import canonical_to_soma
    positions, _ = canonical_to_soma(arrays, rest, parents, pipeline.skeleton)
    errors = np.linalg.norm(positions[targets['indices']] - targets['positions'], axis=-1).max(axis=1)
    failed = [(int(frame), float(error)) for frame, error in zip(targets['indices'], errors) if error > .08]
    return {'passed': not failed, 'limit_m': .08, 'max_error_m': float(errors.max()),
            'failures': ['关键帧 %d 偏差 %.3fm > 0.080m' % item for item in failed]}
