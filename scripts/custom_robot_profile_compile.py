"""Compile validated humanoid mappings in a bounded native-parser subprocess."""
import json
from pathlib import Path
import shutil
import sys
from custom_robot_profiles import JOINTS, digest, encoded

def compile_profile(folder,output):
    import mujoco as mj
    import numpy as np
    from custom_robots import prepare_xml
    request=json.loads((output/'request.json').read_text());cfg=request['configuration']
    work=output/'work';shutil.copytree(folder/'package',work)
    entry=json.loads((folder/'record.json').read_text())['entry'];prepare_xml(work,entry)
    model=mj.MjModel.from_xml_path(str(work/entry))
    # URDF imports are fixed-base in MuJoCo. Add a floating root to a derivative.
    if model.njnt==0 or model.jnt_type[0]!=mj.mjtJoint.mjJNT_FREE:
        import xml.etree.ElementTree as ET
        converted=work/'floating.xml';mj.mj_saveLastXML(str(converted),model)
        tree=ET.parse(converted);bodies=tree.getroot().find('worldbody').findall('body')
        if len(bodies)!=1:raise ValueError('需要单根机器人模型')
        ET.SubElement(bodies[0],'freejoint',name='kinora_root');tree.write(converted)
        model=mj.MjModel.from_xml_path(str(converted))
    if model.nbody>100 or model.ngeom>200 or model.njnt>65:raise ValueError('求解模型最多 100 连杆、200 几何体、64 个关节')
    if model.nq!=model.njnt+6 or model.nv!=model.njnt+5 or model.jnt_type[0]!=mj.mjtJoint.mjJNT_FREE:
        raise ValueError('目前支持单浮动根和有限位的单轴关节')
    if sum(model.body_parentid[1:]==0)!=1:raise ValueError('模型必须只有一个根连杆')
    if model.neq or model.ntendon:raise ValueError('暂不支持闭环约束或肌腱耦合机器人')
    if 'dynamics' in cfg:
        from robot_dynamics import contract
        contract(model,{'configuration':cfg})
    mapping=cfg['mapping'];names={model.body(i).name for i in range(1,model.nbody)}
    if not isinstance(mapping,dict) or set(mapping)!=set(JOINTS) or any(not isinstance(v,str) or v not in names for v in mapping.values()):
        raise ValueError('mapping 必须完整映射 16 个人体节点到模型连杆：'+', '.join(JOINTS))
    if len(set(mapping.values()))<14:raise ValueError('关节映射重复过多，无法确定肢体长度')
    if model.jnt_bodyid[0]!=model.body(mapping['Pelvis']).id:raise ValueError('Pelvis 必须映射到浮动根连杆')
    neutral=cfg.get('neutral',{});locked=cfg.get('locked_joints',[])
    jnames={model.joint(i).name for i in range(1,model.njnt)}
    if not isinstance(neutral,dict) or set(neutral)-jnames or not isinstance(locked,list) or any(not isinstance(n,str) or n not in jnames for n in locked):raise ValueError('neutral 或 locked_joints 包含未知关节')
    for j in range(1,model.njnt):
        name=model.joint(j).name
        if not name or model.jnt_type[j] not in (mj.mjtJoint.mjJNT_HINGE,mj.mjtJoint.mjJNT_SLIDE) or not model.jnt_limited[j]:raise ValueError('所有活动关节都必须命名并设置有限位')
        lo,hi=model.jnt_range[j];v=neutral.get(name,float(np.clip(0,lo,hi)))
        if type(v) not in (float,int) or not np.isfinite(v) or not lo<=v<=hi or lo>=hi:raise ValueError('中立姿态或关节限位无效：'+name)
        model.qpos0[model.jnt_qposadr[j]]=v
        if any(word in name.lower() for word in ('wrist','finger','thumb','pinky','index','middle','ring')) and name not in locked:locked=locked+[name]
    # Only the chain leading to mapped body landmarks can move. Unmapped
    # descendants (including fingers) stay at their explicit neutral pose.
    ancestors=set()
    for link in mapping.values():
        body=model.body(link).id
        while body:
            ancestors.add(body);body=int(model.body_parentid[body])
    for j in range(1,model.njnt):
        if int(model.jnt_bodyid[j]) not in ancestors and model.joint(j).name not in locked:locked=locked+[model.joint(j).name]
    model.qpos0[3:7]=[1,0,0,0]
    data=mj.MjData(model);mj.mj_forward(model,data)
    from retarget_pose import SEGMENTS
    for a,b in SEGMENTS:
        if np.linalg.norm(data.xpos[model.body(mapping[a]).id]-data.xpos[model.body(mapping[b]).id])<.01:raise ValueError('映射肢体长度退化：'+a+' / '+b)
    geoms={model.body(int(model.geom_bodyid[g])).name for g in range(model.ngeom) if model.geom_bodyid[g] and (model.geom_contype[g] or model.geom_conaffinity[g])}
    if any(mapping[k] not in geoms for k in ('Pelvis','L_Foot','R_Foot','L_Wrist','R_Wrist')):raise ValueError('躯干、双足和双腕必须有碰撞几何体')
    pairs=set()
    for a in geoms:
        for b in geoms:
            ia,ib=model.body(a).id,model.body(b).id
            if ia<ib and model.body_parentid[ia]!=ib and model.body_parentid[ib]!=ia and model.body_weldid[ia]!=model.body_weldid[ib]:pairs.add(tuple(sorted((a,b))))
    exclusions=cfg.get('collision_exclusions',[])
    if not isinstance(exclusions,list) or len(exclusions)>50:raise ValueError('碰撞排除项无效')
    for item in exclusions:
        if not isinstance(item,dict) or set(item)!={'bodies','reason'} or not isinstance(item['bodies'],list) or len(item['bodies'])!=2 or any(not isinstance(n,str) for n in item['bodies']) or not isinstance(item['reason'],str) or not 5<=len(item['reason'])<=300:raise ValueError('每个碰撞排除项需要两个连杆及明确原因')
        pair=tuple(sorted(item['bodies']))
        if pair not in pairs:raise ValueError('排除项不是可检查的非相邻连杆对')
        pairs.remove(pair)
    required=[('L_Foot','R_Foot'),('L_Wrist','Pelvis'),('R_Wrist','Pelvis')]
    if any(tuple(sorted((mapping[a],mapping[b]))) not in pairs for a,b in required):raise ValueError('不能省略双足及手臂对躯干的碰撞检查')
    from retarget_quality import bottom
    collision_geoms=[g for g in range(model.ngeom) if model.geom_bodyid[g] and (model.geom_contype[g] or model.geom_conaffinity[g])]
    lows=[bottom(model,data,g) for g in collision_geoms]
    if not lows or any(x is None or not np.isfinite(x) for x in lows):raise ValueError('碰撞几何类型无法完成地面检查')
    pelvis=data.xpos[model.body(mapping['Pelvis']).id]
    if any(data.xpos[model.body(mapping[k]).id,2]>=pelvis[2]-.1 for k in ('L_Foot','R_Foot')):raise ValueError('中立姿态双足必须位于骨盆下方，请核对 Z-up 坐标与映射')
    if data.xpos[model.body(mapping['L_Hip']).id,1]<=data.xpos[model.body(mapping['R_Hip']).id,1]:raise ValueError('中立姿态需使用 +Y 向左的机器人坐标')
    ground_shift=.005-min(lows)
    model.qpos0[2]+=ground_shift
    mj.mj_saveModel(model,str(output/'model.mjb'))
    result=dict(request,model_sha256=digest(output/'model.mjb'),mujoco_version=mj.__version__,dof=model.nv-6,
        collision_pairs=sorted(pairs),locked_joints=sorted(set(locked)),joint_names=[model.joint(i).name for i in range(1,model.njnt)],
        neutral_ground_shift_m=float(ground_shift),coordinates='metres; Z up; +X forward; +Y left',solver='custom-humanoid-ik-v1',ready=True)
    (output/'profile.json').write_text(encoded(result))

if __name__=='__main__':
    folder,output=map(Path,sys.argv[1:])
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(50,50));resource.setrlimit(resource.RLIMIT_FSIZE,(128*1024**2,)*2)
        if sys.platform=='linux':resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,)*2)
        compile_profile(folder,output)
    except Exception as exc:
        (output/'error.txt').write_text(str(exc).replace(str(folder),'[模型]').replace(str(output),'[配置]'));sys.exit(1)
