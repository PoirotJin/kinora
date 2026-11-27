"""Owner-pinned compiled geometry and local FK contract; never accept paths from clients."""
import base64,hashlib,json
import numpy as np

def export(model):
    import mujoco as mj
    m=model;chunks=[];offset=0;meshes={};geoms=[]
    types={2:'sphere',3:'capsule',4:'ellipsoid',5:'cylinder',6:'box'}
    visual={int(m.geom_bodyid[g]) for g in range(m.ngeom) if not m.geom_contype[g] and not m.geom_conaffinity[g]}
    for g in range(m.ngeom):
        b=int(m.geom_bodyid[g]);kind=int(m.geom_type[g])
        if not b or kind==0 or m.geom_rgba[g,3]<=0:continue
        if b in visual and (m.geom_contype[g] or m.geom_conaffinity[g]):continue
        key=None
        if kind==7:
            mid=int(m.geom_dataid[g]);key=str(mid)
            if key not in meshes:
                v=m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid]+m.mesh_vertnum[mid]].astype('<f4');f=m.mesh_face[m.mesh_faceadr[mid]:m.mesh_faceadr[mid]+m.mesh_facenum[mid]].astype('<u4')
                meshes[key]=dict(vertex_offset=offset,vertices=len(v),index_offset=offset+v.nbytes,indices=f.size);chunks.extend([v.tobytes(),f.tobytes()]);offset+=v.nbytes+f.nbytes
        elif kind not in types:raise ValueError('精修实体预览不支持此几何类型')
        geoms.append(dict(id=g,mesh=key,primitive='mesh' if key is not None else types[kind],size=m.geom_size[g].tolist(),body=m.body(b).name,body_id=b,pos=m.geom_pos[g].tolist(),quat=m.geom_quat[g].tolist()))
    if not geoms or offset>24*1024*1024:raise ValueError('机器人实体网格为空或超过 24 MB')
    bodies=[]
    for b in range(m.nbody):
        joints=[]
        for j in range(int(m.body_jntadr[b]),int(m.body_jntadr[b]+m.body_jntnum[b])):
            kind=int(m.jnt_type[j])
            if kind not in (0,2,3):raise ValueError('精修只支持浮动根和单轴关节')
            adr=int(m.jnt_qposadr[j]);joints.append(dict(name=m.joint(j).name,type=kind,index=adr,ref=float(m.qpos0[adr]),pos=m.jnt_pos[j].tolist(),axis=m.jnt_axis[j].tolist()))
        bodies.append(dict(name=m.body(b).name,parent=int(m.body_parentid[b]),pos=m.body_pos[b].tolist(),quat=m.body_quat[b].tolist(),joints=joints))
    rig=dict(version='compiled-fk-v1',nq=int(m.nq),bodies=bodies,geoms=geoms)
    binary=b''.join(chunks);version=hashlib.sha256(binary+json.dumps(rig,sort_keys=True).encode()).hexdigest()
    return dict(manifest=dict(version=version,robot='editor',meshes=meshes,geoms=geoms),binary=base64.b64encode(binary).decode(),rig=rig)
