"""One bounded, hot-configurable dispatcher; inherited slot locks protect live jobs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

DEFAULT = dict(total_slots=16, external_scheduler=True, min_available_bytes=4*1024**3)
DISPATCH_NOTICES = {
    'offline': '重定向调度器离线，恢复后将自动继续排队作业。',
    'low_memory': '重定向调度器因可用内存不足暂停，恢复后将自动继续排队作业。',
    'unavailable': '无法读取重定向调度配置，排队作业已保存。',
}

def profile(root):
    path=Path(root)/'review/retarget/scheduler.json'
    data=dict(DEFAULT)
    if path.exists():data.update(json.loads(path.read_text()))
    if type(data['total_slots']) is not int or not 1<=data['total_slots']<=64:
        raise ValueError('total_slots must be 1..64')
    if type(data['external_scheduler']) is not bool:
        raise ValueError('external_scheduler must be boolean')
    if type(data['min_available_bytes']) is not int or data['min_available_bytes']<1024**3:
        raise ValueError('min_available_bytes must be >= 1 GiB')
    return data

def health(root, now=None):
    try:
        if not profile(root)['external_scheduler']:
            return 'internal'
    except (OSError, ValueError, TypeError):
        return 'unavailable'
    try:
        status = json.loads((Path(root)/'review/retarget/scheduler-status.json').read_text())
        sampled = status['sampled_at']
        low_memory = status['low_memory']
        if type(sampled) not in (float, int) or not math.isfinite(sampled) or type(low_memory) is not bool:
            return 'offline'
        age = (time.time() if now is None else now) - sampled
        if not 0 <= age <= 15:
            return 'offline'
        return 'low_memory' if low_memory else 'online'
    except (OSError, ValueError, KeyError, TypeError):
        return 'offline'

def available_memory():
    with open('/proc/meminfo') as f:
        return next(int(line.split()[1])*1024 for line in f if line.startswith('MemAvailable:'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve()
    # All workers understand the same hard upper bound; profile controls admission.
    os.environ['MOTIONFARM_RETARGET_SLOT_LIMIT']='64'
    from review_retarget import Retarget
    queue=Retarget(root);stop=threading.Event();logging.basicConfig(level=logging.INFO)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    with (queue.folder/'scheduler.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=64,thread_name_prefix='retarget-slot') as pool:
            pending={}
            while not stop.is_set():
                try:
                    config=profile(root)
                    for slot,future in list(pending.items()):
                        if future.done():
                            del pending[slot]
                            try:future.result()
                            except Exception:logging.exception('Slot %d failed; durable queue retained',slot)
                    low_memory=available_memory()<config['min_available_bytes']
                    if config['external_scheduler'] and not low_memory:
                        for slot in range(config['total_slots']):
                            if slot not in pending:pending[slot]=pool.submit(queue.tick,slot,True,stop)
                    status=dict(sampled_at=time.time(),pid=os.getpid(),configured_slots=config['total_slots'],
                                dispatching_slots=sorted(pending),low_memory=low_memory,policy='shared slot locks; fair owner queue; drain on stop')
                    temporary=queue.folder/'scheduler-status.tmp'
                    temporary.write_text(json.dumps(status));os.replace(temporary,queue.folder/'scheduler-status.json')
                except Exception:logging.exception('Scheduler tick failed; no new jobs admitted')
                stop.wait(3)

if __name__=='__main__':main()
