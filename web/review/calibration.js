'use strict';
// Specialty drafts live only within the current login session, never in shared browser storage.
(()=>{
 const drafts=new Map();let draftSession=null,view=0,preserve=()=>{};
 const checkNames={penetration:'肢体穿透',sliding:'接地滑移',root_gait:'根轨迹与步态',semantic:'语义匹配',naturalness:'动作自然程度',ground_contact:'地面接触'};
 const verdictNames={uncertain:'无法确定',normal:'符合 / 正常',defect:'确认存在缺陷',disputed:'多人意见有争议'};
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
 const button=(text,fn)=>{const b=el('button',text);b.type='button';b.onclick=fn;return b;};
 function session(){if(draftSession!==state.session){drafts.clear();draftSession=state.session;preserve=()=>{};}return draftSession;}
 window.preserveMotionCalibration=()=>{if(draftSession===state.session)preserve();else session();};
 window.resetMotionCalibration=()=>{drafts.clear();draftSession=null;view++;preserve=()=>{};};
 const priorReset=window.resetResearch;window.resetResearch=()=>{window.resetMotionCalibration();priorReset?.();};
 window.motionCalibrationUI=(body,id,data)=>{
  session();const currentSession=state.session,seq=++view;let records=[],opinions=[],sha=null,after=null,selected='penetration',base=0,draftSha=null,hydrating=false,ready=false;
  const valid=()=>currentSession===state.session&&seq===view;
  const key=check=>JSON.stringify([currentSession,id,check]);
  const box=el('fieldset'),check=el('select'),verdict=el('select'),notes=el('textarea'),msg=el('p'),current=el('div'),history=el('div');
  box.append(el('legend','专项审核与误报反馈'));
  for(const [v,label] of Object.entries(checkNames))check.append(new Option(label,v));
  for(const v of ['uncertain','normal','defect'])verdict.append(new Option(verdictNames[v],v));
  check.setAttribute('aria-label','审核维度');verdict.setAttribute('aria-label','专项结论');notes.setAttribute('aria-label','专项审核备注');notes.placeholder='时间段、观察理由（可选）';notes.maxLength=2000;msg.setAttribute('role','status');
  const myOpinion=which=>opinions.find(x=>x.check_name===which)?.opinions.find(x=>x.reviewer===state.user?.username);
  function remember(){if(!valid()||hydrating||!ready)return;const own=myOpinion(selected);const dirty=verdict.value!==(own?.verdict||'uncertain')||notes.value!==(own?.notes||'');if(dirty)drafts.set(key(selected),{verdict:verdict.value,notes:notes.value,revision:base,source_sha256:draftSha});else drafts.delete(key(selected));}
  preserve=remember;
  function restore(){save.disabled=!ready;hydrating=true;const own=myOpinion(selected),draft=drafts.get(key(selected));base=draft?.revision??own?.id??0;draftSha=draft?.source_sha256||sha;verdict.value=draft?.verdict||own?.verdict||'uncertain';notes.value=draft?.notes??own?.notes??'';hydrating=false;
   msg.textContent=draft?'已恢复本次会话的未保存草稿；关闭报告或定位播放会保留。':'专项意见按审核员分别保留，多人结论不一致时进入争议状态。';
   if(draft&&draft.source_sha256!==sha){save.disabled=true;msg.textContent='草稿对应的动作源版本已变化，请重新观看后点“载入最新意见”重建草稿。';}
  }
  function render(){current.replaceChildren();for(const item of opinions){const p=el('p',checkNames[item.check_name]+'：'+verdictNames[item.verdict]);for(const r of item.opinions)p.append(el('span',' · '+r.reviewer+' '+verdictNames[r.verdict]));if(item.verdict==='disputed')p.append('；不计入可信校准样本。');current.append(p);}history.replaceChildren();for(const r of records){const p=el('p',`${checkNames[r.check_name]||r.check_name} · ${verdictNames[r.verdict]||r.verdict} · ${r.reviewer} · ${new Date(r.created*1000).toLocaleString()}${r.source_sha256!==sha?' · 历史源版本':''}`);p.append(el('div',r.notes||'无备注'));history.append(p);}more.hidden=after===null;}
  async function load(append=false){if(!append)box.disabled=true;try{const response=await api('/api/research/calibration?'+new URLSearchParams({motion_id:id,...(append&&after?{after}:{} )}));if(!valid())return;records=append?[...records,...response.labels]:response.labels;opinions=response.current;sha=response.source_sha256;after=response.next_after;ready=true;save.disabled=!!draftSha&&draftSha!==sha;render();if(save.disabled)msg.textContent='草稿对应的动作源版本已变化，请重新观看并载入最新意见；旧草稿仍保留。';}finally{if(valid()&&!append)box.disabled=false;}}
  const save=button('保存专项结论',async()=>{if(!ready)return;if(draftSha!==sha){save.disabled=true;msg.textContent='草稿源版本已变化，请重新观看动作，再载入最新意见。';return;}remember();const submission={motion_id:id,check:selected,verdict:verdict.value,notes:notes.value,revision:base,source_sha256:draftSha};box.disabled=true;try{const result=await api('/api/research/calibration',{method:'POST',body:JSON.stringify(submission)});if(!valid())return;drafts.delete(key(submission.check));opinions=result.current;sha=result.source_sha256;draftSha=sha;base=result.event.id;records=[result.event,...records.filter(x=>x.id!==result.event.id)];render();window.refreshMotionAssessment?.(id);msg.textContent=opinions.find(x=>x.check_name===submission.check)?.verdict==='disputed'?'已保存。此项与其他审核员意见不一致，已标记争议并排除可信校准统计。':'已保存，审核记录已更新。';}
   catch(e){if(valid()){msg.textContent=e.message+'；草稿仍保留。';if(e.status===409){rebase.hidden=false;if(e.current?.source_sha256&&e.current.source_sha256!==draftSha){sha=e.current.source_sha256;save.disabled=true;msg.textContent='动作源版本已变化；旧草稿已保留，请重新观看并载入最新意见。';}}}}
   finally{if(valid())box.disabled=false;}});
  const rebase=button('载入最新意见并保留我的草稿',async()=>{remember();try{await load();if(!valid())return;const draft=drafts.get(key(selected));if(draft&&draft.source_sha256!==sha){save.disabled=true;msg.textContent='源版本变化，请使用“载入最新意见”并重新复核。';return;}base=myOpinion(selected)?.id||0;if(draft)draft.revision=base;rebase.hidden=true;msg.textContent='已载入最新版本；请核对保留的草稿，再保存。';}catch(e){if(valid())msg.textContent=e.message;}});rebase.hidden=true;
  const reload=button('载入最新意见',async()=>{remember();if(drafts.has(key(selected))&&!confirm('载入最新意见会放弃此维度的未保存草稿，继续吗？'))return;drafts.delete(key(selected));try{await load();if(valid())restore();}catch(e){if(valid())msg.textContent=e.message;}});
  const more=button('加载更早的专项记录',async()=>{more.disabled=true;try{await load(true);}catch(e){if(valid())msg.textContent=e.message;}finally{if(valid())more.disabled=false;}});more.hidden=true;
  check.onchange=()=>{remember();selected=check.value;restore();};verdict.onchange=remember;notes.oninput=remember;
  box.append(check,verdict,notes,save,reload,rebase,msg);body.append(box,current,el('h3','专项审核记录'),history,more);save.disabled=true;msg.textContent='正在读取专项意见…';
  load().then(()=>{if(valid())restore();}).catch(e=>{if(valid())msg.textContent='专项意见读取失败：'+e.message;});
  if(data.calibration?.similar?.length){const p=el('p','动作相似候选（需人工比较）：');for(const item of data.calibration.similar)p.append(button(item.other_id,async()=>{remember();$('research-report').close();await window.openMotionFromResults(item.other_id,{items:[{motion_id:item.other_id}],source:'相似动作候选'});}));body.append(p);}
 };
})();
