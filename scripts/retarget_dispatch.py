"""Run an allowlisted solver and quarantine every failed new candidate before publication."""
import hashlib,json,os,runpy,sys,time
from pathlib import Path
P=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(P/'scripts'),str(P)]

def main():
    engine=sys.argv.pop(1)
    if engine not in ('retarget_engine.py','fast_retarget_engine.py','gadget_retarget_engine.py','custom_robot_engine.py'):raise ValueError('Unknown engine')
    root=Path(sys.argv[sys.argv.index('--root')+1]);key=sys.argv[sys.argv.index('--job')+1];robot=sys.argv[sys.argv.index('--robot')+1]
    folder=root/'review/retarget'/key
    from retarget_quality import DraftCandidateError,PAIR_VERSION
    try:
        candidate_path=folder/'result.npz'
        engine_rejection=None
        try:
            runpy.run_path(str(P/'scripts'/engine),run_name='__main__')
        except DraftCandidateError as exc:
            candidate_path=folder/'quarantine/result.npz'
            engine_rejection=str(exc)
        import numpy as np,mujoco
        from retarget_robots import model_path
        from retarget_quality import collision_pairs,analyze,failure_summary,retarget_requires_dynamics
        from retarget_clearance import validate
        profile=None
        if robot.startswith('robot_'):
            from review_retarget import Retarget
            from contextlib import closing
            from custom_robot_profiles import read
            with closing(Retarget(root).db()) as db:job=db.execute('SELECT owner,execution FROM jobs WHERE id=?',(key,)).fetchone()
            profile,path=read(root,{'username':job['owner']},robot,json.loads(job['execution'])['custom_robot']['version'])
            model=mujoco.MjModel.from_binary_path(str(path))
        else:model=mujoco.MjModel.from_xml_path(str(model_path(robot)))
        with np.load(candidate_path,allow_pickle=False) as data:arrays={k:data[k] for k in data.files}
        from g2h.motion_semantics import robot as check_direction, message as direction_message
        metadata=json.loads(str(arrays['metadata']))
        if metadata.get('motion_id','').startswith(('mimp','ma_')):
            semantics=check_direction(metadata.get('text',''),arrays['qpos'],arrays['fps'])
        else:
            from semantic_retarget import source_record
            from g2h.motion_semantics import for_record
            source=source_record(root,metadata['motion_id'])
            semantics=for_record(source,arrays['qpos'],arrays['fps'])
        (folder/'semantic.json').write_text(json.dumps(semantics,ensure_ascii=False,indent=2))
        pairs=profile['collision_pairs'] if profile else collision_pairs(model,robot)
        clearance=validate(model,arrays['qpos'],pairs);clearance['version']=PAIR_VERSION+':'+robot;clearance['body_pairs']=pairs
        quality=analyze(model,arrays['qpos'],arrays['fps'],clearance,profile=profile,robot=robot,
                        require_dynamics=retarget_requires_dynamics())
        if profile:
            quality['tracking']=metadata['tracking']
            from robot_diagnostics import tracking_events
            quality['events'].extend(tracking_events(metadata['tracking']))
            quality['export_allowed']=quality['export_allowed'] and metadata['tracking']['passed']
            if not metadata['tracking']['passed']:quality['checks']['tracking']=False
        if 'upright_quality' in metadata:
            quality['upright_quality']=metadata['upright_quality']
            quality['checks']['upright_profile']=metadata['upright_quality']['passed']
            quality['export_allowed']=quality['export_allowed'] and metadata['upright_quality']['passed']
        engine_warning=metadata.get('engine_warning')
        rejection=direction_message(semantics) if semantics['status']=='fail' else engine_rejection
        if semantics['status']=='fail':quality['checks']['direction_semantics']=False
        if rejection or engine_warning or not quality['export_allowed']:
            reason=(rejection or engine_warning or failure_summary(quality)).replace('；结果已隔离，禁止正式导出','').replace('结果已隔离，','')
            quality.update(status='failed',export_allowed=False,reason=reason)
            if engine_warning:quality['engine_warning']=engine_warning
        quality.update(direction_semantics=semantics,pair_version=PAIR_VERSION,robot=robot,engine=engine,created=time.time())
        (folder/'quality.json').write_text(json.dumps(quality,allow_nan=False,indent=2))
        catastrophic=(not quality['checks'].get('finite')
                      or not quality['checks'].get('root_quaternion')
                      or quality.get('metrics',{}).get('surface_penetration_m',0.)>.25
                      or engine_rejection is not None)
        if catastrophic:raise ValueError(quality.get('reason') or failure_summary(quality))
        from review_retarget import Retarget
        from contextlib import closing
        with closing(Retarget(root).db()) as db:row=db.execute('SELECT execution FROM jobs WHERE id=?',(key,)).fetchone()
        metadata=json.loads(str(arrays['metadata']));metadata.update(quality=quality,execution=json.loads(row[0]),engine=dict(name=engine,sha256=hashlib.sha256((P/'scripts'/engine).read_bytes()).hexdigest(),identity_kind='source-sha256'))
        identity=hashlib.sha256()
        fields=('body_parentid','body_pos','body_quat','jnt_type','jnt_pos','jnt_axis','jnt_range','qpos0','geom_bodyid','geom_type','geom_size','geom_pos','geom_quat','geom_contype','geom_conaffinity','mesh_vert','mesh_face')
        for field in fields:
            a=np.asarray(getattr(model,field));identity.update(field.encode());identity.update(str(a.shape).encode());identity.update(a.tobytes())
        metadata['robot_collision_identity']=dict(kind='compiled-collision-and-joints-sha256',sha256=identity.hexdigest(),fields=list(fields))
        metadata['adapter_sources']={name:hashlib.sha256((P/'scripts'/name).read_bytes()).hexdigest() for name in ('retarget_engine.py','retarget_pose.py','retarget_parameters.py','retarget_limits.py','robot_dynamics.py','robot_diagnostics.py','retarget_quality.py','retarget_clearance.py','retarget_dispatch.py','fast_retarget_engine.py','custom_robot_engine.py','custom_robot_profiles.py','custom_robot_profile_compile.py')}
        metadata['runtime']=dict(python=sys.version.split()[0],numpy=np.__version__,mujoco=mujoco.__version__)
        arrays['metadata']=np.array(json.dumps(metadata,ensure_ascii=False));np.savez_compressed(folder/'result.checked.npz',**arrays);os.replace(folder/'result.checked.npz',folder/'result.npz')
        preview=json.loads((folder/'preview.json').read_text());preview['metadata']=metadata;(folder/'preview.json').write_text(json.dumps(preview))
    except BaseException as exc:
        from retarget_quality import quarantine
        quarantine(folder,str(exc))
        raise

if __name__=='__main__':main()
