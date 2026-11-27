"""Bounded, explicit runtime loader; AGENTS.md is not read automatically by Qwen."""
from pathlib import Path

VERSION = 'body_clearance_v1'
BEGIN = '<!-- MOTION_GENERATION_POLICY_BEGIN -->'
END = '<!-- MOTION_GENERATION_POLICY_END -->'
FALLBACK = ('For ordinary standing and locomotion keep the feet hip-width apart '
            'on separate tracks, avoiding unintended crossing or foot contact. '
            'Keep hands and forearms visibly clear of the torso, hips and thighs '
            'with relaxed, slightly outward arms. Preserve the requested movement '
            'and intentional contact; do not claim geometric safety from text.')

def motion_policy(path=None):
    path=Path(path) if path else Path(__file__).resolve().parents[1]/'AGENTS.md'
    try:
        with path.open('r',encoding='utf-8') as f: value=f.read(16385)
        if len(value)>16384 or value.count(BEGIN)!=1 or value.count(END)!=1:return FALLBACK
        section=value.split(BEGIN,1)[1].split(END,1)[0].strip()
        if not section or len(section)>4096 or not section.isascii():return FALLBACK
        return section
    except (OSError,UnicodeError):return FALLBACK
