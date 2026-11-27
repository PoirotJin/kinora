"""Owner-checked adapter from motion task tools to the existing retarget queue.

This module only submits and reconciles durable jobs. It never starts a worker.
A robot result is an artifact reference, never a new human source candidate.
"""
import hashlib
import io
import json
import os
import re
import zipfile

from g2h.harness import Tool


def _job_id(owner, operation_id):
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError('Retarget operation requires a stable ID')
    payload = json.dumps(['harness-retarget-job-v1', owner, operation_id], separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _import_id(owner, source_sha256):
    if not isinstance(source_sha256, str) or not re.fullmatch('[0-9a-f]{64}', source_sha256):
        raise ValueError('Retarget import requires an immutable source hash')
    payload = json.dumps(['harness-retarget-import-v1', owner, source_sha256], separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _authorized_task(context, user, service, args):
    owner = service.owner(user)
    if context.get('owner') != owner:
        raise PermissionError('Retarget owner does not match the authenticated account')
    robot = args.get('robot')
    if not isinstance(robot, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', robot):
        raise ValueError('Retarget requires an explicit robot identifier')
    if not isinstance(args.get('motion_id'), str) or not args['motion_id']:
        raise ValueError('Retarget requires an explicit motion identifier')
    force_new = args.get('force_new', False)
    if type(force_new) is not bool:
        raise ValueError('force_new must be boolean')
    grants = [item.get('check') for item in context.get('task', {}).get('requirements', [])
              if isinstance(item, dict) and isinstance(item.get('check'), dict)]
    matching = [grant for grant in grants if grant.get('type') == 'retarget' and grant.get('robot') == robot]
    if not matching:
        raise PermissionError('Task has no explicit user-authorized retarget requirement for this robot')
    if force_new and not any(grant.get('force_new') is True for grant in matching):
        raise PermissionError('Task does not authorize new retarget computation')
    return owner


def _numeric_export(arrays):
    """Write byte-stable four-field NPZ accepted by the existing import API."""
    import numpy as np
    from gadget_imports import FIELDS, MAX_BYTES, validate_npz
    selected = {key: arrays[key] for key in FIELDS}
    if sum(value.nbytes for value in selected.values()) > MAX_BYTES:
        raise ValueError('Motion exceeds the existing numeric import size limit')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for key in sorted(selected):
            buffer = io.BytesIO()
            np.lib.format.write_array(buffer, np.asarray(selected[key]), allow_pickle=False)
            entry = zipfile.ZipInfo(key + '.npy', date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, buffer.getvalue())
    raw = output.getvalue()
    validate_npz(raw)
    return raw


def _execution(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def register_retarget_tool(registry, service, user):
    """Register ``retarget_motion``; no model, robot or worker is loaded here.

    A task requirement must contain ``check={'type': 'retarget', 'robot': id}``.
    Setting force_new additionally requires force_new=True in that user-created
    requirement. The model driver can read these grants but cannot add them.
    """
    def settle(owner, args, operation_id, queue, prior_handle=None):
        job_id = _job_id(owner, operation_id)
        handle = {'kind': 'retarget', 'job_id': job_id}
        if prior_handle:
            if prior_handle.get('job_id') != job_id:
                raise PermissionError('Retarget handle belongs to a different operation')
            if prior_handle.get('import_motion_id'):
                handle['import_motion_id'] = prior_handle['import_motion_id']
        try:
            row = queue.get(job_id, user)
        except (KeyError, ValueError):
            return {'state': 'unknown', 'handle': handle,
                    'result': {'code': 'retarget_submission_unknown',
                               'message': 'No matching durable job was found; do not resubmit this operation automatically'}}
        if (row.get('owner') != owner or row.get('robot') != args['robot'] or row.get('id') != job_id
                or not re.fullmatch('mimp[0-9a-f]{32}', row.get('motion_id', ''))
                or ('import_motion_id' in handle and handle['import_motion_id'] != row.get('motion_id'))):
            raise PermissionError('Retarget job identity or ownership does not match the operation')
        handle['import_motion_id'] = row['motion_id']
        status = row.get('status')
        actual = {'job_id': job_id, 'robot': row['robot'], 'status': status,
                  'execution': _execution(row.get('execution')),
                  'requested_execution': _execution(row.get('requested_execution')),
                  'reused_from': row.get('reused_from') or None,
                  'result_version': row.get('result_version')}
        if status == 'done':
            actual['artifacts'] = [{'kind': 'retarget', 'job_id': job_id,
                                    'robot': row['robot'], 'name': 'result.npz',
                                    'result_version': row.get('result_version')}]
            return {'state': 'succeeded', 'handle': handle, 'result': actual,
                    'evidence': [{'kind': 'robot_dynamics', 'status': 'unknown',
                                  'body': {'job_id': job_id, 'robot': row['robot'],
                                           'reason': 'Retarget completion does not establish dynamic feasibility or real-robot safety'}}]}
        if status in ('failed', 'cancelled'):
            return {'state': 'failed', 'handle': handle, 'result': actual}
        return {'state': 'waiting' if status in ('queued', 'running', 'stopping') else 'unknown',
                'handle': handle, 'result': actual}

    def execute(context, args, operation_id):
        from review_access import Forbidden
        try:
            owner = _authorized_task(context, user, service, args)
            if service.legacy is None or os.environ.get('MOTIONFARM_HARNESS_ENABLE_RETARGET') != '1':
                return {'state': 'failed', 'result': {'code': 'retarget_disabled',
                                                    'message': 'Existing retarget queue bridge is not enabled'}}
            # Known rejection here has not submitted an import or a robot job.
            from retarget_robots import robot_spec
            from retarget_parameters import validate
            robot_spec(args['robot'])
            if 'parameters' in args:
                validate(args['parameters'])
            source = service._source(user, args['motion_id'])
            from review_retarget import Retarget, require_retargetable_source
            require_retargetable_source(service.root, owner, source['motion_id'])
            arrays, metadata = service.assets.load(owner, source['motion_id'])
            raw = _numeric_export(arrays)
            import_id, job_id = _import_id(owner, metadata['sha256']), _job_id(owner, operation_id)
            from gadget_imports import Imports
            imports = Imports(service.root)
            queue = Retarget(service.root)
            text = 'MotionFarm immutable source ' + metadata['sha256']
        except (PermissionError, Forbidden, ValueError, KeyError, OSError, ImportError) as error:
            return {'state': 'failed', 'result': {'code': 'retarget_preflight_rejected',
                                                'error': type(error).__name__,
                                                'message': str(error)[:512]}}
        # From this call onward a durable side effect may have committed before
        # its acknowledgement is lost. Exceptions must escape to the harness's
        # unknown/reconcile path; they must never be relabeled as a safe refusal.
        imported = imports.save(owner, import_id, text, raw)
        if imported.get('motion_id') != 'mimp' + import_id:
            raise ValueError('Import bridge returned an unexpected source identifier')
        class ImportedStore:
            def detail(self, motion_id):
                imports.get(motion_id, owner)
                return {'location': {'uploaded': True}}
        value = {'id': job_id, 'motion_id': imported['motion_id'], 'robot': args['robot'],
                 'force_new': args.get('force_new', False)}
        if 'parameters' in args:
            value['parameters'] = args['parameters']
        queue.submit(value, user, ImportedStore())
        return settle(owner, args, operation_id, queue, {'job_id': job_id, 'import_motion_id': 'mimp' + import_id})

    def reconcile(context, args, operation_id, handle):
        owner = _authorized_task(context, user, service, args)
        if service.legacy is None:
            return {'state': 'unknown', 'handle': handle,
                    'result': {'message': 'Legacy queue is unavailable; original operation has not been resubmitted'}}
        # Read an accepted operation even after the submission feature is disabled.
        from review_retarget import Retarget
        return settle(owner, args, operation_id, Retarget(service.root), handle)

    fields = {
        'smooth_weight': {'type': 'number', 'minimum': 0, 'maximum': 10},
        'laplacian_weight': {'type': 'number', 'minimum': 0.1, 'maximum': 100},
        'root_orientation_weight': {'type': 'number', 'minimum': 1, 'maximum': 200},
        'limb_direction_weight': {'type': 'number', 'minimum': 0.1, 'maximum': 20},
    }
    tool = Tool('retarget_motion',
                'Retarget an owned motion only when a user requirement explicitly authorizes this robot. '
                'Default force_new=false preserves existing reuse behavior and actual cached parameters; '
                'new computation requires a separate user grant. Robot output is not a human source candidate.',
                {'type': 'object', 'properties': {
                    'motion_id': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                    'robot': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                    'parameters': {'type': 'object', 'properties': fields, 'additionalProperties': False},
                    'force_new': {'type': 'boolean', 'default': False}},
                 'required': ['motion_id', 'robot'], 'additionalProperties': False},
                execute, reconcile=reconcile)
    registry.register(tool)
    return tool
