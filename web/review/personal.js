'use strict';
(()=>{
 let motionItems=[],epoch=0,motionAfter=null,presetAfter=null,editing=null,saving=false,dirty=false,historyAfter=null,historyId=null;
 let presetEpoch=0,presetBusy=false,presetIds=new Set(),historyEpoch=0,historyBusy=false,editorRevision=0;
 const node=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
 const date=t=>new Date(t*1000).toLocaleString('zh-CN');
 function notify(text,error=false){$('personal-notice').textContent=text;$('personal-notice').classList.toggle('error',error);}
 window.closePersonal=()=>{epoch++;presetEpoch++;presetBusy=false;invalidateHistory();$('personal').hidden=true;$('show-personal').setAttribute('aria-pressed','false');};
 window.resetPersonal=()=>{motionItems=[];closePersonal();saving=false;$('preset-save').disabled=false;editing=null;dirty=false;editorRevision++;presetIds.clear();presetAfter=null;$('personal-preset-view').value='active';$('preset-editor').close();$('preset-history').close();$('personal-motions').replaceChildren();$('personal-presets').replaceChildren();$('preset-versions').replaceChildren();$('preset-form').reset();$('preset-robot').replaceChildren();$('preset-message').textContent='';$('preset-history-message').textContent='';notify('');};
 function valid(seq){return seq===epoch&&state.user&&!$('personal').hidden;}
 async function loadMotions(append=false){
  const seq=epoch;$('personal-motion-more').disabled=true;
  try{
   const data=await api('/api/personal/motions?after='+(append?motionAfter||0:0));if(!valid(seq))return;
   if(!append){motionItems=[];$('personal-motions').replaceChildren();}motionItems.push(...data.items);
   for(const item of data.items){
    const card=node('article');card.className='gallery-card';card.dataset.motion=item.motion_id;
    card.append(node('strong',item.motion_id),node('p',item.text),node('p',`${item.topic} · ${item.owner} · ${item.storage_tier==='cold'?'冷存储':'已生成'}`));
    const actions=node('div');actions.className='production-actions';
    const view=node('button','查看 / 播放');view.onclick=()=>window.openMotionFromResults(item.motion_id,{items:motionItems,source:'我的动作 · 当前已载入结果'});
    const retarget=node('button','重定向');retarget.onclick=()=>showRetarget(item.motion_id);actions.append(view,retarget);card.append(actions);
    window.motionResearchActions?.(card,item);window.renderMotionTags?.(card,item);$('personal-motions').append(card);
   }
   motionAfter=data.next_after;$('personal-motion-more').hidden=motionAfter===null;
   $('personal-motion-summary').textContent=$('personal-motions').children.length?`已载入 ${$('personal-motions').children.length} 个动作`:(motionAfter===null?'还没有已生成的个人动作。可前往“定制生成”创建。':'这一页暂无已完成动作，可继续加载。');
  }catch(e){if(valid(seq))notify(e.message,true);}finally{if(valid(seq))$('personal-motion-more').disabled=false;}
 }
 async function loadPresets(append=false,highlight=null){
  if(append&&presetBusy)return;
  const seq=epoch,request=++presetEpoch,archived=$('personal-preset-view').value==='archived';presetBusy=true;$('personal-preset-more').disabled=true;
  const current=()=>valid(seq)&&request===presetEpoch;
  try{
   const query=new URLSearchParams();if(archived)query.set('archived','1');if(append)query.set('after',presetAfter);
   const data=await api('/api/personal/presets'+(query.size?'?'+query:''));if(!current())return;
   if(!append){$('personal-presets').replaceChildren();presetIds.clear();}
   const selected=$('preset-robot').value;
   const robots=[...data.robots];if(selected&&!robots.some(r=>r.id===selected))robots.push({id:selected,label:selected+'（当前不可用）'});
   $('preset-robot').replaceChildren(...robots.map(r=>{const o=node('option',r.label);o.value=r.id;return o;}));
   if(selected)$('preset-robot').value=selected;
   const items=highlight&&!archived?[highlight,...data.items]:data.items;
   for(const p of items){
    if(presetIds.has(p.id))continue;presetIds.add(p.id);
    const card=node('article');card.className='production-panel';card.dataset.preset=p.id;
    card.append(node('h3',p.name),node('p',`${data.robots.find(r=>r.id===p.robot)?.label||p.robot} · v${p.version} · ${date(p.updated)}`),node('p',p.notes||'暂无说明'));
    const summary=node('details');summary.append(node('summary','查看参数 JSON'),node('pre',JSON.stringify(p.parameters,null,2)));card.append(summary);
    const actions=node('div');actions.className='production-actions';const edit=node('button','编辑方案');edit.onclick=()=>openEditor(p);
    const copy=node('button','另存为新方案');copy.onclick=()=>openEditor({...p,id:null,version:0,name:p.name.slice(0,70)+'（副本）'});
    const versions=node('button','版本记录');versions.onclick=()=>openHistory(p.id);
    const use=node('button','用于重定向 v'+p.version);use.disabled=p.execution_mode!=='supported';use.title=p.execution_error||'';use.onclick=()=>showRetarget('',{robot:p.robot,presetId:p.id,presetVersion:p.version});
    if(!p.archived)actions.append(edit);actions.append(copy,versions);if(!p.archived)actions.append(use);
    const archive=node('button',p.archived?'恢复方案':'归档方案');archive.onclick=()=>changeArchive(p,archive);actions.append(archive);card.append(actions);$('personal-presets').append(card);
    if(highlight?.id===p.id&&!$('preset-editor').open){card.tabIndex=-1;card.focus({preventScroll:true});card.scrollIntoView?.({block:'nearest'});}
   }
   presetAfter=data.next_after;$('personal-preset-more').hidden=presetAfter===null;
   const count=$('personal-presets').children.length,quota=Number.isInteger(data.active_count)?` · 活跃 ${data.active_count} / ${data.active_limit} · 已归档 ${data.archived_count}`:'';
   $('personal-preset-summary').textContent=(count?`已载入 ${count} 个${archived?'归档':'活跃'}方案`:(archived?'暂无归档方案。':'还没有活跃方案，点击“新建方案”开始。'))+quota;
   $('personal-new-preset').disabled=false;
  }catch(e){if(current())notify(e.message,true);}finally{if(current()){presetBusy=false;$('personal-preset-more').disabled=false;}}
 }
 async function changeArchive(p,button){
  if(button.disabled)return;
  const action=p.archived?'restore':'archive';
  if(action==='archive'&&!confirm('归档「'+p.name+'」并释放一个活跃名额？历史版本和已有作业记录会保留，可在“已归档”中恢复。'))return;
  const session=state.session,seq=epoch,view=$('personal-preset-view').value;button.disabled=true;
  try{const result=await api('/api/personal/presets',{method:'POST',body:JSON.stringify({id:p.id,version:p.version,archive_revision:p.archive_revision||0,action})});if(session!==state.session||!valid(seq))return;
   notify(action==='archive'?`已归档「${p.name}」，历史版本和已有作业仍保留。`:`已恢复「${p.name}」。`);
   if(action==='restore'&&$('personal-preset-view').value===view)$('personal-preset-view').value='active';
   await loadPresets(false,action==='restore'?result:null);
   if(historyId===p.id&&$('preset-history').open)await openHistory(p.id);
  }catch(error){if(session===state.session&&valid(seq))notify(error.message,true);}finally{if(session===state.session)button.disabled=false;}
 }
 function openEditor(p=null){
  if(saving)return;
  editing=p?.id?{id:p.id,version:p.version}:{id:Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join(''),version:0};
  editorRevision++;
  $('preset-form').reset();$('preset-name').value=p?.name||'';if(p?.robot)$('preset-robot').value=p.robot;
  $('preset-notes').value=p?.notes||'';$('preset-parameters').value=JSON.stringify(p?.parameters||{},null,2);$('preset-message').textContent='';dirty=false;
  $('preset-editor-title').textContent=p?.id?'编辑参数方案 · v'+p.version:'新建参数方案';$('preset-editor').showModal();
 }
 async function openHistory(id,append=false){
  if(!id||(append&&historyBusy))return;
  const seq=epoch,request=++historyEpoch;historyId=id;historyBusy=true;
  const current=()=>valid(seq)&&historyId===id&&request===historyEpoch;
  if(!append){historyAfter=null;$('preset-versions').replaceChildren();$('preset-history-more').hidden=true;if(!$('preset-history').open)$('preset-history').showModal();}
  $('preset-history-message').textContent='加载中…';$('preset-history-more').disabled=true;
  try{const data=await api('/api/personal/presets?id='+id+'&after='+(append?historyAfter||0:0));if(!current())return;
   $('preset-history-message').textContent=data.preset.name+(data.preset.archived?'（已归档；恢复后可从此处使用）':'');
   for(const v of data.versions){const details=node('details');details.append(node('summary',`v${v.version} · ${date(v.updated)} · ${v.owner}`),node('p',v.name+' · '+v.robot),node('p',v.notes),node('pre',JSON.stringify(v.parameters,null,2)));const use=node('button','使用 v'+v.version);use.disabled=!!data.preset.archived;use.onclick=()=>{if(use.disabled)return;closeHistory();return showRetarget('',{robot:v.robot,presetId:id,presetVersion:v.version});};details.append(use);$('preset-versions').append(details);}
   historyAfter=data.next_after;$('preset-history-more').hidden=historyAfter===null;
  }catch(e){if(current())$('preset-history-message').textContent=e.message;}finally{if(current()){historyBusy=false;$('preset-history-more').disabled=false;}}
 }
 function invalidateHistory(){historyEpoch++;historyBusy=false;historyId=null;historyAfter=null;$('preset-history-more').disabled=false;}
 function closeHistory(){invalidateHistory();$('preset-history').close();}
 $('show-personal').onclick=async()=>{
  if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);
  closeRetarget();state.playing=false;state.request++;
  for(const id of ['production','workspace','gallery','monitor','tasks','storage','retarget','gadget'])$(id).hidden=true;
  for(const b of $('page-nav').querySelectorAll('button'))b.setAttribute('aria-pressed',String(b.id==='show-personal'));
  $('personal').hidden=false;$('personal-title').textContent=state.user.username+' 的个人空间';$('personal-new-preset').disabled=true;notify('');epoch++;
  await Promise.all([loadMotions(),loadPresets()]);
 };
 $('personal-refresh').onclick=async()=>{epoch++;notify('');await Promise.all([loadMotions(),loadPresets()]);};
 $('personal-motion-more').onclick=()=>loadMotions(true);$('personal-preset-more').onclick=()=>loadPresets(true);
 $('personal-preset-view').onchange=()=>loadPresets();
 $('personal-new-preset').onclick=()=>openEditor();$('personal-generate').onclick=()=>$('show-home').click();
 $('preset-form').oninput=()=>{dirty=true;editorRevision++;};
 const draft=()=>JSON.stringify(['preset-name','preset-robot','preset-notes','preset-parameters'].map(id=>$(id).value));
 $('preset-form').onsubmit=async e=>{
  e.preventDefault();if(saving)return;const seq=epoch,session=state.session,revision=editorRevision,submittedDraft=draft();
  try{
   const parameters=JSON.parse($('preset-parameters').value);if(!parameters||Array.isArray(parameters)||typeof parameters!=='object')throw new Error('参数必须是 JSON 对象。');
   saving=true;$('preset-save').disabled=true;$('preset-message').textContent='保存中…';
   const result=await api('/api/personal/presets',{method:'POST',body:JSON.stringify({...editing,name:$('preset-name').value,robot:$('preset-robot').value,notes:$('preset-notes').value,parameters})});
   if(!valid(seq))return;
   editing={id:result.id,version:result.version};
   dirty=editorRevision!==revision||draft()!==submittedDraft;
   if(dirty){$('preset-editor-title').textContent='编辑参数方案 · 基于 v'+result.version;$('preset-message').textContent=`已保存提交时的 v${result.version}；保存期间的新修改仍在编辑器中，尚未保存。`;}
   else{$('preset-editor').close();$('preset-message').textContent='';}
   notify(`已保存「${result.name}」v${result.version}。`+(dirty?'当前新修改尚未保存。':''));$('personal-preset-view').value='active';await loadPresets(false,result);
  }catch(error){if(valid(seq))$('preset-message').textContent=error.message;}finally{if(session===state.session){saving=false;$('preset-save').disabled=false;}}
 };
 const canClose=()=>!saving&&(!dirty||confirm('尚未保存参数修改，确定放弃吗？'));
 $('preset-editor-close').onclick=()=>{if(canClose()){$('preset-editor').close();dirty=false;}};
 $('preset-editor').addEventListener('cancel',e=>{if(!canClose())e.preventDefault();else dirty=false;});
 $('preset-history-close').onclick=closeHistory;$('preset-history').addEventListener('cancel',invalidateHistory);$('preset-history-more').onclick=()=>openHistory(historyId,true);
 window.addEventListener('beforeunload',e=>{if(dirty||saving){e.preventDefault();e.returnValue='';}});
})();

/* Presentation-only workbench navigation. Existing handlers own API calls and permissions. */
(()=>{
 function selectTab(id){
  const selected=document.getElementById(id+'-tab');if(!selected)return;
  const bar=selected.parentElement;
  for(const button of bar.querySelectorAll('[role=tab]')){
   const active=button===selected;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
   document.getElementById(button.getAttribute('aria-controls')).hidden=!active;
  }
 }
 for(const bar of document.querySelectorAll('.section-tabs')){
  bar.addEventListener('click',e=>{const b=e.target.closest('[role=tab]');if(b)selectTab(b.getAttribute('aria-controls'));});
  bar.addEventListener('keydown',e=>{
   if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
   const items=[...bar.querySelectorAll('[role=tab]')].filter(b=>getComputedStyle(b).display!=='none');
   const i=items.indexOf(document.activeElement);if(i<0)return;e.preventDefault();
   const next=items[e.key==='Home'?0:e.key==='End'?items.length-1:(i+(e.key==='ArrowRight'?1:-1)+items.length)%items.length];
   selectTab(next.getAttribute('aria-controls'));next.focus();
  });
 }
 const role=()=>selectTab(document.body.dataset.role==='admin'?'production-auto-pane':'production-custom-pane');
 new MutationObserver(role).observe(document.body,{attributes:true,attributeFilter:['data-role']});role();
 $('personal-generate').onclick=()=>{$('show-home').click();selectTab('production-custom-pane');$('custom-description').focus();};
 const pages=['production','workspace','gallery','personal','retarget','monitor','tasks','storage','gadget'];
 const buttons=['show-home','show-review','show-gallery','show-personal','show-retarget','show-monitor','show-tasks','show-storage','show-gadget'];
 // Derive navigation state from the visible page, including programmatic cross-page links.
 const sync=()=>{const current=pages.findIndex(id=>!$(id).hidden);buttons.forEach((id,i)=>$(id).setAttribute('aria-pressed',String(i===current)));};
 const observer=new MutationObserver(sync);pages.forEach(id=>observer.observe($(id),{attributes:true,attributeFilter:['hidden']}));sync();
})();
