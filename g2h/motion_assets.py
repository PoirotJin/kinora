"""Immutable, account-scoped motion versions and exact temporal patch insertion.

Only numeric NPZ files are loaded. Existing HY SMPL-H four-field imports remain
valid. Optional rotation/translation aliases are checked rather than ignored.
"""
from contextlib import contextmanager, nullcontext
import hashlib
from io import BytesIO
from itertools import permutations
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import uuid
import zipfile

import numpy as np

REPRESENTATION = 'human_smplh_y_up_v1'
VERSION = 'motion-assets-v1'
REQUIRED = {'fps', 'native_keypoints3d', 'native_transl', 'smpl_poses'}
STATIC = {'fps', 'smpl_mocap_framerate', 'smpl_num_frames', 'smpl_betas',
          'parents', 'rest_joints', 'scene_spec_json'}
MAX_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 18000
ID_PATTERN = re.compile(r'^ma_[0-9a-f]{32}$')


class MotionArtifactCorrupt(ValueError):
    pass


class SavedMotionUnavailable(OSError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('An authenticated owner is required')
    return value


def _same(a, b):
    return (a.dtype == b.dtype and a.shape == b.shape
            and np.ascontiguousarray(a).tobytes() == np.ascontiguousarray(b).tobytes())


def _rotations(rotvec):
    angles = np.linalg.norm(rotvec, axis=-1)
    axes = rotvec / np.maximum(angles[..., None], 1e-30)
    x, y, z = np.moveaxis(axes, -1, 0)
    zero = np.zeros_like(x)
    skew = np.stack((zero, -z, y, z, zero, -x, -y, x, zero), axis=-1).reshape(angles.shape + (3, 3))
    eye = np.eye(3)
    return eye + np.sin(angles)[..., None, None] * skew + (1 - np.cos(angles))[..., None, None] * (skew @ skew)


def validate_motion(arrays):
    if not isinstance(arrays, dict) or not REQUIRED <= set(arrays):
        raise ValueError('Motion requires fps, native_keypoints3d, native_transl and smpl_poses')
    if len(arrays) > 64:
        raise ValueError('Too many motion fields')
    checked = {}
    size = 0
    for key, value in arrays.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}', key):
            raise ValueError('Invalid motion field name')
        value = np.asarray(value)
        if value.dtype.kind not in 'biuf' or value.dtype.hasobject or value.dtype.itemsize > 8:
            raise ValueError('Motion fields must be real numeric arrays')
        size += value.nbytes
        if size > MAX_BYTES or value.ndim > 5 or not np.isfinite(value).all():
            raise ValueError('Motion has non-finite values or exceeds bounds')
        checked[key] = np.array(value, copy=True)
    fps = checked['fps']
    if fps.shape not in ((), (1,)) or float(fps.reshape(-1)[0]) != 30:
        raise ValueError('The canonical representation requires 30 FPS')
    points = checked['native_keypoints3d']
    if points.ndim != 3 or points.shape[1:] != (52, 3) or not 2 <= len(points) <= MAX_FRAMES:
        raise ValueError('Expected 2..18000 SMPL-H frames with 52 joints')
    frames = len(points)
    if 'scene_spec_json' in checked:
        from .human_interaction import decode
        decode(checked['scene_spec_json'], frames)
    if points.dtype.kind != 'f' or checked['native_transl'].dtype.kind != 'f' or checked['smpl_poses'].dtype.kind != 'f':
        raise ValueError('Canonical positions, translations and poses must be floating point')
    if checked['native_transl'].shape != (frames, 3) or checked['smpl_poses'].shape not in ((frames, 156), (frames, 52, 3)):
        raise ValueError('Translation or pose shape does not match the motion')
    poses = checked['smpl_poses'].reshape(frames, 52, 3)
    optional_shapes = {'native_root_rotations_mat': (frames, 3, 3),
                       'native_rot6d': (frames, 52, 6), 'smpl_trans': (frames, 3),
                       'smpl_Rh': (frames, 3), 'parents': (52,), 'rest_joints': (52, 3)}
    for key, shape in optional_shapes.items():
        if key in checked and checked[key].shape != shape:
            raise ValueError('Invalid optional field shape: ' + key)
    if 'smpl_trans' in checked and not np.allclose(checked['smpl_trans'], checked['native_transl'], atol=1e-6, rtol=0):
        raise ValueError('Translation aliases disagree')
    if 'smpl_Rh' in checked and not np.allclose(checked['smpl_Rh'], poses[:, 0], atol=1e-6, rtol=0):
        raise ValueError('Root rotation aliases disagree')
    matrices = None
    if 'native_root_rotations_mat' in checked or 'native_rot6d' in checked:
        matrices = _rotations(poses.astype(np.float64))
    if 'native_root_rotations_mat' in checked and not np.allclose(checked['native_root_rotations_mat'], matrices[:, 0], atol=2e-3, rtol=0):
        raise ValueError('Root matrix does not match SMPL root rotation')
    if 'native_rot6d' in checked and not np.allclose(checked['native_rot6d'], matrices[..., :2].reshape(frames, 52, 6), atol=2e-3, rtol=0):
        raise ValueError('6D rotation fields do not match SMPL poses')
    if 'smpl_betas' in checked and checked['smpl_betas'].shape not in ((16,), (1, 16), (10,), (1, 10)):
        raise ValueError('Shape parameters must be static')
    for key in ('smpl_mocap_framerate', 'smpl_num_frames'):
        if key in checked:
            expected = 30 if key.endswith('framerate') else frames
            if checked[key].shape not in ((), (1,)) or float(checked[key].reshape(-1)[0]) != expected:
                raise ValueError('Invalid metadata: ' + key)
    if 'parents' in checked:
        parents = checked['parents']
        if parents.dtype.kind not in 'iu' or parents[0] != -1 or any(not 0 <= parents[i] < i for i in range(1, 52)):
            raise ValueError('Invalid joint hierarchy')
    if 'parents' in checked and 'rest_joints' in checked:
        # A four-field legacy payload lacks a rest rig: shape/alias integrity
        # cannot prove its joint positions agree with its SMPL rotations.
        local = matrices if matrices is not None else _rotations(poses.astype(np.float64))
        rest = checked['rest_joints'].astype(np.float64)
        parents = checked['parents']
        global_rot = np.empty_like(local)
        expected = np.empty_like(points, dtype=np.float64)
        global_rot[:, 0] = local[:, 0]
        expected[:, 0] = rest[0]
        for i in range(1, 52):
            parent = int(parents[i])
            global_rot[:, i] = global_rot[:, parent] @ local[:, i]
            expected[:, i] = expected[:, parent] + np.einsum('tij,j->ti', global_rot[:, parent], rest[i] - rest[parent])
        if not np.allclose(points - points[:, :1], expected - expected[:, :1], atol=5e-3, rtol=0):
            raise ValueError('Joint positions do not match rotations and the supplied rest rig')
    return checked


def load_numeric_npz(path):
    try:
        return _load_numeric_npz(path)
    except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Motion is not a supported numeric NPZ archive') from exc


def _load_numeric_npz(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Motion file is missing or too large')
    # Bound uncompressed sizes before NumPy allocates any arrays.
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 64 or sum(x.file_size for x in entries) > MAX_BYTES:
            raise ValueError('Motion archive exceeds limits')
        if any(x.flag_bits & 1 or x.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for x in entries):
            raise ValueError('Unsupported motion archive encoding')
        if any(x.is_dir() or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}\.npy', x.filename) for x in entries):
            raise ValueError('Invalid motion archive entry')
        if len({x.filename for x in entries}) != len(entries):
            raise ValueError('Duplicate motion archive fields')
        for entry in entries:
            with archive.open(entry) as stream:
                version = np.lib.format.read_magic(stream)
                if version == (1, 0):
                    shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                elif version == (2, 0):
                    shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                else:
                    raise ValueError('Unsupported NPY header version')
                if dtype.kind not in 'biuf' or dtype.hasobject or dtype.itemsize > 8 or len(shape) > 5:
                    raise ValueError('Motion arrays must contain bounded real numeric values')
                elements = math.prod(shape)
                if any(type(n) is not int or n < 0 for n in shape) or elements * dtype.itemsize > MAX_BYTES:
                    raise ValueError('Motion header exceeds array allocation bounds')
                if elements * dtype.itemsize != entry.file_size - stream.tell():
                    raise ValueError('Motion header does not match its actual payload size')
    try:
        with np.load(path, allow_pickle=False) as data:
            arrays = {key: data[key] for key in data.files}
    except (TypeError, ValueError, EOFError) as exc:
        raise ValueError('Motion is not a safe numeric NPZ') from exc
    return validate_motion(arrays)


def _digest(arrays):
    result = hashlib.sha256()
    for key in sorted(arrays):
        value = arrays[key]
        result.update(_json([key, value.dtype.str, value.shape]).encode())
        result.update(np.ascontiguousarray(value).tobytes())
    return result.hexdigest()


def selection_bounds(start_frame, end_frame, frames):
    if type(start_frame) is not int or type(end_frame) is not int or not 0 <= start_frame < end_frame <= frames:
        raise ValueError('Selection must be a non-empty half-open frame interval')
    return start_frame, end_frame


def _legacy_npz_order(raw, expected_sha256):
    """Reproduce the four field orders used by legacy snapshot materialization."""
    end_size = 22
    end_start = len(raw) - end_size
    if end_start < 0 or raw[end_start:end_start+4] != b'PK\x05\x06':
        return None
    _, disk, directory_disk, disk_count, count, size, start, comment = struct.unpack_from('<4s4H2IH', raw, end_start)
    if disk or directory_disk or comment or count != len(REQUIRED) or disk_count != count or start + size != end_start:
        return None
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
    if len(entries) != count or {entry.filename for entry in entries} != {key+'.npy' for key in REQUIRED}:
        return None
    offsets = [entry.header_offset for entry in entries] + [start]
    if offsets[0] != 0 or any(left >= right for left, right in zip(offsets, offsets[1:])):
        return None

    # NumPy writes the same compressed member bytes for each field order. Move
    # the local records and rewrite their offsets in the central directory.
    data = memoryview(raw)
    members = []
    position = start
    for left, right in zip(offsets, offsets[1:]):
        if position + 46 > end_start or raw[position:position+4] != b'PK\x01\x02':
            return None
        name_size, extra_size, comment_size = struct.unpack_from('<3H', raw, position+28)
        end = position + 46 + name_size + extra_size + comment_size
        if end > end_start:
            return None
        members.append((data[left:right], data[position:position+42], data[position+46:end]))
        position = end
    if position != end_start:
        return None

    for order in permutations(members):
        offset = 0
        headers = []
        digest = hashlib.sha256()
        for record, prefix, suffix in order:
            digest.update(record)
            headers.extend((prefix, struct.pack('<I', offset), suffix))
            offset += len(record)
        for chunk in headers:
            digest.update(chunk)
        digest.update(data[end_start:])
        if digest.hexdigest() == expected_sha256:
            return b''.join([*(member[0] for member in order), *headers, data[end_start:]])
    return None


class MotionAssets:
    def __init__(self, root):
        self.root = Path(root).resolve() / 'harness_assets'
        if self.root.is_symlink():
            raise ValueError('Asset directory cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / 'objects'
        self.objects.mkdir(exist_ok=True)
        if self.objects.is_symlink():
            raise ValueError('Asset directory cannot be a symlink')
        self.db_path = self.root / 'assets.sqlite3'
        if self.db_path.is_symlink():
            raise ValueError('Asset database cannot be a symlink')
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT,
                request_hash TEXT NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(owner, request_id));
                CREATE INDEX IF NOT EXISTS assets_owner ON assets(owner, id);
                CREATE INDEX IF NOT EXISTS assets_owner_order ON assets(owner);''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _folder(self, owner, motion_id):
        _owner(owner)
        if not isinstance(motion_id, str) or not ID_PATTERN.fullmatch(motion_id):
            raise ValueError('Invalid managed motion ID')
        bucket = hashlib.sha256(owner.encode()).hexdigest()
        parent = self.objects / bucket
        if parent.is_symlink():
            raise ValueError('Invalid owner asset directory')
        folder = parent / motion_id
        if folder.is_symlink() or folder.resolve().parent != parent.resolve():
            raise ValueError('Invalid asset directory')
        return folder

    def get(self, owner, motion_id):
        self._folder(owner, motion_id)
        with self._db() as db:
            row = db.execute('SELECT metadata FROM assets WHERE owner=? AND id=?', (owner, motion_id)).fetchone()
        if row is None:
            raise KeyError('Motion not found')
        return json.loads(row['metadata'])

    def find_request(self, owner, request_id):
        """Look up an immutable upload before applying today's admission limits."""
        _owner(owner)
        if not isinstance(request_id, str):
            raise ValueError('Invalid request ID')
        with self._db() as db:
            row = db.execute('SELECT metadata FROM assets WHERE owner=? AND request_id=?',
                             (owner, request_id)).fetchone()
        return json.loads(row['metadata']) if row else None

    def list(self, owner, limit=50):
        _owner(owner)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid motion list limit')
        with self._db() as db:
            return [json.loads(x['metadata']) for x in db.execute('SELECT metadata FROM assets WHERE owner=? ORDER BY rowid DESC LIMIT ?', (owner, limit))]

    def list_before(self, owner, before, limit=51):
        _owner(owner)
        if type(before) is not int or not 0 < before < 2**63 or not 1 <= limit <= 100:
            raise ValueError('Invalid motion page')
        with self._db() as db:
            rows = db.execute('SELECT rowid AS seq,metadata FROM assets WHERE owner=? AND rowid<? '
                              'ORDER BY rowid DESC LIMIT ?', (owner, before, limit)).fetchall()
        return [(row['seq'], json.loads(row['metadata'])) for row in rows]

    def artifact_path(self, owner, motion_id):
        metadata = self.get(owner, motion_id)
        path = self._folder(owner, motion_id) / 'motion.npz'
        if path.is_symlink():
            raise ValueError('Stored motion integrity check failed')
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
            raise MotionArtifactCorrupt('Stored motion integrity check failed')
        return path

    def artifact_size(self, owner, motion_id):
        self.get(owner, motion_id)
        path = self._folder(owner, motion_id) / 'motion.npz'
        if path.is_symlink():
            raise ValueError('Invalid stored motion path')
        return path.stat().st_size

    def restore_missing_artifact(self, owner, motion_id, raw):
        """Restore the exact bytes of an existing version without replacing a file."""
        return self._restore_artifact(owner, motion_id, raw, replace_corrupt=False)

    def restore_artifact_from_arrays(self, owner, motion_id, arrays):
        """Restore an immutable payload only if reconstruction matches its stored checksum."""
        output = BytesIO()
        np.savez_compressed(output, **validate_motion(arrays))
        return self._restore_artifact(owner, motion_id, output.getvalue(), replace_corrupt=True)

    def _restore_artifact(self, owner, motion_id, raw, *, replace_corrupt):
        from g2h.capacity import SNAPSHOT_RESTORE_PURPOSE, SNAPSHOT_RESTORE_TEMP, reservation

        metadata = self.get(owner, motion_id)
        if hashlib.sha256(raw).hexdigest() != metadata['sha256']:
            try:
                raw = _legacy_npz_order(raw, metadata['sha256'])
            except zipfile.BadZipFile:
                raw = None
            if raw is None:
                raise ValueError('Stored motion integrity check failed')
        path = self._folder(owner, motion_id) / 'motion.npz'
        with reservation(self.root.parent, len(raw), SNAPSHOT_RESTORE_PURPOSE+motion_id) as capacity:
            staged = path.parent/(SNAPSHOT_RESTORE_TEMP+capacity.key)
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'wb') as temporary:
                    temporary.write(raw)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                try:
                    os.link(staged, path)
                except FileExistsError:
                    try:
                        self.artifact_path(owner, motion_id)
                    except MotionArtifactCorrupt:
                        if not replace_corrupt:
                            raise
                        replaced_bytes = path.stat().st_size
                        os.replace(staged, path)
                    else:
                        staged.unlink()
                        capacity.commit(0)
                        return path
                else:
                    replaced_bytes = 0
                    staged.unlink()
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                capacity.commit(len(raw)-replaced_bytes)
            finally:
                staged.unlink(missing_ok=True)
        return path

    def load(self, owner, motion_id):
        metadata = self.get(owner, motion_id)
        return load_numeric_npz(self.artifact_path(owner, motion_id)), metadata

    def import_npz(self, owner, path, **metadata):
        """Internal bridge only: caller must authorize the source path separately."""
        return self.register(owner, load_numeric_npz(path), **metadata)

    def register(self, owner, arrays, *, description='', parent_id=None, selection=None,
                 request_id=None, provenance=None, publish_guard=nullcontext):
        from g2h.capacity import PENDING_MOTION_ASSET

        _owner(owner)
        arrays = validate_motion(arrays)
        if not isinstance(description, str) or len(description) > 8000:
            raise ValueError('Invalid motion description')
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise ValueError('Invalid request ID')
        parent = self.get(owner, parent_id) if parent_id else None
        if selection is not None:
            if parent is None or not isinstance(selection, dict) or set(selection) != {'start_frame', 'end_frame'}:
                raise ValueError('A selection requires its original parent version')
            selection_bounds(selection['start_frame'], selection['end_frame'], parent['frames'])
        content_hash = _digest(arrays)
        request = {'content': content_hash, 'description': description, 'parent_id': parent_id,
                   'selection': selection, 'provenance': provenance or {}}
        request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                row = db.execute('SELECT * FROM assets WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
                if row:
                    if row['request_hash'] != request_hash:
                        raise ValueError('Request ID was already used with different motion arguments')
                    return json.loads(row['metadata'])
            motion_id = 'ma_' + uuid.uuid4().hex
            destination = self._folder(owner, motion_id)
            destination.parent.mkdir(exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='.writing-', dir=destination.parent))
            try:
                (temporary/PENDING_MOTION_ASSET).touch()
                payload = temporary / 'motion.npz'
                with payload.open('wb') as handle:
                    np.savez_compressed(handle, **arrays)
                    handle.flush()
                    os.fsync(handle.fileno())
                    artifact_bytes = os.fstat(handle.fileno()).st_size
                metadata = dict(id=motion_id, motion_id=motion_id, representation=REPRESENTATION,
                                version=VERSION, frames=len(arrays['native_transl']), fps=30,
                                sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
                                artifact_bytes=artifact_bytes,
                                content_sha256=content_hash, description=description,
                                parent_id=parent_id, parent_sha256=parent['sha256'] if parent else None,
                                selection=selection, created_at=time.time(), provenance=provenance or {},
                                geometry_status='unknown', semantic_status='unknown', dynamics_status='unknown',
                                joint_pose_consistency_status='passed' if {'parents', 'rest_joints'} <= set(arrays) else 'unknown')
                with (temporary/'metadata.json').open('w') as handle:
                    handle.write(_json(metadata) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                descriptor = os.open(temporary, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with publish_guard():
                    os.replace(temporary, destination)
                    descriptor = os.open(destination.parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    db.execute('INSERT INTO assets VALUES(?,?,?,?,?)',
                               (motion_id, owner, request_id, request_hash, _json(metadata)))
                    db.commit()
                try:
                    (destination/PENDING_MOTION_ASSET).unlink(missing_ok=True)
                except OSError:
                    pass  # Reconciliation clears the marker for committed versions.
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return metadata

    def context(self, owner, motion_id, start_frame, end_frame, padding=15):
        arrays, metadata = self.load(owner, motion_id)
        selection_bounds(start_frame, end_frame, metadata['frames'])
        if type(padding) is not int or not 0 <= padding <= 300:
            raise ValueError('Invalid context padding')
        first, last = max(0, start_frame - padding), min(metadata['frames'], end_frame + padding)
        context = {}
        for key, value in arrays.items():
            if key not in STATIC and value.ndim and value.shape[0] == metadata['frames']:
                value = value[first:last].copy()
            else:
                value = value.copy()
            value.setflags(write=False)
            context[key] = value
        return {'motion': metadata, 'arrays': context, 'window_start_frame': first,
                'window_end_frame': last, 'selection': {'start_frame': start_frame, 'end_frame': end_frame},
                'read_only': True}

    def apply_patch(self, owner, parent_id, start_frame, end_frame, patch, *,
                    expected_sha256, description='', request_id=None, provenance=None, publish_guard=nullcontext):
        arrays, parent = self.load(owner, parent_id)
        if expected_sha256 != parent['sha256']:
            raise ValueError('Parent hash changed; edit must target the exact original version')
        start_frame, end_frame = selection_bounds(start_frame, end_frame, parent['frames'])
        if not isinstance(patch, dict) or set(patch) != set(arrays):
            raise ValueError('Edited patch must preserve every motion field')
        result = {}
        for key, original in arrays.items():
            edited = np.asarray(patch[key])
            if edited.dtype != original.dtype:
                raise ValueError('Edited patch changed array dtype: ' + key)
            per_frame = key not in STATIC and original.ndim and original.shape[0] == parent['frames']
            if not per_frame:
                if not _same(original, edited):
                    raise ValueError('Edited patch changed static metadata: ' + key)
                result[key] = original.copy()
                continue
            if edited.shape == original.shape:
                if not _same(original[:start_frame], edited[:start_frame]) or not _same(original[end_frame:], edited[end_frame:]):
                    raise ValueError('Edited candidate changed frames outside the selection: ' + key)
                inside = edited[start_frame:end_frame]
            elif edited.shape == (end_frame - start_frame,) + original.shape[1:]:
                inside = edited
            else:
                raise ValueError('Edited patch has an incompatible shape: ' + key)
            result[key] = original.copy()
            result[key][start_frame:end_frame] = inside
            assert _same(result[key][:start_frame], original[:start_frame])
            assert _same(result[key][end_frame:], original[end_frame:])
        return self.register(owner, result, description=description, parent_id=parent_id,
                             selection={'start_frame': start_frame, 'end_frame': end_frame},
                             request_id=request_id, provenance=provenance, publish_guard=publish_guard)
