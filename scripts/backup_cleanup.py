"""Bounded maintenance of disposable files only; never remove motion objects."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time


def clean(project, target, apply=False, now=None, max_entries=100000, seconds=3):
    project, target = Path(project).resolve(), Path(target).resolve()
    now = time.time() if now is None else now
    report = dict(apply=apply, files=0, bytes=0, examined=0, bounded=False)
    # Same lock as the backup writer: old partials cannot belong to a live copy.
    with (target / 'backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + seconds
        folders = [(project / part / '__pycache__', 'pyc') for part in ('scripts', 'g2h')]
        folders.append((target / 'objects', 'partial'))
        for folder, kind in folders:
            if folder.is_symlink() or not folder.exists():
                continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    report['examined'] += 1
                    if report['examined'] > max_entries or time.monotonic() > deadline:
                        report['bounded'] = True
                        return report
                    matches = entry.name.endswith('.pyc') if kind == 'pyc' else re.fullmatch(r'copy-[0-9a-f]{32}\.partial', entry.name)
                    if not matches or not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if now - info.st_mtime < 86400:
                        continue
                    path = Path(entry.path)
                    if apply:
                        current = path.lstat()
                        if not stat.S_ISREG(current.st_mode) or (current.st_ino, current.st_mtime_ns, current.st_size) != (info.st_ino, info.st_mtime_ns, info.st_size):
                            continue
                        path.unlink()
                    report['files'] += 1
                    report['bytes'] += info.st_size
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = json.loads((args.root / 'payload_backup_config.json').read_text())
    print(json.dumps(clean(args.project, config['target'], args.apply)))
