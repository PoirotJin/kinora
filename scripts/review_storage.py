"""Low-priority bounded archive worker and storage inventory for the web UI."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from g2h.cold_storage import package,archive_path
from g2h.capacity import status as capacity_status,Reconciler,worker_exited
from g2h.storage_tiers import cold_root


class Storage:
    def __init__(self,root,config):
        self.root,self.config=Path(root).resolve(),Path(config).resolve()
        self.folder=self.root/'review/storage';self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'storage.sqlite3';self.stop=threading.Event();self.snapshot={};self.last_auto=0
        self.reconciler=Reconciler(self.root)
        self.cold=cold_root(self.root)
        self.cold_reconciler=Reconciler(self.cold) if self.cold else None
        with closing(self.db()) as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,topic TEXT,shard INTEGER,kind TEXT,status TEXT,
                    created REAL,updated REAL,pid INTEGER,identity TEXT,result TEXT,message TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS single_worker ON jobs((1)) WHERE status='running';
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),days INTEGER);
                INSERT OR IGNORE INTO settings VALUES(1,7);
            ''')

    def db(self):
        db=sqlite3.connect(self.path,timeout=2);db.row_factory=sqlite3.Row;return db

    def age(self):
        with closing(self.db()) as db:return db.execute('SELECT days FROM settings WHERE id=1').fetchone()[0]

    def configure(self,value):
        days=value.get('days')
        if days not in (7,30,365000) or type(days) is not int:raise ValueError('请选择 7 天、30 天或仅容量压力时归档')
        with closing(self.db()) as db, db:db.execute('UPDATE settings SET days=? WHERE id=1',(days,))
        return {'ok':True}

    def recover(self):
        with closing(self.db()) as db, db:
            row=db.execute("SELECT * FROM jobs WHERE status='running'").fetchone()
            if row and time.time()-row['updated']>15 and worker_exited(row['pid'],row['identity']):
                db.execute("UPDATE jobs SET status='failed',message='归档进程中断；原数据或已校验的归档仍保留，请检查',updated=? WHERE id=? AND status='running' AND updated=?",(time.time(),row['id'],row['updated']))

    def inventory(self):
        items=[]
        from review_custom import visible_topics
        for topic in visible_topics(self.config):
            folder=self.root/'topics'/topic['id'];path=folder/'index.sqlite3'
            if not path.exists():continue
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                rows=db.execute('SELECT number,bytes FROM shards ORDER BY number LIMIT 5000').fetchall()
            for number,size in rows:
                raw=folder/('motions_%06d.pkl'%number)
                try:
                    stats=raw.stat()
                    tier='hot'
                except FileNotFoundError:
                    # Archiving publishes the cold copy before removing the hot file.
                    try:
                        stats=archive_path(folder,number).stat()
                        tier='cold'
                    except FileNotFoundError:
                        stats=None
                        tier='missing'
                items.append(dict(topic=topic['id'],name=topic['name'],shard=number,raw_bytes=size,
                                  bytes=stats.st_size if stats else None,tier=tier,
                                  modified=stats.st_mtime if stats else None))
        return items

    def launch(self,value):
        self.recover()
        topic=value.get('topic');number=value.get('shard');kind=value.get('kind')
        from review_custom import visible_topics
        if topic not in {t['id'] for t in visible_topics(self.config)} or type(number) is not int or number<0 or kind not in ('archive','export'):
            raise ValueError('无效分包操作')
        job_id=secrets.token_hex(12);now=time.time()
        try:
            with closing(self.db()) as db, db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (job_id,topic,number,kind,'running',now,now,None,None,None,'准备打包'))
        except sqlite3.IntegrityError:raise ValueError('已有打包任务，请等待完成') from None
        try:
            with (self.folder/(job_id+'.log')).open('ab') as log:
                child=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),'--root',str(self.root),'--config',str(self.config),'--job',job_id],
                    stdout=log,stderr=log,start_new_session=True,env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1'))
            from review_production import Production
            with closing(self.db()) as db, db:db.execute('UPDATE jobs SET pid=?,identity=? WHERE id=?',(child.pid,Production.identity(child.pid),job_id))
            threading.Thread(target=child.wait,daemon=True).start()
        except Exception:
            with closing(self.db()) as db, db:db.execute("UPDATE jobs SET status='failed',message='打包进程无法启动' WHERE id=?",(job_id,))
            raise
        return {'job_id':job_id}

    def automatic(self,items,data):
        with closing(self.db()) as db:
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():return
        days=0 if data['total']>=770_000_000_000 else self.age()
        # Choose by age, only whole reviewed shards. No motion arrays are read here.
        for item in sorted(items,key=lambda x:x['modified'] or time.time()):
            if item['tier']!='hot' or not item['modified'] or time.time()-item['modified']<days*86400:continue
            folder=self.root/'topics'/item['topic']
            with closing(sqlite3.connect((folder/'index.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                deadline=time.monotonic()+.3;db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                from g2h.farm_index import short_id
                db.create_function('motion_id',1,short_id)
                db.execute('ATTACH DATABASE ? AS catalog',(str(self.root/'prompts.sqlite3'),))
                db.execute('ATTACH DATABASE ? AS reviews',(str(self.root/'review/reviews.sqlite3'),))
                bad=db.execute("SELECT 1 FROM records r LEFT JOIN catalog.motion_links m ON m.task_id=r.id LEFT JOIN reviews.latest v ON v.motion_id=motion_id(m.number) WHERE r.shard=? AND (v.status IS NULL OR v.status NOT IN ('accepted','rejected')) LIMIT 1",(item['shard'],)).fetchone()
            if not bad:
                self.launch(dict(topic=item['topic'],shard=item['shard'],kind='archive'));return

    def retry(self,value):
        job_id=value.get('job_id')
        if not isinstance(job_id,str) or len(job_id)>128:raise ValueError('无效打包任务')
        with closing(self.db()) as db:
            row=db.execute('SELECT topic,shard,kind,status FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise ValueError('打包任务不存在')
        if row['status']!='failed':raise ValueError('只能重试失败的打包任务')
        # Launch revalidates the shard/topic and the single-worker guard. A new
        # identity preserves the failed attempt and never overwrites an export.
        return self.launch(dict(topic=row['topic'],shard=row['shard'],kind=row['kind']))

    def start(self):
        def loop():
            while not self.stop.is_set():
                complete=False
                try:
                    self.recover();complete=self.reconciler.step(seconds=.25,max_entries=8000)
                    data=capacity_status(self.root,initialize=False)
                    if self.cold_reconciler:
                        cold_root(self.root)  # fail closed if the data disk is unmounted
                        cold_complete=self.cold_reconciler.step(seconds=.25,max_entries=8000)
                        complete=complete and cold_complete
                        data['cold_tier']=capacity_status(self.cold,initialize=False)
                    items=self.inventory() if complete else self.snapshot.get('items',[])
                    self.snapshot=dict(usage=data,items=items,error=None,reconciling=not complete)
                    if complete and time.time()-self.last_auto>300:
                        self.last_auto=time.time();self.automatic(items,data)
                except Exception as exc:self.snapshot=dict(error='存储检测异常：'+str(exc),usage=None,items=[])
                self.stop.wait(30 if complete or self.snapshot.get('error') else .05)
            self.reconciler.close()
            if self.cold_reconciler:self.cold_reconciler.close()
        threading.Thread(target=loop,name='storage-monitor',daemon=True).start()

    def status(self,after=0):
        if type(after) is not int or after<0:raise ValueError('无效分页')
        snapshot=dict(self.snapshot)
        items=snapshot.pop('items',[])
        snapshot['items']=items[after:after+50]
        snapshot['next_after']=after+50 if len(items)>after+50 else None
        snapshot['total_shards']=len(items)
        with closing(self.db()) as db:
            jobs=[dict(row) for row in db.execute('SELECT id,topic,shard,kind,status,created,result,message FROM jobs ORDER BY created DESC LIMIT 30')]
        for job in jobs:
            job['download']=bool(job['result'] and json.loads(job['result']).get('path'))
            job['retryable']=job['status']=='failed'
            if job['status']=='failed' and job['message']=='该主题正在生成，分包将在生成器空闲后处理':
                job['message']='上次因主题写入而未打包；不会自动恢复，请点击“重试”'
            job.pop('result')
        return dict(snapshot,jobs=jobs,days=self.age())

    def download(self,job_id):
        with closing(self.db()) as db:row=db.execute("SELECT result FROM jobs WHERE id=? AND status='completed'",(job_id,)).fetchone()
        if not row:raise ValueError('压缩包尚未完成')
        path=Path(json.loads(row['result'])['path']).resolve()
        cold=cold_root(self.root)
        allowed=path.is_relative_to(self.root) or (cold is not None and path.is_relative_to(cold))
        if not allowed or not path.name.endswith('.mf.zip') or not path.is_file():raise ValueError('压缩包不可用')
        return path

    def execute(self,job_id):
        from review_production import Production
        with closing(self.db()) as db, db:
            db.execute('UPDATE jobs SET pid=?,identity=?,updated=? WHERE id=?',(os.getpid(),Production.identity(os.getpid()),time.time(),job_id))
            job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['status']!='running':return
        try:
            os.nice(10)
            days=0 if capacity_status(self.root)['total']>=770_000_000_000 else self.age()
            target=self.root/'exports'/(job['topic']+'_'+str(job['shard'])+'_'+job_id+'.mf.zip') if job['kind']=='export' else None
            result=package(self.root,job['topic'],job['shard'],target,days)
            message=result.get('reason') or '已校验并完成；动作 ID 保持不变'
            with closing(self.db()) as db, db:db.execute("UPDATE jobs SET status='completed',result=?,message=?,updated=? WHERE id=?",(json.dumps(result),message,time.time(),job_id))
        except Exception as exc:
            print(type(exc).__name__,str(exc),flush=True)
            with closing(self.db()) as db, db:db.execute("UPDATE jobs SET status='failed',message=?,updated=? WHERE id=?",(str(exc)[:500],time.time(),job_id))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--config',type=Path,required=True);parser.add_argument('--job',required=True)
    args=parser.parse_args();Storage(args.root,args.config).execute(args.job)
