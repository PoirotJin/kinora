"""Operations boundaries against a real HTTP server and isolated SQLite data."""
from pathlib import Path
from contextlib import closing
import json
import unittest
import urllib.request
import urllib.error
import test_review_access as access_fixtures


class AdminOpsTests(unittest.TestCase):
    def setUp(self):
        self.f = access_fixtures.AccessTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.server.web = Path(__file__).resolve().parents[1] / 'web/review'

    def login(self, user, path='/api/admin/login'):
        request = urllib.request.Request(self.f.url+path, data=json.dumps(dict(username=user,password='changed-password-123')).encode(), headers={'Origin':self.f.url,'Content-Type':'application/json','X-MotionFarm-Request':'1'})
        try: response=urllib.request.urlopen(request)
        except urllib.error.HTTPError as error: response=error
        with response: return response.status, response.headers, json.load(response)

    def test_admin_only_login_preserves_desktop_login(self):
        with closing(self.f.auth.connect()) as db: before=db.execute('SELECT count(*) FROM sessions').fetchone()[0]
        code,headers,data=self.login('alice')
        self.assertEqual(code,403);self.assertIsNone(headers.get('Set-Cookie'))
        with closing(self.f.auth.connect()) as db: self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],before)
        self.assertEqual(self.login('alice','/api/auth/login')[0],200)
        code,headers,data=self.login('jfc')
        self.assertEqual(code,200);self.assertIn('mf_session=',headers['Set-Cookie']);self.assertEqual(data['user']['role'],'admin')

    def test_audit_is_admin_only_bounded_and_filtered(self):
        self.assertEqual(self.f.call('/api/admin/audit','unknown')[0],401)
        self.assertEqual(self.f.call('/api/admin/audit','alice')[0],403)
        for i in range(55): self.f.auth.audit('test.event','test-actor',str(i))
        code,data=self.f.call('/api/admin/audit?actor=test-actor','jfc')
        self.assertEqual(code,200);self.assertEqual(len(data['items']),50)
        self.assertEqual(set(data['items'][0]),{'id','ts','action','actor','target','peer','outcome'})
        next_page=self.f.call('/api/admin/audit?actor=test-actor&before='+str(data['next_before']),'jfc')[1]
        self.assertEqual(len(next_page['items']),5);self.assertIsNone(next_page['next_before'])
        self.assertEqual(self.f.call('/api/admin/audit?before=bad','jfc')[0],400)
        self.assertEqual(self.f.call('/api/admin/audit?actor=%27%20OR%201%3D1--','jfc')[1]['items'],[])

    def test_shell_and_application_desktop_round_trip(self):
        for path in ('/','/admin','/admin/','/admin-ops.js','/admin-ops.css'):
            with urllib.request.urlopen(self.f.url+path) as r:
                self.assertEqual(r.status,200)
                if path in ('/','/admin','/admin/'):
                    html=r.read().decode();self.assertIn('管理员登录',html);self.assertNotIn('src="/app.js"',html)
        application=dict(username='new-colleague',password='new-password-123',display_name='同事',reason='客户端使用')
        self.assertEqual(self.f.call('/api/account/register','unknown',application)[0],200)
        self.assertEqual(self.f.call('/api/account-applications/new-colleague/approve','jfc',{'note':'已核实'})[0],200)
        self.assertEqual(self.f.call('/api/auth/login','unknown',dict(username=application['username'],password=application['password']))[0],200)


if __name__ == '__main__': unittest.main()
