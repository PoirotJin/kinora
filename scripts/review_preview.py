"""Bounded preview cache and float32 transport; no persisted pickle cache."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import struct
import subprocess
import threading
import numpy as np

VERSION = 'world-f32-direction-v1'


def world_coordinates(joints, translation, root):
    joints = np.ascontiguousarray(joints, dtype='<f4')
    translation = np.ascontiguousarray(translation, dtype='<f4')
    root = np.ascontiguousarray(root, dtype='<f4')
    frames, count, axes = joints.shape
    if not 1 <= frames <= 3600 or not 1 <= count <= 128 or axes != 3 or translation.shape != (frames, 3) or root.shape != (3,):
        raise ValueError('不支持的动作格式')
    binary = os.environ.get('MOTIONFARM_PREVIEW_PACKER', '')
    if binary and frames <= 2000:
        payload = b'MFRAW001' + struct.pack('<II', frames, count) + root.tobytes() + joints.tobytes() + translation.tobytes()
        try:
            result = subprocess.run([binary], input=payload, capture_output=True, timeout=5, check=True)
        except FileNotFoundError:
            # A missing optional helper can safely use the numerical reference.
            result = None
        except subprocess.CalledProcessError as error:
            raise ValueError('动作预览包含无效数据') from error
        except subprocess.TimeoutExpired as error:
            raise OSError('动作预览转换超时') from error
        if result is not None:
            if len(result.stdout) != joints.size * 4:
                raise ValueError('预览转换结果长度错误')
            world = np.frombuffer(result.stdout, dtype='<f4').reshape(joints.shape)
            if not np.isfinite(world).all():
                raise ValueError('动作包含无效数值')
            return world, 'rust'
    world = joints - joints[:, :1, :] + root + translation[:, None, :]
    if not np.isfinite(world).all():
        raise ValueError('动作包含无效数值')
    return world, 'numpy'


def encode_binary(meta, world):
    header = json.dumps(meta, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    header += b' ' * (-len(header) % 4)
    return b'MFPRV001' + struct.pack('<I', len(header)) + header + np.asarray(world, dtype='<f4').tobytes()


class PreviewCache:
    def __init__(self, assets, max_bytes=64 * 1024 * 1024):
        self.assets = Path(assets)
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()
        self.skeleton = None

    def _legacy_skeleton(self):
        if self.skeleton is None:
            parents = np.fromfile(self.assets / 'kintree.bin', dtype=np.int32)
            template = np.fromfile(self.assets / 'j_template.bin', dtype=np.float32).reshape(-1, 3)
            names = json.loads((self.assets / 'joint_names.json').read_text())
            if not 1 <= len(parents) <= 128 or len(template) != len(parents) or len(names) != len(parents):
                raise ValueError('无效骨架配置')
            self.skeleton = parents, template, names
        return self.skeleton

    def get(self, key, motion_id, loader):
        # One cold conversion at a time bounds CPU, allocations, and duplicate IO.
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key][0]
            record = loader()
            data = record['motion']
            joints = np.asarray(data['native_keypoints3d'], dtype=np.float32)
            translation = np.asarray(data['native_transl'], dtype=np.float32)
            fps = float(data['fps'])
            if joints.ndim != 3 or not 1 <= len(joints) <= 3600 or not 1 <= fps <= 240:
                raise ValueError('不支持的动作格式')
            if 'scene_spec_json' in data or 'rest_joints' in data:
                from g2h.human_rig import definition, motion_rig
                template, parents = motion_rig(data)
                _, _, names = definition()
            else:
                parents, template, names = self._legacy_skeleton()
            if joints.shape[1:] != (len(parents), 3):
                raise ValueError('不支持的动作格式')
            world, implementation = world_coordinates(joints, translation, template[0])
            from g2h.motion_semantics import for_record
            quality = dict(record.get('quality', {}))
            quality['direction_semantics'] = for_record(record)
            meta = dict(motion_id=motion_id, created_utc=record.get('created_utc'), fps=fps,
                        frames=len(world), joint_count=len(parents), parents=parents.tolist(), names=names,
                        quality=quality, coordinates='Y up, metres; original world trajectory',
                        duration_s=len(world) / fps, preview_version=VERSION, preview_backend=implementation)
            wire = encode_binary(meta, world)
            entry = (meta, world, wire)
            cost = world.nbytes + len(wire)
            while self.entries and (self.bytes + cost > self.max_bytes or len(self.entries) >= 96):
                _, (_, old_cost) = self.entries.popitem(last=False)
                self.bytes -= old_cost
            if cost <= self.max_bytes:
                self.entries[key] = entry, cost
                self.bytes += cost
            return entry
