"""Bounded, read-only host/database probes. One shared sampler per server."""
from collections import deque
from contextlib import closing
import csv
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time


def read_db(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    deadline=time.monotonic()+.4
    db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
    return db


def cpu_values():
    values=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return sum(values),values[3]+values[4]


def network_values():
    result={}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name,values=line.split(':',1);name=name.strip()
        if not (Path('/sys/class/net')/name/'device').exists():continue
        cols=values.split();result[name]=(int(cols[0]),int(cols[8]))
    return result


def memory_values():
    pages={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
           if line.startswith(('MemTotal:','MemAvailable:'))}
    total=pages['MemTotal']
    used=total-pages['MemAvailable']
    return dict(used=used,total=total,percent=used/total*100)


def shard_complete(folder, number, raw_size, archive_size):
    if archive_size is None:
        try:
            if (folder/('motions_%06d.pkl'%number)).stat().st_size>=raw_size:
                return True
        except FileNotFoundError:
            pass
        # The archiver commits the index before removing the hot file. Refresh
        # that one row if the filesystem is ahead of the earlier index read.
        with closing(read_db(folder/'index.sqlite3')) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_shards'").fetchone():
                return False
            archived=db.execute('SELECT archive_bytes FROM cold_shards WHERE number=?',(number,)).fetchone()
        if archived is None:
            return False
        archive_size=archived[0]
    from g2h.cold_storage import archive_path
    try:
        return archive_path(folder,number).stat().st_size>=archive_size
    except FileNotFoundError:
        return False


def gpu_probe():
    try:
        result=subprocess.run(['nvidia-smi','--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu',
            '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=2,check=True)
        def num(value):
            try:return float(value.strip())
            except ValueError:return None
        return {'available':True,'items':[dict(index=row[0].strip(),name=row[1].strip(),load=num(row[2]),
            used_mb=num(row[3]),total_mb=num(row[4]),temperature=num(row[5])) for row in csv.reader(result.stdout.splitlines())]}
    except (OSError,subprocess.SubprocessError):
        return {'available':False,'items':[],'reason':'nvidia-smi 不可用或采样超时'}


class Monitor:
    def __init__(self,root,config):
        self.root=Path(root);self.config=Path(config);self.lock=threading.Lock()
        self.acceptance=None
        self.rolling=None
        self.rolling_error=None
        self.acceptance_error=None
        self.snapshot={'ready':False};self.history=deque(maxlen=90);self.events=deque(maxlen=50)
        self.previous=None;self.active={};self.high={};self.progress={};self.stop=threading.Event()

    def start(self):
        threading.Thread(target=self.run,name='motionfarm-monitor',daemon=True).start()

    def run(self):
        while not self.stop.is_set():
            try:data=self.sample()
            except Exception:
                data={'ready':False,'sampled_at':time.time(),'error':'监控采样失败，请检查服务状态'}
            try:
                if self.acceptance is None:
                    from review_acceptance import Acceptance
                    self.acceptance=Acceptance(self.root)
                self.acceptance.observe(data)
                data['acceptance']=self.acceptance.get()
                if self.acceptance_error:
                    print('Acceptance recorder recovered',flush=True)
                self.acceptance_error=None
            except Exception as exc:
                kind=type(exc).__name__
                if kind!=self.acceptance_error:
                    print('Acceptance recorder unavailable: '+kind,flush=True)
                self.acceptance_error=kind
                data['acceptance']={'available':False,'error':'验收记录暂不可用，请检查服务日志；生产任务继续运行。'}
            try:
                if self.rolling is None:
                    from review_rolling import RollingMonitor
                    self.rolling=RollingMonitor(self.root)
                self.rolling.observe(data)
                data['rolling_24h']=self.rolling.get()
                self.rolling_error=None
            except Exception as exc:
                kind=type(exc).__name__
                if kind!=self.rolling_error:
                    print('Rolling monitor unavailable: '+kind,flush=True)
                self.rolling_error=kind
                data['rolling_24h']=dict(self.rolling.get()) if self.rolling else {'available':False}
                data['rolling_24h']['error']='最近 24 小时统计刷新失败；保留上次结果，请检查服务日志。'
            with self.lock:self.snapshot=data
            self.stop.wait(10)

    def get(self):
        with self.lock:return dict(self.snapshot)

    def sample(self):
        now=time.time();mono=time.monotonic();alerts=[]
        def alert(key,severity,message):alerts.append(dict(key=key,severity=severity,message=message))
        try:cpu=cpu_values()
        except (OSError,IndexError,ValueError):cpu=None
        try:net=network_values()
        except (OSError,IndexError,ValueError):net={}
        cpu_percent=None;interfaces=[]
        if self.previous:
            before,old_cpu,old_net=self.previous;elapsed=mono-before
            if cpu is not None and old_cpu is not None:
                total=cpu[0]-old_cpu[0]
                if total>0:cpu_percent=round(100*(1-(cpu[1]-old_cpu[1])/total),1)
            for name,(rx,tx) in net.items():
                prev=old_net.get(name)
                interfaces.append(dict(name=name,rx_bps=max(0,(rx-prev[0])/elapsed) if prev else None,tx_bps=max(0,(tx-prev[1])/elapsed) if prev else None))
        self.previous=(mono,cpu,net)
        try:memory=memory_values()
        except (OSError,IndexError,KeyError,ValueError,ZeroDivisionError):memory=dict(used=None,total=None,percent=None)
        memory_percent=memory['percent']
        disk=shutil.disk_usage(self.root);disk_percent=disk.used/disk.total*100
        gpu=gpu_probe()
        if not gpu['available']:alert('gpu_probe','warning','无法读取 GPU 状态（驱动、工具或采样异常）')
        checks=[('cpu',cpu_percent,95,'CPU 持续超过 95%'),('memory',memory_percent,90,'内存持续超过 90%')]
        for item in gpu['items']:
            checks.append(('gpu_heat_'+item['index'],item['temperature'],85,'GPU '+item['index']+' 温度持续超过 85°C'))
        for key,value,threshold,message in checks:
            self.high[key]=self.high.get(key,0)+1 if value is not None and value>=threshold else 0
            if self.high[key]>=3:alert(key,'warning',message+'（连续三次采样）')
        if disk.free<10_000_000_000 or disk_percent>95:alert('disk','critical','数据磁盘空间不足：剩余不足 10 GB 或使用率超过 95%')
        from review_custom import visible_topics
        topics=visible_topics(self.config);rows=[];catalog_ok=True
        activity=None
        production=self.root/'review/production/jobs.sqlite3'
        if production.exists():
            try:
                with closing(read_db(production)) as db:
                    row=db.execute("SELECT stage,topic,updated,message FROM jobs WHERE status IN ('queued','running','stopping') LIMIT 1").fetchone()
                    if row:activity=dict(zip(('stage','topic','heartbeat','message'),row))
            except sqlite3.Error:alert('production_state','warning','生产状态暂不可读')
        if activity and now-activity['heartbeat']>90:
            alert('heartbeat','warning','生产进程超过 90 秒未更新心跳，请检查当前阶段')
        try:
            with closing(read_db(self.root/'prompts.sqlite3')) as db:
                texts=dict(db.execute('SELECT topic,total FROM topic_counts'))
                counted=db.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_counts'").fetchone()
                vectors=dict(db.execute('SELECT topic,total FROM embedding_counts' if counted else 'SELECT topic,count(*) FROM embeddings GROUP BY topic'))
        except (OSError,sqlite3.Error):
            texts={};vectors={};catalog_ok=False;alert('catalog','critical','文本数据库不可读或查询超时')
        try:
            with closing(read_db(self.root/'review/reviews.sqlite3')) as db:reviews=dict(db.execute('SELECT status,count(*) FROM latest GROUP BY status'))
        except (OSError,sqlite3.Error):reviews=None;alert('reviews','warning','审核数据库不可读或查询超时')
        for topic in topics[:100]:
            key=topic['id'];folder=self.root/'topics'/key;path=folder/'index.sqlite3'
            row=dict(id=key,name=topic['name'],texts=texts.get(key,0) if catalog_ok else None,vectors=vectors.get(key,0) if catalog_ok else None,
                motions=0,tasks={},bytes=0,collector='未启动',db_ok=True)
            if path.exists():
                try:
                    with closing(read_db(path)) as db:
                        counted=db.execute("SELECT 1 FROM sqlite_master WHERE name='record_count'").fetchone()
                        row['tasks']=dict(db.execute('SELECT state,total FROM task_state_counts WHERE total>0' if counted else 'SELECT state,count(*) FROM tasks GROUP BY state'))
                        row['motions']=db.execute('SELECT total FROM record_count WHERE id=1' if counted else 'SELECT count(*) FROM records').fetchone()[0]
                        row['bytes']=db.execute('SELECT coalesce(sum(bytes),0) FROM shards').fetchone()[0]
                        latest=db.execute('SELECT number,bytes FROM shards WHERE bytes>0 ORDER BY number DESC LIMIT 3').fetchall()
                        cold_sizes={}
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='cold_shards'").fetchone():
                            cold_sizes=dict(db.execute('SELECT number,archive_bytes FROM cold_shards ORDER BY number DESC LIMIT 3'))
                            saved=db.execute('SELECT coalesce(sum(s.bytes-c.archive_bytes),0) FROM cold_shards c JOIN shards s ON s.number=c.number').fetchone()[0]
                            row['bytes']-=saved
                    for number,size in latest:
                        try:
                            if not shard_complete(folder,number,size,cold_sizes.get(number)):
                                alert('shard_'+key,'critical',topic['name']+' 最新动作分包缺失或截断')
                        except (OSError,RuntimeError,ValueError,KeyError):
                            alert('shard_unavailable_'+key,'warning',topic['name']+' 分包存储暂不可读取，无法完成完整性检查')
                except (OSError,sqlite3.Error):
                    row.update(db_ok=False,motions=None,tasks=None,bytes=None)
                    alert('database_'+key,'critical',topic['name']+' 动作数据库不可读或查询超时')
            pidfile=folder/'collector.pid';alive=False
            if pidfile.exists():
                try:
                    pid=int(pidfile.read_text());cmd=Path('/proc/%d/cmdline'%pid).read_bytes()
                    alive=b'motion_collector.py' in cmd and str(folder).encode() in cmd
                except (OSError,ValueError):pass
            stopped=(folder/'STOP').exists();row['collector']='运行中' if alive else '已停止' if stopped else '未运行'
            tasks=row['tasks'] or {};pending=tasks.get('pending',0);failed=tasks.get('failed',0)
            if failed:alert('failed_'+key,'critical',topic['name']+' 有 '+str(failed)+' 个失败任务')
            expected=activity and now-activity['heartbeat']<90
            if pending and not alive and expected:row['collector']='等待调度 · '+activity['stage']
            if pending and not alive and not expected:alert('queue_'+key,'info' if stopped else 'warning',topic['name']+' 有 '+str(pending)+' 个待处理任务，生成器'+('已主动停止' if stopped else '未运行'))
            previous=self.progress.get(key)
            if not previous or previous[0]!=row['motions'] or not alive or not pending:self.progress[key]=(row['motions'],mono)
            elif mono-previous[1]>600:alert('stalled_'+key,'warning',topic['name']+' 超过 10 分钟未新增动作；可能在加载模型或执行长任务，请检查')
            rows.append(row)
        usage={'available':False,'models':[],'coverage':'本项目已记录的 API 请求；历史 Embedding 无用量记录，不含其他应用调用。失败请求若无 usage 则用量未知，不能视为免费。'}
        path=self.root/'review/api_usage.sqlite3'
        if path.exists():
            try:
                with closing(read_db(path)) as db:
                    result=db.execute('SELECT model,kind,calls,errors,input,output,total,unknown FROM totals').fetchall()
                    usage.update(available=True,models=[dict(zip(('model','kind','calls','errors','input','output','total','unknown'),r)) for r in result])
                    from g2h.api_costs import summarize
                    usage['costs']=summarize(db,path.with_name('api_prices.json'))
                    failures=db.execute("SELECT count(*) FROM events WHERE ts>? AND status!='ok'",(now-900,)).fetchone()[0]
                    if failures:alert('api','warning','最近 15 分钟记录到 '+str(failures)+' 次 API 请求失败')
            except sqlite3.Error:alert('usage','warning','API 用量数据库不可读或查询超时')
        from production_health import read as production_health
        health=production_health(self.root,now)
        alerts.extend(health['alerts'])
        current={a['key']:a for a in alerts}
        for key,a in current.items():
            if key not in self.active or self.active[key]['message']!=a['message']:self.events.append(dict(a,time=now,state='出现'))
        for key,a in self.active.items():
            if key not in current:self.events.append(dict(a,time=now,state='恢复'))
        self.active=current
        rx=[i['rx_bps'] for i in interfaces if i['rx_bps'] is not None]
        tx=[i['tx_bps'] for i in interfaces if i['tx_bps'] is not None]
        point=dict(time=now,cpu=cpu_percent,memory=round(memory_percent,1) if memory_percent is not None else None,
            gpu=max((g['load'] for g in gpu['items'] if g['load'] is not None),default=None),
            rx=sum(rx) if rx else None,tx=sum(tx) if tx else None)
        self.history.append(point)
        return dict(ready=True,sampled_at=now,interval_s=10,cpu=cpu_percent,load=list(os.getloadavg()),cpu_count=os.cpu_count(),
            production=activity,production_health=health,
            memory=memory,disk=dict(used=disk.used,total=disk.total,free=disk.free),
            network=interfaces,gpu=gpu,topics=rows,reviews=reviews,usage=usage,alerts=alerts,events=list(reversed(self.events)),history=list(self.history),
            scope='整台服务器资源；物理网卡吞吐。最近 24 小时统计每分钟汇总，资源采样持久保存并展示缺口；当前状态每 10 秒采样。告警变化保留最近 50 条，服务重启后重置。')
