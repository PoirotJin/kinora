"""Bounded, deterministic diversification and evidence resolution for text only."""
import hashlib
import json
from pathlib import Path
import random
import sqlite3
from contextlib import closing


def exploration_seed(run_id, topic, attempt):
    return int.from_bytes(hashlib.sha256(f'{run_id}:{topic}:{attempt}'.encode()).digest()[:8], 'big')


def plan(topic, memory, seed):
    # These are optional motion cues, never claims of missing global coverage.
    from g2h.motion_traits import AXES
    rng=random.Random(seed)
    axes=['direction','speed','rhythm']
    if topic in ('walking','running','crawling'):axes.append('path')
    if topic=='walking':axes.append('gait')
    if topic=='dancing':axes.extend(['dance','turn'])
    counts=memory.get('counts',{})
    chosen=rng.sample(axes,2)
    values=[]
    for axis in chosen:
        labels=list(AXES[axis])
        # Weighted sampling, rather than repeatedly selecting a single zero bin.
        weights=[1/(1+counts.get(axis,{}).get(label,0))**.5 for label in labels]
        values.append(rng.choices(labels,weights=weights,k=1)[0])
    recipe=composition(topic,rng)
    return dict(movement_composition=recipe,optional_cues=dict(zip(chosen,values)),scope='Recent lexical sample only; these combinations may already exist.',
        instruction='Develop one feasible movement combination within the topic and duration. Use the proposed composition as a starting point: the action order, support changes or path transition should differ meaningfully from neighbors, not merely the adjectives. Keep at most two connected primary actions. Numeric changes alone do not prove novelty. Adapt incompatible cues, keep action order clear, and do not copy a historical prompt with cosmetic changes. For ground topics vary starting posture, support strategy or transition order when feasible. The cues are intentions, not geometry validation.')



# Concrete two-phase families, not claims that these combinations are novel.
# The semantic reviewer remains responsible for accepting or rejecting each one.
COMPOSITIONS={
 'walking': [('walk on a shallow curved path','straighten the path and slow to a stop'),('walk diagonally','change direction with short side steps'),('walk forward with a narrow step width','widen the stance and continue'),('walk backward carefully','pivot and walk forward'),('take alternating short and long steps','settle into even steps')],
 'running': [('run on a gentle arc','straighten and decelerate'),('jog forward','turn and jog diagonally'),('jog with short steps','lengthen the stride gradually'),('run laterally','pivot into forward jogging'),('jog backward slowly','stop in a stable stance')],
 'dancing': [('perform side steps with weight shifts','make a small turn and repeat facing the new direction'),('make two diagonal steps','rock the weight back and recover'),('perform a heel-toe step pattern','turn with short grounded steps'),('shift weight with a low knee bend','rise into a lateral step'),('step forward then back','finish with a quarter turn')],
 'ground_get_up': [('start supine and roll onto one side','push up through side sitting toward kneeling'),('start prone and bring the knees under the hips','push up into a stable half-kneel'),('start side lying and prop on a forearm','shift support to a palm and sit upright'),('start sitting on the floor and shift into half kneeling','stand with controlled weight transfer'),('start prone and press the torso up','bring one foot forward into half kneeling')],
 'standing_to_ground': [('start standing and bend both knees','lower through kneeling into side sitting'),('start standing and move into half kneeling','lower onto the side with hand support'),('start standing and squat','place both palms down and lower into a hands-and-knees position'),('start standing and kneel carefully','lower the torso into prone lying'),('start standing and squat slowly','sit down with hand support')],
 'squatting': [('squat slowly with a balanced stance','rise while shifting weight slightly to one side'),('squat with a moderately wide stance','hold briefly and rise smoothly'),('take a small side step','perform one controlled squat'),('make two shallow squat pulses','rise fully to standing'),('start in a squat and shift weight left','shift right and stand')],
 'crawling': [('crawl forward on hands and knees','curve gently to one side'),('crawl diagonally','pause and resume straight ahead'),('crawl backward slowly','shift weight and change direction'),('crawl with short alternating advances','lengthen the advances slightly'),('crawl low on forearms and knees','turn gradually without crossing the limbs')],
 'floor_movements': [('start supine and curl the torso up','lower smoothly to the floor'),('start prone with palms beside the shoulders','press up and lower in one controlled repetition'),('start side lying and prop on a forearm','lift the torso slightly and lower'),('start seated on the floor with bent knees','lean back with palm support and return upright'),('start supine with bent knees','lift the hips briefly and lower them')]
}


def composition(topic,rng):
    pair=rng.choice(COMPOSITIONS.get(topic,[('perform one clear action within the topic','finish in a stable posture')]))
    return dict(first=pair[0],then=pair[1],
                timing=rng.choice(['first phase brief, second phase sustained','first phase gradual, second phase brief','both phases smoothly connected']),
                note='Adapt to an achievable 8-second clip. Preserve body clearance. No prop, scene change, or geometry certification. Discard if equivalent to prior motion; do not force acceptance.')


def recent_feedback(root,topic):
    """At most 128 decision keys and four three-attempt outboxes, no directory scan."""
    path=Path(root)/'review/agent.sqlite3'
    if not path.exists():return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            rows=db.execute('SELECT id,topic,verdict FROM decisions ORDER BY ts DESC LIMIT 128').fetchall()
        runs=[];result=[]
        for key,t,verdict in rows:
            if t!=topic or verdict=='distinct':continue
            run=key.split(':')[0]
            if not run or Path(run).name!=run or run in runs:continue
            runs.append(run)
            for attempt in range(3,0,-1):
                p=Path(root)/'agent_runs'/run/f'attempt_{attempt:02d}'/'outcome.json'
                if not p.exists() or p.stat().st_size>32000:continue
                data=json.loads(p.read_text())
                for entry in data.get('rejected',[])+data.get('conflicts',[]):
                    if entry.get('text'):
                        result.append({k:entry[k] for k in ('text','reference_text','reason') if k in entry})
                if result:break
            if len(runs)>=4:break
        return result[:4]
    except (OSError,sqlite3.Error,ValueError):return []


def resolved_difference(decision,reference_id,axes,candidate=None,reference=None):
    """A second duplicate verdict must account for every lexical hint explicitly.

    No lexical difference authorizes acceptance. The reviewer may maintain a
    duplicate verdict only with its exact cited evidence and an explanation.
    """
    a=decision.get('difference_assessment')
    if candidate is not None and reference is not None:
        from g2h.motion_traits import traits
        aa,bb=traits(candidate),traits(reference)
        words={term for axis in axes for term in set(aa[axis]) ^ set(bb[axis])}
        explanation=a.get('explanation','').lower() if isinstance(a,dict) else ''
        if any(term.replace('_',' ') not in explanation.replace('_',' ') for term in words):return False
    return (decision.get('verdict')=='duplicate' and isinstance(a,dict)
        and a.get('evidence_text_id')==reference_id
        and isinstance(a.get('axes'),list) and len(a['axes'])==len(axes)
        and set(a['axes'])==set(axes)
        and a.get('same_motion') is True
        and isinstance(a.get('explanation'),str) and len(a['explanation'].strip())>=20)


def direction_conflicts(a,b):
    """Detect opposed action directions without conflating limb clearance text.

    This only requires another review; it cannot authorize acceptance.
    """
    import re
    def events(text):
        text=text.lower()
        text=re.sub(r'\b(?:left|right)\s+(?:foot|feet|leg|knee|hand|arm|shoulder|heel|palm)\b','limb',text)
        result=[]
        verb=r'\b(walk(?:s|ing)?|run(?:s|ning)?|crawl(?:s|ing)?|roll(?:s|ing)?|turn(?:s|ing)?|rotat(?:e|es|ing)|step(?:s|ping)?)\b'
        matches=list(re.finditer(verb,text))
        for i,m in enumerate(matches):
            segment=text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)]
            segment=re.split(r'[.,;]|\b(?:while|with|arms?|hands?|feet|knees?|shoulders?)\b',segment)[0][:100]
            directions=set(re.findall(r'\b(?:left|leftward|right|rightward|forward|forwards|backward|backwards)\b',segment))
            directions={d.replace('leftward','left').replace('rightward','right').rstrip('s') for d in directions}
            if directions:
                action=next(k for k in ('walk','run','crawl','roll','turn','rotat','step') if m[0].startswith(k))
                result.append((action,directions))
        return result
    aa,bb=events(a),events(b);conflicts=[]
    for (action,x),(other,y) in zip(aa,bb):
        if action!=other:continue
        for first,second in [('left','right'),('forward','backward')]:
            if x & {first,second} in ({first},{second}) and y & {first,second} in ({first},{second}) and x & {first,second} != y & {first,second}:
                conflicts.append(dict(action=action,candidate=sorted(x),reference=sorted(y)))
    return conflicts
