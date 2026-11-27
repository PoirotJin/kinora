"""Owner-scoped recovery through the existing quota-controlled queues, never inline GPU work."""
from contextlib import closing
import json
import sqlite3
import time
from g2h.motion_semantics import for_record, message


def source_record(root, mid):
    from g2h.farm_index import read_motion
    with closing(sqlite3.connect('file:'+str(root/'prompts.sqlite3')+'?mode=ro',uri=True)) as db:
        return read_motion(db,root,mid)


def source_scene(record):
    if 'scene' in record:
        return record['scene']
    motion = record.get('motion', {})
    if 'scene_spec_json' in motion:
        from g2h.human_interaction import decode
        return decode(motion['scene_spec_json'], len(motion['native_transl']))
    return None


class MotionStore:
    def __init__(self, root): self.root=root
    def detail(self, mid):
        from g2h.farm_index import motion_number, motion_info
        with closing(sqlite3.connect('file:'+str(self.root/'prompts.sqlite3')+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links WHERE number=?',(motion_number(mid),)).fetchone()
        if not row:raise ValueError('重试源动作不存在')
        return motion_info(self.root,row)


def submit_retry(queue, row, value, user):
    try:
        queue.submit(value,user,MotionStore(queue.root),handoff_from=row['id'])
        return True
    except PermissionError as exc:
        if '队列已满' not in str(exc):raise
        queue.update(row['id'],status='queued',retry_at=time.time()+30,
                     message='方向纠正等待队列空位；失败候选已隔离')
        return False


def cancel_recovery(queue, row):
    from review_custom import Custom

    recovery = queue.folder / row['id'] / 'semantic-recovery.json'
    request_id = 'direction-' + row['id']
    custom_db = queue.root / 'review/custom/jobs.sqlite3'
    if not custom_db.is_file():
        return
    # A committed request can outlive a worker interrupted before it recorded the checkpoint.
    custom = Custom(queue.root)
    with closing(custom.db()) as db:
        child = db.execute('SELECT rewrite_meta FROM requests WHERE id=? AND owner=?',
                           (request_id, row['owner'])).fetchone()
    if child is None:
        return
    if json.loads(child['rewrite_meta']).get('source') != 'semantic_retry':
        try:
            recorded = json.loads(recovery.read_text()).get('request_id') == request_id
        except (FileNotFoundError, json.JSONDecodeError):
            recorded = False
        if not recorded:
            return
    custom.cancel(request_id, {'username': row['owner'], 'role': 'user'})


def recovery_parent_cancelled(root, row):
    request_id = row['id']
    if not request_id.startswith('direction-') or json.loads(row['rewrite_meta']).get('source') != 'semantic_retry':
        return False
    path = root / 'review/retarget/jobs.sqlite3'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
        parent = db.execute('SELECT cancel FROM jobs WHERE id=? AND owner=?',
                            (request_id.removeprefix('direction-'), row['owner'])).fetchone()
    return bool(parent and parent[0])


def preflight(queue, row, folder):
    """Return False while recovery is queued or handed off to a NEW retarget job."""
    if row['motion_id'].startswith(('mimp', 'ma_')):
        return True  # imported or edited source lacks legacy farm catalog link; robot check still runs
    from review_custom import Custom
    from review_access import active_account
    role=active_account(queue.root,row['owner'])
    user=dict(username=row['owner'],role=role)
    if queue.get(row['id'],user)['cancel']:
        cancel_recovery(queue, row)
        queue.update(row['id'],status='cancelled',message='已取消方向纠正');return False
    recovery=folder/'semantic-recovery.json'
    if recovery.exists():
        state=json.loads(recovery.read_text());custom=Custom(queue.root)
        with closing(custom.db()) as db:
            generation=db.execute('SELECT status,message,scene FROM requests WHERE id=? AND owner=?',(state['request_id'],row['owner'])).fetchone()
        if not generation:raise ValueError('方向纠正作业丢失，已停止重试')
        if generation['status'] in ('failed','cancelled'):
            return True
        if generation['status']!='completed':
            queue.update(row['id'],status='queued',retry_at=time.time()+30,
                         message=state['reason']+'；已自动提交人体重新生成：'+state['request_id']+'；'+generation['message']);return False
        original_scene = source_scene(source_record(queue.root, row['motion_id']))
        recovered_scene = json.loads(generation['scene']) if generation['scene'] is not None else None
        if recovered_scene != original_scene:
            queue.update(row['id'], status='failed', message='方向纠正未保留原场景约束，已停止转入重定向')
            return False
        # At most three candidates, so one 50-item page covers this request.
        candidates=custom.results(state['request_id'],0,user)['items']
        mid=next((r['motion_id'] for r in candidates if r.get('state')=='done'),None)
        if not mid:raise ValueError('方向纠正生成完成但未找到可用源动作')
        import hashlib
        child=hashlib.sha256(('semantic-child:'+row['id']).encode()).hexdigest()[:32]
        execution = json.loads(row['execution'])
        request = dict(id=child, motion_id=mid, robot=row['robot'], parent_job_id=row['id'], force_new=True)
        preset = execution.get('preset')
        if preset:
            request.update(preset_id=preset['id'], preset_version=preset['version'])
        else:
            request['parameters'] = execution.get('parameters', {})
        if not submit_retry(queue, row, request, user):
            return False
        queue.update(row['id'],status='failed',message=state['reason']+'；原结果保留，已转入纠正作业 '+child+'，新动作 '+mid)
        return False
    record=source_record(queue.root,row['motion_id'])
    report=for_record(record)
    (folder/'source-semantic.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if report['status']!='fail':return True
    reason=message(report);request_id='direction-'+row['id']
    request = dict(request_id=request_id, description=record['text'], n=1,
                   duration=int(round(record['duration_s'])), prompt_mode='exact_english',
                   original_description=record.get('farm', {}).get('source', {}).get('original_description', ''))
    scene = source_scene(record)
    if scene is not None:
        request['scene'] = scene
    # Serialize with parent cancellation until the child is discoverable.
    with closing(queue.db()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if queue._finish_pending_cancellation(db, row['id'], time.time()):
            return False
        Custom(queue.root).submit(request, row['owner'], source='semantic_retry')
        from qwen_motion_agent import checkpoint_json
        try:
            checkpoint_json(recovery, dict(request_id=request_id, source_motion_id=row['motion_id'], reason=reason))
        except OSError:
            cancel_recovery(queue, row)
            raise
        now = time.time()
        db.execute("UPDATE jobs SET status='queued',retry_at=?,message=?,updated=? WHERE id=?",
                   (now+30, reason+'；已自动提交人体重新生成：'+request_id, now, row['id']))
    return False


def retry_robot(queue, row, report):
    """At most two corrections, linked by parent_job_id; stronger heading objective."""
    from review_access import active_account
    role=active_account(queue.root,row['owner']);user=dict(username=row['owner'],role=role)
    execution=json.loads(row['execution']);ancestor=execution.get('parent_job_id');attempt=1
    while ancestor and attempt<3:
        previous=queue.get(ancestor,user)
        if previous['motion_id']!=row['motion_id']:break
        attempt+=1;ancestor=json.loads(previous['execution']).get('parent_job_id')
    reason=message(report)
    from retarget_cache import engine_for
    if engine_for(queue.root,row['motion_id'],row['robot'])=='retarget_engine_upright_v4.py':
        queue.update(row['id'],status='failed',message=reason+'；该研究批次使用固定版本求解器，不能调整朝向权重重试');return
    if attempt>=3:
        queue.update(row['id'],status='failed',message=reason+'；3 次转换后仍不符，重试已停止');return
    import hashlib
    child=hashlib.sha256(('semantic-robot:'+row['id']).encode()).hexdigest()[:32]
    parameters=execution.get('parameters',{}).copy()
    parameters['root_orientation_weight']=min(200.,parameters.get('root_orientation_weight',40.)*2)
    request=dict(id=child,motion_id=row['motion_id'],robot=row['robot'],parameters=parameters,
                 parent_job_id=row['id'],force_new=True)
    custom_version=(execution.get('custom_robot') or {}).get('version')
    if row['robot'].startswith('robot_') and custom_version:
        request['robot_version']=custom_version
    if not submit_retry(queue,row,request,user):return
    queue.update(row['id'],status='failed',message=reason+f'；已提高朝向保持权重，自动重试 {attempt+1}/3：'+child)
