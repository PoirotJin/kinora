"""Bounded control threads; durable work stays in SQLite, heavy work in processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from contextlib import closing
import threading
import time


def wait_process(child, stop=None):
    while True:
        try:
            code = child.wait(timeout=1)
            if code:
                raise RuntimeError('Stage process exited with code %d' % code)
            return code
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                # The child retains its flock and durable job; a new supervisor
                # cannot duplicate it. Never terminate work on a web reload.
                return None


class Stage:
    def __init__(self, name, workers):
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError('Stage workers must be 1..4')
        self.name, self.capacity = name, workers
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mf-'+name)
        self.lock = threading.Lock()
        self.jobs, self.cooldown = {}, {}
        self.completed = self.failed = 0
        self.last_error = None
        self.closed = False

    def submit(self, key, function, *args):
        with self.lock:
            if self.closed or key in self.jobs or len(self.jobs) >= self.capacity:
                return False
            if time.monotonic() < self.cooldown.get(key, 0):
                return False
            # No unlimited executor queue: admission counts running + submitted.
            future = self.executor.submit(function, *args)
            self.jobs[key] = future
        # add_done_callback may execute synchronously; never hold self.lock here.
        future.add_done_callback(lambda result: self._done(key, result))
        return True

    def _done(self, key, future):
        error = None if future.cancelled() else future.exception()
        with self.lock:
            self.jobs.pop(key, None)
            if error is not None:
                self.failed += 1
                self.last_error = type(error).__name__ + ': stage failed; inspect its log'
                self.cooldown[key] = time.monotonic() + 10
            elif not future.cancelled():
                self.completed += 1

    def snapshot(self):
        with self.lock:
            running = sum(f.running() for f in self.jobs.values())
            return dict(name=self.name,capacity=self.capacity,running=running,
                        pending=len(self.jobs)-running,completed=self.completed,
                        failed=self.failed,last_error=self.last_error)

    def close(self):
        with self.lock:self.closed = True
        self.executor.shutdown(wait=True,cancel_futures=True)


class Pipeline:
    def __init__(self, root):
        self.root = Path(root)
        self.stages = {name:Stage(name,n) for name,n in
                       [('control',1),('prepare',1),('gpu',1),('retarget',2)]}

    def snapshot(self):
        return dict(version=1,sampled_at=time.time(),pid=os.getpid(),
                    stages=[s.snapshot() for s in self.stages.values()],
                    policy='SQLite durable queues; bounded dispatch; shared GPU flock; per-user fair turns',
                    storage_capacity=1)

    def publish(self):
        folder=self.root/'review/pipeline';folder.mkdir(parents=True,exist_ok=True)
        target=folder/'status.json';temporary=folder/'status.tmp'
        data=self.snapshot();data['queues']=queue_counts(self.root)
        temporary.write_text(json.dumps(data));temporary.chmod(0o600)
        os.replace(temporary,target)

    def close(self):
        for stage in self.stages.values():stage.close()


def queue_counts(root):
    result={}
    for name,table in [('custom','requests'),('retarget','jobs'),('storage','jobs'),('production','jobs')]:
        path=Path(root)/'review'/name/'jobs.sqlite3'
        if not path.exists():continue
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                deadline=time.monotonic()+.2
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                result[name]=dict(db.execute("SELECT status,count(*) FROM "+table+" WHERE status IN ('queued','preparing','ready','running','stopping') GROUP BY status"))
        except sqlite3.Error:result[name]={'unavailable':True}
    return result


def status(root):
    from retarget_scheduler import health, DISPATCH_NOTICES
    scheduler = health(root)
    try:
        data=json.loads((Path(root)/'review/pipeline/status.json').read_text())
    except (OSError,ValueError):
        data=dict(available=False,stale=True,stages=[])
    else:
        data['available']=True
        data['stale']=time.time()-data['sampled_at']>15
    data['retarget_scheduler']=dict(state=scheduler,notice=DISPATCH_NOTICES.get(scheduler))
    return data
