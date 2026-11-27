"""Durable text-to-motion queue, with one event-driven collector process."""
import argparse
import datetime
import hashlib
import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from g2h.collection_store import CollectionStore, connect, task_id
from g2h import observability
from inference_metadata import cache_profile, digest, feature_key, metadata_path, read_metadata
from g2h.generation_backend import (HY_MODEL, KIMODO_MODEL, current_recipe,
                                    inference_python, validate_spec)


def notify(root):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            sock.sendto(b"wake", str(root / "wake.sock"))
        except (FileNotFoundError, ConnectionRefusedError, BlockingIOError):
            pass  # Already notified, or the durable queue is read on next start.


def enqueue(args):
    raw = json.loads(args.file.read_text()) if args.file else [{"text": args.text, "topic": args.topic}]
    if not isinstance(raw, list):
        raise ValueError("Input JSON must be a list of prompt objects")
    specs = []
    recipe = current_recipe()
    for item in raw:
        for seed in item.get("seeds", args.seeds):
            spec = {"text": item["text"].strip(), "topic": item.get("topic", args.topic),
                    "duration_s": float(item.get("duration_s", args.duration)),
                    "seed": int(seed), **recipe, "representation": "human_smplh_y_up_v1"}
            spec['cfg_scale'] = float(item.get('cfg_scale', recipe['cfg_scale']))
            if 'scene' in item:
                spec['scene'] = item['scene']
            validate_spec(spec)
            if not 0 <= spec["seed"] < 2**32 or not 1 <= spec["cfg_scale"] <= 20:
                raise ValueError("Invalid seed or cfg_scale")
            specs.append(spec)
    db = connect(args.root)
    before = db.total_changes
    with db:
        for spec in specs:
            db.execute("INSERT OR IGNORE INTO tasks(id, spec) VALUES (?, ?)",
                       (task_id(spec), json.dumps(spec, ensure_ascii=False)))
    added = db.total_changes - before
    db.close()
    notify(args.root)
    print(json.dumps({"enqueued": added, "duplicates": len(specs) - added}))


def _scene_conditioning_matches(requested, effective):
    """Allow attachment-coupled object motion and keyframe pelvis adjustments while locking geometry."""
    if not isinstance(requested, dict) or not isinstance(effective, dict) or set(effective) != set(requested):
        return False
    attached_ids = {
        att['object_id']
        for att in (requested.get('attachments') or ())
        if isinstance(att, dict) and 'object_id' in att
    }
    if any(effective[key] != requested[key] for key in requested if key not in ('objects', 'keyframes')):
        return False
    if len(effective['objects']) != len(requested['objects']):
        return False
    for req_obj, eff_obj in zip(requested['objects'], effective['objects']):
        if req_obj.get('id') in attached_ids:
            req_fixed = {k: req_obj[k] for k in req_obj if k not in ('center_m', 'trajectory')}
            eff_fixed = {k: eff_obj[k] for k in eff_obj if k not in ('center_m', 'trajectory')}
            if eff_fixed != req_fixed:
                return False
        elif eff_obj != req_obj:
            return False
    if len(effective['keyframes']) != len(requested['keyframes']):
        return False
    for orig_kf, cond_kf in zip(requested['keyframes'], effective['keyframes']):
        if set(orig_kf) != set(cond_kf):
            return False
        mutable = ('body_rotvecs', 'pelvis_offset_m') if orig_kf.get('object_id') in attached_ids else ('body_rotvecs',)
        if any(orig_kf[key] != cond_kf[key] for key in orig_kf if key not in mutable):
            return False
    return True


def _handle_direction_semantics(store, job, record):
    """Allow 1 semantic retry for scene-free custom jobs, then accept as review; keep batch topics strict."""
    semantics = record['quality']['direction_semantics']
    if semantics['status'] != 'fail':
        return True
    if job['spec'].get('topic') == 'custom':
        if 'scene' not in job['spec'] and job['spec'].get('semantic_attempt', 1) < 2:
            from g2h.semantic_queue import reject
            reject(store, job, semantics, max_attempts=2)
            return False
        semantics['warnings'] = list(semantics.get('warnings', [])) + list(semantics.get('reasons', []))
        semantics['reasons'] = []
        semantics['status'] = 'review'
        record['quality']['direction_unverified'] = True
        return True
    from g2h.semantic_queue import reject
    reject(store, job, semantics)
    return False


def collect_record(job):
    import numpy as np
    with np.load(job["motion"], allow_pickle=False) as data:
        motion = {k: data[k].copy() for k in data.files}
    fps = float(motion["fps"])
    trans = motion["native_transl"]
    frames = len(trans)
    if trans.shape != (frames, 3) or frames != round(job["spec"]["duration_s"] * fps):
        raise ValueError("Unexpected frame count or translation shape")
    for key, value in motion.items():
        if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
            raise ValueError(f"Non-finite generated data: {key}")
    speed = np.linalg.norm(np.diff(trans, axis=0), axis=1) * fps
    rot = motion["native_root_rotations_mat"]
    relative = np.einsum("tji,tjk->tik", rot[:-1], rot[1:])
    angular_speed = np.arccos(np.clip((np.trace(relative, axis1=1, axis2=2) - 1) / 2, -1, 1)) * fps
    tail = max(1, round(fps))
    q = {"finite": True, "frames": frames, "fps": fps,
         "tail_root_speed_mean_mps": float(speed[-tail:].mean()),
         "tail_root_speed_max_mps": float(speed[-tail:].max()),
         "tail_root_angular_speed_mean_radps": float(angular_speed[-tail:].mean()),
         "root_speed_max_mps": float(speed.max()),
         "tail_stationary_candidate": bool(speed[-tail:].max() < .15 and angular_speed[-tail:].mean() < .3),
         "semantic_verified": False, "robot_dynamics_verified": False,
         "review_required": True}
    from g2h.motion_semantics import for_record
    q['direction_semantics'] = for_record(dict(job['spec'], motion=motion))
    inference = read_metadata(job['motion'])
    if 'scene' in job['spec']:
        from g2h.human_interaction import decode
        requested = job['spec']['scene']
        effective = decode(motion.get('scene_spec_json',np.empty(0,dtype=np.uint8)), frames)
        report = inference.get('scene') or {}
        if (not isinstance(report,dict)
                or inference.get('scene_request_sha256') != digest(requested)
                or inference.get('scene_conditioning_sha256') != digest(effective)
                or not _scene_conditioning_matches(requested, effective)):
            raise ValueError('场景生成结果缺少匹配的验收记录')
        q['human_interaction'] = report
        if not report.get('passed'):
            q['quality_warnings'] = list(report.get('failures', []))
    feature = inference.get('feature', read_metadata(job['features']))
    model = validate_spec(job['spec'])
    if model == KIMODO_MODEL:
        if inference.get('generator_name') != model or not inference.get('conversion'):
            raise ValueError('Missing Kimodo identity or conversion provenance')
        provenance = {'generator': model, 'text_encoders': ['LLM2Vec-Meta-Llama-3-8B-Instruct'],
                      'model_config_sha256': inference['generation_profile']['config_sha256'],
                      'checkpoint_bytes': inference['generator_checkpoint']['bytes']}
    else:
        provenance = {'generator': HY_MODEL, 'text_encoders': ['Qwen3-8B', 'CLIP ViT-L/14'],
                      'model_config_sha256': feature.get('cache_profile', {}).get('config_sha256') or hashlib.sha256((ROOT / 'ckpts/tencent/HY-Motion-1.0/config.yml').read_bytes()).hexdigest(),
                      'checkpoint_bytes': inference.get('generator_checkpoint', {}).get('bytes') or (ROOT / 'ckpts/tencent/HY-Motion-1.0/latest.ckpt').stat().st_size}
    return {"schema_version": 1, "id": job["id"], **job["spec"],
            **({"motion_id": job["spec"]["farm"]["motion_id"],
                "text_id": job["spec"]["farm"]["prompt_id"]}
               if job["spec"].get("farm", {}).get("motion_id") else {}),
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "motion": motion, "quality": q,
            "coordinates": {"up_axis": "Y", "translation_unit": "metre",
                            "smpl_poses": "SMPL-H 52 joints, axis-angle radians"},
            "provenance": {**provenance,
                           "text_features_sha256": hashlib.sha256(Path(job["features"]).read_bytes()).hexdigest() if Path(job['features']).is_file() else None,
                           "prompt_rewrite": False,
                           "text_encoding_device": feature.get('execution_device', 'legacy_unknown'),
                           "generation_device": inference.get('generation_device', 'cuda:0'),
                           "inference": inference or {'runtime_version': 'legacy_unknown'}}}


def run(args):
    args.root.mkdir(parents=True, exist_ok=True)
    farm_root=args.root.parent.parent
    telemetry_root=farm_root if args.root.parent.name=='topics' else args.root
    observability.initialize(telemetry_root)
    with CollectionStore(args.root, args.max_bytes) as store:
        observability.initialize_outbox(telemetry_root,store.db)
        observability.flush_motions(telemetry_root,store.db)
        stop = args.root / "STOP"
        stop.unlink(missing_ok=True)
        work = args.root / "work"
        work.mkdir(exist_ok=True)
        sock_path = args.root / "wake.sock"
        sock_path.unlink(missing_ok=True)
        wake = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        wake.bind(str(sock_path))
        wake.setblocking(False)
        (args.root / "collector.pid").write_text(str(os.getpid()))
        child = None

        def request_stop(signum, frame):
            stop.touch()
            notify(args.root)
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        try:
            batches=0
            while not stop.exists():
                if getattr(args,'max_batches',0) and batches>=args.max_batches:break
                from g2h.cold_storage import guard
                if (farm_root/'prompts.sqlite3').exists():guard(farm_root)
                seed_min=getattr(args,'seed_min',None);seed_max=getattr(args,'seed_max',None)
                where=" AND json_extract(spec,'$.seed') BETWEEN ? AND ?" if seed_min is not None else ''
                params=(seed_min,seed_max,args.batch_size) if seed_min is not None else (args.batch_size,)
                rows = store.db.execute("SELECT id, spec FROM tasks WHERE state='pending'"+where+" ORDER BY rowid LIMIT ?",params).fetchall()
                if not rows:
                    if not args.serve:
                        break
                    print("Queue empty; waiting for new text (persistent models expire after idle timeout).", flush=True)
                    select.select([wake], [], [])
                    while True:
                        try:
                            wake.recv(1024)
                        except BlockingIOError:
                            break
                    continue
                if shutil.disk_usage(args.root).free < args.min_free_gb * 1_000_000_000:
                    raise RuntimeError("Free disk space below configured reserve; collector stopped")
                jobs = []
                # Backend is part of immutable queue identity, including old work.
                batch_model = json.loads(rows[0][1])['model']
                profile = cache_profile(ROOT, batch_model)
                for key, spec in rows:
                    parsed = json.loads(spec)
                    if parsed['model'] != batch_model:
                        break
                    validate_spec(parsed)
                    # Segmented Kimodo prompts share one feature file per ordered text list.
                    feature_id = feature_key(parsed['text'] if 'segments' not in parsed else
                                             [s['text'] for s in parsed['segments']], profile)
                    jobs.append({"id": key, "spec": parsed,
                                 "features": str(work / f"text_{feature_id}.pt"), "motion": str(work / f"{key}.npz")})
                batch = work / "batch.json"
                batch.write_text(json.dumps(jobs, ensure_ascii=False))
                batches+=1
                error = None
                env = dict(os.environ, OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
                           TOKENIZERS_PARALLELISM="false", HF_HUB_OFFLINE="1")
                persistent = os.environ.get('MOTIONFARM_INFER_PERSISTENT') == '1' and os.environ.get('MOTIONFARM_GPU_FD')
                stages = ('run',) if batch_model == KIMODO_MODEL or persistent or os.environ.get('MOTIONFARM_INFER_FUSED') == '1' else ('encode', 'generate')
                telemetry_source,telemetry_job=observability.record_context(jobs[0]['spec'])
                from inference_capacity import temporary_space
                with temporary_space(farm_root, jobs):
                    for stage in stages:
                        needed = "features" if stage == "encode" else "motion"
                        if all(Path(j[needed]).exists() and
                               (stage != 'encode' or read_metadata(j[needed]).get('cache_profile') == profile)
                               for j in jobs):
                            continue
                        if stop.exists():
                            break
                        print(f"Stage {stage}: {len(jobs)} records", flush=True)
                        if persistent:
                            from inference_worker import request_batch
                            try:
                                with observability.span(telemetry_root,'inference',args.root.name,telemetry_source,telemetry_job):
                                    result = request_batch(farm_root, batch, int(os.environ['MOTIONFARM_GPU_FD']),
                                                           args.stage_timeout, stop.exists)
                                print(json.dumps({'persistent_inference': result}), flush=True)
                            except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
                                error = str(exc)[:500]
                            if error:
                                break
                            continue
                        with observability.span(telemetry_root,'inference',args.root.name,telemetry_source,telemetry_job) as timing:
                            python = inference_python(ROOT, batch_model) or sys.executable
                            child = subprocess.Popen([python, "-u", str(ROOT / "scripts/collection_infer.py"),
                                                      stage, "--batch", str(batch)], env=env, start_new_session=True,
                                                      pass_fds=(int(os.environ['MOTIONFARM_GPU_FD']),) if os.environ.get('MOTIONFARM_GPU_FD') else ())
                            try:
                                rc = child.wait(timeout=args.stage_timeout)
                                if rc:
                                    error = f"{stage} exited {rc}; inspect collector.log"
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGTERM)
                                try:
                                    child.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    os.killpg(child.pid, signal.SIGKILL)
                                    child.wait()
                                error = f"{stage} exceeded {args.stage_timeout} seconds"
                            finally:
                                child = None
                            if error:timing.status='failed'
                        if error:
                            break
                for job in jobs:
                    if Path(job["motion"]).exists():
                        try:
                            with observability.span(telemetry_root,'storage',args.root.name,telemetry_source,telemetry_job):
                                record = collect_record(job)
                                if not _handle_direction_semantics(store, job, record):
                                    continue
                                observability.prepare_motion(telemetry_root,store.db,record)
                                store.append(record)
                            observability.flush_motions(telemetry_root,store.db)
                            try:
                                if record.get('motion_id'):
                                    from g2h.direction_index import save
                                    digest=store.db.execute('SELECT sha256 FROM records WHERE id=?',(record['id'],)).fetchone()[0]
                                    save(farm_root,record['motion_id'],digest,record['quality']['direction_semantics'])
                            except (OSError,ValueError,sqlite3.Error):
                                print('Direction index pending; background worker will reconcile',flush=True)
                            try:
                                from g2h.agent_learning import index_record
                                index_record(args.root.parent.parent,record)
                            except Exception as screening_error:
                                print('Screening pending:',type(screening_error).__name__,flush=True)
                            print(json.dumps({"saved": job["id"], "quality": record["quality"]}), flush=True)
                            # Serialized arrays are already durable in the PKL shard.
                            try:
                                removed = 0
                                for completed in (Path(job['motion']), metadata_path(job['motion'])):
                                    if completed.exists():
                                        size = completed.stat().st_size
                                        completed.unlink()
                                        removed += size
                                if (farm_root / 'prompts.sqlite3').exists():
                                    from g2h.capacity import release
                                    release(farm_root, removed)
                            except Exception as cleanup_error:
                                # The PKL commit already succeeded; a conservative
                                # capacity overcount must not requeue this motion.
                                print('Temporary cleanup pending:', type(cleanup_error).__name__, flush=True)
                            # Keep small text features for reproducibility / reuse.
                        except Exception as exc:
                            with store.db:
                                store.db.execute("UPDATE tasks SET state='failed', error=? WHERE id=?",
                                                 (str(exc), job["id"]))
                    elif error and not stop.exists():
                        with store.db:
                            store.db.execute("UPDATE tasks SET state='failed', error=? WHERE id=?", (error, job["id"]))
                if error and not stop.exists():
                    # Do not retry a broken model/OOM forever or consume the entire queue.
                    raise RuntimeError(error)
        finally:
            wake.close()
            sock_path.unlink(missing_ok=True)
            (args.root / "collector.pid").unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT / "outputs/motion_collection")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("enqueue")
    source = e.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--text")
    e.add_argument("--topic", default="custom")
    e.add_argument("--duration", type=float, default=8)
    e.add_argument("--seeds", type=int, nargs="+", default=[20260916])
    r = sub.add_parser("run")
    r.add_argument("--serve", action="store_true")
    r.add_argument('--max-batches',type=int,default=0,help='Fair scheduling quantum; zero drains the queue')
    r.add_argument('--seed-min',type=int)
    r.add_argument('--seed-max',type=int)
    r.add_argument("--batch-size", type=int, default=4)
    r.add_argument("--max-bytes", type=int, default=1_000_000_000)
    r.add_argument("--stage-timeout", type=int, default=1800)
    r.add_argument("--min-free-gb", type=float, default=20)
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("retry-failed")
    args = p.parse_args()
    args.root = args.root.resolve()
    if args.command == "enqueue":
        enqueue(args)
    elif args.command == "run":
        if (args.seed_min is None)!=(args.seed_max is None) or (args.seed_min is not None and not 0<=args.seed_min<=args.seed_max<2**32):
            p.error('Require an inclusive unsigned 32-bit seed range')
        if not 1 <= args.batch_size <= 16:
            p.error("batch-size must be 1..16")
        run(args)
    elif args.command == "stop":
        args.root.mkdir(parents=True, exist_ok=True)
        (args.root / "STOP").touch()
        notify(args.root)
        print("Stop requested; the current stage will finish and save completed records.")
    else:
        db = connect(args.root)
        if args.command == "retry-failed":
            with db:
                db.execute("UPDATE tasks SET state='pending', error=NULL WHERE state='failed' AND error NOT LIKE '方向语义不符：%'")
            notify(args.root)
        print(json.dumps({"tasks": dict(db.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state")),
                          "shards": db.execute("SELECT number, bytes FROM shards ORDER BY number").fetchall(),
                          "recent_errors": db.execute("SELECT id, error FROM tasks WHERE state='failed' LIMIT 5").fetchall()},
                         ensure_ascii=False, indent=2))
        db.close()


if __name__ == "__main__":
    main()
