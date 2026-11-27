"""Deterministic context selection; never load a conversation or full history."""
import json

BUDGETS={'generate':16000,'review':12000}  # UTF-8 bytes, not a guessed token count.
OUTPUT_TOKENS={'generate':384,'review':384}


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def prepare(name,system,payload):
    if name not in BUDGETS:raise ValueError('Unknown agent stage')
    topics=payload.get('topics',[])
    if len(topics)!=1:raise ValueError('Exactly one topic per request is required')
    topic={k:v for k,v in topics[0].items() if k in ('id','name','definition')}
    history=payload.get('history_json',{}).get(topic['id'],{})
    examples=[];seen=set()
    for entry in history.get('examples',[]):
        text=entry.get('text');key=entry.get('id')
        if isinstance(text,str) and len(text)<=1000 and isinstance(key,str) and key not in seen:
            examples.append(dict(id=key,text=text));seen.add(key)
        if len(examples)>=6:break
    result=dict(topics=[topic],duration_s=payload['duration_s'],
        context_scope='Bounded sample only. Omitted history is unknown; never invent historical examples.')
    if name=='generate':
        summary=dict(total_saved_prompts=history.get('total_saved_prompts',0),history_is_sampled=True,
            family_counts={str(k)[:80]:v for k,v in list(history.get('family_counts',{}).items())[:12]},examples=examples)
        feedback=[]
        for entry in payload.get('rejected_feedback',[])[-2:]:
            feedback.append({k:str(v)[:1000 if k in ('text','reference_text') else 240]
                for k,v in entry.items() if k in ('reason','text','reference_text','evidence_text_id',
                    'difference_axes','rewrite_hint','format_error','review_format_error','instruction')})
        result.update(count_per_topic=1,history_json={topic['id']:summary},rejected_feedback=feedback,
            output_template={'prompts':[{'topic_id':topic['id'],'text':'A person ...',
                'attributes':{'direction':'English direction','speed':'English pace','style':'English movement style'}}]},
            output_rule='Use exactly these keys and their spelling. No extra attributes. Cue names belong in the text, not as extra JSON keys. Write complete English descriptions.')
        for key in ('exploration_plan','measured_motion_coverage'):
            value=history.get(key)
            if isinstance(value,dict) and len(encode(value).encode())<=1800:
                result[key]=value
        exploration=history.get('exploration')
        if isinstance(exploration,dict) and len(encode(exploration).encode())<=4500:
            result['exploration_memory']=exploration
    else:
        candidates=payload.get('candidates',[])
        if len(candidates)!=1:raise ValueError('Exactly one candidate per review is required')
        matches=[];seen=set()
        for group in payload.get('candidate_neighbors',[]):
            for entry in group.get('matches',[]):
                key=entry.get('text_id');text=entry.get('text')
                if isinstance(key,str) and isinstance(text,str) and len(text)<=1000 and key not in seen:
                    from g2h.motion_traits import explicit_differences
                    match={k:entry[k] for k in ('text_id','text','similarity') if k in entry}
                    match['explicit_difference_axes']=explicit_differences(candidates[0]['text'],text)
                    matches.append(match);seen.add(key)
                if len(matches)>=5:break
            if len(matches)>=5:break
        # Degraded mode still gets concrete examples, never just family labels.
        if not matches:matches=[dict(text_id=e['id'],text=e['text']) for e in examples[:5]]
        result.update(candidates=candidates,candidate_neighbors=[dict(candidate_index=0,matches=matches)])
        conflicts=payload.get('review_conflicts',[])
        if conflicts:
            if len(conflicts)!=1:raise ValueError('Expected one bounded review conflict')
            conflict=conflicts[0]
            if conflict.get('text')!=candidates[0]['text']:raise ValueError('Conflict candidate mismatch')
            if not any(m['text_id']==conflict.get('evidence_text_id') and
                       m['text']==conflict.get('reference_text') for m in matches):
                raise ValueError('Conflict evidence missing')
            from g2h.motion_traits import traits
            detailed=[]
            for c in conflicts:
                aa,bb=traits(c['text']),traits(c['reference_text'])
                terms={axis:sorted(set(aa[axis]) ^ set(bb[axis])) for axis in c['difference_axes']}
                detailed.append(dict(c, differing_terms=terms))
            result['review_conflicts']=detailed
            result['recheck_scope']='Re-evaluate this same candidate against the cited text. Differences are lexical hints, not proof of validity. Explicitly name each differing_terms value in the explanation and reconcile its meaning in both texts. Do not simply repeat that the main action matches. Do not default to acceptance; uncertain is allowed.'
    def size():return len(system.encode('utf-8'))+len(encode(result).encode('utf-8'))
    dropped=0
    while size()>BUDGETS[name]:
        if name=='review':
            matches=result['candidate_neighbors'][0]['matches']
            protected={c['evidence_text_id'] for c in result.get('review_conflicts',[])}
            removable=next((i for i in range(len(matches)-1,-1,-1) if matches[i]['text_id'] not in protected),None)
            if removable is not None:matches.pop(removable);dropped+=1;continue
        else:
            summary=result['history_json'][topic['id']]
            if summary['examples']:summary['examples'].pop();dropped+=1;continue
            if summary['family_counts']:summary['family_counts'].popitem();dropped+=1;continue
            if result['rejected_feedback']:result['rejected_feedback'].pop(0);dropped+=1;continue
            if result.pop('exploration_memory',None) is not None:dropped+=1;continue
            if result.pop('measured_motion_coverage',None) is not None:dropped+=1;continue
            if result.pop('exploration_plan',None) is not None:dropped+=1;continue
        raise ValueError('Topic/candidate exceeds context budget; shorten configuration before retrying')
    evidence={m['text_id']:m['text'] for g in result.get('candidate_neighbors',[]) for m in g['matches']}
    return result,dict(input_bytes=size(),byte_budget=BUDGETS[name],dropped_items=dropped,
        output_token_limit=OUTPUT_TOKENS[name],evidence_ids=list(evidence)),evidence
