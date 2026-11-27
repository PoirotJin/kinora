"""Account-owned library assets and immutable collection manifests."""
from contextlib import closing, contextmanager
import base64
import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
import zlib

from review_access import Forbidden, require_motion_access
from review_collections import CollectionMembershipChanged, membership_basis
from review_harness import fields

BASE = '/api/harness/personal-assets'
ID = r'[0-9a-f]{32}'
BATCH_SIZE = 20
BATCH_BYTES = 64 * 1024 * 1024
UPLOAD_BYTES = 8 * 1024 * 1024
SNAPSHOT_STORAGE_ERROR = '暂时无法读取或写入快照文件，请稍后重试。'
_SNAPSHOT_LOCKS_GUARD = threading.Lock()
_SNAPSHOT_LOCKS = {}


@contextmanager
def _snapshot_lock(root, owner):
    key = (str(root), owner)
    with _SNAPSHOT_LOCKS_GUARD:
        lock = _SNAPSHOT_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


class SnapshotMemberUnavailable(ValueError):
    def __init__(self, motion_id):
        super().__init__(f'动作 {motion_id} 不可用于快照。请恢复该动作，或将可用动作加入新集合。')



class SnapshotVersionUnavailable(ValueError):
    def __init__(self, motion_id, reason):
        super().__init__(f'快照中的动作 {motion_id} {reason}，无法下载本批次。')


class SnapshotRestoreUnavailable(ValueError):
    def __init__(self, motion_id):
        super().__init__(f'无法恢复快照动作 {motion_id} 的原始版本。')


def label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise ValueError('名称需要 1–120 个字符')
    return value.strip()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode()


def upload(value):
    if not isinstance(value, str) or len(value) > 4 * ((UPLOAD_BYTES + 2) // 3):
        raise ValueError('上传文件超过 8 MiB')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('上传内容无效') from exc
    if not 0 < len(raw) <= UPLOAD_BYTES:
        raise ValueError('上传文件必须在 8 MiB 以内')
    return raw


class Library:
    def __init__(self, service, user):
        self.service = service
        self.user = user
        self.owner = service.owner(user)
        self.store = service.legacy
        self.root = self.store.root / 'review' / 'personal-assets' / hashlib.sha256(self.owner.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.store.reviews()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS personal_asset_versions(
                    seq INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    id TEXT NOT NULL UNIQUE, family TEXT NOT NULL,
                    kind TEXT NOT NULL, name TEXT NOT NULL, created REAL NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_asset_list
                    ON personal_asset_versions(owner, kind, seq DESC);
                CREATE INDEX IF NOT EXISTS personal_asset_family
                    ON personal_asset_versions(owner, family, seq DESC);
                CREATE TABLE IF NOT EXISTS personal_asset_save_requests(
                    owner TEXT NOT NULL, kind TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY(owner, kind, request_id));
                CREATE TABLE IF NOT EXISTS personal_asset_snapshots(
                    seq INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    id TEXT NOT NULL UNIQUE, collection_id TEXT NOT NULL,
                    collection_name TEXT NOT NULL, name TEXT NOT NULL,
                    created REAL NOT NULL, count INTEGER NOT NULL, sha256 TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_snapshot_list
                    ON personal_asset_snapshots(owner, seq DESC);
                CREATE INDEX IF NOT EXISTS personal_snapshot_collection
                    ON personal_asset_snapshots(owner, collection_id, seq DESC);
            """)
            from personal_snapshot_jobs import setup
            setup(db)

    @contextmanager
    def write_lock(self, name):
        with (self.root / name).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def asset(self, asset_id, kind=None):
        if not isinstance(asset_id, str) or re.fullmatch(ID,asset_id) is None:
            raise ValueError('资产编号无效')
        with closing(self.store.reviews()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM personal_asset_versions WHERE owner=? AND id=?',
                             (self.owner, asset_id)).fetchone()
        if row is None or (kind is not None and row['kind'] != kind):
            raise Forbidden('资产不存在或不可访问')
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def asset_for_save_request(self, kind, request_id, fingerprint):
        with closing(self.store.reviews()) as db:
            row = db.execute('SELECT fingerprint,asset_id FROM personal_asset_save_requests '
                             'WHERE owner=? AND kind=? AND request_id=?',
                             (self.owner, kind, request_id)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise ValueError('预设请求编号已被使用')
        return self.asset(row[1], kind)

    def _check_asset_head(self, db, previous):
        head = db.execute('SELECT id FROM personal_asset_versions WHERE owner=? AND family=? '
                          'ORDER BY seq DESC LIMIT 1', (self.owner, previous['family'])).fetchone()
        if head is None or head[0] != previous['id']:
            raise ValueError('资产已有新版，请刷新后再编辑')

    def require_current_asset(self, previous):
        with closing(self.store.reviews()) as db:
            self._check_asset_head(db, previous)

    def save_asset(self, kind, name, data, previous=None, *, save_request=None):
        asset_id = uuid.uuid4().hex
        previous = self.asset(previous, kind) if previous is not None else None
        family = previous['family'] if previous else asset_id
        name, created, payload = label(name), time.time(), encoded(data).decode()
        with closing(self.store.reviews()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if previous:
                self._check_asset_head(db, previous)
            seq = db.execute('INSERT INTO personal_asset_versions(owner,id,family,kind,name,created,data) '
                             'VALUES(?,?,?,?,?,?,?)',
                             (self.owner, asset_id, family, kind, name, created, payload)).lastrowid
            if save_request is not None:
                db.execute('INSERT INTO personal_asset_save_requests VALUES(?,?,?,?,?)',
                           (self.owner, kind, *save_request, asset_id))
            result = dict(seq=seq,id=asset_id,owner=self.owner,family=family,kind=kind,name=name,
                          created=created,data=json.loads(payload))
        return result

    def list_assets(self, kind, after, signature=None):
        with closing(self.store.reviews()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT a.seq,a.id,a.family,a.kind,a.name,a.created FROM personal_asset_versions a
                WHERE a.owner=? AND a.kind=? AND a.seq<? AND NOT EXISTS (
                    SELECT 1 FROM personal_asset_versions b WHERE b.owner=a.owner AND
                    b.family=a.family AND b.seq>a.seq)
                    AND (? IS NULL OR json_extract(a.data, '$.signature') = ?)
                    ORDER BY a.seq DESC LIMIT 51""",
                              (self.owner, kind, after, signature, signature)).fetchall()
        return page(rows)

    def motion(self, mid):
        meta = require_motion_access(self.store, mid, self.user)
        if mid.startswith('ma_'):
            return dict(motion_id=mid, text=meta['description'] or mid, duration=meta['frames']/meta['fps'],
                        provenance=meta.get('provenance', {}), selection=meta.get('selection'),
                        parent_id=meta.get('parent_id'), created=meta['created_at'])
        from gadget_library import detail
        return detail(self.store, mid, self.user)

    def own_motions(self, after):
        rows = self.service.assets.list_before(self.owner, after)
        return page([dict(seq=seq, motion_id=meta['motion_id'], text=meta['description'],
                          duration=meta['frames']/meta['fps']) for seq, meta in rows])

    def history(self, mid):
        items = []
        pending = [mid]
        seen = set()
        while pending and len(seen) < 50:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            try:
                motion = self.motion(current)
            except (ValueError, Forbidden, KeyError):
                if current == mid:
                    raise
                items.append(dict(motion_id=current, unavailable=True))
                continue
            provenance = motion.get('provenance', {})
            sources = provenance.get('sources') or provenance.get('segments') or []
            if provenance.get('source_motion_id'):
                sources = [*sources, dict(motion_id=provenance['source_motion_id'], role='original')]
            normalized = [dict(source) if isinstance(source, dict) else dict(motion_id=source)
                          for source in sources if isinstance(source, (str, dict))]
            if motion.get('parent_id'):
                normalized.insert(0, dict(motion_id=motion['parent_id'], role='parent',
                                          selection=motion.get('selection')))
            pending.extend(s['motion_id'] for s in normalized if isinstance(s.get('motion_id'), str))
            items.append({**motion, 'sources': normalized})
        return dict(items=items, truncated=bool(set(pending) - seen))

    def put_file(self, name, data, *, staging_prefix=None):
        target = self.root / name
        stream = tempfile.NamedTemporaryFile(dir=self.root, prefix=staging_prefix, delete=False)
        temp = Path(stream.name)
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)
        return target

    def materialize(self, mid, *, required_frame=None, _created=None):
        def check_frame(frames):
            if required_frame is not None and not 0 <= required_frame < frames:
                raise ValueError('姿态帧超出动作范围')

        if mid.startswith('ma_'):
            meta = self.service.assets.get(self.owner, mid)
            check_frame(meta['frames'])
            self.service.assets.artifact_path(self.owner, mid)
            return meta
        import numpy as np
        require_motion_access(self.store, mid, self.user)
        key = 'snapshot-source:' + mid
        assets = self.service.assets

        def existing_version():
            meta = assets.find_request(self.owner, key)
            if meta is not None:
                check_frame(meta['frames'])
                self.snapshot_artifact_path(mid, meta['id'])
            return meta

        existing = existing_version()
        if existing is not None:
            return existing
        from g2h.capacity import reservation
        from g2h import resource_quota
        from review_access import active_account
        with self.write_lock('.materialize.lock'):
            require_motion_access(self.store, mid, self.user)
            existing = existing_version()
            if existing is not None:
                return existing
            raw = self.service.download(self.user, mid)
            try:
                with np.load(io.BytesIO(raw), allow_pickle=False) as data:
                    arrays = dict(data)
                    poses = arrays['smpl_poses']
                    frame_count = len(poses)
            except (EOFError, KeyError, OSError, TypeError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
                raise ValueError(f'动作 {mid} 的源 NPZ 无法读取，请检查文件。') from exc
            check_frame(frame_count)
            ticket = 'snapshot-content:' + uuid.uuid4().hex
            role = active_account(self.store.root, self.owner)
            resource_quota.reserve(self.store.root, ticket, self.owner, {'storage_bytes': 2 * len(raw)}, role)
            charged = 0
            try:
                with reservation(self.store.root, 2 * len(raw), ticket) as capacity:
                    meta = assets.register(self.owner, arrays, description=self.motion(mid).get('text') or mid,
                                           request_id=key, provenance={'source_motion_id': mid})
                    charged = meta['artifact_bytes']
                    capacity.commit(charged)
                    if _created is not None:
                        _created.append((meta['id'], ticket, charged))
                    return meta
            finally:
                resource_quota.settle(self.store.root, ticket, {'storage_bytes': charged})

    def _rollback_materialized(self, created):
        if not created:
            return
        from g2h import capacity, resource_quota
        assets = self.service.assets
        for version_id, ticket, size in reversed(created):
            try:
                with assets._db() as db:
                    db.execute('DELETE FROM assets WHERE owner=? AND id=?', (self.owner, version_id))
            except (OSError, sqlite3.Error):
                pass
            try:
                folder = assets._folder(self.owner, version_id)
                if folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)
            except (OSError, ValueError):
                pass
            try:
                capacity.release(self.store.root, size)
            except (OSError, ValueError):
                pass
            try:
                with closing(resource_quota.connect(self.store.root)) as db, db:
                    db.execute('UPDATE reservations SET charged=0 WHERE id=?', (ticket + ':storage_bytes',))
            except (OSError, sqlite3.Error):
                pass

    def snapshot_artifact_path(self, mid, version_id):
        assets = self.service.assets
        try:
            return assets.artifact_path(self.owner, version_id)
        except FileNotFoundError:
            if mid.startswith('ma_'):
                raise
        with self.write_lock('.snapshot-restore.lock'):
            try:
                return assets.artifact_path(self.owner, version_id)
            except FileNotFoundError:
                metadata = assets.get(self.owner, version_id)
                if metadata.get('provenance', {}).get('source_motion_id') != mid:
                    raise SnapshotRestoreUnavailable(mid)
                try:
                    raw = self.service.download(self.user, mid)
                    return assets.restore_missing_artifact(self.owner, version_id, raw)
                except (ValueError, FileNotFoundError) as error:
                    raise SnapshotRestoreUnavailable(mid) from error
                except PermissionError as error:
                    raise OSError('无法恢复缺失的快照文件。') from error

    def collection_basis(self, cid):
        with closing(self.store.reviews()) as db, db:
            db.execute('BEGIN')
            return self.read_collection_basis(db, cid)

    def read_collection_basis(self, db, cid):
        """Read the collection name and membership in the caller's transaction."""
        if not isinstance(cid, str) or re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', cid) is None:
            raise ValueError('集合编号无效')
        collection = db.execute('SELECT name FROM motion_collections WHERE owner=? AND id=?',
                                (self.owner, cid)).fetchone()
        rows = db.execute('SELECT motion_id FROM collection_items WHERE owner=? AND collection_id=? '
                          'ORDER BY motion_id LIMIT 1001', (self.owner, cid)).fetchall()
        if collection is None or not rows:
            raise CollectionMembershipChanged('集合已删除或没有可固定的动作。')
        if len(rows) > 1000:
            raise ValueError('集合超过 1000 个动作，无法固定快照。')
        members = [r[0] for r in rows]
        return collection[0], members, membership_basis(members)

    def validate_snapshot_members(self, members):
        for mid in members:
            try:
                if mid.startswith('ma_'):
                    self.service.assets.artifact_size(self.owner, mid)
                elif mid.startswith('mimp'):
                    from gadget_imports import Imports
                    _, path = Imports(self.store.root).source(mid, self.owner)
                    if self.service.assets.find_request(self.owner, 'snapshot-source:'+mid) is None:
                        path.stat()
                else:
                    require_motion_access(self.store, mid, self.user)
            except (Forbidden, ValueError, KeyError, FileNotFoundError) as error:
                raise SnapshotMemberUnavailable(mid) from error
            except PermissionError as error:
                raise OSError('无法读取快照源动作') from error

    def create_snapshot(self, value, source=None):
        from g2h.capacity import SNAPSHOT_MANIFEST_TEMP

        names = ('id', 'collection_id', 'name', 'expected_basis')
        fields(value, names, names)
        sid, cid, name = value['id'], value['collection_id'], label(value['name'])
        if not isinstance(sid, str) or re.fullmatch(ID, sid) is None:
            raise ValueError('快照编号无效')
        with closing(self.store.reviews()) as db:
            old = db.execute('SELECT owner,collection_id,name FROM personal_asset_snapshots WHERE id=?', (sid,)).fetchone()
        if old:
            if tuple(old) != (self.owner, cid, name):
                raise ValueError('快照编号已被使用')
            saved = self.snapshot(sid)
            if saved['basis'] != value['expected_basis']:
                raise ValueError('快照编号已用于不同成员')
            return saved
        collection_name, members, basis = source or self.collection_basis(cid)
        if basis != value['expected_basis']:
            raise ValueError('集合成员已变化，请刷新后再固定快照')
        self.validate_snapshot_members(members)
        created_assets = []
        with _snapshot_lock(self.store.root, self.owner):
            try:
                items = []
                for mid in members:
                    try:
                        version = self.materialize(mid, _created=created_assets)
                        size = self.service.assets.artifact_size(self.owner, version['id'])
                    except (Forbidden, KeyError, FileNotFoundError) as error:
                        raise SnapshotMemberUnavailable(mid) from error
                    except PermissionError as error:
                        if error.errno is None:
                            raise
                        raise ValueError(f'服务端无法读取或保存动作 {mid}。请检查存储后重新创建快照。') from error
                    items.append(dict(motion_id=mid, version_id=version['id'], sha256=version['sha256'],
                                      label=version['description'][:120], frames=version['frames'], fps=version['fps'],
                                      bytes=size))
                batches = []
                start = total = 0
                for i, member in enumerate(items):
                    size = member['bytes']
                    if i > start and (i-start >= BATCH_SIZE or total + size > BATCH_BYTES):
                        batches.append([start,i])
                        start,total = i,0
                    total += size
                batches.append([start,len(items)])
                manifest = dict(schema=1, id=sid, name=name, created=time.time(), collection_id=cid,
                                collection_name=collection_name, basis=basis, dependencies='results_only', items=items, batches=batches)
                raw = encoded(manifest)
                digest = hashlib.sha256(raw).hexdigest()
                with closing(self.store.reviews()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    old = db.execute('SELECT owner,collection_id,name FROM personal_asset_snapshots WHERE id=?', (sid,)).fetchone()
                    if old:
                        if tuple(old) != (self.owner, cid, name):
                            raise ValueError('快照编号已被使用')
                        saved = self.snapshot(sid)
                        if saved['basis'] != basis:
                            raise ValueError('快照编号已用于不同成员')
                        return saved
                    self.put_file(sid + '.json.gz', gzip.compress(raw, mtime=0), staging_prefix=SNAPSHOT_MANIFEST_TEMP)
                    db.execute('INSERT INTO personal_asset_snapshots(owner,id,collection_id,collection_name,name,created,count,sha256) '
                               'VALUES(?,?,?,?,?,?,?,?)', (self.owner, sid, cid, collection_name, name, manifest['created'], len(items), digest))
                    db.execute("UPDATE personal_snapshot_jobs SET status='done',error='',source=NULL "
                               "WHERE owner=? AND id=? AND collection_id=? AND name=? AND basis=? AND status='queued'",
                               (self.owner, sid, cid, name, basis))
            except BaseException:
                self._rollback_materialized(created_assets)
                raise
        return self.snapshot(sid)

    def snapshot(self, sid):
        with closing(self.store.reviews()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM personal_asset_snapshots WHERE owner=? AND id=?', (self.owner, sid)).fetchone()
        if row is None:
            raise Forbidden('资产快照不存在或不可访问')
        try:
            raw = gzip.decompress((self.root / (sid + '.json.gz')).read_bytes())
        except (EOFError, FileNotFoundError, gzip.BadGzipFile, zlib.error) as exc:
            raise ValueError('快照清单无法读取，请重新创建快照。') from exc
        except OSError as exc:
            raise OSError(SNAPSHOT_STORAGE_ERROR) from exc
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('快照清单校验失败')
        return json.loads(raw)

    def preview_snapshot_member(self, sid, motion_id, version_id):
        from g2h.motion_assets import MotionArtifactCorrupt

        if not re.fullmatch(ID, sid):
            raise ValueError('快照编号无效')
        manifest = self.snapshot(sid)
        member = next((item for item in manifest['items']
                       if item['motion_id'] == motion_id and item['version_id'] == version_id), None)
        if member is None:
            raise Forbidden('该动作版本不属于此快照')
        require_motion_access(self.store, motion_id, self.user)
        try:
            metadata = self.service.assets.get(self.owner, version_id)
            if metadata['sha256'] != member['sha256']:
                raise MotionArtifactCorrupt('快照版本校验失败')
            self.snapshot_artifact_path(motion_id, version_id)
        except (KeyError, FileNotFoundError) as exc:
            raise ValueError(f'快照动作 {motion_id} 的版本文件已丢失。') from exc
        except MotionArtifactCorrupt as exc:
            raise ValueError(f'快照动作 {motion_id} 的版本文件校验失败。') from exc
        return self.service.preview(self.user, version_id)

    def snapshots(self, after, collection=None):
        from personal_snapshot_jobs import list_pending, schedule

        args = (self.owner, after)
        clause = ''
        if collection:
            clause = ' AND collection_id=?'
            args += (collection,)
        with closing(self.store.reviews()) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            rows = db.execute('SELECT * FROM personal_asset_snapshots WHERE owner=? AND seq<?'
                              + clause + ' ORDER BY seq DESC LIMIT 51', args).fetchall()
            jobs = list_pending(db, self.owner, collection) if after == 2**63-1 else []
        for job in jobs:
            if job['status'] == 'queued':
                schedule(self, job['id'])
        return dict(page(rows), jobs=jobs)

    def export(self, sid, batch, path):
        manifest = self.snapshot(sid)
        bounds = self._batch_bounds(manifest, batch)
        path = Path(path)
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=path.parent) as directory:
            staged = Path(directory) / path.name
            with staged.open('wb') as output:
                self._write_batch(manifest, batch, bounds, output)
            os.replace(staged, path)

    @staticmethod
    def _batch_bounds(manifest, batch):
        if not 1 <= batch <= len(manifest['batches']):
            raise ValueError('快照下载批次无效')
        return manifest['batches'][batch-1]

    def _write_batch(self, manifest, batch, bounds, output):
        from g2h.motion_assets import MotionArtifactCorrupt

        start, end = bounds
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as archive:
            # HTTP range requests regenerate this archive. Keep ZIP metadata stable.
            archive.writestr(zipfile.ZipInfo('manifest.json'), encoded(manifest))
            archive.writestr(zipfile.ZipInfo('batch.json'), encoded(dict(batch=batch, batches=len(manifest['batches']),
                                                                         start=start, end=end)))
            for item in manifest['items'][start:end]:
                if not item['motion_id'].startswith('ma_'):
                    require_motion_access(self.store, item['motion_id'], self.user)
                try:
                    source = self.snapshot_artifact_path(item['motion_id'], item['version_id'])
                    data = source.open('rb')
                except (KeyError, FileNotFoundError) as exc:
                    raise SnapshotVersionUnavailable(item['motion_id'], '文件已丢失') from exc
                except SnapshotRestoreUnavailable as exc:
                    raise SnapshotVersionUnavailable(item['motion_id'], '文件缺失且无法恢复原始版本') from exc
                except MotionArtifactCorrupt as exc:
                    raise SnapshotVersionUnavailable(item['motion_id'], '文件已损坏') from exc
                digest = hashlib.sha256()
                size = 0
                member = 'motions/' + item['motion_id'] + '.npz'
                with data, archive.open(member, 'w') as entry:
                    while chunk := data.read(1024 * 1024):
                        size += len(chunk)
                        if size > item['bytes']:
                            raise SnapshotVersionUnavailable(item['motion_id'], '文件校验失败')
                        digest.update(chunk)
                        entry.write(chunk)
                if size != item['bytes'] or digest.hexdigest() != item['sha256']:
                    raise SnapshotVersionUnavailable(item['motion_id'], '文件校验失败')

    def download_snapshot(self, handler, sid, batch):
        from g2h.capacity import CapacityUnavailable, reservation

        try:
            manifest = self.snapshot(sid)
            start, end = self._batch_bounds(manifest, batch)
            # The ZIP stores uncompressed assets; reserve metadata and ZIP headers too.
            estimate = sum(item['bytes'] for item in manifest['items'][start:end]) + len(encoded(manifest)) + 65536
            capacity = reservation(self.store.root, estimate, 'snapshot-download:'+sid)
        except CapacityUnavailable as exc:
            return handler.send(503, {'error':str(exc)})
        except OSError:
            return handler.send(503, {'error':SNAPSHOT_STORAGE_ERROR})
        output = None
        failure = None
        failure_status = 503
        try:
            try:
                # On Unix the OS removes this file when its last handle closes.
                output = tempfile.TemporaryFile(dir=self.root)
                self._write_batch(manifest, batch, (start, end), output)
                output.flush()
            except SnapshotVersionUnavailable as exc:
                failure_status = 410
                failure = str(exc)
            except CapacityUnavailable as exc:
                failure = str(exc)
            except OSError:
                failure = SNAPSHOT_STORAGE_ERROR
            if failure is None:
                return handler.download_stream(output, f'kinora-snapshot-{sid[:8]}-{batch}.zip')
        finally:
            try:
                if output is not None:
                    output.close()
                capacity.commit(0)
            except (OSError, sqlite3.Error) as exc:
                print(f'Snapshot download cleanup pending for {sid}: {exc}',flush=True)
            try:
                capacity.__exit__(None, None, None)
            except (OSError, sqlite3.Error) as exc:
                print(f'Snapshot capacity recovery pending for {sid}: {exc}',flush=True)
        return handler.send(failure_status, {'error':failure})


def page(rows):
    return dict(items=[dict(r) for r in rows[:50]], next_after=rows[49]['seq'] if len(rows)>50 else None)


def handle(handler, service, user, path, query, write):
    library = Library(service, user)
    endpoint = path.removeprefix(BASE).strip('/')
    after = int(query.get('after', [2**63-1])[0])
    if not 0 < after < 2**63:
        raise ValueError('分页编号无效')
    if endpoint == 'motions' and not write:
        return handler.send(200, library.own_motions(after))
    if endpoint == 'history' and not write:
        return handler.send(200, library.history(query.get('motion', [''])[0]))
    if endpoint == 'snapshot-source' and not write:
        name, members, basis = library.collection_basis(query.get('collection', [''])[0])
        library.validate_snapshot_members(members)
        return handler.send(200, dict(name=name, count=len(members), basis=basis))
    if endpoint == 'snapshots' or re.fullmatch(r'snapshot-jobs/' + ID,endpoint):
        from personal_snapshot_jobs import get, submit
        if endpoint.startswith('snapshot-jobs/') and not write:
            return handler.send(200, get(library, endpoint.split('/')[1]))
        if write:
            try:
                return handler.send(202, submit(library, handler.body()))
            except CollectionMembershipChanged as exc:
                return handler.send(409, {'error':str(exc)})
        return handler.send(200, library.snapshots(after, query.get('collection', [None])[0]))
    snapshot = re.fullmatch(r'snapshots/(' + ID + r')(/download)?', endpoint)
    if snapshot and not write:
        sid, download = snapshot.groups()
        if download:
            return library.download_snapshot(handler, sid, int(query.get('batch', ['1'])[0]))
        return handler.send(200, library.snapshot(sid))
    from personal_asset_media import handle as media
    try:
        return media(handler, library, endpoint, query, write, after)
    except OSError as exc:
        if not write or exc.errno not in (errno.ENOSPC, errno.EDQUOT):
            raise
        return handler.send(503, {'error':'服务器写入空间不足，请释放空间后重试。'})
