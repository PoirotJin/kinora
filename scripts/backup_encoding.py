"""Lossless optional zstd/gzip storage. Manifests always describe original bytes."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

try:
    import zstandard
except ImportError:
    zstandard = None


def stored_path(raw):
    raw = Path(raw)
    candidates = [raw, raw.with_name(raw.name + '.zst'), raw.with_name(raw.name + '.gz')]
    path = next((p for p in candidates if p.exists() or p.is_symlink()), raw)
    if path.is_symlink():
        raise ValueError('Backup storage refuses symlinks')
    return path


def open_stored(path, encoding=None):
    path = Path(path)
    suffix = encoding or path.suffix
    if suffix == '.zst':
        if zstandard is None:
            raise RuntimeError('zstandard is required to restore this backup')
        return zstandard.ZstdDecompressor().stream_reader(path.open('rb'), closefd=True)
    return gzip.open(path, 'rb') if suffix == '.gz' else path.open('rb')


def content_hash(path):
    digest = hashlib.sha256()
    with open_stored(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def compact(raw, expected):
    """Caller holds the backup lock. Verify decompressed bytes before unlink."""
    raw = Path(raw)
    if not raw.exists():
        existing = stored_path(raw)
        if content_hash(existing) != expected:
            raise ValueError('Compressed backup checksum mismatch')
        return 0
    if raw.is_symlink():
        raise ValueError('Refuse symlink')
    before = raw.stat()
    suffix = '.zst' if zstandard is not None else '.gz'
    target = raw.with_name(raw.name + suffix)
    temporary = raw.with_name(raw.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        digest = hashlib.sha256()
        with raw.open('rb') as src, temporary.open('xb') as out:
            os.chmod(temporary, before.st_mode & 0o777)
            if os.geteuid() == 0:
                os.chown(temporary, before.st_uid, before.st_gid)
            compressor = (zstandard.ZstdCompressor(level=1, threads=0).stream_writer(out, closefd=False)
                          if zstandard is not None else gzip.GzipFile(filename='', mode='wb', fileobj=out, compresslevel=1, mtime=0))
            with compressor as zipped:
                for block in iter(lambda: src.read(1024 * 1024), b''):
                    digest.update(block)
                    zipped.write(block)
            out.flush(); os.fsync(out.fileno())
        if digest.hexdigest() != expected:
            raise ValueError('Original backup checksum mismatch')
        digest = hashlib.sha256()
        with open_stored(temporary, encoding=suffix) as restored:
            for block in iter(lambda: restored.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('Compressed backup verification failed')
        current = raw.stat()
        if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns):
            raise ValueError('Backup changed while compressing')
        if temporary.stat().st_blocks >= before.st_blocks:
            return 0
        if target.exists() or target.is_symlink():
            if target.is_symlink() or content_hash(target) != expected:
                raise ValueError('Existing compressed backup is corrupt')
            temporary.unlink()
        else:
            os.replace(temporary, target)
        # Publish and fsync before removing the verified redundant source.
        fd=os.open(raw.parent, os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        saved = before.st_blocks * 512 - target.stat().st_blocks * 512
        raw.unlink()
        return saved
    finally:
        temporary.unlink(missing_ok=True)


def compact_databases(folder, report):
    # Topic indexes stay directly readable for cross-snapshot reference audits.
    folder = Path(folder)
    reusable = {}
    for manifest in sorted(folder.parent.glob('db-*/manifest.json')):
        if manifest.parent == folder or manifest.parent.is_symlink():continue
        if manifest.parent.name.endswith(('.partial','.drill')):continue
        old = json.loads(manifest.read_text())
        if not old.get('restore_verified'):continue
        for entry in old['databases']:
            relative = Path(entry['path'])
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Invalid database path')
            candidate = stored_path(manifest.parent/relative)
            if candidate.suffix in ('.gz','.zst') and candidate.is_file():
                reusable.setdefault(entry['sha256'],candidate)
    saved = 0
    for entry in report['databases']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid database path')
        if relative.parts[0] != 'review':continue
        raw=folder/relative
        saved += compact(raw, entry['sha256'])
        current=stored_path(raw); candidate=reusable.get(entry['sha256'])
        if candidate and current.suffix == candidate.suffix and not current.samefile(candidate):
            if content_hash(candidate) != entry['sha256']:
                raise ValueError('Reusable database backup checksum mismatch')
            before=current.stat()
            temporary=current.with_name(current.name+'.'+uuid.uuid4().hex+'.link')
            try:
                os.link(candidate,temporary)
                os.replace(temporary,current)
            finally:temporary.unlink(missing_ok=True)
            if before.st_nlink == 1:saved += before.st_blocks*512
    return saved
