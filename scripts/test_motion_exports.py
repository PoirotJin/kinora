from http.client import IncompleteRead
import io,json,re,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
import urllib.request
from unittest.mock import patch
import numpy as np
from scipy.spatial.transform import Rotation
from motion_exports import bvh,export_batch,export_one
from review_access import Forbidden
from test_kimodo_edit import fixture


def replace_fixture_record_with_undecodable_bytes(store, motion_id):
    from contextlib import closing
    import hashlib
    import sqlite3

    info = store.detail(motion_id)
    location = info['location']
    path = Path(location['file'])
    payload = b'not a serialized motion record'
    path.write_bytes(payload)
    with closing(sqlite3.connect(path.parent / 'index.sqlite3')) as db, db:
        db.execute('UPDATE records SET bytes=?,sha256=? WHERE id=?',
                   (len(payload), hashlib.sha256(payload).hexdigest(), info['task_id']))
        db.execute('UPDATE shards SET bytes=? WHERE number=?', (len(payload), location['shard']))


class Exports(unittest.TestCase):
    def test_undecodable_indexed_record_does_not_abort_other_exports(self):
        from g2h.cold_storage import package
        from harness_service import HarnessService
        from test_review_access import AccessTests

        access = AccessTests()
        access.setUp()
        self.addCleanup(access.tearDown)
        service = access.server.harness_service = HarnessService(access.root, legacy_store=access.store)
        broken = access.fixture.mid
        access.store.save(broken, access.fixture.value)
        replace_fixture_record_with_undecodable_bytes(access.store, broken)
        arrays, rest, parents = fixture(8)
        valid = service.assets.register('alice', dict(arrays, rest_joints=rest, parents=parents))['id']
        raw = service.assets.artifact_path('alice', valid).read_bytes()
        headers = {'Cookie': 'mf_session=' + access.tokens['alice']}
        for tier in ('hot', 'cold'):
            if tier == 'cold':
                package(access.root, 'walking', 0, min_age_days=0)
            with self.subTest(tier=tier):
                for extension in ('npz', 'bvh'):
                    route = f'/api/harness/export?format={extension}&ids={broken},{valid}'
                    request = urllib.request.Request(access.url + route, headers=headers)
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        with zipfile.ZipFile(io.BytesIO(response.read())) as bundle:
                            self.assertNotIn(broken + '.' + extension, bundle.namelist())
                            valid_bytes = bundle.read(valid + '.' + extension)
                            rows = json.loads(bundle.read('export-report.json'))['results']
                    if extension == 'npz':
                        self.assertEqual(valid_bytes, raw)
                    else:
                        self.assertIn(b'Frames: 8', valid_bytes)
                    self.assertEqual([(row['motion_id'], row['status']) for row in rows],
                                     [(broken, 'failed'), (valid, 'saved')])
                    self.assertEqual(rows[0]['reason'], 'invalid_motion')
                status, error = access.call(f'/api/harness/motions/{broken}/download?format=npz')
                self.assertEqual(status, 400)
                self.assertIn('无法解析已保存的源动作记录', error['error'])

    def test_owned_motion_storage_failures_are_explained_in_preview_and_export(self):
        from harness_service import HarnessService
        from test_review_access import AccessTests

        access = AccessTests()
        access.setUp()
        self.addCleanup(access.tearDown)
        service = HarnessService(access.root,legacy_store=access.store)
        access.server.harness_service = service
        user = {'username':'alice','role':'user'}
        arrays, rest, parents = fixture(8)
        arrays = dict(arrays,rest_joints=rest,parents=parents)
        mid = service.assets.register('alice',arrays)['id']
        other = service.assets.register('alice',arrays)['id']
        path = service.assets.artifact_path('alice',mid)
        original = path.read_bytes()
        base = '/api/harness/'
        for kind in ('缺失','损坏'):
            with self.subTest(kind=kind):
                if kind=='缺失':
                    path.unlink()
                else:
                    path.write_bytes(b'incomplete')
                for suffix in ('preview','download?format=npz','download?format=bvh'):
                    route = f'{base}motions/{mid}/{suffix}'
                    status, body = access.call(route)
                    self.assertEqual(status,503,body)
                    self.assertIn('动作文件'+kind,body['error'])
                    self.assertNotIn(str(path),body['error'])
                    self.assertEqual(access.call(route,'bob')[0],403)
                route = f'{base}export?ids={mid}&format=npz'
                status, body = access.call(route)
                self.assertEqual(status,503,body)
                self.assertIn('动作文件'+kind,body['error'])
                with zipfile.ZipFile(io.BytesIO(export_batch(service,user,[mid,other],'npz'))) as bundle:
                    results = json.loads(bundle.read('export-report.json'))['results']
                    self.assertEqual(results[0]['reason'],'read_failed')
                    self.assertIn('动作文件'+kind,results[0]['error'])
                    self.assertIn(other+'.npz',bundle.namelist())
                path.write_bytes(original)
                self.assertEqual(access.call(f'{base}motions/{mid}/preview')[0],200)
                self.assertEqual(export_one(service,user,mid),original)

    def test_removed_or_unreadable_cold_member_does_not_abort_the_batch(self):
        from g2h.cold_storage import package
        from harness_service import HarnessService
        from test_review_access import AccessTests

        access = AccessTests()
        access.setUp()
        self.addCleanup(access.tearDown)
        service = HarnessService(access.root, legacy_store=access.store)
        user = {'username': 'alice', 'role': 'user'}
        arrays, rest, parents = fixture(8)
        valid_id = service.assets.register('alice', dict(arrays, rest_joints=rest, parents=parents))['motion_id']
        removed_id = access.fixture.mid
        self.assertEqual(access.call('/api/motions/' + removed_id + '/remove', 'jfc', {})[0], 200)
        exported = export_batch(service, user, [removed_id, valid_id], 'npz')
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual(report['results'][0]['reason'], 'unavailable')
        self.assertEqual(access.call('/api/motions/' + removed_id + '/restore', 'jfc', {})[0], 200)
        access.store.save(removed_id, access.fixture.value)
        package(access.root, 'walking', 0, min_age_days=0)
        with patch('zipfile.ZipFile.read', side_effect=NotImplementedError('Unsupported archive compression')):
            exported = export_batch(service, user, [removed_id, valid_id], 'npz')
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual(report['results'][0]['reason'], 'read_failed')
            self.assertIn(valid_id + '.npz', archive.namelist())

    def test_scene_library_export_matches_the_saved_candidate_and_preview(self):
        from g2h import bvh_import, human_interaction
        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from test_personal_assets import commit_scene_candidate
        from test_review_access import AccessTests

        access = AccessTests()
        access.setUp()
        self.addCleanup(access.tearDown)
        access.server.harness_service = HarnessService(access.root, legacy_store=access.store)
        with patch.dict('os.environ', {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                       'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            status, submitted = access.call('/api/harness/multi-scene', body={
                'request_id': 'test-export-scene-library', 'duration': 1,
                'actors': [{'id': 'R1', 'start_xz_m': [2, -1], 'heading_deg': 90,
                            'scene': {'kind': human_interaction.VERSION, 'objects': [], 'keyframes': [],
                                      'contacts': [], 'waypoints': [
                                          {'frame': 0, 'xz_m': [2, -1], 'heading_deg': 90},
                                          {'frame': 29, 'xz_m': [2.5, -.75], 'heading_deg': 120},
                                      ]}}],
            })
        self.assertEqual(status, 202, submitted)
        key = submitted['operations'][0]['handle']['request_id']
        custom = Custom(access.root)
        original = commit_scene_candidate(custom, key)
        custom.update(key, status='running', done=0)
        status, restored = access.call(f"/api/harness/tasks/{submitted['id']}/scene-status", body={})
        self.assertEqual(status, 200, restored)
        self.assertEqual(len(restored['candidates']), 1, restored)
        candidate = restored['candidates'][0]['motion_id']
        status, preview = access.call(f'/api/harness/motions/{candidate}/preview')
        self.assertEqual(status, 200, preview)

        def download(mid, format):
            request = urllib.request.Request(access.url + f'/api/harness/motions/{mid}/download?format={format}',
                                             headers={'Cookie': 'mf_session=' + access.tokens['alice']})
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.read()

        original_bvh = download(original, 'bvh')
        self.assertEqual(original_bvh, download(candidate, 'bvh'))
        joints, channels, _, _ = bvh_import.read(original_bvh)
        mapped, missing = bvh_import.mapping(joints)
        self.assertEqual(missing, [])
        self.assertEqual(mapped['pelvis'], 'J0_pelvis')
        points, _ = bvh_import.world(joints, channels, np.eye(3), .01, 0)
        self.assertGreater(np.linalg.norm(points[-1, 0] - points[0, 0]), .5)
        order = [int(name.split('_', 1)[0][1:]) for name, *_ in joints]
        np.testing.assert_allclose(points, np.asarray(preview['joints'])[:, order], atol=1e-6)
        fields = ('parents', 'rest_joints', 'scene_spec_json')
        with np.load(io.BytesIO(download(original, 'npz')), allow_pickle=False) as library, \
             np.load(io.BytesIO(download(candidate, 'npz')), allow_pickle=False) as saved:
            for field in fields:
                np.testing.assert_array_equal(library[field], saved[field])

    def test_bvh_root_and_rotation_roundtrip(self):
        arrays,rest,parents=fixture(8)
        rng=np.random.default_rng(42)
        poses=rng.normal(0,.3,(8,52,3));arrays['smpl_poses']=poses.reshape(8,156)
        arrays['native_transl']=rng.normal(size=(8,3));before=arrays['native_transl'].copy()
        text=bvh(arrays,parents,rest,['joint']*52).decode()
        order=[int(i) for i in re.findall(r'(?:ROOT|JOINT) J(\d+)_',text)]
        data=np.loadtxt(io.StringIO(text.split('Frame Time: ')[1].split('\n',1)[1]))
        np.testing.assert_allclose(data[:,:3]/100,arrays['native_transl']+rest[0],atol=1e-9)
        actual=Rotation.from_euler('ZXY',data[:,3:].reshape(-1,3),degrees=True).as_matrix()
        expected=Rotation.from_rotvec(poses[:,order].reshape(-1,3)).as_matrix()
        np.testing.assert_allclose(actual,expected,atol=1e-8)
        np.testing.assert_array_equal(before,arrays['native_transl'])
        offsets=np.array([[float(x) for x in line.split()[1:]] for line in text.splitlines() if line.strip().startswith('OFFSET')])
        self.assertIn('Frames: 8',text);self.assertEqual(len(order),52)

    def test_bvh_checks_byte_budget_before_converting_an_oversized_motion(self):
        arrays, rest, parents = fixture(8)
        expected = bvh(arrays, parents, rest, None)
        self.assertEqual(bvh(arrays, parents, rest, None, max_bytes=len(expected)), expected)
        with self.assertRaisesRegex(ValueError, '批次超过 64 MiB'):
            bvh(arrays, parents, rest, None, max_bytes=len(expected) - 1)
        with patch('motion_exports.Rotation') as rotations:
            with self.assertRaisesRegex(ValueError, '批次超过 64 MiB'):
                bvh(arrays, parents, rest, None, max_bytes=1)
            rotations.from_rotvec.assert_not_called()

    def test_oversized_bvh_member_leaves_room_for_later_smaller_members(self):
        class Service:
            root = Path(__file__).resolve().parent
            legacy = None

            def download(self, user, mid):
                arrays, rest, parents = fixture(2 if mid == 'small' else 8)
                buffer = io.BytesIO()
                np.savez(buffer, **arrays, parents=parents, rest_joints=rest)
                return buffer.getvalue()

        service = Service()
        first = export_one(service, {}, 'first', 'bvh')
        small = export_one(service, {}, 'small', 'bvh')
        with patch('motion_exports.MAX_BATCH_BYTES', len(first) + len(small), create=True):
            raw = export_batch(service, {}, ['first', 'too-large', 'small'], 'bvh')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), {'first.bvh', 'small.bvh', 'export-report.json'})
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual([row['status'] for row in report['results']], ['saved', 'failed', 'saved'])
            self.assertEqual(report['results'][1]['reason'], 'batch_limit')
    def test_batch_partial_failure_is_explicit_and_bounded(self):
        buffer = io.BytesIO()
        np.savez_compressed(buffer, fps=30)
        source = buffer.getvalue()

        class Service:
            def download(self,user,mid):
                if mid=='denied':raise PermissionError()
                return source
        raw=export_batch(Service(),{'username':'alice'},['allowed','denied'],'npz')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertIn('allowed.npz',z.namelist());self.assertNotIn('denied.npz',z.namelist())
            self.assertEqual(z.read('allowed.npz'), source)
            report=json.loads(z.read('export-report.json'));self.assertEqual([r['status'] for r in report['results']],['saved','failed'])
        for ids in ([],['x']*2,['../x'],[str(i) for i in range(21)]):
            with self.assertRaises(ValueError):export_batch(Service(),{},ids,'npz')
        with self.assertRaises(ValueError):export_batch(Service(),{},['x'],'fbx')

    def test_corrupt_bvh_source_does_not_abort_other_exports(self):
        arrays, rest, parents = fixture(8)
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays, parents=parents, rest_joints=rest)
        source = buffer.getvalue()

        class Service:
            root = Path(__file__).resolve().parent
            legacy = None

            def download(self, user, motion_id):
                return source[:-22] if motion_id == 'damaged' else source

        with zipfile.ZipFile(io.BytesIO(export_batch(Service(), {}, ['damaged', 'valid'], 'bvh'))) as archive:
            self.assertEqual(set(archive.namelist()), {'valid.bvh', 'export-report.json'})
            self.assertIn(b'Frames: 8', archive.read('valid.bvh'))
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual([(row['motion_id'], row['status']) for row in report['results']],
                             [('damaged', 'failed'), ('valid', 'saved')])
        with self.assertRaisesRegex(ValueError, '动作源数据不完整'):
            export_one(Service(), {}, 'damaged', 'bvh')

    def test_bvh_export_reports_unsupported_npz_compression_per_motion(self):
        from test_review_access import AccessTests

        arrays, rest, parents = fixture(8)
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays, parents=parents, rest_joints=rest)
        valid = buffer.getvalue()
        unsupported = bytearray(valid)
        with zipfile.ZipFile(buffer) as archive:
            local_method = archive.infolist()[-1].header_offset + 8
        central_method = valid.rindex(b'PK\x01\x02') + 10
        for offset in (local_method, central_method):
            unsupported[offset:offset + 2] = (99).to_bytes(2, 'little')

        damaged_id, valid_id = 'm00000001', 'm00000002'
        server = AccessTests()
        server.setUp()
        self.addCleanup(server.tearDown)
        server.server.harness_service = SimpleNamespace(
            root=Path(__file__).resolve().parent,
            legacy=None,
            download=lambda user, motion_id: bytes(unsupported) if motion_id == damaged_id else valid,
        )
        status, body = server.call(f'/api/harness/motions/{damaged_id}/download?format=bvh')
        self.assertEqual(status, 400)
        self.assertIn('动作源数据不完整，无法导出 BVH', body['error'])
        url = server.url + f'/api/harness/export?ids={damaged_id},{valid_id}&format=bvh'
        request = urllib.request.Request(url, headers={'Cookie': 'mf_session=' + server.tokens['alice']})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            raw = response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), {valid_id + '.bvh', 'export-report.json'})
            self.assertIn(b'Frames: 8', archive.read(valid_id + '.bvh'))
            results = json.loads(archive.read('export-report.json'))['results']
            self.assertEqual([row['status'] for row in results], ['failed', 'saved'])
            self.assertEqual(results[0]['reason'], 'invalid_motion')

    def test_corrupt_npz_is_rejected_without_reencoding_valid_files(self):
        buffer = io.BytesIO()
        np.savez(buffer, joints=np.arange(12))
        source = buffer.getvalue()
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            entry = archive.getinfo('joints.npy')
            corrupt = bytearray(source)
            name_bytes = int.from_bytes(source[entry.header_offset + 26:entry.header_offset + 28], 'little')
            extra_bytes = int.from_bytes(source[entry.header_offset + 28:entry.header_offset + 30], 'little')
            corrupt[entry.header_offset + 30 + name_bytes + extra_bytes + entry.file_size - 1] ^= 1

        class Service:
            def download(self, user, mid):
                return {'valid': source, 'truncated': source[:-22], 'bad-crc': bytes(corrupt),
                        'wrong-type': b'not an NPZ archive'}[mid]

        self.assertEqual(export_one(Service(), {}, 'valid'), source)
        for mid in ('truncated', 'bad-crc', 'wrong-type'):
            with self.subTest(motion=mid):
                with self.assertRaisesRegex(ValueError, '动作源数据不完整，无法导出 NPZ'):
                    export_one(Service(), {}, mid)
        batch = export_batch(Service(), {}, ['bad-crc', 'valid'], 'npz')
        with zipfile.ZipFile(io.BytesIO(batch)) as archive:
            self.assertEqual(set(archive.namelist()), {'valid.npz', 'export-report.json'})
            self.assertEqual(archive.read('valid.npz'), source)
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual([row['status'] for row in report['results']], ['failed', 'saved'])

    def test_invalid_bvh_skeleton_or_motion_does_not_abort_the_batch(self):
        arrays, rest, parents = fixture(8)

        def source(**overrides):
            buffer = io.BytesIO()
            np.savez_compressed(buffer, **{**arrays, 'parents': parents, 'rest_joints': rest, **overrides})
            return buffer.getvalue()

        members = {
            'bad-parents': source(parents=np.array(0)),
            'float-parents': source(parents=parents.astype(float)),
            'bad-fps': source(fps=np.array([24, 30])),
            'bad-root': source(native_transl=np.zeros((8, 2))),
            'valid': source(),
        }

        class Service:
            root = Path(__file__).resolve().parent
            legacy = None

            def download(self, user, mid):
                return members[mid]

        for mid in tuple(members)[:-1]:
            with self.subTest(motion=mid):
                with self.assertRaisesRegex(ValueError, '无法导出 BVH'):
                    export_one(Service(), {}, mid, 'bvh')
        with zipfile.ZipFile(io.BytesIO(export_batch(Service(), {}, list(members), 'bvh'))) as archive:
            self.assertEqual(set(archive.namelist()), {'valid.bvh', 'export-report.json'})
            self.assertIn(b'Frames: 8', archive.read('valid.bvh'))
            report = json.loads(archive.read('export-report.json'))
            self.assertEqual([row['status'] for row in report['results']], ['failed'] * 4 + ['saved'])

    def test_npz_with_a_valid_zip_checksum_can_still_contain_a_broken_array(self):
        def npy(value, version=None):
            stream = io.BytesIO()
            np.lib.format.write_array(stream, value, version=version)
            return stream.getvalue()

        def npz(*members):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, body in members:
                    archive.writestr(name + '.npy', body)
                self.assertIsNone(archive.testzip())
            return stream.getvalue()

        array = npy(np.arange(12))
        version_three = npy(np.zeros(2, dtype=[('关节', 'f4')]), version=(3, 0))
        sources = {
            'not-npy': npz(('joints', b'actual text')),
            'truncated-array': npz(('joints', array[:-1])),
            'mixed': npz(('joints', array), ('fps', b'not a frame rate')),
            'oversized-header': npz(('joints', array[:8] + bytes([255, 255]) + array[10:])),
            'object-array': npz(('joints', npy(np.array([{'id': 1}], dtype=object)))),
            'valid': npz(('joints', array), ('fps', npy(np.array(30), version=(2, 0))),
                         ('metadata', version_three)),
        }

        class Service:
            def download(self, user, motion_id):
                return sources[motion_id]

        for motion_id in list(sources)[:-1]:
            with self.subTest(motion=motion_id):
                with self.assertRaisesRegex(ValueError, '动作源数据不完整，无法导出 NPZ'):
                    export_one(Service(), {}, motion_id)
        self.assertEqual(export_one(Service(), {}, 'valid'), sources['valid'])
        raw = export_batch(Service(), {}, ['truncated-array', 'valid'], 'npz')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(set(archive.namelist()), {'valid.npz', 'export-report.json'})
            self.assertEqual(archive.read('valid.npz'), sources['valid'])
            rows = json.loads(archive.read('export-report.json'))['results']
            self.assertEqual([row['status'] for row in rows], ['failed', 'saved'])

    def test_batch_without_exportable_motions_is_an_error(self):
        class Service:
            def download(self, user, mid):
                raise FileNotFoundError(mid)

        for fmt in ('npz', 'bvh'):
            with self.subTest(format=fmt):
                with self.assertRaisesRegex(ValueError, '没有可导出的动作'):
                    export_batch(Service(), {}, ['missing1', 'missing2'], fmt)

    def test_batch_with_only_oversized_or_unavailable_motions_reports_the_limit(self):
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        buffer = io.BytesIO()
        np.savez(buffer, joints=np.arange(12))
        source = buffer.getvalue()

        def download(user, mid):
            if mid == 'm00000003':
                raise Forbidden('unavailable private motion')
            return source

        fixture.server.harness_service = SimpleNamespace(download=download)
        route = '/api/harness/export?format=npz&ids='
        with patch('motion_exports.MAX_BATCH_BYTES', len(source)-1):
            for ids in ('m00000001,m00000002', 'm00000001,m00000003'):
                with self.subTest(ids=ids):
                    code, response = fixture.call(route+ids)
                    self.assertEqual(code, 422)
                    self.assertIn('64 MiB', response['error'])
                    self.assertIn('单独下载', response['error'])
                    self.assertNotIn('unavailable private motion', response['error'])
                    self.assertEqual([row['motion_id'] for row in response['failures']],ids.split(','))
                    self.assertEqual([row['reason'] for row in response['failures']],
                                     ['batch_limit', 'unavailable' if ids.endswith('03') else 'batch_limit'])
                    self.assertTrue(all(row['status']=='failed' for row in response['failures']))
                    self.assertNotIn('unavailable private motion',json.dumps(response))
            request = urllib.request.Request(
                fixture.url+'/api/harness/motions/m00000001/download?format=npz',
                headers={'Cookie':'mf_session='+fixture.tokens['alice']},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.read(), source)

    def test_read_and_access_failures_do_not_abort_a_batch_over_http(self):
        from test_review_access import AccessTests

        buffer = io.BytesIO()
        np.savez(buffer, joints=np.arange(12))
        source = buffer.getvalue()
        errors = {
            'm00000001': OSError('backend storage path'),
            'm00000002': Forbidden('unavailable private motion'),
            'm00000003': zipfile.BadZipFile('invalid legacy source'),
            'm00000005': TypeError('invalid stored motion record'),
        }

        def download(user, mid):
            if mid in errors:
                raise errors[mid]
            return source

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.server.harness_service = SimpleNamespace(download=download)
        ids = [*errors, 'm00000004']
        url = fixture.url + '/api/harness/export?format=npz&ids=' + ','.join(ids)
        request = urllib.request.Request(url, headers={'Cookie': 'mf_session=' + fixture.tokens['alice']})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(set(archive.namelist()), {'m00000004.npz', 'export-report.json'})
                self.assertEqual(archive.read('m00000004.npz'), source)
                report = json.loads(archive.read('export-report.json'))
        self.assertEqual([(row['motion_id'], row['status']) for row in report['results']],
                         list(zip(ids, ['failed', 'failed', 'failed', 'failed', 'saved'])))
        self.assertEqual([row['reason'] for row in report['results'][:-1]],
                         ['read_failed', 'unavailable', 'invalid_motion', 'invalid_motion'])
        self.assertNotIn('backend storage path', str(report))
        self.assertNotIn('unavailable private motion', str(report))

    def test_corrupt_single_export_and_all_failed_batch_return_http_errors(self):
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        array = io.BytesIO()
        np.save(array, np.arange(12))
        broken = io.BytesIO()
        with zipfile.ZipFile(broken, 'w') as archive:
            archive.writestr('joints.npy', array.getvalue()[:-1])
        fixture.server.harness_service = SimpleNamespace(download=lambda user, mid: broken.getvalue())
        code, body = fixture.call('/api/harness/motions/m00000001/download?format=npz')
        self.assertEqual(code, 400)
        self.assertIn('动作源数据不完整，无法导出 NPZ', body['error'])
        code, body = fixture.call('/api/harness/export?ids=missing1,missing2&format=npz')
        self.assertEqual(code, 400)
        self.assertIn('没有可导出的动作', body['error'])

    def test_storage_read_errors_and_missing_sources_have_distinct_http_responses(self):
        import errno
        import sqlite3

        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        errors = {
            'm00000001': PermissionError(errno.EACCES, 'test path'),
            'm00000002': sqlite3.OperationalError('database is locked'),
            'm00000003': FileNotFoundError('missing test file'),
        }

        def download(user, mid):
            if mid in errors:
                raise errors[mid]
            output = io.BytesIO()
            np.savez(output, joints=np.arange(12))
            return output.getvalue()

        fixture.server.harness_service = SimpleNamespace(download=download)
        for motion_id, expected in zip(errors, (503, 503, 404)):
            with self.subTest(motion_id=motion_id):
                code, error = fixture.call(f'/api/harness/motions/{motion_id}/download?format=npz')
                self.assertEqual(code, expected)
                self.assertNotIn(str(errors[motion_id]), error['error'])
        route = '/api/harness/export?format=npz&ids='
        self.assertEqual(fixture.call(route+'m00000003')[0], 400)
        code, body = fixture.call(route+','.join(errors))
        self.assertEqual(code, 503)
        self.assertIn('请稍后重试', body['error'])
        self.assertNotIn('test path', body['error'])

        request = urllib.request.Request(fixture.url+route+','.join([*errors, 'm00000004']),
                                         headers={'Cookie':'mf_session='+fixture.tokens['alice']})
        with urllib.request.urlopen(request, timeout=5) as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                rows = json.loads(archive.read('export-report.json'))['results']
                self.assertEqual([row.get('reason') for row in rows],
                                 ['read_failed', 'read_failed', 'unavailable', None])
                self.assertIn('m00000004.npz', archive.namelist())

    def test_download_of_an_imported_source_reports_a_missing_file_only_to_its_owner(self):
        from gadget_imports import FIELDS, Imports
        from test_review_access import AccessTests

        fixture_access = AccessTests()
        fixture_access.setUp()
        self.addCleanup(fixture_access.tearDown)
        arrays, _, _ = fixture(8)
        source = io.BytesIO()
        np.savez(source, **{name:arrays[name] for name in FIELDS})
        imports = Imports(fixture_access.root)
        key = '4'*32
        imports.save('alice', key, 'Imported motion', source.getvalue())
        mid = 'mimp'+key
        path = f'/api/harness/motions/{mid}/download?format=npz'
        asset = imports.folder/(mid+'.npz')
        moved = imports.folder/'temporarily-unavailable.npz'
        asset.rename(moved)
        try:
            self.assertEqual(fixture_access.call(path, 'bob')[0], 403)
            code, error = fixture_access.call(path, 'alice')
            self.assertEqual(code, 404)
            self.assertIn('动作源文件不存在', error['error'])
        finally:
            moved.rename(asset)
        request = urllib.request.Request(fixture_access.url+path,
                                         headers={'Cookie':'mf_session='+fixture_access.tokens['alice']})
        with urllib.request.urlopen(request, timeout=5) as response:
            with np.load(io.BytesIO(response.read()), allow_pickle=False) as restored:
                np.testing.assert_array_equal(restored['native_transl'], arrays['native_transl'])

    def test_interrupted_motion_exports_do_not_append_an_http_error(self):
        from review_server import Handler
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        source = io.BytesIO()
        np.savez(source, joints=np.arange(12))
        fixture.server.harness_service = SimpleNamespace(download=lambda user, mid: source.getvalue())
        headers = {'Cookie': 'mf_session='+fixture.tokens['alice']}
        original = Handler.send
        prefix = b'PK\x03\x04'

        def send(handler, status, data, *args, **kwargs):
            if isinstance(data, bytes) and data.startswith(prefix):
                write = handler.wfile.write

                def interrupt(chunk):
                    if chunk.startswith(prefix):
                        write(prefix)
                        raise TimeoutError('simulated interrupted export')
                    return write(chunk)

                handler.wfile.write = interrupt
            return original(handler, status, data, *args, **kwargs)

        for path in ('/api/harness/motions/m00000001/download?format=npz',
                     '/api/harness/export?ids=m00000001,m00000002&format=npz'):
            with self.subTest(path=path):
                request = urllib.request.Request(fixture.url+path, headers=headers)
                with patch.object(Handler, 'send', send), urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    with self.assertRaises(IncompleteRead) as interrupted:
                        response.read()
                    self.assertEqual(interrupted.exception.partial, prefix)
                with urllib.request.urlopen(request, timeout=5) as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        self.assertIsNone(archive.testzip())

if __name__=='__main__':unittest.main()
