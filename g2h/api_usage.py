"""Durable API admission ledger and separate best-effort telemetry; no prompts/keys."""
from contextlib import contextmanager, closing
import contextvars
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

PATH=Path(__file__).resolve().parents[1]/'outputs/text_motion_farm/review/api_usage.sqlite3'
current_user=contextvars.ContextVar('api_user', default='')
current_path=contextvars.ContextVar('api_path', default=PATH)
current_attribution=contextvars.ContextVar('api_attribution',default={})
current_reservation=contextvars.ContextVar('api_reservation',default=None)
WINDOW_5H=5*3600
WINDOW_WEEK=7*86400
# Ordinary-user caps. One custom job is typically ~1.3k–2.4k chat tokens plus ~40 embedding tokens.
# 80k / 5h ≈ 30–40 jobs with retries; 400k / week ≈ 150 jobs. Admin is unlimited.
LIMIT_5H=int(os.environ.get('MOTIONFARM_USER_API_TOKENS_5H','80000'))
LIMIT_WEEK=int(os.environ.get('MOTIONFARM_USER_API_TOKENS_WEEK','400000'))


class QuotaExceeded(PermissionError):
    pass


def ledger(path):
    """A separate durable DB: telemetry failures cannot lose quota charges."""
    path=Path(path).with_name('api_quota.sqlite3');path.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    except FileExistsError:pass
    else:os.close(fd)
    db=sqlite3.connect(path,timeout=3)
    db.executescript('''CREATE TABLE IF NOT EXISTS reservations(
      id TEXT PRIMARY KEY,username TEXT NOT NULL,ts REAL NOT NULL,model TEXT,kind TEXT,
      reserved INTEGER NOT NULL,charged INTEGER NOT NULL,state TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS quota_user_ts ON reservations(username,ts);
    ''')
    return db


def reserve_api(model,kind,amount,path=None):
    username=current_user.get();path=Path(path or current_path.get())
    if not username:return None  # Automatic production is administrator-owned.
    accounts=path.with_name('accounts.sqlite3')
    with closing(sqlite3.connect(accounts.as_uri()+'?mode=ro',uri=True,timeout=2)) as auth:
        user=auth.execute('SELECT role,disabled FROM users WHERE username=?',(username,)).fetchone()
    if not user or user[1]:raise PermissionError('账户已停用或不存在，停止 API 请求')
    identifier=uuid.uuid4().hex;now=time.time()
    # Legacy spend is immutable for wrapped callers. Read it before taking the
    # admission writer lock; all new calls serialize on the ledger transaction.
    legacy={window:_legacy_spent(username,now-window,path) for window in (WINDOW_5H,WINDOW_WEEK)}
    with closing(ledger(path)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        for window,limit in ((WINDOW_5H,LIMIT_5H),(WINDOW_WEEK,LIMIT_WEEK)):
            used=legacy[window]+db.execute('SELECT coalesce(sum(charged),0) FROM reservations WHERE username=? AND ts>?',
                                           (username,now-window)).fetchone()[0]
            if user[0]!='admin' and used+amount>limit:
                raise QuotaExceeded('Qwen Token 配额不足：已占用 %s，本次预留 %s，上限 %s；保留队列，等待额度恢复。'%(used,amount,limit))
        db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?)',(identifier,username,now,model,kind,amount,amount,'reserved'))
    return (path,identifier)


def settle_api(ticket, response=None):
    if ticket is None:return
    usage=(response or {}).get('usage') or {}
    total=usage.get('total_tokens')
    if type(total) is not int or total<0:
        inp=usage.get('prompt_tokens',usage.get('input_tokens'))
        out=usage.get('completion_tokens',usage.get('output_tokens',0))
        total=inp+out if type(inp) is int and type(out) is int and inp>=0 and out>=0 else None
    with closing(ledger(ticket[0])) as db,db:
        # Unknown transport/billing outcomes retain the full reservation. Never
        # automatically refund after timeout or process crash.
        db.execute("UPDATE reservations SET charged=CASE WHEN ? IS NULL THEN reserved ELSE ? END,state=? WHERE id=? AND state='reserved'",
                   (total,total,'unknown' if total is None else 'settled',ticket[1]))


@contextmanager
def budgeted_request(model,kind,input_value,max_output_tokens=0):
    # UTF-8 bytes plus generous framing overhead is deliberately conservative;
    # provider usage is authoritative at settlement. No tokenizer dependency.
    amount=len(json.dumps(input_value,ensure_ascii=False).encode('utf-8'))+1024+int(max_output_tokens)
    ticket=reserve_api(model,kind,amount)
    token=current_reservation.set(ticket)
    try:
        yield
    finally:
        current_reservation.reset(token)


@contextmanager
def billed_to(username, path=None, purpose='', job_id='', provider=''):
    attribution_token=current_attribution.set(dict(purpose=purpose,job_id=job_id,provider=provider))
    user_token=current_user.set(username or '')
    path_token=current_path.set(Path(path) if path else current_path.get())
    try: yield
    finally:
        current_attribution.reset(attribution_token)
        current_user.reset(user_token)
        current_path.reset(path_token)


@contextmanager
def attributed_call(purpose='', provider='', job_id=''):
    """Fill missing automatic-call attribution, preserving caller job/purpose."""
    current=current_attribution.get()
    token=current_attribution.set({key:current.get(key) or value for key,value in dict(purpose=purpose,provider=provider,job_id=job_id).items()})
    try:yield
    finally:current_attribution.reset(token)


def connect(path=PATH):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=.5)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,ts REAL,model TEXT,kind TEXT,status TEXT,
        input INTEGER,output INTEGER,total INTEGER,known INTEGER);
      CREATE INDEX IF NOT EXISTS api_time ON events(ts);
      CREATE TABLE IF NOT EXISTS cost_events(event_id TEXT PRIMARY KEY,username TEXT,provider TEXT,purpose TEXT,job_id TEXT,
        cached INTEGER,currency TEXT,amount REAL,price_version TEXT,state TEXT,details TEXT);
      CREATE TABLE IF NOT EXISTS totals(model TEXT,kind TEXT,calls INTEGER,errors INTEGER,
        input INTEGER,output INTEGER,total INTEGER,unknown INTEGER,PRIMARY KEY(model,kind));
      CREATE TABLE IF NOT EXISTS spend(id INTEGER PRIMARY KEY,username TEXT NOT NULL,ts REAL NOT NULL,
        tokens INTEGER NOT NULL,kind TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS spend_user_ts ON spend(username,ts);
    ''')
    return db


def record(model,kind,response=None,status='ok',path=None,event_id=None,ts=None):
    path=Path(path or current_path.get())
    ticket=current_reservation.get()
    if ticket is not None:
        try:settle_api(ticket,response)
        except (OSError,sqlite3.Error,ValueError,TypeError):
            # The pre-call reservation remains durable and charged. Preserve a
            # successful result instead of resending a potentially billable call.
            print('MotionFarm: API settlement unavailable; full reservation retained',file=sys.stderr)
    try:
        response=response or {};usage=response.get('usage') or {}
        def number(*keys):
            for key in keys:
                v=usage.get(key)
                if type(v) is int and v>=0:return v
            return 0
        inp=number('prompt_tokens','input_tokens');out=number('completion_tokens','output_tokens')
        total=number('total_tokens') or inp+out
        known=int(bool(usage) and any(k in usage for k in ('total_tokens','prompt_tokens','input_tokens')))
        identifier=event_id or response.get('id') or str(uuid.uuid4())
        identifier=hashlib.sha256((kind+':'+str(identifier)).encode()).hexdigest()
        username=current_user.get()
        charged=total if known and total>0 else (512 if kind=='chat' else 64)
        now=ts or time.time()
        with closing(connect(path)) as db,db:
            added=db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,now,model,kind,status,inp,out,total,known)).rowcount
            if added:
                from g2h.api_costs import load_prices, estimate
                details=usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
                if not isinstance(details,dict):details={}
                cached=details.get('cached_tokens',usage.get('cached_tokens'))
                if type(cached) is not int or cached<0:cached=None
                attr=current_attribution.get()
                # A total-only response cannot be split into priced input/output.
                cost_known=known and any(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','input_tokens')) and (kind=='embedding' or any(type(usage.get(k)) is int and usage[k]>=0 for k in ('completion_tokens','output_tokens')))
                cost=estimate(load_prices(path.with_name('api_prices.json')),model,kind,attr.get('provider',''),inp,out,cached,cost_known)
                db.execute('INSERT INTO cost_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identifier,username,attr.get('provider',''),attr.get('purpose',''),attr.get('job_id',''),cached,cost['currency'],cost['amount'],cost['price_version'],cost['state'],json.dumps(cost.get('details',{}))))
                db.execute('''INSERT INTO totals VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(model,kind) DO UPDATE SET
                    calls=calls+1,errors=errors+excluded.errors,input=input+excluded.input,
                    output=output+excluded.output,total=total+excluded.total,unknown=unknown+excluded.unknown''',
                    (model,kind,int(status!='ok'),inp,out,total,int(not known)))
                if username and status=='ok' and ticket is None:
                    db.execute('INSERT INTO spend(username,ts,tokens,kind) VALUES(?,?,?,?)',
                        (username,now,charged,kind))
        return True
    except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
        # A missing event must not silently look like a complete cost total.
        try:
            from g2h.observability import failure
            failure(path.parent.parent,'api_usage_write',exc)
        except Exception:
            pass
        print('MotionFarm: API telemetry write failed; generation result preserved',file=sys.stderr)
        return False


def _legacy_spent(username, since, path=PATH):
    if not username: return 0
    path=Path(path).resolve()
    if not path.is_file():return 0
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='spend'").fetchone():return 0
        row=db.execute('SELECT coalesce(sum(tokens),0) FROM spend WHERE username=? AND ts>?',(username,since)).fetchone()
    return int(row[0] if row else 0)


def spent(username, since, path=PATH):
    if not username:return 0
    legacy=_legacy_spent(username,since,path)
    with closing(ledger(path)) as db:
        value=db.execute('SELECT coalesce(sum(charged),0) FROM reservations WHERE username=? AND ts>?',
                         (username,since)).fetchone()[0]
    return legacy+int(value)


def quota(username, role='user', path=PATH):
    if role=='admin':
        return dict(limited=False,window_5h=dict(used=0,limit=None),week=dict(used=0,limit=None))
    now=time.time()
    used5=spent(username,now-WINDOW_5H,path)
    usedw=spent(username,now-WINDOW_WEEK,path)
    return dict(limited=True,
        window_5h=dict(used=used5,limit=LIMIT_5H,remaining=max(0,LIMIT_5H-used5)),
        week=dict(used=usedw,limit=LIMIT_WEEK,remaining=max(0,LIMIT_WEEK-usedw)))


def check_quota(username, role='user', extra=0, path=PATH):
    if role=='admin' or not username: return
    info=quota(username,role,path)
    if info['window_5h']['used']+extra>=LIMIT_5H:
        raise QuotaExceeded('近 5 小时 Qwen 额度已用完（%s / %s token）。请稍后再试或联系管理员。'%(info['window_5h']['used'],LIMIT_5H))
    if info['week']['used']+extra>=LIMIT_WEEK:
        raise QuotaExceeded('本周 Qwen 额度已用完（%s / %s token）。请下周再试或联系管理员。'%(info['week']['used'],LIMIT_WEEK))


def import_history(root,path=PATH):
    """One-time bounded import: up to 100 stored successful response files."""
    root=Path(root);imported=0;scanned=0
    for pattern in ('agent_runs/*/attempt_*/*_response.json','prompts/*/response.json'):
        for source in root.glob(pattern):
            if scanned>=100:return {'scanned':scanned,'recorded':imported,'capped':True}
            scanned+=1
            if source.stat().st_size>2_000_000:continue
            data=json.loads(source.read_text())
            if 'usage' not in data:continue
            imported+=int(record(data.get('model','unknown'),'chat',data,path=path,
                event_id=data.get('id') or str(source.relative_to(root)),ts=source.stat().st_mtime))
    return {'scanned':scanned,'recorded':imported,'capped':False}
