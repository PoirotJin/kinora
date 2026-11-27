"""Frozen pre-index search implementation used only as a result-parity oracle."""
from collections import OrderedDict
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
                after=_int(query, 'after', 0, 0, 10_000_000), limit=_int(query, 'limit', 30, 1, 50))


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


def _matrix(db):
    """All prompt vectors as one normalised float32 matrix, appended incrementally."""
    import numpy as np
    config = db.execute('SELECT model,dimensions FROM embedding_config WHERE singleton=1').fetchone()
    if not config:
        raise LookupError('语义索引尚未配置')
    model, dimensions = config
    key = (str(db.execute('PRAGMA database_list').fetchone()[2]), model, dimensions)
    with _LOCK:
        state = _MATRIX.get(key) or dict(cursor=0, ids={}, chunks=[], matrix=np.zeros((0, dimensions), '<f4'))
        rows = db.execute('SELECT number,text_id,vector FROM embeddings WHERE number>? ORDER BY number',
                          (state['cursor'],)).fetchall()
        if rows:
            values = np.stack([np.frombuffer(v, dtype='<f4') for _, _, v in rows])
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
    key = (model, dimensions, q.casefold())
    with _LOCK:
        if key in _VECTORS:
            _VECTORS.move_to_end(key)
            return _VECTORS[key]
    from g2h import semantic_index
    from g2h.api_usage import billed_to, check_quota
    usage = Path(store.root) / 'review/api_usage.sqlite3'
    check_quota(user['username'], user.get('role', 'user'), path=usage)
    with billed_to(user['username'], usage, purpose='library_search', provider='dashscope'):
        vector = semantic_index.embed([q], Path.home() / '.config/gen2humanoid/dashscope_api_key', model, dimensions)[0]
    with _LOCK:
        _VECTORS[key] = vector
        while len(_VECTORS) > 2048:
            _VECTORS.popitem(last=False)
    return vector


def _generated(root, topics):
    """Task IDs with a committed motion record, per topic index."""
    done = set()
    for topic in topics:
        path = Path(root) / 'topics' / topic / 'index.sqlite3'
        if not path.exists():
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            # Transfer one JSON value instead of releasing/reacquiring the GIL
            # for each SQLite row while background service threads are busy.
            done.update(json.loads(db.execute('SELECT json_group_array(id) FROM records').fetchone()[0]))
    return done


def _review_tables(db):
    """Convert the small review tables once, not each motion through a Python UDF.

    Request-local temp tables preserve fresh permissions without schema changes or
    a shared permission cache. SQLite can then join integer primary keys in C.
    """
    from g2h.farm_index import motion_number
    for source, target, field in (
        ('latest', 'find_reviews', 'status'),
        ('motion_owners', 'find_owners', 'username'),
        ('removed', 'find_removed', None),
    ):
        db.execute(f'CREATE TEMP TABLE {target}(number INTEGER PRIMARY KEY' +
                   (f', {field} TEXT)' if field else ')'))
        rows = []
        for row in db.execute(f'SELECT motion_id' + (f', {field}' if field else '') +
                              f' FROM reviewdb.{source}'):
            try:
                number = motion_number(row[0])
            except ValueError:
                continue  # Imported IDs are outside the motion_links catalog.
            if number > 2**63 - 1:
                continue
            rows.append((number, *row[1:]))
        db.executemany(f'INSERT INTO {target} VALUES (' + ('?,?' if field else '?') + ')', rows)


def _candidates(store, user, p):
    topics = {t['id'] for t in store.topics()}
    if p['topic'] and p['topic'] not in topics:
        raise ValueError('未知主题')
    with closing(store.catalog()) as db:
        db.execute('ATTACH DATABASE ? AS reviewdb', (str(store.path),))
        _review_tables(db)
        sql = '''SELECT json_group_array(json_array(m.number,m.task_id,m.text_id,m.topic,json_extract(p.item,'$.text'),
                   json_extract(p.item,'$.duration_s'),r.status,o.username))
                 FROM motion_links m JOIN prompts p ON p.id=m.text_id
                 LEFT JOIN find_reviews r ON r.number=m.number
                 LEFT JOIN find_owners o ON o.number=m.number
                 WHERE NOT EXISTS(SELECT 1 FROM find_removed d WHERE d.number=m.number)'''
        args = []
        from g2h import direction_index
        direction_index.attach(db,store.root)
        direction_sql,direction_args=direction_index.clause(p['status'])
        sql+=direction_sql;args+=direction_args
        if p['topic']:
            sql += ' AND m.topic=?'; args.append(p['topic'])
        if p['author']:
            sql += ' AND o.username=?'; args.append(p['author'])
        if p['status'] == 'pending':
            sql += ' AND r.number IS NULL'
        elif p['status'] and p['status'] not in direction_index.FILTERS:
            sql += ' AND r.status=?'; args.append(p['status'])
        # One native transfer avoids hundreds of thousands of GIL handoffs
        # to the storage monitor. Independent requests can scan concurrently.
        rows = json.loads(db.execute(sql, args).fetchone()[0])
        used_topics = {r[3] for r in rows}
        done = _generated(store.root, used_topics)
        rows = [r for r in rows if r[1] in done]
        semantic = dict(used=False)
        sims = None
        tokens = _latin_tokens(p['q'])
        if _needs_semantic(p['q'], tokens) and rows:
            try:
                import numpy as np
                model, dimensions, state = _matrix(db)
                vector = np.asarray(query_vector(store, user, p['q'], model, dimensions), '<f4')
                index = np.array([state['ids'].get(r[2], -1) for r in rows])
                scores = np.full(len(rows), -1.0, dtype='<f4')
                present = index >= 0
                if present.any():
                    # Many generated motions share a prompt. Score each stored
                    # vector once, avoiding a full vector copy for every motion.
                    prompt_scores = state['matrix'] @ vector
                    scores[present] = prompt_scores[index[present]]
                sims = scores
                semantic = dict(used=True, model=model, missing_vectors=int((~present).sum()))
            except Exception as exc:  # degrade to IDs/keywords; never fail the search
                semantic = dict(used=False, reason=_reason(exc))
    return rows, sims, semantic, tokens


def _reason(exc):
    name = type(exc).__name__
    if name == 'QuotaExceeded':
        return str(exc)
    if isinstance(exc, (ImportError, LookupError, RuntimeError)):
        return '语义检索暂不可用，已按编号和英文关键词匹配'
    return '语义检索请求失败，已按编号和英文关键词匹配'


def rank(rows, sims, q, tokens, sort):
    """Return [(row, score, match)] for relevant rows in the requested order."""
    from g2h.farm_index import short_id
    idq = q.casefold().replace(' ', '')
    id_like = bool(idq) and ' ' not in q.strip() and bool(_ID_LIKE.fullmatch(idq))
    best = float(max(sims)) if sims is not None and len(sims) else None
    threshold = max(SEMANTIC_FLOOR, best - SEMANTIC_BAND) if best is not None else None
    ranked = []
    word_scores = {}
    for i, row in enumerate(rows):
        if not q:
            ranked.append((row, 0.0, None))
            continue
        ids = id_score(short_id(row[0]), idq) if id_like else 0.0
        if row[2] not in word_scores:
            word_scores[row[2]] = keyword_score(row[4] or '', tokens)
        words = word_scores[row[2]]
        sim = float(sims[i]) if sims is not None else None
        semantic = sim is not None and sim >= threshold
        if not (ids or words or semantic):
            continue
        score = 10 * ids + words + (sim if sim is not None and sim > 0 else 0)
        match = 'id' if ids else 'keyword' if words else 'semantic'
        ranked.append((row, score, match))
    if sort == 'relevance' and not q:
        sort = 'created_desc'
    if sort == 'relevance':
        ranked.sort(key=lambda x: (-x[1], -x[0][0]))
    elif sort.startswith('duration'):
        sign = -1 if sort == 'duration_desc' else 1
        # Unknown durations always go last; ties keep relevance, then newest.
        ranked.sort(key=lambda x: (x[0][5] is None, sign * float(x[0][5] or 0), -x[1], -x[0][0]))
    else:
        ranked.sort(key=lambda x: x[0][0], reverse=sort == 'created_desc')
    return ranked


def find(store, user, query):
    p = parse(query)
    from g2h.direction_index import summary
    progress=summary(store.root)
    key = (str(store.path), user['username'], p['q'].casefold(), p['sort'], p['topic'], p['status'], p['author'], progress['checked'])
    now = time.monotonic()
    with _LOCK:
        cached = _RESULTS.get(key)
        if cached and now - cached[0] < _RESULT_TTL and p['after']:
            _RESULTS.move_to_end(key)
            _, ranked, semantic = cached
        else:
            cached = None
    if cached is None:
        rows, sims, semantic, tokens = _candidates(store, user, p)
        ranked = rank(rows, sims, p['q'], tokens, p['sort'])
        with _LOCK:
            _RESULTS[key] = (now, ranked, semantic)
            while len(_RESULTS) > 64:
                _RESULTS.popitem(last=False)
    page = ranked[p['after']:p['after'] + p['limit']]
    items = _page_items(store, page)
    nxt = p['after'] + p['limit']
    return dict(items=items, total=len(ranked), next_after=nxt if nxt < len(ranked) else None,
                sort=p['sort'], semantic=semantic, search_mode='ranked_v1',direction_progress=progress)


def _page_items(store, page):
    from g2h.farm_index import motion_info, short_id
    from review_access import public_motion
    if not page:
        return []
    ids = [short_id(row[0]) for row, _, _ in page]
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
        info.pop('location', None)
        owner, request_id = owners.get(mid, (row[7], None))
        info.update(text=row[4], review=json.loads(reviews[mid]) if mid in reviews else None, owner=owner, request_id=request_id)
        item = public_motion(info)
        item.update(duration=row[5], quality_status=quality.get(mid, 'not_checked'), match=match,
                    score=round(score, 4) if match else None)
        items.append(item)
    from review_tags import attach
    attach(store, items)
    return items
