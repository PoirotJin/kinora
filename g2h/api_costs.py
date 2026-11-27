"""Local configured tariff estimates, never provider invoices. No credentials/prompts."""
import json
import math
import sqlite3
import re
from contextlib import closing
from pathlib import Path


def load_prices(path):
    path=Path(path)
    if not path.exists():return {'version':None,'prices':[],'state':'unconfigured'}
    try:
        if path.stat().st_size>262144:raise ValueError('config too large')
        value=json.loads(path.read_text())
        if not isinstance(value.get('version'),str) or not isinstance(value.get('prices'),list):raise ValueError('invalid format')
        for price in value['prices']:
            if not all(isinstance(price.get(k),str) and price[k] for k in ('model','kind','currency')):raise ValueError('missing identifiers')
            for k in ('input_per_million','cached_input_per_million','output_per_million'):
                v=price.get(k)
                if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0:raise ValueError('invalid rate')
        return dict(value,state='configured')
    except (OSError,ValueError,TypeError,AttributeError):return {'version':None,'prices':[],'state':'invalid'}


def estimate(prices,model,kind,provider,inp,out,cached,known):
    base={'currency':None,'amount':None,'price_version':prices.get('version'),'state':'unknown_usage' if not known else 'unknown_price'}
    if not known:return base
    matches=[p for p in prices['prices'] if p['model']==model and p['kind']==kind and p.get('provider','')==provider]
    if len(matches)!=1:return base
    p=matches[0];base['currency']=p['currency']
    if cached is None and p['cached_input_per_million']!=p['input_per_million']:
        return dict(base,state='unknown_cached_tokens')
    cached=cached or 0
    if cached>inp:return dict(base,state='invalid_cached_tokens')
    components=dict(input=(inp-cached)*p['input_per_million']/1e6,cached_input=cached*p['cached_input_per_million']/1e6,output=out*p['output_per_million']/1e6)
    return dict(base,amount=sum(components.values()),state='configured_estimate',details=dict(rates={key:p[key] for key in ('input_per_million','cached_input_per_million','output_per_million')},components=components))


def summarize(db,prices_path,limit=20000):
    prices=load_prices(prices_path)
    has=db.execute("SELECT 1 FROM sqlite_master WHERE name='cost_events'").fetchone()
    extra='c.username,c.provider,c.purpose,c.job_id,c.cached,c.currency,c.amount,c.price_version,c.state,c.event_id,c.details' if has else ','.join(['NULL']*11)
    join='LEFT JOIN cost_events c ON c.event_id=e.id' if has else ''
    rows=db.execute('SELECT e.ts,e.model,e.kind,e.input,e.output,e.known,e.total,'+extra+',e.status FROM events e '+join+' ORDER BY e.ts DESC LIMIT ?', (limit+1,)).fetchall()
    truncated=len(rows)>limit;rows=rows[:limit];groups={};currencies={};unknown=0;historical=0;components={};unknown_reasons={}
    for row in rows:
        ts,model,kind,inp,out,known,total,user,provider,purpose,job,cached,currency,amount,version,state,eid,details,status=row
        detail=json.loads(details) if details else {}
        if eid is None:
            value=estimate(prices,model,kind,'',inp,out,None,known and inp+out==total)
            detail=value.get('details',{})
            currency,amount,state=value['currency'],value['amount'],value['state'];historical+=1
            if amount is not None:state='historical_estimate'
        key=(model,kind,provider or '',purpose or '',user or '',job or '')
        group=groups.setdefault(key,dict(model=model,kind=kind,provider=provider or '',purpose=purpose or '',username=user or '',job_id=job or '',calls=0,unknown=0,currencies={},historical=0))
        group['calls']+=1
        if eid is None:group['historical']+=1
        if amount is None:
            unknown+=1;group['unknown']+=1
            unknown_reasons[state]=unknown_reasons.get(state,0)+1
        else:
            parts=components.setdefault(currency,dict(input=0,cached_input=0,output=0))
            for k,v in detail.get('components',{}).items():parts[k]=parts.get(k,0)+v
            currencies[currency]=currencies.get(currency,0)+amount
            group['currencies'][currency]=group['currencies'].get(currency,0)+amount
    return dict(available=True,topic_efficiency=topic_efficiency(rows,Path(prices_path).parent.parent,truncated),lifetime=lifetime_reference(db,prices),pricing_state=prices['state'],price_version=prices.get('version'),calls=len(rows),unknown=unknown,unknown_reasons=unknown_reasons,currencies=currencies,components=components,historical_calls=historical,truncated=truncated,limit=limit,oldest_ts=rows[-1][0] if rows else None,groups=list(groups.values()),scope='按已记录请求和配置单价估算；非供应商账单。不同币种不合并；未知费用不计作零。最多最近 20,000 个请求。')


def lifetime_reference(db,prices):
    """Small cumulative token counters priced today; not frozen/actual spend."""
    rows=db.execute('SELECT model,kind,calls,input,output,unknown FROM totals LIMIT 1001').fetchall()
    result=[];currencies={};unknown=0;unpriced_calls=0
    for model,kind,calls,inp,out,missing in rows[:1000]:
        unknown+=missing
        item=dict(model=model,kind=kind,calls=calls,input=inp,output=out,unknown_usage=missing,currency=None,minimum=None,maximum=None)
        matches=[p for p in prices['prices'] if p['model']==model and p['kind']==kind and p.get('provider','')=='']
        if len(matches)==1:
            p=matches[0];minimum=inp*min(p['input_per_million'],p['cached_input_per_million'])/1e6+out*p['output_per_million']/1e6
            maximum=inp*max(p['input_per_million'],p['cached_input_per_million'])/1e6+out*p['output_per_million']/1e6
            item.update(currency=p['currency'],minimum=minimum,maximum=maximum,output_cost=out*p['output_per_million']/1e6,input_minimum=minimum-out*p['output_per_million']/1e6,input_maximum=maximum-out*p['output_per_million']/1e6)
            c=currencies.setdefault(p['currency'],dict(minimum=0,maximum=0));c['minimum']+=minimum;c['maximum']+=maximum
        else:unpriced_calls+=calls
        result.append(item)
    return dict(models=result,currencies=currencies,unknown_usage=unknown,unpriced_calls=unpriced_calls,truncated=len(rows)>1000,scope='累计已记录用量按当前单价的参考范围；缓存输入按全部命中至全部未命中估算。历史单价变化未还原，非实际账单；无用量请求的额外费用未知。')


def topic_efficiency(rows, root, truncated=False):
    """Compare output of the same attributed runs, never lifetime denominators.

    Read at most 100 runs, five small manifests per run, and indexed identities.
    Missing journals/attribution remain unknown instead of reporting free output.
    """
    def read_db(path):
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.15)
        db.set_progress_handler(lambda: 1 if __import__('time').monotonic()>deadline else 0,1000)
        return db
    import time
    deadline=time.monotonic()+1.5
    result=dict(available=False,topics=[],unattributed_calls=0,excluded_runs=0,
        scope='最近请求所属生产批次的当前产出；不是全库均摊。仅统计归属明确的 API 费用，不含 GPU/本地订阅。未知金额不作零；不同币种分别计算。未归属调用不分摊，单位成本仅为已归属部分。')
    try:
        with closing(read_db(root/'prompts.sqlite3')) as catalog:
            topics=[r[0] for r in catalog.execute('SELECT topic FROM topic_counts LIMIT 101') if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',r[0])]
            topics.sort(key=len,reverse=True)
            runs={}
            for row in rows:
                job=row[10] or ''
                topic=next((t for t in topics if job.startswith('auto:') and job.endswith('_'+t)),None)
                if not topic:
                    result['unattributed_calls']+=1;continue
                runs.setdefault(job,dict(topic=topic,rows=[]))['rows'].append(row)
            aggregates={}
            for index,(job,run) in enumerate(runs.items()):
                if index>=100 or time.monotonic()>deadline:
                    result['excluded_runs']+=1;continue
                topic=run['topic']; key=job[5:]
                if not key or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in key):continue
                group=aggregates.setdefault(topic,dict(topic=topic,runs=0,calls=0,unknown=0,currencies={},purposes={},failed_calls=0,failed_currencies={},texts=0,motions=0,accepted=0,output_unknown=False,window_partial=False))
                group['runs']+=1
                if truncated and any(r[0]==rows[-1][0] for r in run['rows']):group['window_partial']=True
                for row in run['rows']:
                    group['calls']+=1
                    currency,amount=row[12],row[13]
                    purpose=row[9] or 'unrecorded'
                    part=group['purposes'].setdefault(purpose,dict(calls=0,unknown=0,currencies={}))
                    part['calls']+=1
                    if amount is None:group['unknown']+=1;part['unknown']+=1
                    else:
                        group['currencies'][currency]=group['currencies'].get(currency,0)+amount
                        part['currencies'][currency]=part['currencies'].get(currency,0)+amount
                    if row[-1]!='ok':
                        group['failed_calls']+=1
                        if amount is not None:group['failed_currencies'][currency]=group['failed_currencies'].get(currency,0)+amount
                ids=set(); found=False
                for attempt in range(1,6):
                    path=root/'agent_runs'/key/('attempt_%02d'%attempt)/'manifest.json'
                    if not path.exists():continue
                    if path.stat().st_size>1048576:raise ValueError('manifest too large')
                    manifest=json.loads(path.read_text())
                    found=True
                    for item in manifest['prompts']:
                        if item.get('topic_id')==topic and item.get('source',{}).get('run_id')==key:ids.add(item['id'])
                if not found:
                    group['output_unknown']=True;continue
                with closing(read_db(root/'topics'/topic/'index.sqlite3')) as motions, closing(read_db(root/'review/reviews.sqlite3')) as reviews:
                    has_removed=reviews.execute("SELECT 1 FROM sqlite_master WHERE name='removed'").fetchone()
                    def short_id(number):
                        text=""
                        while number:
                            number,digit=divmod(number,36);text="0123456789abcdefghijklmnopqrstuvwxyz"[digit]+text
                        return "m"+text.rjust(8,"0")
                    for identity in ids:
                        existing=catalog.execute('SELECT item FROM prompts WHERE id=? AND topic=?',(identity,topic)).fetchone()
                        if not existing or json.loads(existing[0]).get('source',{}).get('run_id')!=key:continue
                        group['texts']+=1
                        links=catalog.execute('SELECT number,task_id FROM motion_links WHERE text_id=?',(identity,)).fetchall()
                        for number,task in links:
                            if not motions.execute('SELECT 1 FROM records WHERE id=?',(task,)).fetchone():continue
                            group['motions']+=1
                            review=reviews.execute('SELECT status FROM latest WHERE motion_id=?',(short_id(number),)).fetchone()
                            removed=has_removed and reviews.execute('SELECT 1 FROM removed WHERE motion_id=?',(short_id(number),)).fetchone()
                            if review and review[0]=='accepted' and not removed:group['accepted']+=1
            for group in aggregates.values():
                valid=not(group['unknown'] or group['output_unknown'] or group['window_partial'])
                group['per_unit']={name:({c:v/group[name] for c,v in group['currencies'].items()} if valid and group[name] else None) for name in ('texts','motions','accepted')}
            result.update(available=True,topics=list(aggregates.values()))
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        result['reason']='批次产出记录缺失、格式异常或查询超时；不能推算单位成本'
    return result
