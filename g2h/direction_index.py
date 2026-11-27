"""Derived direction index, isolated from generation and human review databases."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from g2h.motion_semantics import VERSION

FILTERS = {'direction_pass':'pass','direction_fail':'fail',
           'direction_review':'review','direction_not_checked':'not_checked'}
STATUSES = ('','pending','accepted','rejected','uncertain',*FILTERS)


def path(root):
    return Path(root)/'review/direction.sqlite3'


def connect(root):
    p=path(root);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=.15)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS results(number INTEGER,version TEXT,status TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,report TEXT NOT NULL,checked REAL NOT NULL,
        PRIMARY KEY(number,version));
      CREATE INDEX IF NOT EXISTS direction_status ON results(version,status,number);
      CREATE TABLE IF NOT EXISTS counts(version TEXT,status TEXT,total INTEGER NOT NULL,PRIMARY KEY(version,status));
      CREATE TRIGGER IF NOT EXISTS direction_insert AFTER INSERT ON results BEGIN
        INSERT INTO counts VALUES(new.version,new.status,1) ON CONFLICT(version,status) DO UPDATE SET total=total+1; END;
      CREATE TRIGGER IF NOT EXISTS direction_update AFTER UPDATE OF status ON results WHEN old.status!=new.status BEGIN
        UPDATE counts SET total=total-1 WHERE version=old.version AND status=old.status;
        INSERT INTO counts VALUES(new.version,new.status,1) ON CONFLICT(version,status) DO UPDATE SET total=total+1; END;
      CREATE TABLE IF NOT EXISTS progress(topic TEXT,version TEXT,cursor INTEGER NOT NULL DEFAULT 0,
        upper_bound INTEGER NOT NULL,target INTEGER NOT NULL,scanned INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(topic,version));
      CREATE TABLE IF NOT EXISTS worker(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT,updated REAL,note TEXT);
    ''')
    return db


def put(db, number, digest, report):
    if report['version']!=VERSION or report['status'] not in ('pass','fail','review'):
        raise ValueError('Invalid direction report')
    db.execute('''INSERT INTO results VALUES(?,?,?,?,?,?) ON CONFLICT(number,version)
        DO UPDATE SET status=excluded.status,source_sha256=excluded.source_sha256,
        report=excluded.report,checked=excluded.checked''',
        (number,VERSION,report['status'],digest,json.dumps(report,ensure_ascii=False,allow_nan=False),time.time()))


def save(root, mid, digest, report):
    from g2h.farm_index import motion_number
    number=motion_number(mid)
    with closing(connect(root)) as db,db:
        old=db.execute('SELECT source_sha256,status FROM results WHERE number=? AND version=?',(number,VERSION)).fetchone()
        if old!=(digest,report['status']):put(db,number,digest,report)


def attach(db, root):
    if not path(root).exists():
        with closing(connect(root)):pass
    db.execute('ATTACH DATABASE ? AS directiondb',(str(path(root)),))


def clause(status, column='m.number'):
    if status not in FILTERS:return '',[]
    if status=='direction_not_checked':
        return f' AND {column} NOT IN (SELECT number FROM directiondb.results WHERE version=?)',[VERSION]
    return f' AND {column} IN (SELECT number FROM directiondb.results WHERE version=? AND status=?)',[VERSION,FILTERS[status]]


def statuses(root, ids):
    from g2h.farm_index import motion_number
    pairs={motion_number(mid):mid for mid in ids if not mid.startswith(('mimp','ma_'))}
    if not pairs or not path(root).exists():return {}
    with closing(sqlite3.connect(path(root).as_uri()+'?mode=ro',uri=True,timeout=.15)) as db:
        rows=db.execute('SELECT number,status FROM results WHERE version=? AND number IN ('+','.join('?' for _ in pairs)+')',[VERSION,*pairs])
        return {pairs[n]:status for n,status in rows}


def summary(root):
    if not path(root).exists():return dict(checked=0,scanned=0,target=0,state='not_started')
    with closing(sqlite3.connect(path(root).as_uri()+'?mode=ro',uri=True,timeout=.15)) as db:
        counts=dict(db.execute('SELECT status,total FROM counts WHERE version=?',(VERSION,)))
        target,scanned,errors=db.execute('SELECT coalesce(sum(target),0),coalesce(sum(scanned),0),coalesce(sum(errors),0) FROM progress WHERE version=?',(VERSION,)).fetchone()
        worker=db.execute('SELECT state,updated,note FROM worker WHERE id=1').fetchone()
    return dict(version=VERSION,counts=counts,checked=sum(counts.values()),target=target,scanned=scanned,errors=errors,
                state=worker[0] if worker else 'not_started',updated=worker[1] if worker else None,
                note=worker[2] if worker else '')
