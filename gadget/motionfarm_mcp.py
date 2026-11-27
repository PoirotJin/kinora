#!/usr/bin/env python3
"""MotionFarm MCP stdio bridge. No model runtime or subscription credentials."""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.parse
from motionfarm import Client, download, generation_payload, LOCAL_REWRITE_INSTRUCTION
from prompt_harness import prompt_guidance, check_motion_prompt
from workflow_journal import Journal, RECOVERY_INSTRUCTION, SOURCE_STATUSES


def prop(kind='string', **extra):return dict(type=kind,**extra)
def tool(name, description, properties=None, required=None, read=True):
    return dict(name=name,description=description,inputSchema=dict(type='object',properties=properties or {},required=required or [],additionalProperties=False),
        annotations=dict(readOnlyHint=read,destructiveHint=False,idempotentHint=True,openWorldHint=True))
TOOLS=[
 tool('workflow_recovery','Defaults to offline local records: cached identity is NOT live authorization. After Key rotation use verify_identity=true to authenticate and access the same stable account journal. For accepted jobs missing from old v0.8 journals use discover=true with kind=generate, retarget or source; this only lists current account server IDs, requires read scope, and returns a per-kind next_after cursor. Never submits or retries work. Keep the same output folder. Local listing uses a separate cursor. Follow record.resume exactly; imports use get_motion(mimp+import_id).',dict(kind=prop(enum=['generate','retarget','source']),id=prop(maxLength=100),limit=prop('integer',minimum=1,maximum=100),after=prop(maxLength=200),verify_identity=prop('boolean',default=False),discover=prop('boolean',default=False))),
 tool('get_prompt_guidance','Read the complete local HYM prompt-writing policy, examples, conflict handling and semantic checklist before generating. Offline, no API key or model call required.'),
 tool('check_motion_prompt','Offline format/duration check of your locally written final prompt. Does NOT assess meaning or motion quality. Read get_prompt_guidance and review its semantic checklist before generate_motion.',dict(text=prop(maxLength=2000),duration=prop('integer',minimum=1,maximum=120)),['text']),
 tool('motionfarm_info','List server capabilities, available robots, topics, quotas, and prompt modes.'),
 tool('search_motions','Search public and your own generated motions by ALL case-insensitive keyword terms. Tag filters are exact normalized matches, combined with AND on the same active assignment. A tag without a group matches that name in any group. English descriptions work best. Follow next_after even for an empty page; this is bounded keyword search, not semantic search.',
      dict(author=prop(maxLength=40),robot=prop(),duration_min=prop('number',minimum=0),duration_max=prop('number',minimum=0),quality=prop(),collection=prop(),tag_group=prop(maxLength=64),tag=prop(maxLength=64),tagged_by=prop(maxLength=40),query=prop(maxLength=200),topic=prop(),status=prop(enum=['','pending','accepted','rejected','uncertain']),after=prop('integer',minimum=0),limit=prop('integer',minimum=1,maximum=50))),
 tool('list_tags','Discover shared tag groups and tag names, with creators. Vocabulary is shared; assignments returned by search/get_motion include username and created_at. Paginate using next_after.',dict(tag_group=prop(maxLength=64),after=prop('integer',minimum=0),limit=prop('integer',minimum=1,maximum=200))),
 tool('quality_report','Read versioned quality and provenance; unknown checks are not passed.',dict(kind=prop(enum=['source','retarget']),id=prop()),['kind','id']),
 tool('get_motion','Read metadata for a public or owned motion; never starts generation.',dict(motion_id=prop()),['motion_id']),
 tool('generate_motion','First read get_prompt_guidance, rewrite locally, call check_motion_prompt and review the semantic checklist. Submit generation only, with a stable request_id for retries. Rewrite locally using your current agent/model before submission: 12–70 English words beginning "A person ", preserving intent and motion safety policy. Only exact_english is supported; server rewriting fee is zero, embedding is billed separately. Local subscription costs are external/unknown. original_description and rewrite_agent are optional self-reported provenance. Does NOT retarget. Poll get_job.',
      dict(seed=prop('integer',minimum=0,maximum=4294967295),parent_request_id=prop(),request_id=prop(minLength=8,maxLength=80),text=prop(minLength=2,maxLength=2000),n=prop('integer',minimum=1,maximum=10000),duration=prop('integer',minimum=1,maximum=120),prompt_mode=prop(enum=['exact_english'],default='exact_english'),original_description=prop(minLength=1,maxLength=2000),rewrite_agent=prop('object',properties=dict(name=prop(minLength=1,maxLength=120),model=prop(minLength=1,maxLength=120)),required=[],additionalProperties=False,minProperties=1)),['request_id','text'],False),
 tool('import_motion','Upload a user-selected local HY-compatible SMPL-H numeric NPZ (4 MiB, 30 FPS). Does not generate or retarget. id is a stable 32-hex import request ID; the resulting motion_id is mimp + id. If acknowledgement is lost, use workflow_recovery(kind=source,id=original_id), then its get_motion resume reference. Preserve the original ID and arguments on retry.',dict(id=prop(pattern='^[a-f0-9]{32}$'),file=prop(),text=prop(minLength=1,maxLength=2000)),['id','file','text'],False),
 tool('retarget_motion','Retarget an existing/imported motion for an explicit robot. Default reuses a prior successful result and its ORIGINAL parameters, even if new parameters/preset/parent are supplied. Set force_new=true only when the user requests a newly computed version or parameter experiment; it consumes compute and preserves old results. parent_job_id records lineage but does not force computation. Use a new stable 32-hex id for a requested new version; preserve the SAME id and arguments for retries. Poll get_job and inspect actual result_version/execution.',dict(id=prop(pattern='^[a-f0-9]{32}$'),motion_id=prop(),robot=prop(),parameters=prop('object'),preset_id=prop(pattern='^[a-f0-9]{32}$'),preset_version=prop('integer',minimum=1),parent_job_id=prop(),force_new=prop('boolean',default=False)),['id','motion_id','robot'],False),
 tool('get_job','Check generation/retarget job once. Generation includes paginated motion items, original and effective prompt. Poll no faster than every 10 seconds.',dict(kind=prop(enum=['generate','retarget']),id=prop(),after=prop('integer',minimum=0)),['kind','id']),
 tool('cancel_job','Request cancellation of your generation or retarget job; committed results are preserved.',dict(kind=prop(enum=['generate','retarget']),id=prop()),['kind','id'],False),
 tool('download_artifact','Download source NPZ by motion ID or retarget NPZ by job ID, to the configured local output folder. Returns absolute path and SHA256; never overwrite different content.',dict(kind=prop(enum=['source','retarget']),id=prop()),['kind','id'],False)
]

TOOLS.extend([
 tool('motion_task_capabilities','Read available task tools and model backends, including Kimodo and legacy generators.'),
 tool('list_motion_tasks','List your durable tasks before submitting new work after reconnecting.',dict(limit=prop('integer',minimum=1,maximum=100))),
 tool('get_motion_task','Read owned task, operations, candidates and evidence. Unknown checks are not passed.',dict(task_id=prop()),['task_id']),
 tool('create_motion_task','Create a durable goal using your current local agent. Preserve request_id on retries. Selection is a half-open frame range; a task is not an accepted asset.',
      dict(request_id=prop(minLength=8,maxLength=80),goal=prop(minLength=1,maxLength=4000),
           source_motion_id=prop(),selection=prop('object'),requirements=prop('array'),budget=prop('object')),
      ['request_id','goal'],False),
 tool('motion_task_step','Submit ONE decision from your current local model. action={kind:tool,tool:name,arguments:{}} or wait/finish with message. Preserve decision_id and exact action across retries. Read the task before another decision. Never switch to a server model. finish offers review; it does not accept a candidate.',
      dict(task_id=prop(),decision_id=prop(minLength=8,maxLength=80),action=prop('object')),
      ['task_id','decision_id','action'],False),
 tool('feedback_motion_task','Record user-authorized feedback; existing requirements remain. Do not invent user approval.',
      dict(task_id=prop(),message=prop(minLength=1,maxLength=4000),requirements=prop('array')),['task_id','message'],False),
 tool('cancel_motion_task','Stop further decisions. Already submitted external jobs remain visible and require explicit job cancellation.',dict(task_id=prop()),['task_id'],False),
 tool('import_motion_patch','Upload a user-selected numeric NPZ segment (at most 4 MiB) and splice it into the task selection. Preserve request_id on retries; creates a review candidate and never overwrites the source.',
      dict(task_id=prop(),request_id=prop(minLength=8,maxLength=80),file=prop()),['task_id','request_id','file'],False)
])


def validate(schema, arguments):
    import re
    if not isinstance(arguments,dict):raise ValueError('arguments must be an object')
    if set(arguments)-set(schema['properties']):raise ValueError('Unknown argument')
    if any(k not in arguments for k in schema['required']):raise ValueError('Missing required argument')
    for key,value in arguments.items():
        spec=schema['properties'][key]
        if spec['type']=='object':
            if not isinstance(value,dict):raise ValueError(key+' must be an object')
            if 'properties' in spec:validate(spec,value)
            if len(value)<spec.get('minProperties',0):raise ValueError('Empty '+key)
        if spec['type']=='string' and not isinstance(value,str):raise ValueError(key+' must be a string')
        if spec['type']=='number' and (type(value) not in (int,float) or not __import__('math').isfinite(value)):raise ValueError(key+' must be finite numeric')
        if spec['type']=='integer' and type(value) is not int:raise ValueError(key+' must be an integer')
        if spec['type']=='boolean' and type(value) is not bool:raise ValueError(key+' must be a boolean')
        if 'enum' in spec and value not in spec['enum']:raise ValueError('Invalid '+key)
        if isinstance(value,str):
            if len(value)<spec.get('minLength',0) or len(value)>spec.get('maxLength',10000):raise ValueError('Invalid length for '+key)
            if 'pattern' in spec and not re.fullmatch(spec['pattern'],value):raise ValueError('Invalid '+key)
        elif type(value) in (int,float) and not spec.get('minimum',-10**20)<=value<=spec.get('maximum',10**20):raise ValueError('Out of range: '+key)


def invoke(name, a, client=None):
    definition=next((t for t in TOOLS if t['name']==name),None)
    if definition is None:raise ValueError('Unknown tool')
    if name=='generate_motion' and isinstance(a,dict) and a.get('prompt_mode','exact_english')!='exact_english':raise ValueError(LOCAL_REWRITE_INSTRUCTION)
    validate(definition['inputSchema'],a)
    if name=='get_prompt_guidance':return prompt_guidance()
    if name=='check_motion_prompt':return check_motion_prompt(a['text'],a.get('duration',8))
    client=client or Client()
    if name=='workflow_recovery':
        journal=Journal(client)
        if a.get('discover'):
            if not a.get('kind') or a.get('id'):raise ValueError('Server discovery needs kind and no id; follow its per-kind next_after cursor')
            return journal.discover(a['kind'],a.get('limit',20),a.get('after',''))
        if bool(a.get('kind'))!=bool(a.get('id')):raise ValueError('Provide both kind and id, or neither')
        if a.get('verify_identity'):journal.verify_account()
        result=journal.recover(a.get('kind'),a.get('id'),a.get('limit',20),a.get('after',''))
        if a.get('verify_identity'):result['context'].update(identity_source='authenticated_server',online_authorization_checked=True)
        return result
    quote=urllib.parse.quote
    if name=='motion_task_capabilities':return client.request('harness_capabilities')
    if name=='list_motion_tasks':return client.request('harness_tasks?'+urllib.parse.urlencode(a))
    if name=='get_motion_task':return client.request('harness_task?id='+quote(a['task_id'],safe=''))
    if name=='feedback_motion_task':
        # Feedback appends requirements. A lost acknowledgement must be queried,
        # rather than automatically adding the same instruction a second time.
        return client.request('harness_feedback',a,retry=False)
    if name in ('create_motion_task','motion_task_step','cancel_motion_task'):
        endpoint={'create_motion_task':'harness_create','motion_task_step':'harness_step',
                  'feedback_motion_task':'harness_feedback','cancel_motion_task':'harness_cancel'}[name]
        return client.request(endpoint,a)
    if name=='import_motion_patch':
        path=Path(a['file']).expanduser()
        if not path.is_file() or path.stat().st_size>4*1024*1024:raise ValueError('Select a numeric NPZ segment of at most 4 MiB')
        with path.open('rb') as handle:raw=handle.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('Motion patch exceeds 4 MiB')
        return client.request('harness_import',dict(task_id=a['task_id'],request_id=a['request_id'],npz_base64=base64.b64encode(raw).decode()))
    if name=='motionfarm_info':return client.request('info')
    if name=='search_motions':return client.request('search?'+urllib.parse.urlencode(dict(**{k:a[k] for k in ('author','robot','duration_min','duration_max','quality','collection') if k in a},tag_group=a.get('tag_group',''),tag=a.get('tag',''),tagged_by=a.get('tagged_by',''),q=a.get('query',''),topic=a.get('topic',''),status=a.get('status',''),after=a.get('after',0),limit=a.get('limit',20))))
    if name=='list_tags':return client.request('tags?'+urllib.parse.urlencode(a))
    if name=='quality_report':
        result=client.request(('report' if a['kind']=='source' else 'retarget_report')+'?id='+quote(a['id']))
        Journal(client).ensure_account().update(a['kind'],a['id'],quality_report_fetched=True,quality_state='unverified; inspect returned checks, unknown is not passed')
        return result
    if name=='get_motion':
        result=client.request('motion?id='+quote(a['motion_id']))
        if a['motion_id'].startswith('mimp'):
            status = result.get('source_status','unverified')
            Journal(client).ensure_account().update('source',a['motion_id'][4:],server_motion_id=a['motion_id'],
                server_status=result.get('state','unknown'),resource_observed=True,
                source_status=status if status in SOURCE_STATUSES else 'unverified')
        return result
    if name=='generate_motion':
        payload=dict(generation_payload(a['request_id'],a['text'],prompt_mode=a.get('prompt_mode','exact_english'),n=a.get('n',1),duration=a.get('duration',8),original_description=a.get('original_description'),rewrite_agent=a.get('rewrite_agent')),**{k:a[k] for k in ('seed','parent_request_id') if k in a})
        return Journal(client).submit('generate',a['request_id'],payload,lambda:client.request('generate',payload))
    if name=='retarget_motion':
        payload=dict(a)
        if not payload.get('force_new'):payload.pop('force_new',None) # old journal hash remains compatible
        return Journal(client).submit('retarget',a['id'],payload,lambda:client.request('retarget',payload))
    if name=='import_motion':
        path=Path(a['file']).expanduser()
        if not path.is_file() or path.stat().st_size>4*1024*1024:raise ValueError('Select a numeric NPZ file of at most 4 MiB')
        with path.open('rb') as handle:data=handle.read(4*1024*1024+1)
        if len(data)>4*1024*1024:raise ValueError('Import exceeds 4 MiB')
        payload=dict(id=a['id'],text=a['text'],npz_base64=base64.b64encode(data).decode())
        return Journal(client).submit('source',a['id'],payload,lambda:client.request('import',payload))
    if name=='get_job':
        result=client.request(a['kind']+'?'+urllib.parse.urlencode(dict(id=a['id'],after=a.get('after',0))))
        job=result.get('job',result)
        Journal(client).ensure_account().update(a['kind'],a['id'],server_status=str(job.get('status','unknown'))[:80],next_after=result.get('next_after'))
        return result
    if name=='cancel_job':
        journal=Journal(client).ensure_account();journal.update(a['kind'],a['id'],cancellation='uncertain')
        result=client.request('cancel_generate' if a['kind']=='generate' else 'cancel',dict(id=a['id']))
        journal.update(a['kind'],a['id'],cancellation='requested; query server status')
        return result
    if name=='download_artifact':
        import re
        if not re.fullmatch('[A-Za-z0-9_-]{1,100}',a['id']):raise ValueError('Invalid artifact ID')
        root=Path(os.environ.get('MOTIONFARM_OUTPUT_DIR','./motionfarm-output')).expanduser().resolve()
        path=root/(a['id']+'-'+a['kind']+'.npz')
        if path.is_symlink() or (path.exists() and not path.is_file()):raise ValueError('Unsafe output path')
        result=download(client,a['id'],path,kind=a['kind'])
        Journal(client).ensure_account().update(a['kind'],a['id'],artifact=result)
        return result
    raise ValueError('Unknown tool')


def serve(input_stream=sys.stdin, output_stream=sys.stdout):
    initialized=False;ready=False
    def send(value):output_stream.write(json.dumps(value,ensure_ascii=False)+'\n');output_stream.flush()
    for line in input_stream:
        rid=None
        try:
            if len(line)>8*1024*1024:raise ValueError('Message too large')
            message=json.loads(line)
            if not isinstance(message,dict) or message.get('jsonrpc')!='2.0' or not isinstance(message.get('method'),str):
                send(dict(jsonrpc='2.0',id=None,error=dict(code=-32600,message='Invalid JSON-RPC request')));continue
            method=message['method'];rid=message.get('id');params=message.get('params',{})
            if 'id' not in message:
                if method=='notifications/initialized' and initialized:ready=True
                continue
            if not isinstance(params,dict):raise ValueError('params must be an object')
            if method=='initialize':
                versions=('2025-11-25','2025-06-18','2025-03-26','2024-11-05')
                version=params.get('protocolVersion');version=version if version in versions else versions[0]
                initialized=True
                result=dict(protocolVersion=version,capabilities={'tools':{'listChanged':False}},serverInfo=dict(name='motionfarm-gadget',version='0.9.1'),instructions=RECOVERY_INSTRUCTION+'\n\n'+LOCAL_REWRITE_INSTRUCTION+' Use your local agent/model. Tools are independent: search, generate, retarget. Never retarget unless requested. Default retargeting reuses an old successful version with its original parameters; for an explicitly requested new parameter version set force_new=true and preserve old results. Keep stable job IDs for retries and poll every 10 seconds or slower.\n\n'+prompt_guidance()['guide'])
            elif method=='ping':result={}
            elif not ready:raise ValueError('Initialize MCP before calling tools')
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':
                try:
                    value=invoke(params.get('name'),params.get('arguments',{}))
                    result=dict(content=[dict(type='text',text=json.dumps(value,ensure_ascii=False))],isError=False)
                except Exception as exc:
                    args=params.get('arguments',{});identity={k:args[k] for k in ('id','request_id') if isinstance(args,dict) and k in args}
                    result=dict(content=[dict(type='text',text=json.dumps(dict(error=str(exc),retry_identity=identity),ensure_ascii=False))],isError=True)
            else:
                send(dict(jsonrpc='2.0',id=rid,error=dict(code=-32601,message='Method not found')));continue
            send(dict(jsonrpc='2.0',id=rid,result=result))
        except json.JSONDecodeError:send(dict(jsonrpc='2.0',id=None,error=dict(code=-32700,message='Parse error')))
        except Exception as exc:send(dict(jsonrpc='2.0',id=rid,error=dict(code=-32602,message=str(exc))))


if __name__=='__main__':serve()
