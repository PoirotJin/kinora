"""Owner-scoped reuse of immutable, quality-gated retarget artifacts."""
from contextlib import closing
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
FILES = ('result.npz', 'preview.json', 'quality.json')
ADAPTERS = ('retarget_engine.py', 'retarget_pose.py', 'retarget_parameters.py',
            'retarget_limits.py', 'robot_dynamics.py','robot_diagnostics.py','retarget_quality.py', 'retarget_clearance.py', 'retarget_dispatch.py')
_models = {}
_lock = threading.Lock()

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=512)
def _artifact_digest(path, identity):
    return digest(path)


def sealed_result_matches(folder, names=('result.npz', 'quality.json')):
    """Check selected artifacts against the reuse manifest when present."""
    try:
        manifest = (folder / 'reuse-manifest.json').read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return False
    try:
        files = json.loads(manifest)['files']
        for name in names:
            path = folder / name
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if _artifact_digest(path, identity) != files[name]:
                return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True

def has_fast_engine():
    if os.environ.get('KINORA_DISABLE_FAST_RETARGET') == '1':
        return False
    if not (PROJECT / 'scripts/fast_retarget_engine.py').exists():
        return False
    try:
        import importlib.util
        return importlib.util.find_spec('kinora_retarget_accel') is not None
    except Exception:
        return False

def engine_for(root, mid, robot):
    if mid.startswith('mimp'): return 'gadget_retarget_engine.py'
    if has_fast_engine():
        return 'fast_retarget_engine.py'
    return 'retarget_engine.py' 

def effective(execution):
    from retarget_parameters import validate, VERSION
    b = json.loads(execution) if isinstance(execution, str) else execution
    return {'schema_version': b.get('schema_version', VERSION),
            'parameters': validate(b.get('parameters', {}))}

def model_signature(robot):
    # Stat only the small, explicit robot asset tree; do not scan datasets.
    from retarget_robots import model_path
    folder = PROJECT / 'third_party/holosoma/holosoma_retargeting/models'
    stamp = []
    for p in folder.rglob('*'):
        if p.is_file():
            s = p.stat(); stamp.append((str(p.relative_to(folder)), s.st_size, s.st_mtime_ns, s.st_ctime_ns, digest(p) if p.suffix in ('.xml','.urdf') else None))
        if len(stamp) > 5000: raise ValueError('机器人资源过多，暂不使用结果缓存')
    stamp = hashlib.sha256(json.dumps(sorted(stamp)).encode()).hexdigest()
    # The cache is shared by short-lived workers, and invalidated by all model,
    # interpreter, native-runtime and signature-code file identities.
    interpreter=PROJECT/'.retarget-venv/bin/python'
    runtime_paths={interpreter,Path(__file__).resolve()}
    for pattern in ('mujoco*/*.so*','numpy*/*.so*','numpy/core/*.so*','numpy/_core/*.so*',
                    'mujoco*.dist-info/METADATA','numpy*.dist-info/METADATA'):
        runtime_paths.update((PROJECT/'.retarget-venv').glob('lib/python*/site-packages/'+pattern))
    runtime=[]
    for p in sorted(runtime_paths):
        s=p.stat();runtime.append((str(p),s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
    runtime.append(('signature_code_sha256',digest(Path(__file__))))
    token=hashlib.sha256(json.dumps([robot,stamp,runtime]).encode()).hexdigest()
    key=(robot,token)
    def valid(value):
        return (isinstance(value,dict) and set(value)=={'sha256','mujoco','numpy'}
                and isinstance(value['sha256'],str) and len(value['sha256'])==64
                and all(c in '0123456789abcdef' for c in value['sha256'])
                and all(isinstance(value[n],str) for n in ('mujoco','numpy')))
    with _lock:
        if key not in _models:
            import fcntl
            import secrets
            cache=PROJECT/'var/retarget-model-signatures';cache.mkdir(mode=0o700,parents=True,exist_ok=True)
            target=cache/(token+'.json')
            with (cache/(token+'.lock')).open('a') as gate:
                fcntl.flock(gate,fcntl.LOCK_EX)
                value=None
                try:
                    if target.stat().st_size<4096:
                        candidate=json.loads(target.read_text())
                        if valid(candidate):value=candidate
                except (OSError,ValueError):pass
                if value is None:
                    env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',CUDA_VISIBLE_DEVICES='')
                    result=subprocess.run([str(interpreter),str(Path(__file__)),
                        '--model',str(model_path(robot))],env=env,capture_output=True,text=True,timeout=30,check=True)
                    value=json.loads(result.stdout)
                    if not valid(value):raise ValueError('Invalid compiled model signature')
                    temporary=cache/(token+'.'+secrets.token_hex(6)+'.tmp')
                    temporary.write_text(json.dumps(value));temporary.chmod(0o600);os.replace(temporary,target)
                if len(_models)>20:_models.clear()
                _models[key]=value
        return dict(_models[key],asset_stamp=stamp)

def identity(root, mid, robot, execution, detail=None, *, owner=None):
    if robot.startswith('robot_'):return None  # Custom results are pinned, never reuse a built-in robot cache.
    if mid.startswith('mimp'):
        from gadget_imports import Imports
        source = Imports(root).get(mid)['sha256']
    elif mid.startswith('ma_'):
        if detail and isinstance(detail.get('location'), dict) and detail['location'].get('sha256'):
            source = detail['location']['sha256']
        elif owner:
            from g2h.motion_assets import MotionAssets
            source = MotionAssets(root).get(owner, mid)['sha256']
        else:
            return None
    else:
        if detail is None:
            from g2h.farm_index import lookup_motion
            import sqlite3
            with closing(sqlite3.connect((root / 'prompts.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                detail = lookup_motion(db, root, mid)
        location = detail.get('location')
        if not isinstance(location, dict) or not location.get('sha256'): return None
        source = location['sha256']
    engine = engine_for(root, mid, robot)
    names = set(ADAPTERS) | {engine, 'retarget_robots.py', 'retarget_cache.py'}
    code = {name: digest(PROJECT / 'scripts' / name) for name in sorted(names)}
    package = PROJECT / 'third_party/holosoma/holosoma_retargeting'
    for part in ('config_types', 'src'):
        for path in sorted((package / part).glob('*.py')):
            code[str(path.relative_to(PROJECT))] = digest(path)
    code['data/dynamics/unitree_g1_23dof.json'] = digest(PROJECT/'data/dynamics/unitree_g1_23dof.json')
    code['g2h/human_rig.py'] = digest(PROJECT/'g2h/human_rig.py')
    code['g2h/motion_semantics.py'] = digest(PROJECT / 'g2h/motion_semantics.py')
    code['robot_retarget'] = digest(package / 'examples/robot_retarget.py')
    from g2h.human_rig import load
    rest, parents = load()
    template = hashlib.sha256(rest.tobytes() + parents.tobytes()).hexdigest()
    from retarget_quality import retarget_requires_dynamics
    body = dict(version=1, motion_id=mid, source_sha256=source, robot=robot,
                execution=effective(execution), engine=engine, code=code,
                template=template, model=model_signature(robot),
                dynamics_required=retarget_requires_dynamics())
    body['key'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body

def legacy_matches(root, row, context, metadata):
    # Old exports lack a full cache manifest. Require their recorded solver,
    # quality gate, robot collision identity and source-world hash to agree.
    if context['engine'] != 'retarget_engine.py': return False
    if metadata.get('motion_id') != row['motion_id'] or metadata.get('robot') != row['robot']: return False
    if not metadata.get('quality', {}).get('export_allowed'): return False
    if metadata.get('engine', {}).get('sha256') != context['code'][context['engine']]: return False
    if any(metadata.get('adapter_sources', {}).get(n) != context['code'][n] for n in ADAPTERS): return False
    if metadata.get('robot_collision_identity', {}).get('sha256') != context['model']['sha256']: return False
    if effective(metadata.get('execution', {})) != context['execution']: return False
    import numpy as np
    folder = root / 'review/retarget' / row['id']
    try:
        with np.load(folder / 'result.npz', allow_pickle=False) as data:
            if json.loads(str(data['metadata'])) != metadata: return False
    except (OSError, ValueError, KeyError): return False
    # Files absent from old provenance must predate the old job.
    for name in context['code']:
        if name in ADAPTERS or name in ('retarget_cache.py', context['engine']): continue
        path = (PROJECT / 'third_party/holosoma/holosoma_retargeting/examples/robot_retarget.py'
                if name == 'robot_retarget' else PROJECT / name if '/' in name else PROJECT / 'scripts' / name)
        if path.stat().st_mtime > row['created']: return False
    from retarget_engine import world_points
    from g2h.farm_index import read_motion
    import sqlite3
    with closing(sqlite3.connect((root / 'prompts.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        record = read_motion(db, root, row['motion_id'])
    from g2h.human_rig import motion_rig
    template,_ = motion_rig(record['motion'])
    world = world_points(record['motion'], template)
    return metadata.get('source_sha256') == hashlib.sha256(world.astype('<f8').tobytes()).hexdigest()

def seal(folder, context):
    if not context or not all((folder / n).is_file() for n in FILES): return
    proof = dict(context=context, files={n: digest(folder / n) for n in FILES})
    tmp = folder / 'reuse-manifest.tmp'
    tmp.write_text(json.dumps(proof, sort_keys=True)); os.replace(tmp, folder / 'reuse-manifest.json')

def reuse(queue, db, key, owner, mid, robot, execution, context, *, reuse_prior_parameters=False):
    # Gadget may keep a previous execution's parameters. Source, solver, robot
    # and validation policy must still match the current request.
    if not context or not context.get('key'): return None
    from retarget_quality import retarget_export_verified
    # Explicit index and bounded candidates. Never cross account boundaries.
    rows = db.execute("SELECT * FROM jobs WHERE owner=? AND motion_id=? AND robot=? AND status='done' AND id<>? ORDER BY created DESC LIMIT 100",
                      (owner, mid, robot, key)).fetchall()
    for row in rows:
        folder = queue.folder / row['id']
        try:
            if not all((folder / n).is_file() and (folder / n).stat().st_size for n in FILES): continue
            if not retarget_export_verified(json.loads((folder / 'quality.json').read_text())): continue
            proof_path = folder / 'reuse-manifest.json'
            if proof_path.exists():
                proof = json.loads(proof_path.read_text())
                previous = effective(row['execution'])
                expected_key = context['key']
                if previous != effective(execution):
                    if not reuse_prior_parameters: continue
                    expected_context = {k:v for k,v in context.items() if k != 'key'}
                    expected_context['execution'] = previous
                    expected_key = hashlib.sha256(json.dumps(expected_context, sort_keys=True).encode()).hexdigest()
                if proof.get('context', {}).get('key') != expected_key: continue
                if any(digest(folder / n) != proof['files'].get(n) for n in FILES): continue
            else:
                # Unsealed legacy results remain available as historical jobs.
                continue
            recorded=json.loads((folder / 'preview.json').read_text()).get('metadata',{}).get('execution')
            if recorded and effective(recorded) != previous: continue
            actual=json.dumps(recorded,sort_keys=True) if recorded else row['execution']
            target = queue.folder / key
            target.mkdir(exist_ok=True)
            linked = []
            try:
                for name in (*FILES, 'reuse-manifest.json'):
                    os.link(folder / name, target / name); linked.append(target / name)
            except OSError:
                for path in linked: path.unlink()
                continue
            return dict(source=row['reused_from'] or row['id'], done=row['done'], total=row['total'],
                        execution=actual, cache_key=expected_key)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None

if __name__ == '__main__':
    import numpy as np
    import mujoco
    model = mujoco.MjModel.from_xml_path(sys.argv[2])
    h = hashlib.sha256()
    for field in ('body_parentid','body_pos','body_quat','jnt_type','jnt_pos','jnt_axis','jnt_range','qpos0','geom_bodyid','geom_type','geom_size','geom_pos','geom_quat','geom_contype','geom_conaffinity','mesh_vert','mesh_face'):
        a = np.asarray(getattr(model, field)); h.update(field.encode()); h.update(str(a.shape).encode()); h.update(a.tobytes())
    print(json.dumps(dict(sha256=h.hexdigest(), mujoco=mujoco.__version__, numpy=np.__version__)))


@lru_cache(maxsize=256)
def _version_info(path, identity):
    metadata = json.loads(Path(path).read_text()).get('metadata', {})
    execution = metadata.get('execution', {})
    preset = execution.get('preset') or {}
    parameters = execution.get('parameters')
    if parameters is not None and not isinstance(parameters, dict):
        return None
    retry = metadata.get('continuity_retry') or {}
    if parameters is not None and retry.get('selected') is True and 'smooth_weight' in retry:
        parameters = dict(parameters, smooth_weight=retry['smooth_weight'])
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(',', ':'), allow_nan=False)
    parameter_hash = hashlib.sha256(parameters_json.encode()).hexdigest() if parameters is not None else None
    engine = metadata.get('engine', {})
    version = dict(
        algorithm=engine.get('name') or metadata.get('adapter_version') or '未记录',
        algorithm_version=engine.get('sha256') or metadata.get('adapter_version') or '未记录',
        parameter_version=('方案 '+preset.get('name',preset.get('id',''))+' v'+str(preset.get('version')))
            if preset else ('参数 '+parameter_hash[:12] if parameter_hash else '未记录'),
        parameter_schema=execution.get('schema_version') or '未记录',
        generated_at=metadata.get('created_utc') or '未记录',
    )
    if not all(isinstance(value, str) for value in version.values()):
        return None
    return dict(version, parameters=parameters)


def version_info(folder):
    path = folder / 'preview.json'
    try:
        info = path.stat()
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return _version_info(str(path), identity)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None
