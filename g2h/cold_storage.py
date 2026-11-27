"""Lossless, independently compressed PKL entries with portable metadata.

Only trusted MotionFarm packages may be unpickled. Archive readers never extract
ZIP paths onto disk. Original per-record SHA256 remains authoritative.
"""
import base64
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import zipfile

LIMIT_BYTES=800_000_000_000
RESERVE_BYTES=10_000_000_000


class WriterBusy(RuntimeError):
    """The generator holds the shard lock; this attempt has not packaged data."""


def usage(root, seconds=3, max_files=250000):
    """Metadata-only walk confined to the farm. Fail closed if the budget expires."""
    root=Path(root);deadline=time.monotonic()+seconds;total=0;files=0;hot=0;cold=0;exports=0
    def fail(error):raise error
    for folder,dirs,names in os.walk(root,onerror=fail,followlinks=False):
        if time.monotonic()>deadline:raise RuntimeError('容量统计超出预算，暂停生产')
        if any((Path(folder)/d).is_symlink() for d in dirs):raise RuntimeError('数据目录包含符号链接，无法可靠计算容量')
        for name in names:
            path=Path(folder)/name
            try:
                stat=path.lstat()
            except FileNotFoundError:continue
            if path.is_symlink():raise RuntimeError('数据目录包含符号链接，无法可靠计算容量')
            size=stat.st_size;total+=size;files+=1
            if name.startswith('motions_') and name.endswith('.pkl'):hot+=size
            is_export='exports' in path.relative_to(root).parts
            if name.endswith('.mf.zip') and not is_export:cold+=size
            if is_export:exports+=size
            if files>max_files or time.monotonic()>deadline:raise RuntimeError('容量统计超出预算；暂停生产，需检查存储索引规模')
    return dict(total=total,hot=hot,cold=cold,exports=exports,files=files,limit=LIMIT_BYTES,reserve=RESERVE_BYTES,sampled_at=time.time())


def guard(root, additional=0):
    from g2h.capacity import check
    return check(root, additional)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS cold_shards(number INTEGER PRIMARY KEY,archive_bytes INTEGER NOT NULL,created REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS records_shard ON records(shard,offset)')
    db.commit()


def archive_path(folder,number):
    from g2h.storage_tiers import cold_root
    folder=Path(folder);name='motions_%06d.mf.zip'%number
    legacy=folder/name
    if legacy.exists():return legacy
    cold=cold_root(folder.parent.parent)
    return cold/'topics'/folder.name/name if cold is not None else legacy


def read_payload(folder,shard,offset,size,digest,key):
    raw=Path(folder)/('motions_%06d.pkl'%shard)
    try:
        with raw.open('rb') as stream:stream.seek(offset);payload=stream.read(size)
    except FileNotFoundError:
        with zipfile.ZipFile(archive_path(folder,shard)) as archive:
            member=archive.getinfo('motions/'+key+'.pkl')
            if member.file_size!=size:raise ValueError('归档记录长度不符')
            try:
                payload=archive.read(member)
            except RuntimeError as error:
                raise OSError('无法读取冷归档中的动作记录') from error
    if len(payload)!=size or hashlib.sha256(payload).hexdigest()!=digest:raise ValueError('动作记录校验失败：'+key)
    return payload


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def package(root,topic,number,destination=None,min_age_days=7):
    """One bounded shard per invocation; writer lock excludes collection/rotation.

    destination=None migrates to cold. Explicit export copies a consistent hot
    shard without changing its storage tier. Commit index before removing hot data.
    """
    root=Path(root);folder=root/'topics'/topic;raw=folder/('motions_%06d.pkl'%number)
    with (folder/'writer.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise WriterBusy('该主题正在写入，本次打包未执行；请稍后点击“重试”') from None
        with closing(sqlite3.connect(folder/'index.sqlite3',timeout=2)) as db:
            initialize(db)
            row=db.execute('SELECT bytes FROM shards WHERE number=?',(number,)).fetchone()
            if not row or row[0]<=0:raise ValueError('分包为空或不存在')
            existing=db.execute('SELECT archive_bytes FROM cold_shards WHERE number=?',(number,)).fetchone()
            published=archive_path(folder,number)
            if existing or (not destination and published.exists()):
                if not published.exists():raise RuntimeError('归档文件缺失')
                # Recover either side of publish -> SQL commit -> unlink. Never
                # trust an orphan filename alone before discarding the hot copy.
                if raw.exists() or not existing:
                    if row[0]>1_000_000_000:raise RuntimeError('单包超过 1 GB，需先拆分')
                    records=db.execute('SELECT id,bytes,sha256 FROM records WHERE shard=? ORDER BY offset',(number,)).fetchall()
                    with zipfile.ZipFile(published) as archive:
                        for key,size,digest in records:
                            member=archive.getinfo('motions/'+key+'.pkl')
                            if member.file_size!=size or hashlib.sha256(archive.read(member)).hexdigest()!=digest:
                                raise RuntimeError('已发布归档与当前索引不符，保留热数据')
                    with db:db.execute('INSERT OR REPLACE INTO cold_shards VALUES(?,?,?)',(number,published.stat().st_size,time.time()))
                    if raw.exists():
                        from g2h.capacity import status,release
                        status(root);removed=raw.stat().st_size
                        raw.unlink();sync_dir(folder);release(root,removed)
                return dict(path=str(published),already=True)
            if not raw.exists() or raw.stat().st_size<row[0]:raise RuntimeError('原始分包缺失或截断')
            if row[0]>1_000_000_000:raise RuntimeError('单包超过 1 GB，需先拆分')
            records=db.execute('SELECT id,offset,bytes,sha256 FROM records WHERE shard=? ORDER BY offset',(number,)).fetchall()
            if not destination and time.time()-raw.stat().st_mtime<min_age_days*86400:raise RuntimeError('尚未达到冷归档年龄')
            target=Path(destination) if destination else archive_path(folder,number)
            if target.exists():raise RuntimeError('目标文件已存在，请检查此前未完成的归档')
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary=target.with_suffix(target.suffix+'.partial')
            if temporary.exists():raise RuntimeError('发现未完成的归档临时文件，请检查后清理')
            with closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True)) as catalog, closing(sqlite3.connect((root/'review/reviews.sqlite3').as_uri()+'?mode=ro',uri=True)) as reviews:
                from g2h.farm_index import short_id
                entries=[]
                for key,offset,size,digest in records:
                    link=catalog.execute('SELECT number,text_id,seed FROM motion_links WHERE task_id=?',(key,)).fetchone()
                    if not link:raise RuntimeError('动作缺少文本索引，暂不能打包')
                    mid=short_id(link[0]);review=reviews.execute('SELECT body FROM latest WHERE motion_id=?',(mid,)).fetchone()
                    review=json.loads(review[0]) if review else None
                    if not destination and (not review or review['status'] not in ('accepted','rejected')):raise RuntimeError('整包需全部完成审核（通过或不通过）才能冷归档')
                    prompt=json.loads(catalog.execute('SELECT item FROM prompts WHERE id=?',(link[1],)).fetchone()[0])
                    vector=None;embedding_config=None
                    if catalog.execute("SELECT 1 FROM sqlite_master WHERE name='embeddings'").fetchone():
                        found=catalog.execute('SELECT vector FROM embeddings WHERE text_id=?',(link[1],)).fetchone()
                        if found:vector=base64.b64encode(found[0]).decode('ascii')
                        if catalog.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_config'").fetchone():
                            cfg=catalog.execute('SELECT model,dimensions FROM embedding_config WHERE singleton=1').fetchone()
                            if cfg:embedding_config=dict(model=cfg[0],dimensions=cfg[1],dtype='float32',encoding='base64')
                    entries.append(dict(task_id=key,motion_id=mid,text_id=link[1],topic=topic,seed=link[2],prompt=prompt,review=review,
                        embedding=vector,embedding_config=embedding_config,bytes=size,sha256=digest,member='motions/'+key+'.pkl'))
            manifest=dict(format='MotionFarm-package',version=1,compression='ZIP_LZMA',topic=topic,shard=number,
                created_at=time.time(),raw_bytes=row[0],records=entries,review_note='审核结论为打包时快照；平台内最新审核仍在 reviews.sqlite3')
            manifest_bytes=json.dumps(manifest,ensure_ascii=False).encode()
            from g2h.capacity import reservation
            raw_before=raw.stat().st_size
            # Independent ZIP entries have metadata and compression overhead;
            # reserve the uncompressed payload plus conservative entry overhead.
            from g2h.storage_tiers import cold_root
            cold=cold_root(root)
            external=cold is not None and target.resolve().is_relative_to(cold)
            estimate=row[0]+len(manifest_bytes)+len(entries)*4096+10_000_000
            budget=reservation(root,65536 if external else estimate,
                               'archive:'+topic+':'+str(number),reclaim=not destination)
            cold_budget=None
            try:
                if external:cold_budget=reservation(cold,estimate,'cold-archive:'+topic+':'+str(number),reclaim=True)
                with zipfile.ZipFile(temporary,'x',compression=zipfile.ZIP_LZMA,allowZip64=True) as archive:
                    archive.writestr('manifest.json',manifest_bytes)
                    archive.writestr('README.txt','MotionFarm package v1\nEach motions/*.pkl is one losslessly preserved Python pickle record. Load only trusted files.\nmanifest.json maps motion_id, text_id, topic, prompt, vector and review snapshot.\n')
                    with raw.open('rb') as stream:
                        for entry,(_,offset,size,digest) in zip(entries,records):
                            stream.seek(offset);payload=stream.read(size)
                            if hashlib.sha256(payload).hexdigest()!=digest:raise RuntimeError('原分包 SHA256 校验失败')
                            archive.writestr(entry['member'],payload)
                # Verify all compressed records before changing the authoritative index.
                with zipfile.ZipFile(temporary) as archive:
                    for entry in entries:
                        if hashlib.sha256(archive.read(entry['member'])).hexdigest()!=entry['sha256']:raise RuntimeError('压缩包回读校验失败')
                with temporary.open('rb') as stream:os.fsync(stream.fileno())
                if not destination and temporary.stat().st_size>=row[0]:
                    temporary.unlink();return dict(skipped=True,reason='压缩后未节省空间，保留热数据')
                os.replace(temporary,target);sync_dir(target.parent)
                if not destination:
                    with db:db.execute('INSERT INTO cold_shards VALUES(?,?,?)',(number,target.stat().st_size,time.time()))
                    raw.unlink();sync_dir(folder)
                return dict(path=str(target),raw_bytes=row[0],archive_bytes=target.stat().st_size,records=len(entries))
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            finally:
                try:
                    paths=(raw,) if external else (raw,target,temporary)
                    delta=sum(p.stat().st_size for p in paths if p.exists())-raw_before
                    budget.commit(delta+65536)
                finally:
                    budget.__exit__(None,None,None)
                    if cold_budget is not None:
                        try:cold_budget.commit(sum(p.stat().st_size for p in (target,temporary) if p.exists())+65536)
                        finally:cold_budget.__exit__(None,None,None)
