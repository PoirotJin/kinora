"""Independent, singleton owner of production and custom-job scheduling."""
import argparse
import fcntl
import logging
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_production import Production
from review_custom import Custom
from review_retarget import Retarget
from pipeline_pool import Pipeline
from review_motion_edits import MotionEdits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    temporary = Path(os.environ['SQLITE_TMPDIR'])
    if not temporary.is_dir() or not os.access(temporary, os.W_OK | os.X_OK):
        raise RuntimeError('Persistent SQLite temporary directory is unavailable')
    production = Production(args.root, args.config, worker=True)
    with (production.folder / 'supervisor.lock').open('a') as gate:
        fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        pipeline=Pipeline(args.root)
        custom=Custom(args.root);retarget=Retarget(args.root)
        edits=MotionEdits(args.root);edit_turn=False
        logging.info('Independent bounded pipeline supervisor ready; tmp=%s', temporary)
        try:
            while not stop.is_set():
                try:
                    # Sample the settled previous interval, not the brief burst
                    # of queue checks just submitted below.
                    pipeline.publish()
                except Exception:
                    logging.exception('Pipeline heartbeat unavailable')
                try:
                    pipeline.stages['control'].submit('production',production.tick,False)
                except Exception:
                    logging.exception('Production dispatch unavailable')
                try:
                    edit_turn = not edit_turn
                    if edit_turn: edits.tick(pipeline,stop)
                    custom.tick(pipeline=pipeline,stop=stop)
                    if not edit_turn: edits.tick(pipeline,stop)
                except Exception:
                    logging.exception('Custom dispatch unavailable')
                try:
                    from retarget_scheduler import profile
                    for slot in (() if (args.root/'review/retarget/scheduler.json').exists() and profile(args.root)['external_scheduler'] else range(2)):
                        pipeline.stages['retarget'].submit('retarget-'+str(slot),retarget.tick,slot,True,stop)
                except Exception:
                    logging.exception('Pipeline dispatch failed; durable jobs retained')
                stop.wait(3)
        finally:
            stop.set();pipeline.close()


if __name__ == '__main__':
    main()
