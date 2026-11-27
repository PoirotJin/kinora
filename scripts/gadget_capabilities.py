"""Discoverable execution contract; identities explicitly distinguish stat from content hashes."""
from pathlib import Path
import hashlib

def capabilities():
    from retarget_parameters import schema
    from g2h.generation_backend import current_recipe, HY_MODEL, KIMODO_MODEL, kimodo_paths, kimodo_readiness
    p=Path(__file__).resolve().parents[1];checkpoint=p/'ckpts/tencent/HY-Motion-1.0/latest.ckpt';config=checkpoint.parent/'config.yml'
    model=dict(id='HY-Motion-1.0',identity_kind='file-stat-v1',identity_available=False)
    if checkpoint.exists():
        stat=checkpoint.stat();model.update(identity_available=True,bytes=stat.st_size,mtime_ns=stat.st_mtime_ns)
    if config.exists():model['config_sha256']=hashlib.sha256(config.read_bytes()).hexdigest()
    recipe = current_recipe()
    model['default'] = recipe['model'] == HY_MODEL
    models = [model]
    paths = kimodo_paths(p)
    if paths['source'].is_dir() or recipe['model'] == KIMODO_MODEL:
        checkpoint = paths['checkpoint'] / 'model.safetensors'
        config = paths['checkpoint'] / 'config.yaml'
        readiness = kimodo_readiness(p)
        ready = readiness['available']
        kimodo = dict(id=KIMODO_MODEL, identity_kind='file-stat-v1', identity_available=checkpoint.is_file(),
                      default=recipe['model'] == KIMODO_MODEL, readiness='ready' if ready else 'requires_runtime_preflight',
                      assets_present=ready, duration_min_seconds=1, duration_max_seconds=10,
                      source_skeleton='somaskel77', output_representation='human_smplh_y_up_v1')
        if checkpoint.is_file():
            stat=checkpoint.stat();kimodo.update(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns)
        if config.is_file():kimodo['config_sha256']=hashlib.sha256(config.read_bytes()).hexdigest()
        models.append(kimodo)
    return dict(models=models,generation_recipe=recipe,parameter_schema=schema(),task_schema_version='task-v1',poll_interval_seconds=10,
        idempotency=dict(generate='request_id: 8–80 safe characters',retarget='id: 32 lowercase hex; exact execution parameters must match'),
        steps=['search','motion','report','generate','retarget','retarget_report','source','download','cancel_generate','cancel'],
        search_filters=['q','topic','status','tag_group','tag','tagged_by','author','robot','duration_min','duration_max','quality','collection'],
        reproducibility=dict(seed='Optional 32-bit seed, distinct from allocation ID',parent_request_id='Optional owned parent generation request',parent_job_id='Optional owned parent retarget job',guarantee='Recipe only; matching seed is not a cross-runtime bitwise guarantee'))

def task(kind,row,references=None):
    status=row['status'];canonical={'preparing':'running','ready':'queued','stopping':'cancelling','completed':'succeeded','done':'succeeded','rejected':'failed'}.get(status,status)
    return dict(schema_version='task-v1',id=row.get('id',row.get('request_id')),kind=kind,status=canonical,raw_status=status,
        terminal=canonical in ('succeeded','failed','cancelled'),error=dict(code=status,message=row.get('message','')) if canonical in ('failed','cancelled') else None,
        message=row.get('message',''),results=references or [],poll=dict(endpoint=kind+'?id='+row.get('id',row.get('request_id','')),after_seconds=10))
