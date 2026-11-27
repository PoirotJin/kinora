"""Shared locomotion vocabulary for text-motion checks (gait screening,
direction semantics, body quality). Add words here, not in the checkers.

Stems are matched at a word start and may be followed by any suffix, so
"sprint" covers sprints/sprinting/sprinted. Keep stems long enough not to hit
unrelated words, and keep body-part or in-place words out of these lists.
"""
import re

# Upright gaits: the person travels on their feet in a walking/running pattern.
# Used for "the root should move" and for stance-width checks.
GAIT_STEMS = (
    # walking
    'walk', 'stroll', 'saunter', 'amble', 'strid', 'march', 'hik', 'trudg', 'plod', 'tread', 'trek',
    'tiptoe', 'limp', 'hobbl', 'stagger', 'stumbl', 'shuffl', 'waddl', 'strut', 'swagger', 'promenad',
    'sneak', 'creep', 'prowl', 'stalk', 'pac(?:e|es|ed|ing)\\b',
    # running
    'run(?:s|ning)?\\b', 'ran\\b', 'jog', 'sprint', 'dash', 'scurr', 'scamper', 'trot', 'lope', 'lopes', 'loping',
    'hurr', 'rac(?:e|es|ed|ing)\\b', 'rush', 'charg', 'bolt', 'gallop', 'jaunt', 'backpedal',
)
# Anything that can carry the whole body in a direction, including non-gait
# travel. Not used to infer "must move": a jump may land in place.
TRAVEL_STEMS = GAIT_STEMS + (
    'step', 'side[- ]?step', 'mov', 'travel', 'go(?:es|ing|ne)?\\b', 'went\\b', 'head(?:s|ed|ing)\\b',
    'advanc', 'retreat', 'proceed', 'progress', 'crawl', 'skip', 'hop', 'bound', 'leap', 'jump',
    'lung', 'glid', 'scrambl', 'prance', 'slid(?:e|es|ing)\\b',
)
# Nouns after a direction adjective: "a forward sprint", "a backward walk".
NOUNS = ('sprint', 'dash', 'run', 'jog', 'walk', 'stride', 'step', 'charge', 'rush', 'march', 'lunge',
         'leap', 'jump', 'hop', 'shuffle', 'crawl', 'glide', 'slide', 'bound', 'skip', 'trot', 'gallop',
         'stroll', 'retreat', 'advance', 'roll', 'movement', 'motion', 'travel', 'progress')


def _alternation(stems):
    return '(?:' + '|'.join(s if s.endswith('\\b') else s + r'\w*' for s in stems) + ')'


GAIT = r'\b' + _alternation(GAIT_STEMS)
TRAVEL = r'\b' + _alternation(TRAVEL_STEMS)
# Words allowed between a verb and its direction: "walks slowly and steadily
# backward", "runs straight ahead". Other words break the link, so "walks and
# kicks backward" does not ask for backward travel.
_BETWEEN = r'(?:(?:\w+ly|straight|fast|hard|quick|slow|steadily|and|a little|a bit|very)\s+){0,3}'
_DIRECTION_WORDS = {
    'forward': r'(?:forwards?|ahead|onwards?)',
    'backward': r'(?:backwards?|in reverse|rearward\w*)',
}
_NOUN = '(?:' + '|'.join(NOUNS) + r')\w*'
# Verbs or idioms that state a direction on their own.
_IMPLICIT = {
    'backward': [r'\bbackpedal\w*', r'\bback(?:s|ed|ing)?\s+(?:up|away|off)\b', r'\bstep(?:s|ped|ping)?\s+back\b',
                 r'\bretreat\w*', r'\bmoonwalk\w*'],
    'forward': [r'\badvanc(?:e|es|ed|ing)\b(?!\s+(?:the|his|her|their|one|a)\b)'],
}
# Chinese phrasing. Bare 走/跑 are too broad to imply a direction on their own.
CHINESE = {
    'forward': r'前进|前冲|(?:向|往|朝)前(?:走|跑|冲|跳|移动|行进|迈步|冲刺|迈|奔)',
    'backward': r'后退|倒退|后撤|倒着(?:走|跑)|(?:向|往|朝)后(?:走|跑|跳|移动|退|撤|迈|倒退)',
    'left': r'(?:向|往|朝)左(?:侧移|横移|走|跑|跳|移动|侧步|滑步|平移|迈)|左侧移|左横移',
    'right': r'(?:向|往|朝)右(?:侧移|横移|走|跑|跳|移动|侧步|滑步|平移|迈)|右侧移|右横移',
}
CHINESE_GAIT = r'走路|行走|步行|散步|快走|疾走|奔跑|跑步|慢跑|冲刺|小跑|竞走|踱步'


def directed(direction):
    """Regexes that request whole-body travel in `direction` ('forward'/'backward')."""
    word = _DIRECTION_WORDS[direction]
    return [TRAVEL + r'\s+' + _BETWEEN + word + r'\b',   # walks (slowly) backward
            r'\b' + word + r'\s+' + _NOUN,                # a forward sprint
            *_IMPLICIT[direction]]


def requests(text, direction):
    text = text.lower()
    english = direction in _DIRECTION_WORDS and any(re.search(p, text) for p in directed(direction))
    return bool(english or re.search(CHINESE[direction], text))
