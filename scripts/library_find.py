"""Ranked library search: motion IDs, fuzzy English keywords and cross-lingual
semantic matches in one query box, over every motion the caller may see.

The existing /api/library/search (Gadget-compatible, keyword-all-terms,
bounded windows, no model calls) is unchanged. This module backs
/api/library/find for the desktop and browser workbench.

- Motion IDs: exact > prefix > substring, with or without the leading "m".
- Keywords: each Latin token matches as a substring, with simple stem variants
  (turn/turns/turning/turned), so word forms need not match exactly.
- Semantics: the query is embedded once with the catalog's multilingual
  embedding model and compared with every stored prompt vector (exact cosine,
  not HNSW), so a Chinese query such as "转弯" can match English prompts.
  One embedding call per distinct query; vectors are cached in-process and the
  call is billed to the searching user's existing API quota. If embedding is
  unavailable the search falls back to IDs and keywords and says so.
- Sorting: relevance, requested duration, or catalog order. Motion numbers are
  assigned incrementally when a motion is registered, so catalog order is the
  production order; it is not the exact generation-finish timestamp.

The ranked list is cached briefly per (user, query, filters, sort) so paging is
stable and cheap. Visibility matches the shared Gadget gallery: every generated
farm motion except removed ones. The author filter powers the "mine" view;
private imports and job permissions remain separate.
"""
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import closing
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SORTS = ('relevance', 'duration_desc', 'duration_asc', 'created_desc', 'created_asc')
# Similarity thresholds are not calibrated against labelled data yet. A result
# is kept when it is close to the best match and above an absolute floor.
SEMANTIC_FLOOR = 0.35
SEMANTIC_BAND = 0.18
_RESULT_TTL = 120
_RESULTS = OrderedDict()
_VECTORS = OrderedDict()
_VECTOR_REQUESTS = {}
_MATRIX = {}
_LOCK = threading.RLock()
_ID_LIKE = re.compile(r'm?[0-9a-z]*[0-9][0-9a-z]*')
_TOKEN = re.compile(r"[a-z0-9]+")


def _int(query, key, default, low, high):
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError('无效分页参数') from None
    if not low <= value <= high:
        raise ValueError('无效分页参数')
    return value


def parse(query):
    text = ' '.join(query.get('q', [''])[0].split())
    sort = query.get('sort', ['relevance'])[0] or 'relevance'
    topic = query.get('topic', [''])[0]
    from g2h.direction_index import STATUSES
    status = query.get('status', [''])[0]
    author = query.get('author', [''])[0]
    if len(text) > 200 or sort not in SORTS or status not in STATUSES or len(author) > 40:
        raise ValueError('无效搜索参数')
    return dict(q=text, sort=sort, topic=topic, status=status, author=author,
                after=_int(query, 'after', 0, 0, 10_000_000), limit=_int(query, 'limit', 30, 1, 50),
                keyword_only=bool(_int(query, 'keyword_only', 0, 0, 1)))


def id_score(motion_id, q):
    """Score a motion ID against an ID-like query; 0 when it does not match."""
    q = q.lower()
    bare, mid = q[1:] if q.startswith('m') else q, motion_id.lower()
    if q == mid:
        return 3.0
    if mid.startswith(q) or mid[1:].startswith(bare):
        return 2.0
    # Numeric fragments also match without leading zeros: "a1" finds m0000000a1.
    if bare and bare in mid[1:]:
        return 1.0 if mid[1:].lstrip('0').startswith(bare.lstrip('0') or '0') else 0.5
    return 0.0


def _stems(token):
    forms = {token}
    for suffix in ('ing', 'ed', 'es', 's'):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            base = token[:-len(suffix)]
            forms.add(base)
            if suffix in ('ing', 'ed') and len(base) >= 4 and base[-1] == base[-2]:
                forms.add(base[:-1])  # stepped -> step, running -> run
    return forms


def keyword_score(text, tokens):
    """Fraction of query tokens present in the text, allowing word-form variants."""
    if not tokens:
        return 0.0
    haystack = text.casefold()
    hits = sum(1 for t in tokens if any(form in haystack for form in _stems(t)))
    return hits / len(tokens)


def _latin_tokens(q):
    return [t for t in _TOKEN.findall(q.casefold()) if len(t) >= 2]


def _needs_semantic(q, tokens):
    # Any non-ASCII text (e.g. Chinese) or multi-word phrases benefit from it;
    # a single ID-like token does not.
    if not q:
        return False
    if any(ord(c) > 127 for c in q):
        return True
    # Short Latin fragments (typing in progress, "tu") are not worth a billed call.
    if len(q) < 3:
        return False
    return not (len(tokens) == 1 and _ID_LIKE.fullmatch(tokens[0]))


def _matrix(db, generation=None):
    """All prompt vectors as one normalised float32 matrix, appended incrementally."""
    import numpy as np
    config = db.execute('SELECT model,dimensions FROM embedding_config WHERE singleton=1').fetchone()
    if not config:
        raise LookupError('语义索引尚未配置')
    model, dimensions = config
    key = (str(db.execute('PRAGMA database_list').fetchone()[2]), model, dimensions, generation)
    with _LOCK:
        for old in list(_MATRIX):
            if old[0] == key[0] and old != key:
                del _MATRIX[old]
        state = _MATRIX.get(key) or dict(cursor=0, ids={}, chunks=[], matrix=np.zeros((0, dimensions), '<f4'))
        rows = json.loads(db.execute('SELECT json_group_array(json_array(number,text_id,hex(vector))) '
            'FROM (SELECT number,text_id,vector FROM embeddings WHERE number>? ORDER BY number)',
            (state['cursor'],)).fetchone()[0])
        if rows:
            values = np.stack([np.frombuffer(bytes.fromhex(v), dtype='<f4') for _, _, v in rows])
            if values.shape[1] != dimensions:
                raise LookupError('向量维度与配置不一致')
            base = len(state['matrix'])
            for offset, (_, text_id, _) in enumerate(rows):
                state['ids'][text_id] = base + offset
            state['matrix'] = np.concatenate([state['matrix'], values])
            state['cursor'] = rows[-1][0]
        _MATRIX[key] = state
        return model, dimensions, state


def query_vector(store, user, q, model, dimensions):
    """Embed a query once; repeats are served from cache and not billed again."""
    from g2h import semantic_index
    from g2h.api_usage import billed_to, check_quota

    key = (model, dimensions, q.casefold())
    username = user['username']
    usage = Path(store.root).resolve() / 'review/api_usage.sqlite3'
    request_key = (usage, username, key)
    with _LOCK:
        if key in _VECTORS:
            _VECTORS.move_to_end(key)
            return _VECTORS[key]
        pending = _VECTOR_REQUESTS.get(request_key)
        if pending is None:
            request = _VECTOR_REQUESTS[request_key] = Future()
    if pending is not None:
        return pending.result()

    try:
        check_quota(username, user.get('role', 'user'), path=usage)
        with billed_to(username, usage, purpose='library_search', provider='dashscope'):
            vector = semantic_index.embed([q], Path.home() / '.config/gen2humanoid/dashscope_api_key', model, dimensions)[0]
        with _LOCK:
            _VECTORS[key] = vector
            while len(_VECTORS) > 2048:
                _VECTORS.popitem(last=False)
    except BaseException as exc:
        request.set_exception(exc)
        raise
    else:
        request.set_result(vector)
        return vector
    finally:
        with _LOCK:
            del _VECTOR_REQUESTS[request_key]


def _reason(exc):
    name = type(exc).__name__
    if name == 'QuotaExceeded':
        return str(exc)
    if isinstance(exc, (ImportError, LookupError, RuntimeError)):
        return '语义检索暂不可用，已按编号和英文关键词匹配'
    return '语义检索请求失败，已按编号和英文关键词匹配'


def _rank_index(store, user, p, data, mask):
    import numpy as np
    from library_directions import constrain
    direction_mask = constrain(p['q'], data['text_directions'])
    if direction_mask is not None:
        mask = mask & direction_mask[data['prompt_index']]
    selected = np.flatnonzero(mask)
    semantic = dict(used=False)
    if not len(selected):
        return selected, np.zeros(0), np.zeros(0, np.uint8), semantic
    prompt_indices = data['prompt_index'][selected]
    eligible = np.unique(prompt_indices)
    tokens = _latin_tokens(p['q'])
    words = np.zeros(len(data['prompts']), np.float64)
    if tokens:
        for i in eligible:
            words[i] = keyword_score(data['prompts'][i][1] or '', tokens)
    words = words[prompt_indices]
    sims = np.full(len(selected), -1.0, np.float32)
    if not p['keyword_only'] and _needs_semantic(p['q'], tokens):
        try:
            with closing(store.catalog()) as db:
                model, dimensions, vectors = _matrix(db, data['serial'])
            vector = np.asarray(query_vector(store, user, p['q'], model, dimensions), '<f4')
            positions = np.asarray([vectors['ids'].get(row[0], -1) for row in data['prompts']])
            present = positions[prompt_indices] >= 0
            values = vectors['matrix'] @ vector
            sims[present] = values[positions[prompt_indices[present]]]
            semantic = dict(used=True, model=model, missing_vectors=int((~present).sum()))
        except Exception as exc:
            semantic = dict(used=False, reason=_reason(exc))
    ids = np.zeros(len(selected), np.float64)
    idq = p['q'].casefold().replace(' ', '')
    if idq and ' ' not in p['q'].strip() and _ID_LIKE.fullmatch(idq):
        bare = idq[1:] if idq.startswith('m') else idq
        mids, bases = data['ids'][selected], data['bare_ids'][selected]
        contains = np.char.find(bases, bare) >= 0
        fragment = np.char.startswith(data['trimmed_ids'][selected], bare.lstrip('0') or '0')
        ids[contains] = np.where(fragment[contains], 1.0, .5)
        ids[np.char.startswith(mids, idq) | np.char.startswith(bases, bare)] = 2.0
        ids[mids == idq] = 3.0
    scores = 10 * ids + words + np.maximum(sims.astype(np.float64), 0)
    threshold = max(SEMANTIC_FLOOR, float(sims.max()) - SEMANTIC_BAND)
    keep = (ids > 0) | (words > 0) | (sims >= threshold) if p['q'] else np.ones(len(selected), bool)
    selected, scores = selected[keep], scores[keep]
    matches = np.where(ids[keep] > 0, 1, np.where(words[keep] > 0, 2, 3)).astype(np.uint8)
    if not p['q']:
        matches[:] = 0
    sort = 'created_desc' if p['sort'] == 'relevance' and not p['q'] else p['sort']
    numbers = data['numbers'][selected]
    if sort == 'relevance':
        order = np.lexsort((-numbers, -scores))
    elif sort.startswith('duration'):
        durations = data['durations'][data['prompt_index'][selected]]
        sign = -1 if sort == 'duration_desc' else 1
        order = np.lexsort((-numbers, -scores, sign * np.nan_to_num(durations), np.isnan(durations)))
    else:
        order = np.argsort(numbers if sort == 'created_asc' else -numbers)
    return selected[order], scores[order], matches[order], semantic


def find(store, user, query):
    p = parse(query)
    topics = {t['id'] for t in store.topics()}
    if p['topic'] and p['topic'] not in topics:
        raise ValueError('未知主题')
    from library_search_index import snapshot, filters
    data = snapshot(store)
    mask, fresh = filters(store, data, p)
    from g2h.direction_index import summary
    progress=summary(store.root)
    key = (str(store.path), user['username'], (p['q'].casefold(), p['keyword_only']), p['sort'], p['topic'], p['status'],
           p['author'], data['serial'], fresh)
    now = time.monotonic()
    with _LOCK:
        cached = _RESULTS.get(key)
        if cached and now - cached[0] < _RESULT_TTL:
            _RESULTS.move_to_end(key)
            _, ranked = cached
        else:
            cached = None
    if cached is None:
        ranked = _rank_index(store, user, p, data, mask)
        with _LOCK:
            for old in list(_RESULTS):
                if old[0] == key[0] and old[7:] != key[7:]:
                    del _RESULTS[old]
            # A transient embedding/quota failure must be retried, not cached
            # as an empty semantic search for the whole result TTL.
            if p['keyword_only'] or not _needs_semantic(p['q'], _latin_tokens(p['q'])) or ranked[3].get('used'):
                _RESULTS[key] = (now, ranked)
            while len(_RESULTS) > 64:
                _RESULTS.popitem(last=False)
    indices, scores, matches, semantic = ranked
    page = []
    for i in range(p['after'], min(p['after'] + p['limit'], len(indices))):
        pos = indices[i]
        motion = data['motions'][pos]
        prompt = data['prompts'][data['prompt_index'][pos]]
        row = (*motion, prompt[1], prompt[2], None, None)
        page.append((row, float(scores[i]), (None, 'id', 'keyword', 'semantic')[matches[i]]))
    items = _page_items(store, page)
    nxt = p['after'] + p['limit']
    return dict(items=items, total=len(indices), next_after=nxt if nxt < len(indices) else None,
                sort=p['sort'], semantic=semantic, search_mode='ranked_v1',direction_progress=progress)


def warm(store):
    """Load local retrieval metadata before serving; never call a paid model."""
    from library_search_index import snapshot
    data = snapshot(store)
    try:
        with closing(store.catalog()) as db:
            _matrix(db, data['serial'])
    except (sqlite3.Error, LookupError):
        pass  # A catalog without vectors still supports ID/keyword search.


def _page_items(store, page):
    from g2h.direction_index import statuses
    from g2h.farm_index import motion_info, short_id
    from review_access import public_motion
    if not page:
        return []
    ids = [short_id(row[0]) for row, _, _ in page]
    directions = statuses(store.root, ids)
    with closing(store.reviews()) as db:
        marks = ','.join('?' for _ in ids)
        reviews = {r[0]: r[1] for r in db.execute(f'SELECT motion_id,body FROM latest WHERE motion_id IN ({marks})', ids)}
        try:
            from review_research import VERSION
            quality = {r[0]: r[1] for r in db.execute(
                f'SELECT motion_id,status FROM quality_reports WHERE version=? AND motion_id IN ({marks})', [VERSION, *ids])}
        except (ImportError, sqlite3.Error):
            quality = {}
        try:
            owners = {r[0]: (r[1], r[2]) for r in db.execute(
                f'SELECT motion_id,username,request_id FROM motion_owners WHERE motion_id IN ({marks})', ids)}
        except sqlite3.Error:
            owners = {}
    import json
    items = []
    for (row, score, match), mid in zip(page, ids):
        info = motion_info(store.root, (row[0], row[1], row[2], row[3], 0))
        owner, request_id = owners.get(mid, (row[7], None))
        info.update(text=row[4], review=json.loads(reviews[mid]) if mid in reviews else None, owner=owner, request_id=request_id)
        info['direction_status'] = directions.get(mid, 'not_checked')
        item = public_motion(info)
        item.update(duration=row[5], quality_status=quality.get(mid, 'not_checked'), match=match,
                    score=round(score, 4) if match else None)
        items.append(item)
    from review_tags import attach
    attach(store, items)
    return items
