"""Durable float32 embeddings plus rebuildable per-topic FAISS HNSW indexes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict

MODEL = 'text-embedding-v4'
DIMENSIONS = 512
BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
_CACHE = OrderedDict()
_CACHE_BYTES = 2 * 1024 * 1024 * 1024
_SNAPSHOT_BATCH = 256
_SNAPSHOT_SECONDS = 60
_SNAPSHOT_MIN_BATCH = 16


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS embedding_config(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), model TEXT, dimensions INTEGER);
      CREATE TABLE IF NOT EXISTS embeddings(
        number INTEGER PRIMARY KEY AUTOINCREMENT, text_id TEXT UNIQUE NOT NULL REFERENCES prompts(id),
        topic TEXT NOT NULL, vector BLOB NOT NULL);
      CREATE INDEX IF NOT EXISTS embedding_topic ON embeddings(topic,number);
      CREATE TABLE IF NOT EXISTS embedding_pending(text_id TEXT PRIMARY KEY,topic TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS pending_topic ON embedding_pending(topic);
      CREATE TRIGGER IF NOT EXISTS prompt_needs_vector AFTER INSERT ON prompts BEGIN
        INSERT OR IGNORE INTO embedding_pending VALUES(new.id,new.topic);
      END;
      CREATE TRIGGER IF NOT EXISTS vector_completed AFTER INSERT ON embeddings BEGIN
        DELETE FROM embedding_pending WHERE text_id=new.text_id;
      END;
    ''')
    if not db.execute("SELECT 1 FROM index_meta WHERE key='embedding_queue_v1'").fetchone():
        with db:
            db.execute('''INSERT OR IGNORE INTO embedding_pending SELECT p.id,p.topic FROM prompts p
                LEFT JOIN embeddings e ON e.text_id=p.id WHERE e.text_id IS NULL''')
            db.execute("INSERT INTO index_meta VALUES('embedding_queue_v1','1')")
    from g2h.db_stats import embeddings
    embeddings(db)


def configure(db, model, dimensions):
    if not model or dimensions not in (64,128,256,512,768,1024,1536,2048):
        raise ValueError('Invalid embedding configuration')
    with db:
        db.execute('INSERT OR IGNORE INTO embedding_config VALUES(1,?,?)', (model,dimensions))
    current = db.execute('SELECT model,dimensions FROM embedding_config WHERE singleton=1').fetchone()
    if current != (model,dimensions):
        raise ValueError('Embedding model/dimension mismatch; use the existing configuration or a new root')


def normalized_vector(value, dimensions):
    import numpy as np
    vector = np.asarray(value, dtype='<f4')
    if vector.shape != (dimensions,) or not np.isfinite(vector).all():
        raise ValueError('Invalid embedding shape or nonfinite values')
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:
        raise ValueError('Zero embedding vector')
    return np.ascontiguousarray(vector / norm, dtype='<f4')


def embed(texts, key_file, model=MODEL, dimensions=DIMENSIONS):
    """Up to 10 texts per call; production supervisor owns recovery."""
    from g2h.api_errors import NoRedirect, TemporaryAPIError, http_failure
    if not texts:
        return []
    if len(texts)>30 or any(not isinstance(t,str) or not 1<=len(t)<=4000 for t in texts):
        raise ValueError('Embedding request exceeds bounded input limits')
    key = Path(key_file).read_text().strip()
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError('Invalid API key file')
    result = []
    for start in range(0,len(texts),10):
        batch = texts[start:start+10]
        body = dict(model=model,input=batch,dimensions=dimensions,encoding_format='float')
        request = urllib.request.Request(BASE_URL+'/embeddings', data=json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        from g2h.api_usage import budgeted_request,attributed_call
        with attributed_call('embedding','dashscope'), budgeted_request(model, 'embedding', batch):
            try:
                with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:
                    raw = response.read(2_000_001)
            except urllib.error.HTTPError as exc:
                from g2h.api_usage import record
                record(model,'embedding',status='http_'+str(exc.code))
                raise http_failure(exc, 'Embedding') from None
            except urllib.error.URLError:
                from g2h.api_usage import record
                record(model,'embedding',status='network_error')
                raise TemporaryAPIError('Embedding network failure') from None
            except (TimeoutError, ConnectionError):
                from g2h.api_usage import record
                record(model,'embedding',status='transport_error')
                raise TemporaryAPIError('Embedding request timed out or connection interrupted') from None
            if len(raw)>2_000_000:
                raise ValueError('Embedding response too large')
            payload = json.loads(raw)
            from g2h.api_usage import record
            record(model,'embedding',payload)
        data = payload['data']
        if len(data)!=len(batch) or sorted(d['index'] for d in data)!=list(range(len(batch))):
            raise ValueError('Embedding response indices mismatch')
        result.extend(normalized_vector(d['embedding'],dimensions) for d in sorted(data,key=lambda d:d['index']))
    return result


def save_vector(db, item, vector, dimensions):
    value = normalized_vector(vector,dimensions)
    if not db.execute('SELECT 1 FROM prompts WHERE id=? AND topic=?', (item['id'],item['topic_id'])).fetchone():
        raise ValueError('Embedding refers to missing text')
    db.execute('INSERT OR IGNORE INTO embeddings(text_id,topic,vector) VALUES(?,?,?)',
                (item['id'],item['topic_id'],value.tobytes()))


def backfill(db, key_file, model=MODEL, dimensions=DIMENSIONS, limit=100, topics=None):
    configure(db,model,dimensions)
    # Keep both memory and API spend bounded; committed batches survive interruptions.
    count = 0
    while count < limit:
        topic_ids = [t['id'] for t in topics] if topics else []
        where = (' WHERE q.topic IN ('+','.join('?' for _ in topic_ids)+')') if topic_ids else ''
        rows = db.execute('SELECT p.item FROM embedding_pending q JOIN prompts p ON p.id=q.text_id'+where+
            ' ORDER BY q.rowid LIMIT ?', (*topic_ids,min(10,limit-count))).fetchall()
        if not rows:
            break
        items = [json.loads(r[0]) for r in rows]
        vectors = embed([i['text'] for i in items],key_file,model,dimensions)
        with db:
            for item,vector in zip(items,vectors):
                save_vector(db,item,vector,dimensions)
        count += len(items)
        print(json.dumps({'embedded':count,'model':model,'dimensions':dimensions}),flush=True)
    return count


def require_complete(db, topics):
    for topic in topics:
        if db.execute('SELECT 1 FROM embedding_pending WHERE topic=? LIMIT 1',(topic['id'],)).fetchone():
            raise RuntimeError('Missing historical embeddings; run MotionFarm.py index-data first')


def faiss_module():
    # Optional isolated wheel: never upgrade the inference environment's NumPy.
    vendor = Path(__file__).resolve().parents[1]/'.vendor'
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0,str(vendor))
    try:
        import faiss
    except ImportError:
        raise RuntimeError('FAISS is required; install requirements-index.txt into .vendor') from None
    faiss.omp_set_num_threads(2)
    return faiss


def nearest(db, root, topic, vectors, top_k=5):
    """Approximate cosine neighbors; IDs and full texts come from the SQL catalog."""
    import numpy as np
    if not 1<=top_k<=20:
        raise ValueError('top_k must be 1..20')
    config = db.execute('SELECT model,dimensions FROM embedding_config WHERE singleton=1').fetchone()
    if not config:
        raise ValueError('Embeddings have not been configured')
    model, dimensions = config
    query = np.stack([normalized_vector(v,dimensions) for v in vectors])
    faiss = faiss_module()
    directory = Path(root)/'vector_indexes'
    directory.mkdir(exist_ok=True)
    name = hashlib.sha256((topic+'\0'+model+'\0'+str(dimensions)).encode()).hexdigest()
    path = directory/(name+'.faiss')
    with (directory/(name+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        stamp = path.stat().st_mtime_ns if path.exists() else None
        cached = _CACHE.pop(str(path),None)
        if cached and cached[0] == stamp:
            _, index, cursor, _, persisted, saved_at = cached
        elif path.exists():
            index = faiss.read_index(str(path))
            if index.d != dimensions:
                raise ValueError('Vector index dimension mismatch')
            ids = faiss.vector_to_array(index.id_map)
            cursor = int(ids.max()) if len(ids) else 0
            persisted = int(index.ntotal)
            saved_at = path.stat().st_mtime
        else:
            base = faiss.IndexHNSWFlat(dimensions,16,faiss.METRIC_INNER_PRODUCT)
            base.hnsw.efConstruction = 100
            base.hnsw.efSearch = 64
            index = faiss.IndexIDMap2(base)
            cursor = 0
            persisted = 0
            saved_at = time.time()
        # The SQL store is authoritative. Index updates are append-only and atomic.
        changed = False
        while True:
            rows = db.execute('SELECT number,vector FROM embeddings WHERE topic=? AND number>? '
                              'ORDER BY number LIMIT 256',(topic,cursor)).fetchall()
            if not rows:
                break
            values = np.stack([np.frombuffer(v,dtype='<f4') for _,v in rows])
            index.add_with_ids(values,np.asarray([n for n,_ in rows],dtype='int64'))
            cursor = rows[-1][0]
            changed = True
        pending = int(index.ntotal)-persisted
        # SQLite is the durable append log. A restart replays every vector newer
        # than the persisted cursor; a crash before a snapshot loses no vectors.
        # The minimum batch avoids turning a slow one-prompt loop back into a
        # full-index write on every request merely because 60 seconds elapsed.
        if changed and (pending >= _SNAPSHOT_BATCH or
                        (pending >= _SNAPSHOT_MIN_BATCH and time.time()-saved_at >= _SNAPSHOT_SECONDS)):
            temp = path.with_suffix('.tmp')
            old_size = path.stat().st_size if path.exists() else 0
            budget = None
            if (Path(root)/'prompts.sqlite3').exists():
                from g2h.capacity import reservation
                estimate = int(index.ntotal)*(dimensions*4+320)*2+1_048_576
                budget = reservation(root,estimate,'faiss:'+topic)
            try:
                faiss.write_index(index,str(temp))
                with temp.open('rb') as stream:
                    os.fsync(stream.fileno())
                os.replace(temp,path)
                from g2h.cold_storage import sync_dir
                sync_dir(directory)
                persisted = int(index.ntotal)
                saved_at = time.time()
            finally:
                if temp.exists():
                    temp.unlink()
                if budget:
                    try: budget.commit((path.stat().st_size if path.exists() else 0)-old_size)
                    finally: budget.__exit__(None,None,None)
        # Keep an active large topic resident instead of dropping it at the old
        # 256 MiB cliff. The soft budget evicts other topics; at least the current
        # index stays cached. SQL replay makes dirty cache eviction safe.
        size = int(index.ntotal)*(dimensions*4+192)+65536
        _CACHE[str(path)] = (path.stat().st_mtime_ns if path.exists() else None,
                             index,cursor,size,persisted,saved_at)
        while len(_CACHE)>1 and sum(c[3] for c in _CACHE.values()) > _CACHE_BYTES:
            _CACHE.popitem(last=False)
        if not index.ntotal:
            return [[] for _ in vectors]
        distances, labels = index.search(query,min(top_k,index.ntotal))
    output = []
    for scores,ids in zip(distances,labels):
        matches = []
        for score,number in zip(scores,ids):
            if number<0:
                continue
            row = db.execute('SELECT p.id,p.item FROM embeddings e JOIN prompts p ON p.id=e.text_id '
                             'WHERE e.number=? AND e.topic=?',(int(number),topic)).fetchone()
            if not row:
                raise ValueError('Stale vector index; rebuild from the catalog')
            item = json.loads(row[1])
            matches.append(dict(text_id=row[0],text=item['text'],similarity=float(score)))
        output.append(matches)
    return output
