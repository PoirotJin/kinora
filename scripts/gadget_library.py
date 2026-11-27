"""Shared farm discovery and numeric source export; local imports stay private."""
from contextlib import closing
import io
import json
import re
import time
import zipfile
import zlib
from review_access import Forbidden, public_motion, require_motion_access
from g2h import direction_index
from g2h.farm_index import short_id, motion_number, motion_info, read_motion


def detail(store, mid, user):
    imported=require_motion_access(store,mid,user)
    if mid.startswith('ma_'):
        return dict(motion_id=mid, topic='personal', text=imported['description'] or mid, state='done',
                    owner=user['username'], storage_tier='managed', review=None, tags=[],
                    duration=imported['frames']/imported['fps'], source_download_available=True)
    if isinstance(mid,str) and mid.startswith('mimp'):
        # Imports are validated at 30 FPS on upload. Read only the small NPY
        # header for duration; searching must not load every motion array.
        import numpy as np
        path=store.root/'review/gadget/imports'/(mid+'.npz')
        try:
            with zipfile.ZipFile(path) as archive,archive.open('native_transl.npy') as stream:
                version=np.lib.format.read_magic(stream)
                if version == (1, 0):
                    header = np.lib.format.read_array_header_1_0
                elif version == (2, 0):
                    header = np.lib.format.read_array_header_2_0
                else:
                    raise ValueError('Unsupported NPY version')
                shape, _, _ = header(stream)
                if len(shape) != 2 or shape[1] != 3 or not 2 <= shape[0] <= 720:
                    raise ValueError('Invalid translation array dimensions')
        except (ValueError, KeyError, OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
            raise ValueError('导入动作的源 NPZ 无法读取，请检查文件是否丢失或损坏。') from exc
        return dict(motion_id=mid,topic='import',text=imported['text'],state='done',owner=imported['owner'],storage_tier='import',review=None,tags=[],duration=shape[0]/30,source_download_available=True)
    info=store.detail(mid)
    info['text']=info['prompt']['text']
    from review_tags import attach
    attach(store,[info])
    result=public_motion(info)
    result['duration']=info['prompt'].get('duration_s')
    result['source_download_available']=bool(info.get('location'))
    return result


def search(store, user, query):
    text=query.get('q',[''])[0];topic=query.get('topic',[''])[0];status=query.get('status',[''])[0]
    after=int(query.get('after',['0'])[0]);limit=int(query.get('limit',['20'])[0])
    if len(text)>200 or after<0 or not 1<=limit<=50 or status not in direction_index.STATUSES:
        raise ValueError('无效搜索参数')
    if topic and topic not in {t['id'] for t in store.topics()}:raise ValueError('未知主题')
    author=query.get('author',[''])[0];robot=query.get('robot',[''])[0];quality=query.get('quality',[''])[0];collection=query.get('collection',[''])[0]
    import math
    low=float(query.get('duration_min',['0'])[0]);high=float(query.get('duration_max',['100000'])[0])
    if not math.isfinite(low) or not math.isfinite(high) or not 0<=low<=high or len(author)>40:raise ValueError('无效作者或时长筛选')
    if quality not in ('','not_checked','incomplete','issues','failed','screened'):raise ValueError('无效源动作质量筛选')
    if robot:
        from retarget_robots import robot_spec
        robot_spec(robot)
    if collection:
        return _search_collection(store,user,query,after,limit,low,high)
    # Keyset windows bound the work even when no keyword matches. Callers can
    # continue an empty page using next_after; no full-library scan or model API.
    with closing(store.catalog()) as db:
        db.execute('ATTACH DATABASE ? AS reviewdb',(str(store.path),))
        db.create_function('motion_id',1,short_id,deterministic=True)
        deadline=time.monotonic()+1
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        from review_tags import filter_sql
        tag_sql,tag_args=filter_sql(query,'number')
        direction_index.attach(db,store.root)
        direction_sql,direction_args=direction_index.clause(status,'number')
        tag_sql+=direction_sql;tag_args+=direction_args
        extra='';extra_args=[]
        if author:extra+=' AND o.username=?';extra_args.append(author)
        if collection:
            extra+=' AND EXISTS(SELECT 1 FROM reviewdb.collection_items c WHERE c.motion_id=motion_id(m.number) AND c.owner=? AND c.collection_id=?)';extra_args.extend([user['username'],collection])
        if robot:
            history = (store.root / 'review/retarget/jobs.sqlite3').absolute()
            if not history.is_file():
                return dict(items=[], next_after=None, search_mode='keyword_all_terms', scanned=0,
                            direction_progress=direction_index.summary(store.root))
            db.execute('ATTACH DATABASE ? AS retargetdb', (history.as_uri() + '?mode=ro',))
            extra+=" AND EXISTS(SELECT 1 FROM retargetdb.jobs j WHERE j.motion_id=motion_id(m.number) AND j.owner=? AND j.robot=? AND j.status='done')";extra_args.extend([user['username'],robot])
        selected=None
        if extra:
            selected={r[0] for r in db.execute('SELECT m.number FROM (SELECT * FROM motion_links WHERE number>?'+tag_sql+' ORDER BY number LIMIT 501) m LEFT JOIN reviewdb.motion_owners o ON o.motion_id=motion_id(m.number) WHERE 1=1'+extra,[after]+tag_args+extra_args)}
        rows=db.execute('''SELECT m.number,m.task_id,m.text_id,m.topic,m.seed,p.item,r.body,o.username,o.request_id,d.motion_id,q.status
            FROM (SELECT * FROM motion_links WHERE number>?'''+tag_sql+''' ORDER BY number LIMIT 501) m
            JOIN prompts p ON p.id=m.text_id
            LEFT JOIN reviewdb.latest r ON r.motion_id=motion_id(m.number)
            LEFT JOIN reviewdb.motion_owners o ON o.motion_id=motion_id(m.number)
            LEFT JOIN reviewdb.removed d ON d.motion_id=motion_id(m.number)
            LEFT JOIN reviewdb.quality_reports q ON q.motion_id=motion_id(m.number) AND q.version=?
            ORDER BY m.number''',[after]+tag_args+[__import__('review_research').VERSION]).fetchall()
    result=[];cursor=after;tokens=text.casefold().split()
    for row in rows[:500]:
        cursor=row[0]
        if row[9]:continue
        if selected is not None and cursor not in selected:continue
        if quality and (row[10] or 'not_checked')!=quality:continue
        prompt=json.loads(row[5]);review=json.loads(row[6]) if row[6] else None
        if topic and row[3]!=topic:continue
        duration=prompt.get('duration_s')
        if (low or high!=100000) and (duration is None or not low<=float(duration)<=high):continue
        actual=review.get('status') if review else 'pending'
        if status and status not in direction_index.FILTERS and status!=actual:continue
        if not all(t in prompt['text'].casefold() for t in tokens):continue
        info=motion_info(store.root,row[:5])
        if not info.get('location'):continue
        info.update(text=prompt['text'],review=review,owner=row[7],request_id=row[8])
        item=public_motion(info);item['duration']=prompt.get('duration_s');item['quality_status']=row[10] or 'not_checked';result.append(item)
        if len(result)==limit:break
    from review_tags import attach
    attach(store,result)
    more=bool(rows and (cursor<rows[-1][0] or len(rows)>500))
    directions=direction_index.statuses(store.root,[r['motion_id'] for r in result])
    for item in result:item['direction_status']=directions.get(item['motion_id'],'not_checked')
    return dict(items=result,next_after=cursor if more else None,search_mode='keyword_all_terms',scanned=min(500,sum(r[0]<=cursor for r in rows)),direction_progress=direction_index.summary(store.root))


def _search_collection(store,user,query,after,limit,low,high):
    """Discover members from the private collection, including numeric imports.

    A numeric keyset cursor refers to collection membership rowid for this
    filtered stream. At most 100 members are inspected per request, with an
    advancing cursor even when filters exclude the entire window.
    """
    collection=query['collection'][0]
    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',collection):raise ValueError('无效集合编号')
    from review_tags import filter_sql
    from review_research import VERSION
    with closing(store.catalog()) as db:
        db.execute('ATTACH DATABASE ? AS reviewdb',(str(store.path),))
        db.create_function('farm_number',1,lambda mid:None if mid.startswith(('mimp','ma_')) else motion_number(mid),deterministic=True)
        tags,args=filter_sql(query,'farm_number(c.motion_id)')
        rows=db.execute('''SELECT c.rowid,c.motion_id,q.status FROM
          (SELECT rowid,* FROM reviewdb.collection_items WHERE owner=? AND collection_id=? AND rowid>? ORDER BY rowid LIMIT 101) c
          LEFT JOIN reviewdb.quality_reports q ON q.motion_id=c.motion_id AND q.version=? ORDER BY c.rowid''',
          (user['username'],collection,after,VERSION)).fetchall()
        tagged=None
        if tags:
            tagged={r[0] for r in db.execute('SELECT c.motion_id FROM reviewdb.collection_items c WHERE c.owner=? AND c.collection_id=? AND c.rowid>?'+tags,
                        [user['username'],collection,after]+args)}
        robot=query.get('robot',[''])[0];retargeted=None
        if robot:
            path=store.root/'review/retarget/jobs.sqlite3'
            if path.is_file():
                db.execute('ATTACH DATABASE ? AS retargetdb',(str(path),))
                retargeted={r[0] for r in db.execute('''SELECT c.motion_id FROM reviewdb.collection_items c WHERE c.owner=? AND c.collection_id=?
                  AND EXISTS(SELECT 1 FROM retargetdb.jobs j WHERE j.owner=c.owner AND j.motion_id=c.motion_id AND j.robot=? AND j.status='done')''',
                  (user['username'],collection,robot))}
            else:retargeted=set()
    tokens=query.get('q',[''])[0].casefold().split();result=[];cursor=after;scanned=0
    for membership,mid,quality in rows[:100]:
        cursor=membership;scanned+=1
        if tagged is not None and mid not in tagged:continue
        if retargeted is not None and mid not in retargeted:continue
        quality=quality or 'not_checked'
        if query.get('quality',[''])[0] and query['quality'][0]!=quality:continue
        try:item=detail(store,mid,user)
        except (Forbidden,ValueError,FileNotFoundError):continue
        if item.get('state')!='done':continue
        if query.get('author',[''])[0] and item.get('owner')!=query['author'][0]:continue
        if query.get('topic',[''])[0] and item.get('topic')!=query['topic'][0]:continue
        desired=query.get('status',[''])[0]
        actual=direction_index.statuses(store.root,[mid]).get(mid,'not_checked')
        item['direction_status']=actual
        if desired in direction_index.FILTERS:
            if actual!=direction_index.FILTERS[desired]:continue
        elif desired and (item.get('review') or {}).get('status','pending')!=desired:continue
        if not all(token in item.get('text','').casefold() for token in tokens):continue
        duration=item.get('duration')
        if (low or high!=100000) and (duration is None or not low<=float(duration)<=high):continue
        item['quality_status']=quality;result.append(item)
        if len(result)==limit:break
    return dict(items=result,next_after=cursor if rows and (cursor<rows[-1][0] or len(rows)>100) else None,search_mode='keyword_all_terms',scanned=scanned)


def source_npz(store, mid, user):
    detail(store,mid,user)
    if mid.startswith('ma_'):
        from g2h.motion_assets import MotionAssets
        return MotionAssets(store.root).artifact_path(user['username'],mid).read_bytes()
    if mid.startswith('mimp'):
        from gadget_imports import Imports
        motion=Imports(store.root).read(mid,user['username'])['motion']
    else:
        with closing(store.catalog()) as db:motion=read_motion(db,store.root,mid)['motion']
    from gadget_imports import FIELDS,MAX_BYTES,validate_npz
    import numpy as np
    if not FIELDS.issubset(motion):raise ValueError('该动作缺少完整 HY SMPL-H 字段，无法导出可重定向的源 NPZ')
    arrays={k:np.asarray(motion[k]) for k in FIELDS}
    if any(a.dtype.kind not in 'fi' for a in arrays.values()) or sum(a.nbytes for a in arrays.values())>MAX_BYTES:
        raise ValueError('源动作超出数值 NPZ 导出限制')
    output=io.BytesIO();np.savez_compressed(output,**arrays);raw=output.getvalue()
    validate_npz(raw)
    return raw
