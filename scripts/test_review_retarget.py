import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
import numpy as np
from review_retarget import Retarget,ROBOT
from review_access import Forbidden,allowed
from retarget_engine import world_points

class Store:
    def catalog(self):
        import sqlite3
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE motion_links(number INTEGER)');db.execute('INSERT INTO motion_links VALUES(1)');return db
    def detail(self,mid):return {'location':True}

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        from review_auth import Auth
        self.auth=Auth(self.root/'review')
        for name,role in [('a','user'),('b','user'),('jfc','admin')]:self.auth.create(name,'temporary-test-password',role)
        self.queue=Retarget(self.root);self.a={'username':'a','role':'user'};self.b={'username':'b','role':'user'};self.admin={'username':'jfc','role':'admin'}
    def tearDown(self):self.temp.cleanup()
    def submit(self,n=1,user=None):
        return self.queue.submit({'id':f'{n:032x}','motion_id':'m00000001','robot':ROBOT},user or self.a,Store())

    def test_completed_job_with_missing_result_is_not_exportable(self):
        from unittest.mock import patch

        key = self.submit()['id']
        folder = self.queue.folder / key
        folder.mkdir(exist_ok=True)
        (folder / 'quality.json').write_text(json.dumps({
            'version': 'robot-quality-v3', 'dynamics_required': False, 'export_allowed': True,
        }))
        (folder / 'preview.json').write_text('{}')
        self.queue.update(key, status='done')
        with patch.dict('os.environ', {'KINORA_RETARGET_REQUIRE_DYNAMICS': '0'}):
            row, = self.queue.status(self.a, {'history': ['1']})['items']
            self.assertFalse(row['export_verified'])
            self.assertTrue(row['result_missing'])
            self.assertIn('结果文件缺失', row['export_reason'])
            self.assertFalse(row['source_removed'])
            with self.assertRaisesRegex(ValueError, '结果文件不存在'):
                self.queue.result(key, self.a, 'result.npz', require_dynamic=True)
            with self.assertRaisesRegex(ValueError, '结果文件不存在'):
                self.queue.result(key, self.a, 'preview.json')

    def test_removed_source_is_unavailable_in_completed_job_history(self):
        import sqlite3
        from review_access import Removed, setup
        from unittest.mock import patch

        job = self.submit()
        key, motion = job['id'], job['motion_id']
        folder = self.queue.folder / key
        folder.mkdir(exist_ok=True)
        (folder / 'result.npz').write_bytes(b'result')
        (folder / 'quality.json').write_text(json.dumps({
            'version': 'robot-quality-v3', 'dynamics_required': False, 'export_allowed': True,
        }))
        self.queue.update(key, status='done')
        ledger = self.root / 'review/reviews.sqlite3'
        with patch.dict('os.environ', {'KINORA_RETARGET_REQUIRE_DYNAMICS': '0'}):
            self.assertTrue(self.queue.get(key, self.a)['export_verified'])
            with closing(sqlite3.connect(ledger)) as db, db:
                setup(db)
                db.execute('INSERT INTO removed VALUES (?, ?, ?, ?)', (motion, 1, 'jfc', 'fixture'))
            with self.assertRaises(Removed):
                self.queue.result(key, self.a, 'result.npz', require_dynamic=True)
            row, = self.queue.status(self.a, {'history': ['1']})['items']
            self.assertTrue(row['source_removed'])
            self.assertFalse(row['export_verified'])
            self.assertEqual(row['status'], 'done')
            with closing(sqlite3.connect(ledger)) as db, db:
                db.execute('DELETE FROM removed WHERE motion_id=?', (motion,))
            self.assertTrue(self.queue.get(key, self.a)['export_verified'])

    def test_status_refreshes_watched_job_outside_history_page(self):
        import sqlite3
        from review_access import setup

        original = self.submit()
        key, motion = original['id'], original['motion_id']
        self.queue.update(key, status='done')
        with closing(self.queue.db()) as db, db:
            db.executemany(
                "INSERT INTO jobs(id,owner,motion_id,robot,status,created,updated) VALUES(?,?,?,?,'done',?,?)",
                [(f'{index:032x}', 'a', 'm00000002', ROBOT, original['created'] + index, original['updated'])
                 for index in range(2, 102)],
            )
        with closing(sqlite3.connect(self.root / 'review/reviews.sqlite3')) as db, db:
            setup(db)
            db.execute('INSERT INTO removed VALUES (?, ?, ?, ?)', (motion, 1, 'jfc', 'fixture'))

        for history in ('0', '1'):
            query = {'history': [history], 'watch': [key]}
            page = self.queue.status(self.a, query)
            self.assertEqual(len(page['items']), 100)
            self.assertNotIn(key, [row['id'] for row in page['items']])
            self.assertEqual(page['watched']['id'], key)
            self.assertTrue(page['watched']['source_removed'])
            self.assertIsNone(self.queue.status(self.b, query)['watched'])
        second = self.queue.status(self.a, {'history': ['1'], 'after': [page['next_after']], 'watch': [key]})
        self.assertEqual([row['id'] for row in second['items']], [key])
        self.assertIsNone(second['watched'])

    def test_semantic_handoff_replaces_slot_atomically(self):
        for n in range(1,6):self.submit(n)
        parent=f'{1:032x}';child=f'{6:032x}'
        value=dict(id=child,motion_id='m00000001',robot=ROBOT,parent_job_id=parent,force_new=True)
        self.queue.submit(value,self.a,Store(),handoff_from=parent)
        self.assertEqual(self.queue.get(parent,self.a)['status'],'failed')
        self.assertEqual(self.queue.get(child,self.a)['status'],'queued')
        self.assertEqual(self.queue.status(self.a)['queued'],5)

    def test_cancelled_parent_cannot_handoff(self):
        parent=self.submit()['id'];self.queue.cancel(parent,self.a)
        with self.assertRaises(PermissionError):
            self.queue.submit(dict(id=f'{2:032x}',motion_id='m00000001',robot=ROBOT,force_new=True),self.a,Store(),handoff_from=parent)
    def test_roles(self):
        self.assertTrue(allowed(self.a,'/api/retarget',True));self.assertTrue(allowed(self.a,'/api/retarget/download',False))
        self.assertFalse(allowed(self.a,'/api/retarget/download',True));self.assertFalse(allowed(self.a,'/api/production/start',True))
    def test_owner_results_and_cancel(self):
        row=self.submit();key=row['id'];self.submit(2,self.b)
        self.assertEqual(len(self.queue.status(self.a)['items']),1)
        self.assertEqual(self.queue.status(self.a)['queued'],1)
        self.assertEqual(self.queue.status(self.admin)['queued'],2)
        self.assertEqual(len(self.queue.status(self.admin)['items']),2)
        for name in ('result.npz','preview.json'):
            with self.assertRaises(Forbidden):self.queue.result(key,self.b,name)
        with self.assertRaises(Forbidden):self.queue.cancel(key,self.b)
        self.assertEqual(self.queue.cancel(key,self.admin)['status'],'cancelled')
    def test_idempotency(self):
        self.submit();self.submit();self.assertEqual(len(self.queue.status(self.a)['items']),1)
        with self.assertRaises(ValueError):self.submit(1,self.b)
    def test_concurrent_limit(self):
        def attempt(n):
            try:self.submit(n);return True
            except PermissionError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt,range(1,9))),5)

    def test_concurrent_queue_startup_creates_a_complete_schema(self):
        root = self.root / 'fresh'
        workers = 8
        start = Barrier(workers)

        def read_schema(_):
            start.wait(timeout=5)
            queue = Retarget(root)
            with closing(queue.db()) as db:
                return {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            schemas = list(pool.map(read_schema, range(workers)))
        expected = {'retry_at', 'worker_slot', 'execution', 'cache_key', 'reused_from',
                    'requested_execution', 'reuse_prior_parameters'}
        self.assertTrue(all(expected <= columns for columns in schemas))
    def test_model_and_id_validation(self):
        for v in ({'id':'../bad','motion_id':'m00000001'},{'id':'0'*32,'motion_id':'../../secret'},{'id':'0'*32,'motion_id':'m00000001','robot':'g1'}):
            with self.assertRaises(ValueError):self.queue.submit(v,self.a,Store())
    def test_result_ready(self):
        key=self.submit()['id']
        with self.assertRaises(ValueError):self.queue.result(key,self.a,'result.npz')
        folder=self.queue.folder/key;folder.mkdir();(folder/'result.npz').write_bytes(b'x')
        self.queue.update(key,status='done')
        self.assertEqual(self.queue.result(key,self.a,'result.npz'),folder/'result.npz')
    def test_queue_interleaves_users(self):
        self.submit(1);self.submit(2);self.submit(3,self.b)
        rows={r['id']:r for r in self.queue.status(self.admin)['items']}
        self.assertEqual(rows[f'{3:032x}']['queue_position'],2)

    def test_cancel_wins_over_a_worker_requeue_and_releases_the_slot(self):
        for index in range(1, 6):
            self.submit(index)
        claimed = self.queue.claim(0)
        key = claimed['id']
        self.assertEqual(self.queue.cancel(key, self.a)['status'], 'stopping')
        self.queue.update(key, status='queued', retry_at=100, message='等待资源后重试')
        job = self.queue.get(key, self.a)
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['message'], '已取消')
        self.assertEqual(self.submit(6)['status'], 'queued')
        self.queue.update(key, status='failed', message='求解进程已退出')
        self.assertEqual(self.queue.get(key, self.a)['status'], 'cancelled')
        self.queue.update(key, done=9, total=10, message='迟到的求解进度')
        self.assertEqual(self.queue.get(key, self.a)['message'], '已取消')

    def test_cancel_after_solver_exit_wins_over_completion_and_quarantines_output(self):
        import sys
        from unittest.mock import Mock, patch

        key = self.submit()['id']
        folder = self.queue.folder / key

        def solver_exits(*args, **kwargs):
            (folder / 'result.npz').write_bytes(b'candidate')
            (folder / 'preview.json').write_text('{}')
            self.assertEqual(self.queue.cancel(key, self.a)['status'], 'stopping')
            return Mock(returncode=0, poll=Mock(return_value=0))

        with patch('review_retarget.os.nice'), patch('semantic_retarget.preflight', return_value=True), \
             patch('retarget_cache.identity', return_value=None), \
             patch('review_retarget.subprocess.Popen', side_effect=solver_exits), \
             patch.object(sys, 'argv', ['review_retarget.py', 'worker', '1']):
            self.queue.work()
        row = self.queue.get(key, self.a)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['message'], '已取消')
        self.assertFalse(row['export_verified'])
        self.assertEqual((folder / 'quarantine' / 'result.npz').read_bytes(), b'candidate')
        report = json.loads((folder / 'quality.json').read_text())
        self.assertEqual(report['reason'], '已取消')

    def test_cancellation_before_cache_lookup_does_not_materialize_a_result(self):
        from unittest.mock import patch

        key = self.submit()['id']

        def cancel_during_preflight(*args):
            self.assertEqual(self.queue.cancel(key, self.a)['status'], 'stopping')
            return True

        with patch('review_retarget.os.nice'), patch('retarget_cache.identity', return_value=None), \
             patch('semantic_retarget.preflight', side_effect=cancel_during_preflight), \
             patch('retarget_cache.reuse') as reuse:
            self.queue.work()
        reuse.assert_not_called()
        row = self.queue.get(key, self.a)
        self.assertEqual((row['status'], row['message'], row['reused_from']), ('cancelled', '已取消', ''))

    def test_worker_restart_resolves_an_interrupted_cancellation(self):
        key = self.submit()['id']
        self.assertEqual(self.queue.claim(0)['id'], key)
        self.assertEqual(self.queue.cancel(key, self.a)['message'], '取消请求已接收')
        self.assertIsNone(self.queue.claim(0))
        row = self.queue.get(key, self.a)
        self.assertEqual((row['status'], row['message']), ('cancelled', '已取消'))

    def test_cancelling_during_semantic_analysis_prevents_submission(self):
        from semantic_retarget import preflight
        from unittest.mock import patch

        key = self.submit()['id']
        row = self.queue.claim(0)
        folder = self.queue.folder / key
        folder.mkdir(exist_ok=True)

        def analyse(_record):
            self.assertEqual(self.queue.cancel(key, self.a)['status'], 'stopping')
            return {'status': 'fail', 'reasons': ['fixture direction mismatch']}

        with patch('semantic_retarget.source_record', return_value={
            'text': 'A person walks forward with slow steps while keeping both arms relaxed at their sides.',
            'duration_s': 3,
        }), patch('semantic_retarget.for_record', side_effect=analyse), \
             patch('review_custom.Custom.submit') as submit:
            self.assertFalse(preflight(self.queue, row, folder))
        submit.assert_not_called()
        final = self.queue.get(key, self.a)
        self.assertEqual((final['status'], final['message']), ('cancelled', '已取消'))

    def test_direction_correction_requeues_parent_and_preserves_link_for_cancellation(self):
        from review_custom import Custom
        from semantic_retarget import preflight
        from unittest.mock import patch

        key = self.submit()['id']
        row = self.queue.claim(0)
        folder = self.queue.folder / key
        folder.mkdir(exist_ok=True)
        request_id = 'direction-' + key

        with patch('semantic_retarget.source_record', return_value={
            'text': 'A person walks forward with slow steps while keeping both arms relaxed at their sides.',
            'duration_s': 3,
        }), patch('semantic_retarget.for_record', return_value={
            'status': 'fail', 'reasons': ['fixture direction mismatch'],
        }):
            self.assertFalse(preflight(self.queue, row, folder))

        parent = self.queue.get(key, self.a)
        self.assertEqual(parent['status'], 'queued')
        self.assertGreater(parent['retry_at'], parent['updated'])
        checkpoint = json.loads((folder / 'semantic-recovery.json').read_text())
        self.assertEqual(checkpoint['request_id'], request_id)
        self.assertEqual(self.queue.cancel(key, self.a)['status'], 'cancelled')
        custom = Custom(self.root)
        with closing(custom.db()) as db:
            self.assertEqual(custom.authorize(db, request_id, self.a)['status'], 'cancelled')

    def test_completed_direction_correction_keeps_the_pinned_preset_version(self):
        from unittest.mock import patch

        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from review_personal import mutate, save
        from semantic_retarget import preflight
        from test_personal_assets import commit_scene_candidate
        from test_review_access import AccessTests

        access = AccessTests()
        access.setUp()
        self.addCleanup(access.tearDown)
        service = access.server.harness_service = HarnessService(access.root, legacy_store=access.store)
        queue = access.server.retarget = Retarget(access.root)
        custom = Custom(access.root)
        user = {'username': 'alice', 'role': 'user'}
        preset_input = {'id': 'a' * 32, 'name': 'Pinned weights', 'robot': ROBOT,
                        'parameters': {'smooth_weight': 1.4, 'root_orientation_weight': 64}}
        preset = save(access.store, user, preset_input)
        with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                       'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            status, task = access.call('/api/harness/multi-scene', body={
                'request_id': 'pinned-preset-scene', 'duration': 1, 'actors': [{'id': 'R1'}],
            })
            self.assertEqual(status, 202, task)
            original = commit_scene_candidate(custom, task['operations'][0]['handle']['request_id'])
            key = '1' * 32
            status, parent = access.call('/api/retarget', body={
                'id': key, 'motion_id': original, 'robot': ROBOT,
                'preset_id': preset['id'], 'preset_version': preset['version'],
            })
            self.assertEqual(status, 202, parent)
            queue.update(key, status='running')
            folder = queue.folder / key
            folder.mkdir()
            with patch('semantic_retarget.for_record', return_value={'status': 'fail', 'reasons': ['方向偏差']}):
                self.assertFalse(preflight(queue, queue.get(key, user), folder))

            current = save(access.store, user, dict(preset_input, version=1,
                                                   parameters={'smooth_weight': 4}))
            mutate(access.store, user, {'id': current['id'], 'version': current['version'],
                                        'archive_revision': 0, 'action': 'archive'})
            correction = 'direction-' + key
            corrected_motion = commit_scene_candidate(custom, correction)
            custom.update(correction, status='completed', done=1)
            self.assertFalse(preflight(queue, queue.get(key, user), folder))

            _, fresh = access.call('/api/retarget?history=1')
            child, = [job for job in fresh['items'] if job['motion_id'] == corrected_motion]
            execution = json.loads(child['execution'])
            original_execution = json.loads(parent['execution'])
            self.assertEqual(child['robot'], parent['robot'])
            self.assertEqual(execution['parameters'], original_execution['parameters'])
            self.assertEqual(execution['preset'], original_execution['preset'])
            self.assertEqual(execution['parent_job_id'], key)
            self.assertTrue(execution['force_new'])
            self.assertFalse(preflight(queue, queue.get(key, user), folder))
            _, replay = access.call('/api/retarget?history=1')
            self.assertEqual({job['id'] for job in replay['items']}, {parent['id'], child['id']})

    def test_cancel_finds_committed_direction_correction_without_a_checkpoint(self):
        from review_custom import Custom
        from semantic_retarget import preflight
        from unittest.mock import patch

        key = self.submit()['id']
        row = self.queue.claim(0)
        folder = self.queue.folder / key
        folder.mkdir(exist_ok=True)
        custom = Custom(self.root)
        request_id = 'direction-' + key

        class WorkerInterrupted(BaseException):
            pass

        with patch('semantic_retarget.source_record', return_value={
            'text': 'A person walks forward with slow steps while keeping both arms relaxed at their sides.',
            'duration_s': 3,
        }), patch('semantic_retarget.for_record', return_value={
            'status': 'fail', 'reasons': ['fixture direction mismatch'],
        }), patch('qwen_motion_agent.checkpoint_json', side_effect=WorkerInterrupted):
            with self.assertRaises(WorkerInterrupted):
                preflight(self.queue, row, folder)

        self.assertFalse((folder / 'semantic-recovery.json').exists())
        with closing(custom.db()) as db:
            self.assertEqual(custom.authorize(db, request_id, self.a)['status'], 'queued')
        self.queue.cancel(key, self.admin)
        with closing(custom.db()) as db:
            correction = custom.authorize(db, request_id, self.a)
            self.assertEqual((correction['status'], correction['cancel']), ('cancelled', 1))

    def test_correction_worker_honours_parent_cancel_if_api_did_not_propagate_it(self):
        from review_custom import Custom
        from unittest.mock import patch

        key = self.submit()['id']
        custom = Custom(self.root)
        request_id = 'direction-' + key
        custom.submit({
            'request_id': request_id,
            'description': 'A person walks forward with slow steps while keeping both arms relaxed at their sides.',
            'duration': 3, 'n': 1, 'prompt_mode': 'exact_english',
        }, self.a['username'], source='semantic_retry')
        with patch('semantic_retarget.cancel_recovery', side_effect=RuntimeError('interrupted response')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted response'):
                self.queue.cancel(key, self.a)
        with closing(custom.db()) as db:
            correction = custom.authorize(db, request_id, self.a)
        self.assertFalse(custom.claim(correction, 'prepare', 'preparing'))
        with closing(custom.db()) as db:
            correction = custom.authorize(db, request_id, self.a)
            self.assertEqual((correction['status'], correction['cancel']), ('cancelled', 1))

    def test_cancel_ignores_same_owner_custom_request_with_a_matching_id(self):
        from review_custom import Custom

        key = self.submit()['id']
        custom = Custom(self.root)
        request_id = 'direction-' + key
        custom.submit({
            'request_id': request_id,
            'description': 'A person walks forward with slow steps while keeping both arms relaxed at their sides.',
            'duration': 3, 'n': 1, 'prompt_mode': 'exact_english',
        }, self.a['username'])
        self.assertEqual(self.queue.cancel(key, self.a)['status'], 'cancelled')
        with closing(custom.db()) as db:
            self.assertEqual(custom.authorize(db, request_id, self.a)['status'], 'queued')

    def test_restart_recovers_a_legacy_cancelled_job_left_queued(self):
        key = self.submit()['id']
        with closing(self.queue.db()) as db, db:
            db.execute("UPDATE jobs SET status='queued',cancel=1 WHERE id=?", (key,))
        restarted = Retarget(self.root)
        self.assertEqual(restarted.get(key, self.a)['status'], 'cancelled')
        self.assertEqual(restarted.status(self.a)['queued'], 0)
    def test_coordinate_conversion(self):
        points=np.zeros((2,52,3));points[:,:,1]=-2;points[:,1]=[1,-1,3]
        template=np.zeros((52,3));template[0]=[0,.8,0]
        result=world_points({'native_keypoints3d':points,'native_transl':np.tile([10,2,20],(2,1))},template)
        np.testing.assert_allclose(result[0,0],[10,-20,2.8])
        np.testing.assert_allclose(result[0,1],[11,-23,3.8])
        points[0,1,0]=np.nan
        with self.assertRaises(ValueError):world_points({'native_keypoints3d':points,'native_transl':np.zeros((2,3))},template)

    def test_http_owner_boundary(self):
        import threading
        import urllib.request
        import urllib.error
        from review_server import Server,Handler
        from review_auth import Auth
        server=Server(('127.0.0.1',0),Handler);server.store=Store();server.store.root=self.root;server.retarget=self.queue
        server.auth=self.auth;sessions={}
        for name,role in [('a','user'),('b','user'),('jfc','admin')]:
            sessions[name]=server.auth.change_password(name,'temporary-test-password','changed-test-password')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(server.server_port)
        def request(name,path,value=None,origin=None):
            headers={'Cookie':'mf_session='+sessions[name],'X-MotionFarm-Request':'1','Content-Type':'application/json','Origin':origin or url}
            req=urllib.request.Request(url+path,headers=headers,data=None if value is None else json.dumps(value).encode())
            with urllib.request.urlopen(req,timeout=3) as response:return response.read()
        try:
            key='1'*32
            request('a','/api/retarget',{'id':key,'motion_id':'m00000001'})
            self.assertEqual(len(json.loads(request('b','/api/retarget'))['items']),0)
            for path,value in [('/api/retarget/cancel',{'id':key}),('/api/retarget/download?id='+key,None),('/api/retarget/preview?id='+key,None)]:
                with self.assertRaises(urllib.error.HTTPError) as err:request('b',path,value)
                self.assertEqual(err.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError) as err:request('a','/api/retarget',{},'http://evil.invalid')
            self.assertEqual(err.exception.code,403)
            cancelled = json.loads(request('jfc','/api/retarget/cancel',{'id':key}))
            self.assertEqual((cancelled['status'], cancelled['message']), ('cancelled', '已取消'))
            finished_key = '2'*32
            request('a','/api/retarget',{'id':finished_key,'motion_id':'m00000001'})
            self.queue.update(finished_key, status='done', message='重定向完成')
            finished = json.loads(request('a','/api/retarget/cancel',{'id':finished_key}))
            self.assertEqual((finished['status'], finished['cancel'], finished['message']), ('done', 0, '重定向完成'))
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_worker_rejects_a_queued_source_that_is_now_quarantined(self):
        from unittest.mock import patch
        from g2h.harness import Store as HarnessStore

        # This is the queue state a worker can inherit after an upgrade.
        motion_id = 'ma_' + 'a' * 32
        request = {'id': '9' * 32, 'motion_id': motion_id, 'robot': ROBOT, 'force_new': True}
        queued = self.queue.submit(request, self.a, Store())
        self.assertEqual(queued['status'], 'queued')
        ledger = HarnessStore(self.root)
        task = ledger.create_task('a', 'old-queued-retarget', 'Review queued source')
        ledger.register_candidate('a', task['id'], motion_id, {'quarantined': True})

        with patch('review_retarget.os.nice'), patch('semantic_retarget.preflight', return_value=False) as preflight:
            self.queue.work()
        job = self.queue.get(queued['id'], self.a)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('隔离候选', job['message'])
        preflight.assert_not_called()
        timing = json.loads((self.queue.folder / queued['id'] / 'timing.json').read_text())
        self.assertFalse(timing['solver_started'])

    def test_managed_asset_retarget_and_export_verified(self):
        from types import SimpleNamespace
        from review_server import Handler
        from g2h.motion_assets import MotionAssets

        sessions={}
        for name,_ in [('a','user'),('b','user'),('jfc','admin')]:
            sessions[name]=self.auth.change_password(name,'temporary-test-password','changed-test-password')
        store=Store();store.root=self.root
        server=SimpleNamespace(auth=self.auth,retarget=self.queue,store=store,origins={'http://localhost'})

        def call(name,path,payload=None):
            handler=object.__new__(Handler)
            handler.server=server
            handler.headers={'Host':'localhost','Cookie':'mf_session='+sessions[name],'X-MotionFarm-Request':'1','Content-Type':'application/json','Origin':'http://localhost'}
            handler.path=path
            handler.client_address=('127.0.0.1',12345)
            handler.send=lambda status,body,**_:setattr(handler,'response',(status,body))
            handler.download=lambda path:handler.send(200,path.read_bytes())
            if payload is None:
                handler.dispatch(write=False)
            else:
                handler.body=lambda **_:payload
                handler.dispatch(write=True)
            return handler.response

        assets=MotionAssets(self.root)
        sample={
            'fps':np.array(30.0,dtype=np.float32),
            'native_keypoints3d':np.zeros((30,52,3),dtype=np.float32),
            'native_transl':np.zeros((30,3),dtype=np.float32),
            'smpl_poses':np.zeros((30,156),dtype=np.float32),
        }
        parent_meta=assets.register('a',sample,description='A person walks forward.',request_id='lib:m00000001')
        edited_meta=assets.register('a',sample,description='A person walks forward and waves.',parent_id=parent_meta['motion_id'],request_id='edit:v1')
        from g2h.harness import Store as HarnessStore
        ledger = HarnessStore(self.root)
        isolated_meta = assets.register('a', sample, description='Isolated source', request_id='edit:isolated')
        task = ledger.create_task('a', 'retarget-review', 'Review a failed motion')['id']
        ledger.register_candidate('a', task, isolated_meta['motion_id'], {'quarantined': True})
        isolated_job_key = '4'*32
        status, rejected = call('a', '/api/retarget', {'id': isolated_job_key,
                                'motion_id': isolated_meta['motion_id'], 'robot': ROBOT, 'force_new': True})
        self.assertEqual(status, 400)
        self.assertIn('隔离候选', rejected['error'])
        with self.assertRaisesRegex(ValueError, '作业不存在'):
            self.queue.get(isolated_job_key, self.a)
        ma_job_key='2'*32
        status,submitted=call('a','/api/retarget',{'id':ma_job_key,'motion_id':edited_meta['motion_id'],'robot':ROBOT})
        self.assertEqual(status,202)
        self.assertEqual(submitted['motion_id'],edited_meta['motion_id'])
        self.assertEqual(submitted['source_info']['parent_id'],parent_meta['motion_id'])
        self.assertEqual(submitted['source_info']['description'],'A person walks forward and waves.')

        # Subsequent edit creates a new asset; prior retarget job keeps its exact source version.
        later_meta=assets.register('a',sample,description='A person sits down.',parent_id=edited_meta['motion_id'],request_id='edit:v2')
        self.assertNotEqual(later_meta['motion_id'],edited_meta['motion_id'])
        status,listing=call('a','/api/retarget')
        self.assertEqual(status,200)
        rechecked=listing['items'][0]
        self.assertEqual(rechecked['motion_id'],edited_meta['motion_id'])
        self.assertEqual(rechecked['source_info']['parent_id'],parent_meta['motion_id'])

        # Cross-account submission on another user's ma_* asset must be rejected.
        status,denied=call('b','/api/retarget',{'id':'3'*32,'motion_id':edited_meta['motion_id'],'robot':ROBOT})
        self.assertEqual(status,403)

        # Export verification flag reflects quality policy.
        folder=self.queue.folder/ma_job_key;folder.mkdir(exist_ok=True)
        (folder/'result.npz').write_bytes(b'x');(folder/'preview.json').write_text('{}')
        (folder/'quality.json').write_text(json.dumps({'version':'robot-quality-v3','dynamics_required':False,'export_allowed':False}))
        self.queue.update(ma_job_key,status='done',done=30,total=30)
        unverified=self.queue.get(ma_job_key,self.a)
        self.assertFalse(unverified['export_verified'])
        self.assertIn('重新验收',unverified.get('export_reason',''))
        (folder/'quality.json').write_text(json.dumps({'version':'robot-quality-v3','dynamics_required':False,'export_allowed':True}))
        import os
        from unittest.mock import patch
        with patch.dict(os.environ,{'KINORA_RETARGET_REQUIRE_DYNAMICS':'0'}):
            verified=self.queue.get(ma_job_key,self.a)
            self.assertTrue(verified['export_verified'])
            download = '/api/retarget/download?id=' + ma_job_key
            preview = '/api/retarget/preview?id=' + ma_job_key
            self.assertEqual(call('a', download), (200, b'x'))
            ledger.register_candidate('a', task, edited_meta['motion_id'], {'quarantined': True})
            status, rejected = call('a', download)
            self.assertEqual(status, 400)
            self.assertIn('隔离候选', rejected['error'])
            self.assertEqual(call('a', preview), (200, {}))
            status, history = call('a', '/api/retarget?history=1')
            self.assertEqual(status, 200)
            stored = next(row for row in history['items'] if row['id'] == ma_job_key)
            self.assertFalse(stored['export_verified'])
            self.assertTrue(stored['source_quarantined'])
            self.assertIn('隔离候选', stored['export_reason'])

    def test_custom_robot_resume_skips_semantic_retry(self):
        from unittest.mock import patch

        job_id = '9' * 32
        exec_json = json.dumps({'parameters': {'root_orientation_weight': 40.0}})
        with closing(self.queue.db()) as db, db:
            db.execute("INSERT INTO jobs(id,owner,motion_id,robot,status,created,updated,execution) VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, 'a', 'm00000001', 'robot_' + '1' * 32, 'queued', 1000.0, 1000.0, exec_json))
        folder = self.queue.folder / job_id
        folder.mkdir(parents=True)
        (folder / 'semantic.json').write_text(json.dumps({'status': 'fail', 'reasons': ['方向不符']}))
        with patch('semantic_retarget.retry_robot') as mocked_retry:
            self.queue.work(slot=0)
            mocked_retry.assert_not_called()

    def test_upright_v3_and_v4_keep_wrists_neutral_by_default(self):
        import sys
        from types import ModuleType
        from unittest.mock import patch
        from retarget_pose_upright_v3 import install_pose_targets

        class DummyRetargeter:
            demo_joints = [
                'Pelvis', 'L_Hip', 'R_Hip', 'Spine1', 'L_Knee', 'R_Knee',
                'Spine2', 'L_Ankle', 'R_Ankle', 'Spine3', 'L_Foot', 'R_Foot',
                'Neck', 'L_Collar', 'R_Collar', 'Head', 'L_Shoulder', 'R_Shoulder',
                'L_Elbow', 'R_Elbow', 'L_Wrist', 'R_Wrist',
            ]
            laplacian_match_links = {}
            robot_model = type('M', (), {'qpos0': np.zeros(10)})()
            def _calc_manipulator_jacobians(self, q, links=None, obj_frame=False):
                pts = {n: np.array([i * 0.1, 0.0, 1.0]) for i, n in enumerate(self.demo_joints)}
                return None, pts, None

        world = np.zeros((4, 22, 3))
        world[:, 1] = [0, 0.1, 0.9]; world[:, 2] = [0, -0.1, 0.9]
        world[:, 12] = [0, 0, 1.4]
        for i in range(1, 22):
            world[:, i] = [i * 0.1, 0.0, 1.0 + i * 0.05]
        world[:, 1] = [0, 0.1, 0.9]; world[:, 2] = [0, -0.1, 0.9]
        world[:, 12] = [0, 0, 1.4]
        poses = np.zeros((4, 52, 3))
        poses[:, 20] = [0.25, 0.0, 0.0]

        with patch.dict(sys.modules, {'cvxpy': sys.modules.get('cvxpy') or ModuleType('cvxpy')}):
            install_pose_targets(DummyRetargeter, world, poses=poses)
        holder = DummyRetargeter()
        holder.initialize_pose_callback(np.zeros(10))
        self.assertEqual(holder._upright_wrist_targets, {})
        src = (Path(__file__).resolve().parent / 'retarget_engine_upright_v4.py').read_text()
        self.assertIn("if 'wrist' in name:lower[str(adr)]=upper[str(adr)]=float(np.clip(0.,lo,hi))", src)
        self.assertIn("No hand/finger retargeting; wrist joints fixed at neutral where present", src)


if __name__=='__main__':unittest.main()
