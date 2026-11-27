"""Reject oversized artifacts, runtime state and recognizable credentials in Git blobs."""
import argparse,re,subprocess,sys
from pathlib import PurePosixPath
LIMIT=1024*1024
BLOCKED={'outputs','results','backups','var','ckpts','checkpoints','.vendor','.retarget-venv','.venv','venv','node_modules','benchmarks'}
SUFFIX={'.npz','.npy','.pkl','.pickle','.pt','.pth','.ckpt','.safetensors','.bin','.zip','.tar','.gz','.7z','.sqlite','.sqlite3','.db','.log','.mp4','.mov','.avi','.so','.dll','.dylib','.pem','.key'}
# Keep this exception in sync with the numeric regression fixture in .gitignore.
SEMANTIC_FIXTURE='scripts/fixtures/m000096vn-semantic.npz'
def main():
 p=argparse.ArgumentParser();p.add_argument('--tracked',action='store_true');p.parse_args()
 entries=subprocess.check_output(['git','ls-files','--stage','-z']).split(b'\0');errors=[];total=0
 for entry in entries:
  if not entry:continue
  meta,rawname=entry.split(b'\t',1);mode,oid,stage=meta.split();name=rawname.decode();path=PurePosixPath(name)
  if mode not in (b'100644',b'100755') or stage!=b'0':errors.append(name+': non-regular or unresolved entry');continue
  size=int(subprocess.check_output(['git','cat-file','-s',oid.decode()]));total+=size
  if size>LIMIT:errors.append(name+': exceeds 1 MiB');continue
  blocked_binary=path.suffix.lower() in SUFFIX and name!=SEMANTIC_FIXTURE
  if path.parts[0] in BLOCKED or blocked_binary or path.name.startswith('.env') and path.name!='.env.example':errors.append(name+': runtime / binary / credential path');continue
  data=subprocess.check_output(['git','cat-file','blob',oid.decode()])
  if any(re.search(pattern,data) for pattern in [rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',rb'\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{25,}',rb'\bsk-[A-Za-z0-9_-]{24,}']):errors.append(name+': credential pattern')
 if errors:print('\n'.join(errors),file=sys.stderr);return 1
 print('Repository policy OK: %d files, %d bytes; every file <= 1 MiB.'%(len(entries)-1,total));return 0
if __name__=='__main__':raise SystemExit(main())
