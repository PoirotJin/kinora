"""Text-aware gait screening: travel direction and stance width, Y-up metres.

Body axes come from the skeleton itself: the body's left is the horizontal
left-hip minus right-hip vector, and forward is left x up, which holds for
SMPL-family skeletons (left hip at +X when facing +Z). This avoids relying on
an unverified root-rotation convention; when the hips are degenerate the root
rotation (+Z forward) is used instead and the result says so.

These are screening proxies for human review. Thresholds are explicit, are not
calibrated against labelled motions, and never accept, reject or modify a motion.
"""
import re
import numpy as np
from g2h import motion_vocab as vocab

VERSION = 'gait-v3'
# A clip "travels" in a direction when that signed displacement is at least
# MIN_TRAVEL_M and makes up at least DOMINANT of the root path length.
MIN_TRAVEL_M = .25
DOMINANT = .5
# Stance width is the lateral distance between the feet while they support the
# body, as a fraction of hip-joint width. Ordinary walking in motion-capture
# data is typically around 0.7-1.1 of hip-joint width; below NARROW is flagged.
NARROW_RATIO = .6
CONTACT_M = .045
# One-way travel ("sprints forward") should not double back: net displacement over
# path length below STRAIGHTNESS on a path of at least PATH_CHECK_M is flagged.
STRAIGHTNESS = .5
PATH_CHECK_M = 1.0
# "In place" allows drift; flagged when the root ends up further than this from the start.
IN_PLACE_M = .5

# Directional phrasing comes from the shared vocabulary in g2h.motion_vocab.
_LATERAL = r'\b(?:side[- ]?step\w*|sideways|lateral\w*|shuffl\w*|sidle\w*|crab[- ]?walk\w*)\b'
_SIDE = {'left': r'\b(?:to (?:the|his|her|their) left|leftward[s]?)\b', 'right': r'\b(?:to (?:the|his|her|their) right|rightward[s]?)\b'}
# Intentionally narrow or crossing footwork is not a defect.
_NARROW_OK = r'\b(?:cross[- ]?(?:over|step\w*|ing)|crossover|catwalk|runway|tightrope|balance beam|heel[- ]to[- ]toe|tandem|one[- ](?:leg|foot)|single[- ]leg|feet together|together|kick\w*|pivot\w*|spin\w*|twirl\w*|grapevine|ballet|dance\w*|dancing|hop\w*|jump\w*|leap\w*|lunge\w*|squat\w*|kneel\w*)\b'
 # A stationary *starting* state ("from a stationary crouch", "from a standstill")
# describes where the motion begins, not a request to stay put.
_START_STATE = r'\b(?:from|out of|starting (?:from|in|at)|begins? (?:from|in|at)|after)\s+(?:a|an|the|his|her|their)?\s*(?:\w+\s+)?(?:stationary|standstill|still|standing|resting)\b(?:\s+\w+)?'
# Coming to rest at the end ("... and stands still") is a final state, not in place.
_END_STATE = r'\b(?:and|then|before|until)\s+(?:\w+\s+)?(?:stands?|standing|stops?|stopping|remains?|stays?|comes? to (?:a )?)\s*(?:still|stationary|motionless|rest|a stop|standstill)\b'
_IN_PLACE = r'\b(?:in[ -]place|on the spot|stationary|motionless|stands? still|standing still|stays? (?:still|put)|remain\w* (?:still|in place)|without (?:moving|travel\w*) (?:forward|away))\b'
_NEGATED_IN_PLACE = r'\b(?:not|without|no)\b(?:\W+\w+){0,3}\W+(?:in place|on the spot)\b'
# Wording that allows the path to bend or come back.
_PATH_FREE = r'\b(?:turn\w*|return\w*|comes? back|back to|back and forth|to and fro|around|circl\w*|circle|loop\w*|zig\w*|weav\w*|pac(?:e|es|ing)|u-turn|revers\w*|figure|curv\w*|arc\w*|spiral\w*|wander\w*|then)\b'
_GAIT = vocab.GAIT + r'|\b(?:step\w*|stand\w*|stood)\b|' + vocab.CHINESE_GAIT


def in_place_requested(text):
    """True when the text asks to stay on the spot, not merely to start from rest."""
    text = re.sub(_NEGATED_IN_PLACE, ' ', re.sub(_END_STATE, ' ', re.sub(_START_STATE, ' ', (text or '').lower())))
    return bool(re.search(_IN_PLACE, text))


def travel_intent(text):
    """'backward' / 'forward' / 'left' / 'right', 'in_place', 'mixed' for
    sequences or conflicting requests, or None."""
    text = original = (text or '').lower()
    # "without moving forward", "not stepping backward" do not request that direction.
    text = re.sub(r'\b(?:not|without|never|no)\b(?:\W+\w+){0,2}?\W+(?:forward|backward)s?\b', ' ', text)
    found = {d for d in ('forward', 'backward') if vocab.requests(text, d)}
    if re.search(_LATERAL, text):
        found |= {d for d, p in _SIDE.items() if re.search(p, text)}
    found |= {d for d in ('left', 'right') if re.search(vocab.CHINESE[d], text)}
    if in_place_requested(original):
        found.add('in_place')
    if len(found) > 1:
        return 'mixed'  # e.g. "forward, then backward": needs segment-level review
    return next(iter(found), None)


def _horizontal_unit(v):
    v = v.copy(); v[..., 1] = 0
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return np.divide(v, n, out=np.zeros_like(v), where=n > 1e-9), n[..., 0]


def analyze(record, ground=False):
    m = record['motion']
    p = np.asarray(m['native_keypoints3d'], float); tr = np.asarray(m['native_transl'], float); fps = float(m['fps'])
    if p.ndim != 3 or p.shape[1] < 22 or len(p) < 2 or tr.shape != (len(p), 3) or not np.isfinite(fps) or fps <= 0:
        raise ValueError('Invalid motion for gait checks')
    text = record.get('text', '')
    world = p[:, :22] - p[:, :1] + tr[:, None]
    left, hip_width = _horizontal_unit(world[:, 1] - world[:, 2])
    axes = 'skeleton_hips'
    if np.median(hip_width) < .02:
        rot = np.asarray(m.get('native_root_rotations_mat', np.tile(np.eye(3), (len(p), 1, 1))), float)
        left, _ = _horizontal_unit(rot[:, :, 0]); axes = 'root_rotation_assumed_+Z_forward'
    forward = np.stack([-left[:, 2], np.zeros(len(p)), left[:, 0]], axis=1)  # left x up

    vel = np.diff(tr, axis=0) * fps; vel[:, 1] = 0
    step = np.linalg.norm(vel, axis=1) / fps
    path = float(step.sum())
    fwd = float(np.sum(vel * forward[:-1], axis=1).sum() / fps)
    lat = float(np.sum(vel * left[:-1], axis=1).sum() / fps)
    intent = travel_intent(text)
    observed = None
    if path >= MIN_TRAVEL_M:
        candidates = {'forward': fwd, 'backward': -fwd, 'left': lat, 'right': -lat}
        best = max(candidates, key=candidates.get)
        if candidates[best] >= MIN_TRAVEL_M and candidates[best] >= DOMINANT * path:
            observed = best
    flags = []
    direction_ok = None
    if not ground and intent in ('forward', 'backward', 'left', 'right'):
        opposite = {'forward': 'backward', 'backward': 'forward', 'left': 'right', 'right': 'left'}[intent]
        direction_ok = observed != opposite
        if not direction_ok:
            flags.append('possible_direction_mismatch')
    net = float(np.linalg.norm((tr[-1] - tr[0])[[0, 2]]))
    straightness = net / path if path > 1e-9 else 1.0
    path_ok = None
    if not ground and intent in ('forward', 'backward', 'left', 'right') and not re.search(_PATH_FREE, text.lower()) and path >= PATH_CHECK_M:
        path_ok = straightness >= STRAIGHTNESS
        if not path_ok:
            flags.append('possible_path_reversal')
    reach = float(np.linalg.norm((tr - tr[0])[:, [0, 2]], axis=1).max())
    in_place_ok = None
    if not ground and intent == 'in_place':
        in_place_ok = reach <= IN_PLACE_M
        if not in_place_ok:
            flags.append('possible_in_place_travel')

    # Stance width: each foot's lateral offset from the pelvis while it supports.
    feet = world[:, [7, 10, 8, 11]]
    floor = float(np.percentile(feet[:, :, 1].min(axis=1), 5))
    leg = float(np.median(np.linalg.norm(world[:, 4] - world[:, 7], axis=1)))
    scale = leg / .42 if .15 <= leg <= .8 else 1.0
    low = feet[:, :, 1] - floor < CONTACT_M * scale
    support = np.stack([low[:, 0] | low[:, 1], low[:, 2] | low[:, 3]], axis=1)
    pelvis = world[:, 0]
    off_left = np.sum((world[:, 7] - pelvis) * left, axis=1)
    off_right = np.sum((pelvis - world[:, 8]) * left, axis=1)
    hips = float(np.median(hip_width))
    width = ratio = None
    crossed = False
    stance_ok = None
    upright_gait = re.search(_GAIT, text.lower()) and not re.search(_NARROW_OK, text.lower())
    if support[:, 0].sum() >= 3 and support[:, 1].sum() >= 3 and hips >= .02:
        a, b = float(np.median(off_left[support[:, 0]])), float(np.median(off_right[support[:, 1]]))
        width = a + b; ratio = width / hips
        both = support.all(axis=1)
        # Feet crossing the body's midline while both support the body.
        separation = np.sum((world[both, 7] - world[both, 8]) * left[both], axis=1)
        crossed = bool(both.sum() >= 3 and np.mean(separation < 0) > .2)
        if not ground and upright_gait:
            stance_ok = ratio >= NARROW_RATIO and not crossed
            if ratio < NARROW_RATIO:
                flags.append('possible_narrow_stance')
            if crossed:
                flags.append('possible_crossed_feet')
    return dict(
        version=VERSION, flags=flags,
        checks=dict(direction=direction_ok, path=path_ok, in_place=in_place_ok, stance_width=stance_ok),
        metrics=dict(axes=axes, travel_intent=intent, observed_travel=observed, path_m=round(path, 3),
                     forward_m=round(fwd, 3), left_m=round(lat, 3), net_m=round(net, 3),
                     straightness=round(straightness, 3), max_from_start_m=round(reach, 3), hip_joint_width_m=round(hips, 3),
                     stance_width_m=None if width is None else round(width, 3),
                     stance_width_ratio=None if ratio is None else round(ratio, 3), crossed_feet=crossed),
        thresholds=dict(min_travel_m=MIN_TRAVEL_M, dominant_fraction=DOMINANT, straightness=STRAIGHTNESS,
                    path_check_m=PATH_CHECK_M, in_place_m=IN_PLACE_M, narrow_ratio=NARROW_RATIO, contact_m=CONTACT_M * scale),
        scope='Root travel in skeleton-derived body axes and support-phase foot offsets from the pelvis. '
              'Heuristic and uncalibrated; mixed-direction sequences need segment review; not semantic, collision or robot validation.')
