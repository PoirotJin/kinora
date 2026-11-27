"""Resumable, single-threaded direction backfill. No GPU or model/API calls."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import pickle
import signal
import shutil
import sqlite3
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h import direction_index as index
from g2h.motion_semantics import VERSION, requirements, for_record


def readonly(p):
    return sqlite3.connect(Path(p).resolve().as_uri()+'?mode=ro',uri=True,timeout=.15)


def busy(root):
    """Probe and immediately release; never hold the generator's lock while reading."""
    if shutil.disk_usage(root).free < 10_000_000_000:return '磁盘余量不足，补检暂停'
    p=root/'gpu_worker.lock'
    if p.exists():
        with p.open('rb') as f:
            try:fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
            except BlockingIOError:return '正常生成正在使用 GPU，补检暂停'
    if os.getloadavg()[0] > max(1.,(os.cpu_count() or 1)*.7):return '服务器负载升高，补检暂停'
    return ''


def text_only(item):
    """No measurable constraints: safely classify as review without reading arrays."""
    if item.get('segments'):return None
    found,warnings=requirements(item['text'])
    original=item.get('source',{}).get('original_description','')
    if found or (original and requirements(original)[0]):return None
    return dict(version=VERSION,status='review',checks=[],reasons=[],warnings=warnings,
                evidence='text_only_no_supported_constraint',scope='未识别可可靠检查的方向约束，动作语义待人工审核')


def batch(root, topic, budget=16, stop=None, mib_per_second=8):
    stop=stop or threading.Event()
    with closing(index.connect(root)) as output,closing(readonly(root/'topics'/topic/'index.sqlite3')) as source,closing(readonly(root/'prompts.sqlite3')) as catalog:
        progress=output.execute('SELECT cursor,upper_bound FROM progress WHERE topic=? AND version=?',(topic,VERSION)).fetchone()
        if not progress:
            upper=source.execute('SELECT coalesce(max(rowid),0) FROM records').fetchone()[0]
            total=source.execute('SELECT total FROM record_count WHERE id=1').fetchone()[0]
            with output:output.execute('INSERT INTO progress(topic,version,upper_bound,target) VALUES(?,?,?,?)',(topic,VERSION,upper,total))
            progress=(0,upper)
        rows=source.execute('SELECT rowid,id,shard,offset,bytes,sha256 FROM records WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',(*progress,budget)).fetchall()
        processed=0
        for rowid,key,shard,offset,size,digest in rows:
            if stop.is_set() or busy(root):break
            start=time.monotonic()
            link=catalog.execute('SELECT m.number,p.item FROM motion_links m JOIN prompts p ON p.id=m.text_id WHERE m.task_id=?',(key,)).fetchone()
            if not link:
                # Missing central link is retryable: never advance past it silently.
                raise ValueError('Missing catalog link for '+key)
            number,raw=link
            existing=output.execute('SELECT source_sha256 FROM results WHERE number=? AND version=?',(number,VERSION)).fetchone()
            report=None;read_bytes=0;error=0
            if not existing or existing[0]!=digest:
                item=json.loads(raw);report=text_only(item)
                if report is None:
                    try:
                        from g2h.cold_storage import read_payload
                        record=pickle.loads(read_payload(root/'topics'/topic,shard,offset,size,digest,key))
                        if record['id']!=key:raise ValueError('Motion identity mismatch')
                        report=for_record(record);read_bytes=size
                    except (OSError,ValueError,KeyError,EOFError,pickle.UnpicklingError) as exc:
                        error=1
                        # Unreadable is never a semantic pass/fail; keep diagnostic.
                        report=dict(version=VERSION,status='review',checks=[],reasons=[],warnings=['数据读取或检查失败，需复核：'+type(exc).__name__],evidence='read_error')
                        read_bytes=size
            with output:
                if report:index.put(output,number,digest,report)
                output.execute('UPDATE progress SET cursor=?,scanned=scanned+1,errors=errors+? WHERE topic=? AND version=?',(rowid,error,topic,VERSION))
            processed+=1
            # Cap bytes AND records, including cheap text-only classifications.
            delay=max(.02,read_bytes/(mib_per_second*1024**2))-(time.monotonic()-start)
            if delay>0:stop.wait(delay)
        return processed


def run(args):
    root=args.root.resolve();stop=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stop.set())
    os.nice(15)
    with closing(index.connect(root)) as db:
        pass
    with (root/'review/direction-backfill.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with closing(readonly(root/'prompts.sqlite3')) as db:
            topics=[r[0] for r in db.execute('SELECT topic FROM topic_counts ORDER BY topic') if (root/'topics'/r[0]/'index.sqlite3').exists()]
        for topic in topics:batch(root,topic,0,stop,args.mib_per_second)
        while not stop.is_set():
            reason=busy(root);state='paused' if reason else 'running'
            with closing(index.connect(root)) as db,db:
                db.execute('INSERT OR REPLACE INTO worker VALUES(1,?,?,?)',(state,time.time(),reason))
            if reason:stop.wait(5);continue
            count=0
            for topic in topics:
                if stop.is_set():break
                count+=batch(root,topic,args.batch_size,stop,args.mib_per_second)
            with closing(index.connect(root)) as db:
                remaining=db.execute('SELECT count(*) FROM progress WHERE version=? AND cursor<upper_bound',(VERSION,)).fetchone()[0]
            if not remaining:
                with closing(index.connect(root)) as db,db:
                    db.execute("INSERT OR REPLACE INTO worker VALUES(1,'complete',?,'历史快照补检完成')",(time.time(),))
                if not args.follow:return
                stop.wait(60)
                for topic in topics:
                    with closing(readonly(root/'topics'/topic/'index.sqlite3')) as src,closing(index.connect(root)) as out,out:
                        upper=src.execute('SELECT coalesce(max(rowid),0) FROM records').fetchone()[0]
                        total=src.execute('SELECT total FROM record_count WHERE id=1').fetchone()[0]
                        out.execute('UPDATE progress SET upper_bound=?,target=? WHERE topic=? AND version=?',(upper,total,topic,VERSION))
            if args.once:return
            stop.wait(.25)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--batch-size',type=int,default=16);p.add_argument('--mib-per-second',type=float,default=8)
    p.add_argument('--once',action='store_true');p.add_argument('--follow',action='store_true');args=p.parse_args()
    if not 1<=args.batch_size<=64 or not .1<=args.mib_per_second<=16:p.error('Unsafe resource limits')
    run(args)
