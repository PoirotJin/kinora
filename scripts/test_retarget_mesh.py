import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from review_access import allowed,Forbidden
from test_review_retarget import Tests

class MeshTests(unittest.TestCase):
    def test_full_supported_trajectory_is_exported_and_read_without_clipping(self):
        import hashlib

        import numpy as np
        from retarget_limits import FPS, MAX_FRAMES, MAX_SECONDS
        from retarget_mesh import export_clip, load, visual_geoms
        from retarget_robots import DEFAULT_ROBOT, model_path
        from review_retarget import _read_mesh_preview
        from tempfile import TemporaryDirectory

        try:
            mujoco, model = load(DEFAULT_ROBOT)
        except ModuleNotFoundError as exc:
            if exc.name != 'mujoco':
                raise
            self.skipTest('MuJoCo is unavailable')
        version = hashlib.sha256(model_path(DEFAULT_ROBOT).read_bytes()).hexdigest()
        metadata = json.dumps({'robot': DEFAULT_ROBOT, 'robot_sha256': version})
        names = [model.joint(i).name for i in range(1, model.njnt)]
        poses = np.tile(model.qpos0, (MAX_FRAMES, 1))
        poses[-1, 0] = 1.25
        with TemporaryDirectory() as tmp:
            result = Path(tmp) / 'result.npz'
            preview = Path(tmp) / 'mesh.json'
            np.savez(result, qpos=poses, fps=FPS, joint_names=names, metadata=metadata)
            export_clip(result, preview)
            geom_count = len(visual_geoms(model, mujoco))
            clip = _read_mesh_preview(preview, version, geom_count)
            self.assertIsNotNone(clip)
            self.assertEqual(clip['frames'], MAX_FRAMES)
            self.assertEqual(clip['duration_s'], MAX_SECONDS)
            self.assertEqual(clip['roots'][-1][0], 1.25)
            for invalid_poses, invalid_fps in ((np.vstack((poses, poses[0])), FPS),
                                               (poses, FPS - 1)):
                np.savez(result, qpos=invalid_poses, fps=invalid_fps, joint_names=names, metadata=metadata)
                with self.assertRaisesRegex(ValueError, 'Invalid robot trajectory'):
                    export_clip(result, preview)

    def test_read_only_permissions(self):
        user={'username':'demo','role':'user'}
        for path in ('/api/retarget/model','/api/retarget/mesh-data','/api/retarget/mesh-preview'):
            self.assertTrue(allowed(user,path,False));self.assertFalse(allowed(user,path,True))

    def test_foreign_preview_denied_before_loading_model(self):
        fixture=Tests();fixture.setUp()
        try:
            key=fixture.submit()['id']
            with self.assertRaises(Forbidden):fixture.queue.mesh_preview(key,fixture.b)
            with self.assertRaises(ValueError):fixture.queue.mesh_preview(key,fixture.a)
        finally:fixture.tearDown()

    def test_missing_mesh_resources_report_specific_errors_and_allow_retry(self):
        from review_server import Handler, Server

        fixture = Tests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        key = fixture.submit()['id']
        folder = fixture.queue.folder / key
        folder.mkdir()
        result = b'legacy trajectory'
        (folder / 'result.npz').write_bytes(result)
        fixture.queue.update(key, status='done')

        project = fixture.root / 'project'
        web = project / 'web/review'
        robots = web / 'robot'
        robots.mkdir(parents=True)
        server = Server(('127.0.0.1', 0), Handler)
        server.web = web
        server.store = type('Store', (), {'root': fixture.root})()
        server.retarget = fixture.queue
        server.auth = fixture.auth
        token = fixture.auth.change_password('a', 'temporary-test-password', 'changed-test-password')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path):
            req = Request(f'http://127.0.0.1:{server.server_port}{path}',
                          headers={'Cookie': 'mf_session=' + token})
            with urlopen(req, timeout=3) as response:
                return response.read()

        def expect_unavailable(path, message):
            with self.assertRaises(HTTPError) as raised:
                request(path)
            with raised.exception as error:
                self.assertEqual(error.code, 503)
                self.assertIn(message, json.load(error)['error'])

        preview = '/api/retarget/mesh-preview?id=' + key
        model = '/api/retarget/model'
        mesh = '/api/retarget/mesh-data'
        try:
            with patch('review_retarget.PROJECT', project):
                expect_unavailable(model, 'Humanoid')
                expect_unavailable(preview, 'Humanoid')
                (robots / 'unitree_g1.json').write_text(json.dumps({'version': 'test-version', 'geoms': [{}]}))
                expect_unavailable(mesh, 'Humanoid')
                expect_unavailable(preview, '预览转换服务')

                (robots / 'unitree_g1.bin').write_bytes(b'compiled mesh')
                from hashlib import sha256
                digest = sha256(result).hexdigest()[:16]
                cache = folder / f'mesh-test-version-{digest}.json'
                frame = [[0, 0, 0, 0, 0, 0, 1]]
                clip = {'version':'test-version','frames':2,'fps':30,'poses':[frame,frame],'roots':[[0,0,0],[0,0,0]]}
                cache.write_text(json.dumps(clip))
                self.assertEqual(request(mesh), b'compiled mesh')
                self.assertEqual(json.loads(request(model))['version'], 'test-version')
                self.assertEqual(json.loads(request(preview))['frames'], 2)
                cache.write_text('{"version":"test-version","frames":2,"poses":[[],[]]}')
                def regenerate(command, **kwargs):
                    Path(command[command.index('--output')+1]).write_text(json.dumps(clip))
                with patch('review_retarget.subprocess.run', side_effect=regenerate) as converter:
                    self.assertEqual(json.loads(request(preview)), clip)
                    self.assertEqual(json.loads(request(preview)), clip)
                    converter.assert_called_once()
                self.assertEqual((folder / 'result.npz').read_bytes(), result)
                cache.write_text('{"version":"test-version","frames":2,"poses":[[],[]]}')
                with patch('review_retarget.subprocess.run') as converter:
                    expect_unavailable(preview, '生成的机器人预览无法读取')
                    converter.assert_called_once()
                (robots / 'unitree_g1.json').write_text('{"version":null,"geoms":[]}')
                expect_unavailable(preview, '预览资源损坏')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

if __name__=='__main__':unittest.main()
