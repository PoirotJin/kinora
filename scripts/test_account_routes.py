"""Exercise the public boundary without SMTP or production data."""
from contextlib import closing
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from review_auth import Auth
from review_account_routes import handle_public


class Handler:
    def __init__(self, root, value=None):
        self.server = SimpleNamespace(auth=Auth(root / 'db'), web=root / 'web/review', origins={'https://example.test'})
        self.headers = {'Origin': 'https://example.test', 'Content-Type': 'application/json', 'X-MotionFarm-Request': '1'}
        self.value = value or {}

    def origin_allowed(self, required=False):
        return self.headers.get('Origin') in self.server.origins or (not required and 'Origin' not in self.headers)

    def send(self, status, body, *args):
        self.status, self.result = status, body

    def download(self, path):
        self.send(200, path.read_bytes())

    def peer(self): return 'test'
    def body(self): return self.value


class AccountRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.handler = Handler(self.root)
        self.mail = []
        owner = self

        class FakeMailer:
            origin = 'https://example.test'
            def __call__(self, *args): owner.mail.append(args)
        self.mailer = patch('review_account_routes.Mailer', FakeMailer)

    def call(self, action, value, write=True):
        self.handler.value = value
        self.assertTrue(handle_public(self.handler, '/api/account/' + action, write))
        return self.handler.status, self.handler.result

    def test_unconfigured_service_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.call('status', {}, False), (200, {'email_enabled': False, 'registration_mode': 'approval'}))
            self.assertEqual(self.call('register', {'username':'applicant','password':'initial-password-123','display_name':'同事','reason':'使用客户端'})[0], 200)
            self.assertEqual(self.handler.result['status'], 'pending')
            self.assertEqual(self.call('forgot', {'email':'user@example.com'})[0], 503)
        self.assertEqual(self.handler.server.auth.users(), [])

    def test_csrf_is_checked_before_account_writes(self):
        with self.mailer:
            self.handler.headers['Origin'] = 'https://evil.test'
            self.assertEqual(self.call('forgot', {'email': 'x@example.com'})[0], 403)
            self.handler.headers['Origin'] = 'https://example.test'
            self.handler.headers.pop('X-MotionFarm-Request')
            self.assertEqual(self.call('forgot', {'email': 'x@example.com'})[0], 403)
        self.assertEqual(self.mail, [])

    def test_registration_email_login_and_reset(self):
        with self.mailer:
            # Preserve verification/reset compatibility for previously issued email tokens.
            from review_email_accounts import EmailAccounts
            EmailAccounts(self.handler.server.auth, lambda *args: self.mail.append(args)).register('TEST@example.com','tester','initial-password-123','legacy-registration')
            self.assertEqual(self.handler.server.auth.users(), [])
            self.call('verify', {'token': self.mail[-1][2]})
            auth = self.handler.server.auth
            session, user = auth.login('TEST@example.com', 'initial-password-123', 'login')
            self.assertEqual(user['username'], 'tester')
            self.call('forgot', {'email': 'test@example.com'})
            self.call('reset', {'token': self.mail[-1][2], 'password': 'replacement-password-123'})
            self.assertIsNone(auth.session(session))
            self.assertTrue(auth.login('test@example.com', 'replacement-password-123', 'login'))

    def test_update_feed_and_download_boundary(self):
        root = self.root / 'var/desktop-releases'
        root.mkdir(parents=True)
        path = '/api/desktop/update/darwin/aarch64/0.3.0'
        handle_public(self.handler, path, False)
        self.assertEqual(self.handler.status, 503)
        manifest = {'version': '0.4.0', 'platforms': {'darwin-aarch64': {'signature': 'test', 'url': 'https://example.test/desktop/releases/app.tar.gz'}}}
        (root / 'latest.json').write_text(json.dumps(manifest))
        (root / 'app.tar.gz').write_bytes(b'test-package')
        (root / 'secret').write_text('not published')
        handle_public(self.handler, path, False)
        self.assertEqual(self.handler.result['version'], '0.4.0')
        handle_public(self.handler, '/desktop/releases/app.tar.gz', False)
        self.assertEqual(self.handler.result, b'test-package')
        for invalid in ['/desktop/releases/secret', '/desktop/releases/../../secret']:
            handle_public(self.handler, invalid, False)
            self.assertEqual(self.handler.status, 404)
        handle_public(self.handler, '/api/desktop/update/windows/x86_64/0.3.0', False)
        self.assertEqual(self.handler.status, 503)
        self.handler.headers['Origin'] = 'https://evil.test'
        self.assertEqual(self.call('status', {}, False)[0], 403)


if __name__ == '__main__': unittest.main()

class DesktopDownloadsTest(unittest.TestCase):
    def test_public_installers_escape_notes_and_reject_unlisted_files(self):
        from desktop_download_page import render
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'var/desktop-releases';folder.mkdir(parents=True)
            m={'version':'0.5.0','notes':'<script>alert(1)</script>','platforms':{},'downloads':{'macos-universal':{'url':'https://example.test/desktop/releases/kinora.dmg','sha256':'a'*64}}}
            (folder/'latest.json').write_text(json.dumps(m));(folder/'kinora.dmg').write_bytes(b'installer')
            h=Handler(root);h.headers={}
            self.assertTrue(handle_public(h,'/desktop',False));self.assertEqual(h.status,200)
            self.assertIn(b'/desktop/releases/kinora.dmg',h.result);self.assertNotIn(b'<script>',h.result)
            self.assertIn(b'/desktop/download.css',h.result)
            handle_public(h,'/desktop/download.css',False);self.assertEqual(h.status,200);self.assertIn(b'background:#f5f3ed',h.result)
            handle_public(h,'/desktop/releases/kinora.dmg',False);self.assertEqual(h.result,b'installer')
            handle_public(h,'/desktop/releases/latest.json',False);self.assertEqual(h.status,404)
            handle_public(h,'/desktop',True);self.assertEqual(h.status,405)
            m['downloads']['macos-universal']['url']='javascript:alert(1)'
            self.assertNotIn('href="javascript:',render(m))


class LinuxUpdatePackagesTest(unittest.TestCase):
    def test_deb_and_appimage_are_distinct_signed_targets(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);folder=root/'var/desktop-releases';folder.mkdir(parents=True)
            packages={key:{'url':'https://example.test/desktop/releases/'+name,'signature':sig} for key,name,sig in [
                ('linux-x86_64','kinora.AppImage','image-signature'),
                ('linux-x86_64-appimage','kinora.AppImage','image-signature'),
                ('linux-x86_64-deb','kinora.deb','deb-signature')]}
            (folder/'latest.json').write_text(json.dumps({'version':'0.5.0','platforms':packages}))
            (folder/'kinora.deb').write_bytes(b'deb-bytes')
            h=Handler(root)
            handle_public(h,'/api/desktop/update/linux/x86_64/0.4.18',False)
            self.assertEqual(h.status,200);self.assertNotIn('url',h.result)
            self.assertEqual(h.result['platforms'],packages)
            self.assertEqual(h.result['platforms']['linux-x86_64-deb']['signature'],'deb-signature')
            handle_public(h,'/desktop/releases/kinora.deb',False);self.assertEqual(h.result,b'deb-bytes')
