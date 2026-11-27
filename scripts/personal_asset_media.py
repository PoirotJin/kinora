"""Validation and upload workflows for personal media."""
from contextlib import contextmanager
import hashlib
from io import StringIO
import os
import re
import uuid

from personal_assets import ID, UPLOAD_BYTES, encoded, fields, label, upload
from review_access import require_motion_access

UPLOAD_LIMIT = 12 * 1024 * 1024
KINDS = {'rigs': 'rig', 'poses': 'pose', 'objects': 'object'}


@contextmanager
def storage(library, estimate):
    from g2h.capacity import reservation
    from g2h import resource_quota
    from review_access import active_account
    root = library.store.root
    ticket = 'personal-media:' + uuid.uuid4().hex
    role = active_account(root, library.owner)
    amount = {'storage_bytes': estimate}
    resource_quota.reserve(root, ticket, library.owner, amount, role)
    used = [0]
    try:
        with reservation(root, estimate, ticket) as capacity:
            yield used
            capacity.commit(used[0])
    finally:
        resource_quota.settle(root, ticket, {'storage_bytes': used[0]})


def check_retained_source(library, name, raw, *, restore_missing=False):
    """Return the bytes still to charge for a new source. Call under the media lock."""
    from g2h.capacity import reservation

    path = library.root/name
    if path.is_symlink():
        raise ValueError('资产源文件路径无效')
    try:
        with path.open('rb') as source:
            if source.read(len(raw)+1) == raw:
                return 0
            old_size = os.fstat(source.fileno()).st_size
    except FileNotFoundError:
        if not restore_missing:
            return len(raw)
        old_size = 0
    # A repair replaces or restores bytes already charged to this asset.
    with reservation(library.store.root, len(raw), 'personal-media-repair:'+name) as capacity:
        library.put_file(name, raw)
        capacity.commit(len(raw)-old_size)
    return 0


def restore_retained_source(library, name, raw):
    with library.write_lock('.media-source.lock'):
        check_retained_source(library, name, raw, restore_missing=True)


def save_with_source(library, name, raw, save, *, restore_missing=False, motion_estimate=0):
    # Rig saves and motion imports share the same content-addressed BVH file.
    with library.write_lock('.media-source.lock'):
        source_bytes = check_retained_source(library, name, raw, restore_missing=restore_missing)
        estimate = motion_estimate + source_bytes
        if not estimate:
            return save()
        with storage(library, estimate) as used:
            try:
                if source_bytes:
                    library.put_file(name, raw)
                    used[0] = source_bytes
                result = save()
            except Exception:
                if source_bytes:
                    # A failed save cannot leave a new source for another request to reuse.
                    (library.root/name).unlink(missing_ok=True)
                    used[0] = 0
                raise
            if motion_estimate:
                used[0] += result['artifact_bytes']
            return result


def inspect_bvh(raw):
    from g2h import bvh_import
    parsed = bvh_import.read(raw)
    joints, values, dt, signature = parsed
    mapping, missing = bvh_import.mapping(joints)
    return parsed, dict(signature=signature, frames=len(values), fps=1/dt, rig_save_request_id=True, rig_import_preflight=True,
                        joints=[row[0] for row in joints], mapping=mapping, missing=missing)


def save_rig(library, value):
    from g2h import bvh_import
    fields(value, ('id', 'name', 'file', 'frame', 'mapping', 'units', 'up', 'forward', 'previous', 'validate_import'),
           ('name', 'file', 'mapping', 'units', 'up', 'forward'))
    request_id = value.get('id')
    if 'id' in value and (not isinstance(request_id, str) or not re.fullmatch(ID, request_id)):
        raise ValueError('预设请求编号无效')
    name = label(value['name'])
    raw = upload(value['file'])
    parsed, info = inspect_bvh(raw)
    joints, channels, _, signature = parsed
    units = value['units']
    if type(units) not in (float, int) or units not in (.001, .01, 1):
        raise ValueError('请选择毫米、厘米或米')
    frame = value.get('frame')
    if frame is not None and (type(frame) is not int or not 0 <= frame < len(channels)):
        raise ValueError('校准帧超出范围')
    mapping, missing = bvh_import.mapping(joints, value['mapping'])
    if missing:
        raise ValueError('缺少必要关节：' + ', '.join(missing))
    validate_import = value.get('validate_import', False)
    if type(validate_import) is not bool:
        raise ValueError('预设导入校验参数无效')
    if validate_import:
        pelvis = next(index for index, joint in enumerate(joints) if joint[0] == mapping['pelvis'])
        bvh_import.validate_animation_translations(joints, channels, pelvis)
    reference = channels[0] * 0 if frame is None else channels[frame]
    data = dict(**{k: info[k] for k in ('joints','signature')}, mapping=mapping,
                units=units, up=value['up'], forward=value['forward'],
                calibration_frame=frame, calibration_channels=reference.tolist())
    previous = value.get('previous')
    data['reference_sha256'] = hashlib.sha256(raw).hexdigest()

    def save(save_request=None):
        if previous is not None:
            library.require_current_asset(library.asset(previous, 'rig'))
        bvh_import.convert((joints, channels[:2], 1/30, signature), data)
        return save_with_source(library, data['reference_sha256'] + '.bvh', raw,
                                lambda: library.save_asset('rig', name, data, previous, save_request=save_request))

    # Check the version and reserve storage under the same lock as other rig saves.
    with library.write_lock('.rig-save.lock'):
        if request_id is None:
            return save()
        fingerprint = hashlib.sha256(encoded([name, previous, data])).hexdigest()
        saved = library.asset_for_save_request('rig', request_id, fingerprint)
        if saved is not None:
            restore_retained_source(library, data['reference_sha256'] + '.bvh', raw)
            return saved
        return save((request_id, fingerprint))


def ensure_saved_motion(library, motion, reconstruct, recovery_error):
    from g2h.motion_assets import MotionArtifactCorrupt, SavedMotionUnavailable

    assets = library.service.assets
    try:
        assets.artifact_path(library.owner,motion['id'])
    except (FileNotFoundError, MotionArtifactCorrupt):
        try:
            assets.restore_artifact_from_arrays(library.owner,motion['id'],reconstruct())
        except (FileNotFoundError, SavedMotionUnavailable, ValueError) as error:
            raise ValueError(recovery_error) from error
    return motion


def import_motion(library, value):
    from g2h import bvh_import
    fields(value, ('name','rig','file','id'), ('name','rig','file','id'))
    name = label(value['name'])
    if not isinstance(value['id'], str) or not re.fullmatch(ID, value['id']):
        raise ValueError('导入编号无效')
    rig = library.asset(value['rig'], 'rig')
    raw = upload(value['file'])
    digest = hashlib.sha256(raw).hexdigest()
    request_id = 'personal-bvh:' + value['id']
    assets = library.service.assets

    def convert():
        return bvh_import.convert(bvh_import.read(raw), rig['data'])

    old = assets.find_request(library.owner, request_id)
    arrays = convert() if old is None else None
    with library.write_lock('.bvh-import.lock'):
        if old is None:
            old = assets.find_request(library.owner, request_id)
        if old is not None:
            provenance = old['provenance']
            if (old['description'], provenance.get('rig_version'), provenance.get('original_sha256')) != (name, rig['id'], digest):
                raise ValueError('导入编号已被使用')
            restore_retained_source(library, digest + '.bvh', raw)
            return ensure_saved_motion(library,old,lambda: arrays if arrays is not None else convert(),
                                       '无法校验恢复后的动作，请重新导入为新动作。')

        estimate = sum(a.nbytes for a in arrays.values()) + 32768
        def register():
            return assets.register(library.owner, arrays, description=name, request_id=request_id,
                                   provenance={'kind':'bvh_import','rig_version':rig['id'],
                                               'original_sha256':digest,'root_policy':'preserve_metric_trajectory'})

        return save_with_source(library, digest + '.bvh', raw, register, motion_estimate=estimate)


def triple(value, lower, upper):
    if not isinstance(value, list) or len(value) != 3 or not all(
            type(x) in (int,float) and lower <= x <= upper for x in value):
        raise ValueError('需要三个有效的数值')
    return value


def obj_lines(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise ValueError('OBJ 文件必须使用 UTF-8 编码。') from error
    fragments = []
    first = 0
    for number, line in enumerate(StringIO(text, newline=None), 1):
        line = line.partition('#')[0].rstrip()
        if not line:
            continue
        if not fragments:
            first = number
        if line.endswith('\\'):
            fragments.append(line[:-1])
            continue
        fragments.append(line)
        yield first, ' '.join(fragments).split()
        fragments.clear()
    if fragments:
        raise ValueError(f'OBJ 第 {first} 行：续行未完成')


def mesh_data(raw):
    """Decode positions and indexed faces only. External OBJ/MTL references are ignored."""
    vertices, polygons = [], []
    triangle_count = 0
    for line, parts in obj_lines(raw):
        if parts[0] == 'v':
            if len(vertices) >= 100000:
                raise ValueError('OBJ 超过 10 万顶点')
            try:
                vertex = triple([float(x) for x in parts[1:4]], -1e7, 1e7)
            except ValueError as error:
                raise ValueError(f'OBJ 第 {line} 行：顶点坐标无效') from error
            vertices.append(vertex)
        elif parts[0] == 'f':
            if len(parts) < 4:
                raise ValueError(f'OBJ 第 {line} 行：面至少需要 3 个顶点')
            triangle_count += len(parts) - 3
            if len(parts) > 33 or triangle_count > 200000:
                raise ValueError('OBJ 面片数或单面顶点数过多')
            try:
                indices = [int(x.partition('/')[0]) for x in parts[1:]]
            except ValueError as error:
                raise ValueError(f'OBJ 第 {line} 行：顶点索引无效') from error
            indices = [i-1 if i > 0 else len(vertices)+i if i < 0 else -1 for i in indices]
            if any(not 0 <= i < len(vertices) for i in indices):
                raise ValueError(f'OBJ 第 {line} 行：顶点索引无效')
            polygons.append(indices)
    if len(vertices) < 3 or not polygons:
        raise ValueError('OBJ 未包含可预览的三角网格')
    return vertices, polygons


def read_object_mesh(library, digest):
    try:
        with (library.root/(digest+'.obj')).open('rb') as source:
            raw = source.read(UPLOAD_BYTES+1)
    except FileNotFoundError as error:
        raise ValueError('OBJ 原文件缺失，请重新上传 OBJ 文件。') from error
    except OSError as error:
        raise OSError('暂时无法读取 OBJ 原文件。') from error
    if len(raw) > UPLOAD_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('扫描模型校验失败，请重新上传 OBJ 文件。')
    return raw


def save_object(library, value):
    fields(value, ('id','name','file','size_m','anchors','heading_deg','previous'),
           ('name','size_m','anchors','heading_deg'))
    request_id = value.get('id')
    if 'id' in value and (not isinstance(request_id, str) or not re.fullmatch(ID, request_id)):
        raise ValueError('预设请求编号无效')
    name = label(value['name'])
    size = triple(value['size_m'], .01, 20)
    heading = value['heading_deg']
    if type(heading) not in (float,int) or not -360 <= heading <= 360:
        raise ValueError('朝向角度无效')
    if not isinstance(value['anchors'], list) or len(value['anchors']) > 16:
        raise ValueError('最多保存 16 个接触点')
    anchors = []
    for anchor in value['anchors']:
        fields(anchor, ('name','position_m'), ('name','position_m'))
        position = triple(anchor['position_m'], -20, 20)
        if abs(max(abs(x)-edge/2 for x,edge in zip(position,size))) > .005:
            raise ValueError('接触点须位于简化包围盒的表面')
        anchors.append(dict(name=label(anchor['name']),position_m=position))
    if len({a['name'] for a in anchors}) != len(anchors):
        raise ValueError('接触点名称不能重复')
    previous = value.get('previous')
    raw = upload(value['file']) if 'file' in value else None
    if raw is None and previous is None:
        raise ValueError('请选择 OBJ 扫描文件')
    data = dict(size_m=size, heading_deg=heading, anchors=anchors, origin='box_center')
    base = library.asset(previous, 'object') if previous is not None else None
    previous_digest = base['data']['mesh_sha256'] if base else None
    digest = previous_digest if raw is None else hashlib.sha256(raw).hexdigest()
    data['mesh_sha256'] = digest

    def save(save_request=None):
        if base is not None:
            library.require_current_asset(base)
        if raw is None:
            read_object_mesh(library, data['mesh_sha256'])
            return library.save_asset('object', name, data, previous, save_request=save_request)
        mesh_data(raw)
        return save_with_source(library, digest + '.obj', raw,
                                lambda: library.save_asset('object', name, data, previous, save_request=save_request),
                                restore_missing=digest == previous_digest)

    with library.write_lock('.object-save.lock'):
        if request_id is None:
            return save()
        fingerprint = hashlib.sha256(encoded([name, previous, data])).hexdigest()
        saved = library.asset_for_save_request('object', request_id, fingerprint)
        if saved is not None:
            if raw is not None:
                restore_retained_source(library, data['mesh_sha256'] + '.obj', raw)
            return saved
        return save((request_id, fingerprint))


def save_pose(library, value):
    import numpy as np
    fields(value, ('id','name','motion_id','frame'), ('name','motion_id','frame'))
    request_id = value.get('id')
    if 'id' in value and (not isinstance(request_id, str) or not re.fullmatch(ID, request_id)):
        raise ValueError('预设请求编号无效')
    name = label(value['name'])
    mid, frame = value['motion_id'], value['frame']
    if type(frame) is not int or frame < 0:
        raise ValueError('姿态帧超出动作范围')

    def save(save_request=None):
        motion = library.motion(mid)
        version = library.materialize(motion['motion_id'], required_frame=frame)
        arrays, _ = library.service.assets.load(library.owner, version['id'])
        data = dict(motion_id=motion['motion_id'], version_id=version['id'], frame=frame,
                    smpl_poses=np.asarray(arrays['smpl_poses'][frame]).reshape(-1).tolist())
        return library.save_asset('pose', name, data, save_request=save_request)

    if request_id is None:
        return save()
    fingerprint = hashlib.sha256(encoded([name, mid, frame])).hexdigest()
    with library.write_lock('.pose-save.lock'):
        saved = library.asset_for_save_request('pose', request_id, fingerprint)
        return saved if saved is not None else save((request_id, fingerprint))


def rig_preview(library, asset):
    import numpy as np
    from g2h import bvh_import
    from g2h.human_rig import definition

    profile = asset['data']
    raw = (library.root / (profile['reference_sha256']+'.bvh')).read_bytes()
    if hashlib.sha256(raw).hexdigest() != profile['reference_sha256']:
        raise ValueError('校准 BVH 校验失败')
    joints, _, _, signature = bvh_import.read(raw)
    neutral = np.asarray(profile['calibration_channels'],dtype=float)[None,:]
    source_pelvis = next(i for i,joint in enumerate(joints) if joint[0] == profile['mapping']['pelvis'])
    source, _ = bvh_import.world(joints,neutral,bvh_import.axes(profile['up'],profile['forward']),
                                 profile['units'],source_pelvis)
    result = bvh_import.convert((joints,np.repeat(neutral,2,axis=0),1/30,signature),profile)
    return dict(source=dict(points=source[0].tolist(),parents=[joint[1] for joint in joints]),
                target=dict(points=result['native_keypoints3d'][0].tolist(),parents=definition()[1].tolist()))


def apply_pose(library, value):
    import numpy as np
    from scipy.spatial.transform import Rotation
    from g2h.human_rig import definition, motion_rig
    from g2h.kimodo_adapter import fk

    keys = ('id','pose','motion_id','frame','blend')
    fields(value, keys, keys)
    if not isinstance(value['id'], str) or not re.fullmatch(ID, value['id']):
        raise ValueError('请求编号无效')
    frame, blend = value['frame'], value['blend']
    if type(frame) is not int or frame < 0 or type(blend) is not int or not 1 <= blend <= 30:
        raise ValueError('请选择有效的目标帧及 1–30 帧过渡长度')
    pose = library.asset(value['pose'], 'pose')
    mid = value['motion_id']
    source = require_motion_access(library.store, mid, library.user)
    if not mid.startswith('ma_'):
        source = library.materialize(mid, required_frame=frame)
    if frame >= source['frames']:
        raise ValueError('姿态帧超出动作范围')
    assets = library.service.assets
    key = 'personal-pose:'+value['id']
    def existing_pose_application():
        motion = assets.find_request(library.owner, key)
        if motion is None:
            return None
        provenance = motion['provenance']
        if (motion['parent_id'], provenance.get('pose_version'), provenance.get('frame'),
                provenance.get('blend_frames')) != (source['id'], pose['id'], frame, blend):
            raise ValueError('Request ID was already used with different motion arguments')
        return motion

    start, end = max(0,frame-blend), min(source['frames'],frame+blend+1)

    def build_patch():
        from harness_service import saved_motion_read
        with saved_motion_read():
            arrays, _ = assets.load(library.owner, source['id'])
        if 'scene_spec_json' in arrays:
            raise ValueError('带场景接触约束的动作应在场景编辑中重新求解')
        rest, parents = motion_rig(arrays)
        if not np.array_equal(parents, definition()[1]):
            raise ValueError('动作骨架与姿态关节层级不一致')
        current = arrays['smpl_poses'].reshape(-1,52,3).copy()
        target = np.asarray(pose['data']['smpl_poses']).reshape(52,3)
        old = Rotation.from_rotvec(current[start:end,1:].reshape(-1,3))
        desired = Rotation.from_rotvec(np.tile(target[1:],(end-start,1)))
        weight = .5 + .5 * np.cos(np.pi * np.abs(np.arange(start,end)-frame)/blend)
        updated = old * Rotation.from_rotvec((old.inv()*desired).as_rotvec()*np.repeat(weight,51)[:,None])
        current[start:end,1:] = updated.as_rotvec().reshape(end-start,51,3)
        local = Rotation.from_rotvec(current.reshape(-1,3)).as_matrix().reshape(-1,52,3,3)
        points, _ = fk(local,rest,parents)
        # Keep the root translations so readers can regenerate preview aliases.
        return dict(fps=arrays['fps'],native_keypoints3d=points.astype('f4'),native_transl=arrays['native_transl'],
                    smpl_poses=current.reshape(-1,156),parents=parents,rest_joints=rest)

    existing = existing_pose_application()
    patched = build_patch() if existing is None else None
    provenance = {'tool':'apply_personal_pose','pose_version':pose['id'],
                  'sources':[{'motion_id':pose['data']['version_id'],'role':'pose_reference',
                              'start_frame':pose['data']['frame'],'end_frame':pose['data']['frame']+1}],
                  'frame':frame,'blend_frames':blend}
    with library.write_lock('.pose-apply.lock'):
        if existing is None:
            existing = existing_pose_application()
        if existing is not None:
            return ensure_saved_motion(library,existing,lambda: patched if patched is not None else build_patch(),
                                       '无法恢复已保存的姿态结果，请确认原动作可用后重试。')
        estimate = sum(array.nbytes for array in patched.values()) + 32768
        with storage(library, estimate) as used:
            meta = assets.register(library.owner,patched,description=source['description']+' · 插入姿态',
                                   parent_id=source['id'],selection={'start_frame':start,'end_frame':end},
                                   request_id=key,provenance=provenance)
            used[0] = meta['artifact_bytes']
            return meta



def handle(handler, library, route, query, write, after):
    if write and route == 'inspect-bvh':
        value = handler.body(limit=UPLOAD_LIMIT)
        fields(value, ('file',), ('file',))
        return handler.send(200, inspect_bvh(upload(value['file']))[1])
    if write and route == 'apply-pose':
        return handler.send(201, apply_pose(library, handler.body()))
    if write and route == 'import-bvh':
        return handler.send(201, import_motion(library, handler.body(limit=UPLOAD_LIMIT)))
    if route in KINDS:
        if not write:
            return handler.send(200, library.list_assets(KINDS[route], after, query.get('signature', [None])[0]))
        save = {'rigs':save_rig, 'poses':save_pose, 'objects':save_object}[route]
        return handler.send(201, save(library, handler.body(limit=UPLOAD_LIMIT)))
    asset_match = re.fullmatch(r'(rigs|poses|objects)/(' + ID + r')(/mesh|/calibration)?', route)
    if asset_match and not write:
        group, asset_id, mesh = asset_match.groups()
        asset = library.asset(asset_id, KINDS[group])
        if mesh == '/calibration' and group == 'rigs':
            return handler.send(200, rig_preview(library, asset))
        if mesh:
            if group != 'objects' or mesh != '/mesh':
                return handler.send(404, {'error':'未找到网格'})
            digest = asset['data']['mesh_sha256']
            raw = read_object_mesh(library, digest)
            vertices, polygons = mesh_data(raw)
            faces = [[polygon[0], polygon[j], polygon[j+1]]
                     for polygon in polygons for j in range(1,len(polygon)-1)]
            result = dict(vertices=vertices, faces=faces)
            if len(polygons) != len(faces):
                result['polygons'] = polygons
            return handler.send(200, result)
        return handler.send(200, asset)
    return handler.send(404, {'error':'未找到资产'})
