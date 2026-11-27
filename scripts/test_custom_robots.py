import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from custom_robots import CustomRobots, unpack, prepare_xml

MODEL = b'''<mujoco model="Import test"><compiler angle="radian"/><worldbody><body name="base" pos="0 0 0.7"><freejoint/><geom name="torso" type="box" size=".15 .1 .3"/><body name="arm" pos="0 .2 .2"><joint name="shoulder" type="hinge" axis="1 0 0" range="-1 1"/><geom type="capsule" size=".04 .2"/></body></body></worldbody></mujoco>'''
URDF = b'''<robot name="test"><link name="base"><visual><geometry><box size=".2 .2 .4"/></geometry></visual></link></robot>'''
def archive(files):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        for name,raw in files.items():z.writestr(name,raw)
    return stream.getvalue()

def request(raw=MODEL,name='robot.xml',**extra):
    return dict(name=name,data=base64.b64encode(raw).decode(),**extra)

class Imports(unittest.TestCase):
    def test_import_retry_returns_committed_robot_without_recompiling(self):
        from subprocess import CompletedProcess
        from unittest.mock import patch

        def compile_fixture(args, **kwargs):
            Path(args[-1]).write_text(json.dumps({'summary': {'format': 'URDF', 'dof': 0, 'joints': []}}))
            return CompletedProcess(args, 0)

        with tempfile.TemporaryDirectory() as tmp, patch('custom_robots.subprocess.run', side_effect=compile_fixture) as compile_model:
            alice = CustomRobots(tmp, {'username': 'alice'})
            bob = CustomRobots(tmp, {'username': 'bob'})
            body = request(URDF, 'fixture.urdf', request_id='1' * 32)
            first = alice.import_model(body)['robot']
            for index in range(19):
                (alice.folder / ('robot_' + f'{index:032x}')).mkdir()

            self.assertEqual(alice.import_model(body)['robot'], first)
            self.assertEqual(compile_model.call_count, 1)
            self.assertEqual(len(alice.list()['items']), 1)
            with self.assertRaisesRegex(ValueError, '已用于其他模型或参数'):
                alice.import_model({**body, 'label': 'New label'})
            with self.assertRaisesRegex(ValueError, '最多导入 20'):
                alice.import_model({**body, 'request_id': '2' * 32})
            self.assertNotEqual(bob.import_model(body)['robot']['id'], first['id'])
            self.assertEqual(compile_model.call_count, 2)

    def test_mjcf_compile_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=CustomRobots(tmp,{'username':'alice'});b=CustomRobots(tmp,{'username':'bob'})
            record=a.import_model(request())['robot'];self.assertFalse(record['retarget_ready']);self.assertEqual(record['dof'],1)
            preview=a.preview(record['id']);self.assertEqual(len(preview['manifest']['geoms']),2)
            self.assertEqual(preview['manifest']['geoms'][1]['primitive'],'capsule')
            self.assertEqual(preview['clip']['poses'][0][0][:3],[0,0,.7])
            self.assertEqual((a.path(record['id'])/'package/model.xml').read_bytes(),MODEL)
            self.assertEqual((a.path(record['id'])/'original.bin').read_bytes(),MODEL)
            self.assertEqual(len(a.list()['items']),1);self.assertEqual(b.list()['items'],[])
            with self.assertRaises(ValueError):b.preview(record['id'])
    def test_multiple_entries_and_urdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=CustomRobots(tmp,{'username':'a'});raw=archive({'a.xml':MODEL,'b.xml':MODEL})
            self.assertEqual(a.import_model(request(raw,'robot.zip'))['choose_entry'],['a.xml','b.xml'])
            self.assertEqual(a.list()['items'],[])
            record=a.import_model(request(raw,'robot.zip',entry='b.xml'))['robot'];self.assertEqual(record['entry'],'b.xml')
            self.assertEqual(a.import_model(request(URDF,'test.urdf'))['robot']['format'],'URDF')
    def test_reject_archive_escape_duplicates_entities_and_plugins(self):
        cases=[archive({'../evil.xml':MODEL}),archive({'/evil.xml':MODEL}),archive({'a.xml':MODEL,'A.xml':MODEL}),archive({'a.xml':b'<!DOCTYPE robot [<!ENTITY x "bad">]><robot name="x"/>'})]
        for raw in cases:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):unpack(raw,'robot.zip',Path(tmp))
        for xml in [b'<mujoco><extension><plugin plugin="bad"/></extension></mujoco>',b'<mujoco><include file="/etc/passwd"/></mujoco>',b'<mujoco><include file="../../secret.xml"/></mujoco>']:
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);(p/'model.xml').write_bytes(xml)
                with self.assertRaises(ValueError):prepare_xml(p,'model.xml')
    def test_include_relative_mesh_and_missing_resource(self):
        mesh=b'v 0 0 0\nv .1 0 0\nv 0 .1 0\nv 0 0 .1\nf 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n'
        xml=b'<mujoco><compiler meshdir="meshes"/><asset><mesh name="tetra" file="part.obj"/></asset><include file="body.xml"/></mujoco>'
        body=b'<mujocoinclude><worldbody><body name="base"><geom type="mesh" mesh="tetra"/></body></worldbody></mujocoinclude>'
        with tempfile.TemporaryDirectory() as tmp:
            a=CustomRobots(tmp,{'username':'a'})
            raw=archive({'robot/model.xml':xml,'robot/body.xml':body,'robot/meshes/part.obj':mesh})
            record=a.import_model(request(raw,'test.zip'))['robot'];self.assertEqual(record['geometries'],1)
            self.assertTrue(a.preview(record['id'])['binary'])
            bad=archive({'model.xml':xml,'body.xml':body})
            with self.assertRaisesRegex(ValueError,'缺少资源'):a.import_model(request(bad,'bad.zip'))

    def test_concurrent_imports_cannot_exceed_owner_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        import subprocess
        started, release = Event(), Event()
        real_run = subprocess.run
        def compile_after_release(*args, **kwargs):
            started.set()
            if not release.wait(10): raise AssertionError('Import was not released')
            return real_run(*args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            a = CustomRobots(tmp, {'username': 'alice'})
            a.folder.mkdir(parents=True)
            for i in range(19): (a.folder / ('robot_' + f'{i:032x}')).mkdir()
            with patch('custom_robots.subprocess.run', side_effect=compile_after_release), ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(a.import_model, request())
                try:
                    self.assertTrue(started.wait(10))
                    with self.assertRaisesRegex(ValueError, '正在导入'):
                        CustomRobots(tmp, {'username': 'alice'}).import_model(request())
                finally:
                    release.set()
                self.assertIn('robot', pending.result(timeout=50))
            with self.assertRaisesRegex(ValueError, '最多导入 20'):
                a.import_model(request())
            self.assertEqual(len(list(a.folder.glob('robot_*'))), 20)

    def test_import_lock_excludes_other_process_and_is_owner_scoped(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            a = CustomRobots(tmp, {'username': 'alice'})
            a.folder.mkdir(parents=True)
            code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()"
            process = subprocess.Popen([sys.executable, '-c', code, str(a.folder / '.import.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'locked')
                with self.assertRaisesRegex(ValueError, '正在导入'): a.import_model(request())
                self.assertIn('robot', CustomRobots(tmp, {'username': 'bob'}).import_model(request()))
            finally:
                process.communicate(input='', timeout=10)
            self.assertIn('robot', a.import_model(request()))
            self.assertEqual((a.folder / '.import.lock').stat().st_mode & 0o777, 0o600)

    def test_import_lock_rejects_symlink_and_hardlink(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            a = CustomRobots(tmp, {'username': 'alice'})
            a.folder.mkdir(parents=True)
            target = Path(tmp) / 'unrelated'; target.write_text('preserve')
            lock = a.folder / '.import.lock'
            lock.symlink_to(target)
            with self.assertRaisesRegex(ValueError, '导入锁'): a.import_model(request())
            lock.unlink(); os.link(target, lock)
            with self.assertRaisesRegex(ValueError, '导入锁'): a.import_model(request())
            self.assertEqual(target.read_text(), 'preserve')

    def test_http_route_policy_and_owner_field_rejected(self):
        from review_access import allowed, Forbidden
        from review_harness import handle
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        with tempfile.TemporaryDirectory() as tmp:
            class Handler:
                server=SimpleNamespace(store=SimpleNamespace(root=Path(tmp)), harness_service=object())
                value=request()
                def body(self,limit):return self.value
                def send(self,status,value):return status,value
            h=Handler();user={'username':'a','role':'user'};route='/api/harness/robots'
            self.assertTrue(allowed(user,route,True))
            self.assertFalse(allowed({'role':'viewer'},route,True))
            status,result=handle(h,urlsplit(route),True,user);self.assertEqual(status,201)
            preview=route+'/'+result['robot']['id']+'/preview'
            self.assertTrue(allowed(user,preview,False));self.assertFalse(allowed(user,preview,True))
            with self.assertRaises(ValueError):handle(h,urlsplit(preview),False,{'username':'b','role':'admin'})
            with self.assertRaises(Forbidden):handle(h,urlsplit(route),False,None)
            h.value=request(owner='b')
            with self.assertRaises(ValueError):handle(h,urlsplit(route),True,user)

if __name__=='__main__':unittest.main()
