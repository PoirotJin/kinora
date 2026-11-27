import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import sys

import mujoco
import numpy as np

from custom_robot_engine import measure_tracking, solve
from custom_robot_profiles import read
from gadget_imports import Imports
from harness_retarget import _numeric_export
from retarget_clearance import validate
from retarget_parameters import validate as parameters
from retarget_quality import analyze
from review_auth import Auth
from review_retarget import Retarget
from review_server import Handler, Server
from test_custom_robot_pipeline import setup_robot
from test_kimodo_edit import fixture


class RepairRoundTrip(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'KINORA_RETARGET_REQUIRE_DYNAMICS': '0'}))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        auth = Auth(self.root / 'review')
        self.sessions = {}
        for username, role in [('alice', 'user'), ('bob', 'user'), ('reviewer', 'admin')]:
            auth.create(username, 'temporary-test-password', role)
            self.sessions[username] = auth.change_password(username, 'temporary-test-password', 'changed-test-password')
        owner, robot, profile = setup_robot(self.root)
        user = dict(owner, role='user')
        model = mujoco.MjModel.from_binary_path(str(read(self.root, user, robot['id'], profile['version'])[1]))
        motion, _, _ = fixture(30)
        source = Imports(self.root).save('alice', 'a' * 32, 'A person performs a motion.', _numeric_export(motion))
        self.clean, _, _ = solve(model, profile, motion, parameters({}))
        # Keep both feet in the contact band on this static fixture.
        adr = int(model.joint('L_Elbow_joint').qposadr[0])
        damaged = self.clean.copy()
        damaged[15, adr] = .85
        quality = analyze(model, damaged, 30, validate(model, damaged, profile['collision_pairs']),
                          profile=profile, robot=robot['id'], require_dynamics=False)
        quality['tracking'] = measure_tracking(model, profile, motion, damaged)
        self.assertFalse(quality['checks']['temporal_continuity'])
        self.assertTrue(all(value is True for key, value in quality['checks'].items()
                            if key not in ('temporal_continuity', 'dynamics')), quality['checks'])
        self.assertTrue(quality['tracking']['passed'])

        queue = Retarget(self.root)
        self.job = 'b' * 32
        store = SimpleNamespace(root=self.root, detail=lambda mid: {'location': True})
        for key in (self.job, 'c' * 32, 'd' * 32, 'f' * 32):
            queue.submit(dict(id=key, motion_id=source['motion_id'], robot=robot['id'],
                              robot_version=profile['version'], force_new=True), user, store)
            queue.update(key, status='failed', done=30, total=30)
            folder = queue.folder / key
            isolated = folder / 'quarantine'
            isolated.mkdir(parents=True)
            pose=damaged
            report=quality
            if key == 'c' * 32:
                pose=self.clean.copy()
                pose[:,2]-=.12
                pose[15]=model.qpos0
                for name,value in {'R_Hip_joint':-1.204,'R_Knee_joint':1.062,
                                   'R_Shoulder_joint':.662,'R_Elbow_joint':.593}.items():
                    pose[15,model.joint(name).qposadr[0]]=value
                pose[16,adr]=1.6
                report=analyze(model,pose,30,validate(model,pose,profile['collision_pairs']),
                               profile=profile,robot=robot['id'],require_dynamics=False)
                report['tracking']=measure_tracking(model,profile,motion,pose)
                self.assertFalse(report['checks']['ground_surface'])
                self.assertFalse(report['checks']['joint_limits'])
                self.assertFalse(report['checks']['surface_collision'])
            meta = dict(robot=robot['id'], motion_id=source['motion_id'], text='A person performs a motion.',
                        tracking=report['tracking'])
            if key != 'd' * 32:
                np.savez_compressed(isolated / 'result.npz',
                                    qpos=(pose[:12] if key == 'f' * 32 else pose).astype(np.float32),
                                    fps=np.array(30.),joint_names=np.array(profile['joint_names']),
                                    metadata=np.array(json.dumps(meta)))
            (folder / 'quality.json').write_text(json.dumps(report))
        self.original = queue.folder / self.job / 'quarantine/result.npz'
        self.geometry_original=queue.folder/('c'*32)/'quarantine/result.npz'

        server = Server(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.origin = f'http://127.0.0.1:{server.server_port}'
        server.origins = {self.origin}
        server.store, server.auth, server.retarget, server.web = store, auth, queue, self.root
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

    def request(self, path, body=None, user='alice', binary=False):
        headers = {'Origin': self.origin, 'Cookie': 'mf_session=' + self.sessions[user],
                   'Content-Type': 'application/json', 'X-MotionFarm-Request': '1'}
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(self.origin + path, headers=headers, data=data), timeout=10) as response:
            return (response.read(), response.headers) if binary else json.load(response)

    def denied(self, status, path, body=None, user='alice'):
        with self.assertRaises(HTTPError) as result:
            self.request(path, body, user)
        self.assertEqual(result.exception.code, status)

    def wait_version(self, path, vid):
        for _ in range(120):
            result = self.request(path)
            version = next(v for v in result['versions'] if v['id'] == vid)
            if version['status'] not in ('pending', 'running'):
                return version
            time.sleep(.1)
        self.fail('Repair recheck did not finish')

    def test_download_import_recheck_and_formal_export(self):
        original = self.original.read_bytes()
        geometry_original=self.geometry_original.read_bytes()
        jobs = {row['id']: row for row in self.request('/api/retarget')['items']}
        self.assertTrue(jobs[self.job]['repair_available'])
        self.assertTrue(jobs['c' * 32]['repair_available'])
        self.assertIn('关节限位',jobs['c' * 32]['repair_warning'])
        self.assertIn('地面穿透',jobs['c' * 32]['repair_warning'])
        self.assertIn('自碰撞净空',jobs['c' * 32]['repair_warning'])
        self.assertFalse(jobs['d' * 32]['repair_available'])
        self.assertFalse(jobs['f' * 32]['repair_available'])
        self.denied(403, '/api/retarget/repair-download?id=' + self.job, user='bob')
        self.denied(400, '/api/retarget/repair-download?id=' + self.job, user='reviewer')
        self.denied(400, '/api/retarget/repair-download?id=' + 'd' * 32)
        self.denied(400, '/api/retarget/repair-download?id=' + 'f' * 32)
        source_path='/api/harness/motions/'+jobs['d'*32]['motion_id']+'/download'
        source_raw,_=self.request(source_path,binary=True)
        with np.load(io.BytesIO(source_raw),allow_pickle=False) as source:
            self.assertEqual(len(source['native_keypoints3d']),30)
        self.denied(403,source_path,user='bob')
        self.denied(400, '/api/retarget/download?id=' + self.job)
        base = '/api/harness/robot-edits'
        self.denied(400, base, dict(job_id='d' * 32, request_id='d' * 32))
        geometry = self.request(base, dict(job_id='c' * 32, request_id='d' * 32))
        geometry_preview = self.request(base + '/' + geometry['id'] + '/preview')
        self.assertIn('ground', {event['kind'] for event in geometry_preview['quality']['events']})
        self.assertIn('joint_limit', {event['kind'] for event in geometry_preview['quality']['events']})
        self.assertIn('collision', {event['kind'] for event in geometry_preview['quality']['events']})
        session = self.request(base, dict(job_id=self.job, request_id='e' * 32))
        self.assertTrue(session['repair_available'])
        path = base + '/' + session['id']
        preview = self.request(path + '/preview')
        self.assertFalse(preview['quality']['checks']['temporal_continuity'])
        self.assertTrue(preview['quality']['events'])
        raw, headers = self.request('/api/retarget/repair-download?id=' + self.job, binary=True)
        self.assertIn('needs-repair.npz', headers['Content-Disposition'])
        with np.load(io.BytesIO(raw), allow_pickle=False) as draft:
            arrays = {name: draft[name] for name in draft.files}
        meta = json.loads(str(arrays['metadata']))
        self.assertEqual(meta['repair_source']['job_id'], self.job)
        self.assertFalse(meta['quality']['export_allowed'])

        def submission(request_id, parent, data):
            stream = io.BytesIO()
            np.savez_compressed(stream, **data)
            return dict(request_id=request_id, parent=parent,
                        npz_base64=base64.b64encode(stream.getvalue()).decode())

        raw,_=self.request('/api/retarget/repair-download?id='+'c'*32,binary=True)
        with np.load(io.BytesIO(raw),allow_pickle=False) as draft:
            arrays={name:draft[name] for name in draft.files}
        meta=json.loads(str(arrays['metadata']))
        self.assertFalse(meta['quality']['checks']['surface_collision'])
        # A client-supplied passing report cannot authorize export.
        meta['quality']={'export_allowed':True}
        arrays['metadata']=np.array(json.dumps(meta))
        path=base+'/'+geometry['id']
        upload = path + '/import-repair'
        self.denied(403, upload, submission('1' * 32, 'original', arrays), user='bob')
        self.denied(400, upload, submission('1' * 32, 'original',
                    dict(arrays, joint_names=arrays['joint_names'][::-1])))
        self.denied(400, upload, submission('1' * 32, 'original',
                    dict(arrays, metadata=np.array('{}'))))
        bad = self.request(upload, submission('2' * 32, 'original', arrays))['head']
        failed = self.wait_version(path, bad)
        self.denied(409, upload, submission('4' * 32, 'original', arrays))
        self.denied(409, path + '/history', dict(head='original', action='undo'))
        self.assertEqual(self.request(path)['head'], bad)
        self.assertEqual(failed['status'], 'quarantined', failed)
        self.assertFalse(failed['quality']['checks']['temporal_continuity'])
        self.assertFalse(failed['quality']['checks']['surface_collision'])
        self.assertFalse(failed['quality']['checks']['ground_surface'])
        self.assertFalse(failed['quality']['checks']['joint_limits'])
        self.denied(400, path + '/history', dict(head=bad, action='accept'))
        self.denied(400, path + '/download?version=' + bad)

        corrected = submission('3' * 32, bad, dict(arrays, qpos=self.clean))
        vid = self.request(upload, corrected)['head']
        self.assertEqual(self.request(upload, corrected)['head'], vid)
        ready = self.wait_version(path, vid)
        self.assertEqual(ready['status'], 'ready', ready)
        self.assertTrue(ready['quality']['checks']['temporal_continuity'])
        self.assertTrue(ready['quality']['tracking']['passed'])
        self.assertIsNone(ready['quality']['checks']['dynamics'])
        self.denied(400, path + '/download?version=' + vid)
        accepted = self.request(path + '/history', dict(head=vid, action='accept'))
        self.assertTrue(accepted['accepted_exportable'])
        with patch.dict(os.environ, {'KINORA_RETARGET_REQUIRE_DYNAMICS': '1'}):
            current = self.request(path)
            self.assertEqual(current['accepted'], vid)
            self.assertFalse(current['accepted_exportable'])
            self.denied(400, path + '/download?version=' + vid)
        result, _ = self.request(path + '/download?version=' + vid, binary=True)
        with np.load(io.BytesIO(result), allow_pickle=False) as final:
            np.testing.assert_array_equal(final['qpos'], self.clean.astype(np.float32))
            new_meta = json.loads(str(final['metadata']))
            self.assertEqual(new_meta['robot_edit']['source_job'], 'c'*32)
            self.assertTrue(new_meta['quality']['export_allowed'])
        candidate = self.root / 'review/robot_edits' / geometry['id'] / (vid + '.npz')
        from contextlib import closing
        from robot_editor import Editor

        with np.load(io.BytesIO(result), allow_pickle=False) as original_candidate:
            changed = {name: original_candidate[name].copy() for name in original_candidate.files}
        changed['qpos'][0, 0] += 0.02
        np.savez_compressed(candidate, **changed)
        self.assertFalse(self.request(path)['accepted_exportable'])
        self.denied(410, path + '/download?version=' + vid)

        # Existing installations have completed candidates without a stored file digest.
        editor = Editor(self.root)
        with closing(editor.db()) as db, db:
            digest = db.execute('SELECT trajectory_sha256 FROM versions WHERE id=?', (vid,)).fetchone()[0]
            self.assertEqual(digest, hashlib.sha256(result).hexdigest())
            db.execute('UPDATE versions SET trajectory_sha256=NULL WHERE id=?', (vid,))
        candidate.write_bytes(result)
        self.assertTrue(self.request(path)['accepted_exportable'])
        changed_meta = json.loads(str(changed['metadata']))
        changed_meta['quality']['export_allowed'] = False
        changed['metadata'] = np.array(json.dumps(changed_meta))
        np.savez_compressed(candidate, **changed)
        self.assertFalse(self.request(path)['accepted_exportable'])
        self.denied(410, path + '/download?version=' + vid)
        earlier_candidate = candidate.with_name(bad + '.npz')
        candidate.write_bytes(earlier_candidate.read_bytes())
        swapped = self.request(path)
        self.assertFalse(swapped['accepted_exportable'])
        self.assertFalse(swapped['versions'][-1]['trajectory_available'])
        self.denied(410, path + '/preview?version=' + vid)
        self.denied(410, path + '/download?version=' + vid)
        self.denied(410, path + '/history', dict(head=vid, action='accept'))
        candidate.write_bytes(result)
        self.assertTrue(self.request(path)['accepted_exportable'])
        previous_stat = candidate.stat()
        candidate.write_bytes(b'?' * previous_stat.st_size)
        os.utime(candidate, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        damaged = self.request(path)
        self.assertFalse(damaged['accepted_exportable'])
        self.assertFalse(damaged['versions'][-1]['trajectory_available'])
        self.assertIn('损坏', damaged['versions'][-1]['trajectory_error'])
        self.denied(410, path + '/preview?version=' + vid)
        self.denied(410, path + '/download?version=' + vid)
        self.denied(410, path + '/history', dict(head=vid, action='accept'))
        candidate.unlink()
        unavailable = self.request(path)
        self.assertEqual(unavailable['accepted'], vid)
        self.assertFalse(unavailable['accepted_exportable'])
        self.assertFalse(unavailable['versions'][-1]['trajectory_available'])
        self.denied(410, path + '/preview?version=' + vid)
        self.denied(410, path + '/download?version=' + vid)
        self.denied(410, path + '/history', dict(head=vid, action='accept'))
        self.assertEqual(self.request(path + '/history', dict(head=vid, action='undo'))['head'], bad)
        self.assertEqual(self.request(path + '/preview?version=' + bad)['version'], bad)
        candidate.write_bytes(result)
        self.assertTrue(self.request(path)['accepted_exportable'])
        self.assertEqual(self.original.read_bytes(), original)
        self.assertEqual(self.geometry_original.read_bytes(), geometry_original)
        self.denied(400, '/api/retarget/download?id=' + self.job)
        self.denied(400, '/api/retarget/download?id=' + 'c'*32)

    def test_engine_rejection_keeps_complete_candidate_private_and_runs_quality(self):
        import retarget_dispatch
        from retarget_quality import reject_draft

        key='c'*32
        queue=Retarget(self.root)
        row=queue.get(key,{'username':'alice','role':'user'})
        folder=queue.folder/key
        with np.load(folder/'quarantine/result.npz',allow_pickle=False) as data:
            arrays={name:data[name] for name in data.files}
        (folder/'quarantine/result.npz').unlink()
        metadata=json.loads(str(arrays['metadata']))

        def rejected_engine(*args, **kwargs):
            reject_draft(folder,arrays['qpos'],30,arrays['joint_names'],metadata,'关节越界超过容差')

        argv=['dispatch','custom_robot_engine.py','--root',str(self.root),'--job',key,'--robot',row['robot']]
        with patch.object(sys,'argv',argv),patch.object(retarget_dispatch.runpy,'run_path',side_effect=rejected_engine):
            with self.assertRaisesRegex(ValueError,'关节越界'):
                retarget_dispatch.main()
        self.assertFalse((folder/'result.npz').exists())
        report=json.loads((folder/'quality.json').read_text())
        self.assertFalse(report['export_allowed'])
        self.assertFalse(report['checks']['joint_limits'])
        self.assertFalse(report['checks']['ground_surface'])
        self.assertTrue((folder/'quarantine/result.npz').is_file())

    def test_repair_source_accepts_done_unverified_candidate(self):
        from robot_repair import repair_source
        queue = Retarget(self.root)
        key = self.job
        folder = queue.folder / key
        (folder / 'quarantine/result.npz').replace(folder / 'result.npz')
        queue.update(key, status='done', done=30, total=30)
        row = queue.get(key, {'username': 'alice', 'role': 'user'})
        loaded = repair_source(folder, row, {'username': 'alice'})
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0], folder / 'result.npz')
        self.assertFalse(loaded[1]['export_allowed'])
