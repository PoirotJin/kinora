"""Ephemeral advisory presence; review revision checks remain authoritative."""
import re
import threading
import time

_LOCK = threading.Lock()
_TTL = 65
_MAX = 4096


def presence(store, motion_id, user, value=None, now=None):
    # Apply the same visibility/removal checks as the detail endpoint.
    store.detail(motion_id)
    now = time.monotonic() if now is None else now
    with _LOCK:
        entries = getattr(store, '_review_presence', None)
        if entries is None:
            entries = store._review_presence = {}
        for key in list(entries):
            if now - entries[key]['seen'] > _TTL:
                del entries[key]
        if value is not None:
            if not isinstance(value, dict) or set(value) != {'client', 'active'}:
                raise ValueError('Invalid presence request')
            client = value['client']
            if not isinstance(client, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', client) or type(value['active']) is not bool:
                raise ValueError('Invalid presence request')
            key = (motion_id, user['username'], client)
            if value['active']:
                if key not in entries and len(entries) >= _MAX:
                    raise ValueError('在线审核人数已达上限，请稍后重试')
                entries[key] = {'seen': now}
            else:
                entries.pop(key, None)
        users = sorted({key[1] for key in entries if key[0] == motion_id})
    return {'reviewers': users, 'expires_after_seconds': _TTL, 'advisory': True}
