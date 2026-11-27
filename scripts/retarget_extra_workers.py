"""Bounded supplemental slot range; existing worker groups stay live."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fcntl,json,os,signal,sys,threading,time
from review_retarget import Retarget
start=int(os.environ.get('MOTIONFARM_SLOT_START','2'));end=int(os.environ.get('MOTIONFARM_SLOT_END','8'))
if not 2<=start<end<=int(os.environ.get('MOTIONFARM_RETARGET_SLOT_LIMIT','16')):raise ValueError('Invalid supplemental slot range')
suffix='' if (start,end)==(2,8) else '-%d-%d'%(start,end)
root=Path(sys.argv[1]).resolve();queue=Retarget(root);stop=threading.Event()
for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
with (queue.folder/('extra-workers'+suffix+'.lock')).open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with ThreadPoolExecutor(max_workers=end-start,thread_name_prefix='retarget-extra') as pool:
        pending={}
        while not stop.is_set():
            for slot in range(start,end):
                if slot in pending and pending[slot].done():
                    try:pending.pop(slot).result()
                    except Exception as exc:print('Slot',slot,type(exc).__name__,str(exc)[:200],flush=True)
                if slot not in pending:pending[slot]=pool.submit(queue.tick,slot,True,stop)
            tmp=queue.folder/('extra-workers'+suffix+'.tmp')
            tmp.write_text(json.dumps(dict(sampled_at=time.time(),pid=os.getpid(),slots=list(range(start,end)),capacity=end-start)))
            os.replace(tmp,queue.folder/('extra-workers'+suffix+'.json'))
            stop.wait(3)
