"""Build the downloadable Gadget ZIP from an explicit, maintained source allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import zipfile

PROJECT=Path(__file__).resolve().parents[1]
VERSION='0.9.1'
FILES=('AGENTS.md','PROMPT_GUIDE.md','README.md','codex-config.example.toml','cursor-mcp.example.json','example.json','motionfarm.py','motionfarm_mcp.py','prompt_harness.py','test_client.py','test_prompt_harness.py','test_workflow_journal.py','test_workflow_round2.py','test_account_recovery.py','workflow_journal.py')

def build(output=None):
    source=PROJECT/'gadget';target=Path(output or PROJECT/'web/review/gadget-kit.zip')
    payload={name:(source/name).read_bytes() for name in FILES}
    if any(len(data)>1024*1024 for data in payload.values()) or sum(map(len,payload.values()))>4*1024*1024:raise ValueError('Unexpectedly large Gadget source')
    manifest=dict(version=VERSION,files={name:hashlib.sha256(data).hexdigest() for name,data in sorted(payload.items())})
    payload['BUNDLE-MANIFEST.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_suffix(target.suffix+'.tmp')
    with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,data in sorted(payload.items()):
            entry=zipfile.ZipInfo('motionfarm-gadget/'+name,date_time=(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED;entry.create_system=3;entry.external_attr=0o100644<<16
            archive.writestr(entry,data)
    with zipfile.ZipFile(tmp) as archive:
        if archive.testzip() is not None:raise ValueError('Gadget archive CRC failure')
        if any(archive.read('motionfarm-gadget/'+name)!=data for name,data in payload.items()):raise ValueError('Gadget source/archive mismatch')
    os.replace(tmp,target)
    return dict(version=VERSION,path=str(target),bytes=target.stat().st_size,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),files=len(payload))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args();print(json.dumps(build(args.output),indent=2))
