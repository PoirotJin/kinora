"""Private, owner-scoped retarget design library. Designs never change solver settings."""
from contextlib import closing
import json
import math
import re
import sqlite3
import time
from retarget_robots import catalog,robot_spec
from review_access import Forbidden,public_motion
ACTIVE_LIMIT=500


def setup(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS personal_presets(
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, robot TEXT NOT NULL,
        notes TEXT NOT NULL, parameters TEXT NOT NULL, version INTEGER NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS personal_presets_owner ON personal_presets(owner,id);
      CREATE TABLE IF NOT EXISTS personal_preset_versions(
        preset_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(preset_id,version));
    ''')
    columns={row[1] for row in db.execute('PRAGMA table_info(personal_presets)')}
    for name in ('archived','archive_revision'):
        if name not in columns:
            try:db.execute('ALTER TABLE personal_presets ADD COLUMN '+name+' INTEGER NOT NULL DEFAULT 0')
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc):raise
    db.execute('CREATE INDEX IF NOT EXISTS personal_presets_active ON personal_presets(owner,archived,id)')
    db.commit()


def decode(row):
    item=dict(row);item['parameters']=json.loads(item['parameters']);item['execution_mode']='design_only'
    from retarget_parameters import validate
    try:item['executable_parameters']=validate(item['parameters']);item['execution_mode']='supported'
    except ValueError as e:item['execution_mode']='design_only';item['execution_error']=str(e)
    return item


def connect(store):
    import sqlite3
    db=store.reviews();db.row_factory=sqlite3.Row
    return db


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('无效方案 ID')
    return value


def owned(db,key,username):
    row=db.execute('SELECT * FROM personal_presets WHERE id=? AND owner=?',(identifier(key),username)).fetchone()
    if row is None:raise Forbidden('方案不存在或不属于当前用户')
    return row


def listing(store,user,query):
    after=query.get('after',[''])[0]
    if after:identifier(after)
    archived=query.get('archived',['0'])[0]
    if archived not in ('0','1'):raise ValueError('无效归档筛选')
    with closing(connect(store)) as db:
        rows=db.execute('SELECT * FROM personal_presets WHERE owner=? AND archived=? AND id>? ORDER BY id LIMIT 51',(user['username'],int(archived),after)).fetchall()
        counts=dict(db.execute('SELECT archived,count(*) FROM personal_presets WHERE owner=? GROUP BY archived',(user['username'],)).fetchall())
    return dict(items=[decode(r) for r in rows[:50]],next_after=rows[49]['id'] if len(rows)>50 else None,robots=catalog(),parameter_schema=__import__('retarget_parameters').schema(),active_count=counts.get(0,0),archived_count=counts.get(1,0),active_limit=ACTIVE_LIMIT)


def history(store,user,query):
    key=query.get('id',[''])[0];after=int(query.get('after',['0'])[0])
    if after<0:raise ValueError('无效版本游标')
    with closing(connect(store)) as db:
        row=owned(db,key,user['username'])
        versions=db.execute('SELECT version,body FROM personal_preset_versions WHERE preset_id=? AND version>? ORDER BY version LIMIT 51',(key,after)).fetchall()
    return dict(preset=decode(row),versions=[json.loads(v['body']) for v in versions[:50]],next_after=versions[49]['version'] if len(versions)>50 else None)


def validate_parameters(value,depth=0):
    if depth>8:raise ValueError('参数嵌套最多 8 层')
    if isinstance(value,dict):
        if len(value)>100:raise ValueError('单层参数最多 100 项')
        for k,v in value.items():
            if not isinstance(k,str) or not 1<=len(k)<=100:raise ValueError('参数名需为 1–100 字符')
            validate_parameters(v,depth+1)
    elif isinstance(value,list):
        if len(value)>100:raise ValueError('参数数组最多 100 项')
        for v in value:validate_parameters(v,depth+1)
    elif isinstance(value,str):
        if len(value)>2000:raise ValueError('参数文本过长')
    elif isinstance(value,float):
        if not math.isfinite(value):raise ValueError('参数不可包含 NaN 或 Infinity')
    elif value is not None and not isinstance(value,(bool,int)):raise ValueError('不支持的参数类型')


def save(store,user,value):
    if not isinstance(value,dict) or set(value)-{'id','version','name','robot','notes','parameters'}:raise ValueError('无效字段；方案归属由登录身份确定')
    key=identifier(value.get('id'));version=value.get('version',0)
    if type(version) is not int or version<0:raise ValueError('无效版本')
    name=value.get('name');notes=value.get('notes','');robot=value.get('robot')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('方案名称需为 1–80 字符')
    if not isinstance(notes,str) or len(notes)>2000:raise ValueError('说明最多 2000 字符')
    robot_spec(robot);parameters=value.get('parameters',{})
    if not isinstance(parameters,dict):raise ValueError('参数必须是 JSON 对象')
    validate_parameters(parameters)
    raw=json.dumps(parameters,ensure_ascii=False,sort_keys=True,allow_nan=False)
    if len(raw.encode())>16384:raise ValueError('参数最多 16 KiB')
    name=name.strip();now=time.time();username=user['username']
    with closing(connect(store)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM personal_presets WHERE id=?',(key,)).fetchone()
        if row:
            if row['owner']!=username:raise Forbidden('方案不存在或不属于当前用户')
            if row['archived']:
                from review_server import Conflict
                raise Conflict('方案已归档，请先恢复后编辑；当前草稿保留')
            if (row['name'],row['robot'],row['notes'],row['parameters'])==(name,robot,notes,raw):return decode(row)
            if row['version']!=version:
                from review_server import Conflict
                raise Conflict('方案已有新版本，请重新打开后编辑')
            db.execute('UPDATE personal_presets SET name=?,robot=?,notes=?,parameters=?,version=version+1,updated=? WHERE id=?',(name,robot,notes,raw,now,key))
        else:
            if version!=0:raise ValueError('新方案版本必须为 0')
            if db.execute('SELECT count(*) FROM personal_presets WHERE owner=? AND archived=0',(username,)).fetchone()[0]>=ACTIVE_LIMIT:raise ValueError('活跃方案已达 500 个，请先归档不再使用的方案')
            db.execute('INSERT INTO personal_presets(id,owner,name,robot,notes,parameters,version,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',(key,username,name,robot,notes,raw,1,now,now))
        result=decode(owned(db,key,username))
        db.execute('INSERT INTO personal_preset_versions VALUES(?,?,?)',(key,result['version'],json.dumps(result,ensure_ascii=False)))
    return result


def mutate(store,user,value):
    """Archive is a reversible catalog change; parameter versions stay immutable."""
    if not isinstance(value,dict):raise ValueError('无效方案请求')
    if 'action' not in value:return save(store,user,value)
    if set(value)!={'id','version','archive_revision','action'}:raise ValueError('归档操作需提供方案 ID、参数版本和归档版本')
    action=value['action'];version=value['version'];revision=value['archive_revision']
    if action not in ('archive','restore'):raise ValueError('无效方案操作')
    if type(version) is not int or version<1 or type(revision) is not int or revision<0:raise ValueError('无效方案版本')
    target=1 if action=='archive' else 0
    with closing(connect(store)) as db,db:
        db.execute('BEGIN IMMEDIATE');row=owned(db,value['id'],user['username'])
        from review_server import Conflict
        if row['version']!=version:raise Conflict('方案已有新版本，请刷新后再归档或恢复')
        if row['archive_revision']!=revision:
            # A lost response can be replayed once, but a later restore/archive
            # cycle must not be overwritten by an old request.
            if row['archive_revision']==revision+1 and row['archived']==target:return decode(row)
            raise Conflict('方案归档状态已变化，请刷新后再操作')
        if row['archived']==target:return decode(row)
        if not target and db.execute('SELECT count(*) FROM personal_presets WHERE owner=? AND archived=0',(user['username'],)).fetchone()[0]>=ACTIVE_LIMIT:
            raise ValueError('活跃方案已达 500 个，请先归档其他方案再恢复')
        db.execute('UPDATE personal_presets SET archived=?,archive_revision=archive_revision+1,updated=? WHERE id=?',
                   (target,time.time(),row['id']))
        return decode(owned(db,row['id'],user['username']))


def motions(store,user,query):
    # Even an administrator's personal space is strictly their own.
    result=store.listing(after=int(query.get('after',['0'])[0]),owner=user['username'])
    result['items']=[public_motion(x) for x in result['items'] if x.get('state')=='done']
    return result
