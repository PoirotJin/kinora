"""CPU-only inference with official TMR encoders; no training dependencies.

The upstream ACTOR module and Guo feature converter are loaded unmodified.
Scores are shifted cosine similarities, never acceptance probabilities.
"""
import hashlib
from collections import OrderedDict
import importlib.util
import json
from pathlib import Path
import sys

VERSION = 'tmr_humanml3d_cpu_v1'
ADAPTER = 'hy_smplh_yup_world22_linear20hz_v1'


def world_joints(record):
    import numpy as np
    m = record['motion']
    p = np.asarray(m['native_keypoints3d'], dtype=np.float64)
    t = np.asarray(m['native_transl'], dtype=np.float64)
    fps = float(m['fps'])
    if (p.ndim != 3 or p.shape[1:] != (52, 3) or
        t.shape != (len(p), 3) or not 3 <= len(p) <= 1800 or
        not 1 <= fps <= 240 or not np.isfinite(p).all() or not np.isfinite(t).all()):
        raise ValueError('unsupported_or_invalid_motion')
    # HY-Motion native joints already include body orientation. Match the
    # existing preview's root-relative trajectory. Its constant template-root
    # offset is omitted: Guo normalization removes global translation.
    # Do not rotate twice.
    world = p[:, :22] - p[:, :1] + t[:, None]
    old = np.arange(len(world)) / fps
    new = np.arange(0, old[-1] + 1e-9, 1 / 20)
    if not 3 <= len(new) <= 1200:
        raise ValueError('unsupported_duration')
    out = np.empty((len(new), 22, 3))
    for j in range(22):
        for k in range(3):
            out[:, j, k] = np.interp(new, old, world[:, j, k])
    return out


class Scorer:
    def __init__(self, assets, text_cache_size=128):
        if type(text_cache_size) is not int or not 0 <= text_cache_size <= 1024:
            raise ValueError('invalid_text_cache_size')
        self.text_cache_size = text_cache_size
        self._text_cache = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0
        import torch
        from transformers import AutoTokenizer, AutoModel
        torch.set_num_threads(2)
        assets = Path(assets).resolve()
        source = assets / 'TMR-master'
        model = assets / 'models/tmr_humanml3d_guoh3dfeats'
        config = json.loads((model / 'config.json').read_text())
        spec = importlib.util.spec_from_file_location('motionfarm_tmr_actor', source / 'src/model/actor.py')
        actor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(actor)
        self.encoders = {}
        digest = hashlib.sha256(ADAPTER.encode())
        # Pin every numerical asset and upstream implementation in the score version.
        files = [model / 'config.json', source / 'src/model/actor.py']
        files += sorted((source / 'src/guofeats').rglob('*.py'))
        files += [source / 'src/guofeats/skeleton_example_h3d.npy']
        files += sorted((assets / 'distilbert').glob('*'))
        files += [source / 'stats/humanml3d/guoh3dfeats' / n for n in ('mean.pt', 'std.pt')]
        for name in ('motion_encoder', 'text_encoder'):
            cfg = {k: v for k, v in config['model'][name].items() if not k.startswith('_')}
            encoder = actor.ACTORStyleEncoder(**cfg)
            weights = model / 'last_weights' / (name + '.pt')
            encoder.load_state_dict(torch.load(weights, map_location='cpu', weights_only=True))
            self.encoders[name] = encoder.eval()
            files.append(weights)
        for path in files:
            if path.is_file():
                digest.update(str(path.relative_to(assets)).encode())
                with path.open('rb') as f:
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        digest.update(block)
        self.version = VERSION + ':' + digest.hexdigest()[:20]
        sys.path.insert(0, str(source / 'src'))
        from guofeats import joints_to_guofeats
        self.convert = joints_to_guofeats
        stats = source / 'stats/humanml3d/guoh3dfeats'
        self.mean = torch.load(stats / 'mean.pt', map_location='cpu', weights_only=True)
        self.std = torch.load(stats / 'std.pt', map_location='cpu', weights_only=True)
        self.tokenizer = AutoTokenizer.from_pretrained(assets / 'distilbert', local_files_only=True)
        self.text_model = AutoModel.from_pretrained(assets / 'distilbert', local_files_only=True).eval()

    def encode_text(self, text):
        # This scorer owns fixed, eval-only weights/tokenizer. Versioned LRU is
        # process-local; never cache a motion score or persist text to disk.
        key = (self.version, text)
        if key in self._text_cache:
            self.cache_hits += 1
            self._text_cache.move_to_end(key)
            return self._text_cache[key]
        self.cache_misses += 1
        encoded = self.tokenizer([text], return_tensors='pt', padding=True)
        if encoded.input_ids.shape[1] > 512:
            raise ValueError('text_token_limit')
        tokens = self.text_model(**encoded).last_hidden_state
        result = self.encoders['text_encoder']({'x': tokens, 'mask': encoded.attention_mask.bool()})[:, 0]
        if self.text_cache_size:
            self._text_cache[key] = result.detach().clone()
            while len(self._text_cache) > self.text_cache_size:
                self._text_cache.popitem(last=False)
        return result

    def score(self, record):
        import numpy as np
        import torch
        text = record['text']
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('unsupported_text')
        features = self.convert(world_joints(record))
        if features.ndim != 2 or features.shape[1] != 263 or not np.isfinite(features).all():
            raise ValueError('invalid_converted_features')
        with torch.inference_mode():
            motion = (torch.as_tensor(features, dtype=torch.float32) - self.mean) / (self.std + 1e-12)
            lt = self.encode_text(text)
            # Official sample_mean=True takes the first (mu) token.
            lm = self.encoders['motion_encoder']({'x': motion[None], 'mask': torch.ones((1, len(motion)), dtype=torch.bool)})[:, 0]
            score = float(torch.nn.functional.cosine_similarity(lm, lt)[0] / 2 + .5)
        if not np.isfinite(score):
            raise ValueError('nonfinite_score')
        return dict(score=max(0., min(1., score)), version=self.version,
                    adapter=ADAPTER, frames_20hz=len(features), device='cpu',
                    meaning='shifted_cosine_similarity_not_probability',
                    adapter_validation='shadow_only_pending_human_calibration')
