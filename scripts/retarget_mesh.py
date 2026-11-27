"""Export the exact compiled MuJoCo visual meshes and FK poses, CPU only."""
from pathlib import Path
import argparse
import hashlib
import json
import os

from retarget_limits import MAX_FRAMES, MAX_SECONDS

PROJECT=Path(__file__).resolve().parents[1]
MODEL=PROJECT/'third_party/holosoma/holosoma_retargeting/models/motionfarm/unitree_g1_23dof.xml'
ASSETS=PROJECT/'web/review/robot'
from retarget_robots import model_path, robot_spec, DEFAULT_ROBOT

def load(robot=DEFAULT_ROBOT):
    import mujoco
    return mujoco,mujoco.MjModel.from_xml_path(str(model_path(robot)))

def visual_geoms(model,mujoco):
    import numpy as np
    result=[];seen=set()
    for i in range(model.ngeom):
        mesh=model.geom_type[i]==mujoco.mjtGeom.mjGEOM_MESH
        hand=model.geom_type[i]==mujoco.mjtGeom.mjGEOM_BOX and model.body(int(model.geom_bodyid[i])).name.endswith('hand_link_o')
        if not mesh and not hand:continue
        key=(int(model.geom_bodyid[i]),int(model.geom_dataid[i]),tuple(np.round(model.geom_pos[i],6)),tuple(np.round(model.geom_quat[i],6)))
        if key in seen:continue
        seen.add(key);result.append(i)
    return result

def export_model(robot=DEFAULT_ROBOT):
    import numpy as np
    spec=robot_spec(robot);mj,m=load(robot);ids=visual_geoms(m,mj);chunks=[];offset=0;meshes={}
    for mid in sorted({int(m.geom_dataid[g]) for g in ids if m.geom_type[g]==mj.mjtGeom.mjGEOM_MESH}):
        v=m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid]+m.mesh_vertnum[mid]].astype('<f4')
        f=m.mesh_face[m.mesh_faceadr[mid]:m.mesh_faceadr[mid]+m.mesh_facenum[mid]].astype('<u4')
        meshes[str(mid)]={'vertex_offset':offset,'vertices':len(v),'index_offset':offset+v.nbytes,'indices':f.size}
        chunks.extend([v.tobytes(),f.tobytes()]);offset+=v.nbytes+f.nbytes
    version=hashlib.sha256(model_path(robot).read_bytes()).hexdigest()
    manifest={'version':version,'robot':robot,'meshes':meshes,'geoms':[{'id':g,'mesh':str(int(m.geom_dataid[g])) if m.geom_type[g]==mj.mjtGeom.mjGEOM_MESH else None,
              'size':m.geom_size[g].tolist(),'body':m.body(int(m.geom_bodyid[g])).name,'color':m.geom_rgba[g,:3].tolist()} for g in ids]}
    ASSETS.mkdir(parents=True,exist_ok=True)
    (ASSETS/(spec['asset']+'.bin')).write_bytes(b''.join(chunks))
    (ASSETS/(spec['asset']+'.json')).write_text(json.dumps(manifest))
    print(json.dumps({'geoms':len(ids),'meshes':len(meshes),'bytes':offset,'version':version}))

def export_clip(result,output):
    import numpy as np
    with np.load(result,allow_pickle=False) as archive:
        qpos=np.asarray(archive['qpos']);fps=float(archive['fps']);meta=json.loads(str(archive['metadata']))
        names=archive['joint_names'].tolist()
    robot=meta.get('robot',DEFAULT_ROBOT);mj,m=load(robot);ids=visual_geoms(m,mj);data=mj.MjData(m)
    if (qpos.ndim != 2 or qpos.shape[1] != m.nq or not 2 <= len(qpos) <= MAX_FRAMES
            or not 1 <= fps <= 240 or len(qpos) / fps > MAX_SECONDS or not np.isfinite(qpos).all()):
        raise ValueError('Invalid robot trajectory')
    if names!=[m.joint(i).name for i in range(1,m.njnt)]:raise ValueError('Robot joint order mismatch')
    version=hashlib.sha256(model_path(robot).read_bytes()).hexdigest()
    if meta['robot_sha256']!=version:raise ValueError('Robot model version mismatch')
    frames=[];quat=np.empty(4)
    for pose in qpos:
        data.qpos[:]=pose;mj.mj_forward(m,data);frame=[]
        for g in ids:
            mj.mju_mat2Quat(quat,data.geom_xmat[g])
            frame.append([*data.geom_xpos[g],quat[1],quat[2],quat[3],quat[0]])
        frames.append(frame)
    payload={'version':version,'poses':np.asarray(frames).round(6).tolist(),'roots':qpos[:,:3].round(6).tolist(),
             'fps':fps,'frames':len(qpos),'duration_s':len(qpos)/fps,'metadata':meta}
    temp=output.with_suffix('.tmp');temp.write_text(json.dumps(payload,separators=(',',':')));os.replace(temp,output)
    print('Mesh preview ready',len(qpos))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--model',action='store_true');p.add_argument('--robot',default=DEFAULT_ROBOT);p.add_argument('--result',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    if a.model:export_model(a.robot)
    else:export_clip(a.result,a.output)
