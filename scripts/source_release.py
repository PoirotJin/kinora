"""Immutable source snapshots and checksum-verified, source-only rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

TREES=('scripts','g2h','web','gadget','.github','docs')
EXTENSIONS={'.py','.js','.cjs','.mjs','.css','.html','.md','.yml','.yaml','.txt'}
EXCLUDED={'__pycache__','node_modules','.git','.venv','dist','build'}

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def sources(project):
    for tree in TREES:
        for p in sorted((project/tree).rglob('*')):
            relative=p.relative_to(project)
            if p.is_symlink() or any(part in EXCLUDED for part in relative.parts):continue
            if p.is_file() and p.suffix in EXTENSIONS and not p.name.startswith(('._','.env')):yield relative
    for name in ('AGENTS.md','README.md','requirements.txt','requirements-index.txt','environment.yml'):
        if (project/name).is_file() and not (project/name).is_symlink():yield Path(name)

def snapshot(project,target):
    project=Path(project).resolve();target=Path(target).resolve()
    if target==project or target.is_relative_to(project):raise ValueError('Use an independent release directory')
    target.mkdir(mode=0o700,parents=True,exist_ok=True)
    key=time.strftime('source-%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:8]
    work=target/(key+'.partial');work.mkdir(mode=0o700)
    files=[]
    for relative in sources(project):
        src=project/relative;before=digest(src);dest=work/'source'/relative
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
        if before!=digest(src) or before!=digest(dest):raise ValueError('Source changed during snapshot')
        files.append(dict(path=str(relative),sha256=before))
    report=dict(id=key,created=time.time(),files=files,scope='Source only; runtime data, credentials, model assets and service configuration excluded')
    (work/'manifest.json').write_text(json.dumps(report,indent=2));work.rename(target/key)
    return dict(id=key,files=len(files),directory=str(target/key))

def verify(folder):
    folder=Path(folder).resolve();report=json.loads((folder/'manifest.json').read_text())
    for entry in report['files']:
        relative=Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe manifest path')
        allowed=(relative.parts[0] in TREES and relative.suffix in EXTENSIONS) or str(relative) in ('AGENTS.md','README.md','requirements.txt','requirements-index.txt','environment.yml')
        if not allowed or any(part in EXCLUDED for part in relative.parts):raise ValueError('Manifest is not source-only')
        p=folder/'source'/relative
        if p.is_symlink() or not p.resolve().is_relative_to(folder/'source') or digest(p)!=entry['sha256']:raise ValueError('Release checksum mismatch')
    return report

def restore(folder,project):
    folder=Path(folder).resolve();project=Path(project).resolve();report=verify(folder)
    # Verify every destination before replacing any file. Keep data and Git untouched.
    for entry in report['files']:
        dest=project/entry['path']
        if dest.is_symlink() or not dest.resolve().is_relative_to(project):raise ValueError('Unsafe destination')
    for entry in report['files']:
        dest=project/entry['path'];dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=dest.with_name(dest.name+'.release-tmp');shutil.copy2(folder/'source'/entry['path'],tmp);os.replace(tmp,dest)
    return dict(restored=report['id'],files=len(report['files']),restart_required=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('operation',choices=('snapshot','verify','restore'));p.add_argument('--project',type=Path);p.add_argument('--target',type=Path,required=True);a=p.parse_args()
    if a.operation=='snapshot':result=snapshot(a.project,a.target)
    elif a.operation=='restore':result=restore(a.target,a.project)
    else:r=verify(a.target);result=dict(verified=r['id'],files=len(r['files']))
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
