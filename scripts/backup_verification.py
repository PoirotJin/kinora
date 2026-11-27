"""Bounded restore drill and snapshot-reference audit; independent from cold tier."""
from contextlib import closing
import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from review_payload_backup import restore, atomic_json, sample_entries, safe_path


def audit_references(metadata, payload, max_shards=500):
    """Check bounded shard references in metadata snapshot against payload snapshot."""
    files={(e['tier'],e['path']) for e in payload['files']}
    result=dict(checked_shards=0,missing=[],truncated=False,scope='Metadata-snapshot shard references vs payload manifest; snapshots are not globally atomic.')
    paths=sorted(Path(metadata).glob('topics/*/index.sqlite3'))
    if len(paths)>100:result['truncated']=True
    for p in paths[:100]:
        with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute('SELECT DISTINCT shard FROM records LIMIT ?', (max_shards+1,)).fetchall()
        if len(rows)>max_shards:result['truncated']=True
        for (shard,) in rows[:max_shards]:
            stem='topics/'+p.parent.name+'/motions_%06d'%shard
            result['checked_shards']+=1
            if not any((tier,stem+suffix) in files for tier,suffix in [('hot','.pkl'),('hot','.mf.zip'),('cold','.mf.zip')]):
                result['missing'].append(dict(topic=p.parent.name,shard=shard))
    result['consistent_in_scope']=not result['missing'] and not result['truncated']
    return result


def verify(root, sample=3, max_bytes=512_000_000):
    root=Path(root).resolve()
    config=json.loads((root/'payload_backup_config.json').read_text());target=Path(config['target']).resolve()
    latest=json.loads((target/'latest.json').read_text())
    # Mirror the restore sampler so the IO bound is checked before any copy.
    selected=sample_entries(latest,sample,latest['id'])
    if sum(e['bytes'] for e in selected)>max_bytes:raise ValueError('Restore sample exceeds 512 MB budget; adjust sample explicitly')
    dest=target/('drill-'+uuid.uuid4().hex)
    result=dict(created=time.time(),snapshot=latest['id'],complete=False,sample_bytes=sum(e['bytes'] for e in selected))
    try:
        result['restore']=restore(target,latest['id'],dest,sample,sample_seed=latest['id'])
        cfg=json.loads((root/'backup_config.json').read_text());db_target=Path(cfg['target'])
        linked=latest.get('metadata_snapshot')
        if linked:
            meta_manifest=safe_path(db_target,linked['id'])/'manifest.json'
            if not meta_manifest.is_file():
                result['metadata_snapshot']=linked['id']
                result['metadata_created']=linked.get('created')
                result['payload_created']=latest['created']
                result['metadata_missing']=True
                result['metadata_fresh']=False
                result['complete']=False
                atomic_json(target/'verification.json',result)
                return result
            db_latest=json.loads(meta_manifest.read_text())
        else:
            db_latest=json.loads((db_target/'latest.json').read_text())
        result['metadata_snapshot']=db_latest['id'];result['metadata_created']=db_latest['created'];result['payload_created']=latest['created']
        result['references']=audit_references(db_target/db_latest['id'],latest)
        result['metadata_fresh']=time.time()-db_latest['created']<=36*3600
        result['complete']=result['references']['consistent_in_scope'] and result['metadata_fresh']
        atomic_json(target/'verification.json',result)
        return result
    except Exception as exc:
        result['error_type']=type(exc).__name__;atomic_json(target/'verification.json',result);raise
    finally:
        # Only this invocation's randomly named isolated drill is removed.
        if dest.is_dir():shutil.rmtree(dest)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    result=verify(args.root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not result['complete']:raise SystemExit(1)
