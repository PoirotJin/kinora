"""Bounded SQLite backups and isolated restore drills; motion payloads are not included."""
import argparse,fcntl,hashlib,json,os,shutil,sqlite3,time,uuid
from backup_encoding import stored_path, open_stored, compact_databases
from pathlib import Path
from contextlib import closing

FIXED=('prompts.sqlite3','review/accounts.sqlite3','review/reviews.sqlite3','review/api_usage.sqlite3','review/resource_quota.sqlite3','review/custom/jobs.sqlite3','review/retarget/jobs.sqlite3','review/storage/storage.sqlite3','review/storage/capacity.sqlite3','review/gadget/keys.sqlite3','review/gadget/imports.sqlite3')

def sha256_file(path):
    digest=hashlib.sha256()
    with open_stored(path) as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def status(root):
    root=Path(root);result={'tiers':[],'backup':{'configured':False},'coverage':'SQLite metadata only; large motion files and model weights are not backed up. Cold storage is not a backup.'}
    from g2h.storage_tiers import cold_root
    from g2h.cold_storage import LIMIT_BYTES,RESERVE_BYTES
    try:paths=[('hot',root),('cold',cold_root(root))]
    except (ValueError,RuntimeError,OSError) as exc:paths=[('hot',root)];result['cold_error']=str(exc)
    for tier,path in paths:
        if path is None:continue
        disk=shutil.disk_usage(path);ledger=path/'review/storage/capacity.sqlite3';accounted=None;sampled=None
        if ledger.exists():
            with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute('SELECT total,sampled FROM capacity WHERE id=1').fetchone()
                if row:accounted,sampled=row
        result['tiers'].append(dict(tier=tier,free_bytes=disk.free,total_bytes=disk.total,accounted_bytes=accounted,quota_bytes=LIMIT_BYTES,reserve_bytes=RESERVE_BYTES,ledger_stale=sampled is None or time.time()-sampled>300,
            warning=disk.free<max(RESERVE_BYTES*2,disk.total*.1) or (accounted is not None and accounted>=LIMIT_BYTES*.9)))
    config=root/'backup_config.json'
    if config.exists():
        settings=json.loads(config.read_text());target=Path(settings['target']);latest=target/'latest.json'
        result['backup']=dict(configured=True,available=target.is_dir(),retention=settings['keep'],offsite=False,large_motion_files=False)
        if latest.exists():
            info=json.loads(latest.read_text());result['backup'].update(last_success=info['created'],last_id=info['id'],age_hours=(time.time()-info['created'])/3600,restore_verified=info.get('restore_verified',False))
        else:result['backup']['warning']='No completed backup'
        if result['backup'].get('age_hours',0)>36:result['backup']['warning']='Latest backup is older than 36 hours'
        elif not result['backup'].get('restore_verified',False):result['backup']['warning']='Restore verification missing'
    from review_payload_backup import status as payload_status
    result['motion_backup'] = payload_status(root)
    result['coverage'] = 'SQLite metadata backup and separately configured motion-file backup; inspect each backup status. Cold storage is not a backup. Model weights excluded.'
    return result

def databases(root):
    # Two bounded directory levels, never a filesystem-wide scan.
    paths={root/p for p in FIXED if (root/p).is_file()}
    for pattern in ('topics/*/index.sqlite3','review/*.sqlite3','review/*/*.sqlite3','review/gadget/imports/index.sqlite3'):
        for path in root.glob(pattern):
            if path.is_symlink():raise ValueError('Backup refuses symlink databases')
            paths.add(path)
            if len(paths)>100:raise ValueError('Database inventory exceeds 100 files')
    return sorted(paths)

def sqlite_snapshot(source,destination,progress=None):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=3)) as db,closing(sqlite3.connect(destination)) as out:
        # A pinned WAL snapshot prevents continuous logging from restarting the
        # incremental backup after every write, while writers keep running.
        if db.execute('PRAGMA journal_mode').fetchone()[0]=='wal':
            db.execute('BEGIN')
            db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        db.backup(out,pages=256,progress=progress,sleep=.02)
        if db.in_transaction:db.rollback()  # release the source WAL before checking the copy
        if out.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Backup integrity failure')

def _backup(root):
    root=Path(root).resolve();config=json.loads((root/'backup_config.json').read_text());target=Path(config['target'])
    if not target.is_absolute() or target.is_symlink() or not target.is_dir():raise ValueError('Backup target must be an existing absolute directory')
    target=target.resolve();keep=config.get('keep',7)
    if target==root or target.is_relative_to(root) or not 1<=keep<=30:raise ValueError('Invalid backup target/retention')
    if target.stat().st_dev==root.stat().st_dev:raise ValueError('Backup target must be on a different device from hot data')
    files=databases(root);budget=sum(f.stat().st_size for f in files)*2+100_000_000
    max_bytes=config.get('max_bytes',10_000_000_000); reserve=config.get('reserve_bytes',10_000_000_000)
    if type(max_bytes) is not int or not 100_000_000<=max_bytes<=200_000_000_000 or type(reserve) is not int or reserve<10_000_000_000:raise ValueError('Invalid backup budget')
    if budget>max_bytes or shutil.disk_usage(target).free-budget<reserve:raise ValueError('Backup exceeds configured budget or free-space reserve')
    key=time.strftime('db-%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:8];folder=target/(key+'.partial');folder.mkdir(mode=0o700)
    report=dict(id=key,created=time.time(),coverage='Per-database transaction-consistent SQLite snapshots; no global cross-database atomicity, no motion payloads or model weights.',databases=[],restore_verified=False)
    seconds=config.get('deadline_seconds',120)
    if type(seconds) is not int or not 120<=seconds<=14400:raise ValueError('Invalid backup deadline')
    deadline=time.monotonic()+seconds
    def progress(status,remaining,total):
        if time.monotonic()>deadline:raise TimeoutError('Backup deadline exceeded')
    for source in files:
        dest=folder/source.relative_to(root);dest.parent.mkdir(parents=True,exist_ok=True)
        sqlite_snapshot(source,dest,progress)
        os.chmod(dest,0o600);report['databases'].append(dict(path=str(source.relative_to(root)),bytes=dest.stat().st_size,sha256=sha256_file(dest)))
    (folder/'manifest.json').write_text(json.dumps(report,indent=2));folder.rename(target/key)
    restore_drill(target/key,target/(key+'.drill'))
    report['restore_verified']=True;(target/key/'manifest.json').write_text(json.dumps(report,indent=2))
    compact_databases(target/key,report)
    tmp=target/'latest.tmp';tmp.write_text(json.dumps(report));os.replace(tmp,target/'latest.json')
    older=[]
    for f in target.glob('db-*'):
        if f.is_symlink() or f.name.endswith(('.drill','.partial')) or not f.is_dir():continue
        try:
            if json.loads((f/'manifest.json').read_text()).get('restore_verified'):older.append(f)
        except (OSError,ValueError):continue
    older.sort(reverse=True)
    for old in older[keep:]:
        drill=target/(old.name+'.drill')
        if (drill/'restore-evidence.json').is_file():shutil.rmtree(drill)
        shutil.rmtree(old)
    return report

def backup(root):
    from review_payload_backup import atomic_json
    root=Path(root).resolve()
    target=Path(json.loads((root/'backup_config.json').read_text())['target'])
    with (target/'backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            report=_backup(root)
            atomic_json(target/'last_attempt.json',dict(created=time.time(),complete=True,id=report['id'],restore_verified=True))
            return report
        except Exception as exc:
            atomic_json(target/'last_attempt.json',dict(created=time.time(),complete=False,error_type=type(exc).__name__,error=str(exc)))
            raise

def restore(folder,destination):
    folder=Path(folder).resolve();destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Restore destination must be a new isolated directory')
    report=json.loads((folder/'manifest.json').read_text());destination.mkdir(mode=0o700)
    for entry in report['databases']:
        relative=Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('Invalid backup manifest path')
        source=stored_path(folder/relative);dest=destination/relative
        if source.is_symlink() or sha256_file(source)!=entry['sha256']:raise ValueError('Backup hash mismatch')
        dest.parent.mkdir(parents=True,exist_ok=True)
        with open_stored(source) as src, dest.open('xb') as out:shutil.copyfileobj(src,out,1024*1024)
        os.chmod(dest,0o600)
        with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Restored database is inconsistent')
    (destination/'restore-evidence.json').write_text(json.dumps(dict(source=report['id'],verified=len(report['databases']),created=time.time())))
    return dict(verified=len(report['databases']),destination=str(destination))

def restore_drill(folder,destination):
    folder,destination=Path(folder),Path(destination)
    result=restore(folder,destination)
    # Keep proof alongside the immutable backup; the temporary restored database
    # is not another retained backup and must not double seven-day storage.
    shutil.copy2(destination/'restore-evidence.json',folder/'restore-evidence.json')
    shutil.rmtree(destination)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['status','backup','restore']);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--destination',type=Path)
    args=parser.parse_args();print(json.dumps(status(args.root) if args.operation=='status' else backup(args.root) if args.operation=='backup' else restore(args.root,args.destination),indent=2))
