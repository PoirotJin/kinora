"""Small queue/quality tests; no model imports or GPU jobs."""
import importlib.util
import json
import os
import selectors
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from g2h.collection_store import connect


class QueueTests(unittest.TestCase):
    def command(self, root, *args):
        return [sys.executable, str(ROOT / "scripts/motion_collector.py"), "--root", str(root), *args]

    def test_enqueue_dedup_and_seed_variation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for seeds in (["1"], ["1"], ["1", "2"]):
                subprocess.run(self.command(tmp, "enqueue", "--text", "Stand still.", "--seeds", *seeds),
                               check=True, capture_output=True, timeout=10)
            db = connect(tmp)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            db.close()

    def test_idle_stop_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stop_with_signal in (False, True):
                proc = subprocess.Popen(self.command(tmp, "run", "--serve"), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(proc.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=5), "No idle startup message")
                        self.assertIn("Queue empty", proc.stdout.readline())
                    if stop_with_signal:
                        proc.terminate()
                    else:
                        subprocess.run(self.command(tmp, "stop"), check=True, capture_output=True, timeout=5)
                    proc.communicate(timeout=5)
                    self.assertEqual(proc.returncode, 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()

    def test_invalid_duration_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(self.command(tmp, "enqueue", "--text", "Walk.", "--duration", "30"),
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)

    def test_collect_record_records_scene_warnings_instead_of_rejecting(self):
        from unittest.mock import patch
        import numpy as np
        sys.path.insert(0, str(ROOT / "scripts"))
        import motion_collector
        from g2h import generation_backend, human_interaction, human_rig

        rest, parents, _ = human_rig.definition()
        scene = {
            "kind": human_interaction.VERSION,
            "objects": [{"id": "box", "center_m": [0, 0.5, 2], "size_m": [1, 1, 1], "heading_deg": 0}],
            "waypoints": [{"frame": 0, "xz_m": [0, 0], "heading_deg": 0}],
            "keyframes": [{"frame": 0, "object_id": "box", "heading_deg": 0,
                           "pelvis_offset_m": [0, 0.5, -2], "body_rotvecs": [[0.0, 0.0, 0.0]] * 21}],
            "contacts": [],
        }
        frames = 30
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "motion.npz"
            np.savez(
                path,
                fps=np.array(30),
                native_transl=np.zeros((frames, 3), dtype=np.float32),
                native_keypoints3d=np.broadcast_to(rest, (frames, 52, 3)).astype(np.float32),
                native_root_rotations_mat=np.broadcast_to(np.eye(3), (frames, 3, 3)).astype(np.float32),
                scene_spec_json=human_interaction.encode(scene),
            )
            effective = human_interaction.decode(human_interaction.encode(scene), frames)
            meta = {
                "scene": {"passed": False, "failures": ["接触偏差"]},
                "scene_request_sha256": motion_collector.digest(scene),
                "scene_conditioning_sha256": motion_collector.digest(effective),
                "generator_name": generation_backend.KIMODO_MODEL,
                "conversion": {"adapter_version": generation_backend.KIMODO_ADAPTER},
                "generation_profile": {"config_sha256": "fixture"},
                "generator_checkpoint": {"bytes": 1},
                "feature": {},
            }
            job = {"id": "j1", "motion": str(path), "features": str(Path(tmp) / "f.pt"),
                   "spec": {"text": "A person touches a box.", "duration_s": 1, "seed": 1,
                            "scene": scene,
                            "model": generation_backend.KIMODO_MODEL,
                            "cfg_scale": 2.0,
                            "constraint_cfg_scale": 2.0,
                            "diffusion_steps": 100,
                            "post_processing": True,
                            "adapter_version": generation_backend.KIMODO_ADAPTER}}
            with patch("motion_collector.read_metadata", return_value=meta):
                record = motion_collector.collect_record(job)
            self.assertEqual(record["quality"]["quality_warnings"], ["接触偏差"])
            self.assertFalse(record["quality"]["human_interaction"]["passed"])

    def test_direction_semantics_custom_retries_once_then_accepts_while_batch_stays_strict(self):
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "scripts"))
        import motion_collector

        with patch("g2h.semantic_queue.reject") as mock_reject:
            # Attempt 1 custom without scene -> retry with max_attempts=2
            r1 = {"quality": {"direction_semantics": {"status": "fail", "reasons": ["要求后退"], "warnings": []}}}
            j1 = {"id": "j1", "spec": {"topic": "custom", "semantic_attempt": 1}}
            self.assertFalse(motion_collector._handle_direction_semantics("store", j1, r1))
            mock_reject.assert_called_once_with("store", j1, r1["quality"]["direction_semantics"], max_attempts=2)

            # Attempt 2 custom -> downgraded to review & accepted
            mock_reject.reset_mock()
            r2 = {"quality": {"direction_semantics": {"status": "fail", "reasons": ["要求后退"], "warnings": []}}}
            j2 = {"id": "j2", "spec": {"topic": "custom", "semantic_attempt": 2}}
            self.assertTrue(motion_collector._handle_direction_semantics("store", j2, r2))
            mock_reject.assert_not_called()
            self.assertEqual(r2["quality"]["direction_semantics"]["status"], "review")
            self.assertEqual(r2["quality"]["direction_semantics"]["reasons"], [])
            self.assertEqual(r2["quality"]["direction_semantics"]["warnings"], ["要求后退"])
            self.assertTrue(r2["quality"]["direction_unverified"])

            # Non-custom batch topic -> strict reject()
            r3 = {"quality": {"direction_semantics": {"status": "fail", "reasons": ["要求后退"], "warnings": []}}}
            j3 = {"id": "j3", "spec": {"topic": "walking", "semantic_attempt": 1}}
            self.assertFalse(motion_collector._handle_direction_semantics("store", j3, r3))
            mock_reject.assert_called_once_with("store", j3, r3["quality"]["direction_semantics"])

    def test_collect_record_accepts_attachment_coupled_co_carry_and_blocks_tampering(self):
        from unittest.mock import patch
        import copy
        import numpy as np
        sys.path.insert(0, str(ROOT / "scripts"))
        import motion_collector
        from g2h import generation_backend, human_interaction, human_rig, kimodo_adapter, multi_actor, object_response

        rest, parents, _ = human_rig.definition()
        payload = {
            "duration": 4,
            "objects": [{
                "id": "crate-box",
                "center_m": [0.0, 0.275, 0.0],
                "size_m": [0.65, 0.55, 0.50],
                "heading_deg": 0.0,
            }],
            "actors": [
                {"id": "r1", "start_xz_m": [-0.1, -1.2], "heading_deg": 0.0},
                {"id": "r2", "start_xz_m": [0.1, 1.2], "heading_deg": 180.0},
            ],
            "interactions": [{
                "kind": "co_carry",
                "actors": ["r1", "r2"],
                "object_id": "crate-box",
                "size_m": [0.65, 0.55, 0.50],
                "to_xz_m": [1.0, 0.4],
                "lift_m": 0.32,
                "start_frame": 25,
                "end_frame": 95,
            }],
        }
        plans = multi_actor.build_multi_plans(payload)
        plan = plans[0]
        requested = plan["scene"]
        frames = 120
        local = np.broadcast_to(np.eye(3), (frames, 52, 3, 3)).copy()
        points, _ = kimodo_adapter.fk(local, rest, parents)
        transl = np.column_stack((np.zeros(frames), np.zeros(frames), np.linspace(0.0, 1.0, frames))).astype(np.float32)
        arrays = {
            "fps": np.array(30),
            "native_transl": transl,
            "native_keypoints3d": points.astype(np.float32),
            "native_root_rotations_mat": local[:, 0].astype(np.float32),
            "smpl_poses": np.zeros((frames, 156), dtype=np.float32),
            "smpl_trans": transl.copy(),
            "smpl_Rh": np.zeros((frames, 3), dtype=np.float32),
            "native_rot6d": local[..., :2].reshape(frames, 52, 6).astype(np.float32),
        }
        updated_arrays, effective, audit = object_response.couple_grasped_objects(arrays, requested, rest, parents)
        self.assertGreater(len(effective["objects"][0].get("trajectory", [])), 1)
        self.assertNotEqual(effective["keyframes"][-1]["pelvis_offset_m"], requested["keyframes"][-1]["pelvis_offset_m"])

        spec_base = {
            "text": plan["text"],
            "duration_s": 4,
            "seed": 1,
            "scene": requested,
            "model": generation_backend.KIMODO_MODEL,
            "cfg_scale": 2.0,
            "constraint_cfg_scale": 2.0,
            "diffusion_steps": 100,
            "post_processing": True,
            "adapter_version": generation_backend.KIMODO_ADAPTER,
        }

        def run_collect(req_scene, eff_arrays):
            eff_decoded = human_interaction.decode(eff_arrays["scene_spec_json"], frames)
            meta = {
                "scene": {"passed": True, "object_response": audit},
                "scene_request_sha256": motion_collector.digest(req_scene),
                "scene_conditioning_sha256": motion_collector.digest(eff_decoded),
                "generator_name": generation_backend.KIMODO_MODEL,
                "conversion": {"adapter_version": generation_backend.KIMODO_ADAPTER},
                "generation_profile": {"config_sha256": "fixture"},
                "generator_checkpoint": {"bytes": 1},
                "feature": {},
            }
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "motion.npz"
                np.savez(path, **eff_arrays)
                job = {"id": "j-co-carry", "motion": str(path), "features": str(Path(tmp) / "f.pt"),
                       "spec": dict(spec_base, scene=req_scene)}
                with patch("motion_collector.read_metadata", return_value=meta):
                    return motion_collector.collect_record(job)

        record = run_collect(requested, updated_arrays)
        self.assertTrue(record["quality"]["human_interaction"]["passed"])

        # (a) Non-attached object center_m/trajectory change must be rejected.
        req_unattached = copy.deepcopy(requested)
        req_unattached.pop("attachments", None)
        req_unattached.pop("carry_grip", None)
        req_unattached["objects"].append({"id": "table", "center_m": [2.0, 0.5, 0.0], "size_m": [1.0, 1.0, 1.0], "heading_deg": 0.0})
        eff_unattached = copy.deepcopy(req_unattached)
        eff_unattached["objects"][1]["center_m"] = [2.5, 0.5, 0.0]
        self.assertFalse(motion_collector._scene_conditioning_matches(req_unattached, eff_unattached))
        with self.assertRaisesRegex(ValueError, "场景生成结果缺少匹配的验收记录"):
            run_collect(req_unattached, dict(updated_arrays, scene_spec_json=human_interaction.encode(eff_unattached)))

        # (b) Attached object size_m or heading_deg change must be rejected.
        req_b = copy.deepcopy(requested)
        req_b["keyframes"] = []
        eff_b = copy.deepcopy(effective)
        eff_b["keyframes"] = []
        for field, val in (("size_m", [0.65, 0.70, 0.50]), ("heading_deg", 25.0)):
            eff_bad_geom = copy.deepcopy(eff_b)
            eff_bad_geom["objects"][0][field] = val
            self.assertFalse(motion_collector._scene_conditioning_matches(req_b, eff_bad_geom))
            with self.assertRaisesRegex(ValueError, "场景生成结果缺少匹配的验收记录"):
                run_collect(req_b, dict(updated_arrays, scene_spec_json=human_interaction.encode(eff_bad_geom)))

        # (c) Keyframe pelvis_offset_m change without attachments must be rejected.
        req_no_att = copy.deepcopy(requested)
        req_no_att.pop("attachments", None)
        eff_no_att = copy.deepcopy(req_no_att)
        eff_no_att["keyframes"][0]["pelvis_offset_m"] = [
            eff_no_att["keyframes"][0]["pelvis_offset_m"][0],
            eff_no_att["keyframes"][0]["pelvis_offset_m"][1] + 0.05,
            eff_no_att["keyframes"][0]["pelvis_offset_m"][2],
        ]
        with self.assertRaisesRegex(ValueError, "场景生成结果缺少匹配的验收记录"):
            run_collect(req_no_att, dict(updated_arrays, scene_spec_json=human_interaction.encode(eff_no_att)))


if __name__ == "__main__":
    unittest.main()
