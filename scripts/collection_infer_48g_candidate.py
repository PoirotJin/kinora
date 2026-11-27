"""Isolated encoder / generator processes for the motion collector (no GUI)."""
import argparse
import json
import os
import sys
import time
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HYM = ROOT / "third_party/HY-Motion-1.0"
sys.path[:0] = [str(ROOT), str(HYM)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=["encode", "generate", "run"])
    p.add_argument("--batch", type=Path, required=True)
    p.add_argument("--model", default="HY-Motion-1.0")
    p.add_argument('--encoder-device', choices=['auto', 'cuda', 'offload'], default='auto')
    p.add_argument('--gpu-batch-size', type=int, choices=[1, 2, 4], default=None)
    args = p.parse_args()
    os.environ["HYM_CKPTS_DIR"] = str(ROOT / "ckpts")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.chdir(HYM)
    print(f"stage={args.stage}: importing torch", flush=True)
    import torch
    import numpy as np
    import yaml
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    from hymotion.utils.loaders import load_object
    model_dir = ROOT / "ckpts/tencent" / args.model
    config = yaml.safe_load((model_dir / "config.yml").read_text())
    jobs = json.loads(args.batch.read_text())
    large_gpu = torch.cuda.is_available() and torch.cuda.mem_get_info()[0] >= 32 * 1024**3
    if args.stage in ('encode', 'run') and any(not Path(job['features']).exists() for job in jobs):
        cfg = config["train_pipeline_args"]
        print("Loading Qwen3-8B BF16 + CLIP, CPU threads=2", flush=True)
        encoder = load_object(cfg["text_encoder_module"], cfg["text_encoder_cfg"])
        encoder.eval()
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for the layer-offloaded Qwen encoder")
        resident = args.encoder_device == 'cuda' or (args.encoder_device == 'auto' and torch.cuda.mem_get_info()[0] >= 32 * 1024**3)
        if resident:
            encoder.to('cuda:0')
            print('Qwen + CLIP resident on GPU', flush=True)
        else:
            from accelerate import cpu_offload
            cpu_offload(encoder.llm_text_encoder, execution_device=torch.device("cuda:0"), offload_buffers=True)
            print('Qwen layer offload; CLIP on CPU', flush=True)
        cached = {}
        for job in jobs:
            path = Path(job["features"])
            if path.exists():
                cached[job['spec']['text']] = path
                continue
            text = job['spec']['text']
            if text in cached:
                tmp = path.with_suffix('.tmp')
                shutil.copyfile(cached[text], tmp)
                os.replace(tmp, path)
                print('Reused text features for '+job['id'][:12], flush=True)
                continue
            print(f"Encoding {job['id'][:12]}: {job['spec']['text']}", flush=True)
            torch.cuda.synchronize()
            started = time.perf_counter()
            with torch.inference_mode():
                vec, ctxt, length = encoder.encode([job["spec"]["text"]])
            torch.cuda.synchronize()
            print(f'encode_seconds={time.perf_counter()-started:.3f} peak_allocated_gib={torch.cuda.max_memory_allocated()/1024**3:.3f}', flush=True)
            tmp = path.with_suffix(".tmp")
            torch.save({"text_vec_raw": vec.cpu(), "text_ctxt_raw": ctxt.cpu(),
                        "text_ctxt_raw_length": length.cpu()}, tmp)
            os.replace(tmp, path)
            cached[text] = path
    if args.stage == 'encode':
        return

    print("Loading HY-Motion on GPU without text encoders", flush=True)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable for the generation stage")
    checkpoint = model_dir / "latest.ckpt"
    if not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    pipeline = load_object(config["train_pipeline"], config["train_pipeline_args"],
                           network_module=config["network_module"],
                           network_module_args=config["network_module_args"])
    pipeline.load_in_demo(str(checkpoint), build_text_encoder=False)
    pipeline.to("cuda:0").eval()
    # LBS is independent per frame. The upstream full-clip temporary mesh tensors
    # exceed 8 GB even when DiT fits; retain the exact mesh/grounding calculation.
    body_forward = pipeline.body_model.forward

    def chunked_body_forward(params):
        parts = []
        for start in range(0, len(params["rot6d"]), 16):
            parts.append(body_forward({k: v[start:start + 16] for k, v in params.items()}))
        return {k: torch.cat([part[k] for part in parts], dim=0) for k in parts[0]}

    pipeline.body_model.forward = chunked_body_forward
    from hymotion.pipeline.body_model import construct_smpl_data_dict
    batch_size = args.gpu_batch_size or (4 if large_gpu else 1)
    pending = [job for job in jobs if not Path(job['motion']).exists()]

    def infer(group):
        entries = [torch.load(job['features'], map_location='cpu', weights_only=True) for job in group]
        features = {key: torch.cat([entry[key] for entry in entries], dim=0).to('cuda:0') for key in entries[0]}
        spec = group[0]['spec']
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            output = pipeline.generate([job['spec']['text'] for job in group],
                                       [job['spec']['seed'] for job in group], spec["duration_s"],
                                       cfg_scale=spec["cfg_scale"], hidden_state_dict=features)
        torch.cuda.synchronize()
        print(f'generate_batch={len(group)} seconds={time.perf_counter()-started:.3f} peak_allocated_gib={torch.cuda.max_memory_allocated()/1024**3:.3f}', flush=True)
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
            path = Path(job['motion'])
            smpl = construct_smpl_data_dict(output['rot6d'][index], output['transl'][index])
            arrays = {f'native_{key}': value[index].numpy() for key, value in output.items() if torch.is_tensor(value)}
            arrays.update({f'smpl_{key}': np.asarray(value) for key, value in smpl.items()})
            arrays['fps'] = np.asarray(pipeline.output_mesh_fps)
            tmp = path.with_suffix('.tmp')
            with tmp.open('wb') as file:
                np.savez_compressed(file, **arrays)
            os.replace(tmp, path)
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


if __name__ == "__main__":
    main()
