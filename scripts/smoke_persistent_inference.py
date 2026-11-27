"""Two real 4-second clips in an isolated tree; no API calls or farm DB writes.

Run as the project's jfc account with production paused. Uses the production
GPU flock as well as an isolated worker lock, and requests worker shutdown even
on failure. This is a narrow integration smoke test, not a throughput claim.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from inference_metadata import cache_profile, feature_key, read_metadata, write_json
from inference_worker import request_batch, shutdown_worker

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROJECT / 'benchmarks/persistent-20260920')
    parser.add_argument('--farm-root', type=Path, default=PROJECT / 'outputs/text_motion_farm')
    parser.add_argument('--submit-batch', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--gpu-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    root.relative_to(PROJECT / 'benchmarks')
    if args.submit_batch is not None:
        result = request_batch(root, args.submit_batch, args.gpu_fd, timeout_s=180)
        print(json.dumps(result), flush=True)
        return
    root.mkdir(parents=True, exist_ok=True)
    # No live production/custom inference may overlap this smoke test.
    with (args.farm_root / 'gpu_worker.lock').open('a') as production_lock:
        fcntl.flock(production_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free = subprocess.run(['nvidia-smi', '--id=0', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                              capture_output=True, text=True, check=True, timeout=3)
        if float(free.stdout.strip()) < 32 * 1024:
            raise RuntimeError('Smoke requires 32 GiB free VRAM; stop idle production worker or other GPU work first')
        work = root / 'topics/smoke/work'
        work.mkdir(parents=True, exist_ok=True)
        profile = cache_profile(PROJECT)
        results = []
        os.environ.update(OMP_NUM_THREADS='2', MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2',
                          TOKENIZERS_PARALLELISM='false', HF_HUB_OFFLINE='1')
        # The isolated socket still receives the *real* farm lock FD. A hardlink
        # provides the worker's inode check without a second scheduler lock.
        isolated_lock = root / 'gpu_worker.lock'
        if not isolated_lock.exists():
            os.link(args.farm_root / 'gpu_worker.lock', isolated_lock)
        if not isolated_lock.samefile(args.farm_root / 'gpu_worker.lock'):
            raise RuntimeError('Smoke tree has a different GPU lock; choose a fresh --root')
        lock = production_lock
        try:
            for index, text in enumerate(('A person walks forward at a comfortable pace.',
                                           'A person walks backward slowly.')):
                motion = work / ('smoke-%d.npz' % index)
                if motion.exists():
                    raise RuntimeError('Smoke outputs already exist; choose a fresh --root, do not overwrite evidence')
                job = dict(id='smoke-%d' % index, motion=str(motion),
                           features=str(work / ('text_' + feature_key(text, profile) + '.pt')),
                           spec=dict(text=text, topic='smoke', duration_s=4., seed=20260920 + index,
                                     cfg_scale=5., model='HY-Motion-1.0'))
                batch = work / 'batch.json'
                write_json(batch, [job])
                started = time.perf_counter()
                # Separate short-lived client processes model distinct collector
                # invocations, proving persistence is not merely a Python local.
                submitted = subprocess.run([sys.executable, '-u', str(Path(__file__).resolve()),
                    '--root', str(root), '--submit-batch', str(batch), '--gpu-fd', str(lock.fileno())],
                    pass_fds=(lock.fileno(),), capture_output=True, text=True, timeout=190)
                if submitted.returncode:
                    print(submitted.stderr[-4000:], flush=True)
                    raise RuntimeError('Isolated inference client failed')
                result = json.loads(submitted.stdout.strip().splitlines()[-1])
                result['wall_seconds'] = round(time.perf_counter() - started, 3)
                print('Inference response:', json.dumps(result), flush=True)
                print('Checking finite numeric arrays with numpy (no GPU import in smoke client)', flush=True)
                import numpy as np
                with np.load(motion, allow_pickle=False) as arrays:
                    fps = float(arrays['fps'])
                    shape = list(arrays['native_transl'].shape)
                    assert shape == [round(4 * fps), 3], shape
                    assert all(np.isfinite(arrays[key]).all() for key in arrays.files
                               if np.issubdtype(arrays[key].dtype, np.number))
                provenance = read_metadata(motion)
                assert provenance['persistent'] is True
                assert provenance['feature']['cache_profile'] == profile
                assert provenance['feature']['execution_device'] in ('qwen_cuda_clip_cuda', 'qwen_cuda_layer_offload_clip_cpu')
                result.update(frames=shape[0], fps=fps, finite=True, provenance=provenance)
                results.append(result)
            assert results[0]['worker_pid'] == results[1]['worker_pid'], 'Worker was not reused'
            assert [r['worker_batches'] for r in results] == [1, 2], 'Unexpected preexisting worker state'
            assert results[1]['timings']['encoder_load_seconds'] == 0
            assert results[1]['timings']['pipeline_load_seconds'] == 0
            assert results[1]['timings']['encode_seconds'] > 0, 'Second distinct prompt must use the retained encoder'
            write_json(root / 'results.json', {'batches': results, 'checks_passed': True})
            print('PASS: same worker; second prompt reused both models; finite 120-frame outputs.', flush=True)
        finally:
            shutdown_worker(root, lock.fileno())
            deadline = time.monotonic() + 5
            while (root / 'review/inference/worker.sock').exists() and time.monotonic() < deadline:
                time.sleep(.1)


if __name__ == '__main__':
    main()
