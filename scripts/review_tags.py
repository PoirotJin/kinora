"""Collaborative labels: immutable vocabulary, per-user assignments and audit trail."""
from contextlib import closing
import time
import unicodedata
from g2h.farm_index import motion_number


def setup(db):
    from review_collections import setup as setup_collections
    setup_collections(db)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS tag_groups(
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        created_by TEXT NOT NULL, created_at REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS tags(
        id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, name TEXT NOT NULL,
        name_key TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
        UNIQUE(group_id,name_key));
      CREATE TABLE IF NOT EXISTS motion_tags(
        motion_number INTEGER NOT NULL, tag_id INTEGER NOT NULL, username TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY(motion_number,tag_id,username));
      CREATE INDEX IF NOT EXISTS motion_tags_tag ON motion_tags(tag_id,motion_number,username);
      CREATE INDEX IF NOT EXISTS motion_tags_user ON motion_tags(username,motion_number,tag_id);
      CREATE INDEX IF NOT EXISTS tags_name ON tags(name_key,id);
      CREATE TABLE IF NOT EXISTS tag_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, motion_number INTEGER NOT NULL,
        tag_id INTEGER NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, at REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS tag_events_motion ON tag_events(motion_number,id);
    ''')


def label(value):
    if not isinstance(value,str):raise ValueError('标签组和标签必须是文本')
    value=unicodedata.normalize('NFKC',value).strip()
    if not 1<=len(value)<=64 or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('标签组和标签需为 1–64 个可见字符')
    return value,value.casefold()


def filter_sql(query, column='m.number'):
    """Build an indexed numeric candidate set; all fields match ONE assignment."""
    conditions=[];args=[]
    for key,col in [('tag_group','g.name_key'),('tag','t.name_key')]:
        value=query.get(key,[''])[0]
        if value:
            if key=='tag':
                conditions.append("""t.id IN (
                  SELECT id FROM reviewdb.tags INDEXED BY tags_name WHERE name_key=?
                  UNION SELECT t1.id FROM reviewdb.tag_aliases x JOIN reviewdb.tag_groups g1 ON g1.name_key=x.group_key JOIN reviewdb.tags t1 ON t1.group_id=g1.id AND t1.name_key=x.canonical_key WHERE x.alias_key=?
                  UNION SELECT t2.id FROM reviewdb.tag_aliases x JOIN reviewdb.tag_groups g2 ON g2.name_key=x.group_key JOIN reviewdb.tags t2 ON t2.group_id=g2.id AND t2.name_key=x.alias_key WHERE x.canonical_key=?
                  UNION SELECT t3.id FROM reviewdb.tag_aliases x JOIN reviewdb.tag_aliases y ON x.group_key=y.group_key AND x.canonical_key=y.canonical_key JOIN reviewdb.tag_groups g3 ON g3.name_key=x.group_key JOIN reviewdb.tags t3 ON t3.group_id=g3.id AND t3.name_key=x.alias_key WHERE y.alias_key=?)""")
                args.extend([label(value)[1]]*4)
            else:conditions.append(col+'=?');args.append(label(value)[1])
    username=query.get('tagged_by',[''])[0]
    if username:
        if not isinstance(username,str) or len(username)>40:raise ValueError('无效标注人')
        conditions.append('a.username=?');args.append(username)
    if not conditions:return '',[]
    return (' AND '+column+' IN (SELECT a.motion_number FROM reviewdb.motion_tags a '
            'JOIN reviewdb.tags t ON t.id=a.tag_id JOIN reviewdb.tag_groups g ON g.id=t.group_id '
            'WHERE '+' AND '.join(conditions)+')'),args


def attach(store,items):
    if not items:return
    numbers=[motion_number(x['motion_id']) for x in items]
    grouped={n:[] for n in numbers}
    with closing(store.reviews()) as db:
        rows=db.execute('''SELECT a.motion_number,t.id,g.name,t.name,a.username,a.created_at
          FROM motion_tags a JOIN tags t ON t.id=a.tag_id JOIN tag_groups g ON g.id=t.group_id
          WHERE a.motion_number IN ('''+','.join('?' for _ in numbers)+') ORDER BY g.name,t.name,a.username',numbers)
        for n,tid,group,tag,username,created in rows:
            grouped[n].append(dict(tag_id=tid,group=group,tag=tag,username=username,created_at=created))
    for item,n in zip(items,numbers):item['tags']=grouped[n]


def vocabulary(store,query):
    after=int(query.get('after',['0'])[0]);limit=int(query.get('limit',['100'])[0])
    if after<0 or not 1<=limit<=200:raise ValueError('无效标签分页')
    sql='SELECT t.id,g.name,t.name,t.created_by,t.created_at,g.created_by,g.created_at FROM tags t JOIN tag_groups g ON g.id=t.group_id WHERE t.id>?'
    args=[after]
    if query.get('tag_group',[''])[0]:
        sql+=' AND g.name_key=?';args.append(label(query['tag_group'][0])[1])
    with closing(store.reviews()) as db:rows=db.execute(sql+' ORDER BY t.id LIMIT ?',args+[limit+1]).fetchall()
    return dict(items=[dict(tag_id=r[0],group=r[1],tag=r[2],created_by=r[3],created_at=r[4],group_created_by=r[5],group_created_at=r[6]) for r in rows[:limit]],next_after=rows[limit-1][0] if len(rows)>limit else None)


def read(store,mid,after=0):
    store.detail(mid)  # Same visibility/existence policy as browser Gallery.
    if after<0:raise ValueError('无效历史游标')
    item={'motion_id':mid};attach(store,[item])
    with closing(store.reviews()) as db:
        rows=db.execute('''SELECT e.id,g.name,t.name,e.username,e.action,e.at
          FROM tag_events e JOIN tags t ON t.id=e.tag_id JOIN tag_groups g ON g.id=t.group_id
          WHERE e.motion_number=? AND e.id>? ORDER BY e.id LIMIT 101''',(motion_number(mid),after)).fetchall()
    item['events']=[dict(event_id=r[0],group=r[1],tag=r[2],username=r[3],action=r[4],at=r[5]) for r in rows[:100]]
    item['next_after']=rows[99][0] if len(rows)>100 else None
    return item


def change(store,mid,user,value):
    if not isinstance(value,dict) or set(value)-{'group','tag','action'}:raise ValueError('仅支持 group、tag、action；标注人由登录身份确定')
    group,gkey=label(value.get('group'));tag,tkey=label(value.get('tag'))
    action=value.get('action','add')
    if action not in ('add','remove'):raise ValueError('action 必须是 add 或 remove')
    detail=store.detail(mid)
    if not detail.get('location'):raise ValueError('动作尚未生成')
    number=motion_number(mid);username=user['username'];now=time.time()
    with closing(store.reviews()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        # Recheck removal inside the write transaction.
        if db.execute('SELECT 1 FROM removed WHERE motion_id=?',(mid,)).fetchone():raise ValueError('动作已移入回收站')
        row=db.execute('SELECT t.id FROM tags t JOIN tag_groups g ON g.id=t.group_id WHERE g.name_key=? AND t.name_key=?',(gkey,tkey)).fetchone()
        if action=='add':
            if row and db.execute('SELECT 1 FROM motion_tags WHERE motion_number=? AND tag_id=? AND username=?',(number,row[0],username)).fetchone():
                changed=False
            else:
                total,own=db.execute('SELECT count(*),coalesce(sum(username=?),0) FROM motion_tags WHERE motion_number=?',(username,number)).fetchone()
                if total>=500 or own>=50:raise ValueError('每人每个动作最多 50 个标签，每个动作最多 500 条标注')
                db.execute('INSERT OR IGNORE INTO tag_groups(name,name_key,created_by,created_at) VALUES(?,?,?,?)',(group,gkey,username,now))
                gid=db.execute('SELECT id FROM tag_groups WHERE name_key=?',(gkey,)).fetchone()[0]
                db.execute('INSERT OR IGNORE INTO tags(group_id,name,name_key,created_by,created_at) VALUES(?,?,?,?,?)',(gid,tag,tkey,username,now))
                row=db.execute('SELECT id FROM tags WHERE group_id=? AND name_key=?',(gid,tkey)).fetchone()
                db.execute('INSERT INTO motion_tags VALUES(?,?,?,?)',(number,row[0],username,now));changed=True
        else:
            changed=bool(row and db.execute('DELETE FROM motion_tags WHERE motion_number=? AND tag_id=? AND username=?',(number,row[0],username)).rowcount)
        if changed:db.execute('INSERT INTO tag_events(motion_number,tag_id,username,action,at) VALUES(?,?,?,?,?)',(number,row[0],username,action,now))
    result=read(store,mid);result['changed']=changed
    return result
