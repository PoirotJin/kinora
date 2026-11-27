"""Persistent production jobs and interval scheduler; no ML imports in HTTP server."""
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

PROJECT = Path(__file__).resolve().parents[1]
ACTIVE = ('queued', 'running', 'stopping')


class Production:
    def __init__(self, root, config, *, worker=False):
        self.worker = worker
        self.root, self.config = Path(root).resolve(), Path(config).resolve()
        self.folder = self.root / 'review/production'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        self.path = self.folder / 'jobs.sqlite3'
        self.stop = threading.Event()
        self.scheduler_error = None
        with closing(self.db()) as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,created REAL,updated REAL,status TEXT,
                    stage TEXT,settings TEXT,owner TEXT,source TEXT,pid INTEGER,identity TEXT,stop INTEGER DEFAULT 0,
                    topic TEXT DEFAULT '',message TEXT DEFAULT '',baseline INTEGER DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active ON jobs((1)) WHERE status IN ('queued','running','stopping');
                CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER,
                    interval_s INTEGER,next_at REAL,settings TEXT,owner TEXT,last_message TEXT);
            ''')
            from production_flow import setup
            setup(db)
            from production_health import setup as guard_setup
            guard_setup(db)
            db.commit()

    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def validate(self, value):
        topics = {t['id'] for t in json.loads(self.config.read_text())['topics']}
        selected = value.get('topics')
        if not isinstance(selected, list) or not selected or any(not isinstance(t,str) or t not in topics for t in selected):
            raise ValueError('请选择有效主题')
        selected = list(dict.fromkeys(selected))
        result = dict(topics=selected)
        continuous=value.get('continuous',False)
        if type(continuous) is not bool: raise ValueError('无效持续生产选项')
        result['continuous']=continuous
        for key, lo, hi in [('per_topic',1,20),('k',1,8),('rounds',1,10000),('duration',1,12)]:
            n = 1 if key=='rounds' and continuous else value.get(key)
            if type(n) is not int or not lo <= n <= hi: raise ValueError('%s 必须为 %d–%d 的整数'%(key,lo,hi))
            result[key] = n
        from g2h.generation_backend import current_recipe, KIMODO_MODEL, kimodo_readiness
        if current_recipe()['model'] == KIMODO_MODEL and not kimodo_readiness(PROJECT)['available']:
            raise ValueError('Kimodo 模型尚未完成安装与生成验证')
        if current_recipe()['model'] == KIMODO_MODEL and result['duration'] > 10:
            raise ValueError('Kimodo 单段生成时长为 1–10 秒；请拆分较长动作')
        result['per_topic']=1  # Web rounds visit each selected topic once.
        for key,default in [('queue_low',4),('queue_high',16)]:
            n=value.get(key,default)
            if type(n) is not int or not 0<=n<=256:raise ValueError('队列水位必须是 0–256 整数')
            if key in value:result[key]=n
        if not value.get('queue_low',4)+result['k']<=value.get('queue_high',16):raise ValueError('高水位至少为低水位加 K')
        if len(selected)*result['per_topic'] > 30: raise ValueError('每轮描述总数不能超过 30')
        return result

    @staticmethod
    def identity(pid):
        try: return Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]
        except (OSError,IndexError): return None

    def update(self, job_id, **values):
        values['updated'] = time.time()
        with closing(self.db()) as db, db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', [*values.values(),job_id])

    def recover(self):
        if not self.worker:
            return
        with (self.folder/'recovery.lock').open('a') as gate:
            try:fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            return self._recover_locked()

    def _recover_locked(self):
        from g2h.capacity import worker_exited
        with closing(self.db()) as db:
            active = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','stopping')").fetchone()
        if (active and active['status'] == 'stopping' and active['stage'] == 'queued'
                and active['stop'] and active['pid'] is None):
            # Earlier versions left cancelled, unclaimed jobs in the stopping state.
            with closing(self.db()) as db, db:
                db.execute("UPDATE jobs SET status='stopped',stage='stopped',updated=?,"
                           "message='已停止，尚未开始处理' "
                           "WHERE id=? AND status='stopping' AND stage='queued' AND pid IS NULL AND stop=1",
                           (time.time(), active['id']))
            return
        if active and active['status'] == 'queued' and not active['pid']:
            self._spawn(active['id'])
            return
        if active and time.time()-active['updated'] > 15 and worker_exited(active['pid'], active['identity']):
            with closing(self.db()) as db:runtime=db.execute('SELECT * FROM runtime WHERE job=?',(active['id'],)).fetchone()
            # A legacy worker has no replay-safe cursor. Never invent its progress.
            if not runtime:
                self.update(active['id'],status='failed',stage='interrupted',message='旧版进程已退出，无可用断点；请检查后重新启动。');return
            with (self.root/'gpu_worker.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    self.update(active['id'],stage='recovering',message='主进程退出，等待仍持有 GPU 锁的子进程完成；不重复启动');return
            if active['stop']:
                self.update(active['id'],status='stopped',stage='stopped',message='已停止，断点保留');return
            count=runtime['restarts'] if time.time()-runtime['restarted']<1800 else 0
            if count>=3:
                self.update(active['id'],status='failed',stage='interrupted',message='30 分钟内连续异常退出 3 次，停止自动恢复；请检查日志');return
            with closing(self.db()) as db,db:db.execute('UPDATE runtime SET restarts=?,restarted=? WHERE job=?',(count+1,time.time(),active['id']))
            self.update(active['id'],stage='recovering',message='进程退出，正在从持久化阶段恢复')
            self._spawn(active['id'])

    def _spawn(self,job_id):
        if not self.worker:
            raise RuntimeError('Only the independent production worker may spawn jobs')
        with (self.folder/(job_id+'.log')).open('ab') as log:
            child=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),'--root',str(self.root),
                '--config',str(self.config),'--job',job_id],stdout=log,stderr=log,start_new_session=True,
                env=dict(os.environ,OMP_NUM_THREADS='2',MKL_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2'))
        self.update(job_id,pid=child.pid,identity=self.identity(child.pid))
        threading.Thread(target=child.wait,daemon=True).start()

    def launch(self, settings, owner, source='manual'):
        settings = self.validate(settings)
        self.recover()
        job_id = secrets.token_hex(12)
        now = time.time()
        try:
            with closing(self.db()) as db, db:
                db.execute('INSERT INTO jobs(id,created,updated,status,stage,settings,owner,source) VALUES(?,?,?,?,?,?,?,?)',
                    (job_id,now,now,'queued','queued',json.dumps(settings),owner,source))
                db.execute('INSERT INTO runtime(job,seeds) VALUES(?,?)',
                    (job_id,json.dumps([secrets.randbelow(2**32) for _ in range(settings['k'])])))
        except sqlite3.IntegrityError: raise ValueError('已有生产任务正在运行，请等待完成或停止')
        return {'job_id':job_id}

    def cancel(self):
        now = time.time()
        with closing(self.db()) as db, db:
            db.execute("UPDATE jobs SET stop=1,status='stopped',stage='stopped',updated=?,"
                       "message='已停止，尚未开始处理' WHERE status='queued'", (now,))
            db.execute("UPDATE jobs SET stop=1,status='stopping',updated=? "
                       "WHERE status IN ('running','stopping')", (now,))
        return {'ok':True}

    def configure(self, value, owner):
        enabled = value.get('enabled')
        if type(enabled) is not bool: raise ValueError('无效调度状态')
        if not enabled:
            with closing(self.db()) as db, db: db.execute("UPDATE schedule SET enabled=0,last_message='调度已暂停' WHERE id=1")
            return {'ok':True}
        settings = self.validate(value.get('settings',{}))
        if settings['continuous']: raise ValueError('持续生产无需定时调度；请直接立即启动，或改为有限轮数')
        interval = value.get('interval_minutes')
        if type(interval) is not int or not 5 <= interval <= 10080: raise ValueError('调度间隔为 5–10080 分钟')
        now = time.time()
        with closing(self.db()) as db, db:
            db.execute('INSERT OR REPLACE INTO schedule VALUES(1,?,?,?,?,?,?)',
                (1,interval*60,now+interval*60,json.dumps(settings),owner,'已启用；首次执行时间见下方'))
        return {'ok':True}

    def tick(self, custom=True):
        if not self.worker:
            raise RuntimeError('Scheduler must run in the independent worker service')
        self.recover()
        from review_custom import Custom
        with closing(self.db()) as db:
            active=db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping')").fetchone()
        if custom:Custom(self.root).tick(bool(active))
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            schedule = db.execute('SELECT * FROM schedule WHERE id=1 AND enabled=1 AND next_at<=?',(time.time(),)).fetchone()
            if not schedule: return
            busy = db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','stopping')").fetchone()
            db.execute('UPDATE schedule SET next_at=?,last_message=? WHERE id=1',
                (time.time()+schedule['interval_s'],'本次跳过：已有生产任务' if busy else '本次已触发'))
        if not busy:
            try: self.launch(json.loads(schedule['settings']),schedule['owner'],'scheduler')
            except Exception:
                with closing(self.db()) as db, db: db.execute("UPDATE schedule SET last_message='本次启动失败，请检查任务记录' WHERE id=1")

    def start(self):
        if not self.worker:
            return
        def loop():
            while not self.stop.is_set():
                try: self.tick(); self.scheduler_error = None
                except Exception: self.scheduler_error = '调度器读取失败，请检查数据库与服务日志'
                self.stop.wait(5)
        threading.Thread(target=loop,name='production-scheduler',daemon=True).start()

    def status(self):
        self.recover()
        with closing(self.db()) as db:
            jobs = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 20')]
            schedule = db.execute('SELECT * FROM schedule WHERE id=1').fetchone()
        for job in jobs:
            job['settings'] = json.loads(job['settings'])
            job.pop('identity'); job.pop('pid')
        latest = []; current = []; error = None
        try:
            from g2h.farm_index import short_id
            with closing(sqlite3.connect((self.root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                for topic_config in json.loads(self.config.read_text())['topics']:
                    topic=topic_config['id'];folder=self.root/'topics'/topic
                    if not (folder/'index.sqlite3').exists(): continue
                    with closing(sqlite3.connect((folder/'index.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=.2)) as queue:
                        records=queue.execute('SELECT id,shard,offset FROM records ORDER BY rowid DESC LIMIT 4').fetchall()
                    for task_id,shard,offset in records:
                        row=db.execute('SELECT m.number,p.item FROM motion_links m JOIN prompts p ON p.id=m.text_id WHERE m.task_id=?',(task_id,)).fetchone()
                        if row:
                            latest.append(dict(motion_id=short_id(row[0]),topic=topic,text=json.loads(row[1])['text'],
                                order=[(folder/('motions_%06d.pkl'%shard)).stat().st_mtime if (folder/('motions_%06d.pkl'%shard)).exists() else 0,shard,offset]))
            latest.sort(key=lambda item:item['order'],reverse=True)
            latest=latest[:4]
            if jobs and jobs[0]['status'] in ACTIVE and jobs[0]['topic']:
                folder = self.root/'topics'/jobs[0]['topic']
                batch = folder/'work/batch.json'
                if batch.exists() and batch.stat().st_size < 100000:
                    with closing(sqlite3.connect((folder/'index.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                        for job in json.loads(batch.read_text()):
                            row = db.execute('SELECT state FROM tasks WHERE id=?',(job['id'],)).fetchone()
                            if row and row[0]=='pending': current.append(dict(text=job['spec']['text'],seed=job['spec']['seed']))
        except (OSError,sqlite3.Error,KeyError,ValueError): error = '动作状态暂不可用'
        from production_health import read as production_health
        return dict(health=production_health(self.root),jobs=jobs,schedule=dict(schedule) if schedule else None,latest=latest,current=current,error=error,scheduler_error=self.scheduler_error)

    def cancelled(self, job_id):
        with closing(self.db()) as db:
            return bool(db.execute('SELECT stop FROM jobs WHERE id=?',(job_id,)).fetchone()[0])

    def command(self, job_id, arguments, gpu_fd, folder=None):
        attempts = 0
        while True:
            result = self._command_once(job_id, arguments, gpu_fd, folder)
            if result != 75:
                return result
            attempts += 1
            from production_health import outcome, RETRY_LIMIT
            counts=outcome(self,job_id,'temporary','API 网络、限流或服务临时故障')
            if counts['retries']>=RETRY_LIMIT:
                raise RuntimeError('临时 API 故障已连续重试 6 次，停止自动请求以限制费用；请检查后重新启动')
            delay = (10, 30, 60, 300)[min(attempts-1, 3)]
            self.update(job_id,stage='backoff',message=
                'API 临时故障，第 %d 次恢复等待；%d 秒后重试，可随时停止。已入队数据保留'
                % (attempts, delay))
            print('Temporary API failure; retry %d in %d seconds' % (attempts, delay),flush=True)
            for _ in range(delay):
                if self.cancelled(job_id): return False
                if _%5==0:self.update(job_id,stage='backoff')
                time.sleep(1)
            if self.cancelled(job_id): return False
            self.update(job_id,stage='prompts',message='正在恢复 API 请求（第 %d 次重试）'%attempts)

    def _command_once(self, job_id, arguments, gpu_fd, folder=None):
        if self.cancelled(job_id): return False
        from g2h.cold_storage import guard
        guard(self.root)
        env=dict(os.environ)
        if gpu_fd is None:env.pop('MOTIONFARM_GPU_FD',None)
        else:env['MOTIONFARM_GPU_FD']=str(gpu_fd)
        child = subprocess.Popen(['bash',str(PROJECT/'python3'),*arguments],
            pass_fds=() if gpu_fd is None else (gpu_fd,),env=env)
        # A stop request lets the current API request / inference stage finish.
        # Collector checks STOP between stages and preserves completed records.
        while child.poll() is None:
            if folder and self.cancelled(job_id): (folder/'STOP').touch()
            self.update(job_id)
            time.sleep(1)
        if self.cancelled(job_id): return False
        if child.returncode==75 and Path(arguments[0]).name=='qwen_motion_agent.py':
            return 75
        if child.returncode==3 and Path(arguments[0]).name=='qwen_motion_agent.py':
            from production_health import outcome, SKIP_LIMIT, topic_outcome, classify_step
            kind=classify_step(self.root,arguments)
            topic=arguments[arguments.index('--topic')+1] if '--topic' in arguments else 'unknown'
            topic_outcome(self,topic,kind)
            counts=outcome(self,job_id,'skip',kind)
            if counts['skips']>=SKIP_LIMIT:
                raise RuntimeError('连续 24 个补词步骤无新增描述，生产已停止以限制费用；请检查主题、重复率与拒绝记录')
            self.update(job_id,message='提示词步骤已跳过；连续无新增 '+str(counts['skips'])+' / '+str(SKIP_LIMIT))
            # Bound repeated empty rounds without blocking stop requests.
            for _ in range(10):
                if self.cancelled(job_id):return False
                time.sleep(1)
            return True
        if child.returncode:
            from production_health import outcome
            outcome(self,job_id,'fatal','子进程退出码 '+str(child.returncode))
            stage='文本生成 / 语义审核' if Path(arguments[0]).name=='qwen_motion_agent.py' else '动作生成'
            raise RuntimeError(stage+'异常，退出码 %d；已保存的数据保留，请检查生产日志'%child.returncode)
        if Path(arguments[0]).name=='qwen_motion_agent.py':
            from production_health import outcome
            outcome(self,job_id,'success')
            from production_health import topic_outcome
            topic_outcome(self,arguments[arguments.index('--topic')+1] if '--topic' in arguments else 'unknown','success')
        return True

    def execute(self,job_id):
        from production_flow import execute
        return execute(self,job_id)



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--job',required=True)
    args = parser.parse_args()
    sys.path.insert(0,str(PROJECT))
    Production(args.root,args.config).execute(args.job)
