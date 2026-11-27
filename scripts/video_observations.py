"""CPU stage of the video motion pipeline, run in the isolated vision environment."""
import json
import sys
from pathlib import Path
import tempfile

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from g2h.video_shots import prepare_shots, tracked_landmarks
from g2h.video_pose import fit_observations, scene_conditions


def prepare(source, model, destination):
    with tempfile.TemporaryDirectory(dir=destination) as scratch:
        shots = prepare_shots(source, scratch)
        world, _, scores = tracked_landmarks(shots, model)
    reference, confidence, shot_reports = fit_observations(world,scores,shots)
    frames = len(confidence)
    if frames < 30:
        raise ValueError('视频须包含至少 1 秒的人体动作')
    # Discontinuous cuts cannot share boundary overlap; pad short spans to >= 30
    # frames within the clip so human_interaction.validate always receives >= 1s.
    spans = []
    for cut in shot_reports:
        if spans and cut['cut_joint_jump_m'] <= .28:
            spans[-1] = (spans[-1][0], cut['end_frame'])
        else:
            spans.append((cut['start_frame'], cut['end_frame']))
    ranges = []
    for span_start, span_end in spans:
        start = span_start
        first_in_span = True
        while start < span_end:
            end = min(span_end, start + 150)
            if span_end - end in range(1, 20):
                end -= 20
            dup = 0 if first_in_span else 1
            keep = end - (start + dup)
            win_start, win_end = start, end
            if win_end - win_start < 30:
                win_end = min(frames, win_start + 30)
                if win_end - win_start < 30:
                    win_start = max(0, win_end - 30)
            overlap = (start + dup) - win_start
            ranges.append(dict(
                start=start, end=end, overlap=overlap, keep_frames=keep,
                request=scene_conditions(reference, confidence, shots, win_start, win_end),
            ))
            if end == span_end:
                break
            start = end - 1
            first_in_span = False
    np.savez_compressed(destination/'observed.npz',**reference,confidence=confidence)
    return dict(frames=frames,shots=shot_reports,segments=ranges)


if __name__=='__main__':
    directory = Path(sys.argv[1])
    manifest = prepare(directory/'input.video',Path(sys.argv[2]),directory)
    tmp = directory/'reference.tmp'
    tmp.write_text(json.dumps(manifest,allow_nan=False))
    tmp.replace(directory/'reference.json')
