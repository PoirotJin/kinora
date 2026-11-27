"""Owner-scoped handoff of an isolated robot candidate for repair."""
import base64
import binascii
import hashlib
import io
import json
import math
import zipfile
from functools import lru_cache

from retarget_limits import MAX_FRAMES, MAX_NPZ_BYTES, MAX_SECONDS

UPLOAD_LIMIT = 17 * 1024 * 1024
VERSION = 'robot-repair-v1'


@lru_cache(maxsize=256)
def _complete_trajectory(path, file_id, robot, motion, expected_frames):
    import numpy as np

    try:
        with np.load(path, allow_pickle=False) as data:
            qpos, names = data['qpos'], data['joint_names']
            fps = float(data['fps'].item())
            metadata = json.loads(str(data['metadata']))
            return bool(isinstance(metadata, dict) and metadata.get('robot') == robot
                        and metadata.get('motion_id') == motion
                        and names.ndim == 1 and names.dtype.kind == 'U'
                        and qpos.ndim == 2 and qpos.shape[1] == len(names) + 7
                        and 2 <= len(qpos) <= MAX_FRAMES
                        and (not expected_frames or len(qpos) == expected_frames)
                        and math.isfinite(fps) and 1 <= fps <= 240 and len(qpos) / fps <= MAX_SECONDS
                        and qpos.dtype.kind in 'fiu' and np.isfinite(qpos).all()
                        and np.all(np.linalg.norm(qpos[:, 3:7], axis=1) > 1e-8))
    except (OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile):
        return False


def repair_source(folder, row, user):
    if row['owner'] != user['username']:
        return None
    if row['status'] == 'failed':
        path = folder / 'quarantine' / 'result.npz'
    elif row['status'] == 'done':
        path = folder / 'result.npz'
    else:
        return None
    try:
        stat = path.stat()
        if not path.is_file() or not 0 < stat.st_size <= MAX_NPZ_BYTES:
            return None
        file_id = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if not _complete_trajectory(path, file_id, row['robot'], row['motion_id'], row.get('total')):
            return None
    except OSError:
        return None
    try:
        report_path = folder / 'quality.json'
        quality = json.loads(report_path.read_text()) if report_path.stat().st_size <= MAX_NPZ_BYTES else {}
    except (OSError, ValueError):
        quality = {}
    if not isinstance(quality, dict):
        quality = {}
    if row['status'] == 'done':
        from retarget_quality import retarget_export_verified
        if retarget_export_verified(quality):
            return None
    quality.update(export_allowed=False)
    quality.setdefault('status', 'failed')
    if not isinstance(quality.get('checks'), dict):
        quality['checks'] = {}
    if not isinstance(quality.get('events'), list):
        quality['events'] = []
    quality.setdefault('reason', row.get('message') or '轨迹待修复，请复检后再正式导出')
    return path, quality


def draft_npz(path, row, quality):
    import numpy as np

    raw = path.read_bytes()
    binding = dict(version=VERSION, job_id=row['id'], source_sha256=hashlib.sha256(raw).hexdigest())
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        metadata = json.loads(str(data['metadata']))
        metadata.update(repair_source=binding, quality=quality,
                        validation='待修复候选，仅用于后处理；正式导出须经 Kinora 复检。')
        output = io.BytesIO()
        np.savez_compressed(output, qpos=data['qpos'], fps=data['fps'],
                            joint_names=data['joint_names'],
                            metadata=np.array(json.dumps(metadata, ensure_ascii=False)))
    return output.getvalue()


def imported_trajectory(value, session, original):
    """Accept the repaired qpos only, after checking its binding and layout."""
    import numpy as np

    if not isinstance(value, str) or not 0 < len(value) <= 4 * ((MAX_NPZ_BYTES + 2) // 3):
        raise ValueError('待复检 NPZ 最大 12 MiB')
    fields = {'qpos.npy', 'fps.npy', 'joint_names.npy', 'metadata.npy'}
    try:
        raw = base64.b64decode(value, validate=True)
        if not 0 < len(raw) <= MAX_NPZ_BYTES:
            raise ValueError('待复检 NPZ 最大 12 MiB')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != len(fields) or {item.filename for item in members} != fields:
                raise ValueError('请保留下载文件的 qpos、fps、joint_names 和 metadata 字段')
            if sum(item.file_size for item in members) > MAX_NPZ_BYTES:
                raise ValueError('NPZ 解压后超过 12 MiB')
            for item in members:
                with archive.open(item) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version not in ((1, 0), (2, 0)):
                        raise ValueError('不支持的 NPY 版本')
                    read_header = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                                   else np.lib.format.read_array_header_2_0)
                    shape, _, dtype = read_header(stream)
                    name = item.filename[:-4]
                    expected_shape = original[name].shape if name != 'metadata' else ()
                    if shape != expected_shape or math.prod(shape) * dtype.itemsize != item.file_size - stream.tell():
                        raise ValueError('NPZ 字段形状或数据长度不匹配')
                    if name in ('qpos', 'fps') and (dtype.kind not in 'fiu' or dtype.itemsize > 8):
                        raise ValueError('轨迹和帧率必须是数值数组')
                    if name in ('joint_names', 'metadata') and dtype.kind != 'U':
                        raise ValueError('关节名称和元数据必须是文本')
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            metadata = json.loads(str(data['metadata']))
            binding = dict(version=VERSION, job_id=session['job'], source_sha256=session['source_sha'])
            if not isinstance(metadata, dict) or metadata.get('repair_source') != binding:
                raise ValueError('文件与当前待修复作业不匹配，请使用该作业的待修复 NPZ')
            if not np.array_equal(data['joint_names'], original['joint_names']) or not np.array_equal(data['fps'], original['fps']):
                raise ValueError('请保留原始帧率和关节顺序')
            qpos = data['qpos'].astype(np.float64)
    except (binascii.Error, zipfile.BadZipFile, EOFError, OSError, NotImplementedError, RuntimeError) as error:
        raise ValueError('无法读取待复检 NPZ') from error
    if not np.isfinite(qpos).all() or np.max(np.abs(qpos)) > 100000:
        raise ValueError('轨迹包含非有限或超范围数值')
    return qpos.astype(np.float32)


def recheck(root, row, model, profile, qpos, fps, method, *, upright_required=False):
    from g2h.motion_semantics import for_record, message as direction_message, robot as check_direction
    from retarget_clearance import validate
    from retarget_quality import analyze, collision_pairs, failure_summary, retarget_requires_dynamics, PAIR_VERSION

    mid = row['motion_id']
    if mid.startswith('mimp'):
        from gadget_imports import Imports
        record = Imports(root).read(mid, row['owner'])
        semantics = check_direction(record.get('text', ''), qpos, fps)
    elif mid.startswith('ma_'):
        from g2h.motion_assets import MotionAssets
        arrays, meta = MotionAssets(root).load(row['owner'], mid)
        record = dict(motion_id=mid, text=meta.get('description', ''), motion=arrays)
        semantics = check_direction(record['text'], qpos, fps)
    else:
        from semantic_retarget import source_record
        record = source_record(root, mid)
        semantics = for_record(record, qpos, fps)
    pairs = profile['collision_pairs'] if profile else collision_pairs(model, row['robot'])
    clearance = validate(model, qpos, pairs)
    clearance.update(version=PAIR_VERSION + ':' + row['robot'], body_pairs=pairs)
    quality = analyze(model, qpos, fps, clearance, profile=profile, robot=row['robot'],
                      require_dynamics=retarget_requires_dynamics())
    quality.update(direction_semantics=semantics, pair_version=PAIR_VERSION,
                   repair_recheck=dict(version=VERSION, source_job=row['id'], method=method))
    quality['export_allowed'] = bool(quality['export_allowed'] and semantics['status'] != 'fail'
                                     and quality['checks']['dynamics'] is not False)
    if profile:
        from custom_robot_engine import measure_tracking
        from robot_diagnostics import tracking_events
        tracking = measure_tracking(model, profile, record['motion'], qpos)
        quality['tracking'] = tracking
        quality['events'].extend(tracking_events(tracking))
        quality['export_allowed'] = quality['export_allowed'] and tracking['passed']
    if upright_required or (root / 'review/retarget' / row['id'] / 'upright_quality.json').is_file():
        from retarget_upright_limits_v4 import validate as validate_upright
        upright = validate_upright(model, qpos, fps)
        quality['upright_quality'] = upright
        quality['checks']['upright_profile'] = upright['passed']
        quality['export_allowed'] = quality['export_allowed'] and upright['passed']
    if not quality['export_allowed']:
        quality['status'] = 'failed'
        quality['reason'] = direction_message(semantics) if semantics['status'] == 'fail' else failure_summary(quality)
    return quality


def export_verified(quality):
    from retarget_quality import VERSION as QUALITY_VERSION, PAIR_VERSION, retarget_export_verified
    proof = quality.get('repair_recheck') or {}
    if (proof.get('version') != VERSION or quality.get('version') != QUALITY_VERSION
            or quality.get('pair_version') != PAIR_VERSION or quality.get('export_allowed') is not True):
        return False
    return quality.get('checks', {}).get('dynamics') is not False and retarget_export_verified(quality)
