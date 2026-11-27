"""Minimal-pair controllability probe: does the generator follow one changed word?

Each group holds prompts that differ in a single attribute (forward/backward,
left/right, slow/fast, ...). Every prompt in a group is generated with the same
inference seeds, so paired motions share their noise and differ only by text.
Motions are measured geometrically (g2h.gait_checks), never by a text encoder.

Steps (each run by a person; nothing generates without an explicit count):
  plan      print the prompts, motion count and total seconds; writes nothing
  submit    queue the requests as exact-English custom jobs for --owner, at most
            five active at a time (the per-user limit). Requires --confirm equal
            to the planned motion count. Idempotent: rerun until all are queued.
  analyze   read finished motions and report per-prompt success and per-group
            paired comparisons, for the generator each request was made with.

The generator is the server's current recipe (MOTIONFARM_GENERATOR_MODEL);
request IDs carry the model, so each model is probed and reported separately.
Probe motions go to the manual-only `custom` topic like any custom request.
"""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g2h.gait_checks import analyze as gait

VERSION = 'probe-v1'
N = 8                # seeds per prompt
DURATION = 8         # seconds per motion
BASE_SEED = 7_340_000  # shared inference seeds: BASE_SEED .. BASE_SEED+N-1
ARMS = 'with arms swinging naturally at the sides.'

# group -> [(key, text, expectation)]. Expectations are evaluated by EXPECT below.
GROUPS = {
    'forward_backward': [
        ('fwd', f'A person walks forward at a steady pace for the whole clip, {ARMS}', 'travel:forward'),
        ('bwd', f'A person walks backward at a steady pace for the whole clip, {ARMS}', 'travel:backward'),
    ],
    'backward_phrasing': [
        ('reverse', f'A person walks in reverse at a steady pace for the whole clip, {ARMS}', 'travel:backward'),
        ('backpedal', f'A person backpedals at a steady pace for the whole clip, {ARMS}', 'travel:backward'),
        ('facing', 'A person walks backward, facing away from the direction of travel, '
                   'stepping heel last with each foot.', 'travel:backward'),
    ],
    'left_right': [
        ('left', 'A person side-steps to the left at a steady pace, keeping the body facing forward the whole time.', 'travel:left'),
        ('right', 'A person side-steps to the right at a steady pace, keeping the body facing forward the whole time.', 'travel:right'),
    ],
    'in_place': [
        ('place', f'A person jogs in place at a steady pace without traveling, {ARMS}', 'in_place'),
        ('travel', f'A person jogs forward at a steady pace for the whole clip, {ARMS}', 'travel:forward'),
    ],
    'speed': [
        ('slow', f'A person walks forward very slowly for the whole clip, {ARMS}', 'speed:slow'),
        ('fast', f'A person walks forward very fast for the whole clip, {ARMS}', 'speed:fast'),
    ],
    'path_shape': [
        ('straight', f'A person walks forward in a straight line at a steady pace, {ARMS}', 'straight'),
        ('circle', f'A person walks around in one full circle at a steady pace, {ARMS}', 'circle'),
    ],
    'turn': [
        ('turn_left', 'A person stands still, turns ninety degrees to the left, and then stands still again.', 'turn:left'),
        ('turn_right', 'A person stands still, turns ninety degrees to the right, and then stands still again.', 'turn:right'),
    ],
    'one_way': [
        ('one_way', 'A person sprints forward in a straight line for the whole clip, arms pumping hard.', 'straight'),
        ('return', 'A person sprints forward, turns around, and sprints back to the starting point.', 'return'),
    ],
    'stance': [
        ('wide', 'A person walks forward at a steady pace with feet wide apart and arms swinging naturally at the sides.', 'stance:wide'),
        ('narrow', 'A person walks forward at a steady pace with feet close together and arms swinging naturally at the sides.', 'stance:narrow'),
    ],
}
# Paired comparisons within a group, by seed: (group, a, b, metric, a should be larger by factor)
PAIRS = [('speed', 'fast', 'slow', 'speed_mps', 1.3), ('stance', 'wide', 'narrow', 'stance_width_ratio', 1.2)]


def prompts():
    for group, items in GROUPS.items():
        for key, text, expect in items:
            yield group, key, text, expect


def validate():
    import MotionFarm as farm
    for group, key, text, _ in prompts():
        words = len(text.split())
        if not (farm.english_surface(text) and text.startswith('A person ') and 12 <= words <= 70):
            raise ValueError(f'{group}/{key}: exact-English prompts need "A person", 12-70 words ({words})')


def model_tag():
    from g2h.generation_backend import current_recipe, KIMODO_MODEL
    return 'kimodo' if current_recipe()['model'] == KIMODO_MODEL else 'hy'


def request_id(group, key, tag):
    return f'{VERSION}-{tag}-{group}-{key}'.replace('_', '-')


def measure(record):
    """Geometric facts about one motion; text-independent."""
    m = record['motion']
    q = gait({**record, 'text': ''})['metrics']
    p = np.asarray(m['native_keypoints3d'], float); tr = np.asarray(m['native_transl'], float)
    world = p[:, :22] - p[:, :1] + tr[:, None]
    left = world[:, 1] - world[:, 2]; left[:, 1] = 0
    yaw = np.unwrap(np.arctan2(-left[:, 2], left[:, 0]))  # angle of forward = left x up; + is a left turn
    seconds = len(tr) / float(m['fps'])
    # Stance width needs gait wording to be computed by gait_checks; recompute it on its own.
    stance = gait({**record, 'text': 'A person walks.'})['metrics']['stance_width_ratio']
    return dict(observed_travel=q['observed_travel'], forward_m=q['forward_m'], left_m=q['left_m'],
                path_m=q['path_m'], straightness=q['straightness'], max_from_start_m=q['max_from_start_m'],
                speed_mps=round(q['path_m'] / seconds, 3), net_turn_deg=round(float(np.degrees(yaw[-1] - yaw[0])), 1),
                turn_range_deg=round(float(np.degrees(np.ptp(yaw))), 1), stance_width_ratio=stance, seconds=round(seconds, 2))


def expect(rule, x):
    """True/False for absolute expectations; None when the rule is only comparative."""
    kind, _, arg = rule.partition(':')
    if kind == 'travel':
        return x['observed_travel'] == arg
    if kind == 'in_place':
        return x['max_from_start_m'] <= .5
    if kind == 'straight':
        return x['path_m'] >= 1 and x['straightness'] >= .8
    if kind == 'circle':
        return abs(x['net_turn_deg']) >= 270 and x['straightness'] < .5
    if kind == 'return':
        return x['path_m'] >= 1 and x['straightness'] < .5
    if kind == 'turn':
        sign = 1 if arg == 'left' else -1
        return 45 <= sign * x['net_turn_deg'] <= 135
    return None  # speed:*, stance:* are judged in PAIRS


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0, c - h), 3), round(min(1, c + h), 3)


def plan(_args):
    validate()
    rows = list(prompts())
    for group, key, text, rule in rows:
        print(f'{group:18} {key:10} {rule:16} {text}')
    total = len(rows) * N
    print(json.dumps(dict(version=VERSION, prompts=len(rows), seeds_per_prompt=N, motions=total,
                          seconds_per_motion=DURATION, motion_minutes=round(total * DURATION / 60, 1),
                          inference_seeds=[BASE_SEED, BASE_SEED + N - 1], model=model_tag())))


def submit(args):
    validate()
    total = len(list(prompts())) * N
    if args.confirm != total:
        raise SystemExit(f'--confirm must equal the planned motion count ({total}); run plan first')
    from review_custom import Custom
    custom = Custom(args.root); tag = model_tag(); queued = 0; waiting = []
    for group, key, text, _ in prompts():
        rid = request_id(group, key, tag)
        with closing(custom.db()) as db:
            known = db.execute('SELECT 1 FROM requests WHERE id=?', (rid,)).fetchone()
            active = db.execute("SELECT count(*) FROM requests WHERE owner=? AND status IN ('queued','preparing','ready','running')",
                                (args.owner,)).fetchone()[0]
        if known:
            continue
        if active >= 5:
            waiting.append(rid); continue
        custom.submit(dict(request_id=rid, description=text, n=N, duration=DURATION, prompt_mode='exact_english',
                           seed=BASE_SEED, original_description=f'{VERSION} {group}/{key}'), args.owner, source='local_agent')
        queued += 1
    print(json.dumps(dict(model=tag, queued_now=queued, waiting_for_capacity=len(waiting),
                          note='rerun submit when earlier requests finish' if waiting else 'all requests queued')))


def motions(custom, catalog, rid):
    """(seed offset, motion_id) for a request's committed motions; read-only."""
    from g2h.farm_index import short_id
    with closing(custom.db()) as db:
        row = db.execute('SELECT seed,n,status FROM requests WHERE id=?', (rid,)).fetchone()
    manifest = custom.folder / rid / 'manifest.json'
    if not row or not manifest.exists():
        return (row['status'] if row else 'not_submitted'), []
    text_id = json.loads(manifest.read_text())['prompts'][0]['id']
    rows = catalog.execute('SELECT number,seed FROM motion_links WHERE topic=? AND text_id=? AND seed>=? AND seed<? ORDER BY seed',
                           ('custom', text_id, row['seed'], row['seed'] + row['n'])).fetchall()
    return row['status'], [(seed - row['seed'], short_id(number)) for number, seed in rows]


def analyze(args):
    from review_custom import Custom
    from g2h.farm_index import read_motion
    custom = Custom(args.root); tag = args.model or model_tag()
    measured = {}
    with closing(sqlite3.connect((args.root / 'prompts.sqlite3').as_uri() + '?mode=ro', uri=True)) as catalog:
        for group, key, text, rule in prompts():
            status, found = motions(custom, catalog, request_id(group, key, tag))
            per = {}
            for offset, mid in found:
                try:
                    per[offset] = dict(motion_id=mid, **measure(read_motion(catalog, args.root, mid)))
                except Exception as exc:  # report, do not hide
                    per[offset] = dict(motion_id=mid, error=f'{type(exc).__name__}: {exc}')
            measured[(group, key)] = (status, rule, per)
    report = dict(version=VERSION, model=tag, prompts=[], pairs=[])
    for (group, key), (status, rule, per) in measured.items():
        ok = [expect(rule, x) for x in per.values() if 'error' not in x]
        judged = [v for v in ok if v is not None]
        k = sum(judged)
        report['prompts'].append(dict(group=group, prompt=key, expectation=rule, status=status, motions=len(per),
                                      errors=sum('error' in x for x in per.values()),
                                      success=f'{k}/{len(judged)}' if judged else None, ci95=wilson(k, len(judged)) if judged else None,
                                      medians={m: float(np.median([x[m] for x in per.values() if 'error' not in x and x.get(m) is not None]))
                                               for m in ('forward_m', 'left_m', 'speed_mps', 'straightness', 'net_turn_deg', 'stance_width_ratio')
                                               if any('error' not in x and x.get(m) is not None for x in per.values())},
                                      details=list(per.values()) if args.details else None))
    for group, a, b, metric, factor in PAIRS:
        _, _, pa = measured[(group, a)]; _, _, pb = measured[(group, b)]
        both = [o for o in pa if o in pb and 'error' not in pa[o] and 'error' not in pb[o]
                and pa[o].get(metric) is not None and pb[o].get(metric) is not None]
        k = sum(pa[o][metric] >= factor * pb[o][metric] for o in both)
        report['pairs'].append(dict(group=group, comparison=f'{a}.{metric} >= {factor} x {b}.{metric} (same seed)',
                                    success=f'{k}/{len(both)}', ci95=wilson(k, len(both))))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('step', choices=['plan', 'submit', 'analyze'])
    p.add_argument('--root', type=Path)
    p.add_argument('--owner', help='account that owns the probe requests (submit)')
    p.add_argument('--confirm', type=int, help='planned motion count, to authorize submit')
    p.add_argument('--model', choices=['hy', 'kimodo'], help='analyze requests made with this generator')
    p.add_argument('--details', action='store_true', help='include per-motion measurements')
    a = p.parse_args()
    if a.step != 'plan' and not a.root:
        p.error('--root is required')
    if a.root:
        a.root = a.root.resolve()
    if a.step == 'submit' and not a.owner:
        p.error('--owner is required for submit')
    dict(plan=plan, submit=submit, analyze=analyze)[a.step](a)
