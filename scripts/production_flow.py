"""Durable per-topic production cursor. Replays use deterministic agent outboxes."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

def setup(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS runtime(
      job TEXT PRIMARY KEY,round INTEGER DEFAULT 0,position INTEGER DEFAULT 0,
      phase TEXT DEFAULT 'prompts',seeds TEXT,restarts INTEGER DEFAULT 0,restarted REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS steps(job TEXT,round INTEGER,topic TEXT,phase TEXT,
      status TEXT,updated REAL,PRIMARY KEY(job,round,topic,phase));
      CREATE TABLE IF NOT EXISTS watermarks(topic TEXT PRIMARY KEY,paused INTEGER);
    ''')

def pending(root,topic):
    path=root/'topics'/topic/'index.sqlite3'
    if not path.exists():return 0
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        return db.execute("SELECT count(*) FROM tasks WHERE state='pending'").fetchone()[0]


def ready_capacity(p,job,reader):
    """Temporary ledger reconciliation pauses admission, never bypasses guards."""
    while not p.cancelled(job):
        snapshot=reader(p.root)
        if snapshot.get('ready',True) and not snapshot.get('stale',True):return snapshot
        p.update(job,stage='storage',message='容量账本正在后台核对，暂停新的写入；核对完成后自动继续。')
        for _ in range(5):
            if p.cancelled(job):return None
            time.sleep(1)
    return None

def advance(p,job,round_no,position,phase):
    with closing(p.db()) as db,db:
        db.execute('UPDATE runtime SET round=?,position=?,phase=? WHERE job=?',(round_no,position,phase,job))

def step(p,job,r,topic,phase,status):
    with closing(p.db()) as db,db:
        db.execute('INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?,?)',(job,r,topic,phase,status,time.time()))

def execute(p,job_id):
    from g2h import observability
    observability.initialize(p.root)
    with observability.process_context('production',job_id):
        return _execute(p,job_id)

def _execute(p,job_id):
    from review_production import PROJECT,ACTIVE
    from g2h import observability
    with closing(p.db()) as db:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not job or job['status'] not in ACTIVE:return
    settings=p.validate(json.loads(job['settings']))
    pid = os.getpid()
    with closing(p.db()) as db, db:
        # A concurrent cancellation can release the queue slot before this worker starts.
        claimed = db.execute(
            "UPDATE jobs SET pid=?,identity=?,updated=?,status=CASE WHEN stop=0 THEN 'running' ELSE status END "
            "WHERE id=? AND status IN ('queued','running','stopping')",
            (pid, p.identity(pid), time.time(), job_id),
        ).rowcount
        if not claimed:
            return
        db.execute('INSERT OR IGNORE INTO runtime(job,seeds) VALUES(?,?)',
                   (job_id, json.dumps([secrets.randbelow(2**32) for _ in range(settings['k'])])))
    try:
        with (p.root/'gpu_worker.lock').open('a') as lock:
            # Merely opening the descriptor does not reserve the GPU. API and
            # storage waits must allow ready custom work to use the warm model.
            while not p.cancelled(job_id):
                fcntl.flock(lock,fcntl.LOCK_UN)
                with closing(p.db()) as db:cursor=db.execute('SELECT * FROM runtime WHERE job=?',(job_id,)).fetchone()
                r,pos,phase=cursor['round'],cursor['position'],cursor['phase']
                if not settings['continuous'] and r>=settings['rounds']:break
                topic=settings['topics'][pos];folder=p.root/'topics'/topic
                from production_health import topic_wait
                from production_supply import asynchronous
                background_supply=asynchronous(p.root,settings)
                if phase=='prompts' and (not pending(p.root,topic) or (background_supply and pending(p.root,topic)<32)):
                    from production_resources import replenish
                    added=0
                    # A small per-topic ready buffer hides API/catalog lock waits
                    # without increasing the GPU batch size or custom-job turn.
                    for buffer_slot in range(4 if background_supply else 1):
                        if background_supply and pending(p.root,topic)>=32:break
                        key=job_id+'_'+str(r)+'_'+topic+('_buffer'+str(buffer_slot) if background_supply else '')
                        n=replenish(p.root,topic,key,settings['duration'])
                        if not n and background_supply:
                            from production_rotation import replenish as rotate
                            n=rotate(p.root,topic,key,settings['duration'])
                        if not n:break
                        added+=n
                    if added:
                        step(p,job_id,r,topic,phase,'approved_variant_backfill')
                        advance(p,job_id,r,pos,'motions')
                        p.update(job_id,stage='draining',topic=topic,message='生成已有文本审核描述的 %d 个随机变体，每日轮换有采样上限；常规质量检查仍有效'%added)
                        continue
                if phase=='prompts' and background_supply and not pending(p.root,topic):
                    step(p,job_id,r,topic,phase,'waiting_background_supply')
                    pos+=1
                    if pos==len(settings['topics']):r+=1;pos=0
                    advance(p,job_id,r,pos,'prompts')
                    p.update(job_id,stage='backoff',topic=topic,message='等待后台新描述供应；继续检查其他主题，历史库存耗尽不会结束持续任务。')
                    time.sleep(1)
                    continue
                if phase=='prompts' and not background_supply and topic_wait(p,topic)>0 and not pending(p.root,topic):
                    step(p,job_id,r,topic,phase,'topic_cooldown')
                    pos+=1
                    if pos==len(settings['topics']):r+=1;pos=0
                    advance(p,job_id,r,pos,'prompts')
                    p.update(job_id,stage='backoff',topic=topic,message='主题连续无新增，冷却 1 小时；继续其他主题。费用保护仍有效。')
                    # Avoid a busy loop when every topic is cooling down.
                    for _ in range(10):
                        if p.cancelled(job_id):break
                        time.sleep(1)
                    continue

                from review_storage import Storage
                from g2h.cold_storage import guard
                from g2h.capacity import status as capacity_status
                storage=Storage(p.root,p.config)
                p.update(job_id,stage='storage',topic=topic,message='第 %d 轮：容量与归档检查'%(r+1))
                with observability.span(p.root,'storage_maintenance',topic):
                    snapshot=ready_capacity(p,job_id,capacity_status)
                    if snapshot is None:break
                    storage.recover();storage.automatic(storage.inventory(),snapshot)
                while not p.cancelled(job_id):
                    with closing(storage.db()) as adb:busy=adb.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone()
                    if not busy:break
                    time.sleep(1);storage.recover();p.update(job_id,stage='storage')
                if p.cancelled(job_id):break
                guard(p.root)
                step(p,job_id,r,topic,phase,'running')
                if phase=='prompts':
                    count=pending(p.root,topic)
                    high=settings.get('queue_high',16);low=settings.get('queue_low',4)
                    with closing(p.db()) as db,db:
                        row=db.execute('SELECT paused FROM watermarks WHERE topic=?',(topic,)).fetchone()
                        paused=bool(row and row[0])
                        if count+settings['k']>high:paused=True
                        if count<=low:paused=False
                        # Existing backlog always takes precedence over new API work.
                        from production_resources import profile
                        skip=background_supply or paused or count>low or count+settings['k']>high or (count>0 and profile(p.root)['backfill_variants']>0)
                        db.execute('INSERT OR REPLACE INTO watermarks VALUES(?,?)',(topic,int(paused)))
                    if skip:p.update(job_id,stage='draining',topic=topic,message='%s 积压 %d 个动作，暂停补词并优先生成'%(topic,count))
                    else:
                        p.update(job_id,stage='prompts',topic=topic,message='第 %d 轮：%s 生成 / 审核 / 向量化，断点可恢复'%(r+1,topic))
                        args=[str(PROJECT/'scripts/qwen_motion_agent.py'),'--root',str(p.root),'--config',str(p.config),
                            '--topic',topic,'--per-topic','1','--rounds','1','--duration',str(settings['duration']),
                            '--work-key',job_id+'_'+str(r)+'_'+topic,'--enqueue','--seeds',*[str(x) for x in json.loads(cursor['seeds'])]]
                        if not p.command(job_id,args,None):break
                    step(p,job_id,r,topic,phase,'skipped_backlog' if skip else 'done')
                    advance(p,job_id,r,pos,'motions')
                else:
                    with observability.span(p.root,'gpu_wait') as waiting:
                        while True:
                            try:
                                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                            except BlockingIOError:
                                if p.cancelled(job_id):
                                    waiting.status='cancelled';break
                                p.update(job_id,stage='waiting_gpu',message='等待当前 GPU 小批次完成')
                                time.sleep(1)
                    if p.cancelled(job_id):break
                    p.update(job_id,stage='motions',topic=topic,message='第 %d 轮：%s 动作生成与入库'%(r+1,topic))
                    if pending(p.root,topic):
                        args=[str(PROJECT/'scripts/motion_collector.py'),'--root',str(folder),'run','--batch-size',(str(__import__('production_resources').batch_size(p.root)) if os.environ.get('MOTIONFARM_INFER_FUSED')=='1' or os.environ.get('MOTIONFARM_INFER_PERSISTENT')=='1' else '2'),
                            '--stage-timeout','1800','--max-bytes','256000000','--max-batches','2']
                        if not p.command(job_id,args,lock.fileno(),folder):break
                        with closing(sqlite3.connect(folder/'index.sqlite3')) as db:
                            if db.execute("SELECT 1 FROM tasks WHERE state='failed' LIMIT 1").fetchone():raise RuntimeError(topic+' 有失败任务，需要检查')
                    if not p.cancelled(job_id):
                        try:
                            from review_custom import Custom
                            Custom(p.root).run_batch(lock.fileno(),lambda:p.update(job_id,stage='custom',message='批次间隙处理最多 2 个定制动作，随后恢复自动生产'))
                        except Exception as custom_error:
                            print('Custom scheduling unavailable:',type(custom_error).__name__,flush=True)
                    # Finite batches drain their topic; continuous runs rotate.
                    if not settings['continuous'] and pending(p.root,topic):continue
                    step(p,job_id,r,topic,phase,'done')
                    pos+=1
                    if pos==len(settings['topics']):r+=1;pos=0
                    advance(p,job_id,r,pos,'prompts')
            stopped=p.cancelled(job_id)
            p.update(job_id,status='stopped' if stopped else 'completed',stage='stopped' if stopped else 'completed',
                topic='',message='已停止，断点与已完成数据保留' if stopped else '本批生产完成')
    except Exception as exc:
        print('Production failed:',type(exc).__name__,str(exc),flush=True)
        p.update(job_id,status='failed',stage='failed',topic='',message=str(exc) if isinstance(exc,RuntimeError) else '生产异常，断点已保存；请查看日志')
