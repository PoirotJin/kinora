"""Conservative bilingual *text* direction constraints for retrieval.

These labels describe prompt wording, not measured motion or robot safety.
Travel, turning and limb laterality are deliberately kept separate.
"""
from functools import lru_cache
import re
from typing import NamedTuple

from g2h.motion_vocab import TRAVEL

MOVE_LEFT, MOVE_RIGHT, TURN_LEFT, TURN_RIGHT, ANY_LEFT, ANY_RIGHT = 1, 2, 4, 8, 16, 32
LEFT, RIGHT = 21, 42
_SIDE = re.compile(r'\b(left|right)(?:wards?)?\b|[左右]')
_BREAK = re.compile(r'[.!?;,，。；！？]|\b(?:then|while|whereas|but|and|with|followed by)\b|然后|随后|接着|再|但是|但|并且')
_TRAVEL = re.compile(TRAVEL + r'|\b(?:side[- ]?shuffl\w*|sidestep\w*|movement|motion)\b|行走|走|跑|横移|侧移|平移|移动|迈步|迈|跨步|滑步|滑行|跳|爬')
_TURN = re.compile(r'\b(?:turn\w*|rotat\w*|pivot\w*|spin\w*)\b|转身|转弯|旋转|转|扭身')
_LIMB = re.compile(r'\b(?:arms?|hands?|legs?|feet|foot|head|shoulders?|knees?|elbows?|wrists?|fingers?|toes?)\b|手|臂|腿|脚|头|肩|膝|肘|腕')
_NEXT_LIMB = re.compile(r'^\s*(?:[- ](?:hand|foot)ed\b|(?:upper |lower )?(?:arms?|hands?|legs?|foot|feet|shoulders?|knees?|elbows?|wrists?|fingers?|toes?|eyes?|ears?)\b)|^(?:侧)?(?:手|臂|腿|脚|肩|膝|肘|腕|眼|耳)')
_NEG = re.compile(r"\b(?:not|never|without|avoid\w*|don't|doesn't|cannot|can't|rather than|instead of)\b|不要|不向|不往|不朝|不能|不许|禁止|避免|不再")
_ORDERED = re.compile(r'\bthen\b|followed by|然后|随后|接着|先.+再')
_FACING = re.compile(r'\b(?:face|faces|faced|facing)\b|面向|朝向')
_OSCILLATION = re.compile(r'\bside[- ]to[- ]side\b|左右(?:来回|往返|交替)|(?:来回|往返)(?:向)?左右')


class Profile(NamedTuple):
    positive: int
    negative: int
    events: tuple


@lru_cache(maxsize=4096)
def describe(text):
    text = str(text or '').casefold().replace('’', "'")
    if not (_SIDE.search(text) or _OSCILLATION.search(text)):
        return Profile(0, 0, ())
    positive = negative = 0
    events = []
    inherited = None
    for clause in _BREAK.split(text):
        # Explicit lateral oscillation is incompatible with a one-side query.
        # Arm/leg oscillation is not whole-body travel.
        oscillation = _OSCILLATION.search(clause)
        if oscillation and not _LIMB.search(clause) and not _NEG.search(clause[:oscillation.start()]):
            positive |= ANY_LEFT | ANY_RIGHT
        if not _SIDE.search(clause):
            actions = [(m.start(), m.end(), 'move') for m in _TRAVEL.finditer(clause)]
            actions += [(m.start(), m.end(), 'turn') for m in _TURN.finditer(clause)]
            action = max(actions, default=None)
            if action and not _LIMB.search(clause):
                inherited = action[2]
        previous_side = 0
        for side in _SIDE.finditer(clause):
            before, after = clause[:side.start()], clause[side.end():]
            if _NEXT_LIMB.search(after):
                continue
            # "right now", "left behind" and hand/foot movement aren't travel.
            if re.match(r'\s+(?:now|away|behind|over)\b', after):
                continue
            actions = [(m.start(), m.end(), 'move') for m in _TRAVEL.finditer(before)]
            actions += [(m.start(), m.end(), 'turn') for m in _TURN.finditer(before)]
            action = max(actions, default=None)
            if action:
                category = action[2]
                # "moves the arm to the right" / "turns the head right".
                if _LIMB.search(before[action[1]:]):
                    continue
                if category == 'move' and _FACING.search(before[action[1]:]):
                    continue  # Moving while facing left does not mean moving left.
                # A limb as subject: "the arms move to the right".
                subject = before[:action[0]]
                subject = re.sub(r'\bon (?:both |one |the )?(?:feet|foot|legs?|tiptoes?)\b', '', subject)
                if _LIMB.search(subject):
                    continue
            else:
                if _LIMB.search(before):
                    continue
                # Direction before the action: "right turn", "向右横向移动".
                turn, travel = _TURN.search(after), _TRAVEL.search(after)
                following = sorted([(m.start(), c) for m, c in [(turn, 'turn'), (travel, 'move')] if m])
                if following and following[0][0] < 12:
                    category = following[0][1]
                    if _LIMB.search(after[:following[0][0] + 3]):
                        continue
                else:
                    category = inherited or 'any'
            is_right = side.group() in ('right', 'rightward', 'rightwards', '右')
            bit = {'move': (MOVE_LEFT, MOVE_RIGHT), 'turn': (TURN_LEFT, TURN_RIGHT),
                   'any': (ANY_LEFT, ANY_RIGHT)}[category][is_right]
            scope = clause[previous_side:side.start()]
            scope = re.sub(r'\bnot only\b|不但|不仅', '', scope)
            negated = bool(_NEG.search(scope))
            if negated:
                negative |= bit
            else:
                positive |= bit
                if not events or events[-1] != bit:
                    events.append(bit)
            inherited = category
            previous_side = side.end()
    return Profile(positive, negative, tuple(events))


def prepare(prompts):
    """Run language parsing once per catalog refresh, never per motion row."""
    import numpy as np
    profiles = tuple(describe(row[1]) for row in prompts)
    positive = np.array([p.positive for p in profiles], dtype=np.uint8)
    negative = np.array([p.negative for p in profiles], dtype=np.uint8)
    positive.flags.writeable = negative.flags.writeable = False
    return positive, negative, tuple(p.events for p in profiles)


def constrain(query, prepared):
    import numpy as np
    requested = describe(query)
    if not (requested.positive or requested.negative):
        return None
    positive, negative, events = prepared
    keep = np.ones(len(positive), bool)
    for same, opposite, generic_opposite in ((1, 2, 32), (2, 1, 16), (4, 8, 32), (8, 4, 16)):
        if requested.positive & same:
            keep &= (positive & same != 0) & (negative & same == 0)
            if not requested.positive & opposite:
                keep &= positive & (opposite | generic_opposite) == 0
        if requested.negative & same:
            keep &= positive & (same | (16 if same in (1, 4) else 32)) == 0
    for side, opposite in ((LEFT, RIGHT), (RIGHT, LEFT)):
        generic = ANY_LEFT if side == LEFT else ANY_RIGHT
        if requested.positive & generic:
            keep &= positive & side != 0
            keep &= positive & negative & side == 0
            if not requested.positive & opposite:
                keep &= positive & opposite == 0
        if requested.negative & generic:
            keep &= positive & side == 0
    # Purely negative queries require explicit evidence of an alternative side.
    if not requested.positive:
        keep &= positive != 0
    if _ORDERED.search(query.casefold()) and len(requested.events) > 1:
        relevant = requested.positive
        wanted = requested.events
        ordered = []
        for sequence in events:
            normalized = tuple(ANY_LEFT if bit & LEFT and relevant & ANY_LEFT else
                               ANY_RIGHT if bit & RIGHT and relevant & ANY_RIGHT else bit for bit in sequence)
            projected = tuple(bit for bit in normalized if bit & relevant)
            ordered.append(projected == wanted)
        keep &= np.asarray(ordered)
    return keep
