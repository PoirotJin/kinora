import hashlib,json,tempfile,time,unittest
from pathlib import Path
from contextlib import closing
import numpy as np
import mujoco as mj
from test_custom_robot_pipeline import setup_robot
from test_kimodo_edit import fixture
from custom_robot_profiles import read
from review_auth import Auth
from review_access import Forbidden
from review_retarget import Retarget
from gadget_imports import Imports
from harness_retarget import _numeric_export
from robot_editor import Editor,solve_edit,validate_edit

class EditorTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  auth=Auth(self.root/'review')
  for name in ('alice','bob'):auth.create(name,'temporary-test-password','user')
  self.user,self.robot,self.profile=setup_robot(self.root);self.user['role']='user'
  self.model=mj.MjModel.from_binary_path(str(read(self.root,self.user,self.robot['id'],self.profile['version'])[1]))
  a,_,_=fixture(60);source=Imports(self.root).save('alice','a'*32,'A person performs a motion.',_numeric_export(a))
  class Store:
   def detail(self,mid):return {'location':True}
  queue=Retarget(self.root);job='b'*32;queue.submit(dict(id=job,motion_id=source['motion_id'],robot=self.robot['id'],robot_version=self.profile['version']),self.user,Store());queue.update(job,status='done')
  folder=queue.folder/job;folder.mkdir();self.q=np.tile(self.model.qpos0,(60,1));self.source=folder/'result.npz'
  np.savez_compressed(self.source,qpos=self.q,fps=30,joint_names=np.array(self.profile['joint_names']),metadata=np.array(json.dumps(dict(robot=self.robot['id'],motion_id=source['motion_id'],text='A person performs a motion.'))))
  self.editor=Editor(self.root);self.session=self.editor.create(self.user,dict(job_id=job,request_id='c'*32))['id']
 def body(self):return dict(parent='original',start_frame=5,end_frame=55,pinned_bodies=['L_Foot','R_Foot'],frozen_joints=[],keyframes=[dict(frame=30,joint_offsets={'L_Elbow_joint':.1},body_offsets={},root_offset=[0,0,0])])
 def wait(self):
  for _ in range(200):
   r=self.editor.detail(self.user,self.session)
   if r['versions'][-1]['status'] not in ('pending','running'):return r
   time.sleep(.05)
  self.fail('Editor did not finish')
 def test_existing_candidate_database_upgrades_without_changing_rows(self):
  import sqlite3
  from concurrent.futures import ThreadPoolExecutor

  root = self.root / 'old-editor'
  folder = root / 'review/robot_edits'
  folder.mkdir(parents=True)
  with closing(self.editor.db()) as db:
   schema = db.execute("SELECT sql FROM sqlite_master WHERE name='versions'").fetchone()[0]
  legacy_schema = schema.replace(',trajectory_sha256 TEXT', '')
  with closing(sqlite3.connect(folder / 'index.sqlite3')) as db, db:
   db.execute(legacy_schema)
   db.execute("INSERT INTO versions(id,status,quality) VALUES('legacy','ready','{}')")

  def upgraded_row(_):
   with closing(Editor(root).db()) as db:
    return tuple(db.execute("SELECT status,quality,trajectory_sha256 FROM versions WHERE id='legacy'").fetchone())

  with ThreadPoolExecutor(max_workers=4) as workers:
   self.assertEqual(list(workers.map(upgraded_row, range(4))), [('ready', '{}', None)] * 4)

 def test_model_is_owned_and_preview_cannot_grandfather_old_acceptance(self):
  data=self.editor.render_model(self.user,self.session)
  self.assertEqual(data['rig']['nq'],self.model.nq);self.assertGreater(len(data['manifest']['geoms']),0)
  with self.assertRaises(Forbidden):self.editor.render_model(dict(username='bob',role='user'),self.session)
  from unittest.mock import patch
  with patch('retarget_quality.analyze',side_effect=AssertionError('Preview must not launch simulation')):
   report=self.editor.preview(self.user,self.session)['quality'];self.assertFalse(report['export_allowed'])
 def test_real_worker_versions_acceptance_and_owner(self):
  digest=hashlib.sha256(self.source.read_bytes()).hexdigest();body=dict(self.body(),request_id='d'*32)
  r=self.editor.edit(self.user,self.session,body);version=r['head'];self.assertEqual(self.editor.edit(self.user,self.session,body)['head'],version)
  r=self.wait();self.assertEqual(r['versions'][-1]['status'],'quarantined',r);self.assertEqual(r['versions'][-1]['quality']['dynamics']['status'],'unverified')
  with self.assertRaises(ValueError):self.editor.download(self.user,self.session,version)
  a=self.editor.load(self.user,self.session)[2]['qpos'];np.testing.assert_array_equal(a[:8],self.q[:8]);np.testing.assert_array_equal(a[52:],self.q[52:])
  adr=int(self.model.jnt_qposadr[self.model.joint('L_Elbow_joint').id]);self.assertAlmostEqual(a[30,adr],.1)
  self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),digest)
  with self.assertRaises(ValueError):self.editor.move(self.user,self.session,dict(head=version,action='accept'))
  self.assertEqual(self.editor.move(self.user,self.session,dict(head=version,action='undo'))['head'],'original')
  self.assertEqual(self.editor.move(self.user,self.session,dict(head='original',action='redo'))['head'],version)
  with self.assertRaises(Forbidden):self.editor.detail(dict(username='bob',role='user'),self.session)
 def test_worker_start_failure_returns_the_recorded_candidate(self):
  from unittest.mock import patch
  body=dict(self.body(),request_id='d'*32)
  with patch('robot_editor.subprocess.Popen',side_effect=OSError('worker unavailable')) as launch:
   receipt=self.editor.edit(self.user,self.session,body)
   self.assertEqual(self.editor.edit(self.user,self.session,body),receipt)
  launch.assert_called_once()
  failed,=receipt['versions']
  self.assertEqual(receipt['head'],'original')
  self.assertFalse(receipt['can_undo'])
  self.assertFalse(receipt['can_redo'])
  self.assertNotEqual(failed['id'],receipt['head'])
  self.assertEqual(failed['status'],'failed')
  self.assertEqual(failed['error'],'编辑进程启动失败')
  # Continuing edits on parent='original' must work without EditorVersionConflict.
  with patch('robot_editor.subprocess.Popen',side_effect=OSError('worker unavailable')):
   second=self.editor.edit(self.user,self.session,dict(self.body(),request_id='e'*32))
  self.assertEqual(second['head'],'original')
  self.assertEqual(len(second['versions']),2)
 def test_interrupted_worker_is_reaped_without_a_platform_process_identity(self):
  import subprocess
  import sys
  from unittest.mock import patch
  proc=subprocess.Popen([sys.executable,'-c','raise SystemExit(1)'])
  proc.wait(timeout=5)
  body=dict(self.body(),request_id='d'*32)
  with (patch('robot_editor.subprocess.Popen',return_value=proc),
        patch('g2h.capacity.identity',return_value=''), patch.object(Editor,'watch_worker')):
   receipt=self.editor.edit(self.user,self.session,body)
   with closing(self.editor.db()) as db,db:
    db.execute('UPDATE versions SET created=? WHERE id=?',(time.time()-15,receipt['head']))
   detail=self.editor.detail(self.user,self.session)
  failed,=detail['versions']
  self.assertEqual(detail['head'],'original')
  self.assertFalse(detail['can_redo'])
  self.assertEqual(failed['status'],'failed')
  self.assertIn('进程中断',failed['error'])
 def test_worker_exception_and_preexisting_failed_head_self_heal(self):
  from unittest.mock import patch
  from robot_editor import worker

  with patch('robot_editor.Editor.watch_worker'), patch('robot_editor.subprocess.Popen') as popen:
   popen.return_value.pid = 999999
   with patch('g2h.capacity.identity', return_value=''):
    receipt = self.editor.edit(self.user, self.session, dict(self.body(), request_id='d'*32))
  vid = receipt['versions'][0]['id']
  with patch('robot_editor.solve_edit', side_effect=RuntimeError('solver crash')):
   worker(str(self.root), self.session, vid)
  healed = self.editor.detail(self.user, self.session)
  self.assertEqual(healed['head'], 'original')
  self.assertEqual(healed['versions'][0]['status'], 'failed')
  self.assertFalse(healed['can_redo'])

  # Pre-existing failed DB head & redo entry self-heal on detail/enqueue/move and allow next edit.
  stale = 'a' * 32
  with closing(self.editor.db()) as db, db:
   db.execute("INSERT INTO versions(id,session,parent,request,digest,status,body,created) VALUES(?,?,?,?,?,?,?,?)",
              (stale, self.session, vid, 'e'*32, '0'*64, 'failed', '{}', time.time()))
   db.execute("UPDATE sessions SET head=?, redo=? WHERE id=?", (stale, json.dumps([vid]), self.session))
  after_heal = self.editor.detail(self.user, self.session)
  self.assertEqual(after_heal['head'], 'original')
  self.assertFalse(after_heal['can_redo'])
  next_receipt = self.editor.edit(self.user, self.session, dict(self.body(), request_id='1'*32))
  self.assertEqual(self.wait()['versions'][-1]['status'], 'quarantined')
  self.assertEqual(next_receipt['head'], self.editor.detail(self.user, self.session)['head'])

 def test_solve_edit_handles_narrow_joint_bounds(self):
  from test_custom_robot_pipeline import model_xml
  narrow_xml = model_xml().decode().replace('range="-1.5 1.5"', 'range="0 1e-9"', 1)
  narrow = mj.MjModel.from_xml_string(narrow_xml)
  q = np.tile(narrow.qpos0, (60, 1))
  b = self.body()
  b['keyframes'][0]['joint_offsets'] = {}
  b['keyframes'][0]['body_offsets'] = {'L_Wrist': [0, 0, -.01]}
  out, _ = solve_edit(narrow, self.profile, q, 30, b)
  self.assertTrue(np.isfinite(out).all())
 def test_reaper_keeps_upload_if_worker_identity_changed(self):
  import os
  import subprocess
  import sys
  from unittest.mock import patch
  proc=subprocess.Popen([sys.executable,'-c','raise SystemExit(1)'])
  proc.wait(timeout=5)
  with patch('robot_editor.subprocess.Popen',return_value=proc), patch.object(Editor,'watch_worker'):
   receipt=self.editor.edit(self.user,self.session,dict(self.body(),request_id='d'*32))
  vid=receipt['head']
  upload=self.editor.folder/self.session/(vid+'.upload.npy')
  upload.write_bytes(b'pending input')
  with closing(self.editor.db()) as db,db:
   db.execute('UPDATE versions SET created=? WHERE id=?',(time.time()-15,vid))

  def start_after_read(pid, identity):
   self.assertEqual(pid,proc.pid)
   with closing(self.editor.db()) as db,db:
    db.execute("UPDATE versions SET status='running',pid=?,process_identity='' WHERE id=?",(os.getpid(),vid))
   return True

  with patch('robot_editor.worker_exited',side_effect=start_after_read):
   detail=self.editor.detail(self.user,self.session)
  self.assertEqual(detail['versions'][0]['status'],'running')
  self.assertEqual(upload.read_bytes(),b'pending input')
  with patch('g2h.capacity.identity',return_value=''):
   self.assertEqual(self.editor.detail(self.user,self.session)['versions'][0]['status'],'running')
 def test_zero_offset_validation_preserves_all_frames_and_blocks_unknown_dynamics(self):
  result=self.editor.validate_existing(self.user,dict(job_id='b'*32,request_id='f'*32));self.session=result['id'];result=self.wait()
  self.assertEqual(result['versions'][-1]['status'],'quarantined')
  np.testing.assert_array_equal(self.editor.load(self.user,self.session)[2]['qpos'],self.q)
  self.assertFalse(result['versions'][-1]['quality']['export_allowed'])
 def test_ik_targets_and_pin_constraints(self):
  b=self.body();b['keyframes'][0]['joint_offsets']={};b['keyframes'][0]['body_offsets']={'L_Wrist':[0,0,-.03]};b['frozen_joints']=['L_Shoulder_joint']
  q,r=solve_edit(self.model,self.profile,self.q,30,b);self.assertTrue(r['passed'],r);self.assertTrue(r['outside_unchanged'])
  data=mj.MjData(self.model)
  for f in (5,15,30,54):
   data.qpos[:]=q[f];mj.mj_forward(self.model,data)
   self.assertAlmostEqual(data.xpos[self.model.body('L_Foot').id,2],.03,places=3)
  self.assertGreater(np.abs(q-self.q).max(),.05)
 def test_unreachable_target_is_not_exportable(self):
  b=self.body();b['keyframes'][0]['joint_offsets']={};b['keyframes'][0]['body_offsets']={'L_Wrist':[.4,0,0]}
  self.editor.edit(self.user,self.session,dict(b,request_id='e'*32));r=self.wait();self.assertEqual(r['versions'][-1]['status'],'quarantined',r)
  with self.assertRaises(ValueError):self.editor.move(self.user,self.session,dict(head=r['head'],action='accept'))
  with self.assertRaises(ValueError):self.editor.download(self.user,self.session,r['head'])
 def test_locked_wrist_and_invalid_keyframe_rejected(self):
  b=self.body();b['keyframes'][0]['joint_offsets']={'L_Wrist_joint':.1}
  with self.assertRaises(ValueError):validate_edit(self.model,self.profile,self.q,b)
  b=self.body();b['keyframes'][0]['frame']=5
  with self.assertRaises(ValueError):validate_edit(self.model,self.profile,self.q,b)
 def test_motion_gates_and_frame_markers(self):
  from retarget_quality import analyze
  from retarget_clearance import validate
  q=self.q.copy();q[:,0]=np.arange(60)/30
  r=analyze(self.model,q,30,validate(self.model,q,self.profile['collision_pairs']),profile=self.profile)
  self.assertFalse(r['export_allowed']);self.assertTrue(any(e['kind']=='foot_slide' for e in r['events']))
  q=self.q.copy();q[30:,0]=2;r=analyze(self.model,q,30,validate(self.model,q,self.profile['collision_pairs']),profile=self.profile)
  self.assertFalse(r['export_allowed']);self.assertTrue(any(e['kind']=='root_step' and e['frame']==30 for e in r['events']))
if __name__=='__main__':unittest.main()
