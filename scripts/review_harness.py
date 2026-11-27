"""Authenticated browser routes for the MotionFarm task workspace.

The domain service owns task state. This module only validates HTTP input and
uses the session account; a request can never select its own owner or paths.
"""
import re
import math
from urllib.parse import parse_qs

from g2h.api_usage import QuotaExceeded as ApiQuotaExceeded
from g2h.capacity import CapacityUnavailable
from g2h.motion_assets import SavedMotionUnavailable
from g2h.resource_quota import QuotaExceeded as ResourceQuotaExceeded
from review_access import Forbidden

BASE = '/api/harness'
IDENTIFIER = r'[A-Za-z0-9_-]{1,96}'


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError('请求包含不支持的字段')
    if any(key not in value for key in required):
        raise ValueError('请求字段不完整')
    return value


def create_input(value):
    fields(value, ('request_id', 'goal', 'requirements', 'source_motion_id', 'selection', 'budget'), ('request_id', 'goal'))
    if not isinstance(value['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', value['request_id']):
        raise ValueError('无效请求编号')
    if not isinstance(value['goal'], str) or not 1 <= len(value['goal'].strip()) <= 4000:
        raise ValueError('请填写不超过 4000 字的动作要求')
    motion_id = value.get('source_motion_id')
    if motion_id is not None and (not isinstance(motion_id, str) or not re.fullmatch(IDENTIFIER, motion_id)):
        raise ValueError('无效动作编号')
    selection = value.get('selection')
    if selection is not None:
        fields(selection, ('start_frame', 'end_frame'), ('start_frame', 'end_frame'))
        start, end = selection['start_frame'], selection['end_frame']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 100000:
            raise ValueError('请选择有效的起止帧，结束帧不包含在选区内')
        if not motion_id:
            raise ValueError('局部修改必须选择原动作')
    budget = value.get('budget')
    if budget is not None:
        fields(budget, ('max_attempts', 'max_cost_usd'), ('max_attempts',))
        if type(budget['max_attempts']) is not int or not 1 <= budget['max_attempts'] <= 20:
            raise ValueError('尝试上限应为 1 至 20 次')
        cost=budget.get('max_cost_usd',0.0)
        if type(cost) not in (int,float) or not math.isfinite(cost) or not 0 <= cost <= 1000:
            raise ValueError('费用上限应为 0 至 1000 美元')
    requirements(value.get('requirements'))
    return value


def requirements(value):
    if value is not None and (not isinstance(value, list) or len(value) > 50):
        raise ValueError('要求列表过长或格式不正确')


def service(handler):
    existing = getattr(handler.server, 'harness_service', None)
    if existing is not None:
        return existing
    from harness_service import HarnessService
    # Service instances are cheap. Domain storage handles concurrent callers;
    # no global model or mutable per-account request state is cached here.
    return HarnessService(handler.server.store.root, legacy_store=handler.server.store)


def handle(handler, parsed, write, user):
    """Return a normal HTTP response; caller has checked session, role and CSRF."""
    path = parsed.path
    if not user or not isinstance(user.get('username'), str) or not user['username']:
        raise Forbidden('请登录后继续')
    svc = service(handler)
    query = parse_qs(parsed.query)
    try:
        from personal_assets import BASE as ASSETS_BASE
        if path.startswith(ASSETS_BASE + '/'):
            from personal_assets import handle as assets
            return assets(handler, svc, user, path, query, write)
        if path == BASE + '/dynamics-validation' and write:
            from robot_editor import Editor
            return handler.send(202,Editor(handler.server.store.root).validate_existing(user,handler.body(limit=8192)))
        if path == BASE + '/robot-edits' and write:
            from robot_editor import Editor
            return handler.send(201,Editor(handler.server.store.root).create(user,handler.body(limit=24576)))
        editor = re.fullmatch(BASE+r'/robot-edits/([a-f0-9]{32})(?:/(preview|model|edit|history|download|import-repair))?',path)
        if editor:
            from robot_editor import Editor, EditorTrajectoryMissing, EditorVersionConflict
            e=Editor(handler.server.store.root);sid,action=editor.groups()
            if action is None and not write:return handler.send(200,e.detail(user,sid))
            if action=='model' and not write:return handler.send(200,e.render_model(user,sid))
            try:
                if action=='preview' and not write:return handler.send(200,e.preview(user,sid,query.get('version',[None])[0]))
                if action=='download' and not write:return handler.send(200,e.download(user,sid,query.get('version',[None])[0]),'application/octet-stream',attachment='robot-edited.npz')
                if action=='import-repair' and write:
                    from robot_repair import UPLOAD_LIMIT
                    return handler.send(202,e.import_repair(user,sid,handler.body(limit=UPLOAD_LIMIT)))
                if action in ('edit','history') and write:
                    operation=e.edit if action=='edit' else e.move
                    return handler.send(200,operation(user,sid,handler.body(limit=65536)))
            except EditorVersionConflict as exc:
                return handler.send(409, {'error':str(exc)})
            except EditorTrajectoryMissing as exc:
                return handler.send(410, {'error':str(exc)})
        if path == BASE + '/robots':
            from custom_robots import CustomRobots
            robots = CustomRobots(handler.server.store.root, user)
            return handler.send(201 if write else 200, robots.import_model(handler.body(limit=45*1024*1024)) if write else robots.list())
        robot_preview = re.fullmatch(BASE + r'/robots/(robot_[0-9a-f]{32})/preview', path)
        if robot_preview and not write:
            from custom_robots import CustomRobots
            return handler.send(200, CustomRobots(handler.server.store.root, user).preview(robot_preview[1]))
        robot_profile = re.fullmatch(BASE + r'/robots/(robot_[0-9a-f]{32})/profiles', path)
        if robot_profile:
            from custom_robot_profiles import configure,listing
            return handler.send(201 if write else 200, configure(handler.server.store.root,user,robot_profile[1],handler.body(limit=24576)) if write else listing(handler.server.store.root,user,robot_profile[1]))
        if path == BASE + '/export' and not write:
            if set(query)!={'ids','format'} or any(len(v)!=1 for v in query.values()):raise ValueError('导出参数无效')
            from motion_exports import BatchExportFailed, export_batch
            try:
                bundle = export_batch(svc, user, query['ids'][0].split(','), query['format'][0])
            except BatchExportFailed as exc:
                return handler.send(exc.status, {'error':str(exc),'failures':exc.failures})
            return handler.send(200, bundle, 'application/zip', attachment='kinora-motions.zip')
        if path == BASE + '/results' and not write:
            return handler.send(200,svc.results(user))
        if path == BASE + '/capabilities' and not write:
            return handler.send(200, svc.capabilities(user))
        if path == BASE + '/video-motion':
            import video_motion
            if not write:
                folder = video_motion.owner_folder(svc,user)
                jobs = sorted(folder.glob('*/job.json'), key=lambda p:p.stat().st_mtime,reverse=True)
                return handler.send(200,dict(items=[video_motion.status(svc,user,p.parent.name) for p in jobs[:30]]))
            value = handler.body(limit=45*1024*1024)
            fields(value,('request_id','video_base64'),('request_id','video_base64'))
            return handler.send(202,video_motion.submit(svc,user,value))
        video = re.fullmatch(BASE+r'/video-motion/([0-9a-f]{64})',path)
        if video and not write:
            import video_motion
            return handler.send(200,video_motion.status(svc,user,video[1]))
        if path == BASE + '/paired-scene' and write:
            value = handler.body(limit=4096)
            fields(value, ('request_id','kind','duration','x','z'), ('request_id','kind','duration'))
            if not isinstance(value['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                raise ValueError('无效场景请求编号')
            return handler.send(202,svc.paired_scene(user,value))
        if path == BASE + '/scene' and write:
            value = handler.body(limit=96*1024)
            fields(value, ('request_id','text','duration','scene'), ('request_id','text','duration','scene'))
            if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                raise ValueError('无效场景请求编号')
            return handler.send(202,svc.scene(user,value))
        if path == BASE + '/multi-scene' and write:
            value = handler.body(limit=128*1024)
            fields(value, ('request_id','duration','actors','objects','interactions'), ('request_id','duration','actors'))
            if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                raise ValueError('无效场景请求编号')
            return handler.send(202,svc.multi_scene(user,value))
        if path == BASE + '/object-response' and write:
            value = handler.body(limit=64*1024)
            fields(value, ('request_id','source_motion_id','objects','attachment'), ('request_id','source_motion_id','objects','attachment'))
            if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                raise ValueError('无效请求编号')
            if not isinstance(value['source_motion_id'],str) or not re.fullmatch(IDENTIFIER,value['source_motion_id']):
                raise ValueError('无效动作编号')
            return handler.send(200,svc.object_response(user,value))
        pair = re.fullmatch(BASE + r'/tasks/(' + IDENTIFIER + r')/paired-preview',path)
        if pair and not write:
            return handler.send(200,svc.paired_preview(user,pair[1]))
        multi_prev = re.fullmatch(BASE + r'/tasks/(' + IDENTIFIER + r')/multi-preview',path)
        if multi_prev and not write:
            return handler.send(200,svc.multi_preview(user,multi_prev[1]))
        if path == BASE + '/tasks':
            if write:
                return handler.send(201, svc.create(user, create_input(handler.body(limit=24576))))
            if set(query) - {'limit', 'after', 'grouped'}:
                raise ValueError('不支持的任务筛选')
            return handler.send(200, svc.tasks(user, query))
        if path == BASE + '/splice-preview' and write:
            return handler.send(200,svc.splice_preview(user,handler.body(limit=8192)))
        if path == BASE + '/splice' and write:
            value = handler.body(limit=8192)
            fields(value, ('request_id', 'clips', 'transition_frames', 'seed'), ('request_id', 'clips', 'transition_frames'))
            if not isinstance(value['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', value['request_id']):
                raise ValueError('无效拼接请求编号')
            for clip in value['clips'] if isinstance(value['clips'], list) else []:
                if not isinstance(clip, dict) or not isinstance(clip.get('motion_id'), str) or not re.fullmatch(IDENTIFIER, clip['motion_id']):
                    raise ValueError('无效动作编号')
            return handler.send(200, svc.splice(user, value))
        download = re.fullmatch(BASE + r'/motions/(' + IDENTIFIER + r')/download', path)
        if download and not write:
            if set(query)-{'format'} or any(len(v)!=1 for v in query.values()):
                raise ValueError('下载参数无效')
            from motion_exports import ExportReadFailed, export_one
            format=query.get('format',['npz'])[0]
            try:
                output = export_one(svc,user,download[1],format)
            except ExportReadFailed as exc:
                return handler.send(503, {'error':str(exc)})
            return handler.send(200, output, 'application/octet-stream',
                                attachment=download[1] + '.' + format)
        preview = re.fullmatch(BASE + r'/motions/(' + IDENTIFIER + r')/preview', path)
        if preview and not write:
            if 'snapshot' in query or 'source' in query:
                if set(query) != {'snapshot', 'source'} or any(len(values) != 1 for values in query.values()):
                    raise ValueError('快照预览参数无效')
                from personal_assets import Library
                clip = Library(svc, user).preview_snapshot_member(query['snapshot'][0], query['source'][0], preview[1])
                return handler.send(200, clip)
            return handler.send(200, svc.preview(user, preview[1]))
        task = re.fullmatch(BASE + r'/tasks/(' + IDENTIFIER + r')(?:/(feedback|advance|cancel|accept|import-patch|repair|repair-status|scene-status|scene-resume))?', path)
        if task:
            task_id, action = task.groups()
            if action is None and not write:
                return handler.send(200, svc.detail(user, task_id))
            if action and write:
                value = handler.body(limit=6*1024*1024 if action == 'import-patch' else 24576)
                if action == 'repair':
                    fields(value, ('request_id','prompt','prompt_mode','rewrite_model','seed'), ('request_id','prompt'))
                    if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                        raise ValueError('无效修复请求编号')
                    result = svc.repair(user,task_id,value)
                elif action == 'repair-status':
                    fields(value, ())
                    result = svc.repair_status(user,task_id)
                elif action == 'scene-resume':
                    fields(value, ())
                    return handler.send(202, svc.resume_multi_scene(user, task_id))
                elif action == 'scene-status':
                    fields(value, ())
                    result = svc.scene_status(user,task_id)
                elif action == 'import-patch':
                    fields(value, ('request_id','npz_base64'), ('request_id','npz_base64'))
                    if not isinstance(value['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value['request_id']):
                        raise ValueError('无效导入请求编号')
                    if not isinstance(value['npz_base64'],str) or len(value['npz_base64'])>5592408:
                        raise ValueError('片段文件不得超过 4 MiB')
                    result=svc.import_patch(user,task_id,value)
                elif action == 'feedback':
                    fields(value, ('message', 'requirements'), ('message',))
                    if not isinstance(value['message'], str) or not 1 <= len(value['message'].strip()) <= 4000:
                        raise ValueError('请填写不超过 4000 字的修改意见')
                    requirements(value.get('requirements'))
                    result = svc.feedback(user, task_id, value)
                elif action == 'accept':
                    fields(value, ('candidate_id',), ('candidate_id',))
                    if not isinstance(value['candidate_id'], str) or not re.fullmatch(IDENTIFIER, value['candidate_id']):
                        raise ValueError('无效候选编号')
                    result = svc.accept(user, task_id, value)
                else:
                    fields(value, ())
                    result = svc.advance(user, task_id) if action == 'advance' else svc.cancel(user, task_id)
                return handler.send(200, result)
        return handler.send(404, {'error': 'Not found'})
    except (CapacityUnavailable, SavedMotionUnavailable) as exc:
        return handler.send(503, {'error': str(exc)})
    except (ApiQuotaExceeded, ResourceQuotaExceeded):
        raise
    except PermissionError as exc:
        raise Forbidden(str(exc)) from exc
