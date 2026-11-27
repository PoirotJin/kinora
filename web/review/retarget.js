'use strict';
const retargetState={clip:null,frame:0,playing:false,yaw:.6,pitch:.25,zoom:1,center:[0,.8,0],radius:2,request:0,polling:false,submission:null,history:false,after:null,listing:0,focusJob:null};
window.resetRetarget=()=>{retargetState.listing++;retargetState.focusJob=null;retargetState.polling=false;retargetState.history=false;retargetState.after=null;$('retarget-submit').disabled=false;$('retarget-history').textContent='全部历史';$('retarget-history').disabled=false;$('retarget-jobs-more').disabled=false;$('retarget-jobs-more').hidden=true;retargetState.playing=false;retargetState.clip=null;robotViewer.clear();retargetState.submission=null;retargetState.parentJob=null;retargetState.request++;$('retarget-jobs').replaceChildren();$('retarget-form').reset();$('retarget-text').textContent='作业完成后点击“播放结果”。';$('retarget-notice').textContent='';};
function showRetarget(mid=''){
 if(state.saving||!leave())return false;
 retargetState.parentJob=null;
 window.closePersonal?.();
 if(state.dirty)fillReview(state.current?.review);
 state.playing=false;state.request++;
 for(const id of ['production','workspace','gallery','monitor','tasks','storage'])$(id).hidden=true;
 for(const button of $('page-nav').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.id==='show-retarget'));
 $('retarget').hidden=false;
 if(mid)$('retarget-motion').value=mid;
 if(retargetState.requestedRobot)$('retarget-robot').value=retargetState.requestedRobot;
 refreshRetarget();return true;
}
$('show-retarget').onclick=()=>showRetarget();
$('retarget-current').onclick=()=>{if(state.current)$('retarget-motion').value=state.current.motion_id;else $('retarget-notice').textContent='请先在 Gallery 打开一个动作，或直接输入动作 ID。';};
$('retarget-gallery').onclick=()=>{$('show-gallery').click();};
const retargetNewLabel=document.createElement('label');retargetNewLabel.className='muted';
const retargetNew=document.createElement('input');retargetNew.type='checkbox';retargetNew.id='retarget-force-new';
retargetNewLabel.append(retargetNew,document.createTextNode('重新计算新版本（保留旧结果）；不勾选时优先加载已有结果，沿用其原参数。'));
$('retarget-form').insertBefore(retargetNewLabel,$('retarget-submit').parentElement);
$('retarget-form').onsubmit=async event=>{
 event.preventDefault();if(retargetState.presetLoading||retargetState.presetError){$('retarget-notice').textContent=retargetState.presetError||'参数方案正在加载，请稍候。';return;}const session=state.session;$('retarget-submit').disabled=true;
 const mid=$('retarget-motion').value.trim(),robot=$('retarget-robot').value;
 let execution={};
 try{const preset=$('retarget-preset').value.trim(),raw=$('retarget-parameters').value.trim();if(preset)execution={preset_id:preset,preset_version:Number($('retarget-preset-version').value)};if(raw){if(preset)throw new Error('方案与直接参数不能同时提供');execution.parameters=JSON.parse(raw);}}catch(e){$('retarget-notice').textContent=e.message;$('retarget-submit').disabled=false;return;}
 if(retargetState.parentJob)execution.parent_job_id=retargetState.parentJob;
 if(retargetNew.checked)execution.force_new=true;
 if(retargetState.submission?.motion_id!==mid||retargetState.submission?.robot!==robot||JSON.stringify(retargetState.submission?.executionForm)!==JSON.stringify(execution))retargetState.submission={id:Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join(''),motion_id:mid,robot,...execution,executionForm:execution};
 try{const submitted=await api('/api/retarget',{method:'POST',body:JSON.stringify(Object.fromEntries(Object.entries(retargetState.submission).filter(([k])=>k!=='executionForm')))});if(session!==state.session)return;retargetState.submission=null;retargetState.parentJob=null;retargetState.history=false;retargetState.after=null;retargetState.focusJob=submitted.id;$('retarget-history').textContent='全部历史';$('retarget-jobs-more').hidden=true;$('retarget-notice').textContent=submitted.reused_from?'已找到已有结果，直接加载，无需重新计算。':'已提交，关闭网页后仍会继续。';await refreshRetarget(false,true);if(session===state.session&&submitted.status==='done'&&submitted.reused_from){const loaded=await previewRetarget(submitted.id);if(loaded&&session===state.session)$('retarget-notice').textContent='已加载此前的重定向结果，未重复计算。';}}
 catch(error){if(session===state.session)$('retarget-notice').textContent=error.message;}
 finally{if(session===state.session)$('retarget-submit').disabled=false;}
};
async function refreshRetarget(append=false,manual=false){
 if((retargetState.history&&!manual)||(retargetState.polling&&!manual)||!state.user||$('retarget').hidden)return;
 retargetState.polling=true;const user=state.session,request=++retargetState.listing;$('retarget-history').disabled=true;$('retarget-jobs-more').disabled=true;
 try{
  const query=retargetState.history?'?history=1'+(append?'&after='+encodeURIComponent(retargetState.after):''):'';
  const result=await api('/api/retarget'+query);if(state.session!==user||request!==retargetState.listing)return;
  const robots=result.robots||[],signature=JSON.stringify(robots);
  if(signature!==retargetState.catalogSignature){
   const selected=retargetState.requestedRobot||$('retarget-robot').value;retargetState.requestedRobot=null;retargetState.robots=robots;retargetState.catalogSignature=signature;
   $('retarget-robot').replaceChildren(...robots.map(robot=>{const option=document.createElement('option');option.value=robot.id;option.textContent=robot.label;return option;}));
   if(robots.some(robot=>robot.id===selected))$('retarget-robot').value=selected;
   $('retarget-robot').onchange=()=>{$('retarget-robot-note').textContent=retargetState.robots.find(robot=>robot.id===$('retarget-robot').value)?.note||'';};
   $('retarget-robot').onchange();
  }
  if(retargetState.requestedRobot&&robots.some(r=>r.id===retargetState.requestedRobot)){$('retarget-robot').value=retargetState.requestedRobot;retargetState.requestedRobot=null;$('retarget-robot').onchange?.();}
  $('retarget-summary').textContent=`${result.running} 个运行中 · ${result.queued} 个排队中 · ${retargetState.history?'全部历史 · 按创建时间倒序':(state.user.role==='admin'?'全部用户近期作业':'我的近期作业')}`;
  if(result.dispatch_notice)$('retarget-summary').textContent+=' · '+result.dispatch_notice;
  const list=$('retarget-jobs');
  const expanded=new Set([...list.querySelectorAll('article[data-job]')].filter(card=>card.querySelector('details')?.open).map(card=>card.dataset.job));
  const focused=document.activeElement?.closest('[data-job]');
  const focusKey=focused?{job:focused.dataset.job,index:[...focused.querySelectorAll('button,a,summary')].indexOf(document.activeElement)}:null;
  if(!append)list.replaceChildren();
  retargetState.after=result.next_after??null;$('retarget-jobs-more').hidden=!retargetState.history||retargetState.after===null;
  window.renderPresetRecent?.(result.items);
  for(const job of result.items){
   const card=document.createElement('article');card.className='retarget-job';card.dataset.job=job.id;
   const title=document.createElement('strong');title.textContent=job.motion_id+' · '+({queued:'排队中',running:'运行中',stopping:'正在取消',cancelled:'已取消',done:'已完成',failed:'失败'}[job.status]||job.status);
   const info=document.createElement('p');info.className='muted';info.textContent=`${job.owner} · ${new Date(job.created*1000).toLocaleString()} · ${job.robot}`;
   const progress=document.createElement('progress');progress.max=job.total||1;progress.value=job.done||0;progress.setAttribute('aria-label','已完成帧数');
   const status=document.createElement('p');status.textContent=(job.status==='queued'?`队列位置 ${job.queue_position}（新任务加入后可能调整） · `:'')+job.message;
   const actions=document.createElement('div');actions.className='production-actions';
   if(job.status==='done'){
    const preview=document.createElement('button');preview.textContent='播放结果';preview.onclick=()=>previewRetarget(job.id);
    const download=document.createElement('a');download.textContent='下载 NPZ';download.href='/api/retarget/download?id='+job.id;
    actions.append(preview,download);
   }else if(['queued','running','stopping'].includes(job.status)){
    const cancel=document.createElement('button');cancel.textContent='取消';cancel.disabled=job.status==='stopping';cancel.onclick=async()=>{try{await api('/api/retarget/cancel',{method:'POST',body:JSON.stringify({id:job.id})});if(state.session===user)await refreshRetarget(false,true);}catch(e){if(state.session===user)$('retarget-notice').textContent=e.message;}};actions.append(cancel);
   }
   const source=document.createElement('button');source.textContent='查看原始动作';source.onclick=async()=>{closeRetarget();switchPage(false);await selectMotion(job.motion_id);};actions.append(source);
   window.retargetResearchActions?.(actions,job);card.append(title,info);
   if(job.result_version){const v=job.result_version;const version=document.createElement('p');version.className='muted';version.textContent=`算法：${v.algorithm} · 版本：${v.algorithm_version.length>20?v.algorithm_version.slice(0,12):v.algorithm_version} · ${v.parameter_version}`;version.title=JSON.stringify(v,null,2);card.append(version);const details=document.createElement('details');details.open=expanded.has(job.id);const summary=document.createElement('summary');summary.textContent='实际参数与版本信息';const pre=document.createElement('pre');pre.textContent=JSON.stringify(v,null,2);details.append(summary,pre);card.append(details);}
   card.append(progress,status,actions);$('retarget-jobs').append(card);
  }
  if(!append&&!result.items.length)$('retarget-jobs').textContent='暂无重定向作业。';
  if(retargetState.focusJob){const card=[...list.children].find(n=>n.dataset.job===retargetState.focusJob);if(card){card.tabIndex=-1;card.focus({preventScroll:true});card.scrollIntoView?.({block:'nearest'});retargetState.focusJob=null;}}
  else if(!append&&focusKey){const card=[...list.children].find(n=>n.dataset.job===focusKey.job);card?.querySelectorAll('button,a,summary')[focusKey.index]?.focus({preventScroll:true});}
 }catch(error){if(state.session===user&&request===retargetState.listing)$('retarget-notice').textContent=error.message;}
 finally{if(state.session===user&&request===retargetState.listing){retargetState.polling=false;$('retarget-history').disabled=false;$('retarget-jobs-more').disabled=false;}}
}
async function previewRetarget(id){
 const seq=++retargetState.request;const user=state.session;
 try{
  $('retarget-notice').textContent='正在加载实体模型…';
  retargetState.clip=null;retargetState.playing=false;
  const clip=await api('/api/retarget/mesh-preview?id='+id);if(seq!==retargetState.request||user!==state.session)return;
  const loaded=await robotViewer.load(clip);if(!loaded||seq!==retargetState.request||user!==state.session)return;
  retargetState.clip=clip;retargetState.frame=0;retargetState.playing=true;retargetState.zoom=1;
  $('retarget-notice').textContent='';
  $('retarget-preview-title').textContent=clip.metadata.motion_id+' · '+(clip.metadata.robot_label||clip.metadata.robot||'Unitree G1 · 23 DoF');$('retarget-text').textContent=clip.metadata.text;
  $('retarget-timeline').max=clip.frames-1;$('retarget-play').textContent='暂停';return true;
 }catch(error){if(seq===retargetState.request&&user===state.session)$('retarget-notice').textContent=error.message;}
}
const retargetCanvas=$('retarget-canvas');let retargetLast=0,retargetDrag=null;
function drawRetarget(now){
 const dt=Math.min(.1,(now-retargetLast)/1000||0);retargetLast=now;
 if(!$('retarget').hidden&&!document.hidden&&retargetState.clip){
  const s=retargetState;if(s.playing)s.frame=(s.frame+dt*s.clip.fps)%s.clip.frames;
  const index=Math.floor(s.frame);robotViewer.render(s.clip,index,s);
  $('retarget-timeline').value=index;$('retarget-time').textContent=`${(index/s.clip.fps).toFixed(2)} / ${s.clip.duration_s.toFixed(2)} 秒`;
 }
 requestAnimationFrame(drawRetarget);
}
requestAnimationFrame(drawRetarget);
$('retarget-play').onclick=()=>{retargetState.playing=!retargetState.playing;$('retarget-play').textContent=retargetState.playing?'暂停':'播放';};
$('retarget-timeline').oninput=()=>{retargetState.frame=Number($('retarget-timeline').value);retargetState.playing=false;$('retarget-play').textContent='播放';};
retargetCanvas.onpointerdown=e=>{retargetDrag=[e.clientX,e.clientY];retargetCanvas.setPointerCapture(e.pointerId);};
retargetCanvas.onpointermove=e=>{if(!retargetDrag)return;retargetState.yaw+=(e.clientX-retargetDrag[0])*.008;retargetState.pitch=Math.max(-.3,Math.min(1.4,retargetState.pitch+(e.clientY-retargetDrag[1])*.008));retargetDrag=[e.clientX,e.clientY];};
retargetCanvas.onpointerup=retargetCanvas.onpointercancel=()=>retargetDrag=null;
retargetCanvas.addEventListener('wheel',e=>{e.preventDefault();retargetState.zoom=Math.max(.3,Math.min(4,retargetState.zoom*Math.exp(-e.deltaY*.001)));},{passive:false});
$('retarget-history').onclick=()=>{retargetState.history=!retargetState.history;$('retarget-history').textContent=retargetState.history?'返回实时队列':'全部历史';refreshRetarget(false,true);};
$('retarget-jobs-more').onclick=()=>refreshRetarget(true,true);
setInterval(refreshRetarget,3000);

// The version sent to the solver is explicit; refreshing never changes an edit.
(()=>{
 const old=$('retarget-preset'),select=document.createElement('select');select.id=old.id;
 old.parentNode.firstChild.textContent='个人参数方案';old.replaceWith(select);select.add(new Option('默认参数 / 直接 JSON',''));
 const oldVersion=$('retarget-preset-version'),version=document.createElement('select');version.id=oldVersion.id;oldVersion.replaceWith(version);
 const emptyVersion=()=>{version.replaceChildren(new Option('不适用（使用默认参数）',''));version.disabled=true;};emptyVersion();
 const refresh=document.createElement('button');refresh.type='button';refresh.textContent='刷新我的方案';refresh.setAttribute('aria-label','刷新我的参数方案');select.parentNode.append(refresh);
 const info=document.createElement('div');info.setAttribute('role','status');select.parentNode.parentNode.insertBefore(info,select.parentNode.nextSibling);
 let presets=[],versions=[],schema={},seq=0;
 function explain(){
  info.replaceChildren();const p=versions.find(p=>String(p.version)===version.value);if(!p)return;
  const compatible=p.robot===$('retarget-robot').value,supported=p.execution_mode==='supported';
  const note=document.createElement('p');note.textContent=p.name+' · v'+p.version+' · '+p.robot+' · '+(!compatible?'与目标机器人不兼容':supported?'可执行':'仅设计稿，不可执行')+(p.notes?' · '+p.notes:'');info.append(note);
  const table=document.createElement('table');const head=document.createElement('tr');for(const t of ['参数','默认值','方案值']){const th=document.createElement('th');th.textContent=t;head.append(th);}table.append(head);
  for(const [key,spec] of Object.entries(schema)){const row=document.createElement('tr');for(const text of [spec.label,spec.default,p.parameters[key]??spec.default]){const cell=document.createElement('td');cell.textContent=String(text);row.append(cell);}if((p.parameters[key]??spec.default)!==spec.default)row.lastChild.style.fontWeight='700';table.append(row);}info.append(table);
  if(!supported){const pnote=document.createElement('p');pnote.textContent=p.execution_error||'含当前求解器不支持的参数';info.append(pnote);}
 }
 async function loadVersions(wantedVersion=null){
  const session=state.session,request=++seq,key=select.value;versions=[];emptyVersion();info.replaceChildren();retargetState.presetError='';
  if(!key){retargetState.presetLoading=false;return true;}
  retargetState.presetLoading=true;
  try{let after=null,items=[];do{const data=await api('/api/personal/presets?id='+encodeURIComponent(key)+(after?'&after='+after:''));if(session!==state.session||request!==seq)return false;items.push(...data.versions);after=data.next_after;}while(after!==null);
   versions=items;version.disabled=false;version.replaceChildren(...versions.slice().reverse().map(p=>new Option('v'+p.version+' · '+p.name,String(p.version))));
   if(wantedVersion!==null&&wantedVersion!==''){if(!versions.some(p=>String(p.version)===String(wantedVersion)))throw new Error('所选方案版本不可用，请重新选择版本。');version.value=String(wantedVersion);}
   explain();return true;
  }catch(e){if(session===state.session&&request===seq){retargetState.presetError=e.message;info.textContent=e.message;}return false;}
  finally{if(session===state.session&&request===seq)retargetState.presetLoading=false;}
 }
 async function load(selection=null){
  const session=state.session,request=++seq;let requiredPreset=selection?.presetId||select.value;refresh.disabled=true;
  if(selection?.presetId)retargetState.presetLoading=true;
  try{let after=null,items=[];do{const data=await api('/api/personal/presets'+(after?'?after='+after:''));if(session!==state.session||request!==seq)return false;items.push(...data.items);schema=data.parameter_schema.parameters;after=data.next_after;}while(after!==null);
   // Read live values after the await: an in-flight refresh must not restore an old edit.
   const chosen=selection?.presetId??select.value,chosenVersion=selection?.presetVersion??version.value;requiredPreset=chosen;
   presets=items;select.replaceChildren(new Option('默认参数 / 直接 JSON',''),...presets.map(p=>new Option(p.name+' · '+p.robot+' · 最新 v'+p.version,p.id)));
   if(chosen&&!presets.some(p=>p.id===chosen))throw new Error('所选参数方案不可用，请重新选择。');
   select.value=chosen;return await loadVersions(chosenVersion);
  }catch(e){if(session===state.session&&request===seq){retargetState.presetError=requiredPreset?e.message:'';retargetState.presetLoading=false;info.textContent=e.message;}return false;}
  finally{if(session===state.session)refresh.disabled=false;}
 }
 select.onchange=()=>loadVersions();version.onchange=()=>{seq++;retargetState.presetLoading=false;retargetState.presetError='';explain();};refresh.onclick=()=>load();$('retarget-robot').addEventListener('change',()=>{retargetState.requestedRobot=null;explain();});
 const originalShow=showRetarget;showRetarget=async function(mid='',selection=null){
  if(!originalShow(mid))return false;
  if(retargetState.forcedNewEntry&&!selection?.forceNew){retargetNew.checked=false;retargetState.forcedNewEntry=false;}
  if(selection){
   retargetState.parentJob=selection.parentJob||null;
   if(selection.robot){retargetState.requestedRobot=selection.robot;const robots=$('retarget-robot');if(![...robots.options].some(o=>o.value===selection.robot))robots.add(new Option(selection.robot,selection.robot));robots.value=selection.robot;robots.onchange?.();}
   if(selection.forceNew!==undefined){retargetNew.checked=selection.forceNew;retargetState.forcedNewEntry=selection.forceNew;}
   if(selection.presetId)$('retarget-parameters').value='';
   if(Object.prototype.hasOwnProperty.call(selection,'parameters')){select.value='';$('retarget-parameters').value=JSON.stringify(selection.parameters,null,2);}
  }
  return load(selection);
 };
 const originalReset=window.resetRetarget;window.resetRetarget=()=>{seq++;presets=[];versions=[];retargetState.forcedNewEntry=false;retargetState.presetLoading=false;retargetState.presetError='';info.replaceChildren();select.replaceChildren(new Option('默认参数 / 直接 JSON',''));emptyVersion();refresh.disabled=false;originalReset();};
 // Recent usage derives from server-owned execution snapshots, not browser storage.
 const recent=document.createElement('p');recent.className='muted';info.parentNode.insertBefore(recent,info.nextSibling);
 window.renderPresetRecent=jobs=>{const own=jobs.filter(j=>j.owner===state.user?.username).map(j=>{try{return {...j,execution:typeof j.execution==='string'?JSON.parse(j.execution):j.execution};}catch{return j;}}).filter(j=>j.execution?.preset).slice(0,5);recent.textContent=own.length?'当前已加载作业中的方案使用：'+own.map(j=>j.execution.preset.name+' v'+j.execution.preset.version+'（'+new Date(j.created*1000).toLocaleString()+'）').join('；'):'当前已加载作业中暂无方案使用记录。';};
 const previousReset=window.resetRetarget;window.resetRetarget=()=>{previousReset();recent.textContent='';};
})();
