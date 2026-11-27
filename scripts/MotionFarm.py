"""Qwen prompt expansion and per-topic queues for the existing motion collector."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from g2h.collection_store import connect, task_id
from g2h.generation_backend import current_recipe, manifest_recipe, validate_spec, KIMODO_MODEL, KIMODO_MAX_SECONDS
from g2h.api_errors import NoRedirect, TemporaryAPIError, http_failure

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT_POLICY_VERSION = "english_motion_v2"
SYSTEM = """You write motion descriptions for a single-person text-to-motion model.
OUTPUT CONTRACT
Return only one JSON object with exactly this schema:
{"prompts":[{"topic_id":"...","text":"A person ...",
"attributes":{"direction":"...","speed":"...","style":"..."}}]}.
All keys and string values must be in English. Standard English dance terms such
as 'plie' or 'plié' are allowed; do not provide translations into other languages.
Input topic definitions may be Chinese: understand them, but never output Chinese,
translations, explanations, Markdown, scores, or additional fields.
Generate exactly count_per_topic descriptions for EACH supplied topic_id.
Copy topic IDs exactly. Respect their supplied everyday definitions; examples are
illustrative, not an exhaustive list. Do not mix unrelated primary topics.

MOTION DESCRIPTION RULES
Each text must start with 'A person ' and contain 12-45 English words in 1-2 sentences.
Describe observable physical movement: the main action plus the relevant direction,
trajectory, pace, rhythm, or limb movement. State left/right relative to the person.
Never describe positions in a frame, screen or camera view. For example, use
'diagonally forward and to their right', not 'toward the upper right corner'.
Most descriptions should contain ONE primary action. A minority may combine at most
TWO naturally connected actions that comfortably fit duration_s. Avoid long stories.
Do not force every clip to start from standing, accelerate, turn, slow down, or stop.
Continuous movement throughout a clip is valid. Include starts, stops and transitions
only as deliberate variants. Do not specify exact frame numbers or joint coordinates.
Use ordinary plausible human movement on flat ground. Describe one person only,
without requiring objects, partners, stairs, obstacles, or special scene geometry.
Exclude clothing, appearance, lighting, camera instructions, vague emotions,
and subjective quality claims such as 'perfect', 'beautiful', or 'high quality'.
For dancing, a dance name alone is insufficient: describe recognizable footwork,
rhythm and relevant arm or torso motion. Solo versions of partner dances are valid.
If attributes.style names a dance genre, explicitly name it in text too. Avoid
inventing technical beat rules; plain physical descriptions are preferable.
For walking, turns in place are allowed under the supplied topic definition.
Keep walking distinct from running: do not describe it as jogging-like or sprinting.
For running, describe a running gait and its path clearly; do not substitute walking.

DIVERSITY RULES
Cover distinct movement patterns across each topic's batch and recent history.
Vary direction/path, pace, step size or amplitude, rhythm and subtype where meaningful.
For dancing vary dance styles as well as concrete steps. Do not merely paraphrase
the same action, and do not make every prompt a forward movement or a stop sequence.
Treat the supplied recent prompts as examples to avoid repeating, not templates.
Attribute values must faithfully summarize what text explicitly describes; never
invent a dance style or other property absent from the description.
Before responding, check language, schema, quotas, topic fit, duration plausibility,
physical consistency, and actual action diversity. Output the JSON only."""


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def english_surface(text):
    # A script/format guard, not a semantic language classifier. Allow English
    # loanwords with Latin accents and ordinary typographic punctuation.
    return isinstance(text, str) and bool(text.strip()) and all(
        " " <= c <= "~" or c in "‘’“”–—…" or
        (c.isalpha() and unicodedata.name(c, "").startswith("LATIN "))
        for c in unicodedata.normalize("NFC", text))


def load_topics(path, selected=None):
    topics = json.loads(path.read_text(encoding="utf-8"))["topics"]
    ids = set()
    for topic in topics:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", topic["id"]) or topic["id"] in ids:
            raise ValueError("Topic IDs must be unique safe lowercase identifiers")
        ids.add(topic["id"])
        for key in ("name", "definition"):
            if not isinstance(topic[key], str) or not topic[key].strip():
                raise ValueError("Topic name/definition must be nonempty text")
    if selected and not set(selected) <= ids:
        raise ValueError("Unknown selected topic")
    return [t for t in topics if not selected or t["id"] in selected]


def catalog(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "prompts.sqlite3", timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, topic TEXT NOT NULL, normalized TEXT NOT NULL, item TEXT NOT NULL, UNIQUE(topic, normalized))")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    from g2h import farm_index, semantic_index
    farm_index.initialize(db)
    semantic_index.initialize(db)
    return db


def save_json(path, value):
    # Unique files and exclusive creation prevent accidental result overwrites.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def validate_prompts(content, topics, count):
    value = json.loads(content)
    if not isinstance(value, dict) or set(value) != {"prompts"}:
        raise ValueError("Qwen must return only a prompts object")
    prompts = value.get("prompts")
    expected = {t["id"]: 0 for t in topics}
    if not isinstance(prompts, list) or len(prompts) != len(topics) * count:
        raise ValueError("Qwen returned an unexpected prompt count")
    seen = set()
    for item in prompts:
        if not isinstance(item, dict) or item.get("topic_id") not in expected:
            raise ValueError("Qwen returned an unknown topic")
        if set(item) != {"topic_id", "text", "attributes"}:
            raise ValueError("Qwen returned extra/missing fields; translations are not accepted")
        text = item.get("text")
        if not english_surface(text):
            raise ValueError("Motion text must use English/Latin text, with no Chinese")
        item["text"] = text.strip()
        if not item["text"].startswith("A person ") or not 12 <= len(item["text"].split()) <= 45:
            raise ValueError("Motion text must start with 'A person ' and contain 12-45 words")
        if re.search(r"\b(frame|screen|camera)\b|\b(upper|lower)[ -](left|right)\b", text, re.I):
            raise ValueError("Describe body-relative motion, not camera/frame coordinates")
        if item["topic_id"] == "walking" and re.search(r"\b(jog\w*|sprint\w*|running)\b", text, re.I):
            raise ValueError("Walking prompts must not mix in running gaits")
        attributes = item.get("attributes")
        if not isinstance(attributes, dict) or set(attributes) != {"direction", "speed", "style"} or any(
            not isinstance(attributes.get(k), str) or not 1 <= len(attributes[k]) <= 200
            or not english_surface(attributes[k])
            for k in ("direction", "speed", "style")
        ):
            raise ValueError("Qwen returned invalid action attributes")
        key = (item["topic_id"], normalized(item["text"]))
        if key in seen:
            raise ValueError("Qwen repeated a prompt within the response")
        seen.add(key)
        expected[item["topic_id"]] += 1
    if any(n != count for n in expected.values()):
        raise ValueError("Qwen did not respect per-topic quotas")
    return prompts


def call_qwen(model, key_file, messages, temperature=0.9, max_tokens=12000):
    key = key_file.read_text(encoding="utf-8").strip()
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("API key file must contain one nonempty ASCII token")
    body = {"model": model, "messages": messages, "enable_thinking": False,
            "response_format": {"type": "json_object"}, "max_tokens": max_tokens,
            "temperature": temperature}
    request = urllib.request.Request(BASE_URL + "/chat/completions",
        data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key,
                                               "Content-Type": "application/json"})
    from g2h.api_usage import budgeted_request,attributed_call
    with attributed_call('prompt_generation','dashscope'), budgeted_request(model, 'chat', messages, max_tokens):
        # The production supervisor owns retries; uncertain calls may have been billed.
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("API response exceeded the size limit")
            result=json.loads(raw)
            from g2h.api_usage import record
            record(model,'chat',result)
            return result
        except urllib.error.HTTPError as exc:
            from g2h.api_usage import record
            record(model,'chat',status='http_'+str(exc.code))
            raise http_failure(exc, 'Qwen') from None
        except urllib.error.URLError:
            from g2h.api_usage import record
            record(model,'chat',status='network_error')
            raise TemporaryAPIError("Qwen network failure") from None
        except (TimeoutError, ConnectionError):
            from g2h.api_usage import record
            record(model,'chat',status='transport_error')
            raise TemporaryAPIError("Qwen request timed out or connection interrupted") from None
        except ValueError:
            from g2h.api_usage import record
            record(model,'chat',status='invalid_response')
            raise


def generate(args, topics):
    root = args.root
    db = catalog(root)
    try:
        with (root / "prompt_generation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for _ in range(args.rounds):
                recent = {t["id"]: [json.loads(r[0])["text"] for r in db.execute(
                    "SELECT item FROM prompts WHERE topic=? ORDER BY rowid DESC LIMIT 12", (t["id"],))]
                    for t in topics}
                messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
                    json.dumps({"topics": topics, "count_per_topic": args.per_topic,
                                "duration_s": args.duration, "recent_prompts_to_avoid": recent}, ensure_ascii=False)}]
                run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:12]
                run = root / "prompts" / run_id
                run.mkdir(parents=True)
                request_info = {"model": args.model, "base_url": BASE_URL, "messages": messages,
                                "duration_s": args.duration, "count_per_topic": args.per_topic,
                                "topics": topics, "enable_thinking": False,
                                "prompt_policy_version": PROMPT_POLICY_VERSION}
                save_json(run / "request.json", request_info)
                response = call_qwen(args.model, args.key_file, messages)
                save_json(run / "response.json", response)
                choice = response["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise ValueError("Incomplete Qwen response; archived but not enqueued")
                prompts = validate_prompts(choice["message"]["content"], topics, args.per_topic)
                accepted = []
                for item in prompts:
                    topic_id = item["topic_id"]
                    if db.execute("SELECT 1 FROM prompts WHERE topic=? AND normalized=?",
                                  (topic_id, normalized(item["text"]))).fetchone():
                        continue
                    item = {**item, "duration_s": args.duration,
                            "id": task_id({"topic": topic_id, "text": normalized(item["text"])}),
                            "source": {"model": response.get("model", args.model), "run_id": run_id,
                                       "prompt_policy_version": PROMPT_POLICY_VERSION,
                                       "request_id": response.get("id"), "topic": next(t for t in topics if t["id"] == topic_id)}}
                    accepted.append(item)
                # Durable manifest first: enqueue can recover it after interruption.
                manifest = run / "manifest.json"
                save_json(manifest, {"schema_version": 1, "generation": current_recipe(), "prompts": accepted})
                with db:
                    for item in accepted:
                        db.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, ?, ?)",
                            (item["id"], item["topic_id"], normalized(item["text"]), json.dumps(item, ensure_ascii=False)))
                print(json.dumps({"manifest": str(manifest), "accepted": len(accepted),
                                  "duplicates_skipped": len(prompts) - len(accepted), "usage": response.get("usage")}, ensure_ascii=False), flush=True)
                if args.enqueue:
                    enqueue_manifest(args.root, manifest, args.seeds)
    finally:
        db.close()


def enqueue_manifest(root, manifest, seeds):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("prompts"), list):
        raise ValueError("Invalid farm manifest")
    recipe = manifest_recipe(data)
    jobs = []
    for item in data["prompts"]:
        topic = item["topic_id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", topic):
            raise ValueError("Unsafe topic ID")
        if not isinstance(item["text"], str) or not 1 <= len(item["text"].strip()) <= 4000:
            raise ValueError("Invalid prompt")
        duration = float(item["duration_s"])
        limit = KIMODO_MAX_SECONDS if recipe["model"] == KIMODO_MODEL else 12
        if not 1 <= duration <= limit:
            raise ValueError("Duration must be 1..%d seconds" % limit)
        for seed in seeds:
            if type(seed) is not int or not 0 <= seed < 2**32:
                raise ValueError("Seeds must be unsigned 32-bit integers")
            spec = {"text": item["text"].strip(), "topic": topic, "duration_s": duration,
                    "seed": seed, **recipe,
                    "representation": "human_smplh_y_up_v1"}
            if item.get('segments'):
                spec['segments'] = [{'text': s['text'].strip(), 'frames': s['frames']} for s in item['segments']]
            if 'scene' in item:
                spec['scene'] = item['scene']
            if 'inference_seed_offset' in item:
                effective=seed+item['inference_seed_offset']
                if type(effective) is not int or not 0<=effective<2**32:raise ValueError('Invalid inference seed')
                spec['inference_seed']=effective
            validate_spec(spec)
            identity = task_id(spec)
            spec["farm"] = {"prompt_id": item["id"],
                            "attributes": item["attributes"], "source": item["source"],
                            "label_status": "intended_unverified", "manifest": str(manifest.resolve())}
            # Existing manifests remain replayable; new Qwen responses have no translation.
            if "text_zh" in item:
                spec["farm"]["text_zh"] = item["text_zh"]
            jobs.append((topic, identity, spec))
    # Register stable short IDs centrally before queuing. Interrupted enqueue can
    # be replayed; an unqueued link is explicitly reported as not_enqueued.
    from g2h.farm_index import register_motion
    index = catalog(root)
    try:
        with index:
            for item in data["prompts"]:
                existing = index.execute("SELECT topic,normalized FROM prompts WHERE id=?", (item["id"],)).fetchone()
                catalog_key = normalized(item["text"])
                if 'scene' in item:
                    catalog_key += '\x1fscene:' + item['id']
                expected = (item["topic_id"], catalog_key)
                if existing and existing != expected:
                    raise ValueError("Text ID conflicts with an existing prompt")
                index.execute("INSERT OR IGNORE INTO prompts VALUES(?,?,?,?)",
                    (item["id"], *expected, json.dumps(item, ensure_ascii=False)))
        for topic, identity, spec in jobs:
            spec["farm"]["motion_id"] = register_motion(index,spec["farm"]["prompt_id"],topic,identity,spec["seed"])
    finally:
        index.close()
    counts = {}
    # Validate the entire manifest before any enqueue writes. Replay is idempotent.
    for topic in sorted({j[0] for j in jobs}):
        queue = root / "topics" / topic
        db = connect(queue)
        try:
            before = db.total_changes
            with db:
                for _, identity, spec in (j for j in jobs if j[0] == topic):
                    db.execute("INSERT OR IGNORE INTO tasks(id,spec) VALUES (?,?)",
                               (identity, json.dumps(spec, ensure_ascii=False)))
            counts[topic] = db.total_changes - before
        finally:
            db.close()
    print(json.dumps({"enqueued": counts, "requested": len(jobs)}, ensure_ascii=False), flush=True)


def status(root, topics):
    output = {}
    for topic in topics:
        path = root / "topics" / topic["id"] / "index.sqlite3"
        if not path.exists():
            output[topic["id"]] = {"tasks": {}, "saved_records": 0}
            continue
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            output[topic["id"]] = {"tasks": dict(db.execute("SELECT state,COUNT(*) FROM tasks GROUP BY state")),
                "saved_records": db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                "recent_errors": db.execute("SELECT id,error FROM tasks WHERE state='failed' LIMIT 3").fetchall()}
        finally:
            db.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))


def run_collectors(args, topics):
    args.root.mkdir(parents=True, exist_ok=True)
    # One GPU worker across ALL topics. Finite queue drain, no background watcher.
    with (args.root / "gpu_worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for topic in topics:
            queue = args.root / "topics" / topic["id"]
            if not (queue / "index.sqlite3").exists():
                continue
            command = ["bash", str(PROJECT / "python3"),
                       str(PROJECT / "scripts/motion_collector.py"), "--root", str(queue),
                       "run", "--batch-size", str(args.batch_size),
                       "--stage-timeout", str(args.stage_timeout)]
            print("Collecting topic: " + topic["id"], flush=True)
            subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / "outputs/text_motion_farm")
    parser.add_argument("--config", type=Path, default=PROJECT / "data/farm/topics.json")
    parser.add_argument("--topic", dest="topics", action="append", help="Select a topic before the subcommand; repeat for more topics")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="Generate and validate prompts; no GPU usage")
    gen.add_argument("--model", default="qwen3.8-flash")
    gen.add_argument("--key-file", type=Path, default=Path.home() / ".config/gen2humanoid/dashscope_api_key")
    gen.add_argument("--per-topic", type=int, default=3)
    gen.add_argument("--rounds", type=int, default=1)
    gen.add_argument("--duration", type=float, default=8)
    gen.add_argument("--enqueue", action="store_true")
    gen.add_argument("--seeds", type=int, nargs="+", default=[42, 43])
    enqueue = sub.add_parser("enqueue", help="Idempotently enqueue a saved manifest")
    enqueue.add_argument("--manifest", required=True, type=Path)
    enqueue.add_argument("--seeds", type=int, nargs="+", default=[42, 43])
    sub.add_parser("status")
    run = sub.add_parser("run", help="Drain selected queues serially using the existing collector")
    run.add_argument("--batch-size", type=int, default=2)
    run.add_argument("--stage-timeout", type=int, default=1800)
    import catalog_cli
    catalog_cli.add_commands(sub)
    args = parser.parse_args()
    args.root = args.root.resolve()
    topics = load_topics(args.config, args.topics)
    if not topics:
        parser.error("At least one topic is required")
    if args.command in catalog_cli.COMMANDS:
        catalog_cli.execute(args, topics)
        return
    if hasattr(args, "seeds") and any(not 0 <= s < 2**32 for s in args.seeds):
        parser.error("Seeds must be unsigned 32-bit integers")
    if args.command == "generate":
        if not 1 <= args.per_topic <= 20 or not 1 <= args.rounds <= 1000 or not 1 <= args.duration <= 12:
            parser.error("Require per-topic 1..20, rounds 1..1000, duration 1..12")
        if args.per_topic * len(topics) > 30:
            parser.error("At most 30 prompts per API request; reduce count or select fewer topics")
        generate(args, topics)
    elif args.command == "enqueue":
        enqueue_manifest(args.root, args.manifest, args.seeds)
    elif args.command == "status":
        status(args.root, topics)
    else:
        if not 1 <= args.batch_size <= 16 or not 30 <= args.stage_timeout <= 1800:
            parser.error("Require batch-size 1..16 and stage-timeout 30..1800")
        run_collectors(args, topics)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("Farm stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
