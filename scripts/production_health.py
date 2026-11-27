"""Persisted outcome guards plus read-only production health, not process liveness."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

SKIP_LIMIT = 24
RETRY_LIMIT = 6


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS production_guard(
      job TEXT PRIMARY KEY,skips INTEGER DEFAULT 0,failures INTEGER DEFAULT 0,
      retries INTEGER DEFAULT 0,error_kind TEXT DEFAULT '',last_error TEXT DEFAULT '',updated REAL)''')


def outcome(p, job, kind, message=''):
    with closing(p.db()) as db, db:
        setup(db)
        db.execute('INSERT OR IGNORE INTO production_guard(job,updated) VALUES(?,?)',(job,time.time()))
        if kind=='success':
            db.execute("UPDATE production_guard SET skips=0,failures=0,retries=0,updated=? WHERE job=?",(time.time(),job))
        elif kind=='skip':
            db.execute("UPDATE production_guard SET skips=skips+1,retries=0,error_kind=?,last_error=?,updated=? WHERE job=?",(kind,message,time.time(),job))
        else:
            db.execute("UPDATE production_guard SET failures=failures+1,retries=retries+?,error_kind=?,last_error=?,updated=? WHERE job=?",(int(kind=='temporary'),kind,message,time.time(),job))
        row=db.execute('SELECT skips,failures,retries FROM production_guard WHERE job=?',(job,)).fetchone()
        return dict(zip(('skips','failures','retries'),row))


def read(root, now=None):
    now=time.time() if now is None else now
    root=Path(root);result=dict(available=True,status='idle',alerts=[],last_saved=None,
        last_job_saved=None,last_error='',consecutive_skips=0,consecutive_failures=0,retries=0,
        skip_limit=SKIP_LIMIT,retry_limit=RETRY_LIMIT,stall_after_s=1800)
    job=None
    try:
        path=root/'review/production/jobs.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT id,status,stage,message,created,updated FROM jobs ORDER BY created DESC LIMIT 1').fetchone()
                if row:
                    job=dict(row);result.update(job_id=job['id'],status=job['status'],stage=job['stage'],heartbeat=job['updated'])
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='production_guard'").fetchone():
                        g=db.execute('SELECT * FROM production_guard WHERE job=?',(job['id'],)).fetchone()
                        if g:result.update(consecutive_skips=g['skips'],consecutive_failures=g['failures'],retries=g['retries'],last_error=g['last_error'],error_kind=g['error_kind'])
        path=root/'review/telemetry.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as db:
                # Actual committed outbox facts. New custom work cannot mask a stalled production job.
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                deadline=time.monotonic()+.3
                rows=db.execute('SELECT saved,job FROM motions ORDER BY saved DESC LIMIT 20000').fetchall()
                if rows:result['last_saved']=rows[0][0]
                if job:result['last_job_saved']=next((r[0] for r in rows if r[1]==job['id']),None)
                result['last_job_window_limited']=len(rows)==20000 and result['last_job_saved'] is None
        if job:
            alert=lambda key,severity,message:result['alerts'].append(dict(key=key,severity=severity,message=message))
            if job['status']=='failed':
                result['last_error']=job['message'];alert('production_failed','critical','持续生产已失败停止：'+job['message'])
            elif job['status'] in ('queued','running','stopping'):
                if now-job['updated']>90:alert('production_heartbeat','warning','生产心跳超过 90 秒未更新')
                elapsed=now-max(job['created'],result['last_job_saved'] or job['created'])
                result['no_output_seconds']=max(0,elapsed)
                if elapsed>1800:alert('production_no_output','critical','当前生产任务超过 30 分钟没有已记录的新动作；服务在线不代表生产正常')
                if result['consecutive_skips']>=8:alert('production_skips','warning','连续 '+str(result['consecutive_skips'])+' 个补词步骤无新描述；达到 24 次会停止以限制费用')
        from production_supply import policy
        if policy(root)['enabled']:
            import json
            statuspath=root/'review/production/supply_status.json'
            supply=json.loads(statuspath.read_text()) if statuspath.exists() else {}
            result['prompt_supply']=supply
            result['prompt_supply']['target_motions_per_day']=policy(root)['target_motions_per_day']
            if job and job['status'] in ('queued','running'):
                if now-supply.get('updated',0)>120:
                    result['alerts'].append(dict(key='prompt_supply_stale',severity='warning',message='后台新描述供应超过 120 秒没有心跳；历史库存可能仍在生成'))
                if supply.get('status')=='daily_limit':
                    result['alerts'].append(dict(key='prompt_supply_budget',severity='warning',message='后台补词达到每日调用步骤限制，次日自动恢复；已有合格队列继续生成'))
    except (OSError,sqlite3.Error,ValueError):
        result['available']=False
        result['alerts'].append(dict(key='production_health_unavailable',severity='warning',message='生产健康记录暂不可读，不能判定正常'))
    return result


# Topic guards persist across production jobs; restarting cannot reset spending guards.
def topic_setup(db):
    db.execute("CREATE TABLE IF NOT EXISTS topic_guard(topic TEXT PRIMARY KEY,empty_steps INTEGER DEFAULT 0,cooldown_until REAL DEFAULT 0,kind TEXT DEFAULT '',updated REAL)")

def topic_outcome(p, topic, kind):
    with closing(p.db()) as db, db:
        topic_setup(db)
        db.execute("INSERT OR IGNORE INTO topic_guard(topic,updated) VALUES(?,?)",(topic,time.time()))
        if kind=='success':
            db.execute("UPDATE topic_guard SET empty_steps=0,cooldown_until=0,kind=?,updated=? WHERE topic=?",(kind,time.time(),topic))
        else:
            db.execute("UPDATE topic_guard SET empty_steps=empty_steps+1,kind=?,updated=? WHERE topic=?",(kind,time.time(),topic))
            db.execute("UPDATE topic_guard SET cooldown_until=? WHERE topic=? AND empty_steps>=3",(time.time()+3600,topic))

def topic_wait(p, topic):
    with closing(p.db()) as db:
        topic_setup(db)
        row=db.execute('SELECT cooldown_until FROM topic_guard WHERE topic=?',(topic,)).fetchone()
        return max(0,(row[0] if row else 0)-time.time())

def classify_step(root, arguments):
    # At most three tiny outcome records, never read the accumulated production log.
    import json
    try:key=arguments[arguments.index('--work-key')+1]
    except (ValueError,IndexError):return 'unknown'
    kinds=[]
    for i in range(1,4):
        folder=Path(root)/'agent_runs'/key/('attempt_%02d'%i)
        if any((folder/(name+'_rejected.json')).exists() for name in ('generate','review')):kinds.append('provider_rejection')
        path=folder/'outcome.json'
        if not path.exists():continue
        data=json.loads(path.read_text())
        if data.get('review_evidence_conflict'):kinds.append('review_conflict')
        for item in data.get('rejected',[]):
            reason=str(item.get('reason','')).lower()
            kinds.append('duplicate' if any(x in reason for x in ('neighbor','duplicate','matches','similar')) else 'review_rejection')
        if data.get('format_rejected') or data.get('review_format_rejected'):kinds.append('format_error')
    return ','.join(sorted(set(kinds))) or 'no_accepted_prompt'
