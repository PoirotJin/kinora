"""Approval, credentials, races and public/administrator HTTP boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from review_auth import Auth
from review_account_applications import Applications
import test_review_access as access_fixtures


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.auth = Auth(self.temp.name)
        self.auth.create('jfc', 'temporary-password')
        self.auth.change_password('jfc', 'temporary-password', 'administrator-password')
        self.apps = Applications(self.auth)
        self.value = dict(username='colleague', password='colleague-password', display_name='Colleague', reason='Team testing')

    def test_pending_approval_and_password_privacy(self):
        self.assertEqual(self.apps.submit(self.value, 'peer')['status'], 'pending')
        self.assertIsNone(self.auth.login('colleague', self.value['password'], 'login'))
        self.assertEqual(self.apps.status(self.value, 'peer')['status'], 'pending')
        self.assertNotIn('digest', self.apps.listing()['applications'][0])
        self.assertNotIn('password', str(self.apps.listing()))
        with closing(self.auth.connect()) as db:
            self.assertNotIn(self.value['password'], str(db.execute('SELECT * FROM account_applications').fetchall()))
        self.apps.decide('colleague', 'approve', '', 'jfc', 'admin')
        self.assertEqual(self.apps.status(self.value, 'peer')['status'], 'approved')
        result = self.auth.login('colleague', self.value['password'], 'login')
        self.assertEqual(result[1]['role'], 'user'); self.assertFalse(result[1]['must_change'])
        self.assertEqual(self.apps.decide('colleague', 'approve', '', 'jfc', 'admin')['status'], 'approved')

    def test_rejection_and_conflicting_decision(self):
        self.apps.submit(self.value, 'peer')
        self.apps.decide('colleague', 'reject', 'Please contact the team', 'jfc', 'admin')
        self.assertEqual(self.apps.status(self.value, 'peer')['decision_note'], 'Please contact the team')
        self.assertIsNone(self.auth.login('colleague', self.value['password'], 'login'))
        with self.assertRaises(ValueError): self.apps.decide('colleague', 'approve', '', 'jfc', 'admin')

    def test_duplicate_wrong_password_existing_user_and_privilege(self):
        self.apps.submit(self.value, 'peer')
        self.assertEqual(self.apps.submit(self.value, 'peer')['status'], 'pending')
        with self.assertRaises(ValueError): self.apps.status(dict(self.value, password='incorrect-password'), 'peer')
        with self.assertRaises(ValueError): self.apps.submit(dict(self.value, password='incorrect-password'), 'peer')
        with self.assertRaises(ValueError): self.apps.submit(dict(self.value, username='jfc'), 'peer')
        with self.assertRaises(PermissionError): self.apps.decide('colleague', 'approve', '', 'colleague', 'peer')
        self.auth.create('colleague', 'different-password')
        with self.assertRaises(ValueError): self.apps.decide('colleague', 'approve', '', 'jfc', 'admin')
        self.assertTrue(self.auth.login('colleague', 'different-password', 'login'))

    def test_concurrent_approval_creates_one_user(self):
        self.apps.submit(self.value, 'peer')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.apps.decide('colleague', 'approve', '', 'jfc', 'admin'), range(4)))
        self.assertTrue(all(r['status'] == 'approved' for r in results))
        self.assertEqual(len([u for u in self.auth.users() if u['username'] == 'colleague']), 1)

    def test_throttled_submission(self):
        with patch.object(self.auth, 'throttle', side_effect=PermissionError('rate limited')):
            with self.assertRaises(PermissionError): self.apps.submit(self.value, 'peer')
        self.assertEqual(self.apps.listing()['applications'], [])


class ApplicationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = access_fixtures.AccessTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.call = self.fixture.call
        self.value = dict(username='applicant', password='applicant-password', display_name='Test colleague', reason='Team')

    def test_no_mail_approval_login_and_permissions(self):
        code, status = self.call('/api/account/status', 'unknown')
        self.assertEqual(code, 200); self.assertEqual(status['registration_mode'], 'approval')
        self.assertEqual(self.call('/api/account/register', 'unknown', self.value)[0], 200)
        self.assertEqual(self.call('/api/auth/login', 'unknown', self.value)[0], 401)
        for who, expected in [('unknown', 401), ('alice', 403)]:
            self.assertEqual(self.call('/api/account-applications', who)[0], expected)
            for action in ['approve', 'reject']:
                self.assertEqual(self.call('/api/account-applications/applicant/' + action, who, {})[0], expected)
        self.assertEqual(self.call('/api/account-applications', 'jfc')[1]['applications'][0]['username'], 'applicant')
        self.assertEqual(self.call('/api/account-applications/applicant/approve', 'jfc', {'role': 'admin'})[0], 200)
        self.assertEqual(self.call('/api/account/application-status', 'unknown', self.value)[1]['status'], 'approved')
        code, result = self.call('/api/auth/login', 'unknown', self.value)
        self.assertEqual(code, 200); self.assertEqual(result['user']['role'], 'user')


if __name__ == '__main__': unittest.main()
