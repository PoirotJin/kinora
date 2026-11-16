"""Exercise the published Kinora updater through the Ubuntu accessibility tree."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pyatspi
from gi.repository import GLib

from feed import LATEST_PATH, RELEASES, candidate_feed

OUTPUT = Path('update-smoke').resolve()
report = {'steps': [], 'requests': []}


def release_url(version, name):
    return f'{RELEASES}/download/kinora-v{version}/{name}'


def package_name(version, package_type):
    if package_type == 'appimage':
        return f'kinora-{version}-linux-x86_64.AppImage'
    return f'kinora_{version}_amd64.deb'


def expected_digest(sums, name):
    lines = [line.split() for line in sums.splitlines() if line.strip()]
    digest, = [fields[0] for fields in lines if len(fields) == 2 and fields[1].lstrip('*') == name]
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Unexpected checksum format')
    return digest


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*args):
    return subprocess.check_output(args, text=True, timeout=150).strip()


def record(step, **details):
    report['steps'].append({'step': step, **details})
    print(step, flush=True)


def visible_nodes():
    stack = [pyatspi.Registry.getDesktop(0)]
    while stack:
        node = stack.pop()
        try:
            if node.getState().contains(pyatspi.STATE_SHOWING):
                yield node
            stack.extend(node.getChildAtIndex(i) for i in range(node.childCount) if node.getChildAtIndex(i))
        except (RuntimeError, GLib.Error):
            continue


def element(label, role=None, *, prefix=False):
    for node in visible_nodes():
        try:
            matches = node.name.startswith(label) if prefix else node.name == label
            if matches and (role is None or node.getRoleName() == role):
                return node
        except (RuntimeError, GLib.Error):
            continue
    return None


def wait_for(check, description, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (RuntimeError, GLib.Error):
            pass
        time.sleep(.3)
    raise AssertionError(f'Timed out waiting for {description}')


def save_image(name):
    subprocess.run(['scrot', '-o', str(OUTPUT / f'{name}.png')], check=False, timeout=10)


def button(label):
    node = wait_for(lambda: element(label, 'push button'), label)
    state = node.getState()
    assert state.contains(pyatspi.STATE_SENSITIVE), f'Disabled button: {label}'
    return node


def click(label):
    node = button(label)
    action = node.queryAction()
    assert action.nActions and action.doAction(0), f'Could not activate {label}'


def dismiss_notes():
    if element('知道了', 'push button'):
        click('知道了')


class Probe(BaseHTTPRequestHandler):
    def do_GET(self):
        report['requests'].append({'path': self.path, 'agent': self.headers.get('User-Agent', '')})
        payload = b'{"error":"login required"}'
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def contacted(version):
    return any(request['path'].startswith('/api/auth/me') and
               f'MotionFarm-Desktop/{version}' in request['agent']
               for request in report['requests'])


def installed_version(package):
    return run('dpkg-query', '-W', '-f=${Version}', package)


def download_baseline(version, name):
    path = OUTPUT.parent / name
    run('curl', '-fsSL', '--retry', '3', '--max-time', '120', '-o', str(path), release_url(version, name))
    sums = run('curl', '-fsSL', '--retry', '3', '--max-time', '30', release_url(version, 'SHA256SUMS.txt'))
    assert file_sha256(path) == expected_digest(sums, name), 'Stable installer checksum differs'
    return path


def main(origin, args):
    old, new = args.baseline, args.target
    appimage = args.package == 'appimage'
    env = os.environ.copy()
    baseline = download_baseline(old, package_name(old, args.package))
    candidate = args.candidate_dir / package_name(new, args.package)
    sums = (args.candidate_dir / 'SHA256SUMS.txt').read_text()
    new_digest = expected_digest(sums, candidate.name)
    assert file_sha256(candidate) == new_digest, 'Candidate installer checksum differs'
    if appimage:
        baseline.chmod(0o755)
        executable = str(baseline)
        assert os.access('/dev/fuse', os.R_OK | os.W_OK), 'FUSE is required for the AppImage release check'
        env.pop('APPIMAGE_EXTRACT_AND_RUN', None)
        report['appimage_runtime'] = 'fuse'
    else:
        run('sudo', 'apt-get', 'install', '-y', str(baseline))
        package = run('dpkg-deb', '-f', str(baseline), 'Package')
        assert installed_version(package) == old
        executable, = [path for path in run('dpkg', '-L', package).splitlines() if path.startswith('/usr/bin/')]
        with tempfile.TemporaryDirectory(prefix='kinora-deb-candidate-') as extracted:
            run('dpkg-deb', '-x', str(candidate), extracted)
            new_executable_digest = file_sha256(Path(extracted) / executable.lstrip('/'))
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'com.motionfarm.desktop'
    config = data / 'server.json'
    assert not config.exists()
    with candidate_feed(args.candidate_dir, new, args.package, candidate.name) as (feed_env, downloads, package_path), \
            (OUTPUT / 'kinora.log').open('wb') as log:
        env.update(feed_env)
        subprocess.Popen([executable], stdout=log, stderr=subprocess.STDOUT, env=env)
        button('保存并连接')
        if appimage:
            # The bundled WebKit exposes the label without a name on the entry.
            # Click the field shown above the submit button in the fixed test viewport.
            submit = button('保存并连接').queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
            run('xdotool', 'mousemove', str(submit.x + submit.width // 2), str(submit.y - 83), 'click', '1')
            run('xdotool', 'type', '--clearmodifiers', origin)
        else:
            field = wait_for(lambda: element('服务器地址', 'entry', prefix=True), 'server address input')
            try:
                assert field.queryEditableText().setTextContents(origin)
            except NotImplementedError:
                run('xdotool', 'type', '--clearmodifiers', origin)
        click('保存并连接')
        wait_for(lambda: contacted(old), 'previous stable version connection to the configured server')
        before = config.read_bytes()
        assert json.loads(before)['origin'] == origin
        record('Entered and saved the server address once', installed_version=old)
        wait_for(lambda: element('设置与更新', 'push button') or element('知道了', 'push button'), 'login or release notes')
        dismiss_notes()
        click('设置与更新')
        click('检查更新')
        button('下载更新')
        save_image('01-update-found')
        wait_for(lambda: LATEST_PATH in downloads, 'candidate manifest served', timeout=5)
        record('App found the candidate in the staged GitHub feed')
        click('下载更新')
        wait_for(lambda: element('重启安装', 'push button'), 'verified download and restart button', timeout=200)
        save_image('02-update-verified')
        wait_for(lambda: package_path in downloads, 'candidate package served', timeout=5)
        record('App downloaded and verified the signed candidate')
        click('重启安装')
        wait_for(lambda: contacted(new), 'restarted candidate reconnects to the original address', timeout=100)
        if appimage:
            assert os.access(baseline, os.X_OK)
            assert file_sha256(baseline) == new_digest
        else:
            assert installed_version(package) == new
            assert file_sha256(executable) == new_executable_digest
        report['candidate_sha256'] = new_digest
        assert config.read_bytes() == before
        assert not element('保存并连接', 'push button')
        save_image('03-restarted')
        record('App restarted on the new version and reconnected without onboarding',
               installed_version=new, original_config_unchanged=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', choices=('deb', 'appimage'), required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--candidate-dir', type=Path, required=True)
    args = parser.parse_args()
    versions = [tuple(map(int, version.split('.'))) for version in (args.baseline, args.target)
                if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version)]
    if len(versions) != 2 or not versions[0] < versions[1]:
        parser.error('Expected a stable baseline and a newer candidate version')
    report.update(baseline=args.baseline, target=args.target, package=args.package)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        main(f'http://127.0.0.1:{server.server_port}', args)
        report['passed'] = True
    except BaseException:
        report['passed'] = False
        save_image('failure')
        try:
            for node in visible_nodes():
                try:
                    print(f'{node.getRoleName()}: {node.name}', flush=True)
                except (RuntimeError, GLib.Error):
                    continue
        except (RuntimeError, GLib.Error):
            pass
        raise
    finally:
        (OUTPUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        server.shutdown()
