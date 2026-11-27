"""Private, single-GPU inference service. Imports no ML modules on the client path.

The scheduler's flock is passed using SCM_RIGHTS and held for one request only.
The worker never LOCK_UNs that shared open-file description. Warm models remain
available to the next scheduler quantum; idle exit returns their VRAM.
"""
import argparse
import array
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g2h.generation_backend import HY_MODEL, KIMODO_MODEL, inference_python, validate_spec

PROTOCOL = 1
MAX_PACKET = 16384


_ERR_PREFIX = re.compile(r'^(?:Traceback.*\|)?(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):\s*)+')


def _sanitize_error(exc, fallback):
    if not isinstance(exc, (ValueError, PermissionError, RuntimeError)):
        return fallback
    cleaned = _ERR_PREFIX.sub('', str(exc).strip()).strip()
    return (cleaned[:240] if cleaned else fallback)


class WorkerDisconnected(RuntimeError):
    pass


def private_folder(root, model=HY_MODEL):
    root = Path(root).resolve()
    if model not in (HY_MODEL, KIMODO_MODEL):
        raise ValueError('Unsupported worker backend')
    folder = root / ('review/inference-kimodo' if model == KIMODO_MODEL else 'review/inference')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('Inference socket directory is not owned by this account')
    folder.chmod(0o700)
    if len(os.fsencode(folder / 'worker.sock')) >= 104:
        raise ValueError('Inference socket path is too long')
    return folder


def peer_uid(connection):
    return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def connect_worker(folder):
    path = folder / 'worker.sock'
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('Unsafe inference socket')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    connection.settimeout(2)
    try:
        connection.connect(str(path))
        if peer_uid(connection) != os.getuid():
            raise PermissionError('Unexpected inference worker account')
        return connection
    except BaseException:
        connection.close()
        raise


def ensure_worker(root, deadline=None, model=HY_MODEL):
    folder = private_folder(root, model)
    try:
        return connect_worker(folder)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    with (folder / 'start.lock').open('a') as gate:
        deadline = min(deadline or float('inf'), time.monotonic() + 10)
        while True:
            try:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Inference worker startup lock timed out')
                time.sleep(.1)
        try:
            return connect_worker(folder)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        env = dict(os.environ)
        env.pop('MOTIONFARM_GPU_FD', None)
        log_path = folder / 'worker.log'
        if log_path.exists() and log_path.stat().st_size > 8 * 1024**2:
            log_path.replace(folder / 'worker.previous.log')
        with log_path.open('ab') as log:
            python = inference_python(Path(__file__).resolve().parents[1], model) or sys.executable
            child = subprocess.Popen([python, '-u', str(Path(__file__).resolve()),
                                      '--farm-root', str(root), '--backend-model', model], env=env, stdout=log, stderr=log,
                                     close_fds=True, start_new_session=True)
        threading.Thread(target=child.wait, daemon=True).start()
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('Inference worker exited during startup; inspect worker.log')
            try:
                return connect_worker(folder)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.1)
        raise TimeoutError('Inference worker startup timed out')


def _request_once(root, batch, gpu_fd, timeout_s, cancelled):
    deadline = time.monotonic() + min(1800, max(1, int(timeout_s)))
    jobs = json.loads(Path(batch).read_text())
    models = {validate_spec(j['spec']) for j in jobs}
    if len(models) != 1:
        raise ValueError('Do not mix generation backends in one worker request')
    with ensure_worker(root, deadline, next(iter(models))) as connection:
        if cancelled():
            raise InterruptedError('Inference cancelled before dispatch')
        remaining = math.ceil(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError('Inference worker startup exhausted the request deadline')
        request = {'protocol': PROTOCOL, 'batch': str(Path(batch).resolve()),
                   'timeout_s': min(1800, remaining)}
        fds = array.array('i', [gpu_fd])
        connection.sendmsg([json.dumps(request).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
        while time.monotonic() < deadline + 3:
            if cancelled():
                # Notify the watchdog, then briefly wait for process death before
                # settling temporary-file usage. The FD also prevents overlap.
                connection.shutdown(socket.SHUT_WR)
                connection.settimeout(3)
                try:
                    connection.recv(MAX_PACKET)
                except OSError:
                    pass
                raise InterruptedError('Inference cancelled')
            ready, _, _ = select.select([connection], [], [], .2)
            if ready:
                data = connection.recv(MAX_PACKET)
                if not data:
                    raise WorkerDisconnected('Inference worker disconnected; completed atomic outputs remain recoverable')
                result = json.loads(data)
                if not result.get('ok'):
                    raise RuntimeError(result.get('error', 'Inference worker failed'))
                return result['result']
        raise TimeoutError('Inference worker request timed out')


def request_batch(root, batch, gpu_fd, timeout_s=1800, cancelled=lambda: False, *, retry_unfinished=True):
    """Retry one lost worker within the original deadline; never retry model errors."""
    deadline = time.monotonic() + min(1800, max(1, int(timeout_s)))
    for attempt in range(2):
        remaining = math.ceil(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError('Inference worker request timed out')
        try:
            return _request_once(root, batch, gpu_fd, remaining, cancelled)
        except (WorkerDisconnected, ConnectionResetError, BrokenPipeError):
            if attempt or not retry_unfinished or cancelled():
                raise
            time.sleep(.2)
            print('Inference worker was lost; retrying unfinished atomic outputs once', flush=True)


def shutdown_worker(root, gpu_fd, model=HY_MODEL):
    """Administrative idle shutdown, fenced by the same farm GPU lock."""
    try:
        connection = connect_worker(private_folder(root, model))
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    with connection:
        packet = json.dumps({'protocol': PROTOCOL, 'operation': 'shutdown'}).encode()
        connection.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [gpu_fd]))])
        result = json.loads(connection.recv(MAX_PACKET))
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'Worker shutdown failed'))
        return True


def receive_request(connection, root, backend_model=None):
    if peer_uid(connection) != os.getuid():
        raise PermissionError('Inference peer account mismatch')
    connection.settimeout(3)
    raw, ancillary, flags, _ = connection.recvmsg(MAX_PACKET, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
    descriptors = []
    try:
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array('i')
                values.frombytes(data[:len(data) - len(data) % values.itemsize])
                descriptors.extend(values)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(descriptors) != 1:
            raise ValueError('Inference request requires exactly one GPU lock FD')
        request = json.loads(raw)
        if request.get('protocol') != PROTOCOL:
            raise ValueError('Unsupported inference protocol')
        fd = descriptors[0]
        supplied = os.fstat(fd)
        expected = (root / 'gpu_worker.lock').stat()
        if not stat.S_ISREG(supplied.st_mode) or (supplied.st_dev, supplied.st_ino) != (expected.st_dev, expected.st_ino):
            raise PermissionError('GPU lock descriptor mismatch')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if request.get('operation') == 'shutdown':
            return None, fd, 1
        batch = Path(request['batch']).resolve(strict=True)
        relative = batch.relative_to(root / 'topics')
        if len(relative.parts) != 3 or relative.parts[1:] != ('work', 'batch.json'):
            raise PermissionError('Batch must belong to a topic work directory')
        if batch.stat().st_size > 256 * 1024:
            raise ValueError('Inference batch is too large')
        jobs = json.loads(batch.read_text())
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= 16:
            raise ValueError('Inference batch must have 1..16 jobs')
        for job in jobs:
            for field, suffix in (('features', '.pt'), ('motion', '.npz')):
                path = Path(job[field]).resolve()
                if path.parent != batch.parent or path.suffix != suffix:
                    raise PermissionError('Inference output escapes topic work directory')
            spec = job['spec']
            validate_spec(spec)
            if 'edit' in job:
                import re
                edit = job['edit']
                from g2h.kimodo_edit import valid_request
                if (spec['model'] != KIMODO_MODEL or not valid_request(edit)):
                    raise ValueError('Invalid edit request')
                source = Path(edit['source']).resolve(strict=True)
                if (source.parent != batch.parent or source.suffix != '.npz' or
                        source in (Path(job['motion']).resolve(), Path(job['features']).resolve()) or
                        source.stat().st_uid != os.getuid() or source.stat().st_size > 64*1024*1024):
                    raise PermissionError('Edit source escapes the private work directory')
                if not isinstance(edit['source_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', edit['source_sha256']):
                    raise ValueError('Invalid edit source hash')
                if any(type(edit[k]) is not int for k in ('start_frame', 'end_frame')) or not 0 <= edit['start_frame'] < edit['end_frame'] <= 18000:
                    raise ValueError('Invalid edit interval')
            if backend_model is not None and spec['model'] != backend_model:
                raise ValueError('Worker backend does not match queued specification')
        if len({job['spec']['model'] for job in jobs}) != 1:
            raise ValueError('A batch must use one generation backend')
        timeout_s = request['timeout_s']
        if type(timeout_s) is not int or not 1 <= timeout_s <= 1800:
            raise ValueError('Invalid inference timeout')
        return batch, fd, timeout_s
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def watch_request(connection, finished, timeout_s):
    """A wedged CUDA call must not outlive its GPU lock owner or its deadline."""
    deadline = time.monotonic() + timeout_s
    while not finished.wait(.2):
        if time.monotonic() >= deadline:
            print('Inference request deadline exceeded; worker exiting', flush=True)
            os._exit(124)
        readable, _, _ = select.select([connection], [], [], 0)
        if readable:
            # The protocol has a single request packet; EOF or more bytes means
            # cancellation/protocol failure. Exit releases CUDA and the lock FD.
            print('Inference client disconnected; worker exiting', flush=True)
            os._exit(125)


def serve(root, runner=None, idle_s=300, backend_model=HY_MODEL):
    root = Path(root).resolve()
    folder = private_folder(root, backend_model)
    with (folder / 'singleton.lock').open('a') as gate:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = folder / 'worker.sock'
        path.unlink(missing_ok=True)
        state = {}
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen(2)
            listener.settimeout(idle_s)
            print('Inference worker ready; idle exit after %d seconds' % idle_s, flush=True)
            try:
                while True:
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        print('Inference worker idle; releasing models', flush=True)
                        return
                    with connection:
                        try:
                            batch, gpu_fd, timeout_s = receive_request(connection, root, backend_model)
                        except Exception as exc:
                            try:
                                connection.send(json.dumps({'ok': False, 'error': _sanitize_error(exc, '推理请求校验失败，请检查日志后重试')}).encode())
                            except OSError:
                                pass
                            continue
                        if batch is None:
                            os.close(gpu_fd)
                            connection.send(json.dumps({'ok': True}).encode())
                            print('Inference worker explicitly stopped; releasing models', flush=True)
                            return
                        finished = threading.Event()
                        watcher = threading.Thread(target=watch_request, args=(connection, finished, timeout_s), daemon=True)
                        watcher.start()
                        failure = False
                        try:
                            if runner is None:
                                from collection_infer import run_batch
                                runner = run_batch
                            args = SimpleNamespace(stage='run', batch=batch, model=backend_model,
                                                   encoder_device='auto', gpu_batch_size=None, persistent=True)
                            result = runner(args, state)
                            response = {'ok': True, 'result': {**(result or {}), 'worker_pid': os.getpid()}}
                        except Exception as exc:
                            # CUDA failures may leave the context unusable. A clean
                            # next request starts a fresh worker; completed files stay.
                            import traceback
                            traceback.print_exc()
                            print('Inference failed:', type(exc).__name__, str(exc)[:500], flush=True)
                            response = {'ok': False, 'error': _sanitize_error(exc, '模型推理执行失败，请查看推理日志')}
                            failure = True
                        finally:
                            finished.set()
                            watcher.join(timeout=1)
                            if not failure:
                                os.close(gpu_fd)
                            # A failed CUDA context may still be tearing down.
                            # Keep its FD open until process exit, never unlock early.
                        try:
                            connection.send(json.dumps(response).encode())
                        except OSError:
                            pass
                        if failure:
                            return
            finally:
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--farm-root', type=Path, required=True)
    parser.add_argument('--backend-model', choices=[HY_MODEL, KIMODO_MODEL], default=HY_MODEL)
    args = parser.parse_args()
    idle_s = min(900, max(15, int(os.environ.get('MOTIONFARM_INFER_IDLE_SECONDS', '300'))))
    serve(args.farm_root, idle_s=idle_s, backend_model=args.backend_model)


if __name__ == '__main__':
    main()
