"""Small, versioned workflow API for local agents; no server shell access."""
import base64
import errno
import hashlib
import json
import stat
from contextlib import closing
from urllib.parse import parse_qs
from gadget_keys import Keys
from review_access import Forbidden,owner_of

RECOVERY_HASH_BYTES = 32 * 1024 * 1024

ROUTES={'identity':None,'recovery':'read','report':'read','retarget_report':'read','tags':'read','info':'read','generate':'generate','retarget':'retarget','download':'read','import':'retarget','cancel':'retarget','search':'read','motion':'read','source':'read','cancel_generate':'generate'}
ROUTES.update({'harness_capabilities':'read','harness_tasks':'read','harness_task':'read',
               'harness_create':'generate','harness_feedback':'generate','harness_step':'generate',
               'harness_cancel':'generate','harness_import':'generate'})


def recovered_sources(folder,rows):
    from gadget_imports import ImportedSourceCorrupt, MAX_BYTES, verified_source_bytes

    # Large pages still report missing files, but only hash a bounded amount.
    remaining = RECOVERY_HASH_BYTES
    records = []
    for mid, created, size, digest in rows:
        path = folder/(mid+'.npz')
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                status = 'unavailable'
            elif size != info.st_size or not 0 < size <= MAX_BYTES:
                status = 'damaged'
            elif size > remaining:
                status = 'unverified'
            else:
                remaining -= size
                verified_source_bytes(path,size,digest)
                status = 'available'
        except ImportedSourceCorrupt:
            status = 'damaged'
        except FileNotFoundError:
            status = 'missing'
        except OSError:
            status = 'unavailable'
        records.append(dict(kind='source',id=mid[4:],server_status='done',created_at=created,source_status=status))
    return records


def imported_source_response(handler, root, user, mid, *, download):
    from gadget_imports import Imports, validate_npz, verified_source_bytes

    receipt,path = Imports(root).source(mid,user['username'])
    duration = None
    try:
        raw = verified_source_bytes(path,receipt['bytes'],receipt['sha256'])
        duration = validate_npz(raw)['native_transl'].shape[0]/30
        status = 'available'
    except FileNotFoundError:
        status = 'missing'
    except ValueError:
        status = 'damaged'
    except OSError:
        status = 'unavailable'
    if download:
        if status=='available':
            return handler.send(200,raw,'application/octet-stream',attachment=mid+'-source.npz',
                                content_sha256=receipt['sha256'])
        messages = {
            'missing':'上传的源 NPZ 文件缺失，请用原编号和原文件重新提交后重试。',
            'damaged':'上传的源 NPZ 文件损坏，请用原编号和原文件重新提交后重试。',
            'unavailable':'暂时无法读取已上传的源 NPZ，请稍后重试。',
        }
        return handler.send(503,{'error':messages[status],'source_status':status})
    return handler.send(200,dict(motion_id=mid,topic='import',text=receipt['text'],state='done',
                                 owner=receipt['owner'],storage_tier='import',review=None,tags=[],
                                 duration=duration,sha256=receipt['sha256'],source_status=status,
                                 source_download_available=status=='available'))


def recovery(root,user,query):
    """Discover only this account's existing IDs; never submit/retry work."""
    import math,sqlite3,time
    from pathlib import Path
    kind=query.get('kind',[''])[0];limit=int(query.get('limit',['20'])[0]);cursor=query.get('after',[''])[0]
    if kind not in ('generate','retarget','source') or not 1<=limit<=100:raise ValueError('恢复需指定 kind 和 1–100 条分页大小')
    created=float('inf');key=''
    if cursor:
        try:
            created,key=json.loads(cursor)
            if type(created) not in (int,float) or not math.isfinite(created) or not isinstance(key,str) or not 1<=len(key)<=100:raise ValueError()
        except (ValueError,TypeError):raise ValueError('无效恢复分页游标') from None
    folder,table=('custom','requests') if kind=='generate' else ('retarget','jobs') if kind=='retarget' else ('gadget/imports','imports')
    path=Path(root)/'review'/folder/('index.sqlite3' if kind=='source' else 'jobs.sqlite3')
    rows=[]
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            deadline=time.monotonic()+1;db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            fields='bytes,sha256' if kind=='source' else 'status'
            rows=db.execute('SELECT id,created,'+fields+' FROM '+table+' WHERE owner=?'+(' AND (created<? OR (created=? AND id<?))' if cursor else '')+' ORDER BY created DESC,id DESC LIMIT ?',
                            [user['username']]+([created,created,key] if cursor else [])+[limit+1]).fetchall()
    items = (recovered_sources(path.parent,rows[:limit]) if kind=='source' else
             [dict(kind=kind,id=key,server_status=status,created_at=created) for key,created,status in rows[:limit]])
    return dict(identity=Keys(root).identity(user['username']),records=items,next_after=json.dumps([rows[limit-1][1],rows[limit-1][0]],separators=(',',':')) if len(rows)>limit else None)

def handle_api(handler,parsed,write):
    root=handler.server.store.root
    name=parsed.path.removeprefix('/api/gadget/v1/')
    if name not in ROUTES:return handler.send(404,{'error':'Unknown Gadget endpoint'})
    authorization=handler.headers.get('Authorization','')
    if not authorization.startswith('Bearer '):raise Forbidden('需要Bearer API Key')
    if not handler.origin_allowed():raise Forbidden('Cross-origin request denied')
    if write and handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('请求需要application/json')
    user=Keys(root).authenticate(authorization[7:],ROUTES[name])
    query=parse_qs(parsed.query);key=query.get('id',[''])[0]
    if name.startswith('harness_'):
        from harness_service import HarnessService
        service=HarnessService(root,handler.server.store)
        if name in ('harness_capabilities','harness_tasks','harness_task'):
            if write:return handler.send(405,{'error':'Read-only task endpoint'})
            result=(service.capabilities(user) if name=='harness_capabilities' else
                    service.tasks(user,query) if name=='harness_tasks' else service.detail(user,key))
            return handler.send(200,result)
        if not write:return handler.send(405,{'error':'Task mutation requires POST'})
        limits={'harness_import':6*1024*1024,'harness_step':96*1024,'harness_create':32*1024,'harness_feedback':32*1024}
        value=handler.body(limit=limits.get(name,16000))
        if not isinstance(value,dict):raise ValueError('Task request must be an object')
        if name=='harness_create':return handler.send(201,service.create(user,value))
        task_id=value.get('task_id')
        if name=='harness_step':
            if set(value)!={'task_id','decision_id','action'}:raise ValueError('Step requires task_id, decision_id and action')
            result=service.execute(user,task_id,value['action'],value['decision_id'])
        elif name=='harness_feedback':
            result=service.feedback(user,task_id,{k:v for k,v in value.items() if k!='task_id'})
        elif name=='harness_import':
            result=service.import_patch(user,task_id,{k:v for k,v in value.items() if k!='task_id'})
        elif name=='harness_cancel':
            if set(value)!={'task_id'}:raise ValueError('Cancel requires task_id only')
            result=service.cancel(user,task_id)
        return handler.send(200,result)
    if name in ('identity','recovery'):
        if write:return handler.send(405,{'error':'Read-only Gadget endpoint'})
        return handler.send(200,Keys(root).identity(user['username']) if name=='identity' else recovery(root,user,query))
    if name in ('report','retarget_report') and not write:
        from review_research import report,retarget_report
        return handler.send(200,report(handler.server.store,key,user) if name=='report' else retarget_report(handler.server.retarget,key,user))
    if name=='tags' and not write:
        from review_tags import vocabulary
        return handler.send(200,vocabulary(handler.server.store,query))
    if name=='info' and not write:
        from retarget_robots import catalog
        return handler.send(200,dict(version='1',robots=catalog(),max_unfinished_per_user=5,
            import_format='SMPL-H NPZ: fps=30, native_keypoints3d[T,52,3], native_transl[T,3], smpl_poses[T,156]; 2<=T<=720; meters; HY coordinates Y-up',
            workflows=['search → motion → source','generate → poll → source','generate → poll → retarget → poll → download','import → retarget → poll → download'],
            gadget_version='0.9',**__import__('gadget_capabilities').capabilities(),tag_filters=['tag_group','tag','tagged_by'],prompt_modes=['exact_english'],rewrite_location='local_agent',search_mode='keyword_all_terms',
            topics=handler.server.store.topics(),source_format='HY-compatible numeric SMPL-H NPZ',
            note='Local agent must rewrite to final English before submit. Server never rewrites Gadget prompts. Use A person, 12–70 English words. Embedding API and compute quotas still apply; local subscription cost is external/unknown.'))
    if name in ('search','motion','source') and not write:
        from gadget_library import search,detail,source_npz
        if name=='search':return handler.send(200,search(handler.server.store,user,query))
        if key.startswith('mimp'):
            return imported_source_response(handler,root,user,key,download=name=='source')
        if name=='motion':return handler.send(200,detail(handler.server.store,key,user))
        raw = source_npz(handler.server.store,key,user)
        return handler.send(200,raw,'application/octet-stream',attachment=key+'-source.npz',
                            content_sha256=hashlib.sha256(raw).hexdigest())
    if name=='cancel_generate' and write:
        from review_custom import Custom
        return handler.send(202,Custom(root).cancel(handler.body().get('id'),user))
    if name=='generate':
        from review_custom import Custom
        custom=Custom(root)
        if write:
            value=handler.body()
            if value.get('prompt_mode','exact_english')!='exact_english':raise ValueError('Gadget 任务必须由本地 agent 重写；请提交以 A person 开头的 12–70 词英文提示词，prompt_mode=exact_english。服务器不调用重写 API。')
            value['prompt_mode']='exact_english'
            return handler.send(202,custom.submit(value,user['username'],source='local_agent'))
        result=custom.results(key,int(query.get('after',['0'])[0]),user)
        with closing(custom.db()) as db:row=custom.authorize(db,key,user)
        result['job']={k:row[k] for k in ('id','status','done','n','message','duration','prompt_mode','description','prompt','seed','inference_seed','lineage','rewrite_model','rewrite_meta')}
        result['task']=__import__('gadget_capabilities').task('generate',dict(row),[dict(motion_id=x['motion_id'],source='source?id='+x['motion_id']) for x in result.get('items',[])])
        return handler.send(200,result)
    if name=='import' and write:
        from gadget_imports import Imports
        from g2h.capacity import CapacityUnavailable
        value=handler.body(limit=6*1024*1024)
        if not isinstance(value.get('npz_base64'),str):raise ValueError('缺少npz_base64')
        raw=base64.b64decode(value['npz_base64'],validate=True)
        try:
            imported = Imports(root).save(user['username'],value.get('id'),value.get('text'),raw)
        except CapacityUnavailable as exc:
            return handler.send(503,{'error':str(exc)})
        except OSError as exc:
            if exc.errno not in (errno.ENOSPC,errno.EDQUOT):
                raise
            return handler.send(503,{'error':'服务器写入空间不足，请在空间恢复后重试。'})
        return handler.send(201,imported)
    queue=handler.server.retarget
    if name=='retarget':
        if not write:
            row = queue.get(key,user)
            references = [dict(job_id=key,download='download?id='+key)] if row.get('export_verified') is True else []
            row['task'] = __import__('gadget_capabilities').task('retarget',row,references)
            return handler.send(200,row)
        value=handler.body();mid=value.get('motion_id','')
        if not isinstance(mid,str):raise ValueError('无效motion_id')
        if mid.startswith('mimp'):
            from gadget_imports import Imports
            Imports(root).get(mid,user['username'])
            class Store:
                def detail(self,identifier):
                    Imports(root).get(identifier,user['username']);return {'location':{'uploaded':True}}
            store=Store()
        else:
            from gadget_library import detail
            detail(handler.server.store,mid,user)
            store=handler.server.store
        return handler.send(202,queue.submit(value,user,store,reuse_prior_parameters=True))
    if name=='cancel' and write:return handler.send(202,queue.cancel(handler.body().get('id'),user))
    if name=='download' and not write:return handler.download(queue.result(key,user,'result.npz',require_dynamic=True))
    return handler.send(405,{'error':'Method not allowed'})

def handle_keys(handler,path,write,user):
    keys=Keys(handler.server.store.root)
    if path=='/api/gadget/keys':
        if not write:return handler.send(200,{'keys':keys.list(user['username'])})
        result=keys.create(user['username'],handler.body())
        handler.server.auth.audit('gadget.key.create',user['username'],result['id'],handler.peer())
        return handler.send(201,result)
    if path=='/api/gadget/keys/revoke' and write:
        key=handler.body().get('id');result=keys.revoke(user['username'],key)
        handler.server.auth.audit('gadget.key.revoke',user['username'],key,handler.peer())
        return handler.send(200,result)
    return handler.send(405,{'error':'Method not allowed'})
