"""Bounded localhost regression: idle TLS clients cannot stall new requests."""
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from review_server import Server, Handler


class TLSEntryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'),'openssl needed for temporary localhost certificate')
    def test_unfinished_handshake_does_not_block_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'index.html').write_text('entry-ok')
            cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                            '-nodes','-subj','/CN=localhost','-days','1','-keyout',str(key),'-out',str(cert)],
                           check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            server=Server(('127.0.0.1',0),Handler);server.web=root
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
            server.tls_context=context;server.secure=True
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            idle=socket.create_connection(server.server_address,timeout=2)
            try:
                # This context bypass applies only to this temporary localhost test.
                with urllib.request.urlopen('https://127.0.0.1:%d/'%server.server_port,
                                            context=ssl._create_unverified_context(),timeout=2) as response:
                    self.assertEqual(response.read(),b'entry-ok')
            finally:
                idle.close();server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
