"""Generalized 1-to-4 actor scene planner and multi-actor quality report."""
import copy
import re
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from . import human_interaction
from .human_rig import load
from .kimodo_adapter import fk

VERSION = 'multi-actor-v1'
MIN_PELVIS_CLEARANCE_M = 0.32
HANDSHAKE_GAP_MAX_M = 0.075
CARRY_AGREEMENT_MAX_M = 0.08


def _arms_down_rotvecs():
    rotvecs = np.zeros((21, 3))
    rotvecs[15, 2] = -1.4
    rotvecs[16, 2] = 1.4
    return [[round(float(v), 6) if v else 0 for v in row] for row in rotvecs]


def _wrap_deg(angle):
    return ((float(angle) + 180.0) % 360.0) - 180.0


def _squat_angles(target_y, rest, parents):
    """Return (pelvis_y, body_rotvecs_21) keeping soles at Y=0.005 while lowering shoulders to reach target_y."""
    angles = np.zeros((52, 3))
    angles[16, 2], angles[17, 2] = -1.4, 1.4
    if target_y >= 1.08:
        return 0.915, angles
    need = float(np.clip((1.08 - target_y) / 0.70, 0.0, 1.0))
    hip_bend = 0.25 + 1.15 * need
    spine_lean = 0.12 + 0.45 * need
    angles[1, 0] = angles[2, 0] = -hip_bend
    angles[4, 0] = angles[5, 0] = 2.0 * hip_bend
    angles[7, 0] = angles[8, 0] = -hip_bend
    angles[3, 0] = 0.6 * spine_lean
    angles[6, 0] = 0.4 * spine_lean
    pose, _ = fk(Rotation.from_rotvec(angles).as_matrix()[None], rest, parents)
    sole_y = float(pose[0, [10, 11], 1].min())
    pelvis_y = float(rest[0, 1] - sole_y + 0.005)
    return pelvis_y, angles


def _ensure_english_prompt(text, fallback):
    if not isinstance(text, str) or not text.strip():
        return fallback
    cleaned = ' '.join(text.strip().split())
    if not cleaned.startswith('A person '):
        cleaned = 'A person ' + cleaned[0].lower() + cleaned[1:]
    words = cleaned.split()
    if len(words) < 8:
        cleaned = cleaned.rstrip('.') + ' with balanced posture and clear limb spacing.'
    elif len(words) > 120:
        cleaned = ' '.join(words[:120])
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000].rsplit(' ', 1)[0]
    return cleaned


def _resolve_actor_index(ref, actors, id_map):
    if type(ref) is int and 0 <= ref < len(actors):
        return ref
    if isinstance(ref, str) and ref in id_map:
        return id_map[ref]
    raise ValueError(f'无效交互角色索引：{ref}')


def _actor_start(actor, index):
    xz = actor.get('start_xz_m')
    if xz is None and ('x' in actor or 'z' in actor):
        xz = [actor.get('x', 0.0), actor.get('z', 0.0)]
    if xz is None:
        xz = [index * 1.2, 0.0]
    arr = np.asarray(xz, dtype=float)
    if arr.shape != (2,) or not np.isfinite(arr).all() or np.max(np.abs(arr)) > 100.0:
        raise ValueError('无效角色起始坐标')
    heading = float(actor.get('heading_deg', 0.0))
    if not np.isfinite(heading) or abs(heading) > 360.0:
        raise ValueError('无效角色初始朝向')
    return arr, heading


def _safe_prop_id(raw, default):
    if isinstance(raw, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', raw):
        return raw
    return default


def _merge_objects(primary, extras, limit=16):
    seen = {obj['id'] for obj in primary}
    merged = [copy.deepcopy(obj) for obj in primary]
    for obj in extras:
        if obj['id'] not in seen and len(merged) < limit:
            merged.append(copy.deepcopy(obj))
            seen.add(obj['id'])
    return merged


def _ensure_frame0_ground_pose(scene, start_xz, heading, frames):
    scene = copy.deepcopy(scene)
    if not scene.get('objects'):
        scene['objects'] = [{
            'id': 'floor-anchor',
            'center_m': [round(float(start_xz[0]), 6), 0.005, round(float(start_xz[1]), 6)],
            'size_m': [0.32, 0.01, 0.32],
            'heading_deg': 0.0,
        }]
    if not scene.get('waypoints'):
        scene['waypoints'] = [{'frame': 0, 'xz_m': [round(float(start_xz[0]), 6), round(float(start_xz[1]), 6)],
                               'heading_deg': round(float(heading), 4)}]
    elif scene['waypoints'][0]['frame'] != 0:
        scene['waypoints'].insert(0, {'frame': 0, 'xz_m': [round(float(start_xz[0]), 6), round(float(start_xz[1]), 6)],
                                      'heading_deg': round(float(heading), 4)})
    wp0 = scene['waypoints'][0]
    kf_frames = {k['frame'] for k in scene.get('keyframes', [])}
    if 0 not in kf_frames:
        obj = scene['objects'][0]
        c0 = human_interaction.object_centers(obj, frames)[0]
        inv = Rotation.from_euler('y', obj['heading_deg'], degrees=True).inv()
        world_pelvis = np.array([float(wp0['xz_m'][0]), 0.915, float(wp0['xz_m'][1])])
        offset = inv.apply(world_pelvis - c0)
        key0 = {
            'frame': 0,
            'object_id': obj['id'],
            'pelvis_offset_m': [round(float(v), 6) for v in offset],
            'heading_deg': round(_wrap_deg(wp0['heading_deg'] - obj['heading_deg']), 4),
            'body_rotvecs': _arms_down_rotvecs(),
        }
        scene['keyframes'] = sorted([key0, *scene.get('keyframes', [])], key=lambda k: k['frame'])
    return scene


def _solve_interaction_pair(kind, frames, start_a, heading_a0, start_b, heading_b0,
                            interaction, payload_objects, rest, parents, index):
    carrying = kind == 'co_carry'
    default_start = max(1, round(0.24 * (frames - 1)))
    default_end = min(frames - 1, round(0.78 * (frames - 1)) + 1)
    s_frame = interaction.get('start_frame')
    e_frame = interaction.get('end_frame')
    s_frame = default_start if s_frame is None else int(s_frame)
    e_frame = default_end if e_frame is None else int(e_frame)
    if not 0 <= s_frame < e_frame <= frames:
        raise ValueError('无效交互时间窗')

    t_lo = max(1, s_frame)
    t_hi = min(frames - 2, e_frame - 1)
    if t_hi < t_lo:
        t_lo = max(1, min(frames - 2, s_frame))
        t_hi = t_lo
    times = sorted({round(t) for t in np.linspace(t_lo, t_hi, 15 if carrying else 7)})
    if t_lo not in times:
        times.insert(0, t_lo)
    if t_hi not in times:
        times.append(t_hi)
    times = sorted(set(times))

    prop_id = _safe_prop_id(
        interaction.get('object_id'),
        ('shared-box' if index == 0 else f'shared-box-{index}') if carrying
        else ('hand-contact' if index == 0 else f'hand-contact-{index}'),
    )
    existing = next((o for o in payload_objects if o.get('id') == prop_id), None)

    if carrying:
        if interaction.get('size_m') is not None:
            size = np.asarray(interaction['size_m'], dtype=float)
        elif existing is not None:
            size = np.asarray(existing['size_m'], dtype=float)
        else:
            size = np.array([0.58, 0.38, 0.42])
        if size.shape != (3,) or not np.isfinite(size).all() or min(size) < 0.01:
            raise ValueError('无效合抬箱体尺寸')
    else:
        size = np.array([0.01, 0.01, 0.01])

    if interaction.get('xz_m') is not None:
        mid_xz = np.asarray(interaction['xz_m'], dtype=float)
    elif 'x' in interaction or 'z' in interaction:
        mid_xz = np.array([float(interaction.get('x', 0.0)), float(interaction.get('z', 0.0))])
    elif existing is not None:
        mid_xz = np.asarray(existing['center_m'], dtype=float)[[0, 2]]
    else:
        mid_xz = 0.5 * (start_a + start_b)
    if mid_xz.shape != (2,) or not np.isfinite(mid_xz).all():
        raise ValueError('无效交互中心坐标')

    delta_ab = start_b - start_a
    dist_ab = float(np.linalg.norm(delta_ab))
    dir_ab = delta_ab / dist_ab if dist_ab > 1e-4 else np.array([0.0, 1.0])
    if carrying and existing is not None and 'heading_deg' in existing:
        prop_heading = float(existing['heading_deg'])
    else:
        prop_heading = float(np.rad2deg(np.arctan2(dir_ab[0], dir_ab[1])))
    prop_rot = Rotation.from_euler('y', prop_heading, degrees=True)
    prop_fwd = prop_rot.apply([0.0, 0.0, 1.0])[[0, 2]]
    if float(np.dot(start_a - mid_xz, prop_fwd)) <= float(np.dot(start_b - mid_xz, prop_fwd)):
        sides = (-1, 1)
    else:
        sides = (1, -1)

    if carrying:
        if existing is not None:
            center_y = float(existing['center_m'][1])
        else:
            center_y = float(interaction.get(
                'center_y_m',
                1.02 if size[1] <= 0.45 else max(size[1] / 2.0, min(1.02, 1.25 - size[1] / 2.0)),
            ))
        lift_m = float(interaction.get('lift_m') if interaction.get('lift_m') is not None else 0.08)
        if not np.isfinite(lift_m) or not -0.5 <= lift_m <= 1.2:
            raise ValueError('无效箱体抬升高度')
        if interaction.get('to_xz_m') is not None:
            dest_xz = np.asarray(interaction['to_xz_m'], dtype=float)
            if dest_xz.shape != (2,) or not np.isfinite(dest_xz).all():
                raise ValueError('无效目标搬运坐标')
        else:
            dest_xz = mid_xz + np.array([1.1, 0.0])
        delta_xz = dest_xz - mid_xz
        center = np.array([float(mid_xz[0]), center_y, float(mid_xz[1])])
        low_start = center_y < 0.70

        def displacement(frame):
            u = float(np.clip((frame - t_lo) / max(1, t_hi - t_lo), 0.0, 1.0))
            if low_start:
                u_y = float(np.clip(u / 0.30, 0.0, 1.0))
                dy = lift_m * (3.0 * u_y * u_y - 2.0 * u_y * u_y * u_y)
                u_xz = float(np.clip((u - 0.25) / 0.75, 0.0, 1.0))
                s_xz = u_xz * u_xz * (3.0 - 2.0 * u_xz)
                return np.array([delta_xz[0] * s_xz, dy, delta_xz[1] * s_xz])
            s = u * u * (3.0 - 2.0 * u)
            return np.array([delta_xz[0] * s, lift_m * np.sin(np.pi * u), delta_xz[1] * s])
    else:
        center = np.array([float(mid_xz[0]), 1.16, float(mid_xz[1])])

        def displacement(frame):
            u = float(np.clip((frame - t_lo) / max(1, t_hi - t_lo), 0.0, 1.0))
            return np.array([0.0, 0.045 * np.sin(4.0 * np.pi * u), 0.0])

    track_frames = [0, *times, frames - 1]
    prop = {
        'id': prop_id,
        'center_m': [round(float(v), 6) for v in center],
        'size_m': [round(float(v), 6) for v in size],
        'heading_deg': round(prop_heading, 4),
        'trajectory': [
            {'frame': int(t), 'center_m': [round(float(v), 6) for v in (center + displacement(t))]}
            for t in track_frames
        ],
    }
    prop['trajectory'][0] = {'frame': 0, 'center_m': list(prop['center_m'])}

    arms_down = _arms_down_rotvecs()
    plans = []
    standoff = float(size[2] / 2.0 + 0.37) if carrying else 0.35
    ax = min(0.23, max(0.01, float(size[0] / 2.0) - 0.02))
    for side, start_xz_i, heading_0 in zip(sides, (start_a, start_b), (heading_a0, heading_b0)):
        actor_heading = _wrap_deg(prop_heading + (0.0 if side == -1 else 180.0))
        yaw = Rotation.from_euler('y', actor_heading, degrees=True)
        local_stance = np.array([0.0, 0.0, side * standoff])
        world_stance_offset = prop_rot.apply(local_stance)
        arms = [(20, 16, 18), (21, 17, 19)] if carrying else [(21, 17, 19)]
        anchors = {
            joint: ([(-side if joint == 20 else side) * ax, float(size[1] / 2.0), float(side * size[2] / 2.0)]
                    if carrying else [0.0, 0.0, float(side * 0.005)])
            for joint, _, _ in arms
        }

        def stance_xz(t):
            delta = displacement(t)
            return np.array([center[0] + world_stance_offset[0] + delta[0],
                             center[2] + world_stance_offset[2] + delta[2]])

        p0 = np.array([float(start_xz_i[0]), 0.915, float(start_xz_i[1])])
        c0 = center + displacement(0)
        keys = [{
            'frame': 0,
            'object_id': prop_id,
            'pelvis_offset_m': [round(float(v), 6) for v in prop_rot.inv().apply(p0 - c0)],
            'heading_deg': round(_wrap_deg(heading_0 - prop_heading), 4),
            'body_rotvecs': arms_down,
        }]
        path = [{
            'frame': 0,
            'xz_m': [round(float(start_xz_i[0]), 6), round(float(start_xz_i[1]), 6)],
            'heading_deg': round(_wrap_deg(heading_0), 4),
        }]

        for t in times:
            ct = center + displacement(t)
            target_rim_y = float(ct[1] + (size[1] / 2.0 if carrying else 0.0))
            pelvis_y, base_angles = _squat_angles(target_rim_y, rest, parents)
            xz_t = stance_xz(t)
            pelvis = np.array([xz_t[0], pelvis_y, xz_t[1]])
            angles = base_angles.copy()
            angles[0] = yaw.as_rotvec()
            for wrist, shoulder, elbow in arms:
                target = ct + prop_rot.apply(anchors[wrist])
                initial = np.r_[angles[shoulder], [0.0, 0.3 if wrist == 20 else -0.3, 0.0]]

                def residual(v):
                    angles[[shoulder, elbow]] = v.reshape(2, 3)
                    positions, _ = fk(Rotation.from_rotvec(angles).as_matrix()[None], rest, parents)
                    return np.r_[positions[0, wrist] + pelvis - rest[0] - target, 0.003 * (v - initial)]

                solution = least_squares(residual, initial, max_nfev=80)
                error = float(np.linalg.norm(residual(solution.x)[:3]))
                if carrying:
                    _, global_rot = fk(Rotation.from_rotvec(angles).as_matrix()[None], rest, parents)
                    neutral_axis = np.array([1.0 if wrist == 20 else -1.0, 0.0, 0.0])
                    local_axis = global_rot[0, elbow].T @ prop_rot.apply([0.0, 0.08, -side])
                    local_axis /= np.linalg.norm(local_axis)
                    axis = np.cross(neutral_axis, local_axis)
                    axis_norm = float(np.linalg.norm(axis))
                    if axis_norm > 1e-8:
                        angle = float(np.arctan2(axis_norm, float(np.dot(neutral_axis, local_axis))))
                        angles[wrist] = axis / axis_norm * angle
                if error > 0.018:
                    raise ValueError('共享接触点超出手臂可达范围')
            keys.append({
                'frame': int(t),
                'object_id': prop_id,
                'pelvis_offset_m': [round(float(v), 6) for v in prop_rot.inv().apply(pelvis - ct)],
                'heading_deg': round(_wrap_deg(actor_heading - prop_heading), 4),
                'body_rotvecs': [[round(float(v), 6) if v else 0 for v in row] for row in angles[1:22]],
            })
            path.append({
                'frame': int(t),
                'xz_m': [round(float(xz_t[0]), 6), round(float(xz_t[1]), 6)],
                'heading_deg': round(_wrap_deg(actor_heading), 4),
            })

        end_xz = stance_xz(frames - 1)
        p_end = np.array([end_xz[0], 0.915, end_xz[1]])
        c_end = center + displacement(frames - 1)
        keys.append({
            'frame': frames - 1,
            'object_id': prop_id,
            'pelvis_offset_m': [round(float(v), 6) for v in prop_rot.inv().apply(p_end - c_end)],
            'heading_deg': round(_wrap_deg(actor_heading - prop_heading), 4),
            'body_rotvecs': arms_down,
        })
        path.append({
            'frame': frames - 1,
            'xz_m': [round(float(end_xz[0]), 6), round(float(end_xz[1]), 6)],
            'heading_deg': round(_wrap_deg(actor_heading), 4),
        })

        contacts = [
            {
                'start_frame': s_frame,
                'end_frame': e_frame,
                'object_id': prop_id,
                'joint_index': wrist,
                'anchor_m': [round(float(v), 6) for v in anchors[wrist]],
                'tolerance_m': 0.05,
            }
            for wrist, _, _ in arms
        ]
        scene_objects = _merge_objects([prop], [o for o in payload_objects if o.get('id') != prop_id])
        plan_scene = {
            'kind': human_interaction.VERSION,
            'objects': scene_objects,
            'waypoints': path,
            'keyframes': keys,
            'contacts': contacts,
        }
        if carrying:
            plan_scene['attachments'] = [{
                'object_id': prop_id,
                'start_frame': s_frame,
                'end_frame': e_frame,
                'mode': 'co_carry',
                'joints': [20, 21],
                'anchors_m': [[round(float(v), 6) for v in anchors[20]],
                              [round(float(v), 6) for v in anchors[21]]],
            }]
            if len(scene_objects) == 1:
                plan_scene['carry_grip'] = True
        plans.append(plan_scene)
    return plans


def build_multi_plans(payload):
    """Build synchronized human_interaction_v1 plans for 1..4 actors."""
    if not isinstance(payload, dict):
        raise ValueError('多角色规划参数必须为对象')
    duration = payload.get('duration')
    if type(duration) is not int or not 1 <= duration <= 120:
        raise ValueError('多角色场景时长须为 1–120 秒')
    frames = duration * 30
    actors = payload.get('actors')
    if not isinstance(actors, list) or not 1 <= len(actors) <= 4:
        raise ValueError('多角色场景需要 1–4 个角色')
    payload_objects = payload.get('objects', [])
    if not isinstance(payload_objects, list) or len(payload_objects) > 16:
        raise ValueError('场景物体数量须为 0–16 个')
    interactions = payload.get('interactions', [])
    if not isinstance(interactions, list):
        raise ValueError('无效交互配置列表')

    rest, parents = load()
    id_map = {a['id']: i for i, a in enumerate(actors) if isinstance(a, dict) and isinstance(a.get('id'), str)}
    starts = []
    for i, actor in enumerate(actors):
        if not isinstance(actor, dict):
            raise ValueError('角色配置必须为对象')
        starts.append(_actor_start(actor, i))

    assigned_scenes = {}
    default_texts = {}
    for k, inter in enumerate(interactions):
        if not isinstance(inter, dict) or inter.get('kind') not in ('handshake', 'co_carry'):
            raise ValueError('交互类型仅支持 handshake 或 co_carry')
        pair_refs = inter.get('actors')
        if pair_refs is None:
            if len(actors) < 2:
                raise ValueError('双人交互需要至少 2 个角色')
            pair_refs = [0, 1]
        if not isinstance(pair_refs, (list, tuple)) or len(pair_refs) != 2:
            raise ValueError('每次交互需要恰好 2 个角色')
        ia = _resolve_actor_index(pair_refs[0], actors, id_map)
        ib = _resolve_actor_index(pair_refs[1], actors, id_map)
        if ia == ib:
            raise ValueError('交互角色不能相同')
        if ia in assigned_scenes or ib in assigned_scenes:
            raise ValueError('同一角色不能同时参与多个互斥交互')
        pair_scenes = _solve_interaction_pair(
            inter['kind'], frames,
            starts[ia][0], starts[ia][1],
            starts[ib][0], starts[ib][1],
            inter, payload_objects, rest, parents, k,
        )
        assigned_scenes[ia] = pair_scenes[0]
        assigned_scenes[ib] = pair_scenes[1]
        text = ('A person faces a partner and extends the right arm to shake hands gently with the partner.'
                if inter['kind'] == 'handshake'
                else 'A person grasps a shared box with both hands and carries it smoothly together with a partner.')
        default_texts[ia] = text
        default_texts[ib] = text

    results = []
    for i, actor in enumerate(actors):
        start_xz, heading = starts[i]
        if i in assigned_scenes:
            scene = assigned_scenes[i]
            fallback = default_texts[i]
        elif actor.get('scene') is not None:
            scene = copy.deepcopy(actor['scene'])
            if payload_objects and not scene.get('carry_grip'):
                scene['objects'] = _merge_objects(scene.get('objects', []), payload_objects)
            scene = _ensure_frame0_ground_pose(scene, start_xz, heading, frames)
            fallback = 'A person walks smoothly along the planned route with balanced posture and natural arm swing.'
        else:
            scene = _ensure_frame0_ground_pose({
                'kind': human_interaction.VERSION,
                'objects': copy.deepcopy(payload_objects) if payload_objects else [],
                'waypoints': [{
                    'frame': 0,
                    'xz_m': [round(float(start_xz[0]), 6), round(float(start_xz[1]), 6)],
                    'heading_deg': round(float(heading), 4),
                }],
                'keyframes': [],
                'contacts': [],
            }, start_xz, heading, frames)
            fallback = 'A person stands upright with balanced posture and relaxed arms beside the body.'

        human_interaction.check(scene, frames)
        text = _ensure_english_prompt(actor.get('text'), fallback)
        results.append({'text': text, 'duration': duration, 'scene': scene})
    return results


def multi_actor_report(plans, joints_list):
    """Measure per-actor contacts, shared handshake/carry distances, and pelvis clearance."""
    if not isinstance(plans, (list, tuple)) or not 1 <= len(plans) <= 4 or len(plans) != len(joints_list):
        raise ValueError('多角色检查需要 1–4 个匹配的规划与关节轨迹')

    scenes = [p['scene'] if isinstance(p, dict) and 'scene' in p else p for p in plans]
    tracks = [np.asarray(j, dtype=float) for j in joints_list]
    expected_shape = tracks[0].shape
    if tracks[0].ndim != 3 or expected_shape[1:] != (52, 3) or expected_shape[0] < 1:
        raise ValueError('角色骨架轨迹形状无效')
    for track in tracks:
        if track.shape != expected_shape or not np.isfinite(track).all():
            raise ValueError('所有角色必须使用相同帧数且包含有限三维坐标')

    frames = expected_shape[0]
    contacts = []
    failures = []
    actor_contact_index = {}

    for idx, (sc, track) in enumerate(zip(scenes, tracks)):
        obj_map = {obj['id']: obj for obj in sc['objects']}
        for c in sc.get('contacts', []):
            obj = obj_map[c['object_id']]
            centers = human_interaction.object_centers(obj, frames)
            anchor_world = Rotation.from_euler('y', obj['heading_deg'], degrees=True).apply(c['anchor_m'])
            start, end = int(c['start_frame']), int(c['end_frame'])
            distal = track[start:end, c['joint_index']]
            distances = np.linalg.norm(distal - centers[start:end] - anchor_world, axis=1)
            max_err = float(distances.max())
            passed = bool(np.all(distances <= c['tolerance_m']))
            entry = {
                'actor': idx + 1,
                'object_id': c['object_id'],
                'joint_index': int(c['joint_index']),
                'start_frame': start,
                'end_frame': end,
                'maximum_m': max_err,
                'tolerance_m': float(c['tolerance_m']),
                'passed': passed,
            }
            contacts.append(entry)
            actor_contact_index.setdefault((c['object_id'], idx), []).append((c, obj, centers))
            if not passed:
                failures.append(f'角色 {idx + 1} 物体 {c["object_id"]} 关节 {c["joint_index"]} 接触超差 {max_err:.3f}m')

    shared_interactions = []
    handshake_gap_max = None
    carry_gap_max = None
    by_obj = {}
    for (obj_id, actor_idx), items in actor_contact_index.items():
        by_obj.setdefault(obj_id, []).append((actor_idx, items))

    for obj_id, entries in by_obj.items():
        if len(entries) < 2:
            continue
        for p in range(len(entries)):
            for q in range(p + 1, len(entries)):
                ia, items_a = entries[p]
                ib, items_b = entries[q]
                s = max(min(c['start_frame'] for c, _, _ in items_a),
                        min(c['start_frame'] for c, _, _ in items_b))
                e = min(max(c['end_frame'] for c, _, _ in items_a),
                        max(c['end_frame'] for c, _, _ in items_b))
                if e <= s:
                    continue
                obj_a = items_a[0][1]
                is_handshake = max(obj_a['size_m']) <= 0.05 or obj_id.startswith('hand-contact')
                if is_handshake:
                    ja = items_a[0][0]['joint_index']
                    jb = items_b[0][0]['joint_index']
                    gap = float(np.linalg.norm(tracks[ia][s:e, ja] - tracks[ib][s:e, jb], axis=1).max())
                    passed = gap <= HANDSHAKE_GAP_MAX_M
                    handshake_gap_max = gap if handshake_gap_max is None else max(handshake_gap_max, gap)
                    shared_interactions.append({
                        'kind': 'handshake',
                        'object_id': obj_id,
                        'actors': [ia + 1, ib + 1],
                        'start_frame': s,
                        'end_frame': e,
                        'max_gap_m': gap,
                        'limit_m': HANDSHAKE_GAP_MAX_M,
                        'passed': passed,
                    })
                    if not passed:
                        failures.append(f'角色 {ia + 1}/{ib + 1} 握手手腕间距 {gap:.3f}m > {HANDSHAKE_GAP_MAX_M:.3f}m')
                else:
                    def implied_center(idx_actor, items_actor):
                        rot = Rotation.from_euler('y', items_actor[0][1]['heading_deg'], degrees=True)
                        est = np.zeros((e - s, 3))
                        for c_item, _, _ in items_actor:
                            est += tracks[idx_actor][s:e, c_item['joint_index']] - rot.apply(c_item['anchor_m'])
                        return est / len(items_actor)

                    ca = implied_center(ia, items_a)
                    cb = implied_center(ib, items_b)
                    gap = float(np.linalg.norm(ca - cb, axis=1).max())
                    passed = gap <= CARRY_AGREEMENT_MAX_M
                    carry_gap_max = gap if carry_gap_max is None else max(carry_gap_max, gap)
                    shared_interactions.append({
                        'kind': 'co_carry',
                        'object_id': obj_id,
                        'actors': [ia + 1, ib + 1],
                        'start_frame': s,
                        'end_frame': e,
                        'max_gap_m': gap,
                        'limit_m': CARRY_AGREEMENT_MAX_M,
                        'passed': passed,
                    })
                    if not passed:
                        failures.append(f'角色 {ia + 1}/{ib + 1} 合抬箱体中心偏差 {gap:.3f}m > {CARRY_AGREEMENT_MAX_M:.3f}m')

    if len(tracks) >= 2:
        min_clearance = min(
            float(np.linalg.norm(tracks[i][:, 0] - tracks[j][:, 0], axis=1).min())
            for i in range(len(tracks)) for j in range(i + 1, len(tracks))
        )
        clearance_passed = min_clearance >= MIN_PELVIS_CLEARANCE_M
        if not clearance_passed:
            failures.append(f'角色间骨盆最小净距 {min_clearance:.3f}m < {MIN_PELVIS_CLEARANCE_M:.3f}m')
    else:
        min_clearance = None
        clearance_passed = True

    passed = (
        all(c['passed'] for c in contacts)
        and all(s['passed'] for s in shared_interactions)
        and clearance_passed
    )
    report = {
        'version': VERSION,
        'actor_count': len(tracks),
        'passed': bool(passed),
        'contacts': contacts,
        'shared_interactions': shared_interactions,
        'min_pelvis_clearance_m': min_clearance,
        'min_pelvis_clearance_limit_m': MIN_PELVIS_CLEARANCE_M,
        'clearance_passed': bool(clearance_passed),
        'failures': failures,
    }
    if handshake_gap_max is not None:
        report['wrist_gap_max_m'] = handshake_gap_max
    if carry_gap_max is not None:
        report['carry_gap_max_m'] = carry_gap_max
    return report
