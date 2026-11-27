"""Exercise the Kinora updater with a published or locally served signed candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pyatspi
from gi.repository import GLib

OLD = '0.9.0'
NEW = '0.9.1'
OUTPUT = Path('update-smoke').resolve()
RELEASES = 'https://github.com/PoirotJin/kinora/releases'
report = {'baseline': OLD, 'target': NEW, 'steps': [], 'requests': []}
SESSION_COOKIE = 'mf_session=synthetic-update-test'


def release_url(version, name):
    return f'{RELEASES}/download/kinora-v{version}/{name}'


def appimage_name(version):
    return f'kinora-{version}-linux-x86_64.AppImage'


def appimage_digest(version):
    sums = run('curl', '-fsSL', '--retry', '3', '--max-time', '30', release_url(version, 'SHA256SUMS.txt'))
    entries = [line.split() for line in sums.splitlines() if line.strip()]
    digest, = [digest for digest, name in entries if name.lstrip('*') == appimage_name(version)]
    return digest


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(*args):
    return subprocess.check_output(args, text=True, timeout=150).strip()


def fixture_proxy(candidate, signature, target):
    """Serve the real GitHub URLs over a loopback proxy trusted only by the test app."""
    ca_key, ca = OUTPUT/'fixture-ca.key', OUTPUT/'fixture-ca.pem'
    key, csr, cert = (OUTPUT/('fixture-'+name) for name in ('tls.key','tls.csr','tls.pem'))
    ext = OUTPUT/'fixture-tls.cnf'
    ext.write_text('subjectAltName=DNS:github.com\nextendedKeyUsage=serverAuth\n')
    with (OUTPUT/'fixture-tls.log').open('wb') as log:
        for args in (
            ('req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=Kinora update fixture',
             '-keyout',ca_key,'-out',ca),
            ('req','-newkey','rsa:2048','-nodes','-subj','/CN=github.com','-keyout',key,'-out',csr),
            ('x509','-req','-in',csr,'-CA',ca,'-CAkey',ca_key,'-CAcreateserial','-days','1','-extfile',ext,'-out',cert),
        ):
            subprocess.run(['openssl',*args], check=True, stdout=log, stderr=log, timeout=30)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    ca_key.unlink()
    key.unlink()
    fixture = {'corrupt': True, 'served': []}
    asset_url = release_url(NEW, candidate.name)
    asset_path = urlsplit(asset_url).path

    class UpdateFeed(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/PoirotJin/kinora/releases/latest/download/latest.json':
                platform = {'url': asset_url, 'signature': signature}
                payload = json.dumps({'version': NEW, 'platforms': {target: platform, 'linux-x86_64': platform}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == asset_path:
                corrupt = fixture['corrupt']
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(candidate.stat().st_size))
                self.end_headers()
                with candidate.open('rb') as stream:
                    if corrupt:
                        self.wfile.write(bytes([stream.read(1)[0] ^ 1]))
                    shutil.copyfileobj(stream, self.wfile)
                fixture['served'].append(corrupt)
            else:
                self.send_error(404)

        def log_message(self, *_):
            pass

    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            if self.path != 'github.com:443':
                return self.send_error(403)
            self.send_response(200)
            self.end_headers()
            with tls.wrap_socket(self.connection, server_side=True) as tunnel:
                UpdateFeed(tunnel, self.client_address, self.server)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}'
    return server, fixture, {'HTTPS_PROXY': address, 'https_proxy': address,
                             'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                             'SSL_CERT_FILE': str(ca)}


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
    issued_cookie = False

    def do_GET(self):
        has_cookie = self.headers.get('Cookie') == SESSION_COOKIE
        report['requests'].append({'path': self.path, 'agent': self.headers.get('User-Agent', ''),
                                   'session_cookie_present': has_cookie})
        payload = b'{"error":"login required"}'
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        if self.path == '/api/auth/me' and not Probe.issued_cookie:
            self.send_header('Set-Cookie', SESSION_COOKIE+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600')
            Probe.issued_cookie = True
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def contacted(version, *, require_cookie=False):
    return any(request['path'].startswith('/api/auth/me') and
               f'MotionFarm-Desktop/{version}' in request['agent'] and
               (not require_cookie or request['session_cookie_present'])
               for request in report['requests'])


def installed_version(package):
    return run('dpkg-query', '-W', '-f=${Version}', package)


def download_baseline(name):
    path = OUTPUT.parent / name
    run('curl', '-fsSL', '--retry', '3', '--max-time', '120', '-o', str(path), release_url(OLD, path.name))
    return path


def main(origin, package_type, candidate):
    appimage = package_type == 'appimage'
    env = os.environ.copy()
    if appimage:
        image = download_baseline(appimage_name(OLD))
        old_digest = appimage_digest(OLD)
        assert file_sha256(image) == old_digest
        new_digest = file_sha256(candidate) if candidate else appimage_digest(NEW)
        image.chmod(0o755)
        executable = str(image)
        if not os.access('/dev/fuse', os.R_OK | os.W_OK):
            env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
        report['appimage_runtime'] = 'extract-and-run' if env.get('APPIMAGE_EXTRACT_AND_RUN') == '1' else 'fuse'
    else:
        baseline = download_baseline(f'kinora_{OLD}_amd64.deb')
        run('sudo', 'apt-get', 'install', '-y', str(baseline))
        package = run('dpkg-deb', '-f', str(baseline), 'Package')
        assert installed_version(package) == OLD
        executable, = [path for path in run('dpkg', '-L', package).splitlines() if path.startswith('/usr/bin/')]
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'com.motionfarm.desktop'
    config = data / 'server.json'
    assert not config.exists()
    # Begin as an existing user. WebKit versions expose the onboarding input
    # under different accessibility roles, while the updater uses buttons.
    data.mkdir(parents=True, exist_ok=True)
    before = json.dumps({'origin': origin}).encode()
    config.write_bytes(before)
    proxy = None
    if candidate:
        signature = candidate.with_name(candidate.name+'.sig').read_text().strip()
        proxy, fixture, proxy_env = fixture_proxy(candidate, signature, 'linux-x86_64-'+package_type)
        env.update(proxy_env)
    with (OUTPUT / 'kinora.log').open('wb') as log:
        def launch():
            return subprocess.Popen([executable], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)

        app = launch()
        wait_for(lambda: contacted(OLD), f'{OLD} connection to the configured server')
        assert config.read_bytes() == before
        record('Baseline started with an existing server address', installed_version=OLD)
        wait_for(lambda: element('设置与更新', 'push button') or element('知道了', 'push button'), 'login or release notes')
        dismiss_notes()
        click('设置与更新')
        if proxy:
            click('检查更新')
            click('下载更新')
            def rejected():
                check = element('检查更新', 'push button')
                return (fixture['served'] == [True] and check is not None
                        and check.getState().contains(pyatspi.STATE_SENSITIVE)
                        and element('重启安装', 'push button') is None)

            wait_for(rejected, 'tampered package rejected and updater unlocked', timeout=60)
            assert fixture['served'] == [True], fixture['served']
            assert app.poll() is None and config.read_bytes() == before
            if appimage:
                assert file_sha256(image) == old_digest
            else:
                assert installed_version(package) == OLD
            assert not element('重启安装', 'push button')
            save_image('00-signature-rejected')
            os.killpg(app.pid, signal.SIGTERM)
            app.wait(timeout=15)
            report['requests'].clear()
            fixture['corrupt'] = False
            app = launch()
            wait_for(lambda: contacted(OLD, require_cookie=True), 'original version reconnects with its session after signature rejection')
            assert config.read_bytes() == before
            dismiss_notes()
            click('设置与更新')
            record('Bad signature rejected; existing app relaunched with the original settings', installed_version=OLD)
        click('检查更新')
        button('下载更新')
        save_image('01-update-found')
        record('App found a newer version in the signed update feed')
        click('下载更新')
        wait_for(lambda: element('重启安装', 'push button'), 'verified download and restart button', timeout=200)
        save_image('02-update-verified')
        record('App downloaded and verified the signed package')
        click('重启安装')
        wait_for(lambda: contacted(NEW, require_cookie=True), f'restarted {NEW} reconnects with the original session', timeout=100)
        if proxy:
            assert fixture['served'] == [True, False], fixture['served']
        if appimage:
            assert os.access(image, os.X_OK)
            assert file_sha256(image) == new_digest
            record('The original AppImage path contains the exact signed binary',
                   sha256=new_digest, executable=True)
        else:
            assert installed_version(package) == NEW
        assert config.read_bytes() == before
        assert not element('保存并连接', 'push button')
        save_image('03-restarted')
        record('App restarted on the new version and reconnected without onboarding',
               installed_version=NEW, original_config_unchanged=True, original_session_reused=True)
    if proxy:
        proxy.shutdown()
        proxy.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', choices=('deb', 'appimage'), default='deb')
    parser.add_argument('--old', default=OLD)
    parser.add_argument('--new', default=NEW)
    parser.add_argument('--candidate-dir', type=Path, help='Local signed desktop build artifact')
    args = parser.parse_args()
    OLD, NEW = args.old, args.new
    if not all(re.fullmatch(r'\d+\.\d+\.\d+', version) for version in (OLD, NEW)):
        parser.error('Use numeric versions such as 0.9.2')
    if tuple(map(int, NEW.split('.'))) <= tuple(map(int, OLD.split('.'))):
        parser.error('--new must be newer than --old')
    report.update(baseline=OLD, target=NEW, package=args.package)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    candidate = None
    if args.candidate_dir:
        pattern = '*.AppImage' if args.package == 'appimage' else '*.deb'
        candidate, = (path for path in args.candidate_dir.rglob(pattern) if path.is_file())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        main(f'http://127.0.0.1:{server.server_port}', args.package, candidate)
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
