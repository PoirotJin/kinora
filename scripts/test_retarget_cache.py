import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import closing
from review_retarget import Retarget, ROBOT
from retarget_cache import seal, effective

class Store:
    def detail(self, mid): return {'location': True}

class Tests(unittest.TestCase):
    def setUp(self):
        policy = patch.dict(os.environ, {'KINORA_RETARGET_REQUIRE_DYNAMICS': '0'})
        policy.start()
        self.addCleanup(policy.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        from review_auth import Auth
        auth = Auth(self.root/'review')
        for name in ('a','b'): auth.create(name, 'temporary-test-password', 'user')
        self.queue = Retarget(self.root); self.source = 'source-v1'
        self.patcher = patch('retarget_cache.identity', side_effect=self.identity); self.patcher.start()
    def tearDown(self): self.patcher.stop(); self.tmp.cleanup()
    def identity(self, root, mid, robot, execution, detail=None):
        body = dict(source=self.source, mid=mid, robot=robot, execution=effective(execution))
        return dict(body, key=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest())
    def submit(self, n, owner='a', **extra):
        return self.queue.submit(dict(id=f'{n:032x}', motion_id='m00000001', robot=ROBOT, **extra), {'username':owner,'role':'user'}, Store())
    def seed(self, quality=None):
        row = self.submit(1); folder = self.queue.folder/row['id']; folder.mkdir()
        (folder/'result.npz').write_bytes(b'immutable-test-artifact')
        (folder/'preview.json').write_text('{}')
        if quality is None:
            quality = dict(version='robot-quality-v3', dynamics_required=False, export_allowed=True)
        (folder/'quality.json').write_text(json.dumps(quality))
        seal(folder, self.identity(self.root,row['motion_id'],ROBOT,row['execution']))
        self.queue.update(row['id'],status='done',done=100,total=100)
        return folder
    def test_reuses_without_copying_and_keeps_request_id(self):
        old=self.seed(); new=self.submit(2)
        self.assertEqual(new['status'],'done'); self.assertEqual(new['id'],f'{2:032x}')
        self.assertEqual(new['reused_from'],f'{1:032x}')
        self.assertEqual((old/'result.npz').stat().st_ino,(self.queue.folder/new['id']/'result.npz').stat().st_ino)
        self.assertEqual(self.submit(2)['reused_from'],new['reused_from'])
    def test_other_owner_cannot_reuse(self):
        self.seed(); self.assertEqual(self.submit(2,'b')['status'],'queued')
    def test_parameter_changes_require_new_result(self):
        self.seed(); row=self.submit(2,parameters={'smooth_weight':1});self.assertEqual(row['status'],'queued')
        self.assertEqual(json.loads(row['execution'])['parameters']['smooth_weight'],1)
    def test_explicit_defaults_match(self):
        self.seed(); self.assertEqual(self.submit(2,parameters={'smooth_weight':.2})['status'],'done')
    def test_source_change_requires_new_result(self):
        self.seed(); self.source='source-v2';self.assertEqual(self.submit(2)['status'],'queued')
    def test_corrupt_result_misses(self):
        old=self.seed();(old/'result.npz').write_bytes(b'broken');self.assertEqual(self.submit(2)['status'],'queued')
    def test_corrupt_shared_result_revokes_export_and_replay_keeps_the_job_id(self):
        source = self.seed()
        reused = self.submit(2)
        owner = {'username': 'a', 'role': 'user'}
        ids = [f'{index:032x}' for index in (1, 2)]
        for job_id in ids:
            self.assertTrue(self.queue.get(job_id, owner)['export_verified'])
        (source / 'result.npz').write_bytes(b'corrupt cached result')
        rows = {row['id']: row for row in self.queue.status(owner)['items']}
        for job_id in ids:
            self.assertFalse(rows[job_id]['export_verified'])
            self.assertTrue(rows[job_id]['result_corrupt'])
            with self.assertRaisesRegex(ValueError, '重定向结果完整性校验失败'):
                self.queue.result(job_id, owner, 'result.npz', require_dynamic=True)
        again = self.submit(2)
        self.assertEqual(again['id'], reused['id'])
        self.assertTrue(again['result_corrupt'])
        self.assertEqual(self.submit(3)['status'], 'queued')
    def test_missing_preview_misses(self):
        old=self.seed();(old/'preview.json').unlink();self.assertEqual(self.submit(2)['status'],'queued')

    def test_changed_sealed_preview_rejects_original_and_reused_jobs(self):
        original = self.seed()
        reused = self.submit(2)
        owner = {'username': 'a', 'role': 'user'}
        self.assertEqual(self.queue.result(original.name, owner, 'preview.json').read_text(), '{}')
        (original / 'preview.json').write_text('{"frames": 999}')

        rows = {row['id']: row for row in self.queue.status(owner)['items']}
        for job_id in (original.name, reused['id']):
            with self.subTest(job_id=job_id):
                self.assertTrue(rows[job_id]['preview_corrupt'])
                self.assertIsNone(rows[job_id]['result_version'])
                self.assertTrue(rows[job_id]['export_verified'])
                with self.assertRaisesRegex(ValueError, '重定向骨架预览完整性校验失败'):
                    self.queue.result(job_id, owner, 'preview.json')
                self.assertEqual(self.queue.result(job_id, owner, 'result.npz', require_dynamic=True).read_bytes(),
                                 b'immutable-test-artifact')

    def test_failed_job_not_reused(self):
        self.seed();self.queue.update(f'{1:032x}',status='failed');self.assertEqual(self.submit(2)['status'],'queued')
    def test_quality_failure_misses(self):
        self.seed(dict(version='robot-quality-v3', dynamics_required=False, export_allowed=False))
        self.assertEqual(self.submit(2)['status'],'queued')
    def test_unversioned_quality_misses_even_if_sealed(self):
        self.seed(dict(export_allowed=True))
        self.assertEqual(self.submit(2)['status'],'queued')

    def test_dispatch_notice_only_for_visible_queued_jobs(self):
        self.submit(1)
        alice = {'username':'a', 'role':'user'}
        bob = {'username':'b', 'role':'user'}
        self.assertIn('调度器离线', self.queue.status(alice)['dispatch_notice'])
        self.assertIsNone(self.queue.status(bob)['dispatch_notice'])
        (self.queue.folder/'scheduler-status.json').write_text(json.dumps(
            dict(sampled_at=100, low_memory=True)))
        with patch('retarget_scheduler.time.time', return_value=100):
            self.assertIn('可用内存不足', self.queue.status(alice)['dispatch_notice'])

    def test_download_routes_enforce_current_quality_policy_and_keep_historical_files(self):
        from gadget_keys import Keys
        from review_auth import Auth
        from review_server import Handler

        folder = self.seed()
        artifact = (folder/'result.npz').read_bytes()
        auth = Auth(self.root/'review')
        session = auth.change_password('a', 'temporary-test-password', 'current-test-password')
        token = Keys(self.root).create('a', {'scopes': ['read']})['key']
        server = SimpleNamespace(auth=auth, retarget=self.queue, store=SimpleNamespace(root=self.root))

        def get(path):
            handler = object.__new__(Handler)
            handler.server = server
            handler.headers = {'Cookie': 'mf_session='+session, 'Authorization': 'Bearer '+token}
            handler.path = path+'?id='+folder.name
            handler.send = lambda status, body: setattr(handler, 'response', (status, body))
            handler.download = lambda file: handler.send(200, file.read_bytes())
            handler.dispatch()
            return handler.response

        downloads = ('/api/retarget/download', '/api/gadget/v1/download')
        for path in downloads:
            with self.subTest(path=path, dynamics_required=False):
                self.assertEqual(get(path), (200, artifact))

        with patch.dict(os.environ, {'KINORA_RETARGET_REQUIRE_DYNAMICS': '1'}):
            for path in downloads:
                with self.subTest(path=path, dynamics_required=True):
                    status, body = get(path)
                    self.assertEqual(status, 400)
                    self.assertIn('此历史轨迹尚未通过当前验收', body['error'])
                    self.assertIn('请重新验收', body['error'])
            self.assertEqual(get('/api/retarget/preview'), (200, {}))
            self.assertEqual((folder/'result.npz').read_bytes(), artifact)

            proof = dict(version='robot-quality-v3', dynamics_required=True, export_allowed=True,
                         dynamics=dict(version='robot-dynamics-v1', passed=True, policy_sha256='current'))
            (folder/'quality.json').write_text(json.dumps(proof))
            manifest = json.loads((folder/'reuse-manifest.json').read_text())
            seal(folder, manifest['context'])
            for path in downloads:
                with self.subTest(path=path):
                    with patch('robot_dynamics.policy_fingerprint', return_value='current'):
                        self.assertEqual(get(path), (200, artifact))
                    with patch('robot_dynamics.policy_fingerprint', return_value='changed'):
                        self.assertEqual(get(path)[0], 400)
    def test_old_algorithm_identity_is_not_reused(self):
        old=self.seed();p=old/'reuse-manifest.json';b=json.loads(p.read_text());b['context']['key']='old-engine';p.write_text(json.dumps(b))
        self.assertEqual(self.submit(2)['status'],'queued')
    def test_full_queue_still_allows_cache_hit(self):
        self.seed()
        for n in range(3,8):self.submit(n,parameters={'smooth_weight':1},force_new=True)
        self.assertEqual(self.submit(2)['status'],'done')
    def test_force_new_keeps_old_result(self):
        old=self.seed();row=self.submit(2,force_new=True)
        self.assertEqual(row['status'],'queued');self.assertFalse(row['reused_from'])
        self.assertEqual((old/'result.npz').read_bytes(),b'immutable-test-artifact')
        self.assertEqual(self.submit(2,force_new=True)['id'],row['id'])
    def test_different_requested_parameters_retry_is_idempotent(self):
        self.seed();a=self.submit(2,parameters={'smooth_weight':1});b=self.submit(2,parameters={'smooth_weight':1})
        self.assertEqual(a['id'],b['id'])
    def test_recorded_version_is_displayed(self):
        from retarget_cache import version_info
        old=self.seed();(old/'preview.json').write_text(json.dumps({'metadata':{'engine':{'name':'old-engine','sha256':'abc123'},'execution':{'parameters':{'smooth_weight':.2},'schema_version':'v1','preset':{'name':'My preset','version':3}}}}))
        v=version_info(old);self.assertEqual(v['algorithm'],'old-engine');self.assertEqual(v['parameter_version'],'方案 My preset v3')
        self.assertEqual(v['parameters'],{'smooth_weight':.2})
    def test_bad_preview_metadata_does_not_break_history_or_verified_download(self):
        old = self.seed()
        healthy = self.submit(2)
        preview = old / 'preview.json'
        # Replace only the older job's preview, leaving the reused job's hard link intact.
        replacement = old / 'broken-preview.json'
        self.assertEqual(healthy['status'], 'done')
        owner = {'username': 'a', 'role': 'user'}
        for body in ('[]', '{"metadata":null}', '{"metadata":{"execution":[]}}',
                     '{"metadata":{"execution":{"parameters":{"smooth_weight":NaN}}}}'):
            with self.subTest(body=body):
                replacement.write_text(body)
                replacement.replace(preview)
                rows = {job['id']: job for job in self.queue.status(owner, {'history': ['1']})['items']}
                damaged = rows[old.name]
                self.assertIsNone(damaged['result_version'])
                self.assertTrue(damaged['export_verified'])
                self.assertIsNotNone(rows[healthy['id']]['result_version'])
                self.assertEqual(self.queue.result(old.name, owner, 'result.npz', require_dynamic=True).read_bytes(),
                                 b'immutable-test-artifact')

    def test_unsealed_result_is_historical_only(self):
        old=self.seed();(old/'reuse-manifest.json').unlink()
        self.assertEqual(self.submit(2)['status'],'queued')
        self.assertTrue((old/'result.npz').exists())
    def test_identity_failure_does_not_reuse(self):
        self.seed()
        with patch('retarget_cache.identity',side_effect=ValueError('not identifiable')):
            self.assertEqual(self.submit(2)['status'],'queued')

    def test_progress_is_throttled_but_terminal_update_is_immediate(self):
        row=self.submit(1);key=row['id']
        with patch('review_retarget.time.monotonic',return_value=100):
            self.queue.update(key,done=5,total=100,message='first')
            self.queue.update(key,done=10,total=100,message='intermediate')
            self.assertEqual(self.queue.get(key,{'username':'a','role':'user'})['done'],5)
            self.queue.update(key,done=100,total=100,message='complete')
            self.assertEqual(self.queue.get(key,{'username':'a','role':'user'})['done'],100)

    def test_cache_chain_keeps_original_provenance(self):
        self.seed();self.submit(2);self.assertEqual(self.submit(3)['reused_from'],f'{1:032x}')

if __name__=='__main__':unittest.main()
