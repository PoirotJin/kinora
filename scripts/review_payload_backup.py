"""Bounded incremental motion backups; immutable objects and explicit coverage.

No model weights. Same-host copies are never represented as offsite backups.
"""
import argparse
from backup_encoding import stored_path, open_stored, compact
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
import sys

PATTERNS = ('topics/*/motions_*.pkl', 'topics/*/motions_*.mf.zip', 'review/gadget/imports/mimp*.npz',
            'review/retarget/*/result.npz', 'review/retarget/*/preview.json',
            'review/retarget/*/quality.json', 'review/retarget/*/execution.json')


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    # Status metadata stays readable across root-run backup rotations.
    public_status = path.name in ('latest.json', 'last_attempt.json', 'restore-evidence.json', 'verification.json', 'progress.json') or path.name.endswith('-summary.json')
    if public_status and os.geteuid() == 0:
        os.chown(temporary, -1, path.parent.stat().st_gid)
    os.chmod(temporary, 0o640 if public_status else 0o600)
    os.replace(temporary, path)
    if path.name in ('latest.json','last_attempt.json'):
        summary={k:v for k,v in data.items() if k not in ('files','skipped')}
        summary.update(file_count=len(data.get('files',[])),skipped_count=len(data.get('skipped',[])))
        atomic_json(path.with_name(path.stem+'-summary.json'),summary)


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe backup path')
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Backup path escapes its root')
    return target


def inventory(root, cold=None, max_files=20000):
    rows=[]
    for tier,base,patterns in [('hot',root,PATTERNS),('cold',cold,('topics/*/motions_*.mf.zip',))]:
        if base is None:continue
        base=Path(base).resolve();checked=set()
        def add(path):
            parent=path.parent
            if parent not in checked:
                if parent.is_symlink() or not parent.resolve().is_relative_to(base):
                    raise ValueError('Backup path escapes its root')
                checked.add(parent)
            if path.is_symlink():raise ValueError('Backup refuses symlink files')
            if not path.is_file():return
            rows.append((tier,base,path))
            if len(rows)>max_files:raise ValueError('Payload inventory exceeds configured file bound')
        names=[]
        for pattern in patterns:
            if pattern.startswith('review/retarget/*/'):
                names.append(pattern.rsplit('/',1)[1]);continue
            for path in base.glob(pattern):add(path)
        folder=base/'review/retarget'
        if names and folder.exists():
            if folder.is_symlink() or not folder.resolve().is_relative_to(base):raise ValueError('Unsafe retarget folder')
            # One directory traversal for all artifact types, rather than one
            # traversal per filename over hundreds of thousands of job folders.
            for job in folder.iterdir():
                if job.is_symlink():raise ValueError('Backup refuses symlink job directories')
                if job.is_dir():
                    for name in names:add(job/name)
    return sorted(rows,key=lambda row:str(row[2]))


def fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def same_inode_state(a, b):
    return (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
            and len(a) >= 4 and len(b) >= 4 and list(a[-4:]) == list(b[-4:]))


def copy_object(source, target, rate):
    before = fingerprint(source)
    # Hash before writing: renamed/touched/duplicate files share one object.
    key = hash_file(source, rate)
    if before != fingerprint(source):
        raise RuntimeError('Source changed during backup: ' + str(source))
    destination = stored_path(target / key)
    if destination.exists():
        if destination.is_symlink() or hash_file(destination, rate) != key:
            raise ValueError('Existing backup object is corrupt')
        if before != fingerprint(source):
            raise RuntimeError('Source changed during backup: ' + str(source))
        return key, 0, before
    destination = target / key
    temporary = target / ('copy-' + uuid.uuid4().hex + '.partial')
    digest = hashlib.sha256()
    started = time.monotonic()
    count = 0
    try:
        with source.open('rb') as src, temporary.open('xb') as dst:
            os.chmod(temporary, 0o600)
            for block in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(block)
                digest.update(block)
                count += len(block)
                delay = count / rate - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(min(delay, 1))
            dst.flush()
            os.fsync(dst.fileno())
        if before != fingerprint(source):
            raise RuntimeError('Source changed during backup: ' + str(source))
        key = digest.hexdigest()
        destination = target / key
        if destination.exists():
            if hash_file(destination) != key:
                raise ValueError('Existing backup object is corrupt')
            temporary.unlink()
        else:
            temporary.rename(destination)
        # Read-back proves the destination bytes, not merely source hashing.
        if hash_file(destination) != key:
            raise ValueError('Backup read-back verification failed')
        if source.suffix == '.json':
            compact(destination, key)
        return key, count, before
    finally:
        temporary.unlink(missing_ok=True)


def hash_file(path, rate=25_000_000):
    digest = hashlib.sha256()
    started = time.monotonic()
    count = 0
    with open_stored(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            count += len(block)
            delay = count / rate - (time.monotonic() - started)
            if delay > 0:
                time.sleep(min(delay, 1))
    return digest.hexdigest()


def backup(root, target, cold=None, max_bytes=30_000_000_000, rate=25_000_000, max_files=20000):
    root, target = Path(root).resolve(), Path(target).resolve()
    cold = Path(cold).resolve() if cold else None
    if target == root or target.is_relative_to(root) or (cold and target.is_relative_to(cold)):
        raise ValueError('Backup target must be independent of data directories')
    if not target.is_dir():
        raise ValueError('Configure an existing backup target first')
    if not 1_000_000 <= rate <= 50_000_000 or max_bytes <= 0:
        raise ValueError('Invalid IO budget')
    with (target / 'backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        objects = target / 'objects'
        objects.mkdir(mode=0o700, exist_ok=True)
        snapshots = target / 'snapshots'
        snapshots.mkdir(mode=0o700, exist_ok=True)
        previous = json.loads((target / 'latest.json').read_text()) if (target / 'latest.json').exists() else {}
        cache = {(e['tier'], e['path']): e for e in previous.get('files', [])}
        checkpoint=target/'checkpoint.jsonl'
        if checkpoint.exists():
            with checkpoint.open() as stream:
                for line in stream:
                    try:
                        entry=json.loads(line);cache[(entry['tier'],entry['path'])]=entry
                    except (ValueError,KeyError):continue
        def remember(entry):
            # Each copied object is read-back verified before it enters this journal.
            with checkpoint.open('a') as stream:
                os.chmod(checkpoint,0o600)
                stream.write(json.dumps(entry)+'\n');stream.flush();os.fsync(stream.fileno())

        report = dict(id=time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8], created=time.time(),
                      files=[], skipped=[], copied_bytes=0, reused=0, offsite=False,
                      coverage='Human motion shards (hot/cold), retarget results and quality/execution files. Model weights, checkpoints, videos and detailed logs excluded. Identical file content stored once. File-consistent, not globally atomic.',
                      complete=False, restore_verified=False)
        if type(max_files) is not int or not 1<=max_files<=2_000_000:raise ValueError('Invalid inventory bound')
        metadata_config=root/'backup_config.json'
        if metadata_config.exists():
            metadata_target=Path(json.loads(metadata_config.read_text())['target'])
            metadata=json.loads((metadata_target/'latest.json').read_text())
            if not metadata.get('restore_verified') or time.time()-metadata['created']>36*3600:
                raise ValueError('A fresh restore-verified metadata backup is required before payload copy')
            report['metadata_snapshot']={k:metadata[k] for k in ('id','created')}
        candidates = inventory(root, cold, max_files)
        def reusable(tier, base, path):
            entry = cache.get((tier, str(path.relative_to(base))), {})
            return same_inode_state(entry.get('fingerprint'), fingerprint(path)) and stored_path(objects / entry.get('sha256', 'missing')).is_file()
        changed = sum(p.stat().st_size for tier, base, p in candidates if not reusable(tier, base, p))
        if changed > max_bytes or shutil.disk_usage(target).free - changed < 30_000_000_000:
            raise ValueError('Backup exceeds IO budget or 30 GB free-space reserve')
        sample_keys = set()
        if len(candidates) > 1000:
            ranked = sorted(
                (hashlib.sha256(f"{report['id']}:{tier}:{source.relative_to(base)}".encode()).hexdigest(),
                 (tier, str(source.relative_to(base))))
                for tier, base, source in candidates
                if cache.get((tier, str(source.relative_to(base))), {}).get('object_fingerprint')
            )
            sample_keys = {key for _, key in ranked[:32]}
        for index,(tier, base, source) in enumerate(candidates):
            if index%1000==0:
                atomic_json(target/'progress.json',dict(created=time.time(),snapshot=report['id'],processed=index,total=len(candidates),copied_bytes=report['copied_bytes'],reused=report['reused'],complete=False))
            relative = str(source.relative_to(base))
            old = cache.get((tier, relative))
            cur_stamp = fingerprint(source)
            if old and same_inode_state(old.get('fingerprint'), cur_stamp) and stored_path(objects / old['sha256']).is_file():
                object_path=stored_path(objects / old['sha256'])
                cur_obj_fp=fingerprint(object_path)
                needs_hash=(len(candidates)<=1000
                            or not same_inode_state(old.get('object_fingerprint'), cur_obj_fp)
                            or (tier, relative) in sample_keys)
                if needs_hash and hash_file(object_path, rate) != old['sha256']:
                    raise ValueError('Reused backup object is corrupt: ' + relative)
                refreshed=dict(old,fingerprint=cur_stamp,object_fingerprint=cur_obj_fp)
                report['files'].append(refreshed)
                report['reused'] += 1
                continue
            try:
                key, size, stamp = copy_object(source, objects, rate)
                entry=dict(tier=tier,path=relative,sha256=key,bytes=stamp[2],fingerprint=stamp,object_fingerprint=fingerprint(stored_path(objects/key)))
                remember(entry)
                report['files'].append(entry)
                report['copied_bytes'] += size
                if size == 0:
                    report['reused'] += 1
            except (OSError, RuntimeError) as exc:
                report['skipped'].append(dict(tier=tier, path=relative, reason=str(exc)))
        report['complete'] = not report['skipped']
        atomic_json(snapshots / (report['id'] + '.json'), report)
        atomic_json(target / 'last_attempt.json', report)
        if report['complete']:
            atomic_json(target / 'latest.json', report)
            checkpoint.unlink(missing_ok=True)
        atomic_json(target/'progress.json',dict(created=time.time(),snapshot=report['id'],processed=len(candidates),total=len(candidates),copied_bytes=report['copied_bytes'],reused=report['reused'],complete=report['complete']))
        return report


def sample_entries(report, limit=None, seed=None):
    if not limit:return report['files']
    ordered=sorted(report['files'],key=lambda e:(e['path'].endswith('.json'),
        hashlib.sha256((str(seed)+e['tier']+e['path']).encode()).hexdigest() if seed is not None else e['path']))
    entries=[];groups=set()
    for entry in ordered:
        group=(entry['tier'],Path(entry['path']).suffix)
        if group not in groups and len(entries)<limit:entries.append(entry);groups.add(group)
    return entries+[e for e in ordered if e not in entries][:max(0,limit-len(entries))]


def restore(target, snapshot, destination, limit=None, sample_seed=None):
    target, destination = Path(target).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Restore requires a new isolated destination')
    manifest = safe_path(target / 'snapshots', snapshot + '.json')
    report = json.loads(manifest.read_text())
    if not report['complete']:
        raise ValueError('Incomplete snapshot cannot be restored as complete')
    if limit is not None and limit < 1:
        raise ValueError('Sample size must be positive')
    entries = sample_entries(report,limit,sample_seed)
    required = sum(e['bytes'] for e in entries)
    if shutil.disk_usage(destination.parent).free - required < 10_000_000_000:
        raise ValueError('Insufficient restore space')
    destination.mkdir(mode=0o700)
    for entry in entries:
        key = entry['sha256']
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid object digest')
        source = stored_path(safe_path(target / 'objects', key))
        if hash_file(source) != key:
            raise ValueError('Backup object integrity failure')
        if entry['tier'] not in ('hot', 'cold'):
            raise ValueError('Invalid storage tier')
        dest = safe_path(destination, entry['tier'] + '/' + entry['path'])
        dest.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        copied = 0
        with open_stored(source) as src, dest.open('xb') as out:
            for block in iter(lambda: src.read(1024 * 1024), b''):
                out.write(block)
                copied += len(block)
                delay = copied / 25_000_000 - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(min(delay, 1))
        os.chmod(dest, 0o600)
        if hash_file(dest) != key:
            raise ValueError('Restore hash mismatch')
    evidence = dict(snapshot=snapshot, verified_files=len(entries), total_files=len(report['files']),
                    scope='full' if len(entries) == len(report['files']) else 'sample', created=time.time())
    atomic_json(destination / 'restore-evidence.json', evidence)
    atomic_json(target / 'restore-evidence.json', evidence)
    return evidence


def status(root):
    config = Path(root) / 'payload_backup_config.json'
    if not config.exists():
        return dict(configured=False, offsite=False)
    settings = json.loads(config.read_text())
    target = Path(settings['target'])
    result = dict(configured=True, available=target.is_dir(), offsite=False,
                  coverage='动作分片、冷归档、重定向结果；不含模型权重', retention='Immutable snapshots; no automatic deletion')
    for name in ('latest', 'last_attempt', 'restore-evidence', 'verification', 'progress'):
        path = target / (name + '-summary.json')
        if not path.exists():path=target/(name+'.json')
        if path.exists():
            value = json.loads(path.read_text())
            result[name] = {k: v for k, v in value.items() if k not in ('files', 'skipped')}
            if 'files' in value:
                result[name]['file_count'] = len(value['files'])
                result[name]['skipped_count'] = len(value['skipped'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['backup', 'status', 'restore'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--snapshot')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--sample', type=int)
    args = parser.parse_args()
    if args.operation == 'status':
        result = status(args.root)
    else:
        config = json.loads((args.root / 'payload_backup_config.json').read_text())
        if args.operation == 'backup':
            from g2h.storage_tiers import cold_root
            try:
                result = backup(args.root, config['target'], cold_root(args.root), config.get('max_bytes', 30_000_000_000), config.get('rate', 25_000_000), config.get('max_files', 20000))
            except Exception as exc:
                atomic_json(Path(config['target']) / 'last_attempt.json', dict(created=time.time(), complete=False, error=str(exc), files=[], skipped=[]))
                raise
        else:
            result = restore(config['target'], args.snapshot, args.destination, args.sample)
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=False, indent=2))
    if args.operation == 'backup' and not result['complete']:
        sys.exit(1)
