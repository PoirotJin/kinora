"""Package an explicitly prepared Kinora server tree, excluding installed/runtime assets."""
import argparse,hashlib,json,re,zipfile
from pathlib import Path
EXCLUDE={'.git','.venv','.retarget-venv','.kimodo-venv','.vendor','__pycache__','runtime','ckpts','node_modules'}
ALLOWED={'.py','.js','.cjs','.css','.html','.json','.md','.txt','.toml','.rs','.xml','.urdf','.sh','.woff2','.yml','.yaml'}
NAMES={'LICENSE','NOTICE','THIRD_PARTY_LICENSES','VERSION','.gitignore'}
SECRETS=[rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',rb'\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{25,}',rb'\bsk-[A-Za-z0-9_-]{24,}']
def build(source,output):
    source=source.resolve();output=output.resolve()
    if output.is_relative_to(source):raise ValueError('Output must be outside the prepared source tree')
    version=(source/'VERSION').read_text().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):raise ValueError('Invalid version')
    external={a['path'] for a in json.loads((source/'assets.lock.json').read_text())['files']}
    payload={}
    for p in sorted(source.rglob('*')):
        rel=p.relative_to(source);name=rel.as_posix()
        if not p.is_file() or any(part in EXCLUDE or part.startswith('._') for part in rel.parts):continue
        if p.is_symlink():raise ValueError('Unexpected source symlink: '+name)
        if name in external or name=='SOURCE_MANIFEST.json' or name.startswith('web/review/robot/'):continue
        if p.suffix not in ALLOWED and p.name not in NAMES and name not in {'web/review/gadget-kit.zip','scripts/fixtures/g1-dynamics-positive.npz','data/human_rig/j_template.bin','data/human_rig/kintree.bin'}:raise ValueError('Unexpected file: '+name)
        data=p.read_bytes()
        if len(data)>1024*1024:raise ValueError('Oversized source file: '+name)
        if any(re.search(pattern,data) for pattern in SECRETS):raise ValueError('Credential pattern: '+name)
        if name.startswith('web/review/gadget-kit.zip'):
            import io
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if z.testzip():raise ValueError('Invalid Gadget archive')
                if any(re.search(pattern,z.read(n)) for n in z.namelist() for pattern in SECRETS):raise ValueError('Credential in Gadget')
        payload[name]=data
    manifest={'version':version,'gadget_version':'0.15.2','scope':'Full server code; external model weights/rig meshes and runtime data are not bundled','sha256':{n:hashlib.sha256(b).hexdigest() for n,b in payload.items()}}
    raw=(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode();(source/'SOURCE_MANIFEST.json').write_bytes(raw);payload['SOURCE_MANIFEST.json']=raw
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,data in sorted(payload.items()):
            entry=zipfile.ZipInfo('kinora-server-'+version+'/'+name,date_time=(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;entry.create_system=3;entry.external_attr=0o100644<<16;archive.writestr(entry,data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():raise ValueError('Archive CRC failed')
    print(json.dumps({'file':str(output),'files':len(payload),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path);args=p.parse_args();build(args.source,args.output)
