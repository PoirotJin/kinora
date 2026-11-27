"""Regression tests for BVH normalization and immutable, private snapshots."""
import base64
from contextlib import closing
import errno
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

import numpy as np
from scipy.spatial.transform import Rotation

from g2h import bvh_import, human_rig
from g2h.kimodo_adapter import fk
from g2h.motion_assets import MotionAssets
from motion_exports import bvh
from personal_assets import Library, encoded
from personal_asset_media import apply_pose, import_motion, rig_preview, save_object, save_pose, save_rig
from personal_snapshot_jobs import get, submit
from review_access import Forbidden, allowed
from review_collections import change, collections, setup
from review_harness import handle
from urllib.parse import urlsplit


class Store:
    def __init__(self, root):
        self.root = root
        with closing(self.reviews()) as db:
            setup(db)
        (root / 'review').mkdir()
        with closing(sqlite3.connect(root / 'review' / 'accounts.sqlite3')) as db, db:
            db.execute('CREATE TABLE users(username TEXT, role TEXT, disabled INTEGER)')
            db.executemany('INSERT INTO users VALUES (?, ?, 0)', [('alice','user'), ('bob','admin')])

    def reviews(self):
        return sqlite3.connect(self.root / 'reviews.sqlite3', timeout=10)


def motion_fixture():
    rest, parents, names = human_rig.definition()
    rotations = np.zeros((30, 52, 3))
    rotations[:, 0, 1] = np.linspace(0, 1.5, 30)
    rotations[:, 16, 2] = np.linspace(0, -.6, 30)
    rotations[:, 17, 2] = .6
    points, _ = fk(Rotation.from_rotvec(rotations.reshape(-1,3)).as_matrix().reshape(30,52,3,3),
                   rest,parents)
    root = np.stack((np.linspace(0,2,30),np.linspace(1,.8,30),np.linspace(-1,.5,30)),axis=1)
    arrays = dict(native_keypoints3d=points, native_transl=root, fps=np.asarray(30),
                  smpl_poses=rotations.reshape(30,156),parents=parents,rest_joints=rest)
    return arrays, bvh(arrays,parents,rest,names)


def commit_scene_candidate(custom, key):
    from g2h import human_interaction
    from g2h.collection_store import CollectionStore
    from g2h.farm_index import register_motion
    from MotionFarm import catalog

    with closing(custom.db()) as db:
        job = db.execute('SELECT * FROM requests WHERE id=?', (key,)).fetchone()
    scene = json.loads(job['scene'])
    frames = job['duration'] * 30
    rest, parents, _ = human_rig.definition()
    _, roots, _, path, headings = human_interaction.world_targets(scene, frames)
    rotations = np.zeros((frames, 52, 3))
    rotations[:, 0, 1] = headings
    rotations[:, 1:22] = scene['keyframes'][0]['body_rotvecs']
    local = Rotation.from_rotvec(rotations.reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
    points, _ = fk(local, rest, parents)
    world_root = np.column_stack((path[:, 0], np.full(frames, roots[0, 1]), path[:, 1]))
    arrays = dict(native_keypoints3d=points, native_transl=world_root-rest[0],
                  fps=np.asarray(30), smpl_poses=rotations.reshape(frames, 156), parents=parents,
                  rest_joints=rest, native_root_rotations_mat=local[:, 0],
                  scene_spec_json=human_interaction.encode(scene))
    quality = human_interaction.report(arrays, scene, rest, parents)
    assert quality['passed'], quality
    prompt_id = hashlib.sha256(job['description'].encode()).hexdigest()
    prompt = {'id': prompt_id, 'text': job['description'], 'topic_id': 'custom', 'attributes': {}, 'source': {}}
    folder = custom.folder / key
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'manifest.json').write_text(json.dumps({'prompts': [prompt]}))
    with closing(catalog(custom.root)) as db, db:
        db.execute('INSERT OR IGNORE INTO prompts VALUES(?,?,?,?)',
                   (prompt_id, 'custom', prompt['text'], json.dumps(prompt)))
        motion_id = register_motion(db, prompt_id, 'custom', key, job['seed'])
    with CollectionStore(custom.root / 'topics/custom') as store:
        store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)', (key, '{}'))
        store.db.commit()
        store.append({'id': key, 'text': job['description'], 'duration_s': job['duration'], 'scene': scene,
                      'motion': arrays, 'quality': {'human_interaction': quality}})
    return motion_id


def bvh_with_late_joint_translation(raw):
    header, motion = raw.decode().split('MOTION', 1)
    left_hip = header.index('JOINT J1_left_hip')
    channel_line = header.index('CHANNELS 3', left_hip)
    channel_end = header.index('\n', channel_line)
    header = (header[:channel_line] + header[channel_line:channel_end].replace('CHANNELS 3', 'CHANNELS 4')
              + ' Xposition' + header[channel_end:])
    lines = motion.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith('Frame Time:')) + 1
    for index, line in enumerate(lines[start:], start):
        columns = line.split()
        if columns:
            # Xposition on the left hip follows the root's six and hip's three columns.
            columns.insert(9, '1' if index == start + 3 else '0')
            lines[index] = ' '.join(columns)
    raw = (header + 'MOTION' + '\n'.join(lines) + '\n').encode()
    return raw


class Assets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.alice = dict(username='alice',role='user')
        self.bob = dict(username='bob',role='admin')
        self.service = SimpleNamespace(legacy=self.store,owner=lambda user:user['username'],
                                       assets=MotionAssets(self.root))
        self.library = Library(self.service, self.alice)
        self.arrays, self.raw = motion_fixture()
        self.first = self.service.assets.register('alice',self.arrays,description='搬箱')

    def test_new_rig_for_import_does_not_publish_if_later_frames_are_unsupported(self):
        raw = bvh_with_late_joint_translation(self.raw)
        joints = bvh_import.read(raw)[0]
        mapping, _ = bvh_import.mapping(joints)
        value = dict(id='3'*32, name='Six-DOF child import', file=base64.b64encode(raw).decode(),
                     units=.01, up='+Y', forward='+Z', mapping=mapping, validate_import=True)
        rig = save_rig(self.library, value)
        self.assertEqual(rig['name'], value['name'])
        imported = import_motion(self.library, dict(id='4'*32, name='Child translation supported',
                                                    file=value['file'], rig=rig['id']))
        self.assertTrue(imported['id'].startswith('ma_'))

    def test_bvh_calibration_preserves_root_and_round_trips_body(self):
        parsed = bvh_import.read(self.raw)
        joints,values,dt,signature = parsed
        mapping, missing = bvh_import.mapping(joints)
        self.assertFalse(missing)
        profile = dict(signature=signature,mapping=mapping,up='+Y',forward='+Z',units=.01,
                       calibration_channels=np.zeros(values.shape[1]))
        result = bvh_import.convert(parsed,profile)
        np.testing.assert_allclose(result['native_transl'], self.arrays['native_transl'], atol=2e-6)
        np.testing.assert_allclose(result['smpl_poses'][:,:66], self.arrays['smpl_poses'][:,:66], atol=2e-6)
        self.assertAlmostEqual(dt,1/30,places=8)
        with self.assertRaisesRegex(ValueError,'骨架'):
            bvh_import.convert(parsed,dict(profile,signature='bad'))

    def test_bvh_import_preserves_endpoints_at_non_native_frame_rates(self):
        _, parents, names = human_rig.definition()
        for fps, frames, expected_frames in ((24, 4, 5), (120, 2, 2)):
            with self.subTest(fps=fps):
                source = dict(self.arrays, fps=np.asarray(fps))
                for key in ('native_keypoints3d', 'native_transl', 'smpl_poses'):
                    source[key] = source[key][-frames:]
                raw = bvh(source, parents, source['rest_joints'], names)
                encoded = base64.b64encode(raw).decode()
                joints, _, _, _ = bvh_import.read(raw)
                mapping, _ = bvh_import.mapping(joints)
                rig = save_rig(self.library, dict(name='Round trip', file=encoded, mapping=mapping,
                                                  units=.01, up='+Y', forward='+Z'))
                result = import_motion(self.library, dict(name='Imported', rig=rig['id'], file=encoded,
                                                           id=f'{fps:032x}'))
                imported, _ = self.service.assets.load('alice', result['id'])
                self.assertEqual(int(imported['fps']), 30)
                np.testing.assert_allclose(imported['native_transl'][[0,-1]],
                                           source['native_transl'][[0,-1]], atol=2e-6)
                np.testing.assert_allclose(imported['smpl_poses'][[0,-1],:66],
                                           source['smpl_poses'][[0,-1],:66], atol=2e-6)
                self.assertEqual(len(imported['native_transl']), expected_frames)

    def test_bvh_rig_save_retry_reuses_version_and_does_not_charge_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from g2h import resource_quota

        raw = base64.b64encode(self.raw).decode()
        joints, _, _, _ = bvh_import.read(self.raw)
        mapping, _ = bvh_import.mapping(joints)
        settings = dict(name='Retry-safe rig', file=raw, mapping=mapping,
                        units=.01, up='+Y', forward='+Z')
        initial = save_rig(self.library, settings)
        request = dict(settings, id='9'*32, previous=initial['id'], name='Updated rig')
        before = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(before, len(self.raw))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [job.result() for job in
                             [pool.submit(save_rig, self.library, request) for _ in range(2)]]
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['family'], initial['family'])
        self.assertEqual(save_rig(self.library, request)['id'], first['id'])
        used = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(used, before)
        with self.assertRaisesRegex(ValueError, '预设请求编号已被使用'):
            save_rig(self.library, dict(request, frame=1))

    def test_replaying_a_rig_save_or_bvh_import_repairs_the_retained_source(self):
        from g2h import capacity, resource_quota

        file = base64.b64encode(self.raw).decode()
        mapping, _ = bvh_import.mapping(bvh_import.read(self.raw)[0])
        settings = dict(id='4'*32,name='Repairable reference',file=file,mapping=mapping,
                        units=.01,up='+Y',forward='+Z')
        rig = save_rig(self.library,settings)
        path = self.library.root/(rig['data']['reference_sha256']+'.bvh')
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        path.write_bytes(b'damaged')
        with self.assertRaisesRegex(ValueError,'校验失败'):
            rig_preview(self.library,rig)
        with patch.dict(os.environ,{'MOTIONFARM_USER_STORAGE_BYTES':str(int(before))}):
            self.assertEqual(save_rig(self.library,settings),rig)
        self.assertEqual(path.read_bytes(),self.raw)
        self.assertIn('source',rig_preview(self.library,rig))
        self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],before)

        command = dict(id='5'*32,name='Recoverable import',file=file,rig=rig['id'])
        imported = import_motion(self.library,command)
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        assets = self.service.assets
        artifact = assets.artifact_path('alice',imported['id'])
        expected = artifact.read_bytes()
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                path.unlink()
                if corrupt:
                    artifact.write_bytes(b'truncated')
                else:
                    artifact.unlink()
                with patch.dict(os.environ,{'MOTIONFARM_USER_STORAGE_BYTES':str(int(before))}):
                    self.assertEqual(import_motion(self.library,command),imported)
                self.assertEqual(path.read_bytes(),self.raw)
                self.assertEqual(artifact.read_bytes(),expected)
                self.assertEqual(len(assets.list('alice')),2)
                self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],before)
                self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)
        artifact.write_bytes(b'keep until an exact copy is found')
        changed = bvh_import.convert(bvh_import.read(self.raw),rig['data'])
        changed['native_transl'] = changed['native_transl'] + 0.5
        with patch('g2h.bvh_import.convert',return_value=changed):
            with self.assertRaisesRegex(ValueError,'无法校验恢复后的动作'):
                import_motion(self.library,command)
        self.assertEqual(artifact.read_bytes(),b'keep until an exact copy is found')
        self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],before)

    def test_parallel_bvh_import_retries_charge_only_one_motion(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, local
        from g2h import resource_quota

        upload = base64.b64encode(self.raw).decode()
        mapping, _ = bvh_import.mapping(bvh_import.read(self.raw)[0])
        rig = save_rig(self.library, dict(name='Shared rig', file=upload, mapping=mapping,
                                         units=.01, up='+Y', forward='+Z'))
        request = dict(id='5'*32, name='Concurrent import', file=upload, rig=rig['id'])
        before = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        assets = self.service.assets
        find = assets.find_request
        threads = local()
        checked = Barrier(2, timeout=5)

        def find_after_both_requests_start(owner, request_id):
            result = find(owner, request_id)
            if not getattr(threads, 'checked', False):
                threads.checked = True
                checked.wait()
            return result

        with patch.object(assets, 'find_request', side_effect=find_after_both_requests_start):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(import_motion, self.library, request) for _ in range(2)]
                first, second = (result.result() for result in pending)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(import_motion(self.library, request)['id'], first['id'])
        with self.assertRaisesRegex(ValueError, '导入编号已被使用'):
            import_motion(self.library, dict(request, name='Different import'))
        self.assertEqual(len(assets.list('alice')), 2)
        used = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(used - before, assets.artifact_size('alice', first['id']))

    def test_duplicate_pose_save_requests_return_one_immutable_preset(self):
        from concurrent.futures import ThreadPoolExecutor

        request = dict(id='1'*32, name='Saved pose', motion_id=self.first['id'], frame=9)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = [pool.submit(save_pose, self.library, request) for _ in range(2)]
            first, second = (reply.result() for reply in replies)
        self.assertEqual(first, second)
        self.assertEqual(first['data']['frame'], 9)
        self.assertEqual(first['data']['version_id'], self.first['id'])
        self.assertEqual(self.library.list_assets('pose', 2**63-1)['items'][0]['id'], first['id'])
        self.assertEqual(len(self.library.list_assets('pose', 2**63-1)['items']), 1)
        with self.assertRaisesRegex(ValueError, '预设请求编号已被使用'):
            save_pose(self.library, dict(request, frame=10))

    def test_invalid_pose_frame_does_not_materialize_or_charge_for_imported_source(self):
        from gadget_imports import FIELDS, Imports
        from g2h import resource_quota
        from harness_service import HarnessService

        raw = BytesIO()
        np.savez_compressed(raw, **{name:self.arrays[name] for name in sorted(FIELDS)})
        mid = Imports(self.root).save('alice', '5'*32, 'Unmaterialized pose source', raw.getvalue())['motion_id']
        library = Library(HarnessService(self.root, legacy_store=self.store), self.alice)
        assets = library.service.assets
        size = lambda: resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        prior = size()

        with self.assertRaisesRegex(ValueError, '姿态帧超出动作范围'):
            save_pose(library, dict(id='3'*32, name='Out of bounds', motion_id=mid, frame=30))
        self.assertIsNone(assets.find_request('alice', 'snapshot-source:'+mid))
        self.assertEqual(size(), prior)
        self.assertEqual(library.list_assets('pose', 2**63-1)['items'], [])

        reference = save_pose(library, dict(name='Reference pose', motion_id=self.first['id'], frame=9))
        command = dict(id='4'*32, pose=reference['id'], motion_id=mid, frame=30, blend=3)
        with self.assertRaisesRegex(ValueError, '姿态帧超出动作范围'):
            apply_pose(library, command)
        with self.assertRaisesRegex(ValueError, '请选择有效的目标帧'):
            apply_pose(library, dict(command, frame=9, blend=0))
        self.assertIsNone(assets.find_request('alice', 'snapshot-source:'+mid))
        self.assertEqual(size(), prior)

        pose = save_pose(library, dict(name='Valid last frame', motion_id=mid, frame=29))
        source = assets.find_request('alice', 'snapshot-source:'+mid)
        self.assertEqual(pose['data']['version_id'], source['id'])
        self.assertEqual(pose['data']['frame'], 29)
        self.assertTrue(size() > prior)

    def test_parallel_pose_retries_publish_and_charge_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, local
        from g2h import resource_quota

        assets = self.service.assets
        pose = save_pose(self.library, dict(name='Final pose', motion_id=self.first['id'], frame=29))
        request = dict(id='6'*32, motion_id=self.first['id'], pose=pose['id'], frame=7, blend=3)
        before = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        find = assets.find_request
        threads = local()
        checked = Barrier(2, timeout=5)

        def find_after_both_requests_start(owner, request_id):
            result = find(owner, request_id)
            if not getattr(threads, 'checked', False):
                threads.checked = True
                checked.wait()
            return result

        with patch.object(assets, 'find_request', side_effect=find_after_both_requests_start), \
             patch.object(assets, 'register', wraps=assets.register) as register:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(apply_pose, self.library, request) for _ in range(2)]
                first, second = (result.result() for result in pending)
        self.assertEqual(first['id'], second['id'])
        used = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(used - before, assets.artifact_size('alice', first['id']))
        self.assertEqual(register.call_count, 1)
        with self.assertRaisesRegex(ValueError, 'different motion arguments'):
            apply_pose(self.library, dict(request, frame=8))
        self.assertEqual(len(assets.list('alice')), 2)

    def test_pose_retry_restores_exact_result_and_can_return_it_without_the_parent_file(self):
        from g2h import capacity, resource_quota

        assets = self.service.assets
        pose = save_pose(self.library,dict(name='Final pose',motion_id=self.first['id'],frame=29))
        request = dict(id='9'*32,motion_id=self.first['id'],pose=pose['id'],frame=7,blend=3)
        saved = apply_pose(self.library,request)
        result = assets.artifact_path('alice',saved['id'])
        expected = result.read_bytes()
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        parent = assets.artifact_path('alice',self.first['id'])

        with patch.dict(os.environ,{'MOTIONFARM_USER_STORAGE_BYTES':str(int(before))}):
            for corrupt in (False,True):
                with self.subTest(corrupt=corrupt):
                    if corrupt:
                        result.write_bytes(b'truncated')
                    else:
                        result.unlink(missing_ok=True)
                    self.assertEqual(apply_pose(self.library,request),saved)
                    self.assertEqual(result.read_bytes(),expected)
            parent.unlink()
            self.assertEqual(apply_pose(self.library,request),saved)
            with self.assertRaisesRegex(ValueError,'different motion arguments'):
                apply_pose(self.library,dict(request,frame=8))
            result.write_bytes(b'cannot repair without the parent')
            with self.assertRaisesRegex(ValueError,'请确认原动作可用后重试'):
                apply_pose(self.library,request)
            self.assertEqual(result.read_bytes(),b'cannot repair without the parent')
        self.assertEqual(len(assets.list('alice')),2)
        self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],before)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

    def test_bvh_and_pose_versions_are_private_and_keep_selected_parent(self):
        upload = base64.b64encode(self.raw).decode()
        mapping, _ = bvh_import.mapping(bvh_import.read(self.raw)[0])
        rig = save_rig(self.library,dict(name='Studio A',file=upload,units=.01,up='+Y',forward='+Z',mapping=mapping))
        body = dict(id='a'*32,name='外部 BVH',file=upload,rig=rig['id'])
        imported = import_motion(self.library,body)
        self.assertEqual(import_motion(self.library,body)['id'],imported['id'])
        self.assertEqual(imported['provenance']['rig_version'],rig['id'])
        self.assertEqual(len(rig_preview(self.library,rig)['source']['points']),52)
        self.assertEqual(len(self.library.list_assets('rig',2**63-1,rig['data']['signature'])['items']),1)
        pose = save_pose(self.library,dict(name='结束姿态',motion_id=imported['id'],frame=29))
        request = dict(id='b'*32,motion_id=self.first['id'],pose=pose['id'],frame=6,blend=3)
        result = apply_pose(self.library,request)
        with patch('personal_asset_media.storage', side_effect=AssertionError('Retry must not reserve storage')):
            self.assertEqual(apply_pose(self.library,request)['id'],result['id'])
            with self.assertRaisesRegex(ValueError,'different motion arguments'):
                apply_pose(self.library,{**request,'frame':7})
        output,_ = self.service.assets.load('alice',result['id'])
        np.testing.assert_array_equal(output['native_transl'],self.arrays['native_transl'])
        np.testing.assert_allclose(output['smpl_poses'][6,3:], self.arrays['smpl_poses'][29,3:], atol=1e-6)
        self.assertEqual({row['motion_id'] for row in self.library.history(result['id'])['items']},
                         {self.first['id'],imported['id'],result['id']})
        with self.assertRaises(Forbidden):
            Library(self.service,self.bob).asset(rig['id'])

    def test_object_save_retry_reuses_upload_and_metadata_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        from g2h import resource_quota

        raw = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        params = dict(name='Fixture box', size_m=[1,1,1], anchors=[], heading_deg=0)
        request = dict(params, id='a'*32, file=base64.b64encode(raw).decode())
        before = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = [pool.submit(save_object, self.library, request) for _ in range(2)]
            first, second = (reply.result() for reply in replies)
        self.assertEqual(first, second)
        edited_request = dict(params, id='b'*32, name='Metadata update', previous=first['id'])
        edited = save_object(self.library, edited_request)
        self.assertEqual(save_object(self.library, edited_request), edited)
        self.assertEqual(edited['data']['mesh_sha256'], first['data']['mesh_sha256'])
        self.assertEqual(edited['family'], first['family'])
        with self.assertRaisesRegex(ValueError, '预设请求编号已被使用'):
            save_object(self.library, dict(edited_request, heading_deg=15))
        reused = save_object(self.library, dict(request, id='c'*32, name='Another preset'))
        self.assertNotEqual(reused['id'], first['id'])
        self.assertEqual(reused['data']['mesh_sha256'], first['data']['mesh_sha256'])
        used = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(used - before, len(raw))

    def test_stale_object_replacements_do_not_store_or_charge_for_the_upload(self):
        import hashlib
        from g2h import resource_quota

        source = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        params = dict(name='Fixture',size_m=[1,1,1],heading_deg=0,anchors=[])
        version = save_object(self.library,dict(params,file=base64.b64encode(source).decode()))
        head = save_object(self.library,dict(params,previous=version['id'],heading_deg=25))
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        for keyed in (True, False):
            with self.subTest(keyed=keyed):
                replacement = source + f'# replacement {keyed}\n'.encode()
                request = dict(params,previous=version['id'],file=base64.b64encode(replacement).decode())
                if keyed:
                    request['id'] = 'b'*32
                with self.assertRaisesRegex(ValueError,'已有新版'):
                    save_object(self.library,request)
                path = self.library.root/(hashlib.sha256(replacement).hexdigest()+'.obj')
                self.assertFalse(path.exists())
                used = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
                self.assertEqual(used,before)
                self.assertEqual(self.library.list_assets('object',2**63-1)['items'][0]['id'],head['id'])

    def test_stale_rig_replacement_does_not_charge_for_a_new_reference(self):
        import hashlib
        from g2h import resource_quota

        mapping, _ = bvh_import.mapping(bvh_import.read(self.raw)[0])
        request = dict(name='Rig',file=base64.b64encode(self.raw).decode(),mapping=mapping,
                       units=.01,up='+Y',forward='+Z')
        old = save_rig(self.library,request)
        save_rig(self.library,dict(request,previous=old['id'],name='New head'))
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        replacement = self.raw+b'\n'
        with self.assertRaisesRegex(ValueError,'已有新版'):
            save_rig(self.library,dict(request,previous=old['id'],file=base64.b64encode(replacement).decode()))
        self.assertFalse((self.library.root/(hashlib.sha256(replacement).hexdigest()+'.bvh')).exists())
        self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],before)

    def test_obj_preview_keeps_the_original_edges_of_a_concave_face(self):
        raw = ('\ufeff# Fixture polygon\r\nv 0 0 0\r\nv 2 0 0\r\nv 2 2 0\r\n'
               'v 1 1 0\r\nv 0 2 0\r\nf -5 -4 -3 -2 -1 # concave face\r\n').encode()
        asset = save_object(self.library, dict(name='Concave mesh', file=base64.b64encode(raw).decode(),
                                               size_m=[2,2,1], heading_deg=0, anchors=[]))
        handler = SimpleNamespace(server=SimpleNamespace(harness_service=self.service), send=lambda *args:args)
        route = '/api/harness/personal-assets/objects/' + asset['id'] + '/mesh'
        status, mesh = handle(handler, urlsplit(route), False, self.alice)
        self.assertEqual(status, 200)
        self.assertEqual(len(mesh['vertices']), 5)
        self.assertEqual(mesh['polygons'], [[0,1,2,3,4]])
        self.assertEqual(mesh['faces'], [[0,1,2],[0,2,3],[0,3,4]])

    def test_object_revisions_pin_mesh_and_validate_anchors(self):
        raw = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        initial = dict(name='训练箱',file=base64.b64encode(raw).decode(),size_m=[1,1,1],
                       anchors=[dict(name='把手',position_m=[.5,.2,0])],heading_deg=0)
        version = save_object(self.library,initial)
        edited = save_object(self.library,dict(previous=version['id'],name='新箱子预设',size_m=[1,1,1],
                             anchors=[],heading_deg=30))
        self.assertEqual(edited['data']['mesh_sha256'],version['data']['mesh_sha256'])
        self.assertEqual(edited['family'],version['family'])
        self.assertEqual(self.library.asset(version['id'])['data']['heading_deg'],0)
        with self.assertRaises(ValueError):
            save_object(self.library,{**initial,'anchors':[dict(name='inside',position_m=[0,0,0])]})

    def test_obj_accepts_continued_vertices_and_faces_and_reports_invalid_lines(self):
        from personal_asset_media import mesh_data

        source = (b'v 0 0 \x5c\r\n 0\r\nv 1 0 0\r\nv 0 1 1\r\n'
                  b'f -3 -2 \x5c\r\n -1 # face end\r\n')
        vertices, polygons = mesh_data(source)
        self.assertEqual(vertices,[[0,0,0],[1,0,0],[0,1,1]])
        self.assertEqual(polygons,[[0,1,2]])
        for invalid, message in (
            (b'v 1 x 2\n', 'OBJ 第 1 行：顶点坐标无效'),
            (b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2\n', 'OBJ 第 4 行：面至少需要 3 个顶点'),
            (b'v 0 0 0\nf 1 1 \x5c\nq\n', 'OBJ 第 2 行：顶点索引无效'),
            (b'v 0 0 0\nf 1 1 \x5c', 'OBJ 第 2 行：续行未完成'),
            (b'\xffv 0 0 0', 'OBJ 文件必须使用 UTF-8 编码'),
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError,message):
                mesh_data(invalid)

    def test_personal_scene_matches_exact_and_legacy_rounded_anchors(self):
        from harness_service import HarnessService

        service = HarnessService(self.root, legacy_store=self.store, assets=self.service.assets)
        positions = [[.5, .123456789, 0], [0, -.1234565, -.5]]
        version = self.library.save_asset('object', 'Precision box', {
            'size_m': [1, 1, 1], 'heading_deg': 0,
            'anchors': [{'name': f'Contact {index}', 'position_m': position}
                        for index, position in enumerate(positions)],
        })
        objects = [{'id': 'box', 'asset_version': version['id'], 'size_m': [1, 1, 1], 'heading_deg': 0}]
        for position in positions + [[.5, .123457, 0], [0, -.123456, -.5]]:
            with self.subTest(position=position):
                service._check_personal_scene_objects(self.alice, objects, [{'object_id': 'box', 'anchor_m': position}])
        with self.assertRaisesRegex(ValueError, '接触点'):
            service._check_personal_scene_objects(self.alice, objects, [{'object_id': 'box', 'anchor_m': [.5, .123458, 0]}])

    def test_collection_members_include_owner_scoped_candidate_status(self):
        from g2h.harness import Store as HarnessStore

        ledger = HarnessStore(self.root)
        unchanged = self.first['id']
        accepted = self.service.assets.register('alice', self.arrays, description='Accepted')['id']
        isolated = self.service.assets.register('alice', self.arrays, description='Isolated')['id']
        task = ledger.create_task('alice', 'collection-results', 'Review collection results')['id']
        other = ledger.create_task('bob', 'other-results', 'Other account task')['id']
        chosen = ledger.register_candidate('alice', task, accepted)
        ledger.register_candidate('alice', task, isolated, {'quarantined': True})
        ledger.accept_candidate('alice', task, chosen['id'])
        ledger.register_candidate('bob', other, unchanged, {'quarantined': True})
        change(self.store, self.alice, dict(action='create', id='result-status', name='Results'))
        for motion_id in (accepted, isolated, unchanged):
            change(self.store, self.alice, dict(action='add', id='result-status', motion_id=motion_id))
        members = {item['motion_id']: item for item in
                   collections(self.store, self.alice, {'id': ['result-status']})['items']}
        self.assertEqual((members[accepted]['task_id'], members[accepted]['status'], members[accepted]['accepted']),
                         (task, 'accepted', True))
        self.assertEqual((members[isolated]['task_id'], members[isolated]['status'], members[isolated]['accepted']),
                         (task, 'quarantined', False))
        self.assertNotIn('task_id', members[unchanged])
        self.assertNotIn('status', members[unchanged])

    def test_parallel_legacy_source_materialization_charges_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from io import BytesIO
        from threading import Barrier, local
        from g2h import resource_quota

        payload = BytesIO()
        np.savez_compressed(payload, **self.arrays)
        source = 'm00000001'
        assets = self.service.assets
        find = assets.find_request
        start = Barrier(2, timeout=5)
        threads = local()
        before = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']

        def find_after_both_requests_start(owner, request_id):
            result = find(owner, request_id)
            if not getattr(threads, 'checked', False):
                threads.checked = True
                start.wait()
            return result

        with (patch('personal_assets.require_motion_access'),
              patch.object(self.library, 'motion', return_value={'text': 'Source walk'}),
              patch.object(self.service, 'download', create=True, return_value=payload.getvalue()) as download,
              patch.object(assets, 'find_request', side_effect=find_after_both_requests_start)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(self.library.materialize, source) for _ in range(2)]
                first, second = (job.result() for job in pending)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['provenance']['source_motion_id'], source)
        self.assertEqual(len(assets.list('alice')), 2)
        used = resource_quota.snapshot(self.root, 'alice')['resources']['storage_bytes']['used']
        self.assertEqual(used - before, assets.artifact_size('alice', first['id']))
        download.assert_called_once_with(self.alice, source)

    def test_materializing_a_legacy_motion_charges_before_any_later_read(self):
        from g2h import resource_quota

        payload = BytesIO()
        np.savez_compressed(payload,**self.arrays)
        assets = self.service.assets
        before = resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']
        with (patch('personal_assets.require_motion_access'),
              patch.object(self.library,'motion',return_value={'text':'Legacy source'}),
              patch.object(self.service,'download',create=True,return_value=payload.getvalue()),
              patch.object(assets,'artifact_path',side_effect=OSError(errno.EIO,'output read unavailable'))):
            saved = self.library.materialize('m00000001')
        actual_size = assets.artifact_size('alice',saved['id'])
        self.assertEqual(saved['artifact_bytes'],actual_size)
        self.assertEqual(resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used'],
                         before+actual_size)

    def test_snapshot_retry_during_initial_save_reuses_job_at_queue_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        cid = 'queue-limit'
        change(self.store, self.alice, dict(action='create', id=cid, name='Queue limit'))
        change(self.store, self.alice, dict(action='add', id=cid, motion_id=self.first['id']))
        read_basis = self.library.collection_basis
        basis = read_basis(cid)[2]
        request = dict(id='7'*32, collection_id=cid, name='Concurrent retry', expected_basis=basis)
        start = Barrier(2, timeout=5)
        ready = Barrier(2, timeout=5)

        def read_concurrent_basis(collection_id):
            snapshot = read_basis(collection_id)
            ready.wait()
            return snapshot

        def send_request():
            start.wait()
            return submit(self.library, request)

        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library, dict(request, id='8'*32, name='Already queued'))
            with patch.object(self.library, 'collection_basis', side_effect=read_concurrent_basis):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    replies = [pool.submit(send_request) for _ in range(2)]
                    first, second = (reply.result() for reply in replies)
            self.assertEqual(first, second)
            self.assertEqual(first['id'], request['id'])
            self.assertEqual(first['status'], 'queued')
            with self.assertRaisesRegex(ValueError, '请等待当前快照完成后再提交'):
                submit(self.library, dict(request, id='9'*32, name='Distinct request'))
            with self.assertRaisesRegex(ValueError, '快照编号已被使用'):
                submit(self.library, dict(request, name='Conflicting retry'))

    def test_snapshot_freezes_selection_and_downloads_exact_bytes(self):
        first = self.first['id']
        second = self.service.assets.register('alice',self.arrays,description='编辑版',parent_id=first)['id']
        cid = 'carrying'
        change(self.store,self.alice,dict(id=cid,action='create',name='搬运'))
        change(self.store,self.alice,dict(id=cid,action='add',motion_id=first))
        basis = self.library.collection_basis(cid)[2]
        request = dict(id='c'*32,collection_id=cid,name='训练 v1',expected_basis=basis)
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library,request)
        change(self.store,self.alice,dict(id=cid,action='rename',name='改名后'))
        change(self.store,self.alice,dict(id=cid,action='remove',motion_id=first))
        change(self.store,self.alice,dict(id=cid,action='add',motion_id=second))
        for attempt in range(250):
            job = get(self.library,request['id'])
            if job['status'] != 'queued':
                break
            time.sleep(.02)
        self.assertEqual(job['status'],'done',job)
        snapshot = self.library.snapshot(request['id'])
        self.assertEqual(snapshot['collection_name'], '搬运')
        self.assertEqual([m['version_id'] for m in snapshot['items']],[first])
        self.assertEqual(submit(self.library,request)['status'],'done')
        with self.assertRaisesRegex(ValueError,'集合成员'):
            submit(self.library,dict(request,id='d'*32))
        with self.assertRaises(Forbidden):
            Library(self.service,self.bob).snapshot(request['id'])
        bundle = self.root / 'snapshot.zip'
        self.library.export(request['id'],1,bundle)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(json.loads(archive.read('manifest.json')),snapshot)
            self.assertEqual(archive.read('motions/'+first+'.npz'),
                             self.service.assets.artifact_path('alice',first).read_bytes())

    def test_snapshot_download_restores_a_missing_copy_only_from_identical_bytes(self):
        from gadget_imports import FIELDS, Imports
        from g2h import capacity
        from harness_service import HarnessService

        data = BytesIO()
        np.savez_compressed(data, **{name:self.arrays[name] for name in sorted(FIELDS)})
        imports = Imports(self.root)
        mid = imports.save('alice', '7'*32, 'Recoverable import', data.getvalue())['motion_id']
        cid = 'recovery-content'
        change(self.store, self.alice, dict(id=cid, motion_id=mid))
        library = Library(HarnessService(self.root, legacy_store=self.store), self.alice)
        sid = '8'*32
        legacy = BytesIO()
        np.savez_compressed(legacy, **{name:self.arrays[name] for name in sorted(FIELDS, reverse=True)})
        original_download = library.service.download
        self.assertNotEqual(original_download(self.alice, mid), legacy.getvalue())
        with patch.object(library.service, 'download', return_value=legacy.getvalue()):
            manifest = library.create_snapshot(dict(id=sid, collection_id=cid, name='Recovery snapshot',
                                                   expected_basis=library.collection_basis(cid)[2]))
        version = manifest['items'][0]['version_id']
        copy = library.service.assets.artifact_path('alice', version)
        target = self.root/'snapshot.zip'
        library.export(sid, 1, target)
        baseline = target.read_bytes()
        copy.unlink()
        changed = BytesIO()
        fields = {name:self.arrays[name] for name in sorted(FIELDS)}
        fields['native_transl'] = fields['native_transl'] + 1
        np.savez_compressed(changed, **fields)
        with patch.object(library.service, 'download', return_value=changed.getvalue()):
            with self.assertRaisesRegex(ValueError, '无法恢复'):
                library.export(sid, 1, target)
        self.assertEqual(target.read_bytes(),baseline)
        self.assertFalse(copy.exists())

        library.export(sid, 1, target)
        self.assertEqual(library.snapshot(sid),manifest)
        self.assertEqual(target.read_bytes(),baseline)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

    def test_snapshot_preview_recovers_the_exact_frozen_artifact(self):
        from gadget_imports import FIELDS, Imports
        from harness_service import HarnessService

        rig = self.root / 'third_party/HY-Motion-1.0/retargeting/smplh'
        rig.mkdir(parents=True)
        rest, parents, names = human_rig.definition()
        rest.astype(np.float32).tofile(rig / 'j_template.bin')
        parents.astype(np.int32).tofile(rig / 'kintree.bin')
        (rig / 'joint_names.json').write_text(json.dumps(names))
        data = BytesIO()
        np.savez_compressed(data, **{name: self.arrays[name] for name in sorted(FIELDS)})
        mid = Imports(self.root).save('alice', '1'*32, 'Preview recovery', data.getvalue())['motion_id']
        cid, sid = 'preview-recovery', '2'*32
        change(self.store, self.alice, dict(id=cid, motion_id=mid))
        service = HarnessService(self.root, legacy_store=self.store)
        library = Library(service, self.alice)
        manifest = library.create_snapshot(dict(id=sid, collection_id=cid, name='Preview recovery',
                                                expected_basis=library.collection_basis(cid)[2]))
        member, = manifest['items']
        version = member['version_id']
        path = service.assets.artifact_path('alice', version)
        original = path.read_bytes()
        path.unlink()
        handler = SimpleNamespace(server=SimpleNamespace(harness_service=service), send=lambda *args: args)
        route = f'/api/harness/motions/{version}/preview?snapshot={sid}&source={mid}'
        with self.assertRaises(Forbidden):
            handle(handler, urlsplit(route), False, self.bob)
        with self.assertRaises(Forbidden):
            handle(handler, urlsplit(route.replace(version, self.first['id'])), False, self.alice)
        with patch.object(service, 'download', return_value=b'different source'):
            with self.assertRaisesRegex(ValueError, '无法恢复'):
                handle(handler, urlsplit(route), False, self.alice)
        self.assertFalse(path.exists())
        status, clip = handle(handler, urlsplit(route), False, self.alice)
        self.assertEqual(status, 200, clip)
        self.assertEqual(clip['motion_id'], version)
        self.assertEqual(clip['frames'], member['frames'])
        self.assertEqual(path.read_bytes(), original)

    def test_new_motion_reconciles_an_interrupted_registration_and_retries(self):
        import select
        import subprocess
        import sys

        from g2h import capacity

        assets = self.service.assets
        owner_folder = assets.artifact_path('alice', self.first['id']).parent.parent
        original = set(owner_folder.iterdir())
        payload = self.root/'import.npz'
        np.savez_compressed(payload, **self.arrays)
        script = """
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch
from g2h import capacity
from g2h.motion_assets import MotionAssets

root, payload, request_id, publish = sys.argv[1:]
rename = os.replace
def interrupt(staged, destination):
    if publish == '1':
        rename(staged, destination)
    print('ready', flush=True)
    signal.pause()

with capacity.reservation(root, Path(payload).stat().st_size, 'test-registration'):
    with patch('os.replace', interrupt):
        MotionAssets(root).import_npz('alice', payload, request_id=request_id)
"""
        for published in (False, True):
            with self.subTest(published=published):
                request_id = 'retry-write-'+str(published)
                with subprocess.Popen([sys.executable, '-c', script, str(self.root), str(payload),
                                       request_id, str(int(published))],
                                      stdout=subprocess.PIPE, text=True) as worker:
                    try:
                        self.assertTrue(select.select([worker.stdout], [], [], 5)[0])
                        self.assertEqual(worker.stdout.readline().strip(), 'ready')
                        before = set(owner_folder.iterdir())
                        self.assertEqual(len(before-original), 1)
                        self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                        self.assertEqual(set(owner_folder.iterdir()), before)
                        self.assertEqual(capacity.status(self.root)['reserved_bytes'], payload.stat().st_size)
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                        worker.wait(timeout=5)
                self.assertIsNone(assets.find_request('alice', request_id))
                self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'], 0)
                self.assertEqual(set(owner_folder.iterdir()), original)
                saved = assets.import_npz('alice', payload, request_id=request_id)
                self.assertEqual(assets.import_npz('alice', payload, request_id=request_id), saved)
                reloaded, _ = assets.load('alice', saved['id'])
                np.testing.assert_array_equal(reloaded['native_transl'], self.arrays['native_transl'])
                original.add(owner_folder/saved['id'])

        # The marker may survive a worker that exited just after SQLite committed.
        destination = owner_folder/saved['id']
        marker = destination/capacity.PENDING_MOTION_ASSET
        marker.touch()
        self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
        self.assertFalse(marker.exists())
        self.assertEqual(assets.find_request('alice', request_id), saved)
        self.assertTrue(assets.artifact_path('alice', saved['id']).is_file())

    def test_snapshot_restore_reconciles_staging_after_a_worker_is_killed(self):
        import select
        import signal
        import subprocess
        import sys

        from g2h import capacity

        version = self.first['id']
        artifact = self.service.assets.artifact_path('alice', version)
        raw = artifact.read_bytes()
        source = self.root/'original.npz'
        source.write_bytes(raw)
        script = """
from pathlib import Path
import os
import signal
import sys
from unittest.mock import patch
from g2h.motion_assets import MotionAssets

root, version, source, publish = sys.argv[1:]
link = os.link
def pause(staged, destination):
    if publish == '1':
        link(staged, destination)
    print('ready', flush=True)
    signal.pause()

with patch('os.link', pause):
    MotionAssets(root).restore_missing_artifact('alice', version, Path(source).read_bytes())
"""
        for published in (False, True):
            with self.subTest(published=published):
                artifact.unlink(missing_ok=True)
                with subprocess.Popen([sys.executable, '-c', script, str(self.root), version,
                                       str(source), str(int(published))],
                                      stdout=subprocess.PIPE, text=True) as worker:
                    try:
                        self.assertTrue(select.select([worker.stdout], [], [], 5)[0])
                        self.assertEqual(worker.stdout.readline().strip(), 'ready')
                        before = set(artifact.parent.iterdir())
                        self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                        self.assertEqual(set(artifact.parent.iterdir()), before)
                        self.assertEqual(capacity.status(self.root)['reserved_bytes'], len(raw))
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                        worker.wait(timeout=5)
                    self.assertEqual(worker.returncode, -signal.SIGKILL)
                self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'], 0)
                expected = {'metadata.json', 'motion.npz'} if published else {'metadata.json'}
                self.assertEqual({path.name for path in artifact.parent.iterdir()}, expected)
                if published:
                    self.assertEqual(artifact.read_bytes(), raw)
                else:
                    self.service.assets.restore_missing_artifact('alice', version, raw)
                    self.assertEqual(artifact.read_bytes(), raw)

    def test_snapshot_export_preserves_previous_zip_if_source_changes_after_validation(self):
        first = self.first['id']
        second = self.service.assets.register('alice', self.arrays, description='Second motion')['id']
        cid = 'export-integrity'
        for motion in (first, second):
            change(self.store, self.alice, dict(id=cid, motion_id=motion))
        sid = 'a' * 32
        manifest = self.library.create_snapshot(dict(id=sid, collection_id=cid, name='Export integrity',
                                                     expected_basis=self.library.collection_basis(cid)[2]))
        bundle = self.root / 'existing.zip'
        self.library.export(sid, 1, bundle)
        previous = bundle.read_bytes()

        assets = self.service.assets
        find_artifact = assets.artifact_path
        changed = manifest['items'][-1]
        artifact = find_artifact('alice', changed['version_id'])
        raw = artifact.read_bytes()

        def alter_after_validation(owner, version_id):
            source = find_artifact(owner, version_id)
            if version_id == changed['version_id']:
                source.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            return source

        try:
            with patch.object(assets, 'artifact_path', side_effect=alter_after_validation):
                with self.assertRaisesRegex(ValueError, '文件校验失败'):
                    self.library.export(sid, 1, bundle)
            self.assertEqual(bundle.read_bytes(), previous)
            self.assertFalse(list(bundle.parent.glob('.snapshot-*')))
        finally:
            artifact.write_bytes(raw)

        self.library.export(sid, 1, bundle)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(archive.read('motions/' + changed['motion_id'] + '.npz'), raw)

    def test_snapshot_reports_an_unavailable_member_before_materializing_any_assets(self):
        from gadget_imports import FIELDS, Imports
        from personal_assets import BASE, handle as handle_assets
        from personal_snapshot_jobs import get, run

        cid = 'stale-source'
        change(self.store, self.alice, dict(id=cid, motion_id=self.first['id']))
        unavailable = 'mimp' + '7'*32
        data = BytesIO()
        np.savez(data, **{name:self.arrays[name] for name in FIELDS})
        imports = Imports(self.root)
        imports.save('alice', '7'*32, 'Missing source', data.getvalue())
        change(self.store, self.alice, dict(id=cid, motion_id=unavailable))
        _, path = imports.source(unavailable, 'alice')
        path.rename(path.with_suffix('.held'))
        source = self.library.collection_basis(cid)
        command = dict(id='8'*32, collection_id=cid, name='Source check', expected_basis=source[2])
        handler = SimpleNamespace(send=lambda status, data: data)
        with self.assertRaisesRegex(ValueError, unavailable):
            handle_assets(handler, self.service, self.alice, BASE+'/snapshot-source', {'collection':[cid]}, False)
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library,command)
        with patch.object(self.library, 'materialize', wraps=self.library.materialize) as materialize:
            run(self.library,command['id'])
            materialize.assert_not_called()
        job = get(self.library,command['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn(unavailable,job['error'])
        self.assertFalse((self.library.root/(command['id']+'.json.gz')).exists())

        change(self.store,self.alice,dict(action='remove', id=cid, motion_id=unavailable))
        preview = handle_assets(handler,self.service,self.alice,BASE+'/snapshot-source',{'collection':[cid]},False)
        self.assertEqual(preview['count'],1)
        manifest = self.library.create_snapshot(dict(command,id='9'*32,expected_basis=preview['basis']))
        self.assertEqual([item['motion_id'] for item in manifest['items']],[self.first['id']])

    def test_snapshot_worker_identifies_a_source_lost_after_member_validation(self):
        import errno

        from personal_snapshot_jobs import run

        cid = 'late-source-failure'
        mid = self.first['id']
        change(self.store, self.alice, dict(id=cid,motion_id=mid))
        expected_basis = self.library.collection_basis(cid)[2]
        artifact = self.service.assets.artifact_path('alice',mid)
        held = artifact.with_suffix('.held')
        validate = self.library.validate_snapshot_members
        request = dict(collection_id=cid,name='Late source failure',expected_basis=expected_basis)

        def remove_after_validation(members):
            validate(members)
            artifact.rename(held)

        with patch('personal_snapshot_jobs.schedule'):
            missing = dict(request,id='4'*32)
            submit(self.library,missing)
            try:
                with patch.object(self.library,'validate_snapshot_members',side_effect=remove_after_validation):
                    run(self.library,missing['id'])
            finally:
                held.rename(artifact)
            job = get(self.library,missing['id'])
            self.assertEqual(job['status'],'failed')
            self.assertIn(mid,job['error'])
            self.assertIn('不可用于快照',job['error'])

            denied = dict(request,id='5'*32)
            submit(self.library,denied)
            failure = PermissionError(errno.EACCES,'test server private path',str(artifact))
            with patch.object(self.service.assets,'artifact_path',side_effect=failure):
                run(self.library,denied['id'])
            job = get(self.library,denied['id'])
            self.assertEqual(job['status'],'failed')
            self.assertIn(mid,job['error'])
            self.assertNotIn(str(artifact),job['error'])
            self.assertNotIn('test server private path',job['error'])

            recovered = dict(request,id='6'*32)
            submit(self.library,recovered)
            run(self.library,recovered['id'])
        self.assertEqual(self.library.snapshot(recovered['id'])['items'][0]['motion_id'],mid)

    def test_snapshot_sync_failure_leaves_no_partial_manifest(self):
        from personal_snapshot_jobs import run

        cid='snapshot-sync-failure'
        change(self.store,self.alice,dict(id=cid,motion_id=self.first['id']))
        request=dict(id='7'*32,collection_id=cid,name='Disk failure',
                     expected_basis=self.library.collection_basis(cid)[2])
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library,request)
        with patch('personal_assets.os.fsync',side_effect=OSError('simulated sync failure')):
            run(self.library,request['id'])
        job=get(self.library,request['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('读取动作或写入快照失败',job['error'])
        self.assertEqual([path.name for path in self.library.root.iterdir()],['.snapshot.lock'])

        replacement=dict(request,id='8'*32)
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library,replacement)
        run(self.library,replacement['id'])
        self.assertEqual(get(self.library,replacement['id'])['status'],'done')

    def test_interrupted_snapshot_transfer_releases_temporary_capacity(self):
        from g2h import capacity

        cid='interrupted-transfer'
        change(self.store,self.alice,dict(id=cid,motion_id=self.first['id']))
        sid='9'*32
        self.library.create_snapshot(dict(id=sid,collection_id=cid,name='Interrupted transfer',
                                          expected_basis=self.library.collection_basis(cid)[2]))
        existing=set(self.library.root.iterdir())

        def interrupt(stream, filename):
            self.assertEqual(filename,f'kinora-snapshot-{sid[:8]}-1.zip')
            self.assertEqual(set(self.library.root.iterdir()),existing,'A running export must not leave a named ZIP')
            info=os.fstat(stream.fileno())
            self.assertEqual(info.st_nlink,0)
            self.assertGreaterEqual(capacity.status(self.root)['reserved_bytes'],info.st_size)
            raise BrokenPipeError('client disconnected')

        with self.assertRaisesRegex(BrokenPipeError,'client disconnected'):
            self.library.download_snapshot(SimpleNamespace(download_stream=interrupt),sid,1)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)
        self.assertEqual(set(self.library.root.iterdir()),existing)

    def test_snapshot_can_be_scheduled_again_after_executor_rejects_submission(self):
        from personal_snapshot_jobs import POOL

        collection_id = 'executor-retry'
        change(self.store, self.alice, dict(id=collection_id, motion_id=self.first['id']))
        sid = '1' * 32
        request = dict(id=sid, collection_id=collection_id, name='Retry dispatch',
                       expected_basis=self.library.collection_basis(collection_id)[2])
        submissions = []

        def enqueue(work, *args):
            submissions.append((work, args))
            if len(submissions) == 1:
                raise RuntimeError('executor could not start a worker')

        with patch.object(POOL, 'submit', side_effect=enqueue):
            self.assertEqual(submit(self.library, request)['status'], 'queued')
            self.assertEqual(get(self.library, sid)['status'], 'queued')
            self.assertEqual(len(submissions), 2)

            rejected, accepted = submissions
            rejected[0](*rejected[1])
            self.assertEqual(get(self.library, sid)['status'], 'queued')
            self.assertEqual(len(submissions), 2)

            accepted[0](*accepted[1])
            self.assertEqual(get(self.library, sid)['status'], 'done')
        self.assertEqual(self.library.snapshot(sid)['items'][0]['version_id'], self.first['id'])

    def test_snapshot_worker_retries_after_sqlite_write_conflict(self):
        from personal_snapshot_jobs import run

        cid = 'snapshot-write-conflict'
        change(self.store, self.alice, dict(id=cid, motion_id=self.first['id']))
        sid = '2' * 32
        request = dict(id=sid, collection_id=cid, name='Snapshot retry',
                       expected_basis=self.library.collection_basis(cid)[2])

        def fail_on_busy_database(*args, **kwargs):
            with closing(self.store.reviews()) as writer, writer, closing(self.store.reviews()) as contender:
                writer.execute('BEGIN IMMEDIATE')
                contender.execute('PRAGMA busy_timeout=0')
                contender.execute('BEGIN IMMEDIATE')

        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library, request)
            with patch.object(self.library, 'create_snapshot', side_effect=fail_on_busy_database):
                run(self.library, sid)
            self.assertEqual(get(self.library, sid)['status'], 'queued')
        change(self.store, self.alice, dict(id=cid, action='delete'))
        run(self.library, sid)
        self.assertEqual(get(self.library, sid)['status'], 'done')
        self.assertEqual(self.library.snapshot(sid)['items'][0]['version_id'], self.first['id'])

    def test_snapshot_worker_retries_a_stale_capacity_ledger_and_reports_a_full_disk(self):
        from gadget_imports import FIELDS, Imports
        from g2h import capacity, resource_quota
        from harness_service import HarnessService
        from personal_snapshot_jobs import run

        source = BytesIO()
        np.savez_compressed(source, **{name:self.arrays[name] for name in FIELDS})
        imports = Imports(self.root)
        library = Library(HarnessService(self.root, legacy_store=self.store), self.alice)
        for stale in (True, False):
            with self.subTest(stale=stale):
                mid = imports.save('alice', ('4' if stale else '5')*32, 'Snapshot source',
                                   source.getvalue())['motion_id']
                cid = 'stale-ledger' if stale else 'full-disk'
                change(self.store, self.alice, dict(id=cid, motion_id=mid))
                sid = ('a' if stale else 'b')*32
                command = dict(id=sid, collection_id=cid, name='Capacity snapshot',
                               expected_basis=library.collection_basis(cid)[2])
                with closing(resource_quota.connect(self.root)) as db:
                    charged = resource_quota.usage(db, 'alice', 'storage_bytes', time.time())
                with patch('personal_snapshot_jobs.schedule'):
                    submit(library, command)
                    if stale:
                        with closing(capacity.connect(self.root)) as db, db:
                            db.execute('UPDATE capacity SET sampled=0')
                        run(library, sid)
                        self.assertEqual(get(library, sid)['status'], 'queued')
                    else:
                        with patch('g2h.capacity.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                            run(library, sid)
                        job = get(library, sid)
                        self.assertEqual(job['status'], 'failed')
                        self.assertIn('实际数据盘剩余空间不足', job['error'])
                self.assertFalse((library.root/(sid+'.json.gz')).exists())
                self.assertEqual(capacity.status(self.root)['reserved_bytes'], 0)
                with closing(resource_quota.connect(self.root)) as db:
                    self.assertEqual(resource_quota.usage(db, 'alice', 'storage_bytes', time.time()), charged)
                self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                if not stale:
                    command = dict(command, id='c'*32)
                    with patch('personal_snapshot_jobs.schedule'):
                        submit(library, command)
                run(library, command['id'])
                self.assertEqual(get(library, command['id'])['status'], 'done')
                self.assertEqual(library.snapshot(command['id'])['items'][0]['motion_id'], mid)

    def test_snapshot_list_shows_queued_jobs_before_failures_and_filters_by_collection(self):
        from personal_assets import BASE, handle as handle_assets

        queued = 'a' * 32
        other = 'b' * 32
        with closing(self.store.reviews()) as db, db:
            db.executemany('INSERT INTO personal_snapshot_jobs VALUES(?,?,?,?,?,?,?,?,?)', [
                (queued, 'alice', 'Waiting', 'first', 'basis', '[]', 'queued', '', 0),
                (other, 'alice', 'Other queued', 'second', 'basis', '[]', 'queued', '', 1),
                ('c' * 32, 'bob', 'Private', 'first', 'basis', '[]', 'queued', '', 2),
                *((f'{i:032x}', 'alice', 'Failure', 'first', 'basis', None, 'failed', 'error', i)
                  for i in range(3, 25)),
            ])
        handler = SimpleNamespace(send=lambda status, data: (status, data))
        with patch('personal_snapshot_jobs.schedule'):
            _, all_collections = handle_assets(handler, self.service, self.alice, BASE + '/snapshots', {}, False)
            _, filtered = handle_assets(handler, self.service, self.alice, BASE + '/snapshots',
                                        {'collection': ['first']}, False)
        self.assertEqual([row['id'] for row in all_collections['jobs'][:2]], [other, queued])
        self.assertEqual(filtered['jobs'][0]['id'], queued)
        self.assertEqual(len(filtered['jobs']), 20)
        self.assertTrue(all(row['collection_id'] == 'first' for row in filtered['jobs']))
        self.assertNotIn('c' * 32, {row['id'] for row in filtered['jobs']})

    def test_snapshot_list_keeps_jobs_visible_once_during_publication(self):
        from personal_assets import BASE, handle as handle_assets
        from personal_snapshot_jobs import run

        with closing(self.store.reviews()) as db:
            db.execute('PRAGMA journal_mode=WAL')
        cid = 'snapshot-publication'
        change(self.store, self.alice, dict(id=cid, motion_id=self.first['id']))
        request = dict(id='3' * 32, collection_id=cid, name='Publishing snapshot',
                       expected_basis=self.library.collection_basis(cid)[2])
        sid = request['id']
        handler = SimpleNamespace(send=lambda status, data: data)
        fired = False
        library = self.library

        class InterleavedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                nonlocal fired
                if not fired and sql.startswith('SELECT * FROM personal_asset_snapshots WHERE owner=? AND seq<?'):
                    fired = True
                    run(library, sid)
                return result

        def list_snapshots():
            return handle_assets(handler, self.service, self.alice, BASE + '/snapshots', {}, False)

        with patch('personal_snapshot_jobs.schedule'):
            submit(library, request)
            with patch.object(self.store, 'reviews', side_effect=lambda: sqlite3.connect(
                    self.root / 'reviews.sqlite3', factory=InterleavedConnection)):
                during = list_snapshots()
            self.assertTrue(fired)
            self.assertEqual([row['id'] for row in during['jobs'] + during['items']], [sid])
            self.assertEqual([row['id'] for row in list_snapshots()['items']], [sid])

            interrupted = dict(request, id='4' * 32)
            submit(library, interrupted)
            library.create_snapshot(interrupted)
            recovered = list_snapshots()
            self.assertEqual(recovered['jobs'], [])
            self.assertEqual([row['id'] for row in recovered['items']], [interrupted['id'], sid])

    def test_snapshot_publication_releases_the_queue_slot_in_the_same_commit(self):
        cid = 'commit-snapshot'
        change(self.store, self.alice, dict(id=cid, motion_id=self.first['id']))
        request = dict(id='4'*32, collection_id=cid, name='Published', expected_basis=self.library.collection_basis(cid)[2])
        with patch('personal_snapshot_jobs.schedule'):
            for sid in ('4', '5'):
                submit(self.library, dict(request,id=sid*32))
            manifest = self.library.create_snapshot(request)
            self.assertEqual(get(self.library, request['id'])['status'], 'done')
            self.assertEqual(submit(self.library, dict(request,id='6'*32))['status'], 'queued')
        self.assertEqual(self.library.snapshot(request['id']), manifest)

    def test_snapshot_worker_recovers_an_already_saved_manifest(self):
        from personal_snapshot_jobs import run

        cid = 'recover-snapshot'
        change(self.store, self.alice, dict(id=cid, action='create', name='Recover'))
        change(self.store, self.alice, dict(id=cid, action='add', motion_id=self.first['id']))
        basis = self.library.collection_basis(cid)[2]
        request = dict(id='4'*32, collection_id=cid, name='Recovered', expected_basis=basis)
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library, request)
            manifest = self.library.create_snapshot(request)
            with closing(self.store.reviews()) as db, db:
                db.execute("UPDATE personal_snapshot_jobs SET status='queued', source=? WHERE id=?",
                           (encoded(self.library.collection_basis(cid)).decode(),request['id']))
            self.assertEqual(get(self.library, request['id'])['status'], 'queued')
        with patch.object(self.library, 'materialize', side_effect=AssertionError('Replayed saved snapshot')):
            run(self.library, request['id'])
        self.assertEqual(get(self.library, request['id'])['status'], 'done')
        self.assertEqual(self.library.snapshot(request['id']), manifest)

    def test_snapshot_recovery_advances_past_accounts_locked_by_another_worker(self):
        from contextlib import ExitStack
        from threading import Event, Thread
        from personal_snapshot_jobs import MAX_SCHEDULED, resume_pending

        def wait_for_jobs(job_ids):
            deadline = time.monotonic() + 4
            while True:
                with closing(self.store.reviews()) as db:
                    states = dict(db.execute('SELECT id,status FROM personal_snapshot_jobs'))
                if all(states[job] != 'queued' for job in job_ids) or time.monotonic() >= deadline:
                    return states
                time.sleep(.025)

        blocked = []
        with ExitStack() as locks:
            with patch('personal_snapshot_jobs.schedule'):
                for index in range((MAX_SCHEDULED + 1) // 2):
                    owner = 'blocked' + str(index)
                    with closing(sqlite3.connect(self.root / 'review/accounts.sqlite3')) as db, db:
                        db.execute('INSERT INTO users VALUES (?,?,0)', (owner, 'user'))
                    user = {'username': owner, 'role': 'user'}
                    library = Library(self.service, user)
                    locks.enter_context(library.write_lock('.snapshot.lock'))
                    motion = self.service.assets.register(owner, self.arrays)['id']
                    change(self.store, user, dict(id='recovery', motion_id=motion))
                    basis = library.collection_basis('recovery')[2]
                    for _ in range(2):
                        sid = f'{len(blocked) + 1:032x}'
                        submit(library, dict(id=sid, collection_id='recovery', name='Locked', expected_basis=basis))
                        blocked.append(sid)
                self.assertGreaterEqual(len(blocked), MAX_SCHEDULED)
                change(self.store, self.alice, dict(id='recovery', motion_id=self.first['id']))
                sid = 'f' * 32
                submit(self.library, dict(id=sid, collection_id='recovery', name='Available',
                                          expected_basis=self.library.collection_basis('recovery')[2]))

            stop = Event()
            worker = Thread(target=resume_pending, args=(self.store, stop), daemon=True)
            worker.start()
            try:
                states = wait_for_jobs([sid])
                self.assertEqual(states[sid], 'done')
                self.assertTrue(all(states[job] == 'queued' for job in blocked))
                self.assertEqual(self.library.snapshot(sid)['items'][0]['version_id'], self.first['id'])
                locks.close()
                states = wait_for_jobs(blocked)
                self.assertTrue(all(states[job] == 'done' for job in blocked), states)
            finally:
                stop.set()
                worker.join(timeout=3)

    def test_corrupt_source_finishes_snapshot_job_with_an_error(self):
        from personal_snapshot_jobs import run

        cid = 'damaged-source'
        source = 'm00000001'
        change(self.store, self.alice, dict(id=cid, action='create', name='Damaged source'))
        with patch('gadget_library.detail'), patch('personal_assets.require_motion_access'):
            change(self.store, self.alice, dict(id=cid, action='add', motion_id=source))
            basis = self.library.collection_basis(cid)[2]
            sid = '6' * 32
            request = dict(id=sid, collection_id=cid, name='Damaged snapshot', expected_basis=basis)
            with patch('personal_snapshot_jobs.schedule'):
                self.assertEqual(submit(self.library, request)['status'], 'queued')
            with patch.object(self.service, 'download', create=True, return_value=b'PK\x03\x04truncated') as download:
                run(self.library, sid)
            download.assert_called_once_with(self.alice, source)

        job = get(self.library, sid)
        self.assertEqual(job['status'], 'failed')
        self.assertIn(source, job['error'])
        self.assertIn('NPZ', job['error'])
        with self.assertRaises(Forbidden):
            self.library.snapshot(sid)
        self.assertIsNone(self.service.assets.find_request('alice', 'snapshot-source:' + source))

    def test_snapshot_recovery_reports_a_truncated_manifest(self):
        from personal_snapshot_jobs import run

        cid = 'truncated-manifest'
        change(self.store, self.alice, dict(id=cid, motion_id=self.first['id']))
        sid = '5' * 32
        request = dict(id=sid, collection_id=cid, name='Broken manifest',
                       expected_basis=self.library.collection_basis(cid)[2])
        with patch('personal_snapshot_jobs.schedule'):
            submit(self.library, request)
            self.library.create_snapshot(request)
            with closing(self.store.reviews()) as db, db:
                db.execute("UPDATE personal_snapshot_jobs SET status='queued', source=? WHERE id=?",
                           (encoded(self.library.collection_basis(cid)).decode(),sid))
        manifest = self.library.root / (sid + '.json.gz')
        manifest.write_bytes(manifest.read_bytes()[:-8])

        run(self.library, sid)

        job = get(self.library, sid)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('快照清单', job['error'])
        with self.assertRaisesRegex(ValueError, '快照清单'):
            self.library.export(sid, 1, self.root / 'broken.zip')
        self.assertFalse((self.root / 'broken.zip').exists())

    def test_routes_are_scoped(self):
        base = '/api/harness/personal-assets/'
        for endpoint, write in [('snapshot-source',False),('apply-pose',True),
                                ('snapshots/'+'a'*32+'/download',False)]:
            self.assertTrue(allowed(self.alice,base+endpoint,write))
        for endpoint, write in [('snapshot-source',True),('apply-pose',False),
                                ('objects/../../mesh',False)]:
            self.assertFalse(allowed(self.alice,base+endpoint,write))
        handler = SimpleNamespace(server=SimpleNamespace(harness_service=self.service),send=lambda *args:args)
        code, body = handle(handler,urlsplit(base+'motions'),False,self.alice)
        self.assertEqual(code,200)
        self.assertEqual(body['items'][0]['motion_id'],self.first['id'])


class HttpSnapshot(unittest.TestCase):
    def test_multi_scene_resume_keeps_the_admitted_actors_and_object_version(self):
        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        user = {'username': 'alice'}
        route = '/api/harness/multi-scene'
        assets_route = '/api/harness/personal-assets/objects'
        mesh = base64.b64encode(b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n').decode()
        fields = {'name': 'Shared prop', 'size_m': [1, 2, 1], 'anchors': [], 'heading_deg': 90}
        status, asset = fixture.call(assets_route, body={**fields, 'file': mesh})
        self.assertEqual(status, 201, asset)
        obj = {'id': 'object1-asset', 'asset_version': asset['id'], 'center_m': [3, 1, 0],
               'size_m': fields['size_m'], 'heading_deg': 90}
        value = {'request_id': 'multi-scene-resume', 'duration': 4, 'objects': [obj],
                 'actors': [{'id': 'A', 'label': 'First'}, {'id': 'B', 'label': 'Second'}]}

        def queue_rows():
            with closing(Custom(fixture.root).db()) as db:
                return [(row['id'], json.loads(row['scene'])) for row in
                        db.execute('SELECT id,scene FROM requests ORDER BY id')]

        execute = service.execute

        def stop_before_second(user, task_id, action, decision_id):
            if queue_rows():
                raise OSError('Simulated interruption before submitting the next actor')
            return execute(user, task_id, action, decision_id)

        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            with patch.object(service, 'execute', side_effect=stop_before_second):
                self.assertEqual(fixture.call(route, body=value)[0], 503)
            pending = service.tasks(user)['items']
            self.assertEqual(len(pending), 2)
            group_id, = [task['id'] for task in pending if task.get('scene_group')]
            code, grouped = fixture.call('/api/harness/tasks?limit=1&grouped=1')
            self.assertEqual(code, 200, grouped)
            self.assertEqual([task['id'] for task in grouped['items']], [group_id])
            code, group = fixture.call(f'/api/harness/tasks/{group_id}/scene-status', body={})
            self.assertEqual(code, 200, group)
            self.assertEqual([actor['status'] for actor in group['actors_group']], ['waiting', 'ready'])
            self.assertEqual(group['actors_group'][1]['operations'], [])
            self.assertTrue(group['scene_resume_available'])
            resume = f'/api/harness/tasks/{group_id}/scene-resume'
            self.assertEqual(fixture.call(resume, who='bob', body={})[0], 400)
            originally_queued, = queue_rows()
            self.assertEqual(originally_queued[1]['objects'], [obj])
            status, new_version = fixture.call(assets_route, body={**fields, 'previous': asset['id'],
                                                                   'heading_deg': 0})
            self.assertEqual(status, 201, new_version)

            service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
            changed = dict(value, actors=[*value['actors'], {'id': 'C', 'label': 'Extra'}])
            with patch.object(service, 'execute', wraps=service.execute) as runner:
                self.assertEqual(fixture.call(route, body=changed)[0], 400)
                runner.assert_not_called()
            self.assertEqual(queue_rows(), [originally_queued])
            self.assertEqual(len(service.tasks(user)['items']), 2)

            status, result = fixture.call(resume, body={})
            self.assertEqual(status, 202, result)
            self.assertFalse(result.get('scene_resume_available', False))
            self.assertEqual(result['id'], group_id)
            self.assertEqual(len(result['actors_group']), 2)
            queued = queue_rows()
            self.assertEqual(len(queued), 2)
            self.assertIn(originally_queued, queued)
            self.assertTrue(all(scene['objects'] == [obj] for _, scene in queued))
            self.assertEqual(fixture.call(resume, body={})[0], 202)
            self.assertEqual(queue_rows(), queued)
            status, replay = fixture.call(route, body=value)
            self.assertEqual((status, replay['id']), (202, group_id))
            self.assertEqual(queue_rows(), queued)
            status, restored = fixture.call(f'/api/harness/tasks/{group_id}/scene-status', body={})
            self.assertEqual(status, 200)
            self.assertEqual([a['label'] for a in restored['actors_group']], ['First', 'Second'])

            # A completed pre-upgrade group has metadata and operations but no bound hash.
            with service.store._db(True) as db:
                db.execute("UPDATE scene_groups SET meta=json_remove(meta,'$._request_hash','$._actor_requests') WHERE group_task=?",
                           (group_id,))
            self.assertEqual(fixture.call(resume, body={})[0], 400)
            changed_objects = [dict(obj, center_m=[4, 1, 0])]
            with patch.object(service, 'execute', wraps=service.execute) as runner:
                self.assertEqual(fixture.call(route, body=dict(value, objects=changed_objects))[0], 400)
                runner.assert_not_called()
            status, replay = fixture.call(route, body=value)
            self.assertEqual((status, replay['id']), (202, group_id))
            self.assertEqual(queue_rows(), queued)

    def test_scene_resume_recovers_receipt_written_before_operation(self):
        from g2h.generation_backend import KIMODO_MODEL
        from g2h.harness import Harness, LeaseLost
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        class ProcessExit(BaseException):
            pass

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        user = {'username': 'alice'}
        request = {'request_id': 'receipt-written-before-op', 'duration': 4, 'actors': [{'id': 'A'}, {'id': 'B'}]}
        advance = Harness.advance

        def exit_before_second_claim(harness, owner, task_id, **options):
            if not options.get('reconcile_only') and any(
                row['id'] != task_id and row.get('last_operation_status') for row in service.tasks(user)['items']
            ):
                raise ProcessExit
            return advance(harness, owner, task_id, **options)

        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            with patch.object(Harness, 'advance', new=exit_before_second_claim), self.assertRaises(ProcessExit):
                service.multi_scene(user, request)
            group_id, = [row['id'] for row in service.tasks(user)['items'] if row.get('scene_group')]
            with closing(sqlite3.connect(fixture.root / 'review/harness_requests.sqlite3')) as db:
                self.assertEqual({row[0] for row in db.execute('SELECT state FROM decisions')},
                                 {'recorded', 'uncertain'})
            service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
            route = f'/api/harness/tasks/{group_id}/scene-resume'
            child = service.store.scene_group('alice', group_id)['tasks'][1]
            live = service.store.claim('alice', child, 'original-submit', decision_owner='external-local')
            try:
                status, busy = fixture.call(route, body={})
                self.assertEqual(status, 202, busy)
                self.assertTrue(busy['scene_resume_available'])
                self.assertEqual(busy['actors_group'][1]['operations'], [])
            finally:
                with service.store._db(True) as db:
                    db.execute('UPDATE runs SET lease_until=0 WHERE id=?', (live['run_id'],))
            status, result = fixture.call(route, body={})
            self.assertEqual(status, 202, result)
            self.assertFalse(result.get('scene_resume_available', False))
            with self.assertRaises(LeaseLost):
                service.store.reserve_step(live)
            ops = [actor['operations'] for actor in result['actors_group']]
            self.assertEqual([len(values) for values in ops], [1, 1])
            self.assertEqual(fixture.call(route, body={})[0], 202)
            with closing(Custom(fixture.root).db()) as db:
                self.assertEqual({row['id'] for row in db.execute('SELECT id FROM requests')},
                                 {'hgen_' + values[0]['id'] for values in ops})

    def test_scene_reconciliation_recovers_an_unacknowledged_queue_write(self):
        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        class ProcessExit(BaseException):
            pass

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        user = {'username': 'alice'}
        submit = Custom.submit

        def queue_ids():
            with closing(Custom(fixture.root).db()) as db:
                return {row['id'] for row in db.execute('SELECT id FROM requests')}

        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            for committed in (False, True):
                with self.subTest(committed=committed):
                    before = queue_ids()

                    def interrupt(custom, *args, **kwargs):
                        if committed:
                            submit(custom, *args, **kwargs)
                        raise ProcessExit

                    value = {'request_id': f'scene-before-ack-{committed}', 'duration': 4,
                             'actors': [{'id': 'A'}]}
                    with patch.object(Custom, 'submit', new=interrupt), self.assertRaises(ProcessExit):
                        service.multi_scene(user, value)
                    group_id = service.tasks(user)['items'][0]['id']
                    op, = service.detail(user, group_id)['operations']
                    self.assertEqual((op['status'], op['handle']), ('submitting', None))
                    queue_id = 'hgen_' + op['id']
                    self.assertEqual(queue_ids() - before, {queue_id} if committed else set())
                    service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
                    route = f'/api/harness/tasks/{group_id}/scene-status'
                    if not committed:
                        with patch.object(service, '_scene_enabled', return_value=False):
                            status, disabled = fixture.call(route, body={})
                        self.assertEqual(status, 200, disabled)
                        self.assertEqual(disabled['operations'][0]['status'], 'unknown')
                        self.assertIsNone(disabled['operations'][0]['handle'])
                        self.assertEqual(queue_ids(), before)
                    status, result = fixture.call(route, body={})
                    self.assertEqual(status, 200, result)
                    reconciled, = result['operations']
                    self.assertEqual((reconciled['id'], reconciled['status']), (op['id'], 'waiting'))
                    self.assertEqual(queue_ids() - before, {queue_id})
                    self.assertEqual(fixture.call(route, body={})[0], 200)
                    self.assertEqual(queue_ids() - before, {queue_id})

                    if committed:
                        # A confirmed job missing from the queue requires readback,
                        # even when a status check is repeated after an interruption.
                        with closing(Custom(fixture.root).db()) as db, db:
                            db.execute('DELETE FROM requests WHERE id=?', (queue_id,))
                        for _ in range(2):
                            status, missing = fixture.call(route, body={})
                            self.assertEqual(status, 200, missing)
                            self.assertEqual(missing['operations'][0]['status'], 'unknown')
                            self.assertEqual(missing['operations'][0]['handle']['kind'], 'generate')
                            self.assertEqual(queue_ids(), before)

            before = queue_ids()
            with patch.object(Custom, 'submit', side_effect=ProcessExit), self.assertRaises(ProcessExit):
                service.multi_scene(user, {'request_id': 'cancel-scene-before-ack', 'duration': 4,
                                           'actors': [{'id': 'A'}]})
            group_id = service.tasks(user)['items'][0]['id']
            route = f'/api/harness/tasks/{group_id}'
            self.assertEqual(fixture.call(route + '/cancel', body={})[0], 200)
            status, cancelled = fixture.call(route + '/scene-status', body={})
            self.assertEqual((status, cancelled['status']), (200, 'cancelled'))
            self.assertEqual(queue_ids(), before)

    def test_scene_status_restores_committed_candidates_before_queue_progress_is_written(self):
        import struct

        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        custom = Custom(fixture.root)
        value = {'request_id': 'scene-candidates-before-ack', 'duration': 1,
                 'actors': [{'id': 'A'}, {'id': 'B'}]}
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            status, submitted = fixture.call('/api/harness/multi-scene', body=value)
            self.assertEqual(status, 202, submitted)
            keys = [a['operations'][0]['handle']['request_id'] for a in submitted['actors_group']]
            route = '/api/harness/tasks/' + submitted['id']
            custom.update(keys[0], status='running', done=0)
            with closing(custom.db()) as db:
                updated = db.execute('SELECT updated FROM requests WHERE id=?', (keys[0],)).fetchone()[0]
            status, pending = fixture.call(route + '/scene-status', body={})
            self.assertEqual(status, 200, pending)
            self.assertEqual(pending['operations'][0]['status'], 'waiting')
            with closing(custom.db()) as db:
                self.assertEqual(db.execute('SELECT updated FROM requests WHERE id=?', (keys[0],)).fetchone()[0], updated)
            source_ids = []
            for key, state in zip(keys, ('running', 'failed')):
                source_ids.append(commit_scene_candidate(custom, key))
                custom.update(key, status=state, done=0)
            class ProcessExit(BaseException):
                pass

            with patch.object(service.store, 'settle_operation', side_effect=ProcessExit), self.assertRaises(ProcessExit):
                service.scene_status({'username': 'alice'}, submitted['id'])
            saved, = service.assets.list('alice')
            self.assertEqual(service.detail({'username': 'alice'}, submitted['id'])['candidates'], [])
            fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
            status, restored = fixture.call(route + '/scene-status', body={})
            self.assertEqual(status, 200, restored)
            self.assertEqual([a['operations'][0]['status'] for a in restored['actors_group']],
                             ['succeeded', 'succeeded'])
            candidates = [a['candidates'][0]['motion_id'] for a in restored['actors_group']]
            self.assertEqual(candidates[0], saved['id'])
            self.assertEqual(len(fixture.server.harness_service.assets.list('alice')), 2)
            status, preview = fixture.call(route + '/multi-preview')
            self.assertEqual(status, 200, preview)
            self.assertEqual([a['motion_id'] for a in preview['actors']], candidates)
            self.assertTrue(preview['quality']['passed'], preview['quality'])
            for original, actor in zip(source_ids, preview['actors']):
                base = '/api/motions/' + original
                status, library = fixture.call(base + '/preview')
                self.assertEqual(status, 200, library)
                self.assertEqual(library['parents'], actor['parents'])
                self.assertEqual(library['names'], actor['names'])
                np.testing.assert_allclose(library['joints'], actor['joints'], atol=1e-5)
                status, thumbnail = fixture.call(base + '/thumbnail')
                self.assertEqual(status, 200, thumbnail)
                np.testing.assert_allclose(thumbnail['joints'], actor['joints'][15], atol=1e-5)

                request = urllib.request.Request(fixture.url + base + '/preview.bin', headers={
                    'Cookie': 'mf_session=' + fixture.tokens['alice'],
                })
                with urllib.request.urlopen(request, timeout=5) as response:
                    wire = response.read()
                self.assertEqual(wire[:8], b'MFPRV001')
                header, = struct.unpack('<I', wire[8:12])
                meta = json.loads(wire[12:12+header])
                self.assertEqual(meta['parents'], actor['parents'])
                data = np.frombuffer(wire, dtype='<f4', offset=12+header).reshape(30, 52, 3)
                np.testing.assert_array_equal(data, np.asarray(actor['joints'], dtype='<f4'))
            status, repeated = fixture.call(route + '/scene-status', body={})
            self.assertEqual(status, 200, repeated)
            self.assertEqual([a['candidates'][0]['motion_id'] for a in repeated['actors_group']], candidates)
            with closing(custom.db()) as db:
                self.assertEqual({(row['status'], row['done']) for row in db.execute('SELECT status,done FROM requests')},
                                 {('completed', 1)})

    def test_cancel_during_multi_scene_submission_returns_the_cancelled_group(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from g2h.generation_backend import KIMODO_MODEL
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        value = {'request_id': 'multi-cancel-before-settlement', 'duration': 4,
                 'actors': [{'id': 'A'}, {'id': 'B'}]}
        route = '/api/harness/multi-scene'
        queued = Event()
        proceed = Event()
        settle = service.store.settle_operation

        def pause_settlement(lease, operation_id, result):
            if result['state'] == 'waiting' and not queued.is_set():
                queued.set()
                if not proceed.wait(8):
                    raise RuntimeError('Cancellation did not release the paused submission')
            return settle(lease, operation_id, result)

        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}), \
             patch.object(service.store, 'settle_operation', side_effect=pause_settlement), \
             ThreadPoolExecutor(max_workers=1) as pool:
            submitted = pool.submit(fixture.call, route, body=value)
            try:
                self.assertTrue(queued.wait(4))
                group, = [t for t in service.tasks({'username': 'alice'})['items'] if t.get('scene_group')]
                group_id = group['id']
                status, stopped = fixture.call(f'/api/harness/tasks/{group_id}/cancel', body={})
                self.assertEqual(status, 200, stopped)
                self.assertEqual([actor['status'] for actor in stopped['actors_group']], ['cancelled', 'cancelled'])
            finally:
                proceed.set()
            status, response = submitted.result(timeout=5)
            self.assertEqual(status, 202, response)
            self.assertEqual((response['id'], response['status']), (group_id, 'cancelled'))
            self.assertEqual([actor['status'] for actor in response['actors_group']], ['cancelled', 'cancelled'])
            status, replay = fixture.call(route, body=value)
            self.assertEqual((status, replay['id'], replay['status']), (202, group_id, 'cancelled'))
            self.assertIsNot(replay.get('decision_receipt', {}).get('action_applied'), False)
            with closing(Custom(fixture.root).db()) as db:
                job, = db.execute('SELECT status,cancel FROM requests')
            self.assertEqual((job['status'], job['cancel']), ('cancelled', 1))
            status, response = fixture.call(f'/api/harness/tasks/{group_id}/scene-resume', body={})
            self.assertEqual((status, response['status']), (202, 'cancelled'))
            with closing(Custom(fixture.root).db()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)

            status, waiting = fixture.call(route, body=dict(value, request_id='multi-cancel-during-poll'))
            self.assertEqual(status, 202, waiting)
            group_route = f"/api/harness/tasks/{waiting['id']}"
            queued.clear()
            proceed.clear()
            polled = pool.submit(fixture.call, group_route + '/scene-status', body={})
            try:
                self.assertTrue(queued.wait(4))
                status, snapshot = fixture.call(group_route + '/scene-status', body={})
                self.assertEqual((status, snapshot['status']), (200, 'waiting'))
                self.assertEqual(fixture.call(group_route + '/cancel', body={})[0], 200)
            finally:
                proceed.set()
            status, response = polled.result(timeout=5)
            self.assertEqual((status, response['status']), (200, 'cancelled'))
            self.assertEqual([actor['status'] for actor in response['actors_group']], ['cancelled', 'cancelled'])

    def test_tall_personal_object_scene_queues_with_grounded_initial_pose(self):
        from g2h.generation_backend import KIMODO_MODEL
        from g2h.human_interaction import world_targets
        from harness_service import HarnessService
        from review_custom import Custom
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        mesh = base64.b64encode(b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n').decode()
        size = [.8, 19.87654321, 1.2]
        center_y = size[1] / 2
        anchor = [0, 1.1 - center_y, -.6]
        status, asset = fixture.call('/api/harness/personal-assets/objects', body={
            'name': 'Tall prop', 'file': mesh, 'size_m': size, 'heading_deg': 90,
            'anchors': [{'name': 'Handle', 'position_m': anchor}],
        })
        self.assertEqual(status, 201, asset)
        object_id = 'object1-asset'
        key = {'frame': 0, 'object_id': object_id, 'pelvis_offset_m': [0, .915 - center_y, -2.07],
               'heading_deg': 0, 'body_rotvecs': [[0, 0, 0]] * 21}
        scene = {
            'kind': 'human_interaction_v1',
            'objects': [{'id': object_id, 'asset_version': asset['id'], 'center_m': [1, center_y, 2],
                         'size_m': size, 'heading_deg': 90}],
            'waypoints': [{'frame': 0, 'xz_m': [-1.07, 2], 'heading_deg': 90}],
            'keyframes': [key],
            'contacts': [{'start_frame': 71, 'end_frame': 120, 'object_id': object_id,
                          'joint_index': 21, 'anchor_m': anchor, 'tolerance_m': .08}],
        }
        route = '/api/harness/scene'
        request = {'request_id': 'tall-prop-scene', 'duration': 4, 'scene': scene,
                   'text': 'A person approaches the selected object and touches the marked spot.'}
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
                                     'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}), \
             patch('harness_service.kimodo_readiness', return_value={'available': True}), \
             patch('g2h.generation_backend.kimodo_readiness', return_value={'available': True}):
            status, task = fixture.call(route, body=request)
            self.assertEqual(status, 202, task)
            self.assertEqual(task['operations'][0]['status'], 'waiting')
            with closing(Custom(fixture.root).db()) as db:
                queued, = db.execute('SELECT scene FROM requests')
            saved = json.loads(queued['scene'])
            self.assertEqual(saved, scene)
            np.testing.assert_allclose(world_targets(saved, 120)[1][0], [-1.07, .915, 2])

            for frame, world_height in ((0, -6), (29, .915)):
                with self.subTest(frame=frame, world_height=world_height):
                    key['frame'] = frame
                    key['pelvis_offset_m'][1] = world_height - center_y
                    request['request_id'] = f'tall-prop-invalid-{frame}'
                    status, error = fixture.call(route, body=request)
                    self.assertEqual(status, 400, error)
                    self.assertIn('无效的物体相对人体关键帧', error['error'])

    def test_new_motion_storage_is_settled_without_reopening_the_committed_output(self):
        from g2h import resource_quota
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root,legacy_store=fixture.store)
        fixture.server.harness_service = service
        assets = service.assets
        base = '/api/harness/personal-assets/'
        arrays, raw = motion_fixture()
        source = assets.register('alice',arrays)
        mapping, _ = bvh_import.mapping(bvh_import.read(raw)[0])
        file = base64.b64encode(raw).decode()
        status, rig = fixture.call(base+'rigs',body=dict(name='Rig',file=file,mapping=mapping,
                                                        units=.01,up='+Y',forward='+Z'))
        self.assertEqual(status,201,rig)
        status, pose = fixture.call(base+'poses',body=dict(name='Pose',motion_id=source['id'],frame=9))
        self.assertEqual(status,201,pose)

        def used_storage():
            return resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used']

        for route, payload in (
            ('import-bvh',dict(id='a'*32,name='Imported',rig=rig['id'],file=file)),
            ('apply-pose',dict(id='b'*32,pose=pose['id'],motion_id=source['id'],frame=12,blend=3)),
        ):
            with self.subTest(route=route):
                before = used_storage()
                get = assets.get

                def output_unavailable(owner,motion_id):
                    if motion_id != source['id']:
                        raise OSError(errno.EIO,'simulated post-commit read failure')
                    return get(owner,motion_id)

                with patch.object(assets,'get',side_effect=output_unavailable):
                    status, saved = fixture.call(base+route,body=payload)
                self.assertEqual(status,201,saved)
                self.assertEqual(fixture.call(base+route,body=payload),(201,saved))
                actual_size = assets.artifact_size('alice',saved['id'])
                self.assertEqual(used_storage(),before+actual_size)
                with assets._db() as db:
                    db.execute("UPDATE assets SET metadata=json_remove(metadata,'$.artifact_bytes') WHERE id=?",
                               (saved['id'],))
                saved.pop('artifact_bytes')
                self.assertEqual(fixture.call(base+route,body=payload),(201,saved))
                self.assertEqual(used_storage(),before+actual_size)

    def test_media_upload_rolls_back_new_source_when_database_commit_fails(self):
        from g2h import capacity, resource_quota
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root,legacy_store=fixture.store)
        fixture.server.harness_service = service
        library = Library(service,{'username':'alice'})
        base = '/api/harness/personal-assets/'
        mesh = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        request = dict(id='c'*32,name='OBJ',size_m=[1,1,1],heading_deg=0,anchors=[],
                       file=base64.b64encode(mesh).decode())

        class CommitBusy(sqlite3.Connection):
            def __exit__(self, kind, value, trace):
                if kind is None and self.in_transaction:
                    self.rollback()
                    raise sqlite3.OperationalError('database is locked')
                return super().__exit__(kind,value,trace)

        def used_storage():
            return resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used']

        def simulate_busy():
            return patch.object(fixture.store,'reviews',side_effect=lambda: sqlite3.connect(
                fixture.store.path,timeout=10,factory=CommitBusy))

        def verify_retry(endpoint, payload, source, failure, *, motion=False):
            route = base+endpoint
            list_route = base+'motions' if motion else route
            previous = fixture.call(list_route)[1]['items']
            before = used_storage()
            with failure:
                status, error = fixture.call(route,body=payload)
            self.assertEqual(status,503,error)
            self.assertEqual(fixture.call(list_route)[1]['items'],previous)
            filename = hashlib.sha256(source).hexdigest()+('.obj' if endpoint=='objects' else '.bvh')
            self.assertFalse((library.root/filename).exists())
            self.assertEqual(used_storage(),before)
            self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
            self.assertEqual(capacity.status(fixture.root)['reserved_bytes'],0)
            status, saved = fixture.call(route,body=payload)
            self.assertEqual(status,201,saved)
            self.assertEqual(fixture.call(route,body=payload),(201,saved))
            artifact_size = service.assets.artifact_size('alice',saved['id']) if motion else 0
            self.assertEqual(used_storage(),before+len(source)+artifact_size)
            return saved

        verify_retry('objects', request, mesh, simulate_busy())
        _, raw = motion_fixture()
        mapping, _ = bvh_import.mapping(bvh_import.read(raw)[0])
        rig = verify_retry('rigs', dict(id='d'*32,name='BVH rig',file=base64.b64encode(raw).decode(),
                                      mapping=mapping,units=.01,up='+Y',forward='+Z'), raw, simulate_busy())
        assets = service.assets

        class RegisterBusy(sqlite3.Connection):
            def commit(self):
                if self.in_transaction:
                    raise sqlite3.OperationalError('database is locked')
                return super().commit()

        def failed_registration():
            db = sqlite3.connect(assets.db_path,timeout=5,factory=RegisterBusy)
            db.row_factory = sqlite3.Row
            return closing(db)

        # Use the same skeleton but different source bytes so import must retain its own BVH.
        motion_raw = raw.replace(b'MOTION\n',b'MOTION \n',1)
        self.assertNotEqual(motion_raw,raw)
        verify_retry('import-bvh',dict(id='e'*32,name='BVH motion',rig=rig['id'],
                                      file=base64.b64encode(motion_raw).decode()), motion_raw,
                     patch.object(assets,'_db',side_effect=failed_registration), motion=True)

    def test_media_upload_does_not_keep_an_uncharged_source_after_failed_sync(self):
        from g2h import capacity, resource_quota
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root,legacy_store=fixture.store)
        fixture.server.harness_service = service
        library = Library(service,{'username':'alice'})
        route = '/api/harness/personal-assets/objects'
        mesh = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        sync = os.fsync

        def used_storage():
            return resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used']

        for index, error_number in enumerate((errno.ENOSPC, errno.EDQUOT)):
            with self.subTest(error_number=error_number):
                raw = mesh + f'# {index}\n'.encode()
                request = dict(id=f'{index:032x}',name='OBJ',size_m=[1,1,1],heading_deg=0,anchors=[],
                               file=base64.b64encode(raw).decode())
                before = used_storage()

                def fail_directory_sync(descriptor):
                    if os.path.samestat(os.fstat(descriptor),library.root.stat()):
                        raise OSError(error_number,'storage fault')
                    return sync(descriptor)

                with patch('personal_assets.os.fsync',side_effect=fail_directory_sync):
                    status, error = fixture.call(route,body=request)
                self.assertEqual(status,503,error)
                self.assertEqual(len(fixture.call(route)[1]['items']),index)
                self.assertEqual(len(list(library.root.glob('*.obj'))),index)
                self.assertEqual(used_storage(),before)
                self.assertIn('空间不足',error['error'])
                self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                self.assertEqual(capacity.status(fixture.root)['reserved_bytes'],0)
                status, saved = fixture.call(route,body=request)
                self.assertEqual(status,201,saved)
                self.assertEqual(used_storage(),before+len(raw))
                self.assertEqual(fixture.call(route,body=request),(201,saved))
                self.assertEqual(used_storage(),before+len(raw))

    def test_media_uploads_report_disk_pressure_and_allow_the_same_request_to_retry(self):
        from g2h import capacity, resource_quota
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.server.harness_service = HarnessService(fixture.root,legacy_store=fixture.store)
        capacity.ensure(fixture.root)
        base = '/api/harness/personal-assets/'

        def used_storage():
            return resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used']

        def upload(route, request):
            before = used_storage()
            with patch('g2h.capacity.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
                status, error = fixture.call(base+route,body=request)
            self.assertEqual(status,503,error)
            self.assertIn('实际数据盘剩余空间不足',error['error'])
            self.assertEqual(used_storage(),before)
            self.assertEqual(capacity.status(fixture.root)['reserved_bytes'],0)
            status, asset = fixture.call(base+route,body=request)
            self.assertEqual(status,201,asset)
            self.assertGreater(used_storage(),before)
            used = used_storage()
            self.assertEqual(fixture.call(base+route,body=request),(201,asset))
            self.assertEqual(used_storage(),used)
            return asset

        mesh = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        upload('objects',dict(id='e'*32,name='OBJ',size_m=[1,1,1],heading_deg=0,anchors=[],
                              file=base64.b64encode(mesh).decode()))
        _, raw = motion_fixture()
        mapping, _ = bvh_import.mapping(bvh_import.read(raw)[0])
        bvh_file = base64.b64encode(raw).decode()
        rig = upload('rigs',dict(id='d'*32,name='BVH rig',file=bvh_file,mapping=mapping,
                                 units=.01,up='+Y',forward='+Z'))
        upload('import-bvh',dict(id='f'*32,name='BVH motion',rig=rig['id'],file=bvh_file))

    def test_obj_preview_and_new_revisions_require_a_valid_source(self):
        from g2h import resource_quota
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root,legacy_store=fixture.store)
        fixture.server.harness_service = service
        raw = b'v 0 0 0\nv 1 0 0\nv 0 1 1\nf 1 2 3\n'
        encoded_file = base64.b64encode(raw).decode()
        route = '/api/harness/personal-assets/objects'
        fields = dict(name='Recoverable OBJ',size_m=[1,1,1],heading_deg=0,anchors=[])
        status, asset = fixture.call(route,body=dict(fields,id='a'*32,file=encoded_file))
        self.assertEqual(status,201,asset)
        source = Library(service,{'username':'alice'}).root/(asset['data']['mesh_sha256']+'.obj')
        mesh_route = f"{route}/{asset['id']}/mesh"
        before = resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used']
        edit = dict(fields,previous=asset['id'],id='b'*32,name='Updated object')
        for issue in ('缺失','校验失败'):
            with self.subTest(issue=issue):
                if issue == '缺失':
                    source.unlink()
                else:
                    source.write_bytes(b'truncated OBJ')
                status, error = fixture.call(mesh_route)
                self.assertEqual(status,400)
                self.assertIn(issue,error['error'])
                self.assertIn('重新上传 OBJ',error['error'])
                self.assertEqual(fixture.call(mesh_route,'bob')[0],403)
                status, error = fixture.call(route,body=edit)
                self.assertEqual(status,400)
                self.assertIn('重新上传 OBJ',error['error'])
                self.assertEqual(fixture.call(route,body=dict(fields,id='a'*32,file=encoded_file)),(201,asset))
                self.assertEqual(fixture.call(mesh_route)[1]['faces'],[[0,1,2]])
        source.unlink()
        status, revised = fixture.call(route,body=dict(edit,file=encoded_file))
        self.assertEqual(status,201,revised)
        self.assertEqual(revised['family'],asset['family'])
        self.assertEqual(fixture.call(mesh_route)[1]['faces'],[[0,1,2]])
        self.assertEqual(resource_quota.snapshot(fixture.root,'alice')['resources']['storage_bytes']['used'],before)

    def test_live_capacity_scan_preserves_the_bvh_upload_file(self):
        from g2h import capacity
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root,legacy_store=fixture.store)
        fixture.server.harness_service = service
        _, raw = motion_fixture()
        mapping, _ = bvh_import.mapping(bvh_import.read(raw)[0])
        request = dict(id='4'*32,name='BVH during capacity scan',file=base64.b64encode(raw).decode(),
                       mapping=mapping,units=.01,up='+Y',forward='+Z')
        rename = os.replace
        scanned = []

        def scan_before_publish(staged, destination):
            if Path(destination).suffix == '.bvh':
                self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                scanned.append(Path(staged).is_file())
            return rename(staged,destination)

        route = '/api/harness/personal-assets/rigs'
        with patch('personal_assets.os.replace',side_effect=scan_before_publish):
            code, rig = fixture.call(route,body=request)
        self.assertEqual(scanned,[True])
        self.assertEqual(code,201,rig)
        self.assertEqual(fixture.call(route,body=request),(201,rig))

    def test_collection_changes_invalidate_snapshot_confirmation_and_export_pages(self):
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        owner = {'username':'alice','role':'user'}
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        fixture.server.harness_service = service
        arrays, _ = motion_fixture()
        first = service.assets.register('alice', arrays, description='First')['id']
        second = service.assets.register('alice', arrays, description='Second')['id']
        cid, sid = 'changing-collection', '9'*32
        change(fixture.store, owner, dict(id=cid, motion_id=first))
        root = '/api/harness/personal-assets'
        source_path = f'{root}/snapshot-source?collection={cid}'
        collection_path = f'/api/library/collections?id={cid}'
        status, initial = fixture.call(source_path)
        self.assertEqual(status, 200)
        initial_page = fixture.call(collection_path)[1]
        change(fixture.store, owner, dict(id=cid, motion_id=second))
        command = dict(id=sid, collection_id=cid, name='Collection confirmation', expected_basis=initial['basis'])
        with patch('personal_snapshot_jobs.schedule'):
            status, conflict = fixture.call(root+'/snapshots', body=command)
            self.assertEqual(status, 409, conflict)
            self.assertEqual(fixture.call(f'{root}/snapshot-jobs/{sid}')[0], 403)
            self.assertEqual(initial_page['basis'], initial['basis'])
            after = initial_page['items'][0]['motion_id']
            self.assertEqual(fixture.call(f'{collection_path}&after={after}&expected_basis={initial["basis"]}')[0], 409)
            status, latest = fixture.call(source_path)
            self.assertEqual((status, latest['count']), (200, 2))
            command['expected_basis'] = latest['basis']
            self.assertEqual(fixture.call(root+'/snapshots', body=command)[0], 202)
            change(fixture.store, owner, dict(id=cid, action='remove', motion_id=second))
            self.assertEqual(fixture.call(root+'/snapshots', body=command)[0], 202)

    def test_snapshot_download_reports_storage_failures_without_leaking_paths(self):
        from g2h import capacity
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        owner = {'username':'alice','role':'user'}
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        fixture.server.harness_service = service
        library = Library(service, owner)
        arrays, _ = motion_fixture()
        mid = service.assets.register('alice', arrays)['id']
        cid = 'download-storage'
        change(fixture.store, owner, dict(id=cid,motion_id=mid))
        sid = '8'*32
        library.create_snapshot(dict(id=sid,collection_id=cid,name='Storage permissions',
                                     expected_basis=library.collection_basis(cid)[2]))
        route = f'/api/harness/personal-assets/snapshots/{sid}/download?batch=1'
        artifact = service.assets.artifact_path('alice',mid)
        manifest = library.root/(sid+'.json.gz')
        for source in (artifact, manifest):
            with self.subTest(file=source.name):
                mode = source.stat().st_mode & 0o777
                source.chmod(0)
                try:
                    code, response = fixture.call(route)
                    self.assertEqual(code,503)
                    self.assertIn('稍后重试',response['error'])
                    self.assertNotIn(str(source),response['error'])
                    self.assertEqual(fixture.call(route,'bob')[0],403)
                finally:
                    source.chmod(mode)
                self.assertEqual(capacity.status(fixture.root)['reserved_bytes'],0)

        original = artifact.read_bytes()
        try:
            artifact.unlink()
            code, response = fixture.call(route)
            self.assertEqual(code, 410)
            self.assertIn(mid, response['error'])
            self.assertIn('文件已丢失', response['error'])
            artifact.write_bytes(b'unreadable motion')
            code, response = fixture.call(route)
            self.assertEqual(code, 410)
            self.assertIn(mid, response['error'])
            self.assertIn('文件已损坏', response['error'])
            self.assertEqual(capacity.status(fixture.root)['reserved_bytes'], 0)
        finally:
            artifact.write_bytes(original)

        request = urllib.request.Request(fixture.url+route,
                                         headers={'Cookie':'mf_session='+fixture.tokens['alice']})
        with urllib.request.urlopen(request,timeout=5) as response:
            with zipfile.ZipFile(BytesIO(response.read())) as archive:
                self.assertEqual(archive.read('motions/'+mid+'.npz'),artifact.read_bytes())

    def test_snapshot_download_reports_unrecoverable_batch_without_blocking_other_batches(self):
        from g2h import capacity
        from gadget_imports import FIELDS, Imports
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        owner = {'username': 'alice', 'role': 'user'}
        service = fixture.server.harness_service = HarnessService(fixture.root, legacy_store=fixture.store)
        library = Library(service, owner)
        arrays, _ = motion_fixture()
        imported = BytesIO()
        np.savez_compressed(imported, **{name: arrays[name] for name in sorted(FIELDS)})
        imports = Imports(fixture.root)
        missing = imports.save('alice', '3'*32, 'Unavailable original', imported.getvalue())['motion_id']
        available = service.assets.register('alice', arrays)['id']
        cid, sid = 'two-batches', '9'*32
        for mid in (missing, available):
            change(fixture.store, owner, dict(id=cid, motion_id=mid))
        with patch('personal_assets.BATCH_SIZE', 1):
            manifest = library.create_snapshot(dict(id=sid, collection_id=cid, name='Two batches',
                                                    expected_basis=library.collection_basis(cid)[2]))
        lost_index = next(i for i, item in enumerate(manifest['items']) if item['motion_id'] == missing)
        failed_batch = lost_index + 1
        frozen = service.assets.artifact_path('alice', manifest['items'][lost_index]['version_id'])
        frozen.unlink()
        _, source = imports.source(missing, 'alice')
        source.write_bytes(b'different original file')
        route = f'/api/harness/personal-assets/snapshots/{sid}/download?batch='

        status, error = fixture.call(route + str(failed_batch))
        self.assertEqual(status, 410, error)
        self.assertIn(missing, error['error'])
        self.assertIn('无法恢复', error['error'])
        self.assertFalse(frozen.exists())
        self.assertEqual(capacity.status(fixture.root)['reserved_bytes'], 0)
        request = urllib.request.Request(fixture.url + route + str(3 - failed_batch),
                                         headers={'Cookie': 'mf_session=' + fixture.tokens['alice']})
        with urllib.request.urlopen(request, timeout=5) as response:
            with zipfile.ZipFile(BytesIO(response.read())) as archive:
                self.assertEqual(archive.read(f'motions/{available}.npz'),
                                 service.assets.artifact_path('alice', available).read_bytes())
        deadline = time.monotonic() + 3
        while capacity.status(fixture.root)['reserved_bytes'] and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(capacity.status(fixture.root)['reserved_bytes'], 0)

    def test_capacity_scan_reclaims_unpublished_manifests_after_worker_exit(self):
        import select
        import subprocess
        import sys

        from g2h import capacity
        from harness_service import HarnessService
        from test_review_server import ReviewTests

        fixture = ReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        user = {'username':'alice','role':'user'}
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        library = Library(service, user)
        arrays, _ = motion_fixture()
        mid = service.assets.register('alice', arrays)['id']
        cid = 'manifest-crash'
        change(fixture.store, user, dict(id=cid,motion_id=mid))
        script = """
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch
from harness_service import HarnessService
from personal_assets import Library
from review_server import Store

root, config, rig, payload, publish = sys.argv[1:]
store = Store(Path(root), Path(config), Path(rig))
library = Library(HarnessService(Path(root), legacy_store=store), {'username':'alice','role':'user'})
rename = os.replace
def pause(staged, destination):
    if publish == '1':
        rename(staged, destination)
    print('ready', flush=True)
    signal.pause()

with patch('os.replace', pause):
    library.create_snapshot(json.loads(payload))
"""
        request = dict(collection_id=cid,name='Retry publication',expected_basis=library.collection_basis(cid)[2])
        for published in (False, True):
            with self.subTest(published=published):
                sid = ('8' if published else '9')*32
                snapshot = dict(request,id=sid)
                self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                existing = set(library.root.iterdir())
                with subprocess.Popen([sys.executable,'-c',script,str(fixture.root),str(fixture.config),
                                       str(fixture.assets),json.dumps(snapshot),str(int(published))],
                                      stdout=subprocess.PIPE,text=True) as worker:
                    try:
                        self.assertTrue(select.select([worker.stdout],[],[],5)[0])
                        self.assertEqual(worker.stdout.readline().strip(),'ready')
                        before = set(library.root.iterdir())
                        self.assertEqual(len(before-existing),1)
                        self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                        self.assertEqual(set(library.root.iterdir()),before)
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                        worker.wait(timeout=5)
                self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                self.assertEqual(set(library.root.iterdir()),existing)
                manifest = library.create_snapshot(snapshot)
                self.assertTrue(capacity.Reconciler(fixture.root).step(seconds=3))
                self.assertEqual(library.snapshot(sid),manifest)

    def test_server_start_resumes_queued_snapshot_without_a_status_request(self):
        from threading import Thread
        from harness_service import HarnessService
        from review_auth import Auth
        from review_server import Handler as ReviewHandler, Server
        from test_review_server import ReviewTests

        fixture = ReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        Auth(fixture.store.review_dir).create('alice', 'snapshot-fixture-password')
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        user = {'username': 'alice', 'role': 'user'}
        library = Library(service, user)
        arrays, _ = motion_fixture()
        asset = service.assets.register('alice', arrays)
        change(fixture.store, user, dict(id='recovery', motion_id=asset['id']))
        sid = 'a' * 32
        request = dict(id=sid, collection_id='recovery', name='Queued before restart',
                       expected_basis=library.collection_basis('recovery')[2])
        with patch('personal_snapshot_jobs.schedule'):
            self.assertEqual(submit(library, request)['status'], 'queued')

        server = Server(('127.0.0.1', 0), ReviewHandler)
        server.store = fixture.store
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        deadline = time.monotonic() + 3
        while True:
            with closing(fixture.store.reviews()) as db:
                status, error = db.execute('SELECT status,error FROM personal_snapshot_jobs WHERE id=?', (sid,)).fetchone()
            if status != 'queued' or time.monotonic() >= deadline:
                break
            time.sleep(.025)
        self.assertEqual(status, 'done', error)
        self.assertEqual(library.snapshot(sid)['items'][0]['version_id'], asset['id'])

    def test_bvh_upload_and_immutable_zip_over_authenticated_http(self):
        from g2h import capacity
        from harness_service import HarnessService
        from review_server import Handler
        from test_review_access import AccessTests
        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        fixture.server.harness_service = service
        arrays, raw = motion_fixture()
        body = {'file':base64.b64encode(raw).decode()}
        route = '/api/harness/personal-assets'
        self.assertEqual(fixture.call(route+'/motions','unknown')[0],401)
        status, skeleton = fixture.call(route+'/inspect-bvh',body=body)
        self.assertEqual(status,200)
        self.assertEqual(skeleton['frames'],len(arrays['native_transl']))
        self.assertTrue(skeleton['rig_save_request_id'])
        rig_request = dict(body, id='d'*32, name='HTTP rig', mapping=skeleton['mapping'],
                           units=.01,up='+Y',forward='+Z')
        status, rejected = fixture.call(route+'/rigs',body=dict(rig_request, mapping={}))
        self.assertEqual(status,400,rejected)
        self.assertIn('缺少必要关节', rejected['error'])
        status, rig = fixture.call(route+'/rigs',body=rig_request)
        self.assertEqual(status,201,rig)
        self.assertEqual(fixture.call(route+'/rigs',body=rig_request), (201, rig))
        query = '?signature='+skeleton['signature']
        self.assertEqual(fixture.call(route+'/rigs'+query)[1]['items'][0]['id'],rig['id'])
        self.assertEqual(fixture.call(route+'/rigs'+query,'bob')[1]['items'],[])
        import_request = dict(body,name='资产',rig=rig['id'],id='e'*32)
        status, motion = fixture.call(route+'/import-bvh',body=import_request)
        self.assertEqual(status,201,motion)
        mid = motion['id']
        self.assertEqual(fixture.call(route+'/import-bvh',body=import_request)[1]['id'],mid)
        self.assertEqual(fixture.call(route+'/import-bvh','bob',body=import_request)[0],403)
        self.assertEqual(fixture.call(route+'/motions','bob')[1]['items'],[])
        self.assertEqual(fixture.call('/api/harness/motions/'+mid+'/preview')[1]['frames'],skeleton['frames'])
        imported, _ = service.assets.load('alice',mid)
        np.testing.assert_allclose(imported['native_transl'],arrays['native_transl'],atol=2e-6)
        pose_request = dict(id='2'*32, name='HTTP pose', motion_id=mid, frame=3)
        status, pose = fixture.call(route+'/poses', body=pose_request)
        self.assertEqual(status, 201)
        self.assertEqual(fixture.call(route+'/poses', body=pose_request), (201, pose))
        self.assertEqual(fixture.call('/api/library/collections',body=dict(action='add',id='test',motion_id=mid))[0],200)
        status, record = fixture.call(route+'/snapshot-source?collection=test')
        self.assertEqual(status,200)
        sid = 'f'*32
        value = dict(id=sid,collection_id='test',name='HTTP snapshot',expected_basis=record['basis'])
        self.assertEqual(fixture.call(route+'/snapshots',body=value)[0],202)
        for _ in range(100):
            status, job = fixture.call(route+'/snapshot-jobs/'+sid)
            self.assertEqual(status,200)
            if job['status'] != 'queued':
                break
            time.sleep(.025)
        self.assertEqual(job['status'],'done',job)
        self.assertEqual(fixture.call(route+'/snapshots/'+sid,'bob')[0],403)
        url = fixture.url + route + '/snapshots/' + sid + '/download?batch=1'
        headers = {'Cookie': 'mf_session=' + fixture.tokens['alice']}
        request = urllib.request.Request(url, headers=headers)
        original_download = Handler.download_stream
        with patch('g2h.capacity.shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
             patch.object(Handler, 'download_stream', autospec=True, side_effect=original_download) as stream:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(request,timeout=5)
            with failure.exception as response:
                self.assertEqual(response.status,503)
                self.assertIn('实际数据盘剩余空间不足',json.load(response)['error'])
            stream.assert_not_called()

        def stream_with_reservation(handler,stream,name):
            info = os.fstat(stream.fileno())
            self.assertEqual(info.st_nlink,0)
            self.assertGreaterEqual(capacity.status(fixture.root)['reserved_bytes'],info.st_size)
            return original_download(handler,stream,name)

        with patch('zipfile.time.localtime', return_value=time.gmtime(1700000000)), \
             patch.object(Handler,'download_stream',stream_with_reservation):
            with urllib.request.urlopen(request,timeout=5) as result:
                self.assertEqual(result.headers['Content-Type'],'application/zip')
                self.assertIn(f'kinora-snapshot-{sid[:8]}-1.zip',result.headers['Content-Disposition'])
                bundle = result.read()
        import io
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn('motions/'+mid+'.npz',archive.namelist())
        offset = len(bundle) // 2
        with patch('zipfile.time.localtime', return_value=time.gmtime(1700000010)):
            request = urllib.request.Request(url, headers={**headers, 'Range': f'bytes={offset}-'})
            with urllib.request.urlopen(request, timeout=5) as result:
                self.assertEqual(result.status, 206)
                self.assertEqual(result.headers['Content-Range'], f'bytes {offset}-{len(bundle)-1}/{len(bundle)}')
                self.assertTrue(bundle[offset:] == result.read(), 'A later range must match the original snapshot bytes')
        deadline = time.monotonic() + 3
        while capacity.status(fixture.root)['reserved_bytes'] and time.monotonic() < deadline:
            time.sleep(.025)
        self.assertEqual(capacity.status(fixture.root)['reserved_bytes'],0)
        self.assertFalse(list((fixture.root/'review/personal-assets').glob('*/.snapshot-export-*')))

    def test_snapshot_download_listener_keeps_the_api_available(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Event, Thread

        from harness_service import HarnessService
        from review_server import Handler
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.server.retarget = SimpleNamespace()
        fixture.server.behind_proxy = False
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        owner = {'username':'alice', 'role':'user'}
        arrays, _ = motion_fixture()
        asset = service.assets.register('alice', arrays, description='Download listener fixture')
        change(fixture.store, owner, dict(action='add', id='test', motion_id=asset['id']))
        library = Library(service, owner)
        sid = '7'*32
        library.create_snapshot(dict(id=sid,collection_id='test',name='Download listener',
                                     expected_basis=library.collection_basis('test')[2]))
        path = f'/api/harness/personal-assets/snapshots/{sid}/download'
        def cookie(username):
            return {'Cookie': 'mf_session='+fixture.tokens[username]}

        started, release = Barrier(5), Event()
        original = Handler.download_stream

        def hold(handler, stream, filename):
            started.wait(timeout=5)
            if not release.wait(5):
                raise TimeoutError('The API did not respond while the ZIP was held')
            return original(handler, stream, filename)

        with fixture.server.download_listener(0) as downloads:
            base_url = f'http://127.0.0.1:{downloads.server_port}'
            worker = Thread(target=downloads.serve_forever, daemon=True)
            worker.start()

            def get(target, headers, data=None):
                request = urllib.request.Request(base_url+target, headers=headers, data=data)
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.read()

            try:
                for target, headers, data, expected in (
                    (path, {}, None, 401),
                    (path, cookie('bob'), None, 403),
                    (path, cookie('alice'), b'{}', 404),
                    (path.removesuffix('/download'), cookie('alice'), None, 404),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        get(target, headers, data)
                    self.assertEqual(rejected.exception.code, expected)
                    rejected.exception.close()
                with patch.object(Handler, 'download_stream', hold), ThreadPoolExecutor(max_workers=4) as pool:
                    transfers = [pool.submit(get, path+'?batch=1', cookie('alice')) for _ in range(4)]
                    try:
                        started.wait(timeout=5)
                        self.assertTrue(all(not transfer.done() for transfer in transfers))
                        self.assertEqual(fixture.call('/api/auth/me')[1]['user']['username'], 'alice')
                    finally:
                        release.set()
                    for transfer in transfers:
                        with zipfile.ZipFile(BytesIO(transfer.result(timeout=5))) as archive:
                            self.assertIn('motions/'+asset['id']+'.npz', archive.namelist())
            finally:
                downloads.shutdown()
                worker.join(timeout=3)

    def test_apply_pose_returns_503_saved_motion_unavailable_when_source_corrupt(self):
        from g2h.motion_assets import SavedMotionUnavailable
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        library = Library(service, {'username': 'alice', 'role': 'user'})
        arrays, _ = motion_fixture()
        asset = service.assets.register('alice', arrays, description='Corruptible pose source')
        pose = save_pose(library, dict(id='1' * 32, name='Ref pose', motion_id=asset['id'], frame=5))
        service.assets.artifact_path('alice', asset['id']).write_bytes(b'corrupted-npz-payload')

        with self.assertRaises(SavedMotionUnavailable):
            apply_pose(library, dict(id='2' * 32, motion_id=asset['id'], pose=pose['id'], frame=5, blend=3))

        status, body = fixture.call('/api/harness/personal-assets/apply-pose', body=dict(
            id='3' * 32, motion_id=asset['id'], pose=pose['id'], frame=5, blend=3,
        ))
        self.assertEqual(status, 503, body)

    def test_failed_snapshot_rolls_back_newly_materialized_assets_and_quota(self):
        import io
        from g2h import resource_quota
        from gadget_imports import Imports
        from harness_service import HarnessService
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = HarnessService(fixture.root, legacy_store=fixture.store)
        user = {'username': 'alice', 'role': 'user'}
        library = Library(service, user)
        arrays, _ = motion_fixture()
        buf = io.BytesIO()
        np.savez_compressed(buf, **{k: arrays[k] for k in ('fps', 'native_keypoints3d', 'native_transl', 'smpl_poses')})
        raw_npz = buf.getvalue()
        imp = Imports(fixture.root)
        m1 = imp.save('alice', '1' * 32, 'clip 1', raw_npz)['motion_id']
        m2 = imp.save('alice', '2' * 32, 'clip 2', raw_npz)['motion_id']
        m3 = imp.save('alice', '3' * 32, 'clip 3', raw_npz)['motion_id']
        # Pre-materialize m1 cleanly before snapshot attempt; rollback must preserve m1!
        existing_m1 = library.materialize(m1)
        change(fixture.store, user, dict(id='snaproll', name='Snap Rollback', action='create'))
        for mid in (m1, m2, m3):
            change(fixture.store, user, dict(id='snaproll', action='add', motion_id=mid))
        _, _, basis = library.collection_basis('snaproll')

        snap = resource_quota.snapshot(fixture.root, 'alice', 'user')['resources']['storage_bytes']
        fill_amt = snap['limit'] - snap['used'] - int(2.2 * len(raw_npz))
        resource_quota.reserve(fixture.root, 'fill-snap-quota', 'alice', {'storage_bytes': fill_amt}, 'user')
        resource_quota.settle(fixture.root, 'fill-snap-quota', {'storage_bytes': fill_amt})

        used_before = resource_quota.snapshot(fixture.root, 'alice', 'user')['resources']['storage_bytes']['used']
        with service.assets._db() as adb:
            assets_before = adb.execute('SELECT count(*) FROM assets WHERE owner=?', ('alice',)).fetchone()[0]

        with self.assertRaises(resource_quota.QuotaExceeded):
            library.create_snapshot(dict(id='a' * 32, collection_id='snaproll', name='snap fail', expected_basis=basis))

        used_after = resource_quota.snapshot(fixture.root, 'alice', 'user')['resources']['storage_bytes']['used']
        with service.assets._db() as adb:
            assets_after = adb.execute('SELECT count(*) FROM assets WHERE owner=?', ('alice',)).fetchone()[0]
        self.assertEqual(assets_after, assets_before)
        self.assertEqual(used_after, used_before)
        self.assertIsNotNone(service.assets.find_request('alice', 'snapshot-source:' + m1))
        self.assertIsNone(service.assets.find_request('alice', 'snapshot-source:' + m2))
        self.assertTrue(service.assets.artifact_path('alice', existing_m1['id']).is_file())


if __name__ == '__main__':
    unittest.main()
