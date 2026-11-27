"""Small, deterministic inference identities; never read large weight contents."""
import hashlib
import json
from itertools import islice
from pathlib import Path

FEATURE_VERSION = 'hy-text-features-v2'
RUNTIME_VERSION = 'motionfarm-persistent-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_identity(path):
    path = Path(path)
    stat = path.stat()
    return {'name': path.name, 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'ctime_ns': stat.st_ctime_ns}


def weight_identity(root):
    """Stat only the two known encoder directories, never scan the checkpoint tree."""
    found = []
    for name in ('Qwen3-8B', 'clip-vit-large-patch14'):
        folder = Path(root) / 'ckpts' / name
        entries = list(islice(folder.iterdir(), 513))
        if len(entries) > 512:
            raise ValueError('Unexpected encoder directory size')
        files = [file_identity(p) for p in sorted(entries)
                 if p.is_file() and p.suffix in ('.safetensors', '.bin', '.json', '.model', '.txt')]
        if not files:
            raise ValueError('Missing encoder weights: ' + name)
        found.append({'model': name, 'files': files})
    return {'kind': 'file-stat-v1', 'sha256': digest(found)}


def cache_profile(root, model='HY-Motion-1.0', device_policy='auto'):
    root = Path(root)
    from g2h.generation_backend import HY_MODEL, KIMODO_MODEL, KIMODO_ADAPTER, kimodo_paths
    if model == KIMODO_MODEL:
        paths = kimodo_paths(root)
        config = paths['checkpoint'] / 'config.yaml'
        required = [paths['checkpoint'] / 'model.safetensors', paths['llama'] / 'config.json']
        encoders = []
        for directory in (paths['llama'], paths['text'] / 'McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp',
                          paths['text'] / 'McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised'):
            if not directory.is_dir():
                raise ValueError('Missing authorized Kimodo dependency: ' + str(directory))
            entries = list(islice(directory.iterdir(), 129))
            if len(entries) > 128:
                raise ValueError('Unexpected Kimodo checkpoint directory size')
            files = [file_identity(p) for p in sorted(entries) if p.is_file()
                     and p.suffix in ('.safetensors', '.json', '.model')]
            if not any(p.is_file() and p.suffix == '.safetensors' for p in entries):
                raise ValueError('Missing local Kimodo text encoder weights: ' + str(directory))
            encoders.append({'directory': directory.name, 'files': files})
        for path in required:
            if not path.is_file():
                raise ValueError('Missing authorized Kimodo dependency: ' + str(path))
        source_files = ['kimodo/model/kimodo_model.py', 'kimodo/model/load_model.py',
                        'kimodo/model/llm2vec/llm2vec.py', 'kimodo/model/llm2vec/llm2vec_wrapper.py',
                        'kimodo/skeleton/definitions.py', 'kimodo/postprocess.py']
        source_hashes = {name: hashlib.sha256((paths['source'] / name).read_bytes()).hexdigest()
                         for name in source_files}
        return {'version': 'kimodo-llm2vec-features-v1', 'model': model,
                'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                'checkpoint': file_identity(required[0]), 'text_encoders': encoders,
                'upstream_source_sha256': digest(source_hashes),
                'adapter_version': KIMODO_ADAPTER,
                'adapter_sha256': hashlib.sha256((root / 'g2h/kimodo_adapter.py').read_bytes()).hexdigest(),
                'encoder_source_sha256': hashlib.sha256((root / 'scripts/collection_kimodo.py').read_bytes()).hexdigest(),
                'sequence_source_sha256': hashlib.sha256((root / 'g2h/kimodo_long.py').read_bytes()).hexdigest(),
                'gait_source_sha256': hashlib.sha256((root / 'g2h/kimodo_gait.py').read_bytes()).hexdigest(),
                'bridge_source_sha256': hashlib.sha256((root / 'g2h/kimodo_bridge.py').read_bytes()).hexdigest(),
                'edit_source_sha256': hashlib.sha256((root / 'g2h/kimodo_edit.py').read_bytes()).hexdigest(),
                'dtype_policy': 'llm2vec-bfloat16', 'device_policy': 'cuda'}
    if model != HY_MODEL:
        raise ValueError('Unsupported generation model: ' + model)
    config = root / 'ckpts/tencent' / model / 'config.yml'
    source_files = ('hymotion/network/text_encoders/text_encoder.py',
                    'hymotion/network/text_encoders/model_constants.py')
    source = {name: hashlib.sha256((root / 'third_party/HY-Motion-1.0' / name).read_bytes()).hexdigest()
              for name in source_files}
    return {'version': FEATURE_VERSION, 'model': model,
            'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
            'encoder_source': digest(source), 'weights': weight_identity(root),
            'dtype_policy': {'qwen': 'bfloat16', 'clip': 'float32'},
            'device_policy': device_policy}


def feature_key(text, profile):
    return digest({'text': text, 'encoder': profile})


def metadata_path(path):
    return Path(str(path) + '.meta.json')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
    temporary.replace(path)


def read_metadata(path):
    try:
        data = metadata_path(path).read_text()
        if len(data) > 16384:
            return {}
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
