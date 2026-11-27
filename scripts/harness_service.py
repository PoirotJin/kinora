"""Application boundary shared by the web workspace and external agent tools.

Legacy queues remain the execution authority. A harness operation keeps its own
stable request id and reconciles that request before attempting another submit.
"""
from contextlib import closing, contextmanager, nullcontext
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from g2h.harness import Conflict, Store, Harness, Tool, ToolRegistry, OpenAICompatibleDriver
from g2h.harness.contracts import RunBusy
from g2h.motion_assets import MotionArtifactCorrupt, MotionAssets, SavedMotionUnavailable
from g2h.motion_tools import MotionTools
from g2h.generation_backend import current_recipe, KIMODO_MODEL, kimodo_readiness, kimodo_edit_readiness

PROJECT = Path(__file__).resolve().parents[1]
SCENE_BUDGET = {'max_attempts': 1, 'max_candidates': 1, 'max_cost_usd': 0}


@contextmanager
def saved_motion_read():
    try:
        yield
    except FileNotFoundError as exc:
        raise SavedMotionUnavailable('已保存的动作文件缺失，请联系管理员恢复后重试。') from exc
    except MotionArtifactCorrupt as exc:
        raise SavedMotionUnavailable('已保存的动作文件损坏，请联系管理员恢复后重试。') from exc


class HarnessService:
    def __init__(self, root, legacy_store=None, *, driver=None, assets=None, store=None):
        self.root = Path(root).resolve()
        self.legacy = legacy_store
        self.store = store or Store(self.root)
        self.assets = assets or MotionAssets(self.root)
        self.motion = MotionTools(self.assets)
        self.driver = driver

    @staticmethod
    def owner(user):
        from review_access import Forbidden
        if not isinstance(user, dict) or not user.get('username'):
            raise Forbidden('请登录后访问动作任务')
        return user['username']

    def _task(self, user, task_id):
        return self.store.get_task(self.owner(user), task_id)

    @staticmethod
    def _base(task):
        return task.get('current_motion_id') or task.get('source_motion_id')

    def tasks(self, user, query=None):
        query = query or {}
        limit = query.get('limit', ['50'])
        limit = int(limit[0] if isinstance(limit, list) else limit)
        if not 1 <= limit <= 100:
            raise ValueError('每页任务数必须在 1–100 之间')
        grouped = query.get('grouped', ['0'])
        if grouped not in (['0'], ['1']):
            raise ValueError('无效的任务分组筛选')
        return {'items': self.store.list_tasks(self.owner(user), limit=limit, grouped=grouped == ['1'])}

    def _source(self, user, motion_id):
        """Import only a source already authorized by the existing library."""
        owner = self.owner(user)
        if not isinstance(motion_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', motion_id):
            raise ValueError('无效动作编号')
        if motion_id.startswith('ma_'):
            return self.assets.get(owner, motion_id)
        if self.legacy is None:
            raise ValueError('动作不存在或当前不可访问')
        from gadget_library import source_npz
        from gadget_imports import validate_npz
        raw = source_npz(self.legacy, motion_id, user)
        arrays = validate_npz(raw)
        return self.assets.register(owner, arrays, description='来自动作库 ' + motion_id,
                                    request_id='library:' + motion_id)

    def results(self, user):
        owner=self.owner(user);items=[]
        for task in self.store.list_tasks(owner,limit=100):
            detail=self.store.get_task(owner,task['id'])
            for c in detail['candidates']:
                items.append(dict(motion_id=c['motion_id'],task_id=task['id'],label=task['goal'],
                    status=c['status'],accepted=(detail.get('status')=='accepted'
                        and detail.get('current_motion_id')==c['motion_id']),created=c['created']))
        return {'items':sorted(items,key=lambda c:c['created'],reverse=True),'scope':'最近 100 个编辑与拼接任务'}

    def download(self, user, motion_id):
        """Export an authorized source without registering or modifying it."""
        owner = self.owner(user)
        if not isinstance(motion_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}', motion_id):
            raise ValueError('无效动作编号')
        if motion_id.startswith('ma_'):
            with saved_motion_read():
                return self.assets.artifact_path(owner, motion_id).read_bytes()
        if self.legacy is None:
            raise ValueError('动作不存在或当前不可访问')
        from gadget_library import detail, read_motion
        from contextlib import closing
        import io
        import numpy as np
        if motion_id.startswith('mimp'):
            from gadget_imports import Imports
            motion=Imports(self.root).read(motion_id,owner)['motion']
        else:
            detail(self.legacy, motion_id, user)
            with closing(self.legacy.catalog()) as db:
                motion=read_motion(db,self.root,motion_id)['motion']
        from g2h.motion_assets import REQUIRED, validate_motion
        embedded = {'parents', 'rest_joints', 'scene_spec_json'} & motion.keys()
        arrays = validate_motion({k: np.asarray(motion[k]) for k in sorted(REQUIRED | embedded)})
        output=io.BytesIO();np.savez_compressed(output,**arrays)
        return output.getvalue()

    def create(self, user, value):
        owner = self.owner(user)
        if not isinstance(value, dict):
            raise ValueError('任务参数必须为对象')
        allowed = {'request_id', 'goal', 'requirements', 'source_motion_id', 'selection', 'budget'}
        if set(value) - allowed:
            raise ValueError('未知任务参数')
        source = value.get('source_motion_id')
        if source:
            source = self._source(user, source)['motion_id']
        selection = value.get('selection')
        if selection:
            if not source:
                raise ValueError('局部修改需要来源动作')
            # This also checks the actual frame count before task admission.
            self.assets.context(owner, source, selection.get('start_frame'), selection.get('end_frame'), padding=0)
        return self.store.create_task(owner, value.get('request_id'), value.get('goal'),
                                      requirements=value.get('requirements'), source_motion_id=source,
                                      selection=selection, budget=value.get('budget'))

    def detail(self, user, task_id):
        return self._with_splice_details(self._task(user, task_id))

    @staticmethod
    def _with_splice_details(task):
        gap_fields = ('distance_m', 'turn_deg', 'seconds', 'mean_speed_m_s')
        for candidate in task['candidates']:
            if candidate['motion_id'] != task['source_motion_id'] or candidate['operation_id'] is not None:
                continue
            provenance = candidate['metadata'].get('provenance')
            if not isinstance(provenance, dict) or provenance.get('tool') != 'splice_motions':
                continue
            report = provenance.get('join')
            planner = provenance.get('version')
            if (not isinstance(planner, str) or not planner.startswith('cubic-hermite-splice-')
                    or not isinstance(report, dict) or report.get('version') != planner
                    or report.get('selection') != task['selection']
                    or not all(field in report for field in gap_fields)):
                continue
            task['splice'] = {'joined_motion_id': candidate['motion_id'], 'selection': report['selection'],
                              'planner': planner, 'quality': report,
                              'gap': {field: report[field] for field in gap_fields}}
            break
        return task

    def feedback(self, user, task_id, value):
        if not isinstance(value, dict) or set(value) - {'message', 'requirements'}:
            raise ValueError('无效反馈参数')
        return self.store.add_feedback(self.owner(user), task_id, value.get('message'),
                                       requirements=value.get('requirements'))

    def cancel(self, user, task_id):
        from review_motion_edits import MotionEdits
        owner = self.owner(user)
        group = self.store.scene_group(owner, task_id)
        pair = self.store.scene_partner(owner, task_id)
        related = group['tasks'] if group else [pair['second_task']] if pair else []
        cancelled = self.store.cancel_task(
            owner, task_id, related=related,
            cancel_scene_jobs=lambda operation_ids: self._cancel_scene_jobs(owner, operation_ids),
        )
        MotionEdits(self.root).cancel_task(owner, task_id)
        if group or pair:
            return self.scene_status(user, task_id)
        return cancelled

    def _cancel_scene_jobs(self, owner, operation_ids):
        if not operation_ids:
            return
        from review_custom import Custom

        custom = Custom(self.root)
        user = {'username': owner, 'role': 'user'}
        with closing(custom.db()) as db:
            for operation_id in operation_ids:
                key = 'hgen_' + operation_id
                if db.execute('SELECT 1 FROM requests WHERE id=? AND owner=? AND cancel=0 AND status!=?',
                              (key, owner, 'completed')).fetchone():
                    custom.cancel(key, user)

    def accept(self, user, task_id, value):
        if not isinstance(value, dict) or set(value) != {'candidate_id'}:
            raise ValueError('请选择一个候选版本')
        task = self.store.accept_candidate(self.owner(user), task_id, value['candidate_id'])
        return self._with_splice_details(task)

    def capabilities(self, user):
        self.owner(user)
        try:
            configured = OpenAICompatibleDriver.from_env()
        except ValueError:
            configured = None
        ready = self.driver is not None or configured is not None
        domain = self.motion.capabilities()
        backends = domain['models']
        active = 'kimodo' if current_recipe()['model'] == KIMODO_MODEL else 'hy_motion'
        for item in backends:
            item.setdefault('id', item.get('name', ''))
            item.setdefault('label', {'kimodo': 'Kimodo', 'hy_motion': 'HY-Motion',
                                      'local_editor': '局部语义编辑'}.get(item['id'], item['id']))
            if item['id'] in ('hy_motion', 'kimodo'):
                item['available'] = item['id'] == active and self._generation_enabled()
                item['reason'] = '' if item['available'] else ('当前未选择该生成器' if item['id'] != active else '生成器尚未启用或未完成验证')
            if item['id'] == 'local_editor':
                item['available'] = self._editing_enabled()
                item['reason'] = '' if item['available'] else '局部修复尚未启用或未完成验证'
                item['version'] = 'kimodo-local-edit-v1'
                item['max_duration_s'] = 10
        return {'backends': backends,
                'tools': self._registry(user).schemas(),
                'driver': {'available': ready,
                           'reason': '' if ready else '尚未配置推理模型；可查看任务、检查动作和导入候选。'},
                'generation_enabled': self._generation_enabled(),
                'scene_interaction': self._scene_enabled(),
                'paired_scene': self._scene_enabled(),
                'grouped_tasks': True,
                'multi_scene': self._scene_enabled(),
                'object_response': True,
                'pose_save_request_id': True,
                'object_save_request_id': True,
                'custom_robot_import_request_id': True,
                'video_motion': self._scene_enabled() and __import__('video_motion').available(),
                'editing_enabled': self._editing_enabled(),
                'splicing_enabled': True,
                'splice_limits': self._splice_limits(),
                'edit_limits': {'min_frames':12,'max_frames':300,'fps':30,'transition_within_selection':True},
                'generation_recipe': current_recipe(),
                'retarget_enabled': self.legacy is not None and os.environ.get('MOTIONFARM_HARNESS_ENABLE_RETARGET') == '1',
                'cost_policy': '按配置的单次费用上界预留预算；不会验证或限制供应商最终账单。',
                'selection': 'half_open_frames', 'source_immutable': True,
                'cancel_semantics': '停止后续决策并取消局部修复或该场景的角色作业；其他已提交的生成或重定向作业需单独取消。'}

    def _generation_enabled(self):
        return (self.legacy is not None and os.environ.get('MOTIONFARM_HARNESS_ENABLE_GENERATION') == '1'
                and self._generation_cost() is not None
                and (current_recipe()['model'] != KIMODO_MODEL or kimodo_readiness(PROJECT)['available']))

    def _scene_enabled(self):
        return current_recipe()['model'] == KIMODO_MODEL and self._generation_enabled()

    @staticmethod
    def _splice_limits():
        from g2h.cubic_splice import LIMITS, VERSION
        return dict(LIMITS, planner=VERSION)

    def _editing_enabled(self):
        return os.environ.get('MOTIONFARM_HARNESS_ENABLE_EDITING') == '1' and kimodo_edit_readiness(PROJECT)

    @staticmethod
    def _generation_cost():
        # Local Kimodo inference makes no paid provider call. Existing queue GPU,
        # time and account limits still govern its resource use.
        if current_recipe()['model'] == KIMODO_MODEL:
            return 0.0
        try:
            value = float(os.environ['MOTIONFARM_HARNESS_GENERATION_MAX_USD'])
            return value if math.isfinite(value) and value > 0 else None
        except (KeyError, ValueError):
            return None

    def _registry(self, user):
        registry = ToolRegistry()
        def inspect(context, args, operation_id):
            mid = args.get('motion_id') or self._base(context['task'])
            source = self._source(user, mid)
            report = self.motion.inspect(context['owner'], source['motion_id'])
            return {'state': 'succeeded', 'result': report,
                    'evidence': [{'kind': 'motion_inspection', 'status': 'unknown', 'body': report}]}
        registry.register(Tool('inspect_motion', '测量指定动作；语义和机器人动力学未经验证。',
            {'type': 'object', 'properties': {'motion_id': {'type': 'string'}}, 'additionalProperties': False},
            inspect, side_effect=False))

        def search(context, args, operation_id):
            if self.legacy is None:
                return {'state': 'failed', 'result': {'code': 'library_unavailable', 'message': '动作库尚未连接。'}}
            from gadget_library import search as library_search
            query = {'q': [args['query']], 'limit': [str(args.get('limit', 10))],
                     'after': [str(args.get('after', 0))]}
            return {'state': 'succeeded', 'result': library_search(self.legacy, user, query)}
        registry.register(Tool('search_motions', '按英文关键词查询可访问的现有动作；这是关键词匹配。',
            {'type': 'object', 'properties': {'query': {'type': 'string', 'maxLength': 500},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                'after': {'type': 'integer', 'minimum': 0}}, 'required': ['query'],
             'additionalProperties': False}, search, side_effect=False))

        def splice(context, args, operation_id):
            task = context['task']; selection = task.get('selection')
            base = self._base(task)
            if not selection or not base:
                raise ValueError('此任务没有允许编辑的片段')
            parent = self.assets.get(context['owner'], base)
            arrays, _ = self.assets.load(context['owner'], args['patch_motion_id'])
            result = self.assets.apply_patch(context['owner'], base,
                selection['start_frame'], selection['end_frame'], arrays,
                expected_sha256=parent['sha256'], description=task['goal'], request_id=operation_id)
            return {'state': 'succeeded', 'result': result,
                    'candidates': [{'motion_id': result['motion_id'], 'metadata': result,
                                    'parent_id': base}]}
        registry.register(Tool('apply_motion_patch', '将已导入的动作片段拼回任务选区；原件和选区外数值不变。',
            {'type': 'object', 'properties': {'patch_motion_id': {'type': 'string'}},
             'required': ['patch_motion_id'], 'additionalProperties': False}, splice,
            reconcile=lambda ctx, args, op, handle: splice(ctx, args, op)))

        def repair_root(context, args, operation_id):
            task = context['task']; selection = task.get('selection')
            base = self._base(task)
            if not selection or not base:
                raise ValueError('此任务没有允许编辑的片段')
            parent = self.assets.get(context['owner'], base)
            result = self.motion.repair_root_jitter(context['owner'], base,
                selection['start_frame'], selection['end_frame'], expected_sha256=parent['sha256'],
                request_id=operation_id, **args)
            return {'state': 'succeeded', 'result': result,
                    'candidates': [{'motion_id': result['motion_id'], 'metadata': result,
                                    'parent_id': base}]}
        registry.register(Tool('repair_root_jitter', '仅在用户明确要求平滑根轨迹抖动时使用；不改变身体姿态，不代表语义修复。',
            {'type': 'object', 'properties': {'max_displacement_m': {'type': 'number', 'minimum': 0.001, 'maximum': 0.1}},
             'additionalProperties': False}, repair_root,
            reconcile=lambda ctx, args, op, handle: repair_root(ctx, args, op)))

        def reconcile_edit(context, args, operation_id, handle):
            from review_motion_edits import MotionEdits
            key = 'hedit_' + operation_id
            try:
                row = MotionEdits(self.root).get(context['owner'], key)
            except KeyError:
                return {'state':'failed','result':{'message':'修复请求未被队列接收，请调整参数后重新提交'}}
            handle = {'kind':'local_edit','request_id':key}
            if row['status'] in ('failed','cancelled'):
                return {'state':'failed','handle':handle,'result':{'message':row['message'], **row['result']}}
            if row['status'] != 'completed':
                return {'state':'waiting','handle':handle,'result':{'status':row['status'],'message':row['message']}}
            asset = row['result']['asset']; report = row['result']['validation']
            result_info = {'message':row['message'],'validation':report}
            if row['result'].get('quality_warning'):
                result_info['quality_warning'] = row['result']['quality_warning']
            return {'state':'succeeded','handle':handle,'result':result_info,
                    'candidates':[{'motion_id':asset['motion_id'],'parent_id':row['request']['parent_id'],'metadata':asset}],
                    'evidence':[{'kind':'local_edit_boundaries','status':'passed' if report.get('passed') else 'unknown','body':report},
                                {'kind':'semantic_alignment','status':'unknown','body':{'reason':'需要用户预览确认提示词匹配'}}]}

        def edit(context, args, operation_id):
            if not self._editing_enabled():
                return {'state':'failed','result':{'code':'editor_unavailable','message':'局部修复尚未启用或未完成验证'}}
            task = context['task']; base = self._base(task)
            if not base or not task.get('selection'):
                return {'state':'failed','result':{'message':'请先选择原动作和需要修复的片段'}}
            from review_motion_edits import MotionEdits
            try:
                params = dict(args)
                params.setdefault('seed', int(hashlib.sha256(operation_id.encode()).hexdigest()[:8],16))
                MotionEdits(self.root).submit(context['owner'],task['id'],'hedit_'+operation_id,base,task['selection'],**params)
            except (ValueError, PermissionError) as exc:
                return {'state':'failed','result':{'message':str(exc)}}
            return {'state':'waiting','handle':{'kind':'local_edit','request_id':'hedit_'+operation_id},
                    'result':{'message':'已提交局部修复，选区外保持不变'}}
        registry.register(Tool('edit_motion', '按提示词重新生成任务选区；边界过渡限定在选区内，结果需用户接受。英文直传，中文必须显式选择改写模型。',
            {'type':'object','properties':{'prompt':{'type':'string','minLength':3,'maxLength':2000},
                'prompt_mode':{'type':'string','enum':['exact_english','translate']},
                'rewrite_model':{'type':'string','maxLength':64},
                'seed':{'type':'integer','minimum':0,'maximum':4294967295}},
             'required':['prompt'],'additionalProperties':False}, edit, reconcile=reconcile_edit))

        def reconcile_generation(context, args, operation_id, handle):
            from review_custom import Custom
            if self.legacy is None:
                return {'state': 'unknown', 'handle': handle or {},
                        'result': {'message': '生成桥接未启用；请核对原作业，不能重复提交。'}}
            custom = Custom(self.root); key = 'hgen_' + operation_id
            with closing(custom.db()) as db:
                row = db.execute('SELECT * FROM requests WHERE id=? AND owner=?',
                                 (key, context['owner'])).fetchone()
            if row is None:
                if handle is None and 'scene' in args:
                    if self._scene_enabled():
                        scene = args['scene']
                        self._check_personal_scene_objects(user, scene['objects'], scene['contacts'])
                        # The reserved operation fixes the queue key. Custom.submit
                        # verifies that key and the arguments inside its transaction.
                        return generate(context, args, operation_id)
                    return {'state': 'unknown', 'handle': None,
                            'result': {'message': '场景生成暂不可用，将在恢复后核对原作业。'}}
                return {'state': 'unknown', 'handle': handle or {'request_id': key},
                        'result': {'message': '未查到已提交作业；等待人工核对，禁止自动重放。'}}
            handle = {'request_id': key, 'kind': 'generate'}
            status = row['status']
            if 'scene' in args and status in ('ready', 'running', 'failed', 'cancelled'):
                # The collector can commit everything before the queue worker
                # records its progress. Trust the committed output on recovery.
                if len(custom.reconcile_progress(row, only_complete=True)) >= row['n']:
                    status = 'completed'
            if status in ('failed', 'cancelled'):
                return {'state': 'failed', 'handle': handle,
                        'result': {'status': status, 'message': row['message']}}
            if status != 'completed':
                return {'state': 'waiting', 'handle': handle, 'result': {'status': status}}
            result = custom.results(key, user=user)
            candidates = []
            soft_warning = False
            for item in result['items']:
                if 'scene' in args:
                    from g2h.farm_index import read_motion
                    from g2h.motion_assets import REQUIRED
                    with closing(self.legacy.catalog()) as db:
                        record = read_motion(db,self.root,item['motion_id'])
                    summary = record.get('quality',{}).get('human_interaction')
                    if not isinstance(summary,dict):
                        return {'state':'failed','handle':handle,
                                'result':{'message':'场景动作缺少骨架几何验收记录'}}
                    if summary.get('passed') is not True:
                        soft_warning = True
                    fields = REQUIRED | {'scene_spec_json','rest_joints','parents'}
                    source = self.assets.register(context['owner'], {k:record['motion'][k] for k in fields},
                        description=args['text'], request_id='scene-library:'+item['motion_id'],
                        provenance={'source_motion_id':item['motion_id'],
                                    'human_interaction':summary})
                else:
                    source = self._source(user, item['motion_id'])
                candidates.append({'motion_id': source['motion_id'], 'metadata': source})
            if not candidates:
                return {'state': 'unknown', 'handle': handle, 'result': {'message': '作业完成但未取得可用资产。'}}
            res_payload = {'request_id': key}
            if soft_warning:
                res_payload['message'] = '场景候选已生成（存在轻微接触或几何偏差，请预览确认）'
            return {'state': 'succeeded', 'handle': handle, 'result': res_payload,
                    'candidates': candidates}

        def generate(context, args, operation_id):
            from g2h.api_usage import QuotaExceeded

            if not self._generation_enabled():
                return {'state': 'failed', 'result': {'code': 'generation_disabled',
                        'message': '生成桥接尚未启用，或模型尚未完成安装与验证。'}}
            if context['task'].get('selection'):
                raise ValueError('整段生成不能冒充局部修复')
            if 'scene' in args and not self._scene_enabled():
                raise ValueError('当前服务端未启用人体场景交互')
            from review_custom import Custom
            key = 'hgen_' + operation_id
            guard = (self.store.while_task_not_cancelled(context['owner'], context['task']['id'])
                     if 'scene' in args else nullcontext())
            with guard:
                custom = Custom(self.root)
                try:
                    custom.submit({'request_id': key, 'description': args['text'],
                        'prompt_mode': 'exact_english', 'n': 1, 'duration': args['duration'],
                        'original_description': context['task']['goal'],
                        'rewrite_agent': {'name': 'motionfarm-harness'},
                        **({'scene':args['scene']} if 'scene' in args else {})}, context['owner'], source='local_agent')
                except (ValueError, QuotaExceeded) as exc:
                    return {'state': 'failed', 'result': {'code': 'generation_rejected', 'message': str(exc)}}
            return {'state': 'waiting', 'handle': {'kind': 'generate', 'request_id': key},
                    'result': {'message': '已提交动作生成队列；后续先查询该作业。',
                               'model': current_recipe()['model']}}
        registry.register(Tool('generate_motion', '生成一个新动作；仅用于创建任务，需英文提示词及启用现有队列。',
            {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 2000},
                'duration': {'type': 'integer', 'minimum': 1, 'maximum': 120 if current_recipe()['model'] == KIMODO_MODEL else 12},
                'scene': {'type':'object', 'additionalProperties':True,
                          'description':'可选 human_interaction_v1，Y-up 米制定向盒、路径、关键帧和关节接触'}},
             'required': ['text', 'duration'], 'additionalProperties': False}, generate,
            reconcile=reconcile_generation, max_cost_usd=self._generation_cost() or 0.0))
        from harness_retarget import register_retarget_tool
        register_retarget_tool(registry, self, user)
        return registry

    def advance(self, user, task_id):
        self._task(user, task_id)
        driver = self.driver or OpenAICompatibleDriver.from_env()
        identity = getattr(driver, 'identity', 'server:' + driver.__class__.__name__)
        return Harness(self.store, self._registry(user), driver, driver_id=identity).advance(self.owner(user), task_id)

    @staticmethod
    def _encode_decision(action):
        if not isinstance(action, dict):
            raise ValueError('动作决策必须为对象')
        encoded = json.dumps(action, sort_keys=True, separators=(',', ':'), allow_nan=False)
        args = action.get('arguments')
        has_scene = action.get('tool') == 'generate_motion' and isinstance(args,dict) and 'scene' in args
        if len(encoded.encode()) > (80*1024 if has_scene else 32768):
            raise ValueError('外部决策过大')
        return encoded, args if has_scene else None

    def execute(self, user, task_id, action, decision_id, *, recover_unsubmitted=False):
        """A user's existing local agent may supply one bounded decision."""
        self._task(user, task_id)
        if not isinstance(decision_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', decision_id):
            raise ValueError('外部决策需要稳定的 decision_id')
        encoded, scene_args = self._encode_decision(action)
        if scene_args is not None:
            from g2h.human_interaction import check
            duration = scene_args.get('duration')
            if type(duration) is not int or not 1 <= duration <= 120:
                raise ValueError('人体场景时长须为 1–120 秒')
            check(scene_args['scene'],duration*30)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        from review_access import private_file
        owner = self.owner(user)
        def receipt(result, state):
            result['decision_receipt'] = {'id': decision_id, 'state': state,
                                         'action_applied': state == 'recorded', 'replayed': False}
            return result

        with closing(sqlite3.connect(private_file(self.root / 'review/harness_requests.sqlite3'), timeout=3)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS decisions(owner TEXT,task_id TEXT,id TEXT,digest TEXT,state TEXT,PRIMARY KEY(owner,task_id,id))')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,state FROM decisions WHERE owner=? AND task_id=? AND id=?',
                             (owner, task_id, decision_id)).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ValueError('该决策编号已用于其他参数')
                if row[1] != 'deferred':
                    result = self.detail(user, task_id)
                    # The operation ledger is written before the queue can be mutated.
                    # A resumed scene can try to acquire the original run lease only
                    # while there is no durable operation to reconcile.
                    if not (recover_unsubmitted and row[1] == 'uncertain'
                            and result['status'] == 'ready' and not result['operations']):
                        return receipt(result, row[1])
                db.execute('UPDATE decisions SET state=? WHERE owner=? AND task_id=? AND id=?',
                           ('uncertain', owner, task_id, decision_id))
            else:
                db.execute('INSERT INTO decisions VALUES(?,?,?,?,?)', (owner, task_id, decision_id, digest, 'uncertain'))
        class ExternalDecision:
            identity = 'external_agent'
            cost_upper_bound_usd = 0.0
            called = False
            def next_action(self, context, tools):
                self.called = True
                return action
        driver = ExternalDecision()
        try:
            result = Harness(self.store, self._registry(user), driver, driver_id='external-local').advance(
                owner, task_id, before_first_operation=recover_unsubmitted)
        except Exception as error:
            if recover_unsubmitted and isinstance(error, RunBusy):
                return receipt(self.detail(user, task_id), 'uncertain')
            if not driver.called:
                with closing(sqlite3.connect(self.root / 'review/harness_requests.sqlite3', timeout=3)) as db, db:
                    db.execute("UPDATE decisions SET state='deferred' WHERE owner=? AND task_id=? AND id=? AND state!='recorded'",
                               (owner, task_id, decision_id))
            raise
        admitted = driver.called or (recover_unsubmitted and bool(result['operations']))
        state = 'recorded' if admitted else 'deferred'
        with closing(sqlite3.connect(self.root / 'review/harness_requests.sqlite3', timeout=3)) as db, db:
            db.execute("UPDATE decisions SET state=? WHERE owner=? AND task_id=? AND id=? AND state!='recorded'",
                       (state, owner, task_id, decision_id))
        return receipt(result, state)

    def repair(self, user, task_id, value):
        task = self._task(user, task_id)
        if not self._base(task) or not task.get('selection'):
            raise ValueError('请先保存包含原动作和选区的任务')
        args = {k:v for k,v in value.items() if k != 'request_id'}
        return self.execute(user, task_id, {'kind':'tool','tool':'edit_motion','arguments':args}, value['request_id'])

    def _check_personal_scene_objects(self, user, objects, contacts=()):
        assets = [obj for obj in objects if isinstance(obj, dict) and 'asset_version' in obj]
        if not assets:
            return

        def legacy_coordinate(coordinate):
            # Older clients used Math.round on six-decimal contact coordinates.
            scaled = coordinate * 1_000_000
            lower = math.floor(scaled)
            return (lower + (scaled - lower >= .5)) / 1_000_000

        from personal_assets import Library
        library = Library(self, user)
        for obj in assets:
            data = library.asset(obj['asset_version'], 'object')['data']
            if obj.get('size_m') != data['size_m'] or obj.get('heading_deg') != data['heading_deg']:
                raise ValueError('场景几何与交互物体版本不一致')
            anchors = [a['position_m'] for a in data['anchors']]
            rounded = [[legacy_coordinate(coordinate) for coordinate in anchor] for anchor in anchors]
            allowed_anchors = anchors + rounded
            if any(c['object_id'] == obj['id'] and c['anchor_m'] not in allowed_anchors for c in contacts):
                raise ValueError('场景接触点与交互物体版本不一致')

    def _prepare_scene(self, user, value):
        from g2h.human_interaction import check
        from MotionFarm import english_surface
        text, duration = value['text'], value['duration']
        if (not isinstance(text,str) or not text.startswith('A person ') or len(text)>2000
                or not 8<=len(text.split())<=120 or not english_surface(text)):
            raise ValueError('英文原文需以 A person 开头，8–120 词')
        if type(duration) is not int or not 1<=duration<=120:
            raise ValueError('人体场景时长须为 1–120 秒')
        check(value['scene'],duration*30)
        self._check_personal_scene_objects(user, value['scene']['objects'], value['scene']['contacts'])
        args = {'text':text,'duration':duration,'scene':value['scene']}
        action = {'kind':'tool','tool':'generate_motion','arguments':args}
        self._encode_decision(action)
        return action

    def _submit_scene(self, user, request_id, action):
        task = self.create(user, {'request_id':request_id, 'goal':action['arguments']['text'],
                                 'budget':SCENE_BUDGET})
        return self.execute(user,task['id'],action,request_id)

    def scene(self, user, value):
        if not self._scene_enabled():
            raise ValueError('当前服务端未启用人体场景交互')
        return self._submit_scene(user, value['request_id'], self._prepare_scene(user, value))

    def multi_scene(self, user, value):
        from g2h.multi_actor import build_multi_plans
        if not self._scene_enabled():
            raise ValueError('当前服务端未启用人体场景交互')
        if not isinstance(value, dict):
            raise ValueError('多角色场景参数必须为对象')
        request_id = value.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('无效场景请求编号')
        duration = value.get('duration')
        if type(duration) is not int or not 1 <= duration <= 120:
            raise ValueError('多角色场景时长须为 1–120 秒')
        actors = value.get('actors')
        if not isinstance(actors, list) or not 1 <= len(actors) <= 4 or not all(isinstance(a, dict) for a in actors):
            raise ValueError('多角色场景需要 1–4 个角色')
        objects_meta = [] if value.get('objects') is None else value.get('objects')
        if not isinstance(objects_meta, list):
            raise ValueError('场景物体列表格式无效')
        interactions_meta = [] if value.get('interactions') is None else value.get('interactions')
        if not isinstance(interactions_meta, list):
            raise ValueError('角色交互列表格式无效')
        self._check_personal_scene_objects(user, objects_meta)
        plans = build_multi_plans(value)
        if len(plans) != len(actors):
            raise ValueError('多角色规划数量不匹配')
        actions = []
        for idx, plan in enumerate(plans):
            try:
                actions.append(self._prepare_scene(user, plan))
            except ValueError as error:
                raise ValueError(f'角色 {idx + 1}：{error}') from error
        actor_requests = [(hashlib.sha256(f'multi:{request_id}:{idx}'.encode()).hexdigest(), action['arguments'])
                          for idx, action in enumerate(actions)]
        meta = {
            'actors': [{'label': a.get('label'), 'robot': a.get('robot'), 'id': a.get('id')} for a in actors],
            'interactions': interactions_meta,
            'objects': objects_meta,
        }
        owner = self.owner(user)
        tasks = self.store.admit_scene_group(owner, actor_requests, meta, budget=SCENE_BUDGET)
        group, applied = self._dispatch_scene_group(user, tasks, actor_requests)
        if not applied and group['status'] != 'cancelled':
            group['decision_receipt'] = {'id': request_id, 'action_applied': False}
        return group

    def _dispatch_scene_group(self, user, tasks, actor_requests, *, only_pending=False):
        owner = self.owner(user)
        applied = True
        for task_id, (key, arguments) in zip(tasks, actor_requests):
            if self.store.task_status(owner, tasks[0]) == 'cancelled':
                break
            if only_pending:
                task = self._task(user, task_id)
                if task['status'] != 'ready' or task['operations']:
                    continue
                scene = arguments['scene']
                self._check_personal_scene_objects(user, scene['objects'], scene['contacts'])
            action = {'kind': 'tool', 'tool': 'generate_motion', 'arguments': arguments}
            try:
                if only_pending:
                    result = self.execute(user, task_id, action, key, recover_unsubmitted=True)
                else:
                    result = self.execute(user, task_id, action, key)
            except Conflict:
                # Group cancellation fences the actor lease after its queue write.
                if self.store.task_status(owner, tasks[0]) != 'cancelled':
                    raise
                break
            if result.get('decision_receipt', {}).get('action_applied') is False:
                applied = False
        return self.scene_status(user, tasks[0]), applied

    def resume_multi_scene(self, user, task_id):
        self._task(user, task_id)
        group = self.store.scene_group(self.owner(user), task_id, include_submission=True)
        if not group or not group['has_submission']:
            raise ValueError('此场景未保留续交参数，请从原场景重试')
        if not self._scene_enabled():
            raise ValueError('当前服务端未启用人体场景交互')
        result, _ = self._dispatch_scene_group(user, group['tasks'], group['actor_requests'], only_pending=True)
        return result

    def paired_scene(self, user, value):
        import hashlib
        from g2h.paired_scene import interaction_plans
        if not self._scene_enabled():
            raise ValueError('当前服务端未启用人体场景交互')
        kind, duration = value['kind'], value['duration']
        if type(duration) is not int or not 3 <= duration <= 120:
            raise ValueError('双人动作时长须为 3–120 秒')
        x, z = value.get('x', 0.), value.get('z', 0.)
        if any(type(v) not in (int,float) or not abs(v) <= 97 for v in (x,z)):
            raise ValueError('无效双人交互位置')
        plans = interaction_plans(kind,duration*30,x,z)
        text = ('A person faces a partner and extends the right arm to shake hands gently with the partner.'
                if kind == 'handshake' else 'A person holds a shared box with both hands and steps sideways while carrying the box with a partner.')
        tasks = []
        for actor, plan in enumerate(plans):
            key = hashlib.sha256(f"pair:{value['request_id']}:{actor}".encode()).hexdigest()
            tasks.append(self.scene(user, dict(text=text, duration=duration, scene=plan, request_id=key)))
        self.store.link_scene_pair(self.owner(user), tasks[0]['id'], tasks[1]['id'], kind)
        return self.scene_status(user, tasks[0]['id'])

    def scene_status(self, user, task_id):
        first = self._scene_actor_status(user, task_id)
        owner = self.owner(user)
        pair = self.store.scene_partner(owner, task_id)
        if pair:
            second = self._scene_actor_status(user, pair['second_task'])
            first['pair'] = dict(kind=pair['kind'], task_id=second['id'], status=second['status'],
                                 candidates=second['candidates'], operations=second['operations'])
        grp = self.store.scene_group(owner, task_id)
        if grp:
            actors_meta = grp['meta'].get('actors') or []
            group_items = []
            for i, tid in enumerate(grp['tasks']):
                t = first if tid == task_id else self._scene_actor_status(user, tid)
                meta_i = actors_meta[i] if i < len(actors_meta) and isinstance(actors_meta[i], dict) else {}
                group_items.append({
                    'actor_index': i,
                    'task_id': t['id'],
                    'label': meta_i.get('label'),
                    'robot': meta_i.get('robot'),
                    'status': t['status'],
                    'candidates': t['candidates'],
                    'operations': t['operations'],
                })
            first['actors_group'] = group_items
            if (grp['has_submission'] and first['status'] != 'cancelled'
                    and any(actor['status'] == 'ready' and not actor['operations'] for actor in group_items)):
                first['scene_resume_available'] = True
        return first

    def multi_preview(self, user, task_id):
        from g2h.multi_actor import multi_actor_report
        grp = self.store.scene_group(self.owner(user), task_id)
        if not grp:
            raise ValueError('多角色场景不存在')
        clips, plans = [], []
        for tid in grp['tasks']:
            task = self._task(user, tid)
            op = next((o for o in task['operations'] if o['tool'] == 'generate_motion'), None)
            candidate = next((c for c in task['candidates'] if op and c['operation_id'] == op['id']
                              and c['status'] != 'quarantined'), None)
            if not candidate:
                raise ValueError('多角色场景尚无完整候选')
            clips.append(self.preview(user, candidate['motion_id']))
            plans.append(op['arguments']['scene'])
        quality = multi_actor_report(plans, [c['joints'] for c in clips])
        quality['passed'] = bool(quality['passed']) and all(
            bool((c.get('scene') or {}).get('quality', {}).get('passed', True)) for c in clips
        )
        return {'actors': clips, 'quality': quality, 'meta': grp['meta']}

    def object_response(self, user, value):
        from g2h.harness.contracts import digest
        from g2h.human_interaction import decode
        from g2h.human_rig import motion_rig
        from g2h.object_response import attach_object_to_motion
        owner = self.owner(user)
        if not isinstance(value, dict):
            raise ValueError('物体响应参数必须为对象')
        allowed = {'request_id', 'source_motion_id', 'objects', 'attachment'}
        if set(value) - allowed or not allowed <= set(value):
            raise ValueError('物体响应请求字段无效')
        request_id = value['request_id']
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('无效请求编号')
        source_motion_id = value['source_motion_id']
        if not isinstance(source_motion_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source_motion_id):
            raise ValueError('无效动作编号')
        objects = value['objects']
        if not isinstance(objects, list) or not 1 <= len(objects) <= 16 or not all(isinstance(o, dict) for o in objects):
            raise ValueError('场景物体列表须包含 1–16 个对象')
        attachment = value['attachment']
        if not isinstance(attachment, dict):
            raise ValueError('物体绑定参数必须为对象')

        self._check_personal_scene_objects(user, objects)
        source = self._source(user, source_motion_id)
        asset_request = 'objresp:' + request_id
        geometry_digest = digest(objects)
        asset = self.assets.find_request(owner, asset_request)
        provenance = (asset.get('provenance') or {}) if asset else {}
        recorded_geometry = provenance.get('object_response_geometry_sha256')
        if asset:
            if (provenance.get('source_motion_id') != source['motion_id']
                    or provenance.get('object_response') != attachment
                    or (recorded_geometry is not None and recorded_geometry != geometry_digest)):
                raise ValueError('该请求编号已用于其他物体响应参数')
            if recorded_geometry is not None:
                self.assets.artifact_path(owner, asset['motion_id'])
        if asset is None or recorded_geometry is None:
            arrays, _ = self.assets.load(owner, source['motion_id'])
            rest, parents = motion_rig(arrays)
            updated, scene, report = attach_object_to_motion(arrays, objects, attachment, rest, parents)
            if asset:
                # Older assets stored the calculated scene, not the authored geometry.
                saved, _ = self.assets.load(owner, asset['motion_id'])
                if 'scene_spec_json' not in saved or decode(saved['scene_spec_json'], len(saved['native_transl'])) != scene:
                    raise ValueError('该请求编号已用于其他物体响应参数')
            else:
                asset = self.assets.register(
                    owner, updated,
                    description=f'物体响应 {source["motion_id"]}',
                    request_id=asset_request,
                    provenance={
                        'source_motion_id': source['motion_id'],
                        'human_interaction': report,
                        'object_response': attachment,
                        'object_response_geometry_sha256': geometry_digest,
                    },
                )
        task = self.store.create_task(
            owner, request_id, f'物体响应 {source["motion_id"]}',
            source_motion_id=source['motion_id'],
            budget={'max_attempts': 1, 'max_candidates': 1, 'max_cost_usd': 0},
        )
        if not task['candidates']:
            self.store.register_candidate(owner, task['id'], asset['motion_id'], metadata=asset)
            task = self.detail(user, task['id'])
        return task

    def paired_preview(self, user, task_id):
        from g2h.paired_scene import contact_report
        pair = self.store.scene_partner(self.owner(user),task_id)
        if not pair:
            raise ValueError('双人场景不存在')
        clips, plans = [], []
        for tid in (task_id,pair['second_task']):
            task = self._task(user,tid)
            op = next((o for o in task['operations'] if o['tool']=='generate_motion'),None)
            candidate = next((c for c in task['candidates'] if op and c['operation_id']==op['id']
                              and c['status']!='quarantined'),None)
            if not candidate:
                raise ValueError('双人场景尚无完整候选')
            clips.append(self.preview(user,candidate['motion_id']))
            plans.append(op['arguments']['scene'])
        quality = contact_report(pair['kind'],plans,[clip['joints'] for clip in clips])
        quality['passed'] &= all(c['scene']['quality']['passed'] for c in clips)
        return dict(actors=clips,quality=quality,kind=pair['kind'])

    def _scene_actor_status(self, user, task_id):
        task = self._task(user, task_id)
        if task['status'] == 'cancelled':
            return task
        pending = [op for op in task['operations'] if op['status'] in ('submitting','waiting','unknown')]
        if pending and pending[0]['tool']=='generate_motion' and 'scene' in pending[0]['arguments']:
            owner = self.owner(user)
            try:
                Harness(self.store,self._registry(user),None,driver_id='external-local').advance(
                    owner,task_id,reconcile_only=True)
            except Conflict:
                # Cancellation or another active reconciler owns the current state.
                pass
        return self.detail(user,task_id)

    def splice_preview(self,user,value):
        from g2h import cubic_splice, motion_splice
        value=motion_splice.validate_request(value);parts=[]
        n=value.get('transition_frames')
        cubic_splice.duration(n)
        for clip in value['clips']:
            source=self._source(user,clip['motion_id']);arrays,_=self.assets.load(self.owner(user),source['motion_id']);t=clip['transform']
            parts.append(cubic_splice.transform(arrays,clip['start_frame'],clip['end_frame'],t['turn_right_deg'],t['offset_x_m'],t['offset_z_m']))
        parts[1], rest, parents = cubic_splice.align_rest_origins(*parts)
        gap=motion_splice.gap(*parts);warnings=[]
        try:cubic_splice.check(*parts,n)
        except ValueError as exc:warnings.append(str(exc))
        views, contact = [], []
        feet = [10, 11]
        for part,frame in zip(parts,(-1,0)):
            points = part['native_keypoints3d']
            offset = rest[0] + part['native_transl'] - points[:, 0]
            pose = points[frame] + offset[frame]
            floor = float((points[:, feet, 1] + offset[:, 1, None]).min())
            views.append(pose.tolist())
            contact.append([joint for joint in feet if pose[joint, 1] < floor + .05])
        if contact[0]!=contact[1]:warnings.append('两段边界的低位脚不同，请核对支撑脚并预览生成后的过渡')
        return dict(poses=views,parents=parents.tolist(),gap=gap,contact_proxy=contact,warnings=warnings,preview_only=True,scope='边界姿态叠加预览；接触仅由高度估计，过渡尚未生成')

    def splice(self, user, value):
        """Save a deterministic transition as a candidate in the existing task API."""
        from g2h import cubic_splice, motion_splice
        owner = self.owner(user)
        value = motion_splice.validate_request(value)
        request_id = value['request_id']
        if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('无效拼接请求编号')
        frames = value['transition_frames']
        cubic_splice.duration(frames)
        clips, parts, sources = value['clips'], [], []
        for clip in clips:
            source = self._source(user, clip['motion_id'])
            arrays, _ = self.assets.load(owner, source['motion_id'])
            t = clip['transform']
            parts.append(cubic_splice.transform(arrays, clip['start_frame'], clip['end_frame'],
                                                 t['turn_right_deg'], t['offset_x_m'], t['offset_z_m']))
            sources.append({'motion_id': source['motion_id'], 'requested_id': clip['motion_id'],
                            'sha256': source['sha256'], 'start_frame': clip['start_frame'],
                            'end_frame': clip['end_frame'], 'transform': t})
        asset_request = 'splice-cubic:' + request_id
        asset = self.assets.find_request(owner, asset_request)
        old = asset or self.assets.find_request(owner, 'splice-join:' + request_id)
        if old:
            provenance = old['provenance']
            if provenance.get('sources') != sources or provenance.get('transition_frames') != frames:
                raise ValueError('该请求编号已用于其他拼接')
        if old and asset is None:
            task = self.store.create_task(owner, request_id, old['description'], source_motion_id=old['motion_id'],
                selection=provenance['join']['selection'], requirements=['收势站立 → 自然行走 → 减速站稳 → 接入后段'],
                budget={'max_attempts': 1, 'max_cost_usd': 0})
            if not task['operations'] and task['status'] not in ('accepted', 'cancelled', 'failed'):
                raise ValueError('旧拼接未完成，请新建拼接请求')
            return task
        if asset is None:
            joined, report = cubic_splice.join(*parts, frames)
            goal = '拼接 %s + %s（Cubic Hermite 过渡 %.2f 秒）' % (
                clips[0]['motion_id'], clips[1]['motion_id'], frames / 30)
            asset = self.assets.register(owner, joined, description=goal, request_id=asset_request,
                provenance={'tool': 'splice_motions', 'version': cubic_splice.VERSION, 'sources': sources,
                            'transition_frames': frames, 'join': report,
                            'semantic_verified': False, 'dynamics_verified': False})
        report = asset['provenance']['join']
        task = self.store.create_task(owner, request_id, asset['description'], source_motion_id=asset['motion_id'],
            selection=report['selection'], budget={'max_attempts': 1, 'max_candidates': 1, 'max_cost_usd': 0})
        if not task['candidates']:
            self.store.register_candidate(owner, task['id'], asset['motion_id'],
                metadata=asset)
            task = self._task(user, task['id'])
        return self._with_splice_details(task)

    def repair_status(self, user, task_id):
        task = self._task(user, task_id)
        pending = [o for o in task['operations'] if o['status'] in ('submitting','waiting','unknown')]
        if not pending or pending[0]['tool'] != 'edit_motion':
            return self._with_splice_details(task)
        class NoDecision:
            identity = 'external_agent'
            cost_upper_bound_usd = 0.0
            def next_action(self, *args):
                raise RuntimeError('Reconciliation must not make a new decision')
        Harness(self.store, self._registry(user), NoDecision(), driver_id='external-local').advance(
            self.owner(user), task_id, reconcile_only=True)
        return self.detail(user, task_id)

    def preview(self, user, motion_id):
        import numpy as np
        from review_preview import world_coordinates
        from review_access import Forbidden
        owner = self.owner(user)
        try:
            source = self._source(user, motion_id)
        except KeyError as exc:
            if not motion_id.startswith('ma_'):
                raise
            raise Forbidden('动作不存在或当前不可访问') from exc
        try:
            with saved_motion_read() if motion_id.startswith('ma_') else nullcontext():
                arrays, meta = self.assets.load(owner, source['motion_id'])
        except ValueError as exc:
            raise OSError('无法读取已保存的动作文件') from exc
        if 'scene_spec_json' in arrays or 'rest_joints' in arrays:
            from g2h.human_rig import definition, motion_rig
            from g2h.human_interaction import decode, preview
            rest, parents = motion_rig(arrays)
            _, _, names = definition()
            world, backend = world_coordinates(arrays['native_keypoints3d'],arrays['native_transl'],rest[0])
            scene = (preview(arrays,decode(arrays['scene_spec_json'],len(world)),rest,parents)
                     if 'scene_spec_json' in arrays else None)
            return {'motion_id':meta['motion_id'],'joints':world.tolist(),'parents':parents.tolist(),
                    'names':names,'fps':30,'frames':len(world),'duration_s':len(world)/30,
                    'coordinates':'Y up, metres','preview_backend':backend,'scene':scene}
        rig = self.root / 'third_party/HY-Motion-1.0/retargeting/smplh'
        # Skeleton data is a separate installed asset, never fabricated.
        if self.legacy is not None:
            cache = getattr(self.legacy, 'preview_cache', None)
            if cache is not None:
                rig = cache.assets
        parents = np.fromfile(rig / 'kintree.bin', dtype=np.int32)
        template = np.fromfile(rig / 'j_template.bin', dtype=np.float32).reshape(-1, 3)
        names = json.loads((rig / 'joint_names.json').read_text())
        if len(parents) != 52 or template.shape != (52, 3) or len(names) != 52:
            raise ValueError('缺少兼容的预览骨架')
        world, backend = world_coordinates(arrays['native_keypoints3d'], arrays['native_transl'], template[0])
        fps = float(arrays['fps'])
        return {'motion_id': meta['motion_id'], 'joints': world.tolist(), 'parents': parents.tolist(),
                'names': names, 'fps': fps, 'frames': len(world), 'duration_s': len(world) / fps,
                'coordinates': 'Y up, metres', 'preview_backend': backend, 'quality': {'semantic': 'unknown'}}

    def import_patch(self, user, task_id, value):
        """Bounded numeric upload, then splice under the server's pinned range."""
        owner = self.owner(user); task = self._task(user, task_id)
        if not isinstance(value, dict) or set(value) != {'request_id', 'npz_base64'}:
            raise ValueError('候选上传需要 request_id 和数值 NPZ')
        request_id = value['request_id']
        if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('候选上传需要稳定的请求编号')
        patch_request = 'patch:' + task_id + ':' + request_id
        splice_request = 'splice:' + task_id + ':' + request_id
        existing_patch = self.assets.find_request(owner, patch_request)
        if (len(task['candidates']) >= task['budget']['max_candidates']
                and existing_patch is None):
            raise ValueError('候选预算已用完')
        text = value['npz_base64']
        if not isinstance(text, str) or len(text) > 6 * 1024 * 1024:
            raise ValueError('候选片段过大')
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            raise ValueError('无效候选编码') from None
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('候选片段最大 4 MiB')
        if not task.get('selection') or not self._base(task):
            raise ValueError('该任务没有可编辑片段')
        if task['status'] in ('cancelled', 'accepted', 'failed'):
            existing_result = self.assets.find_request(owner, splice_request)
            if (existing_patch is None or existing_result is None or not any(
                    c['motion_id'] == existing_result['motion_id'] for c in task['candidates'])):
                raise ValueError('任务已结束；请以需要的版本新建任务')
            from g2h.motion_assets import load_numeric_npz
            with tempfile.TemporaryDirectory(prefix='motionfarm-patch-check-') as folder:
                path = Path(folder) / 'patch.npz'; path.write_bytes(raw)
                uploaded = load_numeric_npz(path)
            original, _ = self.assets.load(owner, existing_patch['motion_id'])
            if set(uploaded) != set(original) or any(
                    uploaded[k].dtype != original[k].dtype or uploaded[k].shape != original[k].shape
                    or uploaded[k].tobytes() != original[k].tobytes() for k in original):
                raise ValueError('该请求编号已用于其他片段')
            return task
        # Numeric validation and immutable storage belong to the asset layer.
        with tempfile.TemporaryDirectory(prefix='motionfarm-patch-') as folder:
            path = Path(folder) / 'patch.npz'; path.write_bytes(raw)
            patch = self.assets.import_npz(owner, path, description='用户提供的候选片段',
                                          request_id=patch_request)
        selection = task['selection']; parent = self.assets.get(owner, self._base(task))
        arrays, _ = self.assets.load(owner, patch['motion_id'])
        result = self.assets.apply_patch(owner, parent['motion_id'], selection['start_frame'],
            selection['end_frame'], arrays, expected_sha256=parent['sha256'],
            description=task['goal'], request_id=splice_request)
        self.store.register_candidate(owner, task_id, result['motion_id'], metadata=result,
                                      parent_id=parent['motion_id'])
        return self.detail(user, task_id)
