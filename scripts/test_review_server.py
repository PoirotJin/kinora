"""Local review integrity and HTTP access control tests; synthetic temporary data."""
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock
import numpy as np
import MotionFarm as farm
from g2h.collection_store import CollectionStore
from g2h.farm_index import register_motion
from review_server import Store,Server,Handler,Conflict,validate_review,default_origins
from review_auth import Auth
from g2h.cold_storage import package


class ServerOriginTests(unittest.TestCase):
    def test_local_aliases_and_remote_transport(self):
        self.assertEqual(set(default_origins('127.0.0.1',8765)), {'http://localhost:8765','http://127.0.0.1:8765'})
        self.assertEqual(set(default_origins('localhost',80)), {'http://localhost','http://127.0.0.1'})
        self.assertEqual(default_origins('example.com',443,True), ['https://example.com'])
        self.assertEqual(default_origins('0.0.0.0',8765), ['http://0.0.0.0:8765'])
        self.assertEqual(default_origins('::1',8765), ['http://[::1]:8765'])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=self.root/'topics.json'
        self.config.write_text(json.dumps({'topics':[{'id':'walking','name':'走路','definition':'walking'}]}))
        self.assets=self.root/'assets';self.assets.mkdir()
        np.array([-1,0],dtype=np.int32).tofile(self.assets/'kintree.bin')
        np.array([[0,1,0],[0,2,0]],dtype=np.float32).tofile(self.assets/'j_template.bin')
        (self.assets/'joint_names.json').write_text('["root","head"]')
        db=farm.catalog(self.root)
        item=dict(id='a'*64,text='A person walks forward.',topic_id='walking',attributes={},source={})
        with db: db.execute('INSERT INTO prompts VALUES(?,?,?,?)',(item['id'],'walking',item['text'],json.dumps(item)))
        self.mid=register_motion(db,item['id'],'walking','task1',42);db.close()
        with CollectionStore(self.root/'topics/walking') as store:
            store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('task1','{}'));store.db.commit()
            store.append({'id':'task1','quality':{},'test_padding':'x'*12000,'motion':{'fps':np.array(30),'native_keypoints3d':np.array([[[0,1.2,0],[0,2.2,0]],[[0,1.2,0],[0,2.2,0]]]),'native_transl':np.array([[0,.2,0],[0,.2,1]])}})
        self.store=Store(self.root,self.config,self.assets)
        self.value=dict(revision=0,status='accepted',semantic='match',quality='good',issues=[],corrected_text='',notes='verified',reviewer='test')

    def tearDown(self): self.tmp.cleanup()

    def test_audit_and_conflict_and_filters(self):
        before=(self.root/'topics/walking/motions_000000.pkl').read_bytes()
        first=self.store.save(self.mid,self.value)
        self.assertEqual(first['revision'],1)
        with self.assertRaises(Conflict): self.store.save(self.mid,self.value)
        second=self.store.save(self.mid,{**self.value,'revision':1,'status':'uncertain','semantic':'unsure','notes':'recheck'})
        self.assertEqual(second['revision'],2)
        self.assertEqual(len(self.store.detail(self.mid)['history']),2)
        self.assertEqual(len(self.store.listing(status='pending')['items']),0)
        self.assertEqual(len(self.store.listing(status='uncertain')['items']),1)
        self.assertEqual(before,(self.root/'topics/walking/motions_000000.pkl').read_bytes())

    def test_ground_shift_is_not_added_twice(self):
        clip=self.store.preview(self.mid)
        np.testing.assert_allclose(clip['joints'][0][0],[0,1.2,0],atol=1e-6)
        np.testing.assert_allclose(clip['joints'][1][0],[0,1.2,1],atol=1e-6)
        self.assertEqual(clip['frames'],2)

    def test_invalid_labels_and_conflicting_acceptance(self):
        for changes in [{'status':'accepted','semantic':'mismatch'},{'reviewer':''},{'issues':['invalid']},{'corrected_text':'向前走'},{'quality':'good','issues':['foot_slide']}]:
            with self.assertRaises(ValueError): validate_review({**self.value,**changes})
        with self.assertRaises(ValueError): self.store.save('m00000099',self.value)

    def test_pending_task_cannot_be_reviewed(self):
        db=farm.catalog(self.root);mid=register_motion(db,'a'*64,'walking','notyet',43);db.close()
        with self.assertRaises(ValueError): self.store.save(mid,self.value)

    def test_cold_preview_gallery_and_review_remain_available(self):
        before=self.store.preview(self.mid)
        self.store.save(self.mid,self.value)
        result=package(self.root,'walking',0,min_age_days=0)
        self.assertLess(result['archive_bytes'],result['raw_bytes'])
        self.assertEqual(self.store.preview(self.mid),before)
        self.assertEqual(self.store.listing()['items'][0]['storage_tier'],'cold')
        saved=self.store.save(self.mid,{**self.value,'revision':1,'notes':'review after archive'})
        self.assertEqual(saved['revision'],2)

    def test_http_authorization_and_origin(self):
        server=Server(('127.0.0.1',0),Handler);server.store=self.store;server.auth=Auth(self.root/'review');server.web=self.root
        server.production=Mock()
        server.production.launch.return_value={'job_id':'test-job'}
        server.storage=Mock();archive=self.root/'test.mf.zip';archive.write_bytes(b'archive fixture');server.storage.download.return_value=archive
        server.auth.create('test','temporary-pass-123',role='admin')
        session=server.auth.change_password('test','temporary-pass-123','changed-pass-1234')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        headers={'Cookie':'mf_session='+session,'X-MotionFarm-Request':'1','Content-Type':'application/json','Origin':url}
        try:
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(url+'/api/motions',timeout=3)
            self.assertEqual(error.exception.code,401)
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+'/api/storage/download?id=test',timeout=3)
            self.assertEqual(error.exception.code,401)
            with urllib.request.urlopen(urllib.request.Request(url+'/api/storage/download?id=test',headers=headers),timeout=3) as response:
                self.assertEqual(response.read(),b'archive fixture')
                self.assertEqual(response.headers['Content-Type'],'application/zip')
            request=urllib.request.Request(url+'/api/production/start',data=b'{}',headers={'Content-Type':'application/json','X-MotionFarm-Request':'1','Origin':url})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request,timeout=3)
            self.assertEqual(error.exception.code,401)
            request=urllib.request.Request(url+'/api/production/start',data=b'{}',headers={**headers,'Origin':'http://evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request,timeout=3)
            self.assertEqual(error.exception.code,403)
            server.production.launch.assert_not_called()
            request=urllib.request.Request(url+'/api/production/start',data=b'{}',headers=headers)
            with urllib.request.urlopen(request,timeout=3) as response:self.assertEqual(response.status,202)
            server.production.launch.assert_called_once_with({},'test')
            request=urllib.request.Request(url+'/api/motions',headers=headers)
            with urllib.request.urlopen(request,timeout=3) as response: self.assertEqual(len(json.load(response)['items']),1)
            request=urllib.request.Request(url+'/api/motions/'+self.mid+'/review',data=json.dumps(self.value).encode(),headers={**headers,'Origin':'http://evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request,timeout=3)
            self.assertEqual(error.exception.code,403)
            request=urllib.request.Request(url+'/api/motions/'+self.mid+'/review',data=json.dumps({**self.value,'reviewer':'forged'}).encode(),headers=headers)
            with urllib.request.urlopen(request,timeout=3) as response:
                body=json.load(response);self.assertEqual(body['revision'],1);self.assertEqual(body['reviewer'],'test')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_account_login_change_logout_and_csrf(self):
        auth=Auth(self.root/'review');auth.create('alice','temporary-password-1')
        server=Server(('127.0.0.1',0),Handler);server.store=self.store;server.auth=auth;server.web=self.root
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        def call(path,value=None,token='',extra=None):
            headers={'Content-Type':'application/json','X-MotionFarm-Request':'1','Cookie':'mf_session='+token,'Origin':url}
            headers.update(extra or {})
            request=urllib.request.Request(url+path,data=json.dumps(value).encode() if value is not None else None,headers=headers)
            try:
                with urllib.request.urlopen(request,timeout=3) as response:
                    return response.status,json.load(response),response.headers.get('Set-Cookie','')
            except urllib.error.HTTPError as error:
                return error.code,json.load(error),''
        try:
            login={'username':'alice','password':'temporary-password-1'}
            self.assertEqual(call('/api/auth/login',login,extra={'X-MotionFarm-Request':''})[0],403)
            self.assertEqual(call('/api/auth/login',login,extra={'Origin':'http://evil.invalid'})[0],403)
            self.assertEqual(call('/api/auth/login',{**login,'password':'wrong'})[0],401)
            code,body,cookie=call('/api/auth/login',login)
            self.assertEqual(code,200);self.assertTrue(body['user']['must_change'])
            self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
            token=cookie.split(';')[0].split('=',1)[1]
            self.assertEqual(call('/api/motions',token=token)[0],403)
            self.assertEqual(call('/api/motions',extra={'X-Review-Token':'test-token'})[0],401)
            self.assertEqual(call('/api/auth/me',token=token)[1]['user']['username'],'alice')
            self.assertEqual(call('/api/auth/password',{'old_password':login['password'],'new_password':'short'},token)[0],400)
            code,body,cookie=call('/api/auth/password',{'old_password':login['password'],'new_password':'new-password-4567'},token)
            self.assertEqual(code,200);self.assertFalse(body['user']['must_change'])
            self.assertEqual(call('/api/auth/me',token=token)[0],401)
            token=cookie.split(';')[0].split('=',1)[1]
            self.assertEqual(call('/api/motions',token=token)[0],200)
            custom={'request_id':'test_custom_1','description':'slowly crouch and rise','n':3,'duration':8}
            self.assertEqual(call('/api/custom',custom)[0],401)
            self.assertEqual(call('/api/custom',custom,token,{'Origin':'http://evil.invalid'})[0],403)
            self.assertEqual(call('/api/custom',custom,token)[0],202)
            self.assertEqual(call('/api/custom',custom,token)[0],202)
            self.assertEqual(len(call('/api/custom',token=token)[1]['jobs']),1)
            self.assertEqual(call('/api/custom',token=token)[1]['jobs'][0]['owner'],'alice')
            self.assertEqual(call('/api/custom/cancel',{'request_id':'test_custom_1'},token)[0],202)
            self.assertEqual(call('/api/custom',token=token)[1]['jobs'][0]['status'],'cancelled')
            self.assertEqual(call('/api/auth/login',login)[0],401)
            self.assertEqual(call('/api/auth/logout',{},token)[0],200)
            self.assertEqual(call('/api/auth/me',token=token)[0],401)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_archive_transfer_failure_closes_response_without_appending_an_error(self):
        payload = b'kinora-archive-fixture' * 1024
        prefix = payload[:64]
        archive = self.root/'test.mf.zip'
        archive.write_bytes(payload)

        class InterruptedDownload(Handler):
            def download(self, path):
                write = self.wfile.write

                def interrupt(data):
                    if data.startswith(prefix):
                        write(prefix)
                        if self.path.endswith('timeout'):
                            raise TimeoutError('simulated write timeout')
                        raise BrokenPipeError('simulated connection loss')
                    return write(data)

                self.wfile.write = interrupt
                return super().download(path)

        with Server(('127.0.0.1',0),InterruptedDownload) as server:
            server.store = self.store
            server.web = self.root
            server.auth = Auth(self.root/'review')
            server.auth.create('test','temporary-pass-123',role='admin')
            session = server.auth.change_password('test','temporary-pass-123','changed-pass-1234')
            server.storage = Mock()
            server.storage.download.return_value = archive
            worker = threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/api/storage/download'
                for failure in ('timeout','disconnect'):
                    with self.subTest(failure=failure):
                        request = urllib.request.Request(url+'?id='+failure,headers={'Cookie':'mf_session='+session})
                        with urllib.request.urlopen(request,timeout=3) as response:
                            self.assertEqual(response.status,200)
                            self.assertEqual(response.headers.get('Content-Length'),str(len(payload)))
                            with self.assertRaises(IncompleteRead) as interrupted:
                                response.read()
                            self.assertEqual(interrupted.exception.partial,prefix)
            finally:
                server.shutdown()
                worker.join(timeout=3)

    def test_session_expiry_password_storage_and_rate_limit(self):
        import time
        from contextlib import closing
        auth=Auth(self.root/'review');auth.create('bob','temporary-password-2')
        token,user=auth.login('bob','temporary-password-2','local')
        self.assertEqual(Auth(self.root/'review').session(token)['username'],'bob')
        with closing(auth.connect()) as db, db:
            stored=db.execute('SELECT salt,digest FROM users').fetchone()
            self.assertNotIn('temporary-password-2',stored)
            db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
        self.assertIsNone(auth.session(token))
        for _ in range(12): auth.throttle('other-ip','unknown-user')
        with self.assertRaises(PermissionError): auth.throttle('other-ip','unknown-user')

    def test_preview_assets_resolution_agent_kit_custom_body_download_port_and_nginx(self):
        import io
        import zipfile
        from unittest.mock import patch
        from review_server import resolve_preview_assets, download_path_allowed

        project = Path(self.tmp.name) / 'proj'
        wooden = project / 'third_party/HY-Motion-1.0/scripts/gradio/static/assets/dump_wooden'
        wooden.mkdir(parents=True)
        self.assertEqual(resolve_preview_assets(project), project / 'data/human_rig')
        (wooden / 'kintree.bin').write_bytes(b'1')
        self.assertEqual(resolve_preview_assets(project), project / 'data/human_rig')
        (wooden / 'j_template.bin').write_bytes(b'1')
        self.assertEqual(resolve_preview_assets(project), wooden)
        override = project / 'custom-assets'
        self.assertEqual(resolve_preview_assets(project, override), override)

        for allowed_path in (
            '/api/storage/download',
            '/api/storage/archive',
            '/api/retarget/download',
            '/api/retarget/repair-download',
            f'/api/harness/motions/{self.mid}/download',
            '/api/harness/export',
            '/api/library/export',
            '/api/harness/robot-edits/' + 'a' * 32 + '/download',
            '/api/harness/personal-assets/snapshots/' + 'b' * 32 + '/download',
            '/api/gadget/v1/download',
            '/api/gadget/v1/source',
        ):
            self.assertTrue(download_path_allowed(allowed_path), allowed_path)
        self.assertFalse(download_path_allowed('/api/harness/tasks'))

        nginx_text = (Path(__file__).resolve().parents[1] / 'deploy/nginx.conf').read_text()
        self.assertIn('proxy_read_timeout 90s;', nginx_text)
        for token in (
            'harness/robots|gadget/v1/robot_import',
            'client_max_body_size 45m;',
            'import|harness_import',
            'import-patch',
            'client_max_body_size 17m;',
            'robot_edit_apply|harness_step',
            'client_max_body_size 96k;',
            'personal/presets',
            'client_max_body_size 32k;',
            'retarget/(?:download|repair-download)',
            '(?:harness|library)/export',
            'gadget/v1/(?:download|source)',
        ):
            self.assertIn(token, nginx_text)

        auth = Auth(self.root / 'review')
        auth.create('alice', 'temporary-pass-123')
        token = auth.change_password('alice', 'temporary-pass-123', 'user-password-456')
        with Server(('127.0.0.1', 0), Handler) as server:
            server.store = self.store
            server.web = self.root / 'empty-web'
            server.web.mkdir()
            server.auth = auth
            server.retarget = Mock()
            server.storage = Mock()
            server.behind_proxy = False
            downloads = server.download_listener(0)
            t_main = threading.Thread(target=server.serve_forever, daemon=True)
            t_dl = threading.Thread(target=downloads.serve_forever, daemon=True)
            t_main.start()
            t_dl.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                dl_base = f'http://127.0.0.1:{downloads.server_port}'
                # Missing archive shard returns 404 when mounted; offline cold disk returns 503.
                miss_req = urllib.request.Request(
                    base + '/api/storage/archive?topic=walking&shard=999999',
                    headers={'Cookie': 'mf_session=' + token},
                )
                with self.assertRaises(urllib.error.HTTPError) as miss_err:
                    urllib.request.urlopen(miss_req, timeout=4)
                self.assertEqual(miss_err.exception.code, 404)
                with patch('g2h.storage_tiers.cold_root', side_effect=RuntimeError('冷存储数据盘未正确挂载；暂停归档')):
                    with self.assertRaises(urllib.error.HTTPError) as offline_err:
                        urllib.request.urlopen(miss_req, timeout=4)
                    self.assertEqual(offline_err.exception.code, 503)
                with urllib.request.urlopen(base + '/gadget/agent-kit.zip', timeout=4) as resp:
                    self.assertEqual(resp.status, 200)
                    raw_zip = resp.read()
                self.assertFalse((server.web / 'gadget-kit.zip').exists())
                with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
                    self.assertIn('motionfarm-gadget/BUNDLE-MANIFEST.json', zf.namelist())

                objects = [
                    {
                        'id': f'obj{idx}',
                        'center_m': [float(idx) * 2.0, 0.5, 2.0],
                        'size_m': [0.5, 1.0, 0.5],
                        'heading_deg': 0.0,
                        'trajectory': [{'frame': f, 'center_m': [float(idx) * 2.0, 0.5, 2.0]} for f in range(60)],
                    }
                    for idx in range(8)
                ]
                waypoints = [{'frame': i * 3, 'xz_m': [0.0, float(i) * 0.01], 'heading_deg': 0.0} for i in range(60)]
                payload = json.dumps({
                    'request_id': 'custom_scene_big_1',
                    'description': 'A person walks forward toward the box in the room slowly.',
                    'n': 1,
                    'duration': 15,
                    'prompt_mode': 'exact_english',
                    'scene': {
                        'kind': 'human_interaction_v1',
                        'objects': objects,
                        'waypoints': waypoints, 'keyframes': [], 'contacts': [],
                    },
                }).encode()
                self.assertGreater(len(payload), 16000)
                req = urllib.request.Request(
                    base + '/api/custom', data=payload,
                    headers={'Cookie': 'mf_session=' + token, 'Content-Type': 'application/json',
                             'X-MotionFarm-Request': '1', 'Origin': base},
                )
                from g2h.generation_backend import KIMODO_MODEL
                with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}), \
                     patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
                    with urllib.request.urlopen(req, timeout=4) as resp:
                        self.assertEqual(resp.status, 202)

                dl_req = urllib.request.Request(
                    dl_base + '/api/harness/export?ids=bad&format=npz',
                    headers={'Cookie': 'mf_session=' + token},
                )
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(dl_req, timeout=4)
                self.assertEqual(err.exception.code, 400)
            finally:
                downloads.shutdown(); downloads.server_close()
                server.shutdown(); server.server_close()
                t_dl.join(timeout=3); t_main.join(timeout=3)

    def test_retarget_and_custom_error_sanitization_strips_exception_prefixes(self):
        from review_custom import Custom
        from review_retarget import _clean_engine_error

        self.assertEqual(_clean_engine_error(['Traceback (most recent call last):', 'ValueError: 骨架长度异常']), '骨架长度异常')
        self.assertEqual(_clean_engine_error(['cvxpy.error.SolverError: Solver CLARABEL failed']), '求解器未收敛，请调整参数后重试')
        custom = Custom(self.root)
        log_path = custom.folder / 'custom_err_1.log'
        log_path.write_text('Traceback ...\nRuntimeError: ValueError: 显存分配失败\n')
        self.assertEqual(custom._failure_reason(self.root / 'topics/custom', 'custom_err_1', [0]), '显存分配失败')


if __name__=='__main__': unittest.main()
