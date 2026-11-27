"""Bounded sidecar reconciliation; never run inference or alter motion payloads.

Modes (all bounded by --limit and a --seconds wall-clock budget, so a run stops
cleanly before an outer `timeout` and can simply be repeated):
  default   index unscreened motions among the newest --limit and oldest 6
  --gaps    index unscreened motions anywhere in the catalogue, newest first
  --recheck re-screen indexed motions whose metrics predate the current VERSION
  --motion  print gait/direction evidence for named motions
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h.agent_learning import index_record,rescreen,reviewed,connect,VERSION
from g2h.farm_index import read_motion,short_id

PENDING='Motion is not committed yet'

def catalog(root):
    return closing(sqlite3.connect((root/'prompts.sqlite3').as_uri()+'?mode=ro',uri=True))

class Tally:
    """Counts outcomes; failures are kept apart from motions that are simply not committed."""
    def __init__(self,seconds):
        self.deadline=time.monotonic()+seconds;self.done=0;self.pending=0;self.errors=Counter();self.examples=[]
    def expired(self):return time.monotonic()>=self.deadline
    def attempt(self,mid,work):
        try:self.done+=bool(work())
        except Exception as exc:  # one bad motion must not stop the batch; it is reported instead
            if isinstance(exc,ValueError) and str(exc)==PENDING:self.pending+=1;return
            self.errors[type(exc).__name__]+=1
            if len(self.examples)<10:self.examples.append(dict(motion_id=mid,error=f'{type(exc).__name__}: {exc}'[:200]))
    def report(self,**extra):
        print(json.dumps(dict(**extra,pending=self.pending,failed=sum(self.errors.values()),errors=dict(self.errors),
                              examples=self.examples,stopped_by_time=self.expired()),ensure_ascii=False),flush=True)

def stale_ids(checks,limit):
    return [r[0] for r in checks.execute("SELECT motion_id FROM checks WHERE json_extract(metrics,'$.version') IS NOT ? "
                                         'ORDER BY created DESC LIMIT ?',(VERSION,limit))]

def recheck(root,limit,seconds):
    """Re-screen up to `limit` motions whose stored metrics predate VERSION, newest first."""
    with closing(connect(root)) as checks:ids=stale_ids(checks,limit)
    tally=Tally(seconds)
    with catalog(root) as db:
        for mid in ids:
            if tally.expired():break
            tally.attempt(mid,lambda:rescreen(root,read_motion(db,root,mid)))
    with closing(connect(root)) as checks:
        remaining=checks.execute("SELECT COUNT(*) FROM checks WHERE json_extract(metrics,'$.version') IS NOT ?",(VERSION,)).fetchone()[0]
    tally.report(mode='recheck',version=VERSION,rescreened=tally.done,remaining=remaining,limit=limit)

def inspect(root,ids):
    """Screen named motions and print their gait/direction evidence. Indexed rows
    from an older version are updated; nothing else is written."""
    from g2h.agent_learning import analyze
    with catalog(root) as db:
        for mid in ids:
            try:record=read_motion(db,root,mid)
            except (ValueError,KeyError,FileNotFoundError) as exc:
                print(json.dumps(dict(motion_id=mid,error=str(exc)),ensure_ascii=False),flush=True);continue
            metrics,flags,_=analyze(record);rescreen(root,record)
            print(json.dumps(dict(motion_id=mid,text=record.get('text'),flags=flags,gait=metrics['gait']['metrics'],
                                  body_quality=metrics['body_quality'].get('flags')),ensure_ascii=False),flush=True)

def run(root,limit,seconds,gaps=False):
    tally=Tally(seconds);scanned=0
    with closing(connect(root)) as checks,catalog(root) as db:
        if gaps:
            # Whole catalogue: one pass over motion IDs, newest first, skipping screened ones.
            known={r[0] for r in checks.execute('SELECT motion_id FROM checks')}
            numbers=(r[0] for r in db.execute('SELECT number FROM motion_links ORDER BY number DESC'))
        else:
            known=None
            numbers=[r[0] for r in db.execute('SELECT number FROM motion_links ORDER BY number DESC LIMIT ?',(limit,))]
            numbers=dict.fromkeys(numbers+[r[0] for r in db.execute('SELECT number FROM motion_links ORDER BY number LIMIT 6')])
        for n in numbers:
            if tally.expired() or (gaps and tally.done+tally.pending+sum(tally.errors.values())>=limit):break
            mid=short_id(n);scanned+=1
            if mid in known if known is not None else checks.execute('SELECT 1 FROM checks WHERE motion_id=?',(mid,)).fetchone():continue
            tally.attempt(mid,lambda:index_record(root,read_motion(db,root,mid)) or True)
    reviews=root/'review/reviews.sqlite3'
    if reviews.exists():
        with closing(sqlite3.connect(reviews.as_uri()+'?mode=ro',uri=True)) as db:
            for row in db.execute('SELECT body FROM latest ORDER BY rowid DESC LIMIT 200'):reviewed(root,json.loads(row[0]))
    tally.report(mode='gaps' if gaps else 'window',indexed=tally.done,scanned=scanned,limit=limit)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--limit',type=int,default=60)
    p.add_argument('--recheck',action='store_true',help='re-screen indexed motions from an older screening version')
    p.add_argument('--gaps',action='store_true',help='index unscreened motions across the whole catalogue')
    p.add_argument('--seconds',type=float,default=240,help='wall-clock budget; keep below the outer timeout')
    p.add_argument('--motion',action='append',default=[],help='print gait/direction evidence for a motion ID (repeatable)')
    a=p.parse_args();a.root=a.root.resolve()  # Path.as_uri() needs an absolute path
    cap=50000 if a.recheck or a.gaps else 200
    if not 1<=a.limit<=cap:p.error(f'limit must be 1..{cap}')
    if not 1<=a.seconds<=3600:p.error('seconds must be 1..3600')
    if a.motion:inspect(a.root,a.motion[:50])
    elif a.recheck:recheck(a.root,a.limit,a.seconds)
    else:run(a.root,a.limit,a.seconds,gaps=a.gaps)
