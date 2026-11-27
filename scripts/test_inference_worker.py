"""CPU-only safety tests for persistence, FD lifetime, cancellation and cache identity."""
import array
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import inference_capacity
import inference_metadata as metadata
import inference_worker as worker


def fake_runner(args, state):
    jobs = json.loads(args.batch.read_text())
    state['calls'] = state.get('calls', 0) + 1
    if jobs[0]['spec']['text'] == 'wait':
        time.sleep(8)
    if jobs[0]['spec']['text'] == 'fail':
        raise RuntimeError('mock CUDA failure')
    return {'calls': state['calls'], 'pid': os.getpid()}


@unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Inference worker requires Linux Unix-socket peer credentials')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mf-infer-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.work = self.root / 'topics/walking/work'
        self.work.mkdir(parents=True)
        self.batch = self.work / 'batch.json'
        self.write_batch()
        (self.root / 'gpu_worker.lock').touch()
        self.process = multiprocessing.get_context('spawn').Process(target=worker.serve,
            args=(self.root, fake_runner, 3))
        self.process.start()
        deadline = time.monotonic() + 4
        self.folder = self.root / 'review/inference'
        while not (self.folder / 'worker.sock').exists():
            if time.monotonic() > deadline:
                self.fail('mock worker startup timed out')
            time.sleep(.02)
        self.lock = (self.root / 'gpu_worker.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self):
        self.lock.close()
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(3)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(1)
        self.temp.cleanup()

    def write_batch(self, text='walk', motion=None):
        job = {'id': 'task1', 'features': str(self.work / 'text.pt'),
               'motion': str(motion or self.work / 'motion.npz'),
               'spec': {'model': 'HY-Motion-1.0', 'text': text,
                        'seed': 42, 'duration_s': 8, 'cfg_scale': 5}}
        self.batch.write_text(json.dumps([job]))

    def invoke(self, timeout=5, cancelled=lambda: False):
        # Direct single attempt avoids auto-spawning a real ML runner on test failure.
        return worker._request_once(self.root, self.batch, self.lock.fileno(), timeout, cancelled)

    def test_reuses_state_and_does_not_retain_scheduler_lock(self):
        first, second = self.invoke(), self.invoke()
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual((first['calls'], second['calls']), (1, 2))
        self.lock.close()
        with (self.root / 'gpu_worker.lock').open('a') as next_owner:
            fcntl.flock(next_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(self.process.is_alive())

    def test_rejects_wrong_gpu_lock_descriptor(self):
        with (self.root / 'wrong.lock').open('a') as wrong:
            with self.assertRaisesRegex(RuntimeError, 'descriptor mismatch'):
                worker._request_once(self.root, self.batch, wrong.fileno(), 5, lambda: False)
        self.assertEqual(self.invoke()['calls'], 1)

    def test_rejects_output_escape_before_runner(self):
        self.write_batch(motion=self.root / 'escape.npz')
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.invoke()
        self.write_batch()
        self.assertEqual(self.invoke()['calls'], 1)

    def test_edit_follow_root_flag_must_be_true(self):
        from g2h.generation_backend import KIMODO_MODEL, KIMODO_ADAPTER
        kimodo = multiprocessing.get_context('spawn').Process(target=worker.serve, args=(self.root, fake_runner, 3, KIMODO_MODEL))
        kimodo.start(); self.addCleanup(lambda: (kimodo.terminate(), kimodo.join(3)))
        sock = worker.private_folder(self.root, KIMODO_MODEL) / 'worker.sock'; deadline = time.monotonic() + 4
        while not sock.exists():
            self.assertLess(time.monotonic(), deadline, 'mock Kimodo worker startup timed out'); time.sleep(.02)
        source = self.work / 'source.npz'; source.write_bytes(b'npz')
        spec = {'model': KIMODO_MODEL, 'text': 'walk', 'seed': 1, 'inference_seed': 1, 'duration_s': 2.5,
                'cfg_scale': 2., 'constraint_cfg_scale': 2., 'diffusion_steps': 100, 'post_processing': True,
                'adapter_version': KIMODO_ADAPTER, 'representation': 'human_smplh_y_up_v1'}
        def write(**extra):
            edit = dict({'source': str(source), 'source_sha256': '0' * 64, 'start_frame': 15, 'end_frame': 60}, **extra)
            self.batch.write_text(json.dumps([{'id': 'e', 'features': str(self.work / 'e.pt'),
                                               'motion': str(self.work / 'e.npz'), 'spec': spec, 'edit': edit}]))
        for bad in ({'follow_root': False}, {'follow_root': 'yes'}, {'extra': True}, {'staged_bridge': True}, {'follow_root': True, 'staged_bridge': False}):
            write(**bad)
            with self.assertRaisesRegex(RuntimeError, 'Invalid edit request'): self.invoke()
        write(follow_root=True); self.assertEqual(self.invoke()['calls'], 1)
        write(); self.assertEqual(self.invoke()['calls'], 2)
        write(follow_root=True, staged_bridge=True); self.assertEqual(self.invoke()['calls'], 3)

    def test_failure_replies_and_exits_cleanly(self):
        self.write_batch('fail')
        with self.assertRaisesRegex(RuntimeError, 'mock CUDA failure'):
            self.invoke()
        self.process.join(2)
        self.assertFalse(self.process.is_alive())

    def test_request_deadline_kills_stuck_runner(self):
        self.write_batch('wait')
        with self.assertRaises((worker.WorkerDisconnected, ConnectionResetError)):
            self.invoke(timeout=1)
        self.process.join(2)
        self.assertEqual(self.process.exitcode, 124)

    def test_cancel_kills_stuck_runner(self):
        self.write_batch('wait')
        started = time.monotonic()
        with self.assertRaises(InterruptedError):
            self.invoke(cancelled=lambda: time.monotonic() - started > .4)
        self.process.join(2)
        self.assertEqual(self.process.exitcode, 125)

    def test_idle_exit_releases_memory_owner(self):
        self.invoke()
        self.process.join(4)
        self.assertFalse(self.process.is_alive())

    def test_explicit_shutdown_is_fenced_and_releases_worker(self):
        self.invoke()
        self.assertTrue(worker.shutdown_worker(self.root, self.lock.fileno()))
        self.process.join(2)
        self.assertEqual(self.process.exitcode, 0)

class MetadataTests(unittest.TestCase):
    def test_one_transport_retry_but_no_model_error_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            batch = root / 'batch.json'
            with (root / 'gpu_worker.lock').open('a') as lock:
                with mock.patch.object(worker, '_request_once', side_effect=[worker.WorkerDisconnected('lost'), {'ok': 1}]) as call:
                    self.assertEqual(worker.request_batch(root, batch, lock.fileno()), {'ok': 1})
                    self.assertEqual(call.call_count, 2)
                with mock.patch.object(worker, '_request_once', side_effect=RuntimeError('bad model')) as call:
                    with self.assertRaisesRegex(RuntimeError, 'bad model'):
                        worker.request_batch(root, batch, lock.fileno())
                    self.assertEqual(call.call_count, 1)

    def test_cache_changes_with_weights_and_policy(self):
        with tempfile.TemporaryDirectory(prefix='mf-meta-', dir='/tmp') as folder:
            root = Path(folder)
            for model in ('Qwen3-8B', 'clip-vit-large-patch14'):
                target = root / 'ckpts' / model
                target.mkdir(parents=True)
                (target / 'model.safetensors').write_bytes(b'weights')
            target = root / 'ckpts/tencent/HY-Motion-1.0'
            target.mkdir(parents=True)
            (target / 'config.yml').write_text('encoder: mock')
            target = root / 'third_party/HY-Motion-1.0/hymotion/network/text_encoders'
            target.mkdir(parents=True)
            for name in ('text_encoder.py', 'model_constants.py'):
                (target / name).write_text('mock source')
            original = metadata.cache_profile(root)
            key = metadata.feature_key('walk', original)
            self.assertNotEqual(key, metadata.feature_key('walk', metadata.cache_profile(root, device_policy='offload')))
            (root / 'ckpts/Qwen3-8B/model.safetensors').write_bytes(b'new weights')
            self.assertNotEqual(key, metadata.feature_key('walk', metadata.cache_profile(root)))
            self.assertEqual(original['weights']['kind'], 'file-stat-v1')

    def test_partial_inference_writes_are_accounted_on_failure(self):
        with tempfile.TemporaryDirectory(prefix='mf-space-', dir='/tmp') as folder:
            root = Path(folder)
            (root / 'prompts.sqlite3').touch()
            job = {'features': str(root / 'features.pt'), 'motion': str(root / 'result.npz')}
            Path(job['features']).write_bytes(b'old')
            budget = mock.MagicMock()
            budget.__enter__.return_value = budget
            reservation = mock.Mock(return_value=budget)
            with mock.patch.dict('sys.modules', {'g2h.capacity': SimpleNamespace(reservation=reservation)}):
                with self.assertRaisesRegex(RuntimeError, 'write interrupted'):
                    with inference_capacity.temporary_space(root, [job, job]):
                        Path(job['features']).write_bytes(b'new features')
                        Path(job['motion']).with_suffix('.tmp').write_bytes(b'partial')
                        raise RuntimeError('write interrupted')
            self.assertEqual(reservation.call_args.args[1], 80 * 1024**2)
            budget.commit.assert_called_once_with(len(b'new features') + len(b'partial') - len(b'old'))

    def test_error_sanitization_strips_prefixes_and_hides_internal_exceptions(self):
        self.assertEqual(worker._sanitize_error(ValueError('ValueError: bad frames'), 'fallback'), 'bad frames')
        self.assertEqual(worker._sanitize_error(RuntimeError('RuntimeError: CUDA OOM'), 'fallback'), 'CUDA OOM')
        self.assertEqual(worker._sanitize_error(KeyError('internal_col'), '模型推理执行失败'), '模型推理执行失败')


if __name__ == '__main__':
    unittest.main()
