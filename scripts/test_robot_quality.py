"""Real MuJoCo distance and surface tests, no model inference."""
import unittest
import numpy as np
import mujoco
from retarget_quality import bottom,collision_pairs,analyze,failure_summary,quarantine
from retarget_clearance import validate
from retarget_robots import ROBOTS,model_path

class RobotQuality(unittest.TestCase):
    def test_continuity_failure_reports_worst_event_and_dynamics_status(self):
        message = failure_summary({
            'checks': {'temporal_continuity': False, 'dynamics': None},
            'temporal': {'events': [
                {'kind': 'root_step', 'frame': 2, 'part': 'root', 'value': .32, 'limit': .25},
                {'kind': 'joint_step', 'frame': 2, 'part': 'head_yaw_joint',
                 'value': np.pi, 'limit': np.pi / 4},
            ]},
        })
        self.assertIn('时间连续性未通过，动力学未验证', message)
        self.assertIn('第 3 帧 head_yaw_joint', message)
        self.assertIn('3.14 rad（上限 0.785 rad）', message)
        self.assertIn('结果已隔离，禁止正式导出', message)

    def test_interrupted_continuity_retry_is_quarantined(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            retry_file = folder / 'continuity-retry.npz'
            retry_file.write_bytes(b'partial solver output')
            quarantine(folder, 'cancelled', status='cancelled')
            self.assertFalse(retry_file.exists())
            self.assertEqual((folder / 'quarantine' / retry_file.name).read_bytes(),
                             b'partial solver output')

    def test_all_robot_pair_maps_include_feet_and_arms(self):
        for robot in ROBOTS:
            with self.subTest(robot=robot):
                model=mujoco.MjModel.from_xml_path(str(model_path(robot)));pairs=collision_pairs(model,robot)
                self.assertGreaterEqual(len(pairs),20)
                self.assertTrue(any('left' in a and 'right' in b and 'ankle' in a and 'ankle' in b for a,b in pairs))
                self.assertTrue(any(any(x in a+b for x in ('elbow','wrist','hand')) for a,b in pairs))
    def test_surface_types_and_penetration(self):
        for kind,size,expected in [('sphere','.2',.8),('box','.2 .3 .4',.6),('capsule','.2 .4',.4),('cylinder','.2 .4',.6),('ellipsoid','.2 .3 .4',.6)]:
            model=mujoco.MjModel.from_xml_string(f'<mujoco><worldbody><body pos="0 0 1"><geom type="{kind}" size="{size}"/></body></worldbody></mujoco>');data=mujoco.MjData(model);mujoco.mj_forward(model,data)
            self.assertAlmostEqual(bottom(model,data,0),expected)
    def test_real_overlap_is_rejected(self):
        model=mujoco.MjModel.from_xml_string('<mujoco><worldbody><body name="a" pos="0 0 1"><freejoint/><geom type="sphere" size=".2"/></body><body name="b" pos=".3 0 1"><geom type="sphere" size=".2"/></body></worldbody></mujoco>')
        q=np.tile(model.qpos0,(2,1));bad=validate(model,q,[('a','b')]);self.assertFalse(bad['passed']);self.assertLess(bad['worst']['distance_m'],0)
        q[:,0]=-1;good=validate(model,q,[('a','b')]);self.assertTrue(good['passed'])
        result=analyze(model,q,30,good);self.assertIsNone(result['checks']['foot_slide_proxy']);self.assertIsNone(result['checks']['dynamics'])
        q[:,2]=-.5;self.assertFalse(analyze(model,q,30,good)['export_allowed'])

    def test_failed_candidate_is_physically_quarantined(self):
        import tempfile,json,sys
        import warnings
        from pathlib import Path
        from unittest.mock import patch
        import retarget_dispatch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'review/retarget'/('a'*32);folder.mkdir(parents=True)
            model=mujoco.MjModel.from_xml_path(str(model_path('unitree_g1_23dof')))
            q=np.tile(model.qpos0,(2,1));q[:,2]=-1
            def solver(*args,**kwargs):
                np.savez(folder/'result.npz',qpos=q,fps=30,metadata=np.array(json.dumps({'motion_id':'mimp'+'a'*32,'text':'A person performs a motion.'})))
                (folder/'preview.json').write_text('{}')
            argv=['dispatch','retarget_engine.py','--root',str(root),'--job','a'*32,'--robot','unitree_g1_23dof']
            with (
                patch.object(sys, 'argv', argv),
                patch.object(retarget_dispatch.runpy, 'run_path', side_effect=solver),
                patch('g2h.motion_semantics.robot', return_value={'status': 'unknown'}),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter('error', RuntimeWarning)
                with self.assertRaises(ValueError):retarget_dispatch.main()
            self.assertFalse((folder/'result.npz').exists());self.assertTrue((folder/'quarantine/result.npz').exists())
            quality=json.loads((folder/'quality.json').read_text());self.assertFalse(quality['export_allowed']);self.assertFalse(quality['checks']['ground_surface'])

    def test_soft_failure_publishes_sealed_result_without_quarantine_and_cvxpy_fallback(self):
        import tempfile, json, sys
        from pathlib import Path
        from types import ModuleType, SimpleNamespace
        from unittest.mock import patch
        import retarget_dispatch
        from retarget_engine import install_cvxpy_fallback

        # Verify CVXPY fallback CLARABEL -> OSQP -> SCS on SolverError
        calls = []
        class FakeSolverError(Exception):
            pass
        class FakeProblem:
            def solve(self, *args, **kwargs):
                calls.append(kwargs.get('solver'))
                if kwargs.get('solver') == 'CLARABEL':
                    raise FakeSolverError('clarabel failed')
                self.status = 'optimal'
                return 0.0
        fake_cp = ModuleType('cvxpy')
        fake_cp.CLARABEL = 'CLARABEL'
        fake_cp.OSQP = 'OSQP'
        fake_cp.SCS = 'SCS'
        fake_cp.OPTIMAL = 'optimal'
        fake_cp.OPTIMAL_INACCURATE = 'optimal_inaccurate'
        fake_cp.error = SimpleNamespace(SolverError=FakeSolverError)
        fake_cp.Problem = FakeProblem
        with patch.dict(sys.modules, {'cvxpy': fake_cp}):
            install_cvxpy_fallback()
            FakeProblem().solve(solver='CLARABEL')
            self.assertEqual(calls, ['CLARABEL', 'OSQP'])

        # Verify soft failure (ground penetration ~0.04m <= 0.25m, engine_warning) exits 0 and seals result.npz
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root/'review/retarget'/('b'*32); folder.mkdir(parents=True)
            model = mujoco.MjModel.from_xml_path(str(model_path('unitree_g1_23dof')))
            q = np.tile(model.qpos0, (2, 1)); q[:, 2] = 0.85
            def solver(*args, **kwargs):
                meta = {'motion_id': 'mimp' + 'b'*32, 'text': 'A person performs a motion.', 'engine_warning': '关节越界超过容差'}
                np.savez(folder/'result.npz', qpos=q, fps=30, metadata=np.array(json.dumps(meta)))
                (folder/'preview.json').write_text('{}')
            argv = ['dispatch', 'retarget_engine.py', '--root', str(root), '--job', 'b'*32, '--robot', 'unitree_g1_23dof']
            with patch.object(sys, 'argv', argv), \
                 patch.object(retarget_dispatch.runpy, 'run_path', side_effect=solver), \
                 patch('g2h.motion_semantics.robot', return_value={'status': 'unknown'}), \
                 patch('review_retarget.Retarget') as mock_q:
                mock_q.return_value.db.return_value.execute.return_value.fetchone.return_value = [json.dumps({})]
                retarget_dispatch.main()
            self.assertTrue((folder/'result.npz').exists())
            self.assertFalse((folder/'quarantine/result.npz').exists())
            quality = json.loads((folder/'quality.json').read_text())
            self.assertFalse(quality['export_allowed'])
            self.assertNotIn('结果已隔离', quality.get('reason', ''))

if __name__=='__main__':unittest.main()
