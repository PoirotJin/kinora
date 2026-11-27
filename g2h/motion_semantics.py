"""Conservative, CPU-only body-frame direction checks. Scores are not probabilities.

Native human: Y up, +Z forward. Robot: Z up, +X forward, quaternion wxyz.
Only explicit whole-body locomotion requirements are enforced; incomplete parsing
and unsupported temporal descriptions remain visibly unverified.
"""
import re
import numpy as np
from g2h.gait_checks import in_place_requested
from g2h import motion_vocab as vocab

VERSION = 'direction-v3'
THRESHOLDS = dict(active_speed_mps=.05, directional_speed_mps=.03,
                  minimum_travel_m=.20, direction_fraction=.75,
                  stationary_radius_m=.12, minimum_turn_degrees=20.,
                  one_way_straightness=.5, one_way_min_path_m=1.)
LABELS = dict(forward='前进', backward='后退', left='向左侧移', right='向右侧移',
              turn_left='向左转身', turn_right='向右转身', turn='转身',
              stationary='保持原地', moving='发生位置移动', one_way='单向移动不折返')
# Locomotion words come from the shared vocabulary in g2h.motion_vocab.
# Wording that allows a directional path to bend or come back.
PATH_FREE = r'\b(?:return\w*|comes? back|back to|back and forth|to and fro|around|loop\w*|zig\w*|weav\w*|pac(?:e|es|ing)|u-turn|wander\w*)\b|回到|返回|折返|来回'



def requirements(text):
    text = str(text).lower().replace('’', "'")
    found = set()
    warnings = []
    # Temporal boundaries cannot be inferred safely from prose alone.
    temporal = bool(re.search(r'\b(then|afterwards|subsequently|before|after|followed by)\b|然后|随后|接着|先.+再', text))
    negated = bool(re.search(r"\b(not|never|without|avoid|avoiding|don't|doesn't)\b|不要|不许|禁止|不向|不往", text))
    # Inspect the first clause of each sentence; limb motions must not become
    # whole-body movement constraints (e.g. 'arms swing backward').
    for sentence in re.split(r'[.!?。；;]', text):
        clause = re.split(r'[,，]|\bwhile\b|\bwith\b', sentence)[0].strip()
        if not clause:
            continue
        # "hops forward on both feet" describes how, not a limb moving on its own.
        clause = re.sub(r'\bon (?:both |one |his |her |their |the |the balls of (?:his |her |their |the )?)?(?:feet|foot|legs?|toes|tiptoes?)\b', ' ', clause)
        if re.search(r'\b(arms?|hands?|legs?|feet|foot|head)\b', clause):
            continue
        for name in ('forward', 'backward'):
            # "sprints forward", "walks slowly backward", "a forward sprint", "backpedals"
            if any(re.search(pattern, clause) for pattern in vocab.directed(name)):
                found.add(name)
        for side in ('left', 'right'):
            if re.search(vocab.TRAVEL+r'\s+(?:sideways\s+)?(?:to(?:ward)?\s+)?(?:their\s+|the\s+|his\s+|her\s+)?'+side+r'\b', clause):
                found.add(side)
            if re.search(r'\b(?:turn\w*|rotat\w*|pivot\w*)\s+(?:slowly\s+)?(?:to\s+)?(?:their\s+|the\s+)?'+side+r'\b', clause):
                found.add('turn_'+side)
        body_clause = re.sub(
            r'\b(?:turns?|turning|rotates?|rotating|twists?|twisting)\s+'
            r'(?:(?:a|an|the|two|several|some|each|this|that|his|her|their)\s+)?'
            r'(?:(?:fastening|steering|small|large|heavy|metal|wooden)\s+)?'
            r'(?:tools?|wrench(?:es)?|screwdrivers?|handles?|knobs?|dials?|wheels?|'
            r'screws?|bolts?|nuts?|keys?|valves?|caps?|lids?|pages?|trays?|plates?|parts?)\b',
            ' ', clause)
        if re.search(r'\b(turns?|turning|rotates?|rotating|pivots?|pivoting)\b', body_clause):
            found.add('turn')
        # Shared with gait screening: "from a stationary crouch" is a start state,
        # not a request to stay in place.
        if in_place_requested(clause):
            found.add('stationary')
        if re.search(vocab.GAIT+r'|\b(?:travel\w*|side[- ]?step\w*)', clause):
            found.add('moving')
    zh_body = re.sub(
        r'(?:拧|旋紧|旋转|转动)(?:一个|这|那|多个)?(?:工具|扳手|把手|旋钮|螺丝|螺栓|螺母|阀门|盖|方向盘|钥匙|零件)',
        ' ', text)
    for pattern, name in [(vocab.CHINESE['backward'], 'backward'), (vocab.CHINESE['forward'], 'forward'),
                          (vocab.CHINESE['left'], 'left'), (vocab.CHINESE['right'], 'right'),
                          (vocab.CHINESE_GAIT, 'moving'),
                          (r'左转|向左转', 'turn_left'), (r'右转|向右转', 'turn_right'),
                          (r'转身|旋转', 'turn'), (r'原地|站着不动|保持静止', 'stationary')]:
        if re.search(pattern, zh_body if name == 'turn' else text):
            found.add(name)
    if found & {'forward', 'backward', 'left', 'right'}:
        found.add('moving')
    if 'stationary' in found:
        found.discard('moving')  # marching/walking in place does not travel
    conflicts = any(set(pair) <= found for pair in [('forward','backward'), ('left','right'), ('turn_left','turn_right')])
    if temporal or negated or conflicts:
        warnings.append('包含时序、否定或方向冲突，需分段标注后复核')
        found.clear()
    if re.search(r'\b(curv\w*|circle\w*|arc|degrees?|clockwise|counterclockwise)\b|曲线|弧线|圈|度|顺时针|逆时针', text):
        warnings.append('曲线路径、转角或旋转圈数尚未核验')
    elif found & {'forward', 'backward', 'left', 'right'} and not found & {'turn', 'turn_left', 'turn_right', 'stationary'} \
            and not re.search(PATH_FREE, text):
        # Body-frame direction cannot see a runner who turns around and comes back.
        found.add('one_way')
    if not found:
        warnings.append('未识别到可可靠检查的整体移动方向要求')
    return sorted(found), list(dict.fromkeys(warnings))


def analyze(text, positions, forward, fps, up_axis):
    p = np.asarray(positions, dtype=float)
    f = np.asarray(forward, dtype=float)
    if p.ndim != 2 or p.shape[1:] != (3,) or f.shape != p.shape or len(p) < 3:
        raise ValueError('语义检查：轨迹或朝向形状无效')
    if not np.isfinite(p).all() or not np.isfinite(f).all() or not np.isfinite(fps) or not 1 <= fps <= 240:
        raise ValueError('语义检查：轨迹或帧率无效')
    p = p.copy(); f = f.copy(); p[:, up_axis] = 0; f[:, up_axis] = 0
    norms = np.linalg.norm(f, axis=1)
    requested, warnings = requirements(text)
    if np.any(norms < .2):
        return dict(version=VERSION, status='review', checks=[], reasons=['身体朝向接近竖直，无法可靠判定水平前后'], thresholds=THRESHOLDS)
    f /= norms[:, None]
    up = np.zeros(3); up[up_axis] = 1
    left = np.cross(up, f)
    delta = np.diff(p, axis=0)
    distance = np.linalg.norm(delta, axis=1)
    active = distance * fps > THRESHOLDS['active_speed_mps']
    v_forward = np.sum(delta * f[:-1], axis=1) * fps
    v_left = np.sum(delta * left[:-1], axis=1) * fps
    turn_steps = np.arctan2(np.cross(f[:-1], f[1:]) @ up, np.sum(f[:-1]*f[1:], axis=1))
    yaw = np.r_[0., np.cumsum(turn_steps)]
    metrics = dict(path_m=float(distance.sum()), radius_m=float(np.linalg.norm(p-p[0],axis=1).max()),
                   signed_forward_m=float(v_forward.sum()/fps), signed_left_m=float(v_left.sum()/fps),
                   turn_degrees=float(np.degrees(yaw[-1])), turn_range_degrees=float(np.degrees(np.ptp(yaw))),
                   active_seconds=float(active.sum()/fps))
    checks = []
    for name in requested:
        if name in ('forward','backward','left','right'):
            values = v_forward if name in ('forward','backward') else v_left
            sign = 1 if name in ('forward','left') else -1
            ratio = float((sign*values[active] > THRESHOLDS['directional_speed_mps']).mean()) if active.any() else 0.
            ok = ratio >= THRESHOLDS['direction_fraction'] and metrics['path_m'] >= THRESHOLDS['minimum_travel_m']
            observed = f'符合方向的有效移动帧占 {ratio:.0%}，要求至少 75%'
            metrics[name+'_fraction'] = ratio
        elif name == 'moving':
            ok = metrics['radius_m'] >= THRESHOLDS['minimum_travel_m']
            observed = f"相对起点最大位移 {metrics['radius_m']:.2f} 米，要求至少 0.20 米"
        elif name == 'one_way':
            net = float(np.linalg.norm(p[-1]-p[0])); straight = net/metrics['path_m'] if metrics['path_m'] > 1e-9 else 1.
            metrics['straightness'] = straight
            if metrics['path_m'] < THRESHOLDS['one_way_min_path_m']:
                continue
            observed = f"起终点直线距离 {net:.2f} 米，占总路程 {metrics['path_m']:.2f} 米的 {straight:.0%}，单向移动通常至少 50%"
            # Uncalibrated: a suspected doubling back is sent to human review only,
            # so it never quarantines the motion or triggers regeneration.
            if straight < THRESHOLDS['one_way_straightness']:
                warnings.append('疑似折返，需人工审核：'+observed)
                checks.append(dict(requirement=name, label=LABELS[name], status='review', observed=observed, reason=''))
                continue
            ok = True
        elif name == 'stationary':
            ok = metrics['radius_m'] <= THRESHOLDS['stationary_radius_m']
            observed = f"相对起点最大位移 {metrics['radius_m']:.2f} 米，原地容差 0.12 米"
        else:
            angle = metrics['turn_degrees']
            ok = (metrics['turn_range_degrees'] if name == 'turn' else angle if name == 'turn_left' else -angle) >= THRESHOLDS['minimum_turn_degrees']
            observed = f'净转角 {angle:.1f}°（正值为左转），最低转角 20°'
        checks.append(dict(requirement=name, label=LABELS[name], status='pass' if ok else 'fail', observed=observed,
                           reason='' if ok else f'要求{LABELS[name]}；{observed}'))
    failures = [c['reason'] for c in checks if c['status']=='fail']
    return dict(version=VERSION, status='fail' if failures else 'review' if warnings else 'pass',
                scope='仅整体前后、侧移、转向和位置移动；不代表全部文本语义通过',
                checks=checks, reasons=failures, warnings=warnings, metrics=metrics, thresholds=THRESHOLDS)


def human(text, motion, segments=None):
    if 'native_root_rotations_mat' not in motion or len(motion['native_transl'])<3:
        return dict(version=VERSION,status='review',checks=[],reasons=[],
                    warnings=['缺少可靠身体朝向或有效帧不足，方向语义待审核'],thresholds=THRESHOLDS)
    p = motion['native_transl']; f = np.asarray(motion['native_root_rotations_mat'])[:,:,2]
    return sequence(text, p, f, float(motion['fps']), 1, segments)


def for_record(record, qpos=None, fps=None):
    """Check original user intent as well as the generated English description."""
    checker = (lambda text, segments: human(text,record['motion'],segments)) if qpos is None else (lambda text, segments: robot(text,qpos,fps,segments))
    report=checker(record.get('text',''),record.get('segments'))
    original=record.get('farm',{}).get('source',{}).get('original_description','')
    if original and original.strip()!=record.get('text','').strip():
        source=checker(original,None)
        report['original_intent']=source
        report['reasons'] += ['原始描述：'+x for x in source['reasons']]
        report['warnings'] = report.get('warnings',[])+['原始描述：'+x for x in source.get('warnings',[])]
        if source['status']=='fail':report['status']='fail'
        elif source['status']=='review' and report['status']!='fail':report['status']='review'
    if qpos is None and ('scene' in record or 'scene_spec_json' in record.get('motion', {})):
        if report['status'] == 'fail':
            report['status'] = 'review'
            report['warnings'] = report.get('warnings', []) + list(report.get('reasons', []))
            report['reasons'] = []
    return report


def robot(text, qpos, fps, segments=None):
    q = np.asarray(qpos, dtype=float)
    if q.ndim != 2 or q.shape[1] < 7:
        raise ValueError('语义检查：机器人轨迹无效')
    quat = q[:,3:7]; norms = np.linalg.norm(quat, axis=1)
    if not np.isfinite(quat).all() or np.any(abs(norms-1)>.02):
        raise ValueError('语义检查：机器人朝向四元数无效')
    w,x,y,z = (quat/norms[:,None]).T
    f = np.stack([1-2*(y*y+z*z), 2*(x*y+w*z), 2*(x*z-w*y)], axis=1)
    return sequence(text, q[:,:3], f, float(fps), 2, segments)


def sequence(text, p, f, fps, up, segments=None):
    if not segments:
        return analyze(text, p, f, fps, up)
    if sum(s['frames'] for s in segments) != len(p):
        raise ValueError('语义检查：分段帧数与轨迹不一致')
    results=[]; start=0
    for i, s in enumerate(segments):
        end=start+s['frames']
        # Ignore at most 0.15 s at each internal transition boundary.
        trim=min(round(.15*fps), max(0,(end-start-3)//2))
        a=start+(trim if i else 0); b=end-(trim if i<len(segments)-1 else 0)
        report=analyze(s['text'],p[a:b],f[a:b],fps,up)
        report.update(segment=i+1,start_frame=a,end_frame=b);results.append(report);start=end
    return dict(version=VERSION,status='fail' if any(r['status']=='fail' for r in results) else 'review' if any(r['status']=='review' for r in results) else 'pass',
                checks=[],segments=results,reasons=[f"第{r['segment']}段：{x}" for r in results for x in r['reasons']],
                warnings=[x for r in results for x in r.get('warnings',[])],thresholds=THRESHOLDS)


def message(report):
    if report['status']=='fail':
        return '方向语义不符：'+'；'.join(report['reasons'])
    if report['status']=='review':
        return '方向语义待审核：'+'；'.join(report.get('warnings',[]) or report.get('reasons',[]))
    return '基础方向检查通过（完整文本语义仍待审核）'
