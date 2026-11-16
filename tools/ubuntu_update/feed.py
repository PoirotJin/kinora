"""Serve the verified candidate to the app at its normal GitHub HTTPS URLs."""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading

RELEASES = 'https://github.com/PoirotJin/kinora/releases'
LATEST_PATH = '/PoirotJin/kinora/releases/latest/download/latest.json'


class AssetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.server.assets.get(self.path)
        if path is None:
            self.send_error(404)
            return
        with path.open('rb') as stream:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if path.suffix == '.json' else 'application/octet-stream')
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            shutil.copyfileobj(stream, self.wfile)
        self.server.downloads.append(self.path)

    def log_message(self, *_):
        pass


class TunnelHandler(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        if self.path != 'github.com:443':
            self.send_error(403)
            return
        self.send_response_only(200)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        with self.server.tls.wrap_socket(self.connection, server_side=True) as connection:
            connection.settimeout(45)
            AssetHandler(connection, self.client_address, self.server)

    def log_message(self, *_):
        pass


def make_tls(directory):
    def openssl(*args):
        subprocess.run(['openssl', *args], cwd=directory, check=True, capture_output=True, timeout=20)

    openssl('req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=Kinora disposable update test CA',
            '-addext', 'basicConstraints=critical,CA:TRUE',
            '-keyout', 'ca.key', '-out', 'ca.pem')
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=github.com',
            '-keyout', 'server.key', '-out', 'server.csr')
    (directory / 'tls.ext').write_text('subjectAltName=DNS:github.com\nextendedKeyUsage=serverAuth\n')
    openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
            '-CAcreateserial', '-days', '1', '-extfile', 'tls.ext', '-out', 'server.pem')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / 'server.pem', directory / 'server.key')
    return context


@contextmanager
def candidate_feed(directory, version, package_type, package_name):
    manifest = directory / 'latest.json'
    document = json.loads(manifest.read_text())
    if document['version'] != version:
        raise ValueError('Candidate update manifest version does not match the release')
    url = f'{RELEASES}/download/kinora-v{version}/{package_name}'
    platform = document['platforms'][f'linux-x86_64-{package_type}']
    if platform['url'] != url or not platform['signature']:
        raise ValueError('Candidate update manifest does not contain the signed package')
    path = url.removeprefix('https://github.com')
    with tempfile.TemporaryDirectory(prefix='kinora-update-tls-') as temporary:
        temp = Path(temporary)
        with ThreadingHTTPServer(('127.0.0.1', 0), TunnelHandler) as server:
            server.tls = make_tls(temp)
            server.assets = {LATEST_PATH: manifest, path: directory / package_name}
            server.downloads = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            proxy = f'http://127.0.0.1:{server.server_port}'
            env = {
                'HTTPS_PROXY': proxy, 'https_proxy': proxy,
                'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                'SSL_CERT_FILE': str(temp / 'ca.pem'),
            }
            try:
                yield env, server.downloads, path
            finally:
                server.shutdown()
                thread.join()
