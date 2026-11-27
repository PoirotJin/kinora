"""Versioned generation recipes. Old outboxes and queued specs keep their backend."""
import math
import json
import hashlib
import os
from pathlib import Path

HY_MODEL = 'HY-Motion-1.0'
KIMODO_MODEL = 'Kimodo-SOMA-RP-v1.2'
KIMODO_ADAPTER = 'soma77-smplh52-tpose-fk-v1'
KIMODO_MAX_SECONDS = 120
REPRESENTATION = 'human_smplh_y_up_v1'
EDIT_VALIDATION_FILES = ('g2h/kimodo_edit.py', 'g2h/kimodo_bridge.py', 'g2h/kimodo_gait.py', 'scripts/collection_kimodo.py')


def current_recipe():
    model = os.environ.get('MOTIONFARM_GENERATOR_MODEL', HY_MODEL)
    if model == HY_MODEL:
        return {'model': model, 'cfg_scale': 5.0}
    if model == KIMODO_MODEL:
        return {'model': model, 'cfg_scale': 2.0, 'constraint_cfg_scale': 2.0,
                'diffusion_steps': 100, 'post_processing': True,
                'adapter_version': KIMODO_ADAPTER}
    raise ValueError('Unsupported generation model: ' + model)


def manifest_recipe(data):
    # A pre-switch immutable outbox MUST NOT acquire today's model on replay.
    recipe = data.get('generation', {'model': HY_MODEL, 'cfg_scale': 5.0})
    if not isinstance(recipe, dict):
        raise ValueError('Invalid generation recipe')
    model = recipe.get('model')
    if model not in (HY_MODEL, KIMODO_MODEL):
        raise ValueError('Unsupported generation model')
    if model == HY_MODEL:
        if set(recipe) != {'model', 'cfg_scale'} or recipe['cfg_scale'] != 5.0:
            raise ValueError('Invalid legacy generation recipe')
    elif recipe != {'model': KIMODO_MODEL, 'cfg_scale': 2.0, 'constraint_cfg_scale': 2.0,
                    'diffusion_steps': 100, 'post_processing': True,
                    'adapter_version': KIMODO_ADAPTER}:
        raise ValueError('Unsupported Kimodo generation recipe version')
    return dict(recipe)


def validate_spec(spec):
    model = spec.get('model')
    if model not in (HY_MODEL, KIMODO_MODEL):
        raise ValueError('Unsupported generation model')
    duration = spec.get('duration_s')
    limit = KIMODO_MAX_SECONDS if model == KIMODO_MODEL else 12
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= limit:
        raise ValueError('Generation duration must be 1..%d seconds for %s' % (limit, model))
    if not isinstance(spec.get('text'), str) or not 0 < len(spec['text']) <= 4000:
        raise ValueError('Invalid generation text')
    for key in ('seed', 'inference_seed'):
        value = spec.get(key, spec.get('seed'))
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError('Invalid generation seed')
    cfg = spec.get('cfg_scale')
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 1 <= cfg <= 20:
        raise ValueError('Invalid guidance scale')
    if model == KIMODO_MODEL:
        expected = {k: v for k, v in manifest_recipe({'generation': {
            'model': KIMODO_MODEL, 'cfg_scale': 2.0, 'constraint_cfg_scale': 2.0,
            'diffusion_steps': 100, 'post_processing': True,
            'adapter_version': KIMODO_ADAPTER}}).items() if k != 'cfg_scale'}
        if any(spec.get(k) != v for k, v in expected.items()):
            raise ValueError('Incomplete or changed Kimodo recipe')
    if 'segments' in spec:
        # Planned per-segment prompts are a Kimodo multiprompt feature only.
        if model != KIMODO_MODEL:
            raise ValueError('Segmented prompts require Kimodo')
        from g2h.kimodo_long import check_segments
        check_segments(spec['segments'], round(duration * 30))
    if 'scene' in spec:
        from .human_interaction import check
        if model != KIMODO_MODEL or 'scene_pipeline' in spec:
            raise ValueError('人体交互场景需要 Kimodo Human52 生成链路')
        check(spec['scene'], round(duration * 30))
    if spec.get('representation', REPRESENTATION) != REPRESENTATION:
        raise ValueError('Unsupported output representation')
    return model


def kimodo_paths(root):
    root = Path(root)
    base = root / 'ckpts/kimodo'
    return {'source': root / '.vendor/kimodo', 'python': root / '.kimodo-venv/bin/python',
            'checkpoint': base / 'models' / KIMODO_MODEL,
            'text': base / 'text_encoders',
            'llama': Path(os.environ.get('MOTIONFARM_LLAMA3_PATH', str(
                base / 'text_encoders/meta-llama/Meta-Llama-3-8B-Instruct')))}


def inference_python(root, model):
    return str(kimodo_paths(root)['python']) if model == KIMODO_MODEL else None


def kimodo_readiness(root):
    """A successful offline smoke run admits the exact installed asset files."""
    paths = kimodo_paths(root)
    try:
        receipt = json.loads((Path(root) / 'ckpts/kimodo/ready.json').read_text())
        if receipt.get('model') != KIMODO_MODEL or receipt.get('smoke_test_passed') is not True:
            raise ValueError('Kimodo generation has not passed validation')
        required = [paths['checkpoint'] / 'model.safetensors', paths['checkpoint'] / 'config.yaml',
                    paths['llama'] / 'config.json', paths['llama'] / 'tokenizer.json',
                    paths['llama'] / 'tokenizer_config.json', paths['llama'] / 'model.safetensors.index.json']
        required += [paths['llama'] / ('model-%05d-of-00004.safetensors' % i) for i in range(1, 5)]
        for adapter in ('mntp', 'mntp-supervised'):
            folder = paths['text'] / ('McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-' + adapter)
            required += [folder / 'adapter_config.json', folder / 'adapter_model.safetensors']
        for part in ('body', 'global_root', 'local_root'):
            required += [paths['checkpoint'] / ('stats/motion/' + part) / name for name in ('mean.npy', 'std.npy')]
        for path in required:
            stat = path.stat()
            if receipt['files'].get(str(path.resolve())) != [stat.st_size, stat.st_mtime_ns]:
                raise ValueError('Kimodo asset changed or is incomplete: ' + path.name)
        if not paths['python'].is_file() or not (paths['source'] / 'kimodo/model/load_model.py').is_file():
            raise ValueError('Kimodo runtime is missing')
        return {'available': True, 'reason': '', 'model': KIMODO_MODEL}
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': 'Kimodo 模型尚未完成安装与生成验证', 'model': KIMODO_MODEL}


def kimodo_edit_readiness(root):
    if not kimodo_readiness(root)['available']:
        return False
    try:
        receipt = json.loads((Path(root) / 'ckpts/kimodo/edit-ready.json').read_text())
        return (receipt.get('passed') is True and all(
            receipt.get('code', {}).get(name) == hashlib.sha256((Path(root) / name).read_bytes()).hexdigest()
            for name in EDIT_VALIDATION_FILES))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
