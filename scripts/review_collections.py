"""Private collections, favorites and audited shared tag aliases."""
from contextlib import closing
import hashlib,json,time,re
import sqlite3
import zipfile
import zlib
from review_access import Forbidden

SOURCE_READ_ERRORS = (ValueError, KeyError, OSError, EOFError, zipfile.BadZipFile, zlib.error)
COLLECTION_PAGE_SIZE = 50
MAX_COLLECTION_ITEMS = 1000


class CollectionMissing(ValueError):
    pass


class CollectionMembershipChanged(ValueError):
    pass


def membership_basis(motion_ids):
    raw = json.dumps(motion_ids, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def setup(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS motion_collections(owner TEXT,id TEXT,name TEXT,updated REAL,PRIMARY KEY(owner,id));
    CREATE TABLE IF NOT EXISTS collection_items(owner TEXT,collection_id TEXT,motion_id TEXT,created REAL,PRIMARY KEY(owner,collection_id,motion_id));
    CREATE TABLE IF NOT EXISTS collection_events(id INTEGER PRIMARY KEY,owner TEXT,collection_id TEXT,motion_id TEXT,action TEXT,at REAL);
    CREATE TABLE IF NOT EXISTS tag_aliases(group_key TEXT,alias_key TEXT,canonical_key TEXT,updated REAL,PRIMARY KEY(group_key,alias_key));
    CREATE INDEX IF NOT EXISTS alias_name ON tag_aliases(alias_key,group_key,canonical_key);
    CREATE INDEX IF NOT EXISTS alias_canonical ON tag_aliases(canonical_key,group_key,alias_key);
    CREATE TABLE IF NOT EXISTS tag_alias_events(id INTEGER PRIMARY KEY,actor TEXT,group_key TEXT,alias_key TEXT,canonical_key TEXT,action TEXT,at REAL);''')

def collections(store,user,query):
    key=query.get('id',[''])[0];owner=user['username']
    with closing(store.reviews()) as db:
        db.execute('BEGIN')
        rows=db.execute('SELECT id,name,updated FROM motion_collections WHERE owner=? ORDER BY id LIMIT 101',(owner,)).fetchall()
        result={'collections':[dict(id=r[0],name=r[1],updated=r[2]) for r in rows]}
        if key:
            rows=db.execute('SELECT motion_id,created FROM collection_items WHERE owner=? AND collection_id=? ORDER BY motion_id LIMIT ?',
                            (owner,key,MAX_COLLECTION_ITEMS+1)).fetchall()
    if key:
        basis = membership_basis([row[0] for row in rows])
        expected = query.get('expected_basis', [None])[0]
        if expected is not None and expected != basis:
            raise CollectionMembershipChanged('集合成员已变化，请重新读取集合。')
        result['basis'] = basis
        after = query.get('after', [''])[0]
        rows = [row for row in rows if row[0] > after]
        from gadget_library import detail
        items=[]
        next_after=None
        # A window can contain only removed, corrupt or otherwise inaccessible
        # entries. Advance past it before reporting an empty collection.
        for start in range(0,min(len(rows),MAX_COLLECTION_ITEMS),COLLECTION_PAGE_SIZE):
            end=min(start+COLLECTION_PAGE_SIZE,len(rows))
            for mid,created in rows[start:end]:
                try:item=detail(store,mid,user)
                except (Forbidden, *SOURCE_READ_ERRORS):continue
                item['collected_at']=created;items.append(item)
            next_after=rows[end-1][0] if end<len(rows) else None
            if items:
                break
        managed_ids = [item['motion_id'] for item in items if item['motion_id'].startswith('ma_')]
        if managed_ids:
            from g2h.harness import Store
            if (store.root / Store.LEDGER_PATH).is_file():
                statuses = Store(store.root).candidate_results(owner, managed_ids)
                for item in items:
                    item.update(statuses.get(item['motion_id'], {}))
        result.update(items=items,next_after=next_after)
    return result

def change(store,user,value):
    if set(value)-{'id','name','motion_id','action','require_existing'}:raise ValueError('Unknown collection fields')
    key=value.get('id');name=value.get('name',key);action=value.get('action','add');mid=value.get('motion_id')
    if not isinstance(key,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',key):raise ValueError('Invalid collection ID')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80 or action not in ('add','remove','rename','delete','create'):raise ValueError('Invalid collection change')
    require_existing = value.get('require_existing',False)
    if 'require_existing' in value and (type(require_existing) is not bool or action!='add'):
        raise ValueError('无效的集合添加参数')
    if action in ('add','remove'):
        from gadget_library import detail
        if action=='add':detail(store,mid,user)
        elif not isinstance(mid,str):raise ValueError('Invalid motion ID')
    now=time.time();owner=user['username']
    with closing(store.reviews()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT 1 FROM motion_collections WHERE owner=? AND id=?',(owner,key)).fetchone()
        if action=='delete':
            db.execute('DELETE FROM collection_items WHERE owner=? AND collection_id=?',(owner,key))
            if db.execute('DELETE FROM motion_collections WHERE owner=? AND id=?',(owner,key)).rowcount:
                db.execute('INSERT INTO collection_events(owner,collection_id,motion_id,action,at) VALUES(?,?,?,?,?)',(owner,key,None,'delete',now))
            return {'ok':True,'id':key,'deleted':True}
        if action=='remove' and not prior:return {'ok':True,'id':key}
        if require_existing and not prior:
            raise CollectionMissing('集合已删除或不可用，请选择其他集合。')
        if not prior and db.execute('SELECT count(*) FROM motion_collections WHERE owner=?',(owner,)).fetchone()[0]>=100:raise ValueError('最多 100 个集合')
        db.execute('INSERT OR IGNORE INTO motion_collections VALUES(?,?,?,?)',(owner,key,name,now))
        if action=='rename':db.execute('UPDATE motion_collections SET name=?,updated=? WHERE owner=? AND id=?',(name,now,owner,key))
        elif action=='add':
            exists=db.execute('SELECT 1 FROM collection_items WHERE owner=? AND collection_id=? AND motion_id=?',(owner,key,mid)).fetchone()
            if not exists and db.execute('SELECT count(*) FROM collection_items WHERE owner=? AND collection_id=?',(owner,key)).fetchone()[0]>=MAX_COLLECTION_ITEMS:raise ValueError('每集合最多 1000 个动作')
            if not exists:
                db.execute('INSERT INTO collection_items VALUES(?,?,?,?)',(owner,key,mid,now))
                db.execute('INSERT INTO collection_events(owner,collection_id,motion_id,action,at) VALUES(?,?,?,?,?)',(owner,key,mid,action,now))
        else:
            if db.execute('DELETE FROM collection_items WHERE owner=? AND collection_id=? AND motion_id=?',(owner,key,mid)).rowcount:
                db.execute('INSERT INTO collection_events(owner,collection_id,motion_id,action,at) VALUES(?,?,?,?,?)',(owner,key,mid,action,now))
    return {'ok':True,'id':key}

def aliases(store,user,value=None,query=None):
    from review_tags import label
    if value is None:
        after=int((query or {}).get('after',['0'])[0])
        if after<0:raise ValueError('Invalid alias cursor')
        with closing(store.reviews()) as db:rows=db.execute('SELECT rowid,group_key,alias_key,canonical_key FROM tag_aliases WHERE rowid>? ORDER BY rowid LIMIT 101',(after,)).fetchall()
        return dict(items=[dict(id=r[0],group=r[1],alias=r[2],canonical=r[3]) for r in rows[:100]],next_after=rows[99][0] if len(rows)>100 else None)
    if user.get('role')!='admin':raise Forbidden('仅管理员可整理共享同义词')
    if set(value)-{'group','alias','canonical','action'}:raise ValueError('Unknown alias fields')
    group=label(value.get('group'))[1];alias=label(value.get('alias'))[1];canonical=label(value.get('canonical'))[1];action=value.get('action','add')
    if alias==canonical or action not in ('add','remove'):raise ValueError('Invalid alias')
    with closing(store.reviews()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        if action=='add':
            if not db.execute('SELECT 1 FROM tags t JOIN tag_groups g ON g.id=t.group_id WHERE g.name_key=? AND t.name_key=?',(group,canonical)).fetchone():raise ValueError('目标标签不存在')
            if db.execute('SELECT 1 FROM tag_aliases WHERE group_key=? AND (alias_key=? OR canonical_key=?)',(group,canonical,alias)).fetchone():raise ValueError('同义词不支持链或循环')
            db.execute('INSERT INTO tag_aliases VALUES(?,?,?,?) ON CONFLICT(group_key,alias_key) DO UPDATE SET canonical_key=excluded.canonical_key,updated=excluded.updated',(group,alias,canonical,time.time()))
        else:db.execute('DELETE FROM tag_aliases WHERE group_key=? AND alias_key=?',(group,alias))
        db.execute('INSERT INTO tag_alias_events(actor,group_key,alias_key,canonical_key,action,at) VALUES(?,?,?,?,?,?)',(user['username'],group,alias,canonical,action,time.time()))
    return {'ok':True}

SEARCH_FIELDS={'q','topic','status','author','robot','quality','collection','duration_min','duration_max','tag','tag_group','tag_mode','tagged_by'}
def saved_searches(store,user,query=None,value=None):
    owner=user['username']
    with closing(store.reviews()) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS saved_searches(owner TEXT,name TEXT,filters TEXT,updated REAL,PRIMARY KEY(owner,name))')
        if value is not None:
            if not isinstance(value,dict) or set(value)-{'name','filters','action'}:raise ValueError('无效保存条件')
            name=value.get('name','');filters=value.get('filters',{});action=value.get('action','save')
            if not isinstance(name,str) or not 1<=len(name.strip())<=80 or action not in ('save','delete'):raise ValueError('无效检索名称')
            if not isinstance(filters,dict) or set(filters)-SEARCH_FIELDS or any(not isinstance(v,str) or len(v)>200 for v in filters.values()):raise ValueError('无效筛选条件')
            if action=='delete':db.execute('DELETE FROM saved_searches WHERE owner=? AND name=?',(owner,name))
            else:
                if not db.execute('SELECT 1 FROM saved_searches WHERE owner=? AND name=?',(owner,name)).fetchone() and db.execute('SELECT count(*) FROM saved_searches WHERE owner=?',(owner,)).fetchone()[0]>=50:raise ValueError('最多保存 50 组检索条件')
                db.execute('INSERT OR REPLACE INTO saved_searches VALUES(?,?,?,?)',(owner,name,json.dumps(filters,ensure_ascii=False),time.time()))
        return {'items':[dict(name=r[0],filters=json.loads(r[1]),updated=r[2]) for r in db.execute('SELECT name,filters,updated FROM saved_searches WHERE owner=? ORDER BY updated DESC',(owner,))]}

def _export_retarget_history(path, owner, motion_id):
    records = []
    history = {'retarget_executions': records}
    if not path.is_file():
        return history
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                'SELECT id,robot,status,execution,created FROM jobs WHERE owner=? AND motion_id=? '
                'ORDER BY created DESC LIMIT 101', (owner, motion_id)).fetchall()
    except sqlite3.Error:
        history['retarget_history_error'] = '无法读取重定向历史'
        return history
    history['retarget_history_truncated'] = len(rows) > 100
    for row in rows[:100]:
        record = dict(row)
        try:
            execution = json.loads(record['execution'] or '{}')
            if not isinstance(execution, dict):
                raise ValueError('Invalid retarget execution')
            record['execution'] = execution
        except (TypeError, ValueError):
            record['execution'] = None
            history['retarget_history_error'] = '部分重定向历史的执行参数无法读取'
        records.append(record)
    return history


def export_collection(store,user,query):
    """Explicit bounded export: include every collection entry in the manifest."""
    import io
    from gadget_library import detail,source_npz
    key=query.get('id',[''])[0];owner=user['username']
    with closing(store.reviews()) as db:
        collection=db.execute('SELECT name FROM motion_collections WHERE owner=? AND id=?',(owner,key)).fetchone()
        if not collection:raise Forbidden('集合不存在或不属于当前用户')
        ids=[r[0] for r in db.execute('SELECT motion_id FROM collection_items WHERE owner=? AND collection_id=? ORDER BY motion_id LIMIT 51',(owner,key))]
    if len(ids)>50:raise ValueError('单次导出最多 50 个动作，请拆分集合后重试')
    output=io.BytesIO();entries=[];total=0
    history_path = (store.root / 'review/retarget/jobs.sqlite3').absolute()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
        for mid in ids:
            entry={'motion_id':mid,'asset_status':'unavailable'};entries.append(entry)
            try:
                info=detail(store,mid,user)
            except Forbidden:
                entry['reason']='动作已删除或当前无权访问';continue
            except SOURCE_READ_ERRORS:
                entry['reason']='源动作无法读取，文件可能已丢失或损坏'
                continue
            entry.update(metadata=info)
            entry.update(_export_retarget_history(history_path, owner, mid))
            try:
                raw=source_npz(store,mid,user)
            except Forbidden:
                entry['reason']='动作已删除或当前无权访问'
                continue
            except SOURCE_READ_ERRORS as exc:
                entry['reason']='源动作不可导出：'+type(exc).__name__
                continue
            if total+len(raw)>64*1024*1024:
                entry['reason']='导出达到 64 MiB 动作数据上限';continue
            name='motions/'+mid+'.npz';archive.writestr(name,raw);total+=len(raw)
            entry.update(asset_status='included',file=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        complete = all(entry['asset_status'] == 'included' and 'retarget_history_error' not in entry
                       for entry in entries)
        manifest=dict(schema_version=1,collection_id=key,name=collection[0],owner=owner,created=time.time(),complete=complete,entries=entries,coverage='Source NPZ, current visible tags/review/author, and up to 100 own retarget execution snapshots per motion. Robot result files are not included.')
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.writestr('README.txt','请先查看 manifest.json 的 complete 与每项 asset_status。不可用动作仍在清单中列出。重定向历史读取异常见 retarget_history_error，无法读取的执行参数以 null 表示。\n包内动作文件为源动作 NPZ。\n')
    return output.getvalue()
