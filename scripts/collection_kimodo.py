"""Offline Kimodo worker adapter. Called only inside the farm GPU lock."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from g2h.generation_backend import KIMODO_MODEL, kimodo_paths, validate_spec
from inference_metadata import (cache_profile, digest, file_identity, metadata_path,
                                read_metadata, write_json)


def run_batch(args, state=None):
    state = {} if state is None else state
    started = time.monotonic()
    jobs = json.loads(Path(args.batch).read_text())
    if not jobs or any(validate_spec(j['spec']) != KIMODO_MODEL for j in jobs):
        raise ValueError('A Kimodo batch must contain only pinned Kimodo jobs')
    if any('scene' in job['spec'] and 'edit' in job for job in jobs):
        raise ValueError('人体场景约束适用于整段生成')
    paths = kimodo_paths(ROOT)
    sys.path.insert(0, str(paths['source']))
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      TOKENIZERS_PARALLELISM='false', TEXT_ENCODER_MODE='local',
                      CHECKPOINT_DIR=str(paths['checkpoint'].parent))
    import numpy as np
    import torch
    if not state.get('threads_set'):
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        state['threads_set'] = True
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable for Kimodo generation')
    from kimodo import load_model
    from kimodo.sanitize import sanitize_texts
    from g2h.kimodo_adapter import convert, target_tpose_offsets
    from g2h.kimodo_long import prompt_texts

    def texts_for(job):
        if job.get('edit', {}).get('staged_bridge'):
            from g2h.kimodo_bridge import STAND_TEXT, TEXTS
            return [STAND_TEXT, *TEXTS]
        return prompt_texts(job['spec'])

    profile = cache_profile(ROOT, KIMODO_MODEL)
    identity = digest(profile)
    if state.get('identity') not in (None, identity):
        raise RuntimeError('Kimodo assets changed; restart the idle worker')
    state['identity'] = identity

    def valid(job):
        return Path(job['features']).is_file() and read_metadata(job['features']).get('cache_profile') == profile

    class CachedEncoder:
        def __call__(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            entries = [state['features'][t] for t in texts]
            return torch.cat([e['encoded'] for e in entries]).to('cuda:0'), [1] * len(texts)

    state.setdefault('features', {})
    for job in jobs:
        if not valid(job):
            if args.stage == 'generate':
                raise ValueError('Kimodo text features are missing or outdated')
            if 'text_encoder' not in state:
                # The official encoder applies MNTP, merges it, then applies the
                # supervised adapter. Keep the same order, using local paths only.
                from kimodo.model.llm2vec import LLM2Vec
                from peft import PeftModel
                text_root = paths['text'] / 'McGill-NLP'
                mntp = text_root / 'LLM2Vec-Meta-Llama-3-8B-Instruct-mntp'
                supervised = text_root / 'LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised'
                if not (paths['llama'] / 'config.json').is_file():
                    raise RuntimeError('Authorized Meta-Llama-3-8B-Instruct weights are required; configure MOTIONFARM_LLAMA3_PATH')
                encoder = LLM2Vec.from_pretrained(str(paths['llama']),
                    peft_model_name_or_path=str(mntp), merge_peft=True, torch_dtype=torch.bfloat16)
                encoder.model = PeftModel.from_pretrained(encoder.model, str(supervised))
                encoder.model.config._name_or_path = 'meta-llama/Meta-Llama-3-8B-Instruct'
                encoder.model.to('cuda:0').eval()
                state['text_encoder'] = encoder
            texts = sanitize_texts(texts_for(job))
            with torch.inference_mode():
                encoded = state['text_encoder'].encode(texts, batch_size=1,
                    show_progress_bar=False, device='cuda:0')
            encoded = torch.as_tensor(encoded).reshape(len(texts), 1, 4096).cpu()
            path = Path(job['features'])
            temp = path.with_suffix('.tmp')
            torch.save({'encoded': encoded}, temp)
            os.replace(temp, path)
            write_json(metadata_path(path), {'cache_profile': profile, 'execution_device': 'llm2vec_cuda_bfloat16'})
        value = torch.load(job['features'], map_location='cpu', weights_only=True)
        texts = sanitize_texts(texts_for(job))
        if value['encoded'].shape != (len(texts), 1, 4096) or not torch.isfinite(value['encoded']).all():
            raise ValueError('Invalid Kimodo text feature cache')
        # Segment prompts are encoded together; the multiprompt encoder looks each up by text.
        for i, text in enumerate(texts):
            state['features'][text] = {'encoded': value['encoded'][i:i + 1]}
    if args.stage == 'encode':
        return
    if 'pipeline' not in state:
        # Do not silently disable the official cleanup if its dependency is absent.
        import motion_correction  # noqa: F401
        state['pipeline'] = load_model('kimodo-soma-rp-v1.2', device='cuda:0', text_encoder=CachedEncoder())
    pipeline = state['pipeline']
    timings = {'generate_seconds': 0., 'write_seconds': 0.}
    for job in jobs:
        path = Path(job['motion'])
        spec = job['spec']
        scene = spec.get('scene')
        scene_digest = digest(scene) if scene is not None else None
        if path.exists():
            meta = read_metadata(path)
            if (meta.get('generation_profile_sha256') != identity or
                    meta.get('edit_request_sha256') != (digest(job['edit']) if 'edit' in job else None) or
                    meta.get('scene_request_sha256') != scene_digest):
                raise ValueError('Existing output has a different Kimodo identity')
            continue
        if scene is None:
            if 'joints' not in state:
                rig = ROOT / 'third_party/HY-Motion-1.0/scripts/gradio/static/assets/dump_wooden'
                state['joints'] = np.fromfile(rig / 'j_template.bin', dtype=np.float32).reshape(-1, 3)
                state['target_offsets'] = target_tpose_offsets(state['joints'])
                state['parents'] = np.fromfile(rig / 'kintree.bin', dtype=np.int32)
                if len(state['parents']) != 52:
                    raise ValueError('Unexpected farm compatibility rig')
                state['parents'][0] = -1
            rest, parents = state['joints'], state['parents']
        else:
            from g2h.human_rig import load
            from g2h.human_interaction_preflight import preflight

            rest, parents = load()
            scene, keyframe_preflight = preflight(scene, round(spec['duration_s'] * 30), rest, parents)
            audit_path = path.with_suffix('.preflight.json')
            write_json(audit_path, keyframe_preflight)
        seed = spec.get('inference_seed', spec['seed'])
        import random
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        begin = time.monotonic()
        edit_report = None
        constraint_lst = []
        if 'edit' in job:
            from g2h.human_rig import motion_rig
            from g2h.kimodo_edit import read_source, constraints, compose
            original, edit_plan = read_source(job['edit'])
            rest, parents = motion_rig(original)
            if edit_plan['frames'] != round(spec['duration_s'] * 30):
                raise ValueError('Edit window does not match its pinned duration')
            if not job['edit'].get('staged_bridge'):
                constraint_lst = constraints(original, edit_plan, pipeline, rest, parents)
        from g2h.kimodo_long import pipeline_request, seam_report
        request = pipeline_request(spec['text'], round(spec['duration_s'] * 30), spec.get('segments'))
        bridge_origin = None
        scene_report = None
        if scene is not None:
            from g2h import human_interaction
            constraint_lst, scene_origin, heading = human_interaction.conditioning(
                scene, round(spec['duration_s'] * 30), pipeline, rest, parents)
            request['first_heading_angle'] = heading
        if job.get('edit', {}).get('staged_bridge'):
            from g2h.kimodo_bridge import STAND_TEXT, conditioning
            with torch.inference_mode():
                reference = pipeline(prompts=STAND_TEXT, num_frames=60, num_samples=1,
                    num_denoising_steps=spec['diffusion_steps'], cfg_type='separated',
                    cfg_weight=[spec['cfg_scale'], spec['constraint_cfg_scale']],
                    post_processing=True, return_numpy=True, progress_bar=lambda x, **kw: x)
            reference, _ = convert({k: np.asarray(v)[0] for k, v in reference.items()},
                pipeline.output_skeleton.bone_order_names,
                pipeline.output_skeleton.neutral_joints.cpu().numpy(),
                rest, parents, fps=pipeline.fps)
            constraint_lst, bridge_origin, parts, bridge_targets = conditioning(original, reference, edit_plan,
                pipeline, rest, parents)
            from g2h.motion_splice import heading
            request = {'first_heading_angle': heading(original, edit_plan['start']),
                       'prompts': [p['text'] for p in parts], 'num_frames': [p['frames'] for p in parts],
                       'multi_prompt': True, 'num_transition_frames': 5}
        with torch.inference_mode():
            result = pipeline(**request,
                num_denoising_steps=spec['diffusion_steps'], num_samples=1, constraint_lst=constraint_lst,
                cfg_type='separated', cfg_weight=[spec['cfg_scale'], spec['constraint_cfg_scale']],
                post_processing=spec['post_processing'], return_numpy=True, progress_bar=lambda x, **kw: x)
        torch.cuda.synchronize()
        timings['generate_seconds'] += time.monotonic() - begin
        output = {k: np.asarray(v)[0] for k, v in result.items()}
        source = pipeline.output_skeleton
        arrays, conversion = convert(output, source.bone_order_names,
            source.neutral_joints.cpu().numpy(), rest, parents, fps=pipeline.fps,
            preserve_root=scene is not None, exact_root='edit' in job)
        if len(arrays['native_transl']) != round(spec['duration_s'] * 30):
            raise ValueError('Kimodo returned the wrong frame count')
        if bridge_origin is not None:
            for key in ('native_transl', 'smpl_trans'):
                if key in arrays:
                    arrays[key] += bridge_origin.astype(arrays[key].dtype)
        if scene is not None:
            cond_offset = human_interaction.sole_height_offset(rest, pipeline.skeleton)
            out_offset = human_interaction.sole_height_offset(rest, source)
            residual_sole = out_offset - cond_offset
            if abs(residual_sole) > 1e-7:
                for key in ('native_transl', 'smpl_trans'):
                    if key in arrays:
                        arrays[key][:, 1] += np.asarray(residual_sole, dtype=arrays[key].dtype)
                conversion['vertical_shift_m'] = float(conversion['vertical_shift_m'] + residual_sole)
            for key in ('native_transl', 'smpl_trans'):
                arrays[key] += scene_origin.astype(arrays[key].dtype)
            arrays.update(rest_joints=rest, parents=parents)
            arrays['scene_spec_json'] = human_interaction.encode(scene)
            response_audit = None
            if scene.get('attachments'):
                from g2h.object_response import couple_grasped_objects
                arrays, scene, response_audit = couple_grasped_objects(arrays, scene, rest, parents)
            scene_report = human_interaction.report(arrays, scene, rest, parents)
            if response_audit is not None:
                scene_report['object_response'] = response_audit
            from g2h.human_interaction_repair import personal_wrist_misses, repair
            if ((scene.get('carry_grip') and not scene.get('attachments'))
                    or scene_report['max_skeleton_penetration_m'] > human_interaction.MAX_PENETRATION_M
                    or personal_wrist_misses(scene, scene_report)):
                updated, audit = repair(arrays, scene, rest, parents, quality=scene_report)
                if updated is not arrays:
                    with tempfile.NamedTemporaryFile(dir=path.parent) as handle:
                        np.savez_compressed(handle, **arrays)
                        handle.flush()
                        os.fsync(handle.fileno())
                        handle.seek(0)
                        sha256 = hashlib.sha256(handle.read()).hexdigest()
                        original = path.with_name(f'{path.stem}.unrepaired-{sha256}.npz')
                        try:
                            os.link(handle.name, original)
                        except FileExistsError:
                            with original.open('rb') as existing:
                                if hashlib.sha256(existing.read()).hexdigest() != sha256:
                                    raise ValueError('保存的原始候选校验失败')
                    audit.update(original_sha256=sha256, original_npz=original.name)
                    arrays = updated
                    if scene.get('attachments'):
                        from g2h.object_response import couple_grasped_objects
                        arrays, scene, response_audit = couple_grasped_objects(arrays, scene, rest, parents)
                    scene_report = human_interaction.report(arrays, scene, rest, parents)
                    if response_audit is not None:
                        scene_report['object_response'] = response_audit
                scene_report['repair'] = audit
            scene_report['keyframe_preflight'] = {
                'version': keyframe_preflight['version'], 'audit_file': audit_path.name,
                'repaired_frames': [key['frame'] for key in keyframe_preflight['keyframes']
                                    if key.get('status') == 'repaired'],
            }
            if not scene_report['passed']:
                rejected = path.with_name(path.stem + '.quarantine.npz')
                if not rejected.exists():
                    with rejected.open('xb') as handle:
                        np.savez_compressed(handle, **arrays)
                    write_json(metadata_path(rejected), scene_report)
                scene_report['warning'] = '；'.join(scene_report['failures'])[:200]
        sequence_report = seam_report(arrays, round(spec['duration_s'] * 30),
            parts if bridge_origin is not None else spec.get('segments'),
            transition_frames=5 if bridge_origin is not None else 30)
        if bridge_origin is not None:
            from g2h.kimodo_bridge import anchor_report, forward_report
            anchors = anchor_report(arrays, bridge_targets, pipeline, rest, parents)
            sequence_report['anchors'] = anchors
            sequence_report['forward_walk'] = forward_report(arrays, bridge_targets)
            sequence_report['passed'] = sequence_report['passed'] and anchors['passed'] and sequence_report['forward_walk']['passed']
        if not sequence_report['passed']:
            rejected = path.with_name(path.stem + '.quarantine.npz')
            if not rejected.exists():
                with rejected.open('xb') as handle:
                    np.savez_compressed(handle, **arrays)
                write_json(metadata_path(rejected), sequence_report)
            details = sequence_report.get('anchors', {}).get('failures', []) + sequence_report.get('forward_walk', {}).get('failures', [])
            details += ['阶段衔接帧 %d：位移 %.3fm，加速度 %.1fm/s²，旋转 %.1f°' %
                (c['frame'], c['joint_step_max_m'], c['joint_acceleration_max_m_s2'],
                 np.rad2deg(c['rotation_step_max_rad'])) for c in sequence_report['boundaries'] if not c['passed']]
            sequence_report['warning'] = '；'.join(details)[:200]
        if 'edit' in job:
            arrays, edit_report = compose(original, arrays, job['edit']['start_frame'],
                job['edit']['end_frame'], edit_plan, rest, parents)
            if bridge_origin is not None:
                from g2h.kimodo_bridge import VERSION, LABELS
                from g2h.kimodo_gait import review as review_gait
                edit_report['bridge'] = {'version': VERSION, 'forward_walk': sequence_report['forward_walk'],
                    'gait_review': review_gait(arrays, job['edit']['start_frame'], job['edit']['end_frame']), 'stages': [
                    dict(part, label=label) for part, label in zip(parts, LABELS)],
                    'semantic_verified': False, 'dynamics_verified': False}
        begin = time.monotonic()
        temp = path.with_suffix('.tmp')
        with temp.open('wb') as handle:
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        write_json(metadata_path(path), {'runtime_version': 'motionfarm-kimodo-v1',
            'generator_name': KIMODO_MODEL, 'generation_profile_sha256': identity,
            'generation_profile': profile, 'effective_seed': seed,
            'generator_checkpoint': file_identity(paths['checkpoint'] / 'model.safetensors'),
            'feature': read_metadata(job['features']), 'conversion': conversion,
            'edit_request_sha256': digest(job['edit']) if 'edit' in job else None,
            'editing': edit_report, 'sequence': sequence_report,
            'scene_request_sha256': scene_digest, 'scene': scene_report,
            'scene_conditioning_sha256': digest(scene) if scene is not None else None,
            'generation_device': 'cuda:0', 'generation_batch_size': 1,
            'torch_version': torch.__version__, 'persistent': bool(getattr(args, 'persistent', False))})
        os.replace(temp, path)
        timings['write_seconds'] += time.monotonic() - begin
        print('Kimodo saved ' + str(path), flush=True)
    # Do not retain every prompt embedding in a process that runs all day.
    state['features'] = {}
    state['batches'] = state.get('batches', 0) + 1
    return {'seconds': round(time.monotonic() - started, 3), 'worker_batches': state['batches'],
            'model': KIMODO_MODEL, 'model_resident': True,
            'encoder_resident': 'text_encoder' in state, 'timings': timings}
