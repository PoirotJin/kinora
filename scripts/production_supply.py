"""Independent durable text supply. Never takes the GPU lock or relaxes review."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time

DEFAULT = dict(enabled=False, queue_high=256, max_steps_per_day=12000,
               max_attempts=2, target_motions_per_day=48000, rolling_variants=False,
               daily_variants_per_description=8)


def policy(root):
    path=Path(root)/'review/production/supply_policy.json'
    values=dict(DEFAULT)
    if path.exists(): values.update(json.loads(path.read_text()))
    if type(values['enabled']) is not bool or type(values['rolling_variants']) is not bool: raise ValueError('Invalid supply enable flag')
    for key,lo,hi in [('queue_high',8,1024),('max_steps_per_day',1,20000),
                      ('max_attempts',1,3),('target_motions_per_day',1,100000),('daily_variants_per_description',1,8)]:
        if type(values[key]) is not int or not lo<=values[key]<=hi:
            raise ValueError('Invalid supply policy '+key)
    return values


def asynchronous(root, settings):
    return bool(settings.get('continuous') and policy(root)['enabled'])


def setup(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS topics(
        topic TEXT PRIMARY KEY,failures INTEGER DEFAULT 0,wait_until REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY,steps INTEGER DEFAULT 0,
        accepted INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS inflight(id INTEGER PRIMARY KEY CHECK(id=1),
        job TEXT,topic TEXT,work_key TEXT,seeds TEXT,duration INTEGER,day TEXT);
    ''')
    db.commit()


def reserve(db, job, topic, settings, limit, now=None):
    day=time.strftime('%Y-%m-%d',time.localtime(time.time() if now is None else now))
    with db:
        old=db.execute('SELECT * FROM inflight WHERE id=1').fetchone()
        if old and old['job']==job: return dict(old)
        if old: db.execute('DELETE FROM inflight')
        db.execute('INSERT OR IGNORE INTO daily(day) VALUES(?)',(day,))
        if db.execute('SELECT steps FROM daily WHERE day=?',(day,)).fetchone()[0]>=limit:
            return None
        key='supply_'+secrets.token_hex(12)
        seeds=json.dumps([secrets.randbelow(2**32) for _ in range(settings['k'])])
        db.execute('UPDATE daily SET steps=steps+1 WHERE day=?',(day,))
        db.execute('INSERT INTO inflight VALUES(1,?,?,?,?,?,?)',
                   (job,topic,key,seeds,settings['duration'],day))
        return dict(db.execute('SELECT * FROM inflight').fetchone())


def finish(db, work, code, now=None):
    now=time.time() if now is None else now
    with db:
        if not db.execute('SELECT 1 FROM inflight WHERE work_key=?',(work['work_key'],)).fetchone(): return
        db.execute('INSERT OR IGNORE INTO topics(topic) VALUES(?)',(work['topic'],))
        failures=db.execute('SELECT failures FROM topics WHERE topic=?',(work['topic'],)).fetchone()[0]
        if code==0:
            db.execute('UPDATE daily SET accepted=accepted+1 WHERE day=?',(work['day'],))
            failures=0;wait=0
        else:
            failures+=1
            # Lack of novelty and provider errors cool only the producer topic.
            # Saturated topics are periodically retried, never permanently disabled.
            wait=now+min(3600,30*2**min(failures-1,7))
        db.execute('UPDATE topics SET failures=?,wait_until=? WHERE topic=?',
                   (failures,wait,work['topic']))
        db.execute('DELETE FROM inflight WHERE work_key=?',(work['work_key'],))


def active(root):
    path=Path(root)/'review/production/jobs.sqlite3'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        row=db.execute("SELECT id,settings FROM jobs WHERE status IN ('queued','running') AND stop=0 LIMIT 1").fetchone()
    if not row: return None
    return row[0],json.loads(row[1])


def publish(root,db,status,**extra):
    day=time.strftime('%Y-%m-%d')
    row=db.execute('SELECT steps,accepted FROM daily WHERE day=?',(day,)).fetchone()
    body=dict(updated=time.time(),status=status,day=day,steps=row[0] if row else 0,
              accepted_descriptions=row[1] if row else 0,**extra)
    rules=policy(root)
    body.update(rolling_variants=rules['rolling_variants'],
                daily_variants_per_description=rules['daily_variants_per_description'],
                max_steps_per_day=rules['max_steps_per_day'],
                target_motions_per_day=rules['target_motions_per_day'])
    path=Path(root)/'review/production/supply_status.json'
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(body,ensure_ascii=False));temp.chmod(0o600)
    os.replace(temp,path)


def run(root,config,stop):
    from production_flow import pending
    folder=root/'review/production';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'supply.sqlite3'
    with (folder/'supply.lock').open('a') as gate, closing(sqlite3.connect(path,timeout=2)) as db:
        fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path.chmod(0o600);db.row_factory=sqlite3.Row;setup(db)
        position=0
        while not stop.is_set():
            try:
                rules=policy(root);job=active(root)
                if not rules['enabled'] or not job or not job[1]['continuous']:
                    publish(root,db,'paused');stop.wait(5);continue
                jid,settings=job
                old=db.execute('SELECT * FROM inflight WHERE id=1').fetchone()
                selected=None
                if old and old['job']==jid: selected=old['topic']
                else:
                    topics=settings['topics']
                    for offset in range(len(topics)):
                        i=(position+offset)%len(topics);topic=topics[i]
                        wait=db.execute('SELECT wait_until FROM topics WHERE topic=?',(topic,)).fetchone()
                        if (not wait or wait[0]<=time.time()) and pending(root,topic)+settings['k']<=rules['queue_high']:
                            selected=topic;position=(i+1)%len(topics);break
                if not selected:
                    publish(root,db,'buffered_or_cooling');stop.wait(5);continue
                # The agent owns this same gate. Probe before allocating a paid
                # step; contention is deferred, rather than counted as rejection.
                with (root/'prompt_generation.lock').open('a') as promptgate:
                    try: fcntl.flock(promptgate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError: stop.wait(2);continue
                work=reserve(db,jid,selected,settings,rules['max_steps_per_day'])
                if not work:
                    publish(root,db,'daily_limit',limit=rules['max_steps_per_day']);stop.wait(30);continue
                args=[sys.executable,'-u',str(Path(__file__).with_name('qwen_motion_agent.py')),
                      '--root',str(root),'--config',str(config),'--topic',work['topic'],
                      '--work-key',work['work_key'],'--per-topic','1','--rounds','1',
                      '--duration',str(work['duration']),'--max-attempts',str(rules['max_attempts']),
                      '--enqueue','--seeds',*map(str,json.loads(work['seeds']))]
                started=time.monotonic();log=folder/'supply-current.log'
                with log.open('w') as output:
                    log.chmod(0o600)
                    child=subprocess.Popen(args,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                    cancelled=False
                    try:
                        while child.poll() is None:
                            publish(root,db,'generating',topic=selected,work_key=work['work_key'],
                                    target_motions_per_day=rules['target_motions_per_day'])
                            current=active(root)
                            if stop.is_set() or not current or current[0]!=jid or not policy(root)['enabled']:
                                cancelled=True;break
                            if time.monotonic()-started>300: break
                            stop.wait(3)
                    finally:
                        if child.poll() is None:
                            os.killpg(child.pid,signal.SIGTERM)
                            try: child.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid,signal.SIGKILL);child.wait()
                    if cancelled:
                        # Preserve identical key/seeds for crash-safe replay.
                        publish(root,db,'paused');continue
                    finish(db,work,child.returncode)
                    publish(root,db,'ready' if child.returncode==0 else 'retry_cooling',
                            topic=selected,last_exit=child.returncode)
                # Leave an explicit lock-free opportunity for historical enqueue.
                stop.wait(2)
            except (OSError,ValueError,sqlite3.Error) as exc:
                print('Supply deferred: '+str(exc),flush=True);stop.wait(10)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True);args=p.parse_args()
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    run(args.root.resolve(),args.config.resolve(),stop)


if __name__=='__main__': main()
