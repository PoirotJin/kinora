"""One-shot lossless backup compaction, under both existing writer locks."""
import argparse
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import time
from backup_encoding import compact, compact_databases, stored_path


def entries(path):
    decoder=json.JSONDecoder()
    with Path(path).open() as stream:
        buf=stream.read(1024*1024)
        start=buf.index('"files": [')+len('"files": [')
        buf=buf[start:];pos=0
        while True:
            while pos<len(buf) and buf[pos] in ' \r\n\t,':pos+=1
            if pos<len(buf) and buf[pos]==']':return
            try:entry,end=decoder.raw_decode(buf,pos)
            except json.JSONDecodeError:
                more=stream.read(1024*1024)
                if not more:raise
                buf=buf[pos:]+more;pos=0;continue
            pos=end
            yield entry


def run(root):
    root=Path(root)
    payload=Path(json.loads((root/'payload_backup_config.json').read_text())['target'])
    database=Path(json.loads((root/'backup_config.json').read_text())['target'])
    progress=payload/'compaction-progress.json'
    result=dict(started=time.time(),complete=False,database_saved_bytes=0,payload_saved_bytes=0,database_snapshots=0,payload_objects=0)
    def publish():
        temporary=progress.with_suffix('.tmp');temporary.write_text(json.dumps(result,indent=2));temporary.replace(progress)
        print(json.dumps(result),flush=True)
    with ExitStack() as stack:
        for target in (database,payload):
            lock=stack.enter_context((target/'backup.lock').open('a'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            publish()
            for folder in sorted(database.glob('db-*')):
                if folder.is_symlink() or folder.name.endswith(('.partial','.drill')):continue
                manifest=folder/'manifest.json'
                if not manifest.is_file():continue
                report=json.loads(manifest.read_text())
                if not report.get('restore_verified'):continue
                result['database_saved_bytes']+=compact_databases(folder,report)
                result['database_snapshots']+=1;publish()
            seen=set();last=time.monotonic()
            for entry in entries(payload/'latest.json'):
                if not entry['path'].endswith('.json'):continue
                key=entry['sha256']
                if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid digest')
                if key in seen:continue
                seen.add(key)
                raw=payload/'objects'/key
                if raw.exists():
                    result['payload_saved_bytes']+=compact(raw,key)
                else:
                    # Reruns reverify already compressed objects as well.
                    compact(raw,key)
                result['payload_objects']+=1
                if time.monotonic()-last>=20:
                    publish();last=time.monotonic()
            result['complete']=True;result['finished']=time.time();publish()
        except BaseException as error:
            result['error']=str(error);publish();raise
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    run(parser.parse_args().root)
