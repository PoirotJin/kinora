"""Bounded single-person BVH decoding and fixed-rig motion transfer."""
import hashlib
import json
import re

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .human_rig import BODY_NAMES, definition
from .kimodo_adapter import fk


CHANNELS = {axis + kind for axis in 'XYZ' for kind in ('position', 'rotation')}


def read(raw):
    try:
        header, motion = re.split(r'(?im)^\s*MOTION\s*$', raw.decode('utf-8-sig'), maxsplit=1)
    except (UnicodeError, ValueError) as exc:
        raise ValueError('无法识别 BVH 文件') from exc
    tokens = iter(re.findall(r'[^\s{}]+|[{}]', header))
    joints = []
    columns = 0

    def take(expected=None):
        token = next(tokens, None)
        if token is None or (expected is not None and token != expected):
            raise ValueError('BVH 骨架层级格式无效')
        return token

    def offset():
        take('OFFSET')
        value = [float(take()) for _ in range(3)]
        if not np.isfinite(value).all() or max(map(abs, value)) > 100000:
            raise ValueError('BVH 骨骼偏移无效')
        return value

    def joint(parent):
        nonlocal columns
        if len(joints) >= 128:
            raise ValueError('BVH 超过 128 个关节')
        name = take()
        if name in {'{', '}'} or len(name) > 100 or any(row[0] == name for row in joints):
            raise ValueError('BVH 关节名称无效或重复')
        take('{')
        position = offset()
        take('CHANNELS')
        count = int(take())
        if not 0 <= count <= 6:
            raise ValueError('BVH 关节通道数无效')
        channels = [take() for _ in range(count)]
        if len(set(channels)) != count or not set(channels) <= CHANNELS:
            raise ValueError('BVH 关节通道无效')
        rotation = [x for x in channels if x.endswith('rotation')]
        if len(rotation) not in (0, 3):
            raise ValueError('BVH 只支持三轴关节旋转通道')
        index = len(joints)
        joints.append((name, parent, position, channels, columns))
        columns += count
        while True:
            token = take()
            if token == '}':
                break
            if token == 'JOINT':
                joint(index)
            elif token == 'End':
                take('Site')
                take('{')
                offset()
                take('}')
            else:
                raise ValueError('BVH 骨骼包含未识别的字段')

    take('HIERARCHY')
    take('ROOT')
    joint(-1)
    if next(tokens, None) is not None or not columns:
        raise ValueError('只支持单根、单人 BVH 骨架')

    head = re.match(r'\s*Frames:\s*(\d{1,5})\s+Frame Time:\s*([^\s]+)[ \t]*\r?\n', motion)
    if head is None:
        raise ValueError('BVH 缺少帧数或采样间隔')
    frames, dt = int(head[1]), float(head[2])
    if not 2 <= frames <= 28801 or not 1/241 <= dt <= 1 or (frames - 1)*dt > 120:
        raise ValueError('支持最长 120 秒、最高约 240 FPS 的 BVH')
    rows = motion[head.end():].splitlines()
    rows = [row.split() for row in rows if row.strip()]
    if len(rows) != frames or any(len(row) != columns for row in rows):
        raise ValueError('BVH 动作通道数与骨架不一致')
    values = np.asarray(rows, dtype=np.float64)
    if not np.isfinite(values).all() or np.abs(values).max() > 1e7:
        raise ValueError('BVH 动作数据包含异常数值')
    signature = hashlib.sha256(json.dumps([row[:4] for row in joints], separators=(',', ':')).encode()).hexdigest()
    return joints, values, dt, signature


def mapping(joints, configured=None):
    names = [row[0] for row in joints]
    if configured is not None:
        if not isinstance(configured, dict) or not set(configured) <= set(BODY_NAMES):
            raise ValueError('关节映射格式无效')
        mapped = {k: v for k, v in configured.items() if v}
    else:
        by_name = {re.sub(r'[^a-z0-9]', '', re.sub(r'^J\d+_', '', name).lower()): name for name in names}
        aliases = {'pelvis': 'hips', 'spine1': 'spine', 'left_hip': 'leftupleg', 'right_hip': 'rightupleg',
                   'left_knee': 'leftleg', 'right_knee': 'rightleg', 'left_ankle': 'leftfoot',
                   'right_ankle': 'rightfoot', 'left_foot': 'lefttoebase', 'right_foot': 'righttoebase',
                   'left_collar': 'leftshoulder', 'right_collar': 'rightshoulder',
                   'left_shoulder': 'leftarm', 'right_shoulder': 'rightarm', 'left_elbow': 'leftforearm',
                   'right_elbow': 'rightforearm', 'left_wrist': 'lefthand', 'right_wrist': 'righthand'}
        mapped = {}
        for name in BODY_NAMES:
            normal = name.replace('_', '')
            candidate = (by_name.get(aliases.get(name)) or by_name.get(normal)) if 'hips' in by_name else by_name.get(normal)
            if candidate and candidate not in mapped.values():
                mapped[name] = candidate
    missing = [name for name in BODY_NAMES if name not in mapped and name not in
               {'spine1', 'spine2', 'spine3', 'left_collar', 'right_collar', 'left_foot', 'right_foot', 'neck'}]
    if any(not isinstance(v, str) or v not in names for v in mapped.values()) or len(set(mapped.values())) != len(mapped):
        raise ValueError('关节映射包含未知或重复的源关节')
    return mapped, missing


def axes(up, forward):
    vectors = {sign + axis: eye * scale for sign, scale in (('+', 1), ('-', -1))
               for axis, eye in zip('XYZ', np.eye(3))}
    if up not in vectors or forward not in vectors or up[-1] == forward[-1]:
        raise ValueError('上轴和前向必须选用不同坐标轴')
    y, z = vectors[up], vectors[forward]
    return np.stack((np.cross(y, z), y, z))


def validate_animation_translations(joints, values, pelvis):
    """Allow 6-DOF child translation channels; world() evaluates full joint offsets."""
    return None


def world(joints, values, basis, units, pelvis):
    count = len(values)
    rotations = np.empty((count, len(joints), 3, 3))
    points = np.empty((count, len(joints), 3))
    for i, (_, parent, offset, channels, col) in enumerate(joints):
        xyz = np.broadcast_to(offset, (count, 3)).copy()
        order, angles = [], []
        for j, channel in enumerate(channels):
            if channel.endswith('position'):
                data = values[:, col+j]
                xyz[:, 'XYZ'.index(channel[0])] += data
            else:
                order.append(channel[0])
                angles.append(values[:, col+j])
        local = Rotation.from_euler(''.join(order), np.stack(angles, axis=1), degrees=True).as_matrix() if angles else np.eye(3)
        if parent == -1:
            rotations[:, i] = local
            points[:, i] = xyz
        else:
            rotations[:, i] = rotations[:, parent] @ local
            points[:, i] = points[:, parent] + np.einsum('fij,fj->fi', rotations[:, parent], xyz)
    return points @ basis.T * units, basis @ rotations @ basis.T


def swing(source, target):
    a, b = source / np.linalg.norm(source), target / np.linalg.norm(target)
    cross = np.cross(a, b)
    length = np.linalg.norm(cross)
    dot = np.clip(np.dot(a, b), -1, 1)
    if length > 1e-8:
        return Rotation.from_rotvec(cross / length * np.arctan2(length, dot)).as_matrix()
    if dot > 0:
        return np.eye(3)
    normal = np.cross(a, np.eye(3)[np.argmin(np.abs(a))])
    return Rotation.from_rotvec(np.pi * normal / np.linalg.norm(normal)).as_matrix()


def convert(parsed, profile):
    joints, channels, dt, signature = parsed
    if profile['signature'] != signature:
        raise ValueError('BVH 骨架与预设不匹配，请为这个骨架创建预设')
    joint_map, missing = mapping(joints, profile['mapping'])
    if missing:
        raise ValueError('缺少必要关节：' + ', '.join(missing))
    names = [row[0] for row in joints]
    ids = {BODY_NAMES.index(key): names.index(value) for key, value in joint_map.items()}
    basis = axes(profile['up'], profile['forward'])
    points, rotations = world(joints, channels, basis, profile['units'], ids[0])
    try:
        reference = np.asarray(profile['calibration_channels'], dtype=float).reshape(1, -1)
    except (TypeError, ValueError) as exc:
        raise ValueError('BVH 校准通道格式无效') from exc
    if reference.shape[1] != channels.shape[1] or not np.isfinite(reference).all():
        raise ValueError('BVH 校准通道数与骨架不一致')
    ref_points, ref_rots = world(joints, reference, basis, profile['units'], ids[0])
    rest, parents, target_names = definition()
    normalized = {re.sub(r'^J\d+_', '', name).lower(): i for i,name in enumerate(names)}
    for i,name in enumerate(target_names[22:],22):
        if name in normalized and normalized[name] not in ids.values():
            ids[i] = normalized[name]

    def ancestor(a, b):
        while b >= 0 and b != a:
            b = joints[b][1]
        return b == a

    for target, source in ids.items():
        parent = parents[target]
        while parent >= 0 and parent not in ids:
            parent = parents[parent]
        if parent >= 0 and not ancestor(ids[parent], source):
            raise ValueError('关节父子关系不正确：' + target_names[target])

    leg_length = 0
    joint = ids[7]
    while joint != ids[1]:
        leg_length += np.linalg.norm(joints[joint][2]) * profile['units']
        joint = joints[joint][1]
    if not .15 <= leg_length <= 2:
        raise ValueError('校准后的腿长异常，请检查 BVH 单位与关节映射')

    primary_child = {1:4, 2:5, 3:6, 4:7, 5:8, 6:9, 7:10, 8:11, 9:12,
                     12:15, 13:16, 14:17, 16:18, 17:19, 18:20, 19:21}
    times = np.arange(len(points)) * dt
    # Round the duration to the nearest 30 fps interval and retain both endpoint poses.
    count = min(3600, max(2, int(np.floor(times[-1] * 30 + .5)) + 1))
    samples = np.linspace(0, times[-1], count)
    global_rot = np.empty((count, 52, 3, 3))
    local = np.empty_like(global_rot)
    correction = {}
    for i in range(52):
        parent = parents[i]
        if i not in ids:
            correction[i] = correction[parent]
            global_rot[:, i] = global_rot[:, parent]
            local[:, i] = np.eye(3)
            continue
        source = ids[i]
        # Use the reference segment direction for swing, carrying roll from
        # the calibrated parent when a segment does not constrain rotation.
        if i == 0:
            lateral = ref_points[0, ids[1]] - ref_points[0, ids[2]]
            forward = np.cross(lateral, [0, 1, 0])
            if np.linalg.norm(forward) < 1e-7:
                raise ValueError('校准姿态无法确定骨盆朝向')
            orientation = Rotation.from_euler('Y', np.arctan2(forward[0], forward[2])).as_matrix()
        else:
            orientation = correction[parent]
        child = primary_child.get(i)
        if child in ids:
            direction = ref_points[0, ids[child]] - ref_points[0, source]
            if np.linalg.norm(direction) > 1e-7:
                orientation = swing(orientation @ (rest[child] - rest[i]), direction) @ orientation
        correction[i] = orientation
        transfer = rotations[:, source] @ ref_rots[0, source].T @ orientation
        global_rot[:, i] = Slerp(times, Rotation.from_matrix(transfer))(samples).as_matrix()
        local[:, i] = global_rot[:, i] if i == 0 else global_rot[:, parent].transpose(0, 2, 1) @ global_rot[:, i]
    translated = np.stack([np.interp(samples, times, points[:, ids[0], a]) for a in range(3)], axis=1)
    pose_points, _ = fk(local, rest, parents)
    return dict(fps=np.asarray(30), native_keypoints3d=pose_points.astype('f4'),
                native_transl=(translated-rest[0]).astype('f4'),
                smpl_poses=Rotation.from_matrix(local.reshape(-1, 3, 3)).as_rotvec().reshape(count, 156).astype('f4'),
                parents=parents, rest_joints=rest)
