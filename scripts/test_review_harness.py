"""Task HTTP boundary tests, no models, GPU, accounts or network required."""
from copy import deepcopy
import base64
import io
import json
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import urlsplit
import unittest

from review_access import Forbidden, allowed
from review_harness import handle


class Service:
    def __init__(self):
        self.records = {}
        self.accepted = []
        self.advances = []

    def create(self, user, body):
        task = dict(id='task-1', owner=user['username'], **deepcopy(body))
        self.records[task['id']] = task
        return task

    def detail(self, user, task_id):
        task = self.records[task_id]
        if task['owner'] != user['username']:
            raise PermissionError('任务属于其他账户')
        return task

    def tasks(self, user, query):
        return dict(items=[x for x in self.records.values() if x['owner'] == user['username']])

    def capabilities(self, user):
        return dict(backends=[dict(id='kimodo', available=False)])

    def accept(self, user, task_id, body):
        task = self.detail(user, task_id)
        self.accepted.append((task_id, body['candidate_id']))
        return task

    def advance(self, user, task_id):
        task = self.detail(user, task_id)
        self.advances.append(task_id)
        return task

    def cancel(self, user, task_id):
        return self.detail(user, task_id)

    def feedback(self, user, task_id, body):
        return self.detail(user, task_id)

    def preview(self, user, motion_id):
        return dict(motion_id=motion_id)


class Handler:
    def __init__(self, service, value=None):
        self.server = SimpleNamespace(harness_service=service)
        self.value = value
        self.limit = None

    def body(self, limit):
        self.limit = limit
        return deepcopy(self.value)

    def send(self, status, value):
        return status, value


class Routes(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.alice = dict(username='alice', role='user')
        self.bob = dict(username='bob', role='admin')
        self.value = dict(request_id='request-1', goal='让选中的走路片段更慢', source_motion_id='m00000001',
                          selection=dict(start_frame=10, end_frame=20), budget=dict(max_attempts=3))

    def call(self, path='/api/harness/tasks', body=None, user=None):
        handler = Handler(self.service, body)
        return handle(handler, urlsplit(path), body is not None, user or self.alice)

    def test_owner_comes_from_session_and_cross_account_denied(self):
        self.assertEqual(self.call(body=self.value)[1]['owner'], 'alice')
        self.assertEqual(self.call(user=self.bob)[1]['items'], [])
        with self.assertRaises(Forbidden):
            self.call('/api/harness/tasks/task-1', user=self.bob)
        with self.assertRaises(ValueError):
            self.call(body={**self.value, 'owner': 'bob'})
        with self.assertRaises(Forbidden):
            handle(Handler(self.service), urlsplit('/api/harness/tasks'), False, {})

    def test_frames_and_budget_rejected_before_dispatch(self):
        for update in [dict(selection=dict(start_frame=True, end_frame=20)), dict(selection=dict(start_frame=20, end_frame=20)),
                       dict(selection=dict(start_frame=-1, end_frame=20)), dict(selection=dict(start_frame=0, end_frame=100001)),
                       dict(budget=dict(max_attempts=3,max_cost_usd=float('nan'))), dict(budget=dict(max_attempts=3,max_cost_usd=True)), dict(budget=dict(max_attempts=0)), dict(budget=dict(max_attempts=True)), dict(budget=dict(max_attempts=21)),
                       dict(source_motion_id='../secret'), dict(request_id='x'), dict(goal='  '),
                       dict(selection=dict(start_frame=0, end_frame=2, padding=2)), dict(source_motion_id=None)]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.call(body={**self.value, **update})
        self.assertEqual(self.service.records, {})

    def test_acceptance_requires_explicit_post_and_identifier(self):
        self.call(body=self.value)
        self.assertEqual(self.call('/api/harness/tasks/task-1/accept')[0], 404)
        self.assertEqual(self.service.accepted, [])
        with self.assertRaises(ValueError):
            self.call('/api/harness/tasks/task-1/accept', dict(candidate_id='../bad'))
        self.assertEqual(self.call('/api/harness/tasks/task-1/accept', dict(candidate_id='candidate-1'))[0], 200)
        self.assertEqual(self.service.accepted, [('task-1', 'candidate-1')])
        with self.assertRaises(Forbidden):
            self.call('/api/harness/tasks/task-1/accept', dict(candidate_id='candidate-1'), self.bob)

    def test_mutations_do_not_accept_extra_fields_or_empty_feedback(self):
        self.call(body=self.value)
        for action, body in [('advance', {'tool': 'generate'}), ('cancel', {'owner': 'bob'}), ('feedback', {'message': ''}),
                             ('feedback', {'message': 'x', 'selection': {}}), ('accept', {'candidate_id': 'x', 'force': True})]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.call('/api/harness/tasks/task-1/'+action, body)
        self.assertEqual(self.service.advances, [])

    def test_permission_policy_is_default_deny_and_method_specific(self):
        for path, write in [('/api/harness/tasks', True), ('/api/harness/tasks/task-1', False),
                            ('/api/harness/tasks/task-1/advance', True), ('/api/harness/motions/m00000001/preview', False)]:
            self.assertTrue(allowed(self.alice, path, write))
        for path, write in [('/api/harness/admin', True), ('/api/harness/tasks/task-1/accept', False),
                            ('/api/harness/capabilities', True), ('/api/harness/motions/m00000001/preview', True),
                            ('/api/harness/tasks/../bad', False)]:
            self.assertFalse(allowed(self.alice, path, write))
        self.assertFalse(allowed(dict(username='alice', role='unknown'), '/api/harness/tasks', False))

    def test_body_limit_and_kimodo_placeholder(self):
        handler = Handler(self.service, self.value)
        handle(handler, urlsplit('/api/harness/tasks'), True, self.alice)
        self.assertEqual(handler.limit, 24576)
        result = self.call('/api/harness/capabilities')[1]
        self.assertFalse(result['backends'][0]['available'])
        with self.assertRaises(ValueError):
            self.call('/api/harness/tasks?owner=bob')


class RealServiceRoutes(unittest.TestCase):
    """Exercise real SQLite, assets, splice and task state through the routes."""
    def setUp(self):
        import numpy as np
        from harness_service import HarnessService
        self.np = np
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = HarnessService(self.root)
        self.alice = dict(username='alice', role='user')
        self.bob = dict(username='bob', role='admin')
        points = np.zeros((12, 52, 3), dtype=np.float32)
        points[:, :, 1] = np.linspace(0.1, 1.5, 52)
        self.arrays = dict(fps=np.asarray(30), native_keypoints3d=points,
                           native_transl=np.zeros((12, 3), dtype=np.float32),
                           smpl_poses=np.zeros((12, 156), dtype=np.float32))
        self.asset = self.service.assets.register('alice', self.arrays, description='Test walk')
        self.value = dict(request_id='real-request-1', goal='修改选中的片段，保持其他部分',
                          source_motion_id=self.asset['motion_id'], selection=dict(start_frame=4, end_frame=8),
                          budget=dict(max_attempts=3, max_cost_usd=0))

    def call(self, path='/api/harness/tasks', body=None, user=None):
        return handle(Handler(self.service, body), urlsplit(path), body is not None, user or self.alice)

    def upload(self):
        np = self.np
        arrays = {key: value[4:8].copy() if value.ndim and value.shape[0] == 12 else value.copy()
                  for key, value in self.arrays.items()}
        arrays['native_keypoints3d'][:, 30, 0] = 0.05
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays)
        return dict(request_id='patch-request-1', npz_base64=base64.b64encode(buffer.getvalue()).decode())

    def test_real_splice_is_immutable_idempotent_and_user_accepted(self):
        task = self.call(body=self.value)[1]
        path = '/api/harness/tasks/' + task['id']
        self.assertEqual(self.call(body=self.value)[1]['id'], task['id'])
        result = self.call(path + '/import-patch', self.upload())[1]
        self.assertEqual(len(result['candidates']), 1)
        self.assertNotEqual(result['status'], 'accepted')
        replay = self.call(path + '/import-patch', self.upload())[1]
        self.assertEqual(len(replay['candidates']), 1)
        candidate = replay['candidates'][0]
        arrays, meta = self.service.assets.load('alice', candidate['motion_id'])
        self.assertEqual(meta['parent_id'], self.asset['motion_id'])
        for key, value in self.arrays.items():
            if value.ndim and value.shape[0] == 12:
                self.np.testing.assert_array_equal(arrays[key][:4], value[:4])
                self.np.testing.assert_array_equal(arrays[key][8:], value[8:])
        original, _ = self.service.assets.load('alice', self.asset['motion_id'])
        self.np.testing.assert_array_equal(original['native_keypoints3d'], self.arrays['native_keypoints3d'])
        accepted = self.call(path + '/accept', dict(candidate_id=candidate['id']))[1]
        self.assertEqual(accepted['status'], 'accepted')

    def test_real_actual_frame_limit_account_scope_and_disabled_kimodo(self):
        with self.assertRaises(ValueError):
            self.call(body={**self.value, 'selection': dict(start_frame=4, end_frame=13)})
        with self.assertRaises((ValueError, KeyError, Forbidden, PermissionError)):
            self.call(body=self.value, user=self.bob)
        task = self.call(body=self.value)[1]
        self.assertEqual(self.call(user=self.bob)[1]['items'], [])
        with self.assertRaises((KeyError, Forbidden)):
            self.call('/api/harness/tasks/' + task['id'], user=self.bob)
        caps = self.call('/api/harness/capabilities')[1]
        kimodo = next(x for x in caps['backends'] if x['id'] == 'kimodo')
        self.assertFalse(kimodo['available'])
        with self.assertRaises(ValueError):
            self.call('/api/harness/tasks/' + task['id'] + '/import-patch',
                      dict(request_id='patch-request-2', npz_base64='not-base64'))

    def test_real_preview_uses_installed_rig_and_reports_unknown_semantics(self):
        np = self.np
        rig = self.root / 'third_party/HY-Motion-1.0/retargeting/smplh'
        rig.mkdir(parents=True)
        np.asarray([-1] + list(range(51)), dtype=np.int32).tofile(rig / 'kintree.bin')
        np.zeros((52, 3), dtype=np.float32).tofile(rig / 'j_template.bin')
        (rig / 'joint_names.json').write_text(json.dumps(['joint' + str(i) for i in range(52)]))
        status, preview = self.call('/api/harness/motions/' + self.asset['motion_id'] + '/preview')
        self.assertEqual(status, 200)
        self.assertEqual(preview['frames'], 12)
        self.assertEqual(len(preview['joints'][0]), 52)
        self.assertEqual(preview['quality']['semantic'], 'unknown')


class BrowserSessionRoutes(unittest.TestCase):
    """Use the existing HTTP/account fixture to exercise the real entry point."""
    def setUp(self):
        from test_review_access import AccessTests
        self.fixture = AccessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_stopping_linked_scenes_stops_every_owned_queue_job(self):
        from contextlib import closing
        from unittest.mock import patch
        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom

        fixture = self.fixture
        root = fixture.server.store.root
        separate = fixture.job()[1]['request_id']
        cases = [
            ('paired-scene', dict(request_id='stop-paired-scene', kind='handshake', duration=3)),
            ('multi-scene', dict(request_id='stop-multi-scene', duration=3, actors=[{'id': 'R1'}, {'id': 'R2'}])),
        ]
        with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL, 'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            for endpoint, body in cases:
                with self.subTest(endpoint=endpoint):
                    code, group = fixture.call('/api/harness/' + endpoint, body=body)
                    self.assertEqual(code, 202, group)
                    members = group.get('actors_group') or [group, group['pair']]
                    members = [member.get('id') or member['task_id'] for member in members]
                    route = '/api/harness/tasks/' + group['id']
                    self.assertEqual(fixture.call(route + '/cancel', 'bob', {})[0], 400)
                    status, cancelled = fixture.call(route + '/cancel', body={})
                    self.assertEqual(status, 200)
                    if endpoint == 'paired-scene':
                        self.assertEqual(cancelled.get('pair', {}).get('status'), 'cancelled')
                    else:
                        self.assertEqual([item['status'] for item in cancelled.get('actors_group', [])],
                                         ['cancelled'] * len(members))
                    restarted = HarnessService(root)
                    for actor in members:
                        detail = restarted.store.get_task('alice', actor)
                        self.assertEqual(detail['status'], 'cancelled')
                        job = detail['operations'][0]['handle']['request_id']
                        with closing(Custom(root).db()) as db:
                            row = db.execute('SELECT status,cancel FROM requests WHERE id=?', (job,)).fetchone()
                            self.assertEqual(tuple(row), ('cancelled', 1))
                    self.assertEqual(fixture.call(route + '/cancel', body={})[0], 200)
            from test_human_interaction import fixture as scene
            service = HarnessService(root, legacy_store=fixture.server.store)
            user = {'username': 'alice', 'role': 'user'}
            goal = 'A person stands near a box before turning to the left.'
            task = service.store.create_task('alice', 'stop-before-queue', goal)
            generator = service._registry(user).get('generate_motion')
            lease = service.store.claim('alice', task['id'], 'paused-before-queue')
            args = {'text': goal, 'duration': 1, 'scene': scene()}
            operation = service.store.reserve_operation(lease, generator, args)
            route = '/api/harness/tasks/' + task['id'] + '/cancel'
            self.assertEqual(fixture.call(route, body={})[0], 200)
            with self.assertRaises(InterruptedError):
                generator.execute({'owner': 'alice', 'task': task}, args, operation['id'])
            service.store.release(lease)
            with closing(Custom(root).db()) as db:
                self.assertIsNone(db.execute('SELECT id FROM requests WHERE id=?',
                                             ('hgen_' + operation['id'],)).fetchone())
        with closing(Custom(root).db()) as db:
            row = db.execute('SELECT status,cancel FROM requests WHERE id=?', (separate,)).fetchone()
            self.assertEqual(tuple(row), ('queued', 0))

    def test_npz_download_requires_session_and_asset_owner(self):
        from harness_service import HarnessService
        from test_motion_tools import motion
        fixture = self.fixture
        svc = HarnessService(fixture.server.store.root)
        asset = svc.assets.register('alice', motion(aliases=False))
        path = '/api/harness/motions/' + asset['motion_id'] + '/download'
        for who, expected in [('unknown', 401), ('bob', 403)]:
            request = urllib.request.Request(fixture.url + path, headers={'Cookie':'mf_session='+fixture.tokens.get(who,'')})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
        missing = '/api/harness/motions/ma_' + '9'*32 + '/download'
        self.assertEqual(fixture.call(path, 'bob'), fixture.call(missing, 'bob'))
        request = urllib.request.Request(fixture.url + path, headers={'Cookie':'mf_session='+fixture.tokens['alice']})
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read()
            self.assertIn('.npz', response.headers['Content-Disposition'])
        import numpy as np
        with np.load(io.BytesIO(raw), allow_pickle=False) as arrays:
            self.assertIn('native_transl', arrays.files)

    def test_full_scene_queue_records_rejected_actors_after_service_restart(self):
        from contextlib import closing
        from unittest.mock import patch
        from g2h.generation_backend import KIMODO_MODEL
        from review_custom import Custom

        fixture = self.fixture
        unrelated = [fixture.job(f'unrelated-{index}')[1]['request_id'] for index in range(4)]
        endpoint = '/api/harness/multi-scene'
        body = dict(request_id='queue-limit-scene', duration=3, actors=[{'id': f'R{i}'} for i in range(1, 4)])
        with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL, 'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            status, first = fixture.call(endpoint, body=body)
            self.assertEqual(status, 202, first)
            route = '/api/harness/tasks/' + first['id']
            status, restarted = fixture.call(route + '/scene-status', body={})
            self.assertEqual(status, 200, restarted)
            active, *rejected = [actor['operations'][0] for actor in restarted['actors_group']]
            self.assertEqual(active['status'], 'waiting')
            for operation in rejected:
                self.assertEqual(operation['status'], 'failed')
                self.assertIn('每用户最多 5 个未完成作业', operation['result']['message'])
            status, repeat = fixture.call(endpoint, body=body)
            self.assertEqual(status, 202, repeat)
            self.assertEqual([actor['task_id'] for actor in repeat['actors_group']],
                             [actor['task_id'] for actor in first['actors_group']])
            self.assertEqual(fixture.call(route + '/cancel', body={})[0], 200)
        with closing(Custom(fixture.server.store.root).db()) as db:
            jobs = dict(db.execute('SELECT id,status FROM requests'))
        self.assertEqual(jobs, {**dict.fromkeys(unrelated, 'queued'), active['handle']['request_id']: 'cancelled'})

    def test_object_response_replay_checks_full_geometry_and_legacy_output(self):
        from copy import deepcopy
        from unittest.mock import patch
        from g2h.object_response import attach_object_to_motion
        from harness_service import HarnessService
        from test_multi_actor_and_response import _standing_walk_motion

        fixture = self.fixture
        service = HarnessService(fixture.server.store.root)
        motion, rest, parents = _standing_walk_motion(walk_z=.6, raise_arms_window=(28, 72))
        source = service.assets.register('alice', dict(motion, rest_joints=rest, parents=parents))['motion_id']
        objects = [
            {'id': 'parcel', 'center_m': [0, .25, 0], 'size_m': [.5, .5, .44], 'heading_deg': 0},
            {'id': 'obstacle', 'center_m': [3, .25, 3], 'size_m': [.5, .5, .5], 'heading_deg': 0},
        ]
        attachment = {'object_id': 'parcel', 'start_frame': 28, 'end_frame': 72, 'auto_place': True, 'mode': 'carry'}
        path = '/api/harness/object-response'
        request = dict(request_id='object-geometry-1', source_motion_id=source, objects=objects, attachment=attachment)
        status, first = fixture.call(path, body=request)
        self.assertEqual(status, 200, first)
        with patch('g2h.object_response.attach_object_to_motion', side_effect=AssertionError('recomputed a new receipt')):
            self.assertEqual(fixture.call(path, body=request), (200, first))
        for index, prop, value in [(0, 'heading_deg', 45), (1, 'center_m', [4, .25, 3]),
                                   (1, 'trajectory', [{'frame': 0, 'center_m': [3, .25, 3]},
                                                      {'frame': 20, 'center_m': [4, .25, 3]}])]:
            with self.subTest(property=prop):
                changed = deepcopy(request)
                changed['objects'][index][prop] = value
                status, conflict = fixture.call(path, body=changed)
                self.assertEqual(status, 400)
                self.assertIn('请求编号已用于其他物体响应参数', conflict['error'])

        legacy = dict(request, request_id='legacy-object-geometry')
        processed, _, report = attach_object_to_motion(motion, objects, attachment, rest, parents)
        saved = service.assets.register('alice', processed, request_id='objresp:' + legacy['request_id'],
            description=f'物体响应 {source}',
            provenance={'source_motion_id': source, 'human_interaction': report, 'object_response': attachment})
        status, replayed = fixture.call(path, body=legacy)
        self.assertEqual(status, 200, replayed)
        self.assertEqual(replayed['candidates'][0]['motion_id'], saved['motion_id'])
        changed = deepcopy(legacy)
        changed['objects'][1]['center_m'][0] = 7
        self.assertEqual(fixture.call(path, body=changed)[0], 400)

    def test_managed_preview_distinguishes_unavailable_assets_from_storage_errors(self):
        from harness_service import HarnessService
        from test_motion_tools import motion

        fixture = self.fixture
        service = HarnessService(fixture.server.store.root)
        asset = service.assets.register('alice', motion(aliases=False))
        prefix = '/api/harness/motions/'
        path = prefix + asset['motion_id'] + '/preview'
        hidden = fixture.call(path, 'bob')
        absent = fixture.call(prefix + 'ma_' + 'f' * 32 + '/preview', 'bob')
        self.assertEqual(hidden, absent)
        self.assertEqual(hidden[0], 403)

        service.assets.artifact_path('alice', asset['motion_id']).unlink()
        status, _ = fixture.call(path, 'alice')
        self.assertEqual(status, 503)

    def test_managed_preview_recovers_after_corrupt_payload_is_restored(self):
        from harness_service import HarnessService
        from test_personal_assets import motion_fixture

        fixture = self.fixture
        service = HarnessService(fixture.server.store.root)
        arrays, _ = motion_fixture()
        asset = service.assets.register('alice', arrays)
        path = '/api/harness/motions/' + asset['motion_id'] + '/preview'
        status, original = fixture.call(path)
        self.assertEqual(status, 200)
        self.assertEqual(original['frames'], 30)

        artifact = service.assets.artifact_path('alice', asset['motion_id'])
        raw = artifact.read_bytes()
        artifact.write_bytes(raw[:32])
        status, failure = fixture.call(path)
        self.assertEqual(status, 503)
        self.assertIn('动作文件损坏', failure['error'])
        self.assertIn('恢复后重试', failure['error'])
        self.assertEqual(service.assets.get('alice', asset['motion_id'])['sha256'], asset['sha256'])

        artifact.write_bytes(raw)
        self.assertEqual(fixture.call(path), (200, original))

    def test_session_csrf_ownership_and_placeholder_at_http_boundary(self):
        fixture = self.fixture
        value = dict(request_id='browser-request-1', goal='建立一个待处理的动作任务',
                     budget=dict(max_attempts=2, max_cost_usd=0))
        self.assertEqual(fixture.call('/api/harness/tasks', 'unknown')[0], 401)
        status, task = fixture.call('/api/harness/tasks', body=value)
        self.assertEqual(status, 201)
        self.assertEqual(task['owner'], 'alice')
        self.assertEqual(fixture.call('/api/harness/tasks', body=value)[1]['id'], task['id'])
        status, tasks = fixture.call('/api/harness/tasks', 'bob')
        self.assertEqual(status, 200)
        self.assertEqual(tasks['items'], [])
        self.assertIn(fixture.call('/api/harness/tasks/' + task['id'], 'bob')[0], (400, 403, 404))
        status, caps = fixture.call('/api/harness/capabilities')
        self.assertEqual(status, 200)
        self.assertFalse(next(x for x in caps['backends'] if x['id'] == 'kimodo')['available'])
        request = urllib.request.Request(fixture.url + '/api/harness/tasks',
            data=json.dumps({**value, 'request_id': 'no-csrf-header'}).encode(),
            headers={'Cookie': 'mf_session=' + fixture.tokens['alice'], 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(error.exception.code, 403)

    def test_quota_exceeded_maps_to_429_while_other_permission_errors_remain_403(self):
        from unittest.mock import patch
        from g2h.api_usage import QuotaExceeded as ApiQuotaExceeded
        from g2h.resource_quota import QuotaExceeded as ResourceQuotaExceeded

        fixture = self.fixture
        for exc, expected in (
            (ApiQuotaExceeded('api quota reached'), 429),
            (ResourceQuotaExceeded('storage quota reached'), 429),
            (PermissionError('account disabled'), 403),
        ):
            with self.subTest(exc=type(exc).__name__), patch.object(
                fixture.server.harness_service if hasattr(fixture.server, 'harness_service') else __import__('harness_service').HarnessService,
                'capabilities',
                side_effect=exc,
            ):
                status, body = fixture.call('/api/harness/capabilities')
                self.assertEqual(status, expected, body)

    def test_soft_warnings_publish_candidates_across_edit_scene_and_splice(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from g2h import cubic_splice
        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from review_motion_edits import MotionEdits
        from test_human_interaction import fixture as scene
        from test_personal_assets import motion_fixture

        fixture = self.fixture
        root = fixture.server.store.root
        service = HarnessService(root, legacy_store=fixture.server.store)
        user = {'username': 'alice', 'role': 'user'}
        arrays, _ = motion_fixture()
        a1 = service.assets.register('alice', arrays, description='clip A')
        a2 = service.assets.register('alice', arrays, description='clip B')

        # 1) splice with soft seam warning (report['passed'] == False) still registers candidate.
        soft_report = {'passed': False, 'failures': ['过渡速度略快'], 'selection': {'start_frame': 20, 'end_frame': 40}}
        xform = {'turn_right_deg': 0, 'offset_x_m': 0, 'offset_z_m': 0}
        with patch.object(cubic_splice, 'join', return_value=(arrays, soft_report)):
            task = service.splice(user, {
                'request_id': 'splice-soft-warn-1',
                'clips': [
                    {'motion_id': a1['motion_id'], 'start_frame': 0, 'end_frame': 30, 'transform': dict(xform)},
                    {'motion_id': a2['motion_id'], 'start_frame': 0, 'end_frame': 30, 'transform': dict(xform)},
                ],
                'transition_frames': 18,
            })
        self.assertEqual(len(task['candidates']), 1)
        spliced_meta = service.assets.get('alice', task['candidates'][0]['motion_id'])
        self.assertFalse(spliced_meta['provenance']['join']['passed'])

        # 2) reconcile_edit with soft seam warning returns succeeded + candidate + unknown boundary status.
        queue = MotionEdits(root)
        edit_task = service.store.create_task('alice', 'edit-soft-warn', 'raise arm', source_motion_id=a1['motion_id'],
                                            selection={'start_frame': 5, 'end_frame': 20})
        op_id = 'op_soft_edit_1'
        with patch('review_motion_edits.kimodo_edit_readiness', return_value=True):
            queue.submit('alice', edit_task['id'], 'hedit_' + op_id, a1['motion_id'],
                         {'start_frame': 5, 'end_frame': 20}, 'raise left arm gently')
        edit_report = {'passed': False, 'version': 1, 'failures': ['接缝位移略大']}
        queue._complete('hedit_' + op_id, a2, edit_report)
        edit_tool = service._registry(user).get('edit_motion')
        rec_edit = edit_tool.reconcile({'owner': 'alice', 'task': edit_task}, {'prompt': 'raise left arm gently'}, op_id, {'kind': 'local_edit'})
        self.assertEqual(rec_edit['state'], 'succeeded')
        self.assertEqual(len(rec_edit['candidates']), 1)
        self.assertEqual(rec_edit['evidence'][0]['status'], 'unknown')

        # 3) reconcile_generation with human_interaction.passed == False still returns succeeded + warning message.
        custom = Custom(root)
        gen_op = 'op_soft_scene_1'
        with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL, 'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            custom.submit({'request_id': 'hgen_' + gen_op, 'description': 'A person walks calmly to a box and stops.',
                           'prompt_mode': 'exact_english', 'n': 1, 'duration': 1, 'scene': scene()}, 'alice')
        custom.update('hgen_' + gen_op, status='completed')
        fake_rec = {'motion': {**arrays, 'scene_spec_json': __import__('g2h.human_interaction', fromlist=['encode']).encode(scene())},
                    'quality': {'human_interaction': {'passed': False, 'failures': ['轻微穿模']}}}
        with patch.object(Custom, 'results', return_value={'items': [{'motion_id': 'm00000001'}]}), \
             patch('g2h.farm_index.read_motion', return_value=fake_rec):
            gen_tool = service._registry(user).get('generate_motion')
            rec_gen = gen_tool.reconcile({'owner': 'alice', 'task': edit_task}, {'text': 'A person walks calmly to a box and stops.', 'duration': 1, 'scene': scene()}, gen_op, {'request_id': 'hgen_' + gen_op})
        self.assertEqual(rec_gen['state'], 'succeeded')
        self.assertEqual(len(rec_gen['candidates']), 1)
        self.assertIn('预览确认', rec_gen['result']['message'])


if __name__ == '__main__':
    unittest.main()
