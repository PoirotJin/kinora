"""Admin recovery listing with stable keyset pagination and exact motion lookup."""
import base64,json,math,re
from contextlib import closing
from review_access import Forbidden


def setup(db):
    db.execute('CREATE INDEX IF NOT EXISTS removed_order ON removed(removed_at DESC,motion_id DESC)')


def listing(store,user,query):
    if user.get('role')!='admin':raise Forbidden('仅管理员可查看回收站')
    q=query.get('q',[''])[0].strip();after=query.get('after',[''])[0]
    if q and not re.fullmatch('m[0-9a-z]{8,}',q):raise ValueError('请输入完整动作编号，例如 m00000001')
    limit=int(query.get('limit',['50'])[0])
    if not 1<=limit<=100:raise ValueError('每页条数需为 1–100')
    clause='';args=[]
    if q:clause=' WHERE motion_id=?';args.append(q)
    if after:
        try:
            if len(after)>300:raise ValueError()
            timestamp,mid,search=json.loads(base64.urlsafe_b64decode(after+'='*(-len(after)%4)))
            if type(timestamp) not in (int,float) or not math.isfinite(timestamp) or not re.fullmatch('m[0-9a-z]{8,}',mid) or search!=q:raise ValueError()
        except (ValueError,TypeError,UnicodeError):raise ValueError('无效回收站分页游标，请刷新后重试') from None
        clause+=(' AND ' if clause else ' WHERE ')+'(removed_at<? OR (removed_at=? AND motion_id<?))'
        args.extend([timestamp,timestamp,mid])
    with closing(store.reviews()) as db:
        setup(db)
        rows=db.execute('SELECT motion_id,removed_at,username,reason FROM removed'+clause+' ORDER BY removed_at DESC,motion_id DESC LIMIT ?',[*args,limit+1]).fetchall()
    items=[dict(motion_id=r[0],removed_at=r[1],username=r[2],reason=r[3]) for r in rows[:limit]]
    cursor=None
    if len(rows)>limit:
        last=items[-1];cursor=base64.urlsafe_b64encode(json.dumps([last['removed_at'],last['motion_id'],q],separators=(',',':')).encode()).decode().rstrip('=')
    return dict(items=items,next_after=cursor,query=q)
