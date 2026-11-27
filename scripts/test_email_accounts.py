import tempfile
import unittest
from contextlib import closing

from review_auth import Auth
from review_email_accounts import EmailAccounts


class EmailAccountsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.auth = Auth(self.directory.name)
        self.mail = []
        self.accounts = EmailAccounts(self.auth, lambda *args: self.mail.append(args))

    def register(self):
        self.accounts.register('user@example.com', 'new-user', 'initial-password-123', 'registration')
        token = self.mail[-1][2]
        self.accounts.consume(token, 'register', 'verification')
        return token

    def test_verification_and_replay(self):
        self.accounts.register('user@example.com', 'new-user', 'initial-password-123', 'registration')
        self.assertEqual(self.auth.users(), [])
        token = self.mail[-1][2]
        self.accounts.consume(token, 'register', 'verification')
        _, user = self.auth.login('new-user', 'initial-password-123', 'login')
        self.assertEqual(user['role'], 'user')
        self.assertFalse(user['must_change'])
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'register', 'verification')

    def test_reset_revokes_session_and_old_password(self):
        self.register()
        session, _ = self.auth.login('new-user', 'initial-password-123', 'login')
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        self.accounts.consume(token, 'reset', 'reset', 'replacement-password-123')
        self.assertIsNone(self.auth.session(session))
        self.assertIsNone(self.auth.login('new-user', 'initial-password-123', 'login'))
        self.assertTrue(self.auth.login('new-user', 'replacement-password-123', 'login'))
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'reset', 'reset', 'replacement-password-456')

    def test_disabled_account_cannot_reset(self):
        self.register()
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        with closing(self.auth.connect()) as db, db:
            db.execute("UPDATE users SET disabled=1 WHERE username='new-user'")
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'reset', 'reset', 'replacement-password-123')

    def test_password_change_revokes_existing_reset_link(self):
        self.register()
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        session = self.auth.change_password('new-user', 'initial-password-123', 'rotated-password-123')
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'reset', 'reset', 'attacker-password-123')
        self.assertIsNotNone(self.auth.session(session))
        self.assertTrue(self.auth.login('new-user', 'rotated-password-123', 'login'))

    def test_admin_password_reset_revokes_existing_reset_link(self):
        self.register()
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        replacement = self.auth.reset_password('new-user')
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'reset', 'reset', 'attacker-password-123')
        self.assertTrue(self.auth.login('new-user', replacement, 'login'))

    def test_failed_password_change_preserves_valid_recovery_link(self):
        self.register()
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        with self.assertRaises(ValueError):
            self.auth.change_password('new-user', 'incorrect-password-123', 'rotated-password-123')
        self.accounts.consume(token, 'reset', 'reset', 'recovered-password-123')
        self.assertTrue(self.auth.login('new-user', 'recovered-password-123', 'login'))

    def test_expired_and_wrong_purpose_tokens(self):
        token = self.register()
        self.accounts.request_reset('user@example.com', 'reset')
        token = self.mail[-1][2]
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'register', 'reset')
        with closing(self.auth.connect()) as db, db:
            db.execute('UPDATE email_tokens SET expires=0')
        with self.assertRaises(ValueError):
            self.accounts.consume(token, 'reset', 'reset', 'replacement-password-123')

    def test_unknown_email_has_same_response(self):
        self.register()
        known = self.accounts.request_reset('user@example.com', 'reset')
        count = len(self.mail)
        unknown = self.accounts.request_reset('missing@example.com', 'reset')
        self.assertEqual(known, unknown)
        self.assertEqual(count, len(self.mail))

    def test_delivery_failure_removes_token(self):
        def fail(*args):
            raise RuntimeError('smtp failure')
        self.accounts.send_mail = fail
        with self.assertRaises(OSError):
            self.accounts.register('user@example.com', 'new-user', 'initial-password-123', 'registration')
        with closing(self.auth.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM email_tokens').fetchone()[0], 0)

    def test_approval_and_admin_protection(self):
        self.accounts.require_approval = True
        self.register()
        self.assertTrue(self.auth.users()[0]['disabled'])
        with self.assertRaises(ValueError):
            self.accounts.register('admin@example.com', 'jfc', 'initial-password-123', 'registration')


if __name__ == '__main__':
    unittest.main()
