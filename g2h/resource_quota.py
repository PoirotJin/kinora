"""Durable user resource reservations, independent of best-effort telemetry.

Reserve one bounded work quantum at claim time, not an entire large request.
Uncertain/crashed quanta remain charged conservatively; successful settlement is
idempotent. Storage is a cumulative produced-byte allowance (not disk occupancy).
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time

DAY=86400
DEFAULTS={'motion_seconds':120000, 'gpu_seconds':21600,
          'retarget_seconds':21600, 'storage_bytes':20*1024**3}


class QuotaExceeded(PermissionError):
    pass


def limits():
    return {name:int(os.environ.get('MOTIONFARM_USER_'+name.upper(),value))
            for name,value in DEFAULTS.items()}


def connect(root):
    path=Path(root)/'review/resource_quota.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    except FileExistsError:pass
    else:os.close(fd)
    db=sqlite3.connect(path,timeout=3)
    db.executescript('''CREATE TABLE IF NOT EXISTS reservations(
      id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,ts REAL NOT NULL,
      reserved REAL NOT NULL,charged REAL NOT NULL,state TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS resource_owner_kind_ts ON reservations(owner,kind,ts);
    ''')
    return db


def usage(db,owner,kind,now):
    # Storage limits cumulative output, including retained/archived results.
    cutoff=0 if kind=='storage_bytes' else now-DAY
    return float(db.execute('SELECT coalesce(sum(charged),0) FROM reservations WHERE owner=? AND kind=? AND ts>?',
                            (owner,kind,cutoff)).fetchone()[0])


def reserve(root,key,owner,amounts,role='user'):
    """Atomic all-or-nothing admission; repeated IDs never double-reserve."""
    if not key or not owner or not amounts or not set(amounts)<=set(DEFAULTS):
        raise ValueError('Invalid resource reservation')
    if any(not isinstance(v,(int,float)) or not 0<=v<float('inf') for v in amounts.values()):
        raise ValueError('Invalid resource quantity')
    caps=limits();now=time.time()
    with closing(connect(root)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        existing={r[0]:r for r in db.execute('SELECT kind,owner,reserved FROM reservations WHERE id IN ('+
                  ','.join('?' for _ in amounts)+')',[key+':'+kind for kind in amounts])}
        for kind,amount in amounts.items():
            old=existing.get(kind)
            if old:
                if old[1]!=owner or old[2]!=amount:raise ValueError('Reservation ID reused with different values')
                continue
            used=usage(db,owner,kind,now)
            if role!='admin' and used+amount>caps[kind]:
                scope='累计生成存储' if kind=='storage_bytes' else '近 24 小时'
                raise QuotaExceeded('%s %s 配额不足（已占用 %.0f，需预留 %.0f，上限 %.0f）；保留队列，请稍后再试或联系管理员。'%
                                    (scope,kind,used,amount,caps[kind]))
        for kind,amount in amounts.items():
            db.execute('INSERT OR IGNORE INTO reservations VALUES(?,?,?,?,?,?,?)',
                       (key+':'+kind,owner,kind,now,amount,amount,'reserved'))
    return key


def settle(root,key,actual):
    if not set(actual)<=set(DEFAULTS) or any(not isinstance(v,(int,float)) or not 0<=v<float('inf') for v in actual.values()):
        raise ValueError('Invalid settlement')
    with closing(connect(root)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        for kind,amount in actual.items():
            changed=db.execute("UPDATE reservations SET charged=?,state='settled' WHERE id=? AND state='reserved'",
                               (amount,key+':'+kind)).rowcount
            if not changed and not db.execute('SELECT 1 FROM reservations WHERE id=?',(key+':'+kind,)).fetchone():
                raise ValueError('Missing resource reservation')


def snapshot(root,owner,role='user'):
    caps=limits();now=time.time()
    with closing(connect(root)) as db:
        values={kind:dict(used=round(usage(db,owner,kind,now),2),limit=None if role=='admin' else cap,
                          window='lifetime_generated' if kind=='storage_bytes' else '24h') for kind,cap in caps.items()}
    return {'limited':role!='admin','resources':values}
