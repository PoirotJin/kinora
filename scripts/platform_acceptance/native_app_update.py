"""Verify the published desktop binary against a private, signed candidate.

Run only on disposable Windows and macOS runners. Test endpoints, certificates,
session material and application output stay on the runner.
"""
import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener, urlopen

OLD, NEW = '0.10.0', '0.10.1'
WINDOWS = sys.platform == 'win32'
RELEASE = 'https://github.com/PoirotJin/kinora/releases'
FEED = '/PoirotJin/kinora/releases/latest/download/latest.json'
LOCAL = build_opener(ProxyHandler({}))
SUMMARY = []


class TestFailure(Exception):
    pass


class Server(ThreadingHTTPServer):
    def handle_error(self, _request, _client_address):
        # Avoid printing local addresses, request content or tracebacks.
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def payload(self, status, value, cookie=None):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            self.send_header('Set-Cookie', cookie + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600')
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def serve(handler):
    with Server(('localhost', 0), handler) as server:
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield 'http://localhost:' + str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


def command(*args, timeout=120):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def receipt(message):
    SUMMARY.append(message)
    print('PASS ' + message, flush=True)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def release_file(version, name):
    return f'{RELEASE}/download/kinora-v{version}/{name}'


def published_asset(work, name):
    with urlopen(release_file(OLD, 'SHA256SUMS.txt'),timeout=30) as response:
        checksums = dict((file.lstrip('*'),digest) for digest,file in
                         (line.split() for line in response.read().decode().splitlines() if line.strip()))
    path = work/name
    with urlopen(release_file(OLD,name),timeout=45) as response, path.open('wb') as target:
        shutil.copyfileobj(response,target)
    if checksums[name] != sha256(path):
        raise TestFailure('Published baseline checksum did not match')
    return path


def install_baseline(work):
    destination = work/'installed'
    if WINDOWS:
        installer = published_asset(work, f'kinora_{OLD}_x64-setup.exe')
        command(str(installer),'/S','/D='+str(destination))
        executable, = (p for p in destination.glob('*.exe') if 'uninstall' not in p.name.lower())
        data = Path(os.environ['LOCALAPPDATA'])/'com.motionfarm.desktop'
    else:
        installer = published_asset(work, f'kinora_{OLD}_universal.dmg')
        mount = work/'mount'
        mount.mkdir()
        command('hdiutil','attach','-nobrowse','-readonly','-mountpoint',str(mount),str(installer))
        try:
            original, = mount.glob('*.app')
            destination.mkdir()
            bundle = destination/original.name
            command('ditto',str(original),str(bundle))
        finally:
            command('hdiutil','detach',str(mount))
        with (bundle/'Contents/Info.plist').open('rb') as plist:
            executable = bundle/'Contents/MacOS'/plistlib.load(plist)['CFBundleExecutable']
        data = Path.home()/'Library/Application Support/com.motionfarm.desktop'
    return executable,data


def installed_version(executable):
    if WINDOWS:
        value = command('powershell','-NoProfile','-Command',
                        f"(Get-Item -LiteralPath '{str(executable).replace(chr(39), chr(39)*2)}').VersionInfo.ProductVersion")
        if value.endswith('.0'):
            return value.removesuffix('.0') if value.count('.') == 3 else value
        return value
    with (executable.parent.parent/'Info.plist').open('rb') as plist:
        return plistlib.load(plist)['CFBundleShortVersionString']


def fixture_tls(work, stack):
    ca_key, ca = work/'root.key',work/'root.crt'
    key, request, cert = work/'server.key',work/'server.csr',work/'server.crt'
    extensions = work/'server.cnf'
    extensions.write_text('subjectAltName=DNS:github.com\nextendedKeyUsage=serverAuth\n')
    command('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=Kinora disposable upgrade test','-keyout',str(ca_key),'-out',str(ca))
    command('openssl','req','-newkey','rsa:2048','-nodes','-subj','/CN=github.com',
            '-keyout',str(key),'-out',str(request))
    command('openssl','x509','-req','-in',str(request),'-CA',str(ca),'-CAkey',str(ca_key),
            '-CAcreateserial','-days','1','-extfile',str(extensions),'-out',str(cert))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert,key)
    fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(ca.read_text())).hexdigest().upper()
    if WINDOWS:
        command('certutil','-f','-user','-addstore','Root',str(ca))
        stack.callback(command,'certutil','-user','-delstore','Root',fingerprint)
    else:
        chain = '/Library/Keychains/System.keychain'
        command('sudo','-n','security','add-trusted-cert','-d','-r','trustRoot','-p','ssl','-k',chain,str(ca))
        stack.callback(command,'sudo','-n','security','delete-certificate','-Z',fingerprint,chain)
    ca_key.unlink()
    key.unlink()
    return tls


def update_proxy(tls, candidate, transfers):
    signature = candidate.with_name(candidate.name+'.sig').read_text().strip()
    url = release_file(NEW,candidate.name)
    targets = ('windows-x86_64','windows-x86_64-nsis') if WINDOWS else (
        'darwin-aarch64','darwin-x86_64','darwin-aarch64-app','darwin-x86_64-app')

    class Feed(Handler):
        def do_GET(self):
            if self.path == FEED:
                self.payload(200, {'version':NEW,'platforms':{target:{'url':url,'signature':signature}
                                                             for target in targets}})
            elif self.path == urlsplit(url).path:
                tampered = not transfers
                self.send_response(200)
                self.send_header('Content-Length',str(candidate.stat().st_size))
                self.end_headers()
                with candidate.open('rb') as body:
                    if tampered:
                        self.wfile.write(bytes([body.read(1)[0]^1]))
                    shutil.copyfileobj(body,self.wfile)
                transfers.append(tampered)
            else:
                self.send_error(404)

    class Proxy(Handler):
        def do_CONNECT(self):
            if self.path != 'github.com:443':
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            with tls.wrap_socket(self.connection,server_side=True) as tunnel:
                Feed(tunnel,self.client_address,self.server)

    return Proxy


class Desktop:
    selectors = {'设置与更新':'gate-settings','知道了':'release-dismiss','检查更新':'upd-check',
                 '下载更新':'upd-download','重启安装':'upd-install'}

    def __init__(self, executable, proxy):
        self.executable = executable
        self.env = dict(os.environ,HTTPS_PROXY=proxy,NO_PROXY='localhost')
        self.env.update(https_proxy=proxy,no_proxy='localhost')
        if WINDOWS:
            with socket.socket() as sock:
                sock.bind(('localhost',0))
                self.port = sock.getsockname()[1]
            self.env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = (
                f'--remote-debugging-port={self.port} --remote-debugging-address=localhost')
        self.process = None

    def start(self):
        self.process = subprocess.Popen([str(self.executable)],env=self.env,stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=20)

    def windows_control(self, label, activate):
        import websocket
        try:
            with LOCAL.open(f'http://localhost:{self.port}/json/list',timeout=2) as response:
                pages = json.load(response)
            # Only inspect the app's bundled renderer.
            page = next(p for p in pages if p['type']=='page' and urlsplit(p['url']).hostname=='tauri.localhost')
            with closing(websocket.create_connection(page['webSocketDebuggerUrl'],suppress_origin=True,timeout=3)) as session:
                expression = """
                (() => {
                  const element = document.getElementById(SELECTOR);
                  if (!element?.getClientRects().length || element.disabled) return false;
                  if (ACTIVATE) element.click();
                  return true;
                })()
                """.replace('SELECTOR',json.dumps(self.selectors[label])).replace('ACTIVATE',json.dumps(activate))
                session.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':expression,
                                                                                      'returnByValue':True}}))
                return json.loads(session.recv()).get('result',{}).get('result',{}).get('value',False)
        except (OSError, StopIteration, KeyError, ValueError, websocket.WebSocketException):
            return False

    def control(self, label, activate=False):
        if WINDOWS:
            return self.windows_control(label,activate)
        script = Path(__file__).with_name('macos_update_control.applescript')
        try:
            result = command('osascript',str(script),str(self.process.pid),label,
                             'click' if activate else 'inspect',timeout=12)
        except subprocess.CalledProcessError as error:
            if any(term in error.stderr for term in ('-1743','assistive access','-25211')):
                raise TestFailure('macOS accessibility permission unavailable') from None
            return False
        except subprocess.TimeoutExpired:
            return False
        if result == 'permission':
            raise TestFailure('macOS accessibility permission unavailable')
        return result == 'ready'

    def settings(self):
        wait(lambda:self.control('设置与更新') or self.control('知道了'),'Baseline update settings')
        if self.control('知道了'):
            self.click('知道了')
        self.click('设置与更新')

    def click(self, label):
        wait(lambda:self.control(label,activate=True),'Updater control unavailable')


def wait(predicate, message, timeout=60):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():
            return
        time.sleep(.5)
    raise TestFailure(message)


def test(candidate_dir, work, stack):
    suffix = '*-setup.exe' if WINDOWS else '*.app.tar.gz'
    candidate, = candidate_dir.rglob(suffix)
    executable,data = install_baseline(work)
    baseline_hash = sha256(executable)
    if installed_version(executable) != OLD:
        raise TestFailure('Installed baseline version mismatch')

    observations = set()
    cookie = 'mf_session='+secrets.token_hex(20)
    issued = False

    class Probe(Handler):
        def do_GET(self):
            nonlocal issued
            if self.path != '/api/auth/me':
                self.payload(401,{})
                return
            agent = self.headers.get('User-Agent','')
            has_cookie = self.headers.get('Cookie') == cookie
            for version in (OLD,NEW):
                if agent == f'MotionFarm-Desktop/{version}':
                    observations.add((version,has_cookie))
            self.payload(401,{'error':'Fixture session'},None if issued else cookie)
            issued = True

    origin = stack.enter_context(serve(Probe))
    data.mkdir(parents=True,exist_ok=True)
    config = data/'server.json'
    before = json.dumps({'origin':origin}).encode()
    with config.open('xb') as stream:
        stream.write(before)

    transfers = []
    tls = fixture_tls(work,stack)
    proxy = stack.enter_context(serve(update_proxy(tls,candidate,transfers)))
    app = Desktop(executable,proxy)
    stack.callback(app.stop)
    app.start()
    wait(lambda:(OLD,False) in observations,'Published baseline did not contact its saved server')
    receipt('Published 0.10.0 launched with saved settings')

    app.settings()
    app.click('检查更新')
    app.click('下载更新')
    wait(lambda:transfers == [True] and app.control('检查更新') and not app.control('重启安装'),
         'Tampered download did not return to a non-installable state',timeout=90)
    if app.process.poll() is not None or sha256(executable) != baseline_hash or config.read_bytes() != before:
        raise TestFailure('Tampered download changed the baseline or its settings')
    receipt('Tampered package rejected and 0.10.0 left intact')

    app.stop()
    observations.clear()
    app.start()
    wait(lambda:(OLD,True) in observations,'Relaunched 0.10.0 did not restore its session')
    if config.read_bytes() != before:
        raise TestFailure('Server preference changed after baseline relaunch')
    receipt('0.10.0 reopened with the original server and session')

    app.settings()
    app.click('检查更新')
    app.click('下载更新')
    wait(lambda:transfers == [True,False] and app.control('重启安装'),
         'Genuine candidate did not pass signature verification',timeout=120)
    receipt('Genuine candidate passed in-app signature verification')
    app.click('重启安装')
    wait(lambda:(NEW,True) in observations,'0.10.1 did not restart with the original session',timeout=150)
    if config.read_bytes() != before or installed_version(executable) != NEW or sha256(executable) == baseline_hash:
        raise TestFailure('Installed version or saved configuration mismatch after genuine update')
    receipt('0.10.1 installed and restarted with the original server and session')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate_dir',type=Path)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform not in {'win32','darwin'}:
        raise SystemExit('This smoke test requires a disposable Windows or macOS GitHub Actions runner')
    try:
        with tempfile.TemporaryDirectory(prefix='kinora-native-update-') as temp, ExitStack() as stack:
            test(args.candidate_dir,Path(temp),stack)
    except TestFailure as error:
        print('FAIL '+str(error),file=sys.stderr)
        return 1
    except Exception as error:
        # Subprocess errors and network exceptions may embed local paths or addresses.
        print('FAIL Test setup or verification raised '+type(error).__name__,file=sys.stderr)
        return 1
    finally:
        if path := os.environ.get('GITHUB_STEP_SUMMARY'):
            with Path(path).open('a',encoding='utf-8') as output:
                output.write('\n'.join('- '+step for step in SUMMARY)+'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
