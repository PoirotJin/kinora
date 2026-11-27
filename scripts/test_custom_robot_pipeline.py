import base64
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from custom_robots import CustomRobots
from custom_robot_profiles import configure,read,JOINTS


def model_xml():
    def body(name,pos,children=''):
        joint='<freejoint/>' if name=='Pelvis' else '<joint name="'+name+'_joint" axis="1 0 0" range="-1.5 1.5"/>'
        return f'<body name="{name}" pos="{pos}">{joint}<geom name="{name}_surface" type="sphere" size=".025" mass="1"/>{children}</body>'
    children=body('Neck','0 0 .4')
    for side,sign in [('L',1),('R',-1)]:
        foot=body(side+'_Foot','.10 0 0')
        ankle=body(side+'_Ankle','0 0 -.4',foot)
        knee=body(side+'_Knee','0 0 -.4',ankle)
        children+=body(side+'_Hip',f'0 {sign*.1} -.05',knee)
        wrist=body(side+'_Wrist',f'0 {sign*.25} 0')
        elbow=body(side+'_Elbow',f'0 {sign*.25} 0',wrist)
        children+=body(side+'_Shoulder',f'0 {sign*.2} .2',elbow)
    return ('<mujoco><compiler angle="radian"/><worldbody>'+body('Pelvis','0 0 .9',children)+'</worldbody></mujoco>').encode()

def setup_robot(root):
    owner={'username':'alice'}
    r=CustomRobots(root,owner).import_model(dict(name='humanoid.xml',data=base64.b64encode(model_xml()).decode()))['robot']
    cfg=dict(mapping={k:k for k in JOINTS},locked_joints=['L_Wrist_joint','R_Wrist_joint'])
    p=configure(root,owner,r['id'],cfg)
    return owner,r,p

class Pipeline(unittest.TestCase):
    def test_profile_is_immutable_owner_scoped_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner,r,p=setup_robot(tmp)
            again=configure(tmp,owner,r['id'],p['configuration']);self.assertEqual(p,again)
            with self.assertRaises(ValueError):read(tmp,{'username':'bob'},r['id'],p['version'])
            changed=dict(p['configuration'],neutral={'L_Knee_joint':.1})
            second=configure(tmp,owner,r['id'],changed);self.assertNotEqual(second['version'],p['version'])
            self.assertEqual(read(tmp,owner,r['id'],p['version'])[0],p)
            path=read(tmp,owner,r['id'],p['version'])[1];path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'校验'):read(tmp,owner,r['id'],p['version'])

    def test_real_solver_and_collision_quality(self):
        import mujoco as mj
        from custom_robot_engine import solve
        from retarget_parameters import validate
        from retarget_clearance import validate as clearance
        from retarget_quality import analyze
        from test_kimodo_edit import fixture
        with tempfile.TemporaryDirectory() as tmp:
            owner,r,p=setup_robot(tmp);m=mj.MjModel.from_binary_path(str(read(tmp,owner,r['id'],p['version'])[1]))
            source,_,_=fixture(30)
            q,cost,tracking=solve(m,p,source,validate({}))
            self.assertEqual(q.shape,(30,m.nq));self.assertTrue(tracking['passed'],tracking)
            report=analyze(m,q,30,clearance(m,q,p['collision_pairs']),profile=p)
            self.assertFalse(report['export_allowed']);self.assertEqual(report['dynamics']['status'],'unverified');self.assertTrue(all(v is not False for k,v in report['checks'].items() if k!='dynamics'))
            for name in p['locked_joints']:
                adr=m.jnt_qposadr[m.joint(name).id];np.testing.assert_array_equal(q[:,adr],m.qpos0[adr])

    def test_turning_with_asymmetric_leg_reach_controls_landmark_outliers(self):
        import mujoco as mj
        from scipy.spatial.transform import Rotation
        from custom_robot_engine import solve
        from retarget_parameters import validate
        from retarget_clearance import validate as clearance
        from retarget_quality import analyze
        from test_kimodo_edit import fixture
        from g2h.kimodo_adapter import fk
        source,rest,parents=fixture(40)
        local=np.broadcast_to(np.eye(3),(40,52,3,3)).copy()
        local[:,0]=Rotation.from_euler('y',np.linspace(0,np.pi/2,40)).as_matrix()
        local[:,2]=Rotation.from_euler('z',.65*np.sin(np.linspace(0,np.pi,40))).as_matrix()
        source['native_keypoints3d']=fk(local,rest,parents)[0]
        source['smpl_poses']=Rotation.from_matrix(local.reshape(-1,3,3)).as_rotvec().reshape(40,156)
        before={k:v.copy() for k,v in source.items()}
        with tempfile.TemporaryDirectory() as tmp:
            owner,r,p=setup_robot(tmp)
            m=mj.MjModel.from_binary_path(str(read(tmp,owner,r['id'],p['version'])[1]))
            q,_,tracking=solve(m,p,source,validate({}))
            # The quadratic-only objective exceeds 13 cm on this FK fixture.
            self.assertLess(tracking['max_m'],.12)
            self.assertEqual(tracking['limits'],dict(rms_m=.15,max_m=.35))
            self.assertTrue(tracking['passed'])
            report=analyze(m,q,30,clearance(m,q,p['collision_pairs']),profile=p);self.assertFalse(report['export_allowed']);self.assertEqual(report['dynamics']['status'],'unverified');self.assertFalse(report['checks']['foot_slide_proxy']);self.assertTrue(all(v is not False for k,v in report['checks'].items() if k not in ('dynamics','foot_slide_proxy')))
            for k in source:np.testing.assert_array_equal(source[k],before[k])

    def test_queue_to_real_dispatch_pins_profile_and_quarantines_failure(self):
        import subprocess,sys,os
        from review_auth import Auth
        from review_retarget import Retarget
        from gadget_imports import Imports
        from harness_retarget import _numeric_export
        from test_kimodo_edit import fixture
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);auth=Auth(root/'review');auth.create('alice','temporary-test-password','user')
            owner,r,p=setup_robot(root);owner['role']='user'
            source,_,_=fixture(30)
            imported=Imports(root).save('alice','a'*32,'A person performs a motion.',_numeric_export(source))
            class Store:
                def detail(self,mid):return {'location':True}
            queue=Retarget(root);request=dict(id='b'*32,motion_id=imported['motion_id'],robot=r['id'],robot_version=p['version'])
            first=queue.submit(request,owner,Store());self.assertEqual(queue.submit(request,owner,Store())['id'],first['id'])
            with self.assertRaises(ValueError):queue.submit(dict(request,id='c'*32,robot_version=None),owner,Store())
            # Custom dispatch never recovers or claims a paused built-in batch.
            queue.submit(dict(id='d'*32,motion_id=imported['motion_id'],robot='unitree_g1_23dof'),owner,Store())
            queue.update('d'*32,status='running')
            row=queue.claim(0,custom_only=True);self.assertEqual(row['id'],first['id'])
            self.assertEqual(queue.get('d'*32,owner)['status'],'running')
            command=[sys.executable,str(Path(__file__).with_name('retarget_dispatch.py')),'custom_robot_engine.py','--root',str(root),'--job',first['id'],'--motion',imported['motion_id'],'--robot',r['id']]
            env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
            env['KINORA_RETARGET_REQUIRE_DYNAMICS']='1'
            result=subprocess.run(command,capture_output=True,text=True,timeout=60,cwd=tmp,env=env)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            folder=queue.folder/first['id'];quality_path=folder/'quality.json'
            self.assertTrue(quality_path.is_file(), result.stdout+result.stderr)
            quality=json.loads(quality_path.read_text());self.assertFalse(quality['export_allowed']);self.assertEqual(quality['dynamics']['status'],'unverified');self.assertTrue((folder/'result.npz').is_file())
            with np.load(folder/'result.npz',allow_pickle=False) as data:
                metadata=json.loads(str(data['metadata']));self.assertEqual(metadata['custom_robot']['version'],p['version'])
            # A corrupt pinned model fails closed; no prior successful file remains publishable.
            read(root,owner,r['id'],p['version'])[1].write_bytes(b'changed')
            failed=subprocess.run(command,capture_output=True,text=True,timeout=60,cwd=tmp,env=env)
            self.assertNotEqual(failed.returncode,0);self.assertFalse((folder/'result.npz').exists())
            self.assertTrue((folder/'quarantine/result.npz').exists())

    def test_configuration_rejects_missing_mapping_and_unsafe_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner,r,p=setup_robot(tmp)
            for cfg in ({'mapping':{}},dict(p['configuration'],collision_exclusions=[{'bodies':['L_Foot','R_Foot'],'reason':'skip feet for test'}])):
                with self.assertRaises(ValueError):configure(tmp,owner,r['id'],cfg)

    def test_solve_supports_narrow_joint_bounds(self):
        import mujoco as mj
        from custom_robot_engine import solve
        from retarget_parameters import validate
        from test_kimodo_edit import fixture
        with tempfile.TemporaryDirectory() as tmp:
            owner = {'username': 'alice'}
            narrow_xml = model_xml().decode().replace('range="-1.5 1.5"', 'range="0 1e-9"', 1).encode()
            r = CustomRobots(tmp, owner).import_model(dict(name='narrow.xml', data=base64.b64encode(narrow_xml).decode()))['robot']
            p = configure(tmp, owner, r['id'], dict(mapping={k: k for k in JOINTS}, locked_joints=['L_Wrist_joint', 'R_Wrist_joint']))
            m = mj.MjModel.from_binary_path(str(read(tmp, owner, r['id'], p['version'])[1]))
            source, _, _ = fixture(12)
            q, _, _ = solve(m, p, source, validate({}))
            self.assertEqual(q.shape, (12, m.nq))
            self.assertTrue(np.isfinite(q).all())

if __name__=='__main__':unittest.main()
