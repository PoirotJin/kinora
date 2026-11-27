"""Encoder / generator entry point, reusable by the private inference worker."""
import argparse
import json
import os
import sys
import time
import shutil
from pathlib import Path
from inference_metadata import (RUNTIME_VERSION, cache_profile, digest, file_identity,
                                metadata_path, read_metadata, write_json)

ROOT = Path(__file__).resolve().parents[1]
HYM = ROOT / "third_party/HY-Motion-1.0"
sys.path[:0] = [str(ROOT), str(HYM)]


def run_batch(args, state=None):
    jobs = json.loads(args.batch.read_text())
    from g2h.generation_backend import KIMODO_MODEL, validate_spec
    models = {validate_spec(job['spec']) for job in jobs}
    if len(models) != 1:
        raise ValueError('Do not mix generation backends in one batch')
    if models == {KIMODO_MODEL}:
        from collection_kimodo import run_batch as run_kimodo
        return run_kimodo(args, state)
    if models:
        args.model = next(iter(models))
    state = {} if state is None else state
    started_batch = time.perf_counter()
    timings = dict(encoder_load_seconds=0., encode_seconds=0., pipeline_load_seconds=0.,
                   generate_seconds=0., write_seconds=0.)
    os.environ["HYM_CKPTS_DIR"] = str(ROOT / "ckpts")
    os.environ["USE_HF_MODELS"] = "0"  # Provenance fingerprints these explicit local checkpoints.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.chdir(HYM)
    print(f"stage={args.stage}: importing torch", flush=True)
    import torch
    import numpy as np
    import yaml
    if not getattr(run_batch, '_threads_set', False):
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        run_batch._threads_set = True
    from hymotion.utils.loaders import load_object
    model_dir = ROOT / "ckpts/tencent" / args.model
    config = yaml.safe_load((model_dir / "config.yml").read_text())
    jobs = json.loads(args.batch.read_text())
    profile = cache_profile(ROOT, args.model, args.encoder_device)
    identity = digest({'features': profile, 'checkpoint': file_identity(model_dir / 'latest.ckpt')})
    if state.get('identity') not in (None, identity):
        # Never retain an old in-memory checkpoint after files/config have changed.
        import gc
        state.clear()
        gc.collect()
        torch.cuda.empty_cache()
    state['identity'] = identity
    large_gpu = state.setdefault('large_gpu', torch.cuda.is_available() and torch.cuda.mem_get_info()[0] >= 32 * 1024**3)

    def feature_valid(job):
        meta = read_metadata(job['features'])
        return Path(job['features']).is_file() and meta.get('cache_profile') == profile

    if args.stage in ('encode', 'run') and any(not feature_valid(job) for job in jobs):
        cfg = config["train_pipeline_args"]
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for the layer-offloaded Qwen encoder")
        if 'encoder' not in state:
            started_load = time.perf_counter()
            print("Loading Qwen3-8B BF16 + CLIP, CPU threads=2", flush=True)
            encoder = load_object(cfg["text_encoder_module"], cfg["text_encoder_cfg"])
            encoder.eval()
            resident = args.encoder_device == 'cuda' or (args.encoder_device == 'auto' and large_gpu)
            if resident:
                encoder.to('cuda:0')
                print('Qwen + CLIP resident on GPU', flush=True)
            else:
                from accelerate import cpu_offload
                cpu_offload(encoder.llm_text_encoder, execution_device=torch.device("cuda:0"), offload_buffers=True)
                print('Qwen layer offload; CLIP on CPU', flush=True)
            state['encoder'] = encoder
            state['encoder_runtime'] = {
                'execution_device': 'qwen_cuda_clip_cuda' if resident else 'qwen_cuda_layer_offload_clip_cpu',
                'qwen_dtype': str(next(encoder.llm_text_encoder.parameters()).dtype),
                'clip_dtype': str(next(encoder.sentence_emb_text_encoder.parameters()).dtype)}
            torch.cuda.synchronize()
            timings['encoder_load_seconds'] += time.perf_counter() - started_load
        encoder = state['encoder']
        cached = {}
        for job in jobs:
            path = Path(job["features"])
            if feature_valid(job):
                cached[job['spec']['text']] = path
                continue
            text = job['spec']['text']
            if text in cached:
                tmp = path.with_suffix('.tmp')
                shutil.copyfile(cached[text], tmp)
                os.replace(tmp, path)
                write_json(metadata_path(path), read_metadata(cached[text]))
                print('Reused text features for '+job['id'][:12], flush=True)
                continue
            print(f"Encoding {job['id'][:12]}: {job['spec']['text']}", flush=True)
            torch.cuda.synchronize()
            started = time.perf_counter()
            with torch.inference_mode():
                vec, ctxt, length = encoder.encode([job["spec"]["text"]])
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - started
            timings['encode_seconds'] += elapsed
            print(f'encode_seconds={elapsed:.3f} peak_allocated_gib={torch.cuda.max_memory_allocated()/1024**3:.3f}', flush=True)
            tmp = path.with_suffix(".tmp")
            torch.save({"text_vec_raw": vec.cpu(), "text_ctxt_raw": ctxt.cpu(),
                        "text_ctxt_raw_length": length.cpu()}, tmp)
            os.replace(tmp, path)
            write_json(metadata_path(path), {'cache_profile': profile, **state['encoder_runtime']})
            cached[text] = path
    if args.stage == 'encode':
        return

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable for the generation stage")
    checkpoint = model_dir / "latest.ckpt"
    if not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    if 'pipeline' not in state:
        started_load = time.perf_counter()
        print("Loading HY-Motion on GPU without text encoders", flush=True)
        pipeline = load_object(config["train_pipeline"], config["train_pipeline_args"],
                               network_module=config["network_module"],
                               network_module_args=config["network_module_args"])
        pipeline.load_in_demo(str(checkpoint), build_text_encoder=False)
        pipeline.to("cuda:0").eval()
        # Keep the verified, bounded LBS calculation; a larger chunk needs its own benchmark.
        body_forward = pipeline.body_model.forward

        def chunked_body_forward(params):
            parts = []
            for start in range(0, len(params["rot6d"]), 16):
                parts.append(body_forward({k: v[start:start + 16] for k, v in params.items()}))
            return {k: torch.cat([part[k] for part in parts], dim=0) for k in parts[0]}

        pipeline.body_model.forward = chunked_body_forward
        state['pipeline'] = pipeline
        torch.cuda.synchronize()
        timings['pipeline_load_seconds'] += time.perf_counter() - started_load
    pipeline = state['pipeline']
    from hymotion.pipeline.body_model import construct_smpl_data_dict
    batch_size = args.gpu_batch_size or (4 if large_gpu else 1)
    pending = [job for job in jobs if not Path(job['motion']).exists()]

    def infer(group):
        if any(not feature_valid(job) for job in group):
            raise ValueError('Missing or obsolete text feature metadata; encode the batch again')
        entries = [torch.load(job['features'], map_location='cpu', weights_only=True) for job in group]
        features = {key: torch.cat([entry[key] for entry in entries], dim=0).to('cuda:0') for key in entries[0]}
        spec = group[0]['spec']
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            output = pipeline.generate([job['spec']['text'] for job in group],
                                       [job['spec'].get('inference_seed',job['spec']['seed']) for job in group], spec["duration_s"],
                                       cfg_scale=spec["cfg_scale"], hidden_state_dict=features)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        timings['generate_seconds'] += elapsed
        print(f'generate_batch={len(group)} seconds={elapsed:.3f} peak_allocated_gib={torch.cuda.max_memory_allocated()/1024**3:.3f}', flush=True)
        return output

    def generate_group(group):
        failed = False
        try:
            output = infer(group)
        except torch.cuda.OutOfMemoryError:
            if len(group) == 1:
                raise
            failed = True
        if failed:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            print('GPU batch out of memory; retrying each seed separately', flush=True)
            for job in group:
                generate_group([job])
            return
        for index, job in enumerate(group):
            started_write = time.perf_counter()
            path = Path(job['motion'])
            smpl = construct_smpl_data_dict(output['rot6d'][index], output['transl'][index])
            arrays = {f'native_{key}': value[index].numpy() for key, value in output.items() if torch.is_tensor(value)}
            arrays.update({f'smpl_{key}': np.asarray(value) for key, value in smpl.items()})
            arrays['fps'] = np.asarray(pipeline.output_mesh_fps)
            tmp = path.with_suffix('.tmp')
            with tmp.open('wb') as file:
                np.savez_compressed(file, **arrays)
            write_json(metadata_path(path), {
                'runtime_version': RUNTIME_VERSION,
                'effective_seed': job['spec'].get('inference_seed',job['spec']['seed']),
                'generator_identity_kind': 'file-stat-v1',
                'feature': read_metadata(job['features']),
                'generator_checkpoint': file_identity(checkpoint),
                'generation_device': 'cuda:0', 'generation_batch_size': len(group),
                'torch_version': torch.__version__,
                'persistent': bool(getattr(args, 'persistent', False))})
            os.replace(tmp, path)
            timings['write_seconds'] += time.perf_counter() - started_write
            print(f'Saved {path}', flush=True)

    while pending:
        group = [pending.pop(0)]
        spec = group[0]['spec']
        while pending and len(group) < batch_size:
            other = pending[0]['spec']
            if (other['duration_s'], other['cfg_scale']) != (spec['duration_s'], spec['cfg_scale']):
                break
            group.append(pending.pop(0))
        generate_group(group)
    state['batches'] = state.get('batches', 0) + 1
    return {'seconds': round(time.perf_counter() - started_batch, 3),
            'worker_batches': state['batches'], 'model_resident': 'pipeline' in state,
            'encoder_resident': 'encoder' in state,
            'timings': {key: round(value, 3) for key, value in timings.items()}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('stage', choices=['encode', 'generate', 'run'])
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--model', default='HY-Motion-1.0')
    p.add_argument('--encoder-device', choices=['auto', 'cuda', 'offload'], default='auto')
    p.add_argument('--gpu-batch-size', type=int, choices=[1, 2, 4], default=None)
    args = p.parse_args()
    run_batch(args)


if __name__ == "__main__":
    main()
