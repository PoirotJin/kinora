#!/usr/bin/env python3
"""MotionFarm Gadget CLI. Python standard library only."""
import argparse
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from prompt_harness import prompt_guidance, check_motion_prompt

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def validate_download_response(response, data):
    length = response.headers.get('Content-Length')
    if response.status != 200 or not data or (length is not None and
            (not re.fullmatch(r'[0-9]+',length.strip()) or int(length) != len(data))):
        raise ValueError('Downloaded artifact is incomplete; retry the download')
    checksum = response.headers.get('X-MotionFarm-Content-SHA256')
    if checksum is not None and hashlib.sha256(data).hexdigest() != checksum.strip().lower():
        raise ValueError('Downloaded artifact checksum does not match; retry the download')


class Client:
    def __init__(self):
        self.url=os.environ.get('MOTIONFARM_URL','').rstrip('/')
        parsed=urllib.parse.urlsplit(self.url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:raise ValueError('Set MOTIONFARM_URL to an HTTPS server URL')
        self.key=os.environ.get('MOTIONFARM_API_KEY','')
        if not self.key and os.environ.get('MOTIONFARM_API_KEY_FILE'):
            self.key=Path(os.environ['MOTIONFARM_API_KEY_FILE']).expanduser().read_text().strip()
        if not self.key:raise ValueError('Set MOTIONFARM_API_KEY; do not put secrets in prompts or command arguments')
        context=ssl.create_default_context(cafile=os.environ.get('MOTIONFARM_CA_FILE') or None)
        self.opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=context))
    def request(self,endpoint,value=None,binary=False,retry=True):
        raw=json.dumps(value).encode() if value is not None else None
        request=urllib.request.Request(self.url+'/api/gadget/v1/'+endpoint,data=raw,headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        attempts=3 if retry else 1
        for attempt in range(attempts):
            try:
                with self.opener.open(request,timeout=20) as response:
                    data=response.read(32*1024*1024+1)
                    if len(data)>32*1024*1024:raise ValueError('Response exceeds size limit')
                    if binary:
                        validate_download_response(response,data)
                    return data if binary else json.loads(data)
            except urllib.error.HTTPError as exc:
                detail=exc.read(2048).decode(errors='replace')
                if exc.code in (429,502,503,504) and attempt<attempts-1:time.sleep(min(5*2**attempt,60));continue
                raise RuntimeError('HTTP '+str(exc.code)+': '+detail) from None
            except (urllib.error.URLError,TimeoutError):
                if attempt==attempts-1:raise
                time.sleep(min(5*2**attempt,60))
    def wait(self,kind,key,timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=self.request(kind+'?id='+urllib.parse.quote(key))
            job=value.get('job',value);status=job['status']
            print(json.dumps({'id':key,'status':status,'done':job.get('done'),'message':job.get('message')},ensure_ascii=False),file=sys.stderr)
            if status in ('completed','done'):return value
            if status in ('failed','cancelled','rejected'):raise RuntimeError('Job '+key+' ended: '+str(job))
            time.sleep(10)
        raise TimeoutError('Job remains on server; rerun with the same state file to resume')

def save_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));os.replace(temporary,path)


def matching_download(path, data):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
            raise ValueError('Unsafe or oversized output path')
        with path.open('rb') as existing:
            if existing.read(len(data)+1) != data:
                raise ValueError('Output exists with different content: '+str(path))
        return True
    except FileNotFoundError:
        return False


def publish_download(temporary, path):
    if os.name=='nt':
        os.rename(temporary,path)
        return
    if sys.platform in ('darwin','linux'):
        try:
            import ctypes
            libc = ctypes.CDLL(None,use_errno=True)
            rename = libc.renamex_np if sys.platform=='darwin' else libc.renameat2
        except (ImportError, AttributeError, OSError):
            pass
        else:
            source, target = os.fsencode(temporary), os.fsencode(path)
            if sys.platform=='darwin':
                rename.argtypes = (ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint)
                result = rename(source,target,0x00000004)  # RENAME_EXCL
            else:
                rename.argtypes = (ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
                at_fdcwd = -100
                result = rename(at_fdcwd,source,at_fdcwd,target,1)  # RENAME_NOREPLACE
            if result==0:
                return
            error = ctypes.get_errno()
            if error not in (errno.ENOSYS,errno.EINVAL,errno.ENOTSUP,errno.EOPNOTSUPP):
                raise OSError(error,os.strerror(error),str(path))
    os.link(temporary,path)


def download(client,key,path,kind='retarget'):
    endpoint='source' if kind=='source' else 'download'
    data=client.request(endpoint+'?id='+urllib.parse.quote(key),binary=True)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    artifact = dict(path=str(path),sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
    if matching_download(path,data):
        return artifact
    import tempfile
    fd,temporary=tempfile.mkstemp(prefix='.artifact-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:handle.write(data);handle.flush();os.fsync(handle.fileno())
        try:
            publish_download(temporary,path)
        except FileExistsError:
            if not matching_download(path,data):
                raise ValueError('Output changed during download; retry: '+str(path)) from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return artifact

LOCAL_REWRITE_INSTRUCTION = ('Gadget requires local-agent rewriting: use your current Codex/Cursor model to convert the request into 12–70 English words beginning "A person ", preserving motion intent and safety constraints; submit exact_english. Read get_prompt_guidance / PROMPT_GUIDE.md and run check_motion_prompt before submitting. Server Qwen translation is unavailable through Gadget.')

def generation_payload(request_id, text, n=1, duration=8, prompt_mode='exact_english', original_description=None, rewrite_agent=None):
    if prompt_mode != 'exact_english':raise ValueError(LOCAL_REWRITE_INSTRUCTION)
    check = check_motion_prompt(text, duration)
    if not check['format_valid']:raise ValueError(LOCAL_REWRITE_INSTRUCTION+' '+ '; '.join(check['errors']))
    value=dict(request_id=request_id,description=text,n=n,duration=duration,prompt_mode='exact_english')
    if original_description is not None:
        if not isinstance(original_description,str) or not 1<=len(original_description)<=2000:raise ValueError('original_description must contain 1–2000 characters')
        value['original_description']=original_description
    if rewrite_agent is not None:
        if not isinstance(rewrite_agent,dict) or not rewrite_agent or set(rewrite_agent)-{'name','model'}:raise ValueError('rewrite_agent accepts only self-reported name and model')
        if any(not isinstance(v,str) or not 1<=len(v)<=120 for v in rewrite_agent.values()):raise ValueError('rewrite_agent name/model must contain 1–120 characters')
        value['rewrite_agent']=rewrite_agent
    return value

RETARGET_FIELDS=('parameters','preset_id','preset_version','parent_job_id')

def retarget_execution(item,manifest=None):
    value={k:item[k] for k in RETARGET_FIELDS if k in item}
    fresh=item.get('force_new',(manifest or {}).get('force_new',False))
    if type(fresh) is not bool:raise ValueError('force_new must be a boolean')
    if fresh:value['force_new']=True
    return value

def retarget_identity(run_id,item_id,motion_id,robot,execution):
    scope=json.dumps(dict(version=2,item_id=item_id,motion_id=motion_id,robot=robot,execution=execution),sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return uuid.uuid5(uuid.UUID(run_id),scope).hex


def run_manifest(client,manifest_path,output,timeout):
    raw=Path(manifest_path).read_bytes();manifest=json.loads(raw);digest=hashlib.sha256(raw).hexdigest()
    items=manifest['items'];default_workflow=manifest.get('workflow','generate')
    if not isinstance(items,list) or not 1<=len(items)<=1000:raise ValueError('Manifest needs 1–1000 items')
    seen=set()
    for item in items:
        key=item['id'];workflow=item.get('workflow',default_workflow)
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',key) or key in seen:raise ValueError('Unique safe item IDs required')
        seen.add(key)
        if workflow not in ('generate','generate_retarget','retarget'):raise ValueError('Unknown workflow')
        execution=retarget_execution(item,manifest)
        if workflow=='generate' and execution.get('force_new'):raise ValueError('force_new applies only to retarget workflows')
        if workflow!='generate' and not item.get('robot',manifest.get('robot')):raise ValueError('Retarget workflows require an explicit robot')
        if workflow=='retarget':
            if not isinstance(item.get('motion_id'),str):raise ValueError('Retarget item requires motion_id')
        else:
            if not isinstance(item.get('text'),str) or not 2<=len(item['text'])<=2000:raise ValueError('Each text must contain 2–2000 characters')
            if type(item.get('n',1)) is not int or not 1<=item.get('n',1)<=10000:raise ValueError('n must be an integer 1–10000')
            if type(item.get('duration',8)) is not int or not 1<=item.get('duration',8)<=120:raise ValueError('duration must be an integer 1–120')
            generation_payload('validation',item['text'],prompt_mode=item.get('prompt_mode',manifest.get('prompt_mode','exact_english')),original_description=item.get('original_description'),rewrite_agent=item.get('rewrite_agent',manifest.get('rewrite_agent')))
    out=Path(output);state_path=out/'workflow-state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'manifest_sha256':digest,'run_id':uuid.uuid4().hex,'items':{},'retarget_id_scheme':'item-execution-v2'}
    if state['manifest_sha256']!=digest:raise ValueError('Manifest changed; use a different output directory')
    recorded={}
    for item in items:
        robot=item.get('robot',manifest.get('robot'))
        for mid,result in state.get('items',{}).get(item['id'],{}).get('motions',{}).items():
            jid=result.get('retarget_id')
            if not jid:continue
            recipe=json.dumps(dict(motion_id=mid,robot=robot,execution=retarget_execution(item,manifest)),sort_keys=True)
            if jid in recorded and recorded[jid]!=recipe:
                raise ValueError('Legacy workflow state shares a retarget ID across conflicting items. Original jobs are preserved. Inspect that server job; move the conflicting item to a separate manifest/output directory. Do not replace an uncertain job ID.')
            recorded[jid]=recipe
    save_json(state_path,state)
    for item in items:
        workflow=item.get('workflow',default_workflow);robot=item.get('robot',manifest.get('robot'))
        record=state['items'].setdefault(item['id'],{})
        if workflow=='retarget':motions=[client.request('motion?id='+urllib.parse.quote(item['motion_id']))]
        else:
            request='gadget-'+uuid.uuid5(uuid.UUID(state['run_id']),item['id']).hex
            record['request_id']=request;save_json(state_path,state)
            client.request('generate',dict(generation_payload(request,item['text'],n=item.get('n',1),duration=item.get('duration',8),prompt_mode=item.get('prompt_mode',manifest.get('prompt_mode','exact_english')),original_description=item.get('original_description'),rewrite_agent=item.get('rewrite_agent',manifest.get('rewrite_agent'))),**{k:item[k] for k in ('seed','parent_request_id') if k in item}))
            response=client.wait('generate',request,timeout);record['job']=response.get('job')
            after=0;motions=[]
            while True:
                page=client.request('generate?id='+request+'&after='+str(after));motions.extend(page['items'])
                after=page.get('next_after')
                if after is None:break
            if len(motions)!=item.get('n',1):raise RuntimeError('Generated count differs from manifest; inspect job before continuing')
        record.setdefault('motions',{})
        for motion in motions:
            mid=motion['motion_id'];target=out/item['id']/mid
            result=record['motions'].setdefault(mid,{})
            if workflow=='generate':result['source']=download(client,mid,target/'source.npz',kind='source')
            else:
                execution=retarget_execution(item,manifest)
                # A pre-v0.8 state was journaled before POST. Preserve its exact
                # identity instead of accidentally starting another server job.
                job=result.get('retarget_id') or retarget_identity(state['run_id'],item['id'],mid,robot,execution)
                result['retarget_id']=job;save_json(state_path,state)
                client.request('retarget',dict(id=job,motion_id=mid,robot=robot,**execution))
                finished = client.wait('retarget',job,timeout)
                if finished.get('export_verified') is False:
                    reason = finished.get('export_reason') or 'Retarget result is not available for export'
                    raise RuntimeError(f'Retarget job {job}: {reason}')
                result['retarget']=download(client,job,target/'reference.npz')
            (target/'text.txt').write_text(motion['text']+'\n')
            (target/'requested_text.txt').write_text(item.get('text',motion['text'])+'\n')
            save_json(target/'quality.json',client.request(('report' if workflow=='generate' else 'retarget_report')+'?id='+urllib.parse.quote(mid if workflow=='generate' else job)))
            save_json(target/'metadata.json',dict(motion=motion,workflow=workflow,robot=robot,artifacts=result,job=record.get('job'),original_description=item.get('original_description'),rewrite_agent=item.get('rewrite_agent',manifest.get('rewrite_agent'))))
            save_json(state_path,state)
    state['completed']=True;save_json(state_path,state);return state


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('info')
    commands.add_parser('prompt-guide')
    p=commands.add_parser('check-prompt');p.add_argument('--text',required=True);p.add_argument('--duration',type=int,default=8)
    p=commands.add_parser('tags');p.add_argument('--tag-group',default='');p.add_argument('--after',type=int,default=0);p.add_argument('--limit',type=int,default=100)
    p=commands.add_parser('search');p.add_argument('--tag-group',default='');p.add_argument('--tag',default='');p.add_argument('--tagged-by',default='');p.add_argument('--author',default='');p.add_argument('--robot',default='');p.add_argument('--quality',default='');p.add_argument('--collection',default='');p.add_argument('--duration-min',type=float);p.add_argument('--duration-max',type=float);p.add_argument('--query',default='');p.add_argument('--topic',default='');p.add_argument('--status',default='');p.add_argument('--after',type=int,default=0);p.add_argument('--limit',type=int,default=20)
    p=commands.add_parser('report');p.add_argument('kind',choices=['source','retarget']);p.add_argument('id')
    p=commands.add_parser('motion');p.add_argument('id')
    p=commands.add_parser('cancel');p.add_argument('kind',choices=['generate','retarget']);p.add_argument('id')
    p=commands.add_parser('generate');p.add_argument('--id',required=True);p.add_argument('--text',required=True);p.add_argument('--n',type=int,default=1);p.add_argument('--duration',type=int,default=8);p.add_argument('--prompt-mode',default='exact_english',help=LOCAL_REWRITE_INSTRUCTION);p.add_argument('--original-description');p.add_argument('--rewrite-agent-name');p.add_argument('--rewrite-agent-model');p.add_argument('--seed',type=int);p.add_argument('--parent-request-id')
    p=commands.add_parser('status');p.add_argument('kind',choices=['generate','retarget']);p.add_argument('id')
    p=commands.add_parser('retarget');p.add_argument('--id',required=True);p.add_argument('--motion',required=True);p.add_argument('--robot',required=True);p.add_argument('--parameters',type=json.loads);p.add_argument('--preset-id');p.add_argument('--preset-version',type=int);p.add_argument('--parent-job-id');p.add_argument('--force-new',action='store_true',help='Explicitly compute a new version with these parameters; preserves old results and consumes compute. Default reuses a prior successful version, including its old parameters.')
    p=commands.add_parser('upload');p.add_argument('file');p.add_argument('--id',required=True);p.add_argument('--text',required=True)
    p=commands.add_parser('download');p.add_argument('id');p.add_argument('--kind',choices=['source','retarget'],default='retarget');p.add_argument('--out',required=True)
    p=commands.add_parser('run');p.add_argument('manifest');p.add_argument('--out',required=True);p.add_argument('--wait-timeout',type=int,default=7200)
    args=parser.parse_args()
    if args.command in ('prompt-guide','check-prompt'):
        result=prompt_guidance() if args.command=='prompt-guide' else check_motion_prompt(args.text,args.duration)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result.get('format_valid') is False:raise SystemExit(2)
        return
    client=Client()
    if args.command=='info':result=client.request('info')
    elif args.command=='tags':result=client.request('tags?'+urllib.parse.urlencode(dict(tag_group=args.tag_group,after=args.after,limit=args.limit)))
    elif args.command=='search':result=client.request('search?'+urllib.parse.urlencode(dict(**{k:getattr(args,k) for k in ('author','robot','quality','collection','duration_min','duration_max') if getattr(args,k) is not None},tag_group=args.tag_group,tag=args.tag,tagged_by=args.tagged_by,q=args.query,topic=args.topic,status=args.status,after=args.after,limit=args.limit)))
    elif args.command=='report':result=client.request(('report' if args.kind=='source' else 'retarget_report')+'?id='+urllib.parse.quote(args.id))
    elif args.command=='motion':result=client.request('motion?id='+urllib.parse.quote(args.id))
    elif args.command=='cancel':result=client.request('cancel_generate' if args.kind=='generate' else 'cancel',{'id':args.id})
    elif args.command=='generate':result=client.request('generate',dict(generation_payload(args.id,args.text,n=args.n,duration=args.duration,prompt_mode=args.prompt_mode,original_description=args.original_description,rewrite_agent={k:v for k,v in [('name',args.rewrite_agent_name),('model',args.rewrite_agent_model)] if v is not None} or None),**{k:getattr(args,k) for k in ('seed','parent_request_id') if getattr(args,k) is not None}))
    elif args.command=='status':result=client.request(args.kind+'?id='+urllib.parse.quote(args.id))
    elif args.command=='retarget':result=client.request('retarget',dict(id=args.id,motion_id=args.motion,robot=args.robot,**retarget_execution({k:getattr(args,k) for k in (*RETARGET_FIELDS,'force_new') if getattr(args,k) is not None})))
    elif args.command=='upload':
        path=Path(args.file)
        if path.stat().st_size>4*1024*1024:raise ValueError('NPZ maximum size is 4 MiB')
        result=client.request('import',dict(id=args.id,text=args.text,npz_base64=base64.b64encode(path.read_bytes()).decode()))
    elif args.command=='download':result=download(client,args.id,args.out,kind=args.kind)
    else:result=run_manifest(client,args.manifest,args.out,args.wait_timeout)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,TimeoutError,OSError,KeyError) as exc:print('Error: '+str(exc),file=sys.stderr);sys.exit(1)
