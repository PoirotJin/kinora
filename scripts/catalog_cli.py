"""Bounded CLI queries and explicit incremental index migration."""
import fcntl
import json
import os
from pathlib import Path
import re

from g2h import farm_index as ids, semantic_index as vectors

COMMANDS = {'text','motion','search','index-data','topic-add','texts'}


def embedding_options(parser):
    parser.add_argument('--embedding-model',default=vectors.MODEL)
    parser.add_argument('--dimensions',type=int,default=vectors.DIMENSIONS)
    parser.add_argument('--key-file',type=Path,default=Path.home()/'.config/gen2humanoid/dashscope_api_key')


def add_commands(sub):
    text = sub.add_parser('text',help='Find text, vector metadata and its motion IDs')
    text.add_argument('text_id')
    text.add_argument('--limit',type=int,default=100)
    text.add_argument('--after',type=int,default=0,help='Motion number pagination cursor')
    text.add_argument('--include-vector',action='store_true')
    listing = sub.add_parser('texts',help='List texts by topic with cursor pagination')
    listing.add_argument('--limit',type=int,default=20)
    listing.add_argument('--after',type=int,default=0)
    motion = sub.add_parser('motion',help='Find a motion and direct shard byte location')
    motion.add_argument('motion_id')
    search = sub.add_parser('search',help='Semantic search within one selected topic')
    search.add_argument('text')
    search.add_argument('--top-k',type=int,default=5)
    embedding_options(search)
    index = sub.add_parser('index-data',help='Incrementally backfill IDs and embeddings; bounded API usage')
    index.add_argument('--limit',type=int,default=100)
    index.add_argument('--metadata-only',action='store_true')
    embedding_options(index)
    topic = sub.add_parser('topic-add',help='Add a new topic without changing existing IDs')
    topic.add_argument('topic_id')
    topic.add_argument('--name',required=True)
    topic.add_argument('--definition',required=True)


def add_topic(args):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',args.topic_id):
        raise ValueError('Topic ID must be a safe lowercase identifier')
    if not args.name.strip() or not args.definition.strip():
        raise ValueError('Topic name and definition must be nonempty')
    with args.config.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data = json.loads(args.config.read_text())
        if any(t['id']==args.topic_id for t in data['topics']):
            raise ValueError('Topic already exists')
        data['topics'].append(dict(id=args.topic_id,name=args.name,definition=args.definition))
        temporary = args.config.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,args.config)
    return {'added':args.topic_id}


def execute(args, topics):
    import MotionFarm as farm
    if hasattr(args,'limit') and not 1<=args.limit<=1000:
        raise ValueError('limit must be 1..1000')
    if getattr(args,'after',0)<0:
        raise ValueError('after must be nonnegative')
    if args.command=='topic-add':
        print(json.dumps(add_topic(args),ensure_ascii=False))
        return
    db = farm.catalog(args.root)
    try:
        if args.command=='index-data':
            with (args.root/'prompt_generation.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                migrated = ids.sync_legacy(db,args.root,topics,args.limit)
                embedded = 0
                if not args.metadata_only:
                    embedded = vectors.backfill(db,args.key_file,args.embedding_model,args.dimensions,args.limit,topics)
                    for topic in topics:
                        row = db.execute('SELECT vector FROM embeddings WHERE topic=? LIMIT 1',(topic['id'],)).fetchone()
                        if row:
                            import numpy as np
                            vectors.nearest(db,args.root,topic['id'],[np.frombuffer(row[0],dtype='<f4')],1)
                result = {'migrated_tasks':migrated,'embedded':embedded,
                    'remaining_embeddings':db.execute('SELECT COUNT(*) FROM embedding_pending').fetchone()[0]}
        elif args.command=='text':
            row = db.execute('SELECT item FROM prompts WHERE id=?',(args.text_id,)).fetchone()
            if not row:
                raise ValueError('Unknown text ID')
            result = {'text_id':args.text_id,'prompt':json.loads(row[0])}
            rows = db.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links WHERE text_id=? '
                'AND number>? ORDER BY number LIMIT ?',(args.text_id,args.after,args.limit+1)).fetchall()
            result['motions'] = [ids.motion_info(args.root,r) for r in rows[:args.limit]]
            result['next_after'] = rows[args.limit-1][0] if len(rows)>args.limit else None
            vector = db.execute('SELECT number,vector FROM embeddings WHERE text_id=?',(args.text_id,)).fetchone()
            result['embedding'] = None
            if vector:
                model,dimensions = db.execute('SELECT model,dimensions FROM embedding_config').fetchone()
                result['embedding'] = dict(vector_id=vector[0],model=model,dimensions=dimensions)
                if args.include_vector:
                    import numpy as np
                    result['embedding']['values'] = np.frombuffer(vector[1],dtype='<f4').tolist()
        elif args.command=='motion':
            result = ids.lookup_motion(db,args.root,args.motion_id)
            result['prompt'] = json.loads(db.execute('SELECT item FROM prompts WHERE id=?',(result['text_id'],)).fetchone()[0])
        elif args.command=='texts':
            if len(topics)!=1:
                raise ValueError('Select exactly one --topic for texts')
            rows = db.execute('SELECT rowid,item FROM prompts WHERE topic=? AND rowid>? ORDER BY rowid LIMIT ?',
                (topics[0]['id'],args.after,args.limit+1)).fetchall()
            result = dict(texts=[json.loads(r[1]) for r in rows[:args.limit]],
                next_after=rows[args.limit-1][0] if len(rows)>args.limit else None)
        elif args.command=='search':
            if len(topics)!=1 or not 1<=args.top_k<=20:
                raise ValueError('Select exactly one --topic and top-k 1..20')
            vectors.configure(db,args.embedding_model,args.dimensions)
            vectors.require_complete(db,topics)
            query = vectors.embed([args.text],args.key_file,args.embedding_model,args.dimensions)
            result = dict(topic=topics[0]['id'],matches=vectors.nearest(db,args.root,topics[0]['id'],query,args.top_k)[0],
                          approximate=True)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        db.close()
