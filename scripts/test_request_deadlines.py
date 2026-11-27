"""Bounded localhost checks for slow input and unaffected response streaming."""
import json
import socket
import threading
import time
import unittest
import urllib.request

from review_server import Handler, Server


class DeadlineHandler(Handler):
    read_timeout = 0.4
    header_timeout = 0.4

    def do_GET(self):
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Length', '10')
            self.end_headers()
            for _ in range(10):
                self.wfile.write(b'x')
                self.wfile.flush()
                time.sleep(0.06)
        else:
            self.send(200, {'healthy': True})

    def do_POST(self):
        try:
            self.send(200, self.body())
        except TimeoutError:
            self.close_connection = True
            self.send(408, {'error': 'read deadline'})


class RequestDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(('127.0.0.1', 0), DeadlineHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.sockets = []
        self.stop = threading.Event()
        self.writers = []
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.stop.set()
        for writer in self.writers:
            writer.join(timeout=2)
        for connection in self.sockets:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self, prefix):
        connection = socket.create_connection(self.server.server_address, timeout=2)
        connection.sendall(prefix)
        self.sockets.append(connection)
        return connection

    def drip(self, connection, byte=b'x'):
        def write():
            while not self.stop.wait(0.04):
                try:
                    connection.sendall(byte)
                except OSError:
                    return
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        self.writers.append(writer)

    def test_slow_headers_release_all_worker_slots(self):
        for _ in range(8):
            connection = self.connect(b'GET / HTTP/1.1\r\nHost: localhost\r\nX-Slow: ')
            self.drip(connection)
        # Each socket makes progress well inside the inactivity timeout.
        # Its incomplete header must nevertheless hit the absolute deadline.
        for connection in self.sockets:
            try:
                self.assertEqual(connection.recv(1024), b'')
            except ConnectionResetError:
                pass
        with urllib.request.urlopen(self.url, timeout=2) as response:
            self.assertEqual(json.load(response), {'healthy': True})

    def test_partial_body_times_out_despite_progress(self):
        connection = self.connect(b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n{')
        self.drip(connection, b' ')
        response = connection.recv(4096)
        self.assertIn(b' 408 ', response)
        with urllib.request.urlopen(self.url, timeout=2) as healthy:
            self.assertEqual(healthy.status, 200)

    def test_complete_body_and_long_response_still_work(self):
        request = urllib.request.Request(self.url, data=b'{"accepted":true}', headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(json.load(response), {'accepted': True})
        with urllib.request.urlopen(self.url + '/stream', timeout=2) as response:
            self.assertEqual(response.read(), b'x' * 10)

    def test_upload_budget_scales_with_declared_size(self):
        class UploadHandler(DeadlineHandler):
            body_min_rate = 32
        self.server.RequestHandlerClass = UploadHandler
        payload = json.dumps({'upload': 'x' * 60}).encode()
        connection = self.connect(b'POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n')
        for start in range(0, len(payload), 10):
            connection.sendall(payload[start:start + 10])
            time.sleep(0.08)
        self.assertIn(b' 200 ', connection.recv(4096))


if __name__ == '__main__':
    unittest.main()
