"""Administrator-configured prompt rewrite adapters. Credentials never leave the server."""
import json
from pathlib import Path
import re
import urllib.request
import urllib.error
from g2h.api_errors import NoRedirect, TemporaryAPIError, http_failure

DEFAULT='qwen-default'

def registry(root):
    path=Path(root)/'review/rewrite_models.json'
    models=[dict(id=DEFAULT,label='Qwen · qwen3.8-flash',provider='dashscope',model='qwen3.8-flash',adapter='qwen',key_file=str(Path.home()/'.config/gen2humanoid/dashscope_api_key'),enabled=True)]
    if path.exists():
        if path.stat().st_size>65536:raise ValueError('重写模型配置过大')
        models=json.loads(path.read_text()).get('models',[])
    if not isinstance(models,list) or not 1<=len(models)<=30:raise ValueError('无效重写模型配置')
    seen=set()
    for row in models:
        if not isinstance(row,dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',str(row.get('id',''))) or row['id'] in seen:raise ValueError('无效或重复的重写模型编号')
        seen.add(row['id'])
        if row.get('adapter') not in ('qwen','openai_compatible'):raise ValueError('不支持的重写模型适配器')
        for field in ('provider','model','label','key_file'):
            if not isinstance(row.get(field),str) or not row[field] or len(row[field])>512:raise ValueError('重写模型缺少配置字段')
        if row['adapter']=='openai_compatible' and not str(row.get('base_url','')).startswith('https://'):raise ValueError('重写 API 必须使用 HTTPS')
    return models

def resolve(root,key):
    if not isinstance(key,str):raise ValueError('无效重写模型编号')
    row=next((r for r in registry(root) if r['id']==key and r.get('enabled',True)),None)
    if row is None:raise ValueError('重写模型不存在或已停用，请重新选择')
    return row

def public_models(root):
    return {'models':[dict(id=r['id'],label=r['label'],provider=r['provider'],model=r['model'],configured=Path(r['key_file']).expanduser().is_file()) for r in registry(root) if r.get('enabled',True)],'default':DEFAULT,'scope':'服务器仅提供管理员配置的模型；英文原文直传不调用重写 API，Embedding 仍单独计费。'}

def call(root,key,messages,max_tokens=512):
    model=resolve(root,key)
    if model['adapter']=='qwen':
        import MotionFarm as farm
        return farm.call_qwen(model['model'],Path(model['key_file']).expanduser(),messages,temperature=.3,max_tokens=max_tokens)
    secret=Path(model['key_file']).expanduser().read_text().strip()
    if not secret or not secret.isascii() or any(c.isspace() for c in secret):raise ValueError('重写 API 密钥格式无效')
    payload=dict(model=model['model'],messages=messages,temperature=.3,max_tokens=max_tokens,response_format={'type':'json_object'})
    request=urllib.request.Request(model['base_url'].rstrip('/')+'/chat/completions',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
    from g2h.api_usage import budgeted_request,record
    with budgeted_request(model['model'],'chat',messages,max_tokens):
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=60) as response:raw=response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError('重写 API 响应过大')
            result=json.loads(raw);record(model['model'],'chat',result);return result
        except urllib.error.HTTPError as exc:
            record(model['model'],'chat',status='http_'+str(exc.code));raise http_failure(exc,model['provider']) from None
        except (urllib.error.URLError,TimeoutError,ConnectionError):
            record(model['model'],'chat',status='transport_error');raise TemporaryAPIError('重写 API 暂时不可用') from None
        except ValueError:
            record(model['model'],'chat',status='invalid_response');raise
