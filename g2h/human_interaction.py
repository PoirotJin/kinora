"""Environment interactions for the canonical Human52 motion path."""
import json
import math
import re

VERSION = 'human_interaction_v1'
MAX_SCENE_BYTES = 64 * 1024
MAX_PENETRATION_M = .025
MAX_KEYFRAME_ERROR_M = .12
BONE_SAMPLE_FRACTIONS = (.25, .5, .75)


def is_interaction(scene):
    return isinstance(scene, dict) and scene.get('kind') == VERSION


def validate(scene, frames):
    from .kimodo_scene import _number, _vector

    def entries(field, minimum, maximum):
        items = scene[field]
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ValueError(f'无效的人体场景 {field}')
        return items

    def keys(item, fields):
        if not isinstance(item, dict) or set(item) != set(fields.split()):
            raise ValueError('人体场景字段无效：' + fields)

    def frame(value):
        return type(value) is int and 0 <= value < frames

    if not is_interaction(scene) or set(scene)-{'carry_grip','attachments'} != {'kind', 'objects', 'waypoints', 'keyframes', 'contacts'} or frames < 30:
        raise ValueError('人体交互场景需要 objects、waypoints、keyframes 和 contacts')
    objects = {}
    for item in entries('objects', 1, 16):
        if not isinstance(item, dict) or set(item)-{'trajectory','asset_version'} != {'id','center_m','size_m','heading_deg'}:
            raise ValueError('无效的场景物体字段')
        name = item['id']
        if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', name)
                or name in objects or not _vector(item['center_m'], 3, 100)
                or not _vector(item['size_m'], 3, 20) or min(item['size_m']) < .01
                or not _number(item['heading_deg'], 360)):
            raise ValueError('无效或重复的场景物体')
        if 'asset_version' in item and (not isinstance(item['asset_version'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', item['asset_version'])):
            raise ValueError('无效的交互物体版本')
        if 'trajectory' in item:
            track = item['trajectory']
            if not isinstance(track, list) or not 1 <= len(track) <= 128:
                raise ValueError('物体轨迹需要 1–128 个世界坐标关键点')
            previous = -1
            for point in track:
                keys(point, 'frame center_m')
                if not frame(point['frame']) or point['frame'] <= previous or not _vector(point['center_m'], 3, 100):
                    raise ValueError('无效的物体轨迹关键点')
                previous = point['frame']
            if track[0] != {'frame':0, 'center_m':item['center_m']}:
                raise ValueError('物体轨迹必须从初始位置开始')
        objects[name] = item

    previous = -1
    for item in entries('waypoints', 1, 128):
        if isinstance(item, dict) and 'tolerance_m' in item:
            fields = 'frame xz_m heading_deg tolerance_m'
            if 'through_frame' in item:
                fields += ' through_frame'
                if not (frame(item['frame']) and frame(item['through_frame'])
                        and item['frame'] <= item['through_frame']):
                    raise ValueError('无效的目标点动作区间')
            keys(item, fields)
            if not _number(item['tolerance_m'], .25) or item['tolerance_m'] <= 0:
                raise ValueError('无效的目标点位置容差')
        else:
            keys(item, 'frame xz_m heading_deg')
        if (not frame(item['frame']) or item['frame'] <= previous
                or not _vector(item['xz_m'], 2, 100) or not _number(item['heading_deg'], 360)):
            raise ValueError('无效的人体场景路径点')
        previous = item['frame']
    if scene['waypoints'][0]['frame'] != 0:
        raise ValueError('首个路径点必须在第 0 帧')

    previous = -1
    for item in entries('keyframes', 0, 32):
        keys(item, 'frame object_id pelvis_offset_m heading_deg body_rotvecs')
        angles = item['body_rotvecs']
        if (not frame(item['frame']) or item['frame'] <= previous
                or not isinstance(item['object_id'], str) or item['object_id'] not in objects
                or not _number(item['heading_deg'], 360)
                or not isinstance(angles, list) or len(angles) != 21
                or not all(_vector(row, 3, math.pi) for row in angles)):
            raise ValueError('无效的物体相对人体关键帧')
        offset = item['pelvis_offset_m']
        first = item['frame'] == 0
        if not _vector(offset, 3, 300 if first else 5):
            raise ValueError('无效的物体相对人体关键帧')
        if first and not _number(offset[1], 5):
            # A standing start beside a tall personal asset is relative to its box center.
            obj = objects[item['object_id']]
            if 'asset_version' not in obj or not _number(obj['center_m'][1] + offset[1], 5):
                raise ValueError('无效的物体相对人体关键帧')
        previous = item['frame']

    for item in entries('contacts', 0, 32):
        keys(item, 'start_frame end_frame object_id joint_index anchor_m tolerance_m')
        name = item['object_id']
        if (not frame(item['start_frame']) or type(item['end_frame']) is not int
                or not item['start_frame'] < item['end_frame'] <= frames
                or type(item['joint_index']) is not int or not 0 <= item['joint_index'] < 52
                or not isinstance(name, str) or name not in objects
                or not _vector(item['anchor_m'], 3, 20)
                or not _number(item['tolerance_m'], .15) or item['tolerance_m'] < .005):
            raise ValueError('无效的关节接触区间或接触容差')
        edge = max(abs(x)-size/2 for x, size in zip(item['anchor_m'], objects[name]['size_m']))
        if abs(edge) > .005:
            raise ValueError('接触目标必须位于物体表面')

    if 'carry_grip' in scene:
        contacts = scene['contacts']
        if (scene['carry_grip'] is not True or len(objects) != 1 or len(contacts) != 2
                or {c['joint_index'] for c in contacts} != {20,21}
                or any(abs(c['anchor_m'][2]) < .005 for c in contacts)
                or contacts[0]['start_frame'] != contacts[1]['start_frame']
                or contacts[0]['end_frame'] != contacts[1]['end_frame']):
            raise ValueError('搬箱需要双腕同时握住同一物体的边缘')

    if 'attachments' in scene:
        for item in entries('attachments', 0, 8):
            keys(item, 'object_id start_frame end_frame mode joints anchors_m')
            name = item['object_id']
            if (not isinstance(name, str) or name not in objects
                    or not frame(item['start_frame']) or type(item['end_frame']) is not int
                    or not item['start_frame'] < item['end_frame'] <= frames
                    or item['mode'] not in ('lift', 'carry', 'co_carry', 'follow_hands', 'place')):
                raise ValueError('无效的物体附着窗口或模式')
            joints = item['joints']
            anchors = item['anchors_m']
            if (not isinstance(joints, list) or not 1 <= len(joints) <= 2
                    or any(type(j) is not int or j not in (20, 21) for j in joints)
                    or len(set(joints)) != len(joints)
                    or not isinstance(anchors, list) or len(anchors) != len(joints)):
                raise ValueError('无效的物体附着关节或锚点数量')
            for anchor in anchors:
                if not _vector(anchor, 3, 20):
                    raise ValueError('无效的物体附着锚点坐标')
                edge = max(abs(x) - size / 2 for x, size in zip(anchor, objects[name]['size_m']))
                if abs(edge) > .005:
                    raise ValueError('附着锚点必须位于物体表面')


def object_centers(obj, frames):
    import numpy as np

    track = obj.get('trajectory')
    if not track:
        return np.broadcast_to(obj['center_m'], (frames, 3))
    times = [point['frame'] for point in track]
    return np.column_stack([np.interp(np.arange(frames), times, axis)
                            for axis in np.asarray([point['center_m'] for point in track]).T])


def check(scene, frames):
    import numpy as np
    from scipy.spatial.transform import Rotation

    validate(scene, frames)
    encode(scene)
    world_targets(scene, frames)
    objects = {obj['id']: obj for obj in scene['objects']}
    occupied = {}
    for contact in scene['contacts']:
        start, end = contact['start_frame'], contact['end_frame']
        obj = objects[contact['object_id']]
        position = Rotation.from_euler('y', obj['heading_deg'], degrees=True).apply(contact['anchor_m'])
        target = object_centers(obj, frames)[start:end] + position
        joint = contact['joint_index']
        if joint not in occupied:
            occupied[joint] = np.full((frames, 3), np.nan)
        prior = occupied[joint][start:end]
        conflict = np.flatnonzero(np.linalg.norm(prior-target, axis=1) > 1e-6)
        if len(conflict):
            raise ValueError(f'同一帧的关节接触目标冲突：关节 {joint}，帧 {start + int(conflict[0])}')
        prior[:] = target


def skeleton_samples(world, parents):
    import numpy as np

    parent_points = world[..., np.asarray(parents)[1:], :]
    bones = world[..., 1:, :] - parent_points
    return np.concatenate([world, *(parent_points + bones*f for f in BONE_SAMPLE_FRACTIONS)], axis=-2)


def world_targets(scene, frames):
    import numpy as np
    from scipy.spatial.transform import Rotation

    objects = {item['id']: item for item in scene['objects']}
    centers = {name:object_centers(item, frames) for name,item in objects.items()}
    knots = {p['frame']: (p['xz_m'], p['heading_deg']) for p in scene['waypoints']}
    roots, angles, keyframes = [], [], []
    for key in scene['keyframes']:
        obj = objects[key['object_id']]
        root = centers[key['object_id']][key['frame']] + Rotation.from_euler(
            'y', obj['heading_deg'], degrees=True).apply(key['pelvis_offset_m'])
        heading = obj['heading_deg'] + key['heading_deg']
        index = key['frame']
        if index in knots:
            xy, previous = knots[index]
            if np.linalg.norm(root[[0, 2]] - xy) > .001 or abs((heading-previous+180) % 360-180) > .1:
                raise ValueError('同一帧的路径点与物体相对关键帧冲突')
        knots[index] = (root[[0, 2]], heading)
        roots.append(root)
        angles.append(heading)
        keyframes.append(index)
    indices = sorted(knots)
    positions = np.asarray([knots[i][0] for i in indices])
    headings = np.unwrap(np.deg2rad([knots[i][1] for i in indices]))
    samples = np.arange(frames)
    path = np.column_stack([np.interp(samples, indices, axis) for axis in positions.T])
    return np.asarray(keyframes, dtype=int), np.asarray(roots).reshape(-1, 3), angles, path, np.interp(samples, indices, headings)


def sole_height_offset(rest, skeleton):
    """Return vertical offset (human_sole - soma_sole) so Y=0 soles align in SOMA."""
    import numpy as np

    rest_arr = np.asarray(rest, dtype=np.float64)
    human_sole = float(rest_arr[[7, 8, 10, 11], 1].min() - rest_arr[0, 1])
    names = getattr(skeleton, 'bone_order_names', None)
    neutral = getattr(skeleton, 'neutral_joints', None)
    if names is None or neutral is None:
        return 0.0
    names = list(names)
    foot_names = ('LeftFoot', 'LeftToeBase', 'RightFoot', 'RightToeBase')
    if not all(name in names for name in foot_names) or 'Hips' not in names:
        return 0.0
    src_rest = neutral.detach().cpu().numpy() if hasattr(neutral, 'detach') else np.asarray(neutral)
    src_rest = np.asarray(src_rest, dtype=np.float64)
    foot_ids = [names.index(name) for name in foot_names]
    hips_idx = names.index('Hips')
    soma_sole = float(src_rest[foot_ids, 1].min() - src_rest[hips_idx, 1])
    return human_sole - soma_sole


def conditioning(scene, frames, pipeline, rest, parents):
    import numpy as np
    from scipy.spatial.transform import Rotation
    from .kimodo_edit import canonical_to_soma
    from .kimodo_scene import make_constraints

    indices, roots, angles, path, heading = world_targets(scene, frames)
    origin = np.array([path[0, 0], 0., path[0, 1]])
    if not len(indices):
        return make_constraints(pipeline, np.arange(frames), indices, path, heading, origin, None, None)
    body = np.asarray([key['body_rotvecs'] for key in scene['keyframes']])
    if len(indices) >= 2 and indices[-1] == frames - 1:
        previous, final = scene['keyframes'][-2:]
        planted = {
            c['joint_index'] for c in scene['contacts']
            if c['object_id'] == final['object_id']
            and c['start_frame'] <= previous['frame'] and c['end_frame'] == frames
        }
        if previous['object_id'] == final['object_id'] and {10, 11} <= planted and (
                previous['pelvis_offset_m'] == final['pelvis_offset_m']
                and previous['heading_deg'] == final['heading_deg']
                and previous['body_rotvecs'] == final['body_rotvecs']):
            # Hold the planted final stance between the two identical poses.
            hold = np.arange(indices[-2] + 1, indices[-1])
            if len(hold):
                indices = np.r_[indices[:-1], hold, indices[-1]]
                support = next(obj for obj in scene['objects'] if obj['id'] == final['object_id'])
                centers = object_centers(support, frames)
                hold_roots = centers[hold] + (roots[-1] - centers[-1])
                roots = np.concatenate((roots[:-1], hold_roots, roots[-1:]))
                angles = [*angles[:-1], *([angles[-1]] * len(hold)), angles[-1]]
                body = np.concatenate((body[:-1], np.repeat(body[-1:], len(hold), axis=0), body[-1:]))
    poses = np.zeros((len(indices), 52, 3))
    poses[:, 0] = Rotation.from_rotvec(np.deg2rad(angles)[:, None] * np.array([0., 1., 0.])).as_rotvec()
    poses[:, 1:22] = body
    positions, rotations = canonical_to_soma(
        {'smpl_poses': poses, 'native_transl': roots-origin-rest[0]}, rest, parents, pipeline.skeleton)
    positions[:, :, 1] += sole_height_offset(rest, pipeline.skeleton)
    route = np.setdiff1d(np.arange(frames), indices)
    return make_constraints(pipeline, route, indices, path, heading, origin, positions, rotations)


def encode(scene):
    import numpy as np

    payload = json.dumps(scene, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(',', ':')).encode()
    if len(payload) > MAX_SCENE_BYTES:
        raise ValueError('人体场景超过 64 KiB')
    return np.frombuffer(payload, dtype=np.uint8).copy()


def decode(value, frames):
    import numpy as np

    if value.ndim != 1 or value.dtype != np.uint8 or not 0 < len(value) <= MAX_SCENE_BYTES:
        raise ValueError('无效人体场景 NPZ 字段')
    try:
        scene = json.loads(value.tobytes())
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError('人体场景 NPZ 字段须包含 UTF-8 JSON') from error
    check(scene, frames)
    return scene


def measure_arrivals(xz, scene):
    import numpy as np

    arrivals = []
    for point in scene['waypoints']:
        if 'tolerance_m' in point:
            frame = point['frame']
            end = point.get('through_frame', frame)
            distances = np.linalg.norm(xz[frame:end+1] - point['xz_m'], axis=1)
            distance = float(distances.max())
            worst_frame = frame + int(distances.argmax())
            passed = distance <= point['tolerance_m']
            arrivals.append(dict(frame=frame, error_m=distance, through_frame=end, worst_frame=worst_frame,
                                 tolerance_m=point['tolerance_m'], passed=passed))
    return arrivals


def report(arrays, scene, rest, parents, *, frame_distances=False):
    """Measure a skeletal proxy at generated samples in the object's frame."""
    import numpy as np
    from scipy.spatial.transform import Rotation

    points = np.asarray(arrays['native_keypoints3d'], dtype=float)
    root = np.asarray(arrays['native_transl'], dtype=float) + rest[0]
    world = points - points[:, :1] + root[:, None, :]
    indices, roots, _, path, _ = world_targets(scene, len(root))
    error = np.linalg.norm(root[indices]-roots, axis=1)
    # Sample along limb segments to catch parts crossing a box between joints.
    samples = skeleton_samples(world, parents)
    objects = {}
    max_depth = 0.
    deepest = None
    for obj in scene['objects']:
        inverse = Rotation.from_euler('y', -obj['heading_deg'], degrees=True).as_matrix()
        center = object_centers(obj, len(root))
        local = np.einsum('ij,nj->ni', inverse, (samples - center[:, None]).reshape(-1, 3))
        depth = np.maximum(0., np.min(np.asarray(obj['size_m'])/2-np.abs(local), axis=1))
        if float(depth.max()) > max_depth:
            max_depth = float(depth.max())
            deepest = {'object_id':obj['id'], 'frame':int(depth.argmax())//samples.shape[1]}
        objects[obj['id']] = (inverse, center)

    contacts = []
    failed = []
    for item in scene['contacts']:
        inverse, center = objects[item['object_id']]
        start, stop, joint = item['start_frame'], item['end_frame'], item['joint_index']
        local = np.einsum('ij,nj->ni', inverse, world[start:stop, joint] - center[start:stop])
        distance = np.linalg.norm(local-item['anchor_m'], axis=1)
        tolerance = item['tolerance_m']
        hit = distance <= tolerance
        passed = bool(hit.all())
        measurement = dict(start_frame=start, end_frame=stop, object_id=item['object_id'],
                           joint_index=joint, hit_fraction=float(hit.mean()),
                           max_error_m=float(distance.max()), passed=passed)
        if frame_distances:
            measurement['distance_m'] = distance.tolist()
        contacts.append(measurement)
        if not passed:
            measurement['first_failed_frame'] = start + int(np.flatnonzero(~hit)[0])
            failed.append(f'{item["object_id"]} 关节 {joint} 接触超差')
    arrivals = measure_arrivals(root[:, [0, 2]], scene)
    failed.extend(f"第 {p['worst_frame']} 帧偏离目标点" for p in arrivals if not p['passed'])
    if float(error.max(initial=0.)) > MAX_KEYFRAME_ERROR_M:
        failed.append('物体相对骨盆关键帧超差')
    if max_depth > MAX_PENETRATION_M:
        failed.append('人体骨架采样点穿入物体')
    support_y = 0.0
    for obj in scene['objects']:
        inverse, center = objects[obj['id']]
        local_root = np.einsum('ij,j->i', inverse, root[0] - center[0])
        half = np.asarray(obj['size_m'], dtype=float) / 2.0
        top_y = float(center[0, 1] + half[1])
        if (float(obj['size_m'][1]) > 0.02 and abs(local_root[0]) <= half[0]
                and abs(local_root[2]) <= half[2] and 0.02 < top_y <= float(root[0, 1])):
            support_y = max(support_y, top_y)
    initial_foot_clearance_m = float(world[0, [7, 8, 10, 11], 1].min() - support_y)
    if len(indices) and int(indices[0]) == 0:
        rest_arr = np.asarray(rest, dtype=float)
        leg_drop = max(0.0, float(rest_arr[0, 1] - rest_arr[[7, 8, 10, 11], 1].min()))
        if float(roots[0, 1] - support_y) <= max(1.15, leg_drop + 0.15) and initial_foot_clearance_m > 0.08:
            failed.append(f'第 0 帧脚部离地悬空 {initial_foot_clearance_m:.3f}m > 0.080m')
    return {'version': VERSION, 'passed': not failed, 'failures': failed,
            'scope': 'Human52 joint and sampled bone geometry at 30 FPS; oriented boxes with optional prescribed translations.',
            'route_rms_m': float(np.sqrt(np.mean(np.sum((root[:, [0, 2]]-path)**2, axis=1)))),
            'initial_foot_clearance_m': initial_foot_clearance_m,
            'arrivals': arrivals,
            'keyframes': [{'frame': int(i), 'pelvis_error_m': float(e)} for i, e in zip(indices, error)],
            'max_skeleton_penetration_m': max_depth, 'deepest_sample':deepest, 'contacts': contacts}


def preview(arrays, scene, rest, parents):
    import numpy as np
    from scipy.spatial.transform import Rotation
    from .kimodo_adapter import fk

    frames = len(arrays['native_transl'])
    indices, roots, headings, _, _ = world_targets(scene,frames)
    ghosts = []
    if len(indices):
        local = np.zeros((len(indices),52,3))
        local[:,0] = Rotation.from_rotvec(np.deg2rad(headings)[:, None] * np.array([0., 1., 0.])).as_rotvec()
        local[:,1:22] = [key['body_rotvecs'] for key in scene['keyframes']]
        pose, _ = fk(Rotation.from_rotvec(local.reshape(-1,3)).as_matrix().reshape(-1,52,3,3),rest,parents)
        ghosts = pose - pose[:,:1] + roots[:,None]
    quality = report(arrays,scene,rest,parents)
    events = [{'frame':c['first_failed_frame'],'reason':f'{c["object_id"]} 关节 {c["joint_index"]} 接触超差'}
              for c in quality['contacts'] if not c['passed']]
    events.extend({'frame':p['worst_frame'],'reason':'偏离目标点'} for p in quality['arrivals'] if not p['passed'])
    events.extend({'frame':k['frame'],'reason':'骨盆关键帧超差'} for k in quality['keyframes']
                  if k['pelvis_error_m'] > MAX_KEYFRAME_ERROR_M)
    if quality['max_skeleton_penetration_m'] > MAX_PENETRATION_M:
        sample = quality['deepest_sample']
        events.append({'frame':sample['frame'],'reason':sample['object_id']+' 骨架穿入'})
    if quality.get('initial_foot_clearance_m', 0.0) > 0.08 and any('悬空' in f for f in quality['failures']):
        events.append({'frame': 0, 'reason': '起始脚底悬空超差'})
    return {'objects':scene['objects'],
            'targets':[{'frame':int(i),'joints':p.tolist()} for i,p in zip(indices,ghosts)],
            'events':sorted(events,key=lambda event:event['frame']), 'quality':quality}
