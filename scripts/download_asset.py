"""Explicit, authenticated single-file asset download; no implicit bulk downloads."""
import argparse,json,os,shlex,subprocess,uuid
from pathlib import Path,PurePosixPath
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('path',nargs='?');p.add_argument('--list',action='store_true');p.add_argument('--host',default='motionfarm');a=p.parse_args()
 manifest=json.loads((ROOT/'docs/ASSET_DOWNLOADS.json').read_text());entries={x['path']:x for x in manifest['files']}
 if a.list:
  for row in entries.values():print(str(row['bytes'])+'\t'+row['path']+'\t'+row['url'])
  return
 if a.path not in entries:p.error('Select an exact path from --list')
 if a.host.startswith('-') or any(c.isspace() for c in a.host):p.error('Invalid SSH host')
 row=entries[a.path];rel=PurePosixPath(a.path)
 if rel.is_absolute() or '..' in rel.parts:p.error('Unsafe manifest path')
 dest=ROOT/a.path
 if not dest.resolve().is_relative_to(ROOT):p.error('Destination escapes repository')
 if dest.exists():p.error('Destination already exists; refusing overwrite')
 remote=manifest['root']+'/'+a.path
 code='import os,json,sys;s=os.stat(sys.argv[1]);print(json.dumps([s.st_size,s.st_mtime_ns]))'
 command=shlex.join(['python3','-c',code,remote])
 def check():
  got=json.loads(subprocess.check_output(['ssh',a.host,command],text=True))
  if got!=[row['bytes'],row['mtime_ns']]:raise RuntimeError('Remote asset changed; refresh manifest before downloading')
 check();dest.parent.mkdir(parents=True,exist_ok=True);tmp=dest.with_name(dest.name+'.download-'+uuid.uuid4().hex)
 try:
  subprocess.run(['scp',a.host+':'+shlex.quote(remote),str(tmp)],check=True)
  if tmp.stat().st_size!=row['bytes']:raise RuntimeError('Downloaded size mismatch')
  check();os.link(tmp,dest);print('Downloaded '+str(dest))
 finally:
  if tmp.exists():tmp.unlink()
if __name__=='__main__':main()
