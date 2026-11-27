"""Bounded, separate-process compiler for user-supplied robot models."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys

def compile_model(package, entry):
    from custom_robots import prepare_xml
    import numpy as np
    import mujoco as mj
    prepare_xml(package, entry)
    m = mj.MjModel.from_xml_path(str(package / entry))
    if m.nbody > 1000 or m.njnt > 500 or m.ngeom > 3000 or m.nmeshvert > 1000000:
        raise ValueError('模型超出预览上限（1000 连杆、500 关节、100 万顶点）')
    d = mj.MjData(m); mj.mj_forward(m, d)
    chunks = []; offset = 0; meshes = {}; geoms = []; poses = []
    types = {mj.mjtGeom.mjGEOM_BOX:'box', mj.mjtGeom.mjGEOM_SPHERE:'sphere', mj.mjtGeom.mjGEOM_CAPSULE:'capsule', mj.mjtGeom.mjGEOM_CYLINDER:'cylinder', mj.mjtGeom.mjGEOM_ELLIPSOID:'ellipsoid'}
    # Prefer visual-only geometry where a body has separate collision shapes.
    visual_bodies = {int(m.geom_bodyid[i]) for i in range(m.ngeom) if not m.geom_contype[i] and not m.geom_conaffinity[i]}
    for i in range(m.ngeom):
        body = int(m.geom_bodyid[i]); kind = m.geom_type[i]
        if body == 0 or kind == mj.mjtGeom.mjGEOM_PLANE: continue
        if body in visual_bodies and (m.geom_contype[i] or m.geom_conaffinity[i]): continue
        if m.geom_rgba[i,3] <= 0: continue
        mesh = None
        if kind == mj.mjtGeom.mjGEOM_MESH:
            mid = int(m.geom_dataid[i]); mesh = str(mid)
            if mesh not in meshes:
                v = m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid]+m.mesh_vertnum[mid]].astype('<f4')
                f = m.mesh_face[m.mesh_faceadr[mid]:m.mesh_faceadr[mid]+m.mesh_facenum[mid]].astype('<u4')
                meshes[mesh] = dict(vertex_offset=offset, vertices=len(v), index_offset=offset+v.nbytes, indices=f.size)
                chunks.extend([v.tobytes(), f.tobytes()]); offset += v.nbytes+f.nbytes
            primitive = 'mesh'
        else:
            primitive = types.get(kind)
            if not primitive: raise ValueError('模型包含暂不支持预览的几何类型')
        geoms.append(dict(id=i, mesh=mesh, primitive=primitive, size=m.geom_size[i].tolist(), body=m.body(body).name or str(body), color=(m.mat_rgba[m.geom_matid[i],:3] if m.geom_matid[i] >= 0 else m.geom_rgba[i,:3]).tolist()))
        quat = np.zeros(4); mj.mju_mat2Quat(quat, d.geom_xmat[i])
        poses.append([*d.geom_xpos[i].tolist(), *quat[1:].tolist(), float(quat[0])])
    if not geoms: raise ValueError('模型中没有可预览的机器人几何体')
    if offset > 32*1024*1024: raise ValueError('模型网格超过 32 MB 预览上限')
    joints = []
    for i in range(m.njnt):
        types_j = {mj.mjtJoint.mjJNT_FREE:'free', mj.mjtJoint.mjJNT_BALL:'ball', mj.mjtJoint.mjJNT_SLIDE:'prismatic', mj.mjtJoint.mjJNT_HINGE:'revolute'}
        joints.append(dict(name=m.joint(i).name or str(i), type=types_j[m.jnt_type[i]], limited=bool(m.jnt_limited[i]), range=m.jnt_range[i].tolist()))
    version = hashlib.sha256(b''.join(chunks) + json.dumps(poses).encode()).hexdigest()
    return dict(summary=dict(format='URDF' if entry.lower().endswith('.urdf') else 'MJCF', bodies=int(m.nbody-1), dof=int(m.nv - 6 * sum(j['type']=='free' for j in joints)), base_dof=6 * sum(j['type']=='free' for j in joints), joints=joints, geometries=len(geoms), note='模型已导入；预览显示几何与基础颜色，不显示贴图。需配置人体关节映射和碰撞规则后才能用于重定向。'), manifest=dict(version=version, robot='custom', meshes=meshes, geoms=geoms), binary=base64.b64encode(b''.join(chunks)).decode(), clip=dict(version=version, metadata={'robot':'custom'}, poses=[poses], roots=[[0,0,0]], frames=1, fps=1, duration_s=1))

if __name__ == '__main__':
    package, entry, output = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    work = package.parent / 'compiled'
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64*1024*1024, 64*1024*1024))
        if sys.platform == 'linux': resource.setrlimit(resource.RLIMIT_AS, (4*1024**3, 4*1024**3))
        shutil.copytree(package, work)
        result = compile_model(work, entry)
        output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as e:
        # Strip the private temporary path from model compiler diagnostics.
        message = str(e).replace(str(package.parent), '[模型包]')
        (package.parent / 'error.txt').write_text('导入失败：' + message[:600])
        sys.exit(1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
