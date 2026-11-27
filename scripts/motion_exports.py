"""Read-only, account-authorized human motion exports. BVH units: centimetres."""
import io
import json
import math
import re
import sqlite3
import warnings
import zipfile
import zlib
import numpy as np
from scipy.spatial.transform import Rotation

from g2h.motion_assets import SavedMotionUnavailable
from review_access import Forbidden, Removed

MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_NPY_HEADER_BYTES = 10_000
SOURCE_DECODE_ERRORS = (
    EOFError, OSError, TypeError, ValueError, RuntimeError, OverflowError,
    zipfile.BadZipFile, zlib.error,
)


class ExportLimitExceeded(ValueError):
    pass


class ExportReadFailed(OSError):
    def __init__(self, message='暂时无法读取动作文件，请稍后重试。'):
        super().__init__(message)


class BatchExportFailed(ValueError):
    def __init__(self, status, message, failures):
        super().__init__(message)
        self.status = status
        self.failures = failures


def check_export_size(size, max_bytes):
    if max_bytes is not None and size > max_bytes:
        raise ExportLimitExceeded('批次超过 64 MiB，请减少所选动作')


def bvh(arrays, parents, rest, names, *, max_bytes=None):
    n = 52
    try:
        parents = np.asarray(parents)
        rest = np.asarray(rest, dtype=float)
        roots = np.asarray(arrays['native_transl'], dtype=float)
        poses = np.asarray(arrays['smpl_poses'], dtype=float)
        fps = np.asarray(arrays['fps'], dtype=float).item()
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError('动作源数据不完整，无法导出 BVH') from error
    if names is None:
        names = [f'Joint_{i}' for i in range(n)]
    if (parents.shape != (n,) or parents.dtype.kind not in 'iu' or rest.shape != (n, 3)
            or not isinstance(names, (list, tuple)) or len(names) != n):
        raise ValueError('缺少兼容的 SMPL-H 骨架，无法导出 BVH')
    if roots.ndim != 2 or roots.shape[1] != 3 or not len(roots) or not 0 < fps <= 240:
        raise ValueError('动作帧或帧率无效，无法导出 BVH')
    if poses.shape not in ((len(roots), n * 3), (len(roots), n, 3)):
        raise ValueError('关节旋转数据形状无效，无法导出 BVH')
    poses = poses.reshape(len(roots), n, 3)
    if not all(np.isfinite(x).all() for x in (roots,poses,rest)) or parents[0] not in (-1,0) or any(not 0<=parents[j]<j for j in range(1,n)):
        raise ValueError('动作或骨架包含无效数值')
    lines=['HIERARCHY'];order=[]
    def visit(j,depth):
        indent='  '*depth;order.append(j)
        name=re.sub(r'[^A-Za-z0-9_]', '_',str(names[j])) or 'Joint'
        lines.extend([indent+('ROOT ' if j==0 else 'JOINT ')+'J'+str(j)+'_'+name,indent+'{'])
        offset=np.zeros(3) if j==0 else (rest[j]-rest[parents[j]])*100
        lines.append(indent+'  OFFSET '+' '.join(f'{v:.8f}' for v in offset))
        lines.append(indent+'  CHANNELS '+('6 Xposition Yposition Zposition ' if j==0 else '3 ')+'Zrotation Xrotation Yrotation')
        children=np.flatnonzero(parents[1:]==j)+1
        for child in children:visit(int(child),depth+1)
        if not len(children):lines.extend([indent+'  End Site',indent+'  {',indent+'    OFFSET 0 0 0',indent+'  }'])
        lines.append(indent+'}')
    visit(0,0)
    lines += ['MOTION', f'Frames: {len(roots)}', f'Frame Time: {1/fps:.10f}']
    header = ('\n'.join(lines) + '\n').encode()
    # Every finite channel needs at least ten characters plus a separator or newline.
    check_export_size(len(header) + len(roots) * (3 + n * 3) * 11, max_bytes)
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore',message='Gimbal lock detected')
        angles=Rotation.from_rotvec(poses.reshape(-1,3)).as_euler('ZXY',degrees=True).reshape(len(roots),n,3)
    # Unwrap equivalent Euler values, without changing poses or root trajectories.
    angles=np.rad2deg(np.unwrap(np.deg2rad(angles),axis=0))
    channels=np.concatenate(((roots+rest[0])*100,angles[:,order].reshape(len(roots),-1)),axis=1)
    if not np.isfinite(channels).all():
        raise ValueError('转换后的动作包含无效数值，无法导出 BVH')
    output = io.BytesIO()
    output.write(header)
    for row in channels:
        line = (' '.join(f'{value:.8f}' for value in row) + '\n').encode()
        check_export_size(output.tell() + len(line), max_bytes)
        output.write(line)
    return output.getvalue()


def validate_npz_archive(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        arrays = [entry for entry in archive.infolist() if entry.filename.endswith('.npy') and not entry.is_dir()]
        if not arrays or len({entry.filename for entry in arrays}) != len(arrays):
            raise ValueError('NPZ does not contain uniquely named arrays')
        for entry in arrays:
            with archive.open(entry) as stream:
                version = np.lib.format.read_magic(stream)
                if version not in ((1, 0), (2, 0), (3, 0)):
                    raise ValueError('Unsupported NPY version')
                header_offset = stream.tell()
                length_bytes = 2 if version == (1, 0) else 4
                size_field = stream.read(length_bytes)
                if len(size_field) != length_bytes or int.from_bytes(size_field, 'little') > MAX_NPY_HEADER_BYTES:
                    raise ValueError('Invalid NPY header size')
                stream.seek(header_offset)
                if version == (1, 0):
                    shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                elif version == (2, 0):
                    shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                else:
                    # NumPy exposes no public reader for version 3 UTF-8 headers.
                    try:
                        from numpy.lib.format import _read_array_header
                    except ImportError:
                        from numpy.lib._format_impl import _read_array_header
                    shape, _, dtype = _read_array_header(stream, version, max_header_size=MAX_NPY_HEADER_BYTES)
                if (dtype.hasobject or any(size < 0 for size in shape)
                        or math.prod(shape) * dtype.itemsize != entry.file_size - stream.tell()):
                    raise ValueError('Invalid NPY array size or type')
        if archive.testzip() is not None:
            raise zipfile.BadZipFile('Invalid NPZ checksum')


def export_one(service,user,mid,format='npz', *, max_bytes=None):
    if format not in ('npz','bvh'):raise ValueError('仅支持 NPZ 或 BVH')
    try:
        raw = service.download(user,mid)
    except KeyError as error:
        if mid.startswith('ma_'):
            raise Forbidden('动作不存在或当前不可访问') from error
        raise
    except FileNotFoundError as error:
        raise Removed('动作源文件不存在或已移除。') from error
    except SavedMotionUnavailable as error:
        raise ExportReadFailed(str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise ExportReadFailed() from error
    if format == 'npz':
        check_export_size(len(raw), max_bytes)
        try:
            validate_npz_archive(raw)
        except SOURCE_DECODE_ERRORS as error:
            raise ValueError('动作源数据不完整，无法导出 NPZ') from error
        return raw
    try:
        with np.load(io.BytesIO(raw), allow_pickle=False) as data:
            arrays = dict(data)
    except SOURCE_DECODE_ERRORS as error:
        raise ValueError('动作源数据不完整，无法导出 BVH') from error
    parents = arrays.get('parents')
    rest = arrays.get('rest_joints')
    if rest is not None or 'scene_spec_json' in arrays:
        from g2h.human_rig import definition, motion_rig

        _, default_parents, names = definition()
        if rest is None:
            try:
                rest, parents = motion_rig(arrays)
            except ValueError as error:
                raise ValueError('无法读取动作内嵌骨架，无法导出 BVH') from error
        elif parents is None:
            parents = default_parents
    else:
        rig = service.root/'third_party/HY-Motion-1.0/retargeting/smplh'
        cache = getattr(service.legacy, 'preview_cache', None)
        if cache is not None:
            rig = cache.assets
        try:
            if parents is None:
                parents = np.fromfile(rig/'kintree.bin', dtype=np.int32)
            rest = np.fromfile(rig/'j_template.bin', dtype=np.float32).reshape(-1, 3)
            names_path = rig/'joint_names.json'
            names = json.loads(names_path.read_text()) if names_path.exists() else None
        except (OSError, ValueError) as error:
            raise ValueError('无法读取 SMPL-H 骨架，无法导出 BVH') from error
    return bvh(arrays,parents,rest,names,max_bytes=max_bytes)


def export_batch(service,user,ids,format):
    if not 1<=len(ids)<=20 or len(set(ids))!=len(ids) or any(not re.fullmatch('[A-Za-z0-9_-]{1,96}',x) for x in ids):
        raise ValueError('每次选择 1–20 个不同动作')
    if format not in ('npz','bvh'):raise ValueError('仅支持 NPZ 或 BVH')
    output=io.BytesIO();results=[];total=0
    messages = {
        'batch_limit': '超出批次大小限制，请减少所选动作或单独下载。',
        'unavailable': '动作不存在或当前无权下载。',
        'invalid_motion': '动作数据或骨架不完整，无法导出。',
    }
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for mid in ids:
            message = None
            try:
                raw = export_one(service,user,mid,format,max_bytes=MAX_BATCH_BYTES - total)
            except ExportLimitExceeded:
                reason = 'batch_limit'
            except (Forbidden, KeyError, Removed):
                reason = 'unavailable'
            except ExportReadFailed as error:
                reason = 'read_failed'
                message = str(error)
            except (ValueError, TypeError, OverflowError, EOFError, zipfile.BadZipFile, zlib.error):
                reason = 'invalid_motion'
            else:
                filename = mid + '.' + format
                archive.writestr(filename, raw)
                total += len(raw)
                results.append({'motion_id':mid,'status':'saved','file':filename})
                continue
            results.append({'motion_id':mid,'status':'failed','reason':reason,'error':message or messages[reason]})
        if not any(row['status'] == 'saved' for row in results):
            read_errors = {row['error'] for row in results if row['reason'] == 'read_failed'}
            if read_errors:
                message = read_errors.pop() if len(read_errors)==1 else str(ExportReadFailed())
                raise BatchExportFailed(503,message,results)
            if any(row['reason'] == 'batch_limit' for row in results):
                raise BatchExportFailed(422,'没有动作可加入批次。超出 64 MiB 批量大小限制的动作，请尝试单独下载。',results)
            raise BatchExportFailed(400,'没有可导出的动作，请查看失败明细。',results)
        archive.writestr('export-report.json',json.dumps({'format':format,'bvh_units':'centimetres','results':results},ensure_ascii=False,indent=2))
    return output.getvalue()
