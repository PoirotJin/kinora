"""Kinematic object response and post-hoc object-to-motion coupling for Human52."""
import copy
import numpy as np
from scipy.spatial.transform import Rotation

from . import carry_grip, human_interaction
from .motion_assets import validate_motion

VERSION = 'object-response-v1'


def _world_joints(arrays, rest):
    points = np.asarray(arrays['native_keypoints3d'], dtype=np.float64)
    transl = np.asarray(arrays['native_transl'], dtype=np.float64)
    return points - points[:, :1] + transl[:, None, :] + np.asarray(rest, dtype=np.float64)[0]


def _pelvis_forward_xz(world):
    across = world[:, 1] - world[:, 2]
    facing = np.column_stack((-across[:, 2], across[:, 0]))
    norms = np.linalg.norm(facing, axis=1, keepdims=True)
    fallback = np.tile(np.array([[0., 1.]]), (len(world), 1))
    return np.where(norms > 1e-6, facing / np.maximum(norms, 1e-12), fallback)


def _project_reachable(center, pivots, r_min, r_max):
    c = center.astype(np.float64).copy()
    for _ in range(10):
        for p in pivots:
            delta = c - p
            dist = float(np.linalg.norm(delta))
            if dist > r_max:
                c = p + delta * (r_max / dist)
            elif dist < r_min:
                direction = delta / dist if dist > 1e-8 else np.array([0., 0., 1.])
                c = p + direction * r_min
    return c


def _compress_trajectory(centers, must_keep, max_points=128):
    frames = len(centers)
    if frames <= max_points:
        indices = list(range(frames))
    else:
        kept = sorted({i for i in must_keep if 0 <= i < frames} | {0, frames - 1})
        while len(kept) < max_points:
            interp = np.column_stack([
                np.interp(np.arange(frames), kept, centers[kept, ax]) for ax in range(3)
            ])
            err = np.linalg.norm(centers - interp, axis=1)
            err[kept] = -1.0
            worst = int(np.argmax(err))
            if err[worst] <= 1e-4:
                break
            kept.append(worst)
            kept.sort()
        indices = kept
    return [{'frame': int(f), 'center_m': [round(float(v), 6) for v in centers[f]]} for f in indices]


def _clear_torso_if_needed(center, world_frame, obj, fwd_xz):
    inv = Rotation.from_euler('y', obj['heading_deg'], degrees=True).inv()
    half = np.asarray(obj['size_m'], dtype=np.float64) / 2.0 + 0.08
    push = np.array([fwd_xz[0], 0.0, fwd_xz[1]])
    for joint_idx in (0, 3, 6, 9, 12, 15):
        pt = world_frame[joint_idx]
        local = inv.apply(pt - center)
        if np.all(np.abs(local) < half):
            need = float(half[2] + 0.14) - float(np.dot(center - pt, push))
            if need > 0.0:
                center = center + push * need
    return center


def couple_grasped_objects(arrays, scene, rest, parents):
    """Couple grasped objects and wrists over each attachment window with < 1 mm slip."""
    rest = np.asarray(rest, dtype=np.float64)
    parents = np.asarray(parents, dtype=int)
    updated_arrays = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in arrays.items()}
    updated_scene = copy.deepcopy(scene)
    frames = len(updated_arrays['native_transl'])
    attachments = updated_scene.get('attachments', [])
    if not attachments:
        updated_arrays['scene_spec_json'] = human_interaction.encode(updated_scene)
        return updated_arrays, updated_scene, {
            'version': VERSION, 'coupled': 0, 'max_slip_m': 0.0, 'attachments': []
        }

    obj_map = {obj['id']: obj for obj in updated_scene['objects']}
    audits = []
    for att in attachments:
        obj = obj_map[att['object_id']]
        start_frame = int(att['start_frame'])
        end_frame = int(att['end_frame'])
        mode = att['mode']
        joints = tuple(int(j) for j in att['joints'])
        anchors = np.asarray(att['anchors_m'], dtype=np.float64)
        box_rot = Rotation.from_euler('y', obj['heading_deg'], degrees=True)
        rot_anchors = box_rot.apply(anchors)
        mean_rot_anchor = rot_anchors.mean(axis=0)

        world = _world_joints(updated_arrays, rest)
        fwd_xz = _pelvis_forward_xz(world)
        orig_centers = human_interaction.object_centers(obj, frames).copy()
        guide = orig_centers.copy()

        shoulders = [int(parents[parents[w]]) for w in joints]
        elbows = [int(parents[w]) for w in joints]
        uppers = [float(np.linalg.norm(rest[s] - rest[e])) for s, e in zip(shoulders, elbows)]
        lowers = [float(np.linalg.norm(rest[e] - rest[w])) for e, w in zip(elbows, joints)]
        r_max = min(u + l for u, l in zip(uppers, lowers)) - 2e-3
        r_min = max(abs(u - l) for u, l in zip(uppers, lowers)) + 2e-3

        has_track = 'trajectory' in obj and len(obj['trajectory']) > 1
        start_pivots = [world[start_frame, s] - ra for s, ra in zip(shoulders, rot_anchors)]
        base_start = _project_reachable(orig_centers[start_frame], start_pivots, r_min, r_max)
        orig_mid_start = world[start_frame, list(joints)].mean(axis=0)

        for f in range(start_frame, end_frame):
            pivots = [world[f, s] - ra for s, ra in zip(shoulders, rot_anchors)]
            if mode == 'follow_hands':
                orig_mid = world[f, list(joints)].mean(axis=0)
                c = orig_mid - mean_rot_anchor
                c = _clear_torso_if_needed(c, world[f], obj, fwd_xz[f])
            elif not has_track:
                orig_mid = world[f, list(joints)].mean(axis=0)
                c = base_start + (orig_mid - orig_mid_start)
                c = _clear_torso_if_needed(c, world[f], obj, fwd_xz[f])
            else:
                c = orig_centers[f].copy()
            guide[f] = _project_reachable(c, pivots, r_min, r_max)

        guide_obj = dict(
            obj,
            trajectory=[{'frame': int(f), 'center_m': guide[f].tolist()} for f in range(frames)],
        )
        updated_arrays = carry_grip.fit_window(
            updated_arrays, guide_obj, start_frame, end_frame, anchors, rest, parents,
            ramp_frames=8, joints=joints,
        )
        world = _world_joints(updated_arrays, rest)

        new_centers = orig_centers.copy()
        for f in range(start_frame, end_frame):
            if len(joints) == 2:
                hand_mid = 0.5 * (world[f, joints[0]] + world[f, joints[1]])
            else:
                hand_mid = world[f, joints[0]]
            new_centers[f] = hand_mid - mean_rot_anchor
        if end_frame < frames:
            last_pos = new_centers[end_frame - 1].copy()
            if mode == 'place':
                floor_y = float(obj['size_m'][1]) / 2.0
                drop_len = min(15, frames - end_frame)
                for k in range(frames - end_frame):
                    f = end_frame + k
                    if k < drop_len:
                        u = (k + 1) / float(drop_len)
                        s = 3.0 * u * u - 2.0 * u * u * u
                        y = last_pos[1] * (1.0 - s) + floor_y * s
                    else:
                        y = floor_y
                    new_centers[f] = [last_pos[0], y, last_pos[2]]
            else:
                new_centers[end_frame:] = last_pos

        if start_frame == 0:
            obj['center_m'] = [round(float(v), 6) for v in new_centers[0]]
        else:
            obj['center_m'] = [round(float(v), 6) for v in obj['center_m']]
            new_centers[0] = np.asarray(obj['center_m'], dtype=np.float64)

        must_keep = {0, max(0, start_frame - 1), start_frame, end_frame - 1,
                     min(frames - 1, end_frame), frames - 1}
        must_keep.update(
            int(kf['frame']) for kf in updated_scene.get('keyframes', [])
            if kf.get('object_id') == obj['id']
        )
        if mode == 'place' and end_frame < frames:
            must_keep.update(range(end_frame, min(frames, end_frame + 16)))
        track = _compress_trajectory(new_centers, must_keep, max_points=128)
        track[0] = {'frame': 0, 'center_m': list(obj['center_m'])}
        obj['trajectory'] = track

        actual = human_interaction.object_centers(obj, frames)
        box_inv = box_rot.inv()
        for kf in updated_scene.get('keyframes', []):
            if kf.get('object_id') != obj['id']:
                continue
            f = int(kf['frame'])
            orig_world = orig_centers[f] + box_rot.apply(kf['pelvis_offset_m'])
            new_offset = box_inv.apply(orig_world - actual[f])
            kf['pelvis_offset_m'] = [round(float(v), 6) for v in new_offset]
        slips = [
            np.linalg.norm(world[start_frame:end_frame, j] - (actual[start_frame:end_frame] + ra), axis=1)
            for j, ra in zip(joints, rot_anchors)
        ]
        combined = np.concatenate(slips)
        max_slip = float(combined.max())
        mean_slip = float(combined.mean())
        audits.append({
            'object_id': obj['id'],
            'start_frame': start_frame,
            'end_frame': end_frame,
            'mode': mode,
            'joints': list(joints),
            'max_slip_m': max_slip,
            'mean_slip_m': mean_slip,
            'passed': max_slip < 0.001,
        })

    updated_arrays['scene_spec_json'] = human_interaction.encode(updated_scene)
    audit = {
        'version': VERSION,
        'coupled': len(audits),
        'max_slip_m': max((a['max_slip_m'] for a in audits), default=0.0),
        'attachments': audits,
    }
    return updated_arrays, updated_scene, audit


def _detect_grasp_window(world, fwd_xz):
    frames = len(world)
    fallback = (round(0.25 * (frames - 1)), round(0.80 * (frames - 1)))
    mid = 0.5 * (world[:, 20] + world[:, 21])
    pel = world[:, 0]
    rel_xz = mid[:, [0, 2]] - pel[:, [0, 2]]
    fwd_reach = (rel_xz * fwd_xz).sum(axis=1)
    wrist_sep = np.linalg.norm(world[:, 20] - world[:, 21], axis=1)
    mid_vel = np.linalg.norm(np.gradient(mid, axis=0), axis=1) * 30.0
    score_mask = (fwd_reach > 0.15) & (mid[:, 1] >= 0.20) & (wrist_sep < 1.0) & (mid_vel < 2.2)

    # Bridge single-frame dips inside a grasp plateau.
    bridged = score_mask.copy()
    for f in range(1, frames - 1):
        if not bridged[f] and bridged[f - 1] and bridged[f + 1]:
            bridged[f] = True

    best_start, best_end = None, None
    cur_start = None
    for f, flag in enumerate(bridged):
        if flag and cur_start is None:
            cur_start = f
        elif not flag and cur_start is not None:
            if best_start is None or (f - cur_start) > (best_end - best_start):
                best_start, best_end = cur_start, f
            cur_start = None
    if cur_start is not None and (best_start is None or (frames - cur_start) > (best_end - best_start)):
        best_start, best_end = cur_start, frames

    if best_start is None or (best_end - best_start) < max(10, round(0.15 * frames)):
        return fallback
    return int(best_start), int(best_end)


def attach_object_to_motion(arrays, objects, attachment_spec, rest, parents):
    """Attach a dynamic object to a free-text or video-extracted motion and return report."""
    rest_arr = np.asarray(rest, dtype=np.float64)
    parents_arr = np.asarray(parents, dtype=np.int32)
    if not isinstance(objects, list) or not 1 <= len(objects) <= 16:
        raise ValueError('物体列表需要 1–16 个物体')
    if not isinstance(attachment_spec, dict) or 'object_id' not in attachment_spec:
        raise ValueError('attachment_spec 需要包含 object_id')

    objs = copy.deepcopy(objects)
    frames = len(arrays['native_transl'])
    if frames < 30:
        raise ValueError('物体响应绑定至少需要 30 帧（1 秒）动作')

    target_id = attachment_spec['object_id']
    obj = next((o for o in objs if o.get('id') == target_id), None)
    if obj is None:
        obj = next((o for o in objs if o.get('id') == f'{target_id}-box'), None)
    if obj is None:
        raise ValueError(f'未找到目标物体：{target_id}')

    mode = attachment_spec.get('mode', 'carry')
    if mode not in ('lift', 'carry', 'co_carry', 'follow_hands', 'place'):
        raise ValueError('无效的物体响应模式：' + str(mode))

    world = _world_joints(arrays, rest_arr)
    fwd_xz = _pelvis_forward_xz(world)

    start_frame = attachment_spec.get('start_frame')
    end_frame = attachment_spec.get('end_frame')
    auto_detect = bool(attachment_spec.get('auto_detect', True))
    if start_frame is None or end_frame is None:
        det_start, det_end = (_detect_grasp_window(world, fwd_xz) if auto_detect
                              else (round(0.25 * (frames - 1)), round(0.80 * (frames - 1))))
        if start_frame is None:
            start_frame = det_start if (end_frame is None or det_start < end_frame) else min(det_start, end_frame - 1)
        if end_frame is None:
            end_frame = det_end if det_end > start_frame else max(det_end, start_frame + 1)
    start_frame = int(start_frame)
    end_frame = int(end_frame)
    if not 0 <= start_frame < end_frame <= frames:
        raise ValueError('无效的抓取帧区间')

    bx, by, bz = [float(v) for v in obj['size_m']]
    az = max(-bz / 2.0, min(bz / 2.0, -0.05))
    anchors_m = [
        [round(bx / 2.0, 6), 0.0, round(az, 6)],
        [round(-bx / 2.0, 6), 0.0, round(az, 6)],
    ]

    inv = Rotation.from_euler('y', obj['heading_deg'], degrees=True).inv()
    half = np.asarray(obj['size_m'], dtype=np.float64) / 2.0 + 0.12
    body_pts = world[[0, start_frame]][:, [0, 1, 2, 4, 5, 7, 8, 10, 11]].reshape(-1, 3)
    local_body = inv.apply(body_pts - np.asarray(obj['center_m'], dtype=np.float64))
    overlaps_pelvis = bool(np.any(np.all(np.abs(local_body) < half, axis=1)))
    auto_place = bool(attachment_spec.get('auto_place', True))

    if auto_place or overlaps_pelvis:
        fwd = fwd_xz[start_frame]
        yaw_deg = round(float(np.rad2deg(np.arctan2(fwd[0], fwd[1]))), 4)
        pel_xz = world[start_frame, 0, [0, 2]]
        cx, cz = (pel_xz + fwd * (bz / 2.0 + 0.34)).tolist()
        sh0, sh1 = world[start_frame, 16], world[start_frame, 17]
        upper = float(np.linalg.norm(rest_arr[16] - rest_arr[18]))
        lower = float(np.linalg.norm(rest_arr[18] - rest_arr[20]))
        r_min, r_max = abs(upper - lower) + 5e-3, upper + lower - 5e-3
        box_rot = Rotation.from_euler('y', yaw_deg, degrees=True)
        floor_c = np.array([cx, by / 2.0, cz])
        floor_ok = all(
            r_min <= float(np.linalg.norm(floor_c + box_rot.apply(a) - sh)) <= r_max
            for sh, a in zip((sh0, sh1), anchors_m)
        )
        if floor_ok:
            cy = by / 2.0
        else:
            sh_y = float(0.5 * (sh0[1] + sh1[1]))
            orig_mid_y = float(0.5 * (world[start_frame, 20, 1] + world[start_frame, 21, 1]))
            cy = max(by / 2.0, min(sh_y - 0.12, max(0.55, orig_mid_y)))
        obj['center_m'] = [round(cx, 6), round(cy, 6), round(cz, 6)]
        obj['heading_deg'] = yaw_deg
        obj.pop('trajectory', None)

    yaw0 = round(float(np.rad2deg(np.arctan2(fwd_xz[0, 0], fwd_xz[0, 1]))), 4)
    scene_dict = {
        'kind': human_interaction.VERSION,
        'objects': objs,
        'waypoints': [{
            'frame': 0,
            'xz_m': [round(float(world[0, 0, 0]), 6), round(float(world[0, 0, 2]), 6)],
            'heading_deg': yaw0,
        }],
        'keyframes': [],
        'contacts': [
            {
                'start_frame': start_frame,
                'end_frame': end_frame,
                'object_id': obj['id'],
                'joint_index': j,
                'anchor_m': a,
                'tolerance_m': 0.02,
            }
            for j, a in zip((20, 21), anchors_m)
        ],
        'attachments': [{
            'object_id': obj['id'],
            'start_frame': start_frame,
            'end_frame': end_frame,
            'mode': mode,
            'joints': [20, 21],
            'anchors_m': anchors_m,
        }],
    }

    updated_arrays, updated_scene, audit = couple_grasped_objects(arrays, scene_dict, rest_arr, parents_arr)
    updated_arrays['rest_joints'] = rest_arr.astype(np.float32)
    updated_arrays['parents'] = parents_arr.astype(np.int32)
    updated_arrays['scene_spec_json'] = human_interaction.encode(updated_scene)
    human_interaction.check(updated_scene, frames)
    validate_motion(updated_arrays)
    report_dict = human_interaction.report(updated_arrays, updated_scene, rest_arr, parents_arr)
    if not report_dict['passed'] and report_dict['max_skeleton_penetration_m'] > human_interaction.MAX_PENETRATION_M:
        from .human_interaction_repair import _repair
        repaired, _, _ = _repair(updated_arrays, updated_scene, rest_arr, parents_arr, lift_feet=True)
        if repaired is not updated_arrays:
            repaired_report = human_interaction.report(repaired, updated_scene, rest_arr, parents_arr)
            if repaired_report['max_skeleton_penetration_m'] < report_dict['max_skeleton_penetration_m']:
                updated_arrays = repaired
                updated_arrays['rest_joints'] = rest_arr.astype(np.float32)
                updated_arrays['parents'] = parents_arr.astype(np.int32)
                updated_arrays['scene_spec_json'] = human_interaction.encode(updated_scene)
                report_dict = repaired_report
    report_dict['object_response'] = audit
    return updated_arrays, updated_scene, report_dict
