"""Bounded reference-conditioned sequencing through Kimodo's native multiprompt API."""
import math
import numpy as np
from scipy.spatial.transform import Rotation

VERSION = 'kimodo-reference-sequence-v2'
MAX_SECONDS = 120
TRANSITION_FRAMES = 30
WINDOW_FRAMES = 300
MIN_SEGMENT_FRAMES = 30
MAX_SEGMENTS = 40


def _frames(frames):
    if type(frames) is not int or not 30 <= frames <= MAX_SECONDS * 30:
        raise ValueError('Kimodo generation requires 30..3600 frames')
    return frames


def segment_limits(frames):
    """Window budget handed to the prompt planner; seconds at 30 fps."""
    _frames(frames)
    later = WINDOW_FRAMES - TRANSITION_FRAMES
    least = 1 if frames <= WINDOW_FRAMES else 1 + math.ceil((frames - WINDOW_FRAMES) / later)
    return {'duration_s': frames / 30, 'min_segments': least,
            'max_segments': max(least, min(MAX_SEGMENTS, frames // MIN_SEGMENT_FRAMES)),
            'first_segment_max_s': WINDOW_FRAMES / 30, 'later_segment_max_s': later / 30,
            'segment_min_s': MIN_SEGMENT_FRAMES / 30}


def check_segments(segments, frames):
    """Planned per-segment prompts must exactly tile the request inside the model window."""
    _frames(frames)
    if not isinstance(segments, list) or not 2 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError('Segmented Kimodo requests need 2..%d segments' % MAX_SEGMENTS)
    for i, item in enumerate(segments):
        if not isinstance(item, dict) or set(item) != {'text', 'frames'}:
            raise ValueError('Each segment needs exactly text and frames')
        if not isinstance(item['text'], str) or not 0 < len(item['text']) <= 1000:
            raise ValueError('Invalid segment text')
        # A later segment's window also holds the re-synthesized reference prefix.
        limit = WINDOW_FRAMES if i == 0 else WINDOW_FRAMES - TRANSITION_FRAMES
        if type(item['frames']) is not int or not MIN_SEGMENT_FRAMES <= item['frames'] <= limit:
            raise ValueError('Segment %d must last %d..%d frames' % (i + 1, MIN_SEGMENT_FRAMES, limit))
    if sum(item['frames'] for item in segments) != frames:
        raise ValueError('Segment frames must add up to the requested duration')
    return segments


def planned_segments(items, frames):
    """Convert planner seconds to frames that sum exactly to the request (largest remainder)."""
    _frames(frames)
    if not isinstance(items, list) or not items:
        raise ValueError('The plan must contain at least one segment')
    seconds = [item.get('seconds') if isinstance(item, dict) else None for item in items]
    if any(type(s) not in (int, float) or not math.isfinite(s) or s <= 0 for s in seconds):
        raise ValueError('Every segment needs positive seconds')
    total = sum(seconds)
    if abs(total - frames / 30) > max(1, .05 * frames / 30):
        raise ValueError('Segment seconds add up to %.1f, not %.1f' % (total, frames / 30))
    exact = [s / total * frames for s in seconds]
    counts = [int(x) for x in exact]
    for i in sorted(range(len(exact)), key=lambda i: counts[i] - exact[i])[:frames - sum(counts)]:
        counts[i] += 1
    segments = [{'text': item['text'], 'frames': n} for item, n in zip(items, counts)]
    if len(segments) == 1:
        if frames > WINDOW_FRAMES:
            raise ValueError('A single segment cannot exceed %d frames' % WINDOW_FRAMES)
        return segments
    return check_segments(segments, frames)


def prompt_texts(spec):
    """Texts Kimodo encodes for a spec, in segment order."""
    return [s['text'] for s in spec['segments']] if spec.get('segments') else [spec['text']]


def generation_plan(frames, segments=None):
    if segments:
        return [s['frames'] for s in check_segments(segments, frames)]
    _frames(frames)
    if frames <= WINDOW_FRAMES:
        return [frames]
    # The official API adds the reference prefix to each later segment.
    # Reserve that space inside the model's 300-frame window; balance the
    # remainder so a request just beyond a window never has a tiny last part.
    count = math.ceil(frames / (WINDOW_FRAMES - TRANSITION_FRAMES))
    size, extra = divmod(frames, count)
    return [size + (i < extra) for i in range(count)]


def pipeline_request(text, frames, segments=None):
    plan = generation_plan(frames, segments)
    if len(plan) == 1:
        return {'prompts': text, 'num_frames': frames}
    texts = [s['text'] for s in segments] if segments else [text] * len(plan)
    return {'prompts': texts, 'num_frames': plan,
            'multi_prompt': True, 'num_transition_frames': TRANSITION_FRAMES}


def seam_report(arrays, frames, segments=None, transition_frames=TRANSITION_FRAMES):
    segments = generation_plan(frames, segments)
    if len(arrays['native_transl']) != frames:
        raise ValueError('Long motion frame count does not match the request')
    world = arrays['native_keypoints3d'].astype(float) + arrays['native_transl'][:, None]
    local = Rotation.from_rotvec(arrays['smpl_poses'].reshape(-1, 3)).as_matrix().reshape(frames, 52, 3, 3)
    checks = []
    # The previous tail is re-synthesized too; measure both ends of that region.
    for boundary in np.cumsum(segments)[:-1]:
        for cut in (int(boundary) - transition_frames, int(boundary)):
            lo, hi = max(0, cut-3), min(frames, cut+4)
            step = float(np.linalg.norm(np.diff(world[lo:hi], axis=0), axis=-1).max())
            acceleration = float(np.linalg.norm(np.diff(world[lo:hi], n=2, axis=0), axis=-1).max()*900)
            r = local[lo:hi]
            delta = r[:-1].swapaxes(-1, -2) @ r[1:]
            angle = float(Rotation.from_matrix(delta.reshape(-1, 3, 3)).magnitude().max())
            passed = bool(step <= .25 and acceleration <= 150 and angle <= np.deg2rad(45))
            checks.append({'frame': cut, 'joint_step_max_m': step,
                           'joint_acceleration_max_m_s2': acceleration,
                           'rotation_step_max_rad': angle, 'passed': passed})
    return {'version': VERSION, 'passed': all(c['passed'] for c in checks),
            'frames': frames, 'segment_frames': segments, 'transition_frames': transition_frames if checks else 0,
            'boundaries': checks, 'limits': {'joint_step_m': .25, 'joint_acceleration_m_s2': 150,
                                           'rotation_step_deg': 45},
            'scope': 'Numerical seam screening only; semantics, contact quality and robot dynamics require review.'}
