"""Bounded active review selection; never writes final human reviews or source motions."""
from contextlib import closing
import hashlib,json,math,sqlite3,time
from pathlib import Path
VERSION='assessment-v1'
CHECKS={'semantic':'语义匹配','penetration':'肢体穿透','sliding':'接地滑移','root_gait':'根轨迹与步态','naturalness':'动作自然程度','ground_contact':'地面接触'}
MAP={'penetration':'self_collision_proxy','sliding':'stance_slide','root_gait':'root_gait_consistency'}

def summarize(machine,opinions=(),source_sha=None):
    matching=bool(source_sha and machine.get('source_sha256')==source_sha)
    evidence=machine.get('evidence') or {};body=evidence.get('body_quality') or {}
    dimensions=[];reasons=[]
    for key,title in CHECKS.items():
        score=None;state='unknown';measurement={};basis='未检查'
        if matching and key=='semantic':
            value=machine.get('score')
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
                score=round(value*100,2);basis='TMR 原始相似度，非正确率'
                state='pass' if machine.get('state')=='auto_passed' else 'uncertain'
                measurement={'similarity':value,'calibration':machine.get('calibration')}
        elif matching and key in MAP:
            value=(body.get('checks') or {}).get(MAP[key]);basis='人体骨架/胶囊代理检查'
            if value is True:score=100;state='pass'
            elif value is False:score=0;state='flagged'
            measurement={'metrics':body.get('metrics',{}),'thresholds':body.get('thresholds',{}),'events':(body.get('events') or {}).get({'penetration':'self_collision','sliding':'stance_slide','root_gait':'root_gait_mismatch'}[key],[])}
        human=next((x for x in opinions if x['check_name']==key and x['source_sha256']==source_sha),None)
        human_verdict=human['verdict'] if human else None
        resolved=human_verdict in ('normal','defect')
        if human_verdict in ('uncertain','disputed'):reasons.append(title+'：人工意见不确定或有争议')
        elif not resolved and state in ('unknown','uncertain','flagged'):reasons.append(title+'：'+{'unknown':'缺少适用的检查证据','uncertain':'语义分数尚不足以自动判定','flagged':'检测到可疑缺陷'}[state])
        if resolved and ((human_verdict=='normal' and state=='flagged') or (human_verdict=='defect' and state=='pass')):reasons.append(title+'：人工与机器证据冲突')
        dimensions.append({'check':key,'title':title,'score':score,'state':state,'basis':basis,'measurement':measurement,'human_verdict':human_verdict})
    if not matching:reasons.insert(0,'尚未取得当前动作源版本的机器评分')
    defects=[x['title'] for x in dimensions if x['human_verdict']=='defect']
    return {'version':VERSION,'state':'needs_review' if reasons else 'human_defect' if defects else 'human_dimensions_complete','dimensions':dimensions,'reasons':list(dict.fromkeys(reasons)),'source_sha256':source_sha,'detector_version':machine.get('version'),'human_status_unchanged':True,'score_note':'0/100仅表示代理检查未通过/通过，不是缺陷概率；未知项不计分。自然程度及地面接触先由人工标注。'}

def detail(store,user,mid):
    from review_calibration import _source,evidence
    from g2h.auto_review import detail as machine_detail
    source,sha=_source(store,user,mid)
    labels=evidence(store.root,mid,sha)
    result=summarize(machine_detail(store.root,mid),labels['current'],sha)
    result.update(motion_id=mid,topic=source['topic'],human_review=source.get('review'))
    return result

def queue(store,user,values):
    from review_access import Forbidden
    limit=int(values.get('limit',20));topic=values.get('topic','');lane=values.get('lane','active')
    if not 1<=limit<=40 or lane not in ('active','random'):raise ValueError('无效审核队列参数')
    path=Path(store.root)/'review/auto_review.sqlite3'
    if not path.exists():return {'items':[],'state':'no_scores','candidate_limit':4000}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        v=db.execute("SELECT value FROM runtime WHERE key='version'").fetchone()
        if not v:return {'items':[],'state':'no_active_detector','candidate_limit':4000}
        where='version=?'+(' AND topic=?' if topic else '');args=[v[0]]+([topic] if topic else [])
        rows=[]
        for order in ('DESC','ASC'):
            rows.extend(dict(r) for r in db.execute('SELECT motion_id,text_id,topic,score,flags,source_sha256,created FROM scores WHERE '+where+' ORDER BY created '+order+' LIMIT 2000',args))
    candidates={r['motion_id']:r for r in rows};ranked=[]
    # Bring recent human uncertainty/disputes into view even outside the machine candidate window.
    disputed=set();op=Path(store.root)/'review/operations.sqlite3'
    if op.exists():
        with closing(sqlite3.connect(op.resolve().as_uri()+'?mode=ro',uri=True)) as labels:
            opinions={};latest=labels.execute('SELECT motion_id,source_sha256,check_name,reviewer,verdict FROM labels ORDER BY id DESC LIMIT 1000').fetchall()
            for mid,sha,check,reviewer,verdict in latest:
                opinions.setdefault((mid,sha,check,reviewer),verdict)
            groups={}
            for (mid,sha,check,reviewer),verdict in opinions.items():groups.setdefault((mid,sha,check),set()).add(verdict)
            disputed={mid for (mid,sha,check),vs in groups.items() if 'uncertain' in vs or {'normal','defect'}<=vs}
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as scores:
            scores.row_factory=sqlite3.Row
            for mid in sorted(disputed)[:100]:
                row=scores.execute('SELECT motion_id,text_id,topic,score,flags,source_sha256,created FROM scores WHERE version=? AND motion_id=?',(v[0],mid)).fetchone()
                if row and (not topic or row['topic']==topic):candidates[mid]=dict(row)

    for r in candidates.values():
        flags=json.loads(r['flags']);score=r['score']
        selection='missing_evidence' if score is None else 'detector_flag' if flags else 'semantic_boundary'
        priority=4 if r['motion_id'] in disputed else 3 if score is None else 2 if flags else 1-abs(score-.5)
        if lane=='random':priority=int(hashlib.sha256((time.strftime('%Y-%m-%d',time.gmtime())+r['motion_id']).encode()).hexdigest()[:12],16);selection='random_audit'
        ranked.append((priority,r,selection))
    ranked.sort(key=lambda x:(x[0],x[1]['created']),reverse=True)
    items=[];groups=set()
    # Only bounded source/access reads; unavailable/private/removed motions never enter queue.
    inspected=0
    for _,r,selection in ranked:
        if len(items)>=limit or inspected>=120:break
        if lane=='active' and r['text_id'] in groups:continue
        inspected+=1
        try:a=detail(store,user,r['motion_id'])
        except (ValueError,Forbidden,PermissionError):continue
        human=a.get('human_review') or {}
        if human.get('status') in ('accepted','rejected') and human.get('motion_sha256')==a['source_sha256'] and r['motion_id'] not in disputed:continue
        if lane=='active' and a['state']!='needs_review':continue
        if any('冲突' in x or '争议' in x for x in a['reasons']):selection='evidence_conflict'
        items.append(dict(motion_id=r['motion_id'],topic=r['topic'],text_id=r['text_id'],selection=selection,reasons=a['reasons'],dimensions=a['dimensions'],state=a['state']))
        groups.add(r['text_id'])
    return {'items':items,'state':'ready','lane':lane,'candidate_limit':4000,'inspected':inspected,'detector_version':v[0],'scope':'当前检测版本的最近及最早各2000条候选；主动队列按描述去重，仅选择可访问公共动作。随机抽查独立入口，不用于证明全库错误率。','human_status_unchanged':True}
