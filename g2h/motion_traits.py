"""Conservative lexical motion differences; never a semantic equivalence test."""
import re

AXES={
 'direction':{'backward':r'\bbackward[s]?\b','forward':r'\bforward[s]?\b','left':r'\bleft\b','right':r'\bright\b'},
 'path':{'straight':r'\bstraight\b','circle':r'\bcircl\w*\b','zigzag':r'\bzig[ -]?zag\b','diagonal':r'\bdiagonal\w*\b','spiral':r'\bspiral\b'},
 'speed':{'slow':r'\bslow\w*\b','fast':r'\b(?:fast|brisk\w*|sprint\w*)\b','steady':r'\b(?:steady|constant)\b','accelerate':r'\baccelerat\w*\b','decelerate':r'\bdecelerat\w*\b'},
 'gait':{'shuffle':r'\bshuffl\w*\b','crossover':r'\b(?:crossover|cross[ -]step)\b','high_knees':r'\bhigh[ -]knee\w*\b','limp':r'\blimp\w*\b'},
 'turn':{'clockwise':r'(?<!counter)\bclockwise\b','counterclockwise':r'\b(?:counterclockwise|anti-clockwise)\b','90':r'\b(?:ninety|90)[ -]degree','180':r'\b(?:180|one hundred and eighty)[ -]degree'},
 'rhythm':{'pause':r'\b(?:paus\w*|stop\w*)\b','continuous':r'\bcontinu\w*\b','alternating':r'\balternat\w*\b'},
 'dance':{n:rf'\b{n}\b' for n in ('ballet','waltz','salsa','tango','cha-cha','hip-hop','flamenco','tap','foxtrot')}
}

def traits(text):
    return {axis:[label for label,pattern in labels.items() if re.search(pattern,text.lower())]
            for axis,labels in AXES.items()}

def explicit_differences(a,b):
    aa,bb=traits(a),traits(b)
    # Missing attributes are unknown, not proof of a difference.
    return [axis for axis in AXES if aa[axis] and bb[axis] and set(aa[axis])!=set(bb[axis])]
