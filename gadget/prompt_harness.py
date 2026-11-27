"""Offline prompting cues and format checks; never calls a model or API."""
import hashlib
from pathlib import Path
import unicodedata

HARNESS_VERSION = '1'
POLICY_VERSION = 'body_clearance_v1'

def prompt_guidance():
    guide = Path(__file__).with_name('PROMPT_GUIDE.md').read_text(encoding='utf-8')
    return dict(harness_version=HARNESS_VERSION, policy_version=POLICY_VERSION,
                guide_sha256=hashlib.sha256(guide.encode()).hexdigest(),
                rewrite_location='local_agent', guide=guide)

def check_motion_prompt(text, duration=8):
    errors = []
    if not isinstance(text, str):
        errors.append('text must be a string')
        text = ''
    words = len(text.split())
    if not text.startswith('A person '):
        errors.append('Begin exactly with "A person ".')
    if not 12 <= words <= 70:
        errors.append('Use 12–70 whitespace-separated English words.')
    if len(text) > 1000:
        errors.append('Use at most 1000 characters (generation worker limit).')
    if not text.strip() or not all(
        ' ' <= c <= '~' or c in '‘’“”–—…' or
        (c.isalpha() and unicodedata.name(c, '').startswith('LATIN '))
        for c in unicodedata.normalize('NFC', text)
    ):
        errors.append('Use a single paragraph with English/Latin letters and ordinary punctuation; no line breaks or non-Latin scripts.')
    if type(duration) is not int or not 1 <= duration <= 120:
        errors.append('duration must be an integer from 1 to 120 seconds.')
    return dict(format_valid=not errors, errors=errors, word_count=words,
                character_count=len(text), duration=duration,
                semantic_review_required=True, motion_quality='unverified',
                next_step='Review the semantic checklist in get_prompt_guidance before submitting; this checker cannot judge meaning, collision, balance or dynamics.',
                harness_version=HARNESS_VERSION, policy_version=POLICY_VERSION)
