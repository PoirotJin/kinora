"""Prepare independently tracked camera shots on the Human52 30 Hz timeline."""
import os
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess


def ffmpeg_binary():
    binary = os.environ.get('MOTIONFARM_FFMPEG') or shutil.which('ffmpeg')
    if binary:
        return binary
    from imageio_ffmpeg import get_ffmpeg_exe
    return get_ffmpeg_exe()


def prepare_shots(source, output, *, max_seconds=120):
    """Decode once to a CFR master. Return exact half-open ranges and tracker inputs."""
    source = Path(source).resolve(strict=True)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    master = output/'camera-master.mp4'
    cmd = [ffmpeg_binary(), '-hide_banner', '-nostdin', '-v', 'error', '-n',
           '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm,avi,mpeg,mpegts',
           '-max_pixels','33554432','-i', str(source), '-map', '0:v:0', '-an']
    # Keep one overflow frame so excessive inputs fail instead of silently truncating.
    filt = "setpts=PTS-STARTPTS,fps=30,scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p,scdet,metadata=print:file=-"
    report = subprocess.run(cmd + ['-vf', filt, '-frames:v', str(max_seconds*30+1),
                            '-c:v', 'libx264', '-crf', '16', str(master)],
                            check=True, capture_output=True, text=True, timeout=max_seconds*5+30)
    scores = [float(value) for value in re.findall(r'^lavfi\.scd\.score=([\d.]+)$', report.stdout, re.M)]
    if not 1 <= len(scores) <= max_seconds*30:
        raise ValueError('视频为空或超过支持的提取时长')
    changes = [float(v) for v in re.findall(r'^lavfi\.scd\.mafd=([\d.]+)$', report.stdout, re.M)]
    if len(changes) != len(scores):
        raise ValueError('缺少分镜差异数据')
    cuts = [0]
    for i in range(1,len(scores)):
        # A subject can occupy a small fraction of a static background. Detect a
        # local change spike against the motion on both sides of the boundary.
        neighbors = changes[max(1,i-8):i]+changes[i+1:i+9]
        background = median(neighbors) if neighbors else changes[i]
        if scores[i] >= 10. or (scores[i] >= .6 and changes[i] >= 6*max(.08,background)):
            cuts.append(i)
    cuts.append(len(scores))
    shots = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
        path = output/f'shot-{index:03d}.mp4'
        if len(cuts) == 2:
            path = master
        else:
            # Re-encode exact frames; keyframe-aligned stream-copy seeks may cross a cut.
            subprocess.run([cmd[0], '-nostdin', '-v','error','-n','-ss',f'{start/30:.9f}',
                            '-i',str(master),'-map','0:v:0','-frames:v',str(end-start),
                            '-c:v','libx264','-crf','16',str(path)],check=True,timeout=max_seconds*3+30)
        shots.append(dict(start_frame=start,end_frame=end,path=str(path),cut_score=scores[start]))
    return shots


def tracked_landmarks(shots, model_path):
    """Track one subject per shot. Missing detections stay missing on the source clock."""
    import imageio_ffmpeg
    import mediapipe as mp
    import numpy as np

    vision = mp.tasks.vision
    options = vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=vision.RunningMode.VIDEO, num_poses=2)
    frames = shots[-1]['end_frame']
    world = np.full((frames,33,3),np.nan,dtype=np.float32)
    screen = np.full_like(world,np.nan)
    confidence = np.zeros((frames,33),dtype=np.float32)
    for shot in shots:
        reader = imageio_ffmpeg.read_frames(shot['path'],pix_fmt='rgb24')
        prev_center = None
        multi_person_frames = 0
        torso_idx = (11,12,23,24)
        try:
            w,h = next(reader)['size']
            count = 0
            with vision.PoseLandmarker.create_from_options(options) as tracker:
                for count,raw in enumerate(reader,1):
                    i = shot['start_frame']+count-1
                    if i >= shot['end_frame']:
                        raise ValueError('视频分镜解码帧数不一致')
                    rgb = np.frombuffer(raw,np.uint8).reshape(h,w,3)
                    result = tracker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=rgb),
                                                      round((count-1)*1000/30))
                    if not result.pose_world_landmarks:
                        continue
                    chosen = 0
                    if len(result.pose_world_landmarks) > 1:
                        multi_person_frames += 1
                        best_key = None
                        for idx, s_lm in enumerate(result.pose_landmarks):
                            pts = np.array([(s_lm[k].x, s_lm[k].y) for k in torso_idx], dtype=np.float32)
                            center = pts.mean(axis=0)
                            vis = float(np.mean([min(s_lm[k].visibility, s_lm[k].presence) for k in torso_idx]))
                            if prev_center is not None:
                                score = (float(np.linalg.norm(center - prev_center)), -vis)
                            else:
                                torso_size = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
                                score = (-vis * torso_size, -vis)
                            if best_key is None or score < best_key[0]:
                                best_key = (score, idx)
                        chosen = best_key[1]
                    chosen_world = result.pose_world_landmarks[chosen]
                    chosen_screen = result.pose_landmarks[chosen]
                    for j,(p,s) in enumerate(zip(chosen_world,chosen_screen)):
                        world[i,j] = (p.x,-p.y,-p.z)
                        screen[i,j] = (s.x,s.y,s.z)
                        confidence[i,j] = min(s.visibility,s.presence)
                    prev_center = np.mean([(chosen_screen[k].x, chosen_screen[k].y) for k in torso_idx], axis=0)
            if count != shot['end_frame']-shot['start_frame']:
                raise ValueError('视频分镜解码帧数不一致')
        finally:
            reader.close()
        shot['multi_person_frames'] = multi_person_frames
    return world, screen, confidence
