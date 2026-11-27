"""Real local HTTPS requests must never redirect provider credentials."""
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from rewrite_models import call
import MotionFarm as farm
from g2h import semantic_index


class RewriteRedirectTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'), 'openssl needed for local test certificate')
    def test_redirects_rejected_and_direct_response_works(self):
        captured = []
        class Destination(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                captured.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"choices":[],"data":[{"index":0,"embedding":[1,0]}]}')
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(self.server.response_code)
                if self.server.response_code != 200:
                    self.send_header('Location', 'http://127.0.0.1:%d/redirected' % destination.server_port)
                self.end_headers()
                self.wfile.write(b'{"choices":[],"data":[{"index":0,"embedding":[1,0]}]}')

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cert, key = root / 'cert.pem', root / 'tls.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                            '-nodes', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-days', '1', '-keyout', str(key), '-out', str(cert)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
            provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            provider.socket = server_context.wrap_socket(provider.socket, server_side=True)
            client_context = ssl.create_default_context(cafile=str(cert))
            api_key = root / 'synthetic.key'
            api_key.write_text('SYNTHETIC_TEST_TOKEN')
            (root / 'review').mkdir()
            model = dict(id='audit', label='Audit', provider='audit', model='test', adapter='openai_compatible',
                         key_file=str(api_key), base_url='https://127.0.0.1:%d/v1' % provider.server_port)
            (root / 'review/rewrite_models.json').write_text(json.dumps({'models': [model]}))
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (provider, destination)]
            for thread in threads:
                thread.start()
            build_opener = urllib.request.build_opener
            def local_opener(*handlers):
                # Trust only this test certificate and avoid machine proxy settings.
                return build_opener(*handlers, urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=client_context))
            try:
                with patch('urllib.request.build_opener', side_effect=local_opener), \
                     patch('g2h.api_usage.budgeted_request', return_value=nullcontext()), \
                     patch('g2h.api_usage.record'), \
                     patch.object(farm, 'BASE_URL', model['base_url']), \
                     patch.object(semantic_index, 'BASE_URL', model['base_url']):
                    messages = [{'role': 'user', 'content': 'Synthetic test'}]
                    adapters = {
                        'rewrite': lambda: call(root, 'audit', messages),
                        'qwen': lambda: farm.call_qwen('test', api_key, messages),
                        'embedding': lambda: semantic_index.embed(['Synthetic test'], api_key, dimensions=2),
                    }
                    for code in (301, 302, 303, 307, 308):
                        for name, adapter in adapters.items():
                            with self.subTest(code=code, adapter=name):
                                provider.response_code = code
                                with self.assertRaises(RuntimeError):
                                    adapter()
                                self.assertEqual(captured, [], 'Redirect target must receive no credential-bearing request')
                    provider.response_code = 200
                    self.assertEqual(adapters['rewrite']()['choices'], [])
                    self.assertEqual(adapters['qwen']()['choices'], [])
                    self.assertEqual(adapters['embedding']()[0].tolist(), [1, 0])
            finally:
                for server in (provider, destination):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
