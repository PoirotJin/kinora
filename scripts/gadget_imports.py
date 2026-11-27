"""Bounded numeric SMPL-H NPZ uploads; never unpickle untrusted files."""
from contextlib import closing, suppress
import fcntl
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import zipfile
import zlib
from g2h.cold_storage import sync_dir
from review_access import private_file,Forbidden,active_account

MAX_BYTES=4*1024*1024
FIELDS={'fps','native_keypoints3d','native_transl','smpl_poses'}


class ImportedSourceCorrupt(ValueError):
    pass


def verified_source_bytes(path, size, digest):
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ImportedSourceCorrupt('上传动作校验失败')
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError('无法读取上传动作文件')
    with path.open('rb') as source:
        raw = source.read(size+1)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest:
        raise ImportedSourceCorrupt('上传动作校验失败')
    return raw

def write_source(path, raw):
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        sync_dir(path.parent)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)

def validate_npz(raw):
    try:
        return _read_npz(raw)
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError, lzma.LZMAError, zlib.error) as error:
        raise ValueError('NPZ 压缩文件不完整或无法读取，请重新导出后重试。') from error


def _read_npz(raw):
    import numpy as np
    if not 0<len(raw)<=MAX_BYTES:raise ValueError('NPZ最大4 MiB')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members=archive.infolist()
        if len(members)!=4 or {m.filename for m in members}!={k+'.npy' for k in FIELDS}:raise ValueError('NPZ仅允许规定的4个数值字段')
        if any(member.flag_bits & 1 for member in members):raise ValueError('NPZ 压缩文件不能加密。')
        if sum(m.file_size for m in members)>MAX_BYTES:raise ValueError('解压后NPZ超出限制')
        shapes={}
        for member in members:
            with archive.open(member) as stream:
                version=np.lib.format.read_magic(stream)
                if version==(1,0):shape,order,dtype=np.lib.format.read_array_header_1_0(stream)
                elif version==(2,0):shape,order,dtype=np.lib.format.read_array_header_2_0(stream)
                else:raise ValueError('不支持的NPY版本')
                if dtype.kind not in 'fi' or dtype.itemsize>8 or len(shape)>3:raise ValueError('仅接受数值数组')
                size=1
                for n in shape:
                    if not 1<=n<=720:raise ValueError('无效数组维度')
                    size*=n
                if size*dtype.itemsize>MAX_BYTES or size*dtype.itemsize!=member.file_size-stream.tell():raise ValueError('数组头与数据大小不一致')
                shapes[member.filename[:-4]]=shape
        frames=shapes['native_keypoints3d'][0] if shapes['native_keypoints3d'] else 0
        if not 2<=frames<=720 or shapes['native_keypoints3d']!=(frames,52,3) or shapes['native_transl']!=(frames,3) or shapes['smpl_poses'] not in ((frames,156),(frames,52,3)) or shapes['fps'] not in ((),(1,)):raise ValueError('SMPL-H字段形状不符合要求')
    with np.load(io.BytesIO(raw),allow_pickle=False) as data:
        arrays={name:np.array(data[name],copy=True) for name in FIELDS}
    if not all(np.isfinite(a).all() for a in arrays.values()):raise ValueError('数组包含NaN/Infinity')
    if float(arrays['fps'].reshape(-1)[0])!=30:raise ValueError('当前重定向器要求30 FPS，请先在本地转换')
    if max(float(np.abs(arrays[k]).max()) for k in FIELDS)>100000:raise ValueError('数值范围异常')
    return arrays

class Imports:
    def __init__(self,root):
        self.root=Path(root);self.folder=self.root/'review/gadget/imports';self.path=private_file(self.folder/'index.sqlite3')
        with closing(self.db()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,owner TEXT,sha256 TEXT,text TEXT,created REAL,bytes INTEGER)');db.commit()
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row;return db
    def get(self,mid,owner=None):
        if not isinstance(mid,str) or not re.fullmatch('mimp[a-f0-9]{32}',mid):raise ValueError('无效上传动作ID')
        with closing(self.db()) as db:row=db.execute('SELECT * FROM imports WHERE id=?',(mid,)).fetchone()
        if row is None or (owner is not None and row['owner']!=owner):raise Forbidden('上传动作不存在或不属于当前账户')
        return dict(row)
    def save(self,owner,key,text,raw):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('import id需要32位十六进制')
        if not isinstance(text,str) or not 1<=len(text)<=2000:raise ValueError('上传动作描述需要1–2000字符')
        role=active_account(self.root,owner);mid='mimp'+key;digest=hashlib.sha256(raw).hexdigest()
        destination=self.folder/(mid+'.npz')
        from g2h.capacity import reservation
        from g2h import resource_quota

        # Preserve ownership of the source path even if SQLite rolls back the
        # transaction itself before the file has been cleaned up.
        with (self.folder/'.save.lock').open('a') as lock, closing(self.db()) as db, db:
            fcntl.flock(lock,fcntl.LOCK_EX)
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM imports WHERE id=?', (mid,)).fetchone()
            if old:
                if (old['owner'],old['sha256'],old['text'])!=(owner,digest,text):raise ValueError('上传ID已被不同内容使用')
                self.restore_source(destination, raw)
                return {'motion_id':mid,'sha256':digest}
            validate_npz(raw)
            if db.execute('SELECT count(*) FROM imports WHERE owner=?',(owner,)).fetchone()[0]>=500:raise ValueError('当前每用户最多500个上传动作')
            ticket = 'import:' + secrets.token_hex(16)
            charged = 0
            resource_quota.reserve(self.root,ticket,owner,{'storage_bytes':len(raw)},role)
            try:
                with reservation(self.root, len(raw), 'gadget-import:' + mid) as capacity:
                    try:
                        write_source(destination,raw)
                        charged=len(raw)
                        db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?)',
                                   (mid,owner,digest,text,time.time(),len(raw)))
                        db.commit()
                    except (OSError, sqlite3.Error):
                        unpublished = db.in_transaction
                        if not unpublished:
                            # SQLite can end the failed transaction itself. Reacquire
                            # the writer lock before checking whether the file is in use.
                            db.execute('BEGIN IMMEDIATE')
                            unpublished = db.execute('SELECT 1 FROM imports WHERE id=?',(mid,)).fetchone() is None
                        if unpublished:
                            destination.unlink(missing_ok=True)
                            charged=0
                            with suppress(OSError):
                                sync_dir(destination.parent)
                        raise
                    capacity.commit(charged)
            finally:
                resource_quota.settle(self.root,ticket,{'storage_bytes':charged})
        return {'motion_id':mid,'sha256':digest}

    def restore_source(self, destination, raw):
        from g2h.capacity import reservation

        try:
            old_size = destination.stat().st_size
            matches = old_size == len(raw) and destination.read_bytes() == raw
        except FileNotFoundError:
            old_size = 0
        else:
            if matches:
                # A prior repair may have failed after rename but before syncing.
                sync_dir(destination.parent)
                return
        with reservation(self.root, len(raw), 'gadget-import-restore:' + destination.stem) as capacity:
            write_source(destination, raw)
            capacity.commit(len(raw) - old_size)

    def source(self,mid,owner):
        return self.get(mid,owner), self.folder/(mid+'.npz')

    def read(self,mid,owner):
        row,path=self.source(mid,owner)
        raw=verified_source_bytes(path,row['bytes'],row['sha256'])
        return dict(text=row['text'],text_id=None,motion_id=mid,motion=validate_npz(raw))
