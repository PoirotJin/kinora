'use strict';
(() => {
 let epoch=0,detailEpoch=0,sourceEpoch=0,candidateEpoch=0,current=null,busy=false,source=null,candidate=null,frame=0,playing=false,last=0,pendingCreate=null,pendingImport=null;
 let editingEnabled=false,pendingRepair=null,repairTimer=null,repairPolling=false,rewriteLoaded=false;
 const labels={pending:'待执行',ready:'待执行',running:'执行中',waiting:'等待结果',waiting_user:'等待你的判断',waiting_tool:'等待工具结果',needs_input:'需要补充信息',blocked:'需要处理',completed:'已完成',finished:'执行结束',accepted:'已接受',cancelled:'已停止',failed:'执行失败',unknown:'未检查',pass:'通过',passed:'通过',fail:'未通过',failed_check:'未通过',unavailable:'待接入',awaiting_review:'等待你的验收',needs_attention:'需要处理',budget_exhausted:'预算已用尽',active:'待执行'};
 const human=value=>labels[value]||value||'未知';
 const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
 const action=(label,fn)=>{const button=el('button',label);button.type='button';button.onclick=fn;return button;};
 const taskOf=value=>value?.task||value;
 const items=value=>Array.isArray(value)?value:Array.isArray(value?.items)?value.items:[];
 const idOf=value=>value?.id||value?.task_id;
 const currentSource=task=>task?.current_motion_id||task?.source_motion_id;
 const valid=(session,seq)=>state.session===session&&epoch===seq&&state.user&&!$('harness').hidden;
 const notify=(text,error=false)=>{$('harness-notice').textContent=text;$('harness-notice').classList.toggle('error',error);};
 const uid=()=>crypto.randomUUID?crypto.randomUUID().replaceAll('-',''):Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
 function selection(){
  const start=Number($('harness-start').value),end=Number($('harness-end').value);
  if(!Number.isInteger(start)||!Number.isInteger(end)||start<0||end<=start||(source&&end>source.frames))throw new Error('请选择有效的起止帧；结束帧不包含在选区内。');
  return {start_frame:start,end_frame:end};
 }
 // Cut coordinates use [0, frames]; the playhead uses [0, frames - 1].
 let timelineZoom=1,timelineDrag=null;
 const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
 const timecode=(f,fps=30)=>{const t=Math.max(0,f/fps);return String(Math.floor(t/60)).padStart(2,'0')+':'+(t%60).toFixed(2).padStart(5,'0');};
 const rangeLocked=()=>busy||Boolean(pendingRepair)||hasPendingRepair(current);
 function syncTimeline(){
  const n=source?.frames||0,fps=source?.fps||30,locked=rangeLocked();let range=null;
  try{if(source)range=selection();}catch(_error){}
  if(!source){$('harness-selection-note').textContent='先载入一个动作';$('harness-selection-note').classList.toggle('error',false);}$('harness-track-empty').hidden=Boolean(source);$('harness-selection').hidden=!range;$('harness-playhead').hidden=!n;
  $('harness-track-stage').setAttribute('aria-disabled',String(!source));$('harness-track-stage').setAttribute('data-range-locked',String(locked));
  for(const id of ['harness-trim-start','harness-trim-end'])$(id).setAttribute('aria-disabled',String(!source||locked));
  for(const id of ['harness-start','harness-end','harness-mark-start','harness-mark-end','harness-move-selection'])$(id).disabled=!source||locked;
  for(const id of ['harness-frame-prev','harness-frame-next','harness-timeline'])$(id).disabled=!n;
  $('harness-play').disabled=!source&&!candidate;
  $('harness-zoom-in').disabled=!n||timelineZoom>=16;$('harness-zoom-out').disabled=!n||timelineZoom<=1;$('harness-zoom-fit').disabled=!n;
  const editable=range&&n>=30&&range.end_frame-range.start_frame>=12&&range.end_frame-range.start_frame<=300;
  $('harness-run-repair').disabled=busy||!editingEnabled||(!hasPendingRepair(current)&&!editable);
  $('harness-run-repair').textContent=hasPendingRepair(current)?'查看修复进度':'修复选中片段';
  if(range){
   const {start_frame:a,end_frame:b}=range,sel=$('harness-selection');sel.style.left=(a/n*100)+'%';sel.style.width=((b-a)/n*100)+'%';sel.classList.toggle('invalid',!editable);
   $('harness-selection-duration').textContent=((b-a)/fps).toFixed(2)+' 秒';
   for(const [id,value,min,max] of [['harness-trim-start',a,0,b-1],['harness-trim-end',b,a+1,n]]){
    const node=$(id);node.setAttribute('aria-valuemin',min);node.setAttribute('aria-valuemax',max);node.setAttribute('aria-valuenow',value);node.setAttribute('aria-valuetext',timecode(value,fps)+'，第 '+value+' 帧');
   }
  }
  syncPlayhead();
 }
 function syncPlayhead(){
  const n=source?.frames||candidate?.frames||0,fps=source?.fps||candidate?.fps||30;
  $('harness-timeline').value=Math.floor(frame);$('harness-clock').textContent=timecode(frame,fps)+(n?' / '+timecode(n,fps):'');
  $('harness-playhead').style.left=(n?clamp(frame,0,n-1)/n*100:0)+'%';
  const node=$('harness-playhead');node.setAttribute('aria-valuemin',0);node.setAttribute('aria-valuemax',Math.max(0,n-1));node.setAttribute('aria-valuenow',Math.floor(frame));node.setAttribute('aria-valuetext',timecode(frame,fps)+'，第 '+Math.floor(frame)+' 帧');
 }
 function seek(value){const n=source?.frames||candidate?.frames;if(!n)return;frame=clamp(Math.round(value),0,n-1);playing=false;$('harness-play').textContent='播放';paint();}
 function changeRange(start,end,preview){if(!source)return;$('harness-start').value=clamp(Math.round(start),0,source.frames-1);$('harness-end').value=clamp(Math.round(end),Number($('harness-start').value)+1,source.frames);updateRange();if(preview!==undefined)seek(preview);}
 function timelineAt(x){const rect=$('harness-track').getBoundingClientRect();return rect.width&&source?clamp(Math.round((x-rect.left)/rect.width*source.frames),0,source.frames):0;}
 function dragValid(d){return d&&source&&d.sourceEpoch===sourceEpoch&&d.epoch===epoch&&d.session===state.session;}
 function endTimelineDrag(commit=true){
  const d=timelineDrag;if(!d)return;timelineDrag=null;
  if(!commit&&dragValid(d)){changeRange(d.range.start_frame,d.range.end_frame);seek(d.frame);}
  if(d.node.hasPointerCapture?.(d.id))d.node.releasePointerCapture(d.id);
 }
 function beginTimelineDrag(event,kind){
  if(!source||event.button!==0||(event.isPrimary===false)||(kind!=='seek'&&rangeLocked()))return;
  let range;try{range=selection();}catch(_error){range={start_frame:0,end_frame:source.frames};}
  endTimelineDrag();event.preventDefault();event.stopPropagation();playing=false;$('harness-play').textContent='播放';
  const node=event.currentTarget;timelineDrag={kind,node,id:event.pointerId,x:event.clientX,lastX:event.clientX,anchor:timelineAt(event.clientX),range,frame,sourceEpoch,epoch,session:state.session,moved:false};
  node.setPointerCapture?.(event.pointerId);(kind==='select'||node.id==='harness-ruler'?$('harness-track-stage'):node).focus({preventScroll:true});
  if(kind==='seek')seek(timelineAt(event.clientX));
 }
 function moveTimelineDrag(event){
  const d=timelineDrag;if(!d||d.id!==event.pointerId)return;if(!dragValid(d)){endTimelineDrag();return;}
  event.preventDefault();d.lastX=event.clientX;d.moved=d.moved||Math.abs(d.lastX-d.x)>=4;
  if(d.kind!=='select'||d.moved)applyTimelineDrag(d.lastX);
 }
 function applyTimelineDrag(x){
  const d=timelineDrag;if(!dragValid(d))return;const at=timelineAt(x),a=d.range.start_frame,b=d.range.end_frame;
  if(d.kind==='seek')seek(at);
  else if(d.kind==='select'){const lo=Math.min(d.anchor,at),hi=Math.max(d.anchor,at);changeRange(Math.min(lo,source.frames-1),Math.max(hi,lo+1),at);}
  else if(d.kind==='start'){const next=clamp(at,0,b-1);changeRange(next,b,next);}
  else if(d.kind==='end'){const next=clamp(at,a+1,source.frames);changeRange(a,next,next-1);}
  else {const next=clamp(a+at-d.anchor,0,source.frames-(b-a));changeRange(next,next+b-a,next);}
 }
 function finishTimelineDrag(event){const d=timelineDrag;if(!d||d.id!==event.pointerId)return;if(dragValid(d)&&d.kind==='select'&&!d.moved)seek(timelineAt(event.clientX));endTimelineDrag();}
 function autoScrollTimeline(delta){
  const d=timelineDrag;if(!dragValid(d)||!d.moved)return;const box=$('harness-track-scroll'),r=box.getBoundingClientRect();
  const direction=d.lastX<r.left+24?-1:d.lastX>r.right-24?1:0;if(!direction)return;
  const old=box.scrollLeft;box.scrollLeft=clamp(old+direction*420*delta,0,box.scrollWidth-box.clientWidth);
  if(box.scrollLeft!==old)applyTimelineDrag(d.lastX);
 }
 function timelineKey(event,kind){
  if(!source)return;
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();endTimelineDrag(false);return;}
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();event.stopPropagation();if(kind!=='seek'&&rangeLocked())return;
  const step=(event.shiftKey?10:1)*(event.key==='ArrowLeft'?-1:1),n=source.frames;let range;
  try{range=selection();}catch(_error){return;}const a=range.start_frame,b=range.end_frame;
  const position=(value,min,max)=>event.key==='Home'?min:event.key==='End'?max:clamp(value+step,min,max);
  if(kind==='seek')seek(position(frame,0,n-1));
  else if(kind==='start'){const v=position(a,0,b-1);changeRange(v,b,v);}
  else if(kind==='end'){const v=position(b,a+1,n);changeRange(a,v,v-1);}
  else {const v=position(a,0,n-(b-a));changeRange(v,v+b-a,v);}
 }
 function drawTimeline(){
  const stage=$('harness-track-stage');stage.style.width=(timelineZoom*100)+'%';$('harness-zoom-label').textContent=timelineZoom+'×';
  const ruler=$('harness-ruler');ruler.replaceChildren();syncTimeline();
  const canvas=$('harness-filmstrip'),width=$('harness-track').getBoundingClientRect().width;if(!width)return;
  const dpr=Math.min(devicePixelRatio||1,2);canvas.width=Math.min(8192,Math.round(width*dpr));canvas.height=Math.round(90*dpr);
  const ctx=canvas.getContext('2d');ctx.setTransform(canvas.width/width,0,0,dpr,0,0);ctx.clearRect(0,0,width,90);if(!source)return;
  const duration=source.frames/source.fps,wanted=duration/Math.max(2,width/90),steps=[1/30,2/30,5/30,.25,.5,1,2,5,10,15,30,60,120,300,600],step=steps.find(s=>s>=wanted)||600;
  for(let i=0;i*step<=duration&&i<200;i++){const mark=el('span',timecode(i*step*source.fps,source.fps));mark.style.left=(i*step/duration*100)+'%';ruler.append(mark);}
  const count=Math.max(2,Math.min(80,Math.floor(width/80))),tile=width/count;
  for(let i=0;i<count;i++){
   const points=source.joints[Math.min(source.frames-1,Math.floor((i+.5)/count*source.frames))].slice(0,22);
   const projected=points.map(p=>[p[0]*.85+p[2]*.52,p[1]]),xs=projected.map(p=>p[0]),ys=projected.map(p=>p[1]);
   const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),scale=Math.min((tile-16)/Math.max(.3,maxX-minX),62/Math.max(.5,maxY-minY));
   const xy=projected.map(p=>[i*tile+tile/2+(p[0]-(minX+maxX)/2)*scale,80-(p[1]-minY)*scale]);
   ctx.strokeStyle='#93a4b4';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(i*tile,0);ctx.lineTo(i*tile,90);ctx.stroke();ctx.strokeStyle='#547088';ctx.lineWidth=1.5;
   for(let j=1;j<points.length;j++){const parent=source.parents[j];if(parent<0||parent>=points.length)continue;ctx.beginPath();ctx.moveTo(...xy[parent]);ctx.lineTo(...xy[j]);ctx.stroke();}
  }
 }
 function zoomTimeline(value){
  if(!source)return;endTimelineDrag();const box=$('harness-track-scroll'),old=timelineZoom;
  const focus=(box.scrollLeft+box.clientWidth/2)/(box.clientWidth*old||1);timelineZoom=clamp(value,1,16);drawTimeline();
  box.scrollLeft=timelineZoom===1?0:Math.max(0,focus*box.clientWidth*timelineZoom-box.clientWidth/2);
 }
 for(const [id,kind] of [['harness-track','select'],['harness-ruler','seek'],['harness-playhead','seek'],['harness-trim-start','start'],['harness-trim-end','end'],['harness-move-selection','move']]){
  const node=$(id);node.onpointerdown=e=>beginTimelineDrag(e,kind);node.onpointermove=moveTimelineDrag;node.onpointerup=finishTimelineDrag;
  node.onpointercancel=()=>endTimelineDrag(false);node.onlostpointercapture=()=>endTimelineDrag(false);
  if(!['harness-track','harness-ruler'].includes(id))node.onkeydown=e=>timelineKey(e,kind);
 }
 $('harness-track-stage').onkeydown=event=>{
  if(event.key==='Escape'){event.preventDefault();endTimelineDrag(false);return;}
  if(event.key===' '){event.preventDefault();$('harness-play').onclick();}
  else if(event.key.toLowerCase()==='i'&&!rangeLocked()){event.preventDefault();$('harness-mark-start').onclick();}
  else if(event.key.toLowerCase()==='o'&&!rangeLocked()){event.preventDefault();$('harness-mark-end').onclick();}
  else if(event.target===$('harness-track-stage'))timelineKey(event,'seek');
 };
 $('harness-frame-prev').onclick=()=>seek(frame-1);$('harness-frame-next').onclick=()=>seek(frame+1);
 $('harness-zoom-in').onclick=()=>zoomTimeline(timelineZoom*2);$('harness-zoom-out').onclick=()=>zoomTimeline(timelineZoom/2);$('harness-zoom-fit').onclick=()=>zoomTimeline(1);
 function setBusy(value){busy=value;for(const id of ['harness-submit','harness-advance','harness-cancel','harness-feedback-submit','harness-import-submit'])$(id).disabled=value;syncTimeline();}
 function stopRepairPolling(){if(repairTimer!==null)clearTimeout(repairTimer);repairTimer=null;}
 function hasPendingRepair(task){return items(task?.operations).some(op=>op.tool==='edit_motion'&&['submitting','waiting','unknown'].includes(op.status));}
 function scheduleRepair(){stopRepairPolling();if(current&&hasPendingRepair(current)&&!['cancelled','failed'].includes(current.status))repairTimer=setTimeout(pollRepair,10000);}
 async function pollRepair(){
  repairTimer=null;if(repairPolling||!current||!hasPendingRepair(current)||$('harness').hidden)return;
  const session=state.session,seq=epoch,id=idOf(current);repairPolling=true;
  try{const result=await api('/api/harness/tasks/'+encodeURIComponent(id)+'/repair-status',{method:'POST',body:'{}'});if(!valid(session,seq)||idOf(current)!==id)return;render(result);const latest=items(current.candidates).at(-1);if(!hasPendingRepair(current)&&latest?.motion_id)await loadCandidate(latest.motion_id);}
  catch(error){if(valid(session,seq)&&idOf(current)===id){$('harness-repair-progress').textContent='进度暂时无法读取，原请求仍保留：'+error.message;scheduleRepair();}}
  finally{repairPolling=false;}
 }
 window.closeHarness=()=>{endTimelineDrag();epoch++;playing=false;stopRepairPolling();setBusy(false);$('harness').hidden=true;$('show-harness').setAttribute('aria-pressed','false');};
 window.resetHarness=()=>{window.closeHarness();current=null;source=null;candidate=null;pendingCreate=null;pendingImport=null;pendingRepair=null;rewriteLoaded=false;editingEnabled=false;detailEpoch++;sourceEpoch++;candidateEpoch++;frame=0;setBusy(false);$('harness-create').reset();$('harness-feedback-form').reset();$('harness-repair-progress').textContent='';$('harness-task-list').replaceChildren();$('harness-detail').hidden=true;$('harness-source-label').textContent='尚未载入';timelineZoom=1;drawTimeline();$('harness-candidate-label').textContent='尚无候选';$('harness-capabilities').replaceChildren(el('span','Kimodo · 待接入'));notify('');};
 async function refresh(){
  const session=state.session,seq=epoch;
  try{
   const [data,caps]=await Promise.all([api('/api/harness/tasks'),api('/api/harness/capabilities')]);if(!valid(session,seq))return;
   editingEnabled=caps.editing_enabled===true;syncTimeline();
   if(editingEnabled&&!rewriteLoaded){try{const models=await api('/api/rewrite/models');if(!valid(session,seq))return;const select=$('harness-rewrite-model');select.replaceChildren();const direct=el('option','英文原文直传（无改写费用）');direct.value='exact_english';select.append(direct);for(const model of items(models.models)){const option=el('option',model.label+(model.configured?'':'（未配置）'));option.value=model.id;option.disabled=!model.configured;select.append(option);}select.value=models.models?.some(m=>m.id===models.default&&m.configured)?models.default:'exact_english';rewriteLoaded=true;}catch(error){notify('改写模型列表读取失败，可使用英文原文：'+error.message,true);}}
   const list=$('harness-task-list');list.replaceChildren();
   for(const task of items(data)){const button=action((task.goal||'动作任务').slice(0,80)+' · '+human(task.status||task.state),()=>loadTask(idOf(task)));button.classList.toggle('active',idOf(task)===idOf(current));list.append(button);}
   if(!list.children.length)list.append(el('p','还没有任务。先选择一个动作，或描述希望生成的动作。'));
   const backends=items(caps.backends||caps.models);$('harness-capabilities').replaceChildren();
   for(const backend of backends){const badge=el('span',(backend.label||backend.id||backend.name)+' · '+(backend.available?'可用':'待接入'));badge.className='badge';badge.title=backend.reason||'';$('harness-capabilities').append(badge);}
   if(!backends.some(x=>String(x.id||x.name||'').toLowerCase().includes('kimodo'))){const badge=el('span','Kimodo · 待接入');badge.className='badge';$('harness-capabilities').append(badge);}
   if(caps.driver?.available===false)$('harness-capabilities').append(el('span',editingEnabled?'选区修复可直接执行；自动规划模型尚未接入。':'任务已可保存；自动决策模型尚未接入。'));
  }catch(error){if(valid(session,seq))notify(error.message,true);}
 }
 async function open(){
  if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);
  closeRetarget();window.closePersonal?.();window.closeGadget?.();state.playing=false;state.request++;
  for(const id of ['production','workspace','gallery','monitor','tasks','storage','retarget','gadget','personal'])$(id).hidden=true;
  for(const button of $('page-nav').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.id==='show-harness'));
  $('harness').hidden=false;epoch++;notify('');await refresh();drawTimeline();paint();
 }
 async function loadSource(id,range=null){
  endTimelineDrag();const session=state.session,seq=epoch,request=++sourceEpoch;source=null;frame=0;playing=false;timelineZoom=1;$('harness-track-scroll').scrollLeft=0;drawTimeline();$('harness-source-label').textContent='正在读取原动作…';
  try{const clip=await api('/api/harness/motions/'+encodeURIComponent(id)+'/preview');if(!valid(session,seq)||request!==sourceEpoch)return;
   source=normalize(clip);$('harness-source').value=id;$('harness-source-label').textContent=id+' · '+source.frames+' 帧 · '+source.fps+' FPS';
   $('harness-start').value=range?.start_frame??0;$('harness-end').value=range?.end_frame??source.frames;updateRange();$('harness-timeline').max=source.frames-1;drawTimeline();paint();
  }catch(error){if(valid(session,seq)&&request===sourceEpoch){$('harness-source-label').textContent='原动作未载入';notify(error.message,true);}}
 }
 function normalize(clip){
  if(!Array.isArray(clip?.joints)||!clip.joints.length||!Array.isArray(clip.parents)||!Number.isFinite(clip.fps)||clip.fps<=0)throw new Error('动作预览格式不完整');
  const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const frame of clip.joints)for(const point of frame.slice(0,22))for(let i=0;i<3;i++){min[i]=Math.min(min[i],point[i]);max[i]=Math.max(max[i],point[i]);}
  return {...clip,frames:clip.joints.length,names:clip.names||clip.parents.map((_,i)=>String(i)),duration_s:clip.joints.length/clip.fps,bounds:{min,max}};
 }
 function updateRange(){
  const note=$('harness-selection-note');
  try{const r=selection(),fps=source?.fps||30,n=r.end_frame-r.start_frame;note.textContent=source?timecode(r.start_frame,fps)+' – '+timecode(r.end_frame,fps)+' · 已选 '+(n/fps).toFixed(2)+' 秒':'先载入一个动作';const invalid=source&&(source.frames<30||n<12||n>300);note.classList.toggle('error',Boolean(invalid));if(invalid)note.textContent+=source.frames<30?' · 原动作需至少 1 秒':' · 请选择 0.4–10 秒';}
  catch(error){note.textContent=error.message;note.classList.toggle('error',true);}
  syncTimeline();
 }

 async function loadTask(id){
  endTimelineDrag();const session=state.session,seq=epoch,request=++detailEpoch;
  try{const result=await api('/api/harness/tasks/'+encodeURIComponent(id));if(!valid(session,seq)||request!==detailEpoch)return;candidate=null;candidateEpoch++;$('harness-candidate-label').textContent='选择一个候选进行对比';render(result);const task=taskOf(result);if(currentSource(task))await loadSource(currentSource(task),task.selection);}
  catch(error){if(valid(session,seq)&&request===detailEpoch)notify(error.message,true);}
 }
 function render(result){
  const previousId=idOf(current);current=taskOf(result);const task=current;if(previousId!==idOf(task)){candidate=null;candidateEpoch++;if(!currentSource(task)){source=null;sourceEpoch++;$('harness-source').value='';$('harness-source-label').textContent='此任务尚未关联原动作';}}$('harness-detail').hidden=false;$('harness-task-title').textContent='任务详情';$('harness-task-state').textContent=human(task.status||task.state);$('harness-task-summary').textContent=task.goal||'';
  const budget=task.budget||{},used=task.usage?.attempts??task.attempts??task.attempts_used;
  const edit=items(task.operations).filter(op=>op.tool==='edit_motion').at(-1);$('harness-repair-progress').textContent=edit?.result?.message||'';scheduleRepair();syncTimeline();
  $('harness-task-budget').textContent='尝试上限：'+(budget.max_attempts??'未设置')+(used===undefined?'':' · 已用 '+used+' 次')+' · 模型费用上限 '+(budget.max_cost_usd??0)+' 美元'+(task.message?' · '+task.message:'');
  const reqs=items(result.requirements||task.requirements);$('harness-requirements').replaceChildren(...(reqs.length?reqs.map(value=>el('p',typeof value==='string'?value:value.text||value.description||value.message||JSON.stringify(value))):[el('p','以任务原始要求及补充意见为准。')]));
  const candidates=items(result.candidates||task.candidates);$('harness-candidates').replaceChildren();
  for(const value of candidates){const box=el('article'),mid=value.motion_id||value.asset_id;box.append(el('strong',mid||value.id||value.candidate_id),el('p',value.metadata?.description||value.description||human(value.status)));
   const actions=el('div');actions.className='production-actions';if(mid)actions.append(action('与原动作对比',()=>loadCandidate(mid)),action('以此版本新建任务',()=>branch(mid)));
   const acceptedId=task.accepted_candidate_id||task.accepted_candidate,candidateId=value.id||value.candidate_id;const accepted=Boolean(acceptedId)&&acceptedId===candidateId||value.status==='accepted';
   if(accepted)actions.append(el('span','已接受，原版本仍保留。'));else{const accept=action('接受此版本',()=>mutate('accept',{candidate_id:value.id||value.candidate_id}));accept.disabled=busy||value.status==='quarantined';actions.append(accept);}box.append(actions);$('harness-candidates').append(box);
  }
  if(!candidates.length)$('harness-candidates').append(el('p','尚无候选。后端未接入时不会自动产生修复结果。'));
  const evidence=items(result.evidence||result.evidences||task.evidence||task.evidences);$('harness-evidence').replaceChildren();
  for(const value of evidence){const box=el('article');box.append(el('strong',human(value.status||value.result?.status||'unknown')+' · '+(value.kind||value.check||value.name||'检查')),el('p',value.message||value.summary||value.result?.message||value.body?.message||''));const details=el('details');details.append(el('summary','查看测量记录'),el('pre',JSON.stringify(value,null,2)));box.append(details);$('harness-evidence').append(box);}
  if(!evidence.length)$('harness-evidence').append(el('p','还没有检查记录。'));
  const events=items(result.events||task.events);$('harness-events').replaceChildren(el('p','任务编号：'+idOf(task)));for(const event of events){const box=el('article');box.append(el('p',event.message||event.type||event.kind||'任务更新'),el('pre',JSON.stringify(event.body||event.payload||event,null,2)));$('harness-events').append(box);}
 }
 async function loadCandidate(id){
  const session=state.session,seq=epoch,request=++candidateEpoch;candidate=null;$('harness-candidate-label').textContent='正在读取候选…';
  try{const clip=await api('/api/harness/motions/'+encodeURIComponent(id)+'/preview');if(!valid(session,seq)||request!==candidateEpoch)return;candidate=normalize(clip);$('harness-candidate-label').textContent=id+' · '+candidate.frames+' 帧';paint();}catch(error){if(valid(session,seq)&&request===candidateEpoch){$('harness-candidate-label').textContent='候选未载入';notify(error.message,true);}}
 }
 async function mutate(actionName,body={}){
  if(busy||!current)return;const session=state.session,seq=epoch,id=idOf(current);setBusy(true);notify('');
  try{const result=await api('/api/harness/tasks/'+encodeURIComponent(id)+'/'+actionName,{method:'POST',body:JSON.stringify(body)});if(!valid(session,seq)||idOf(current)!==id)return;render(result);if(actionName==='accept'&&currentSource(current))await loadSource(currentSource(current),current.selection);if(actionName==='feedback')$('harness-feedback-form').reset();notify(actionName==='accept'?'已接受新版本，原动作和此前版本仍保留。':actionName==='advance'?'本步已执行；请查看状态和检查结果。':'任务已更新。');await refresh();}
  catch(error){if(valid(session,seq))notify(error.message,true);}finally{if(valid(session,seq))setBusy(false);}
 }
 function branch(id){if(busy)return;$('harness-source').value=id;$('harness-goal').value='';current=null;candidate=null;candidateEpoch++;detailEpoch++;$('harness-detail').hidden=true;pendingCreate=null;const loading=loadSource(id);$('harness-goal').focus();notify('将从这个版本创建新任务，原任务记录保留。');return loading;}
 $('show-harness').onclick=open;$('harness-refresh').onclick=async()=>{await refresh();if(current)await loadTask(idOf(current));};
 $('harness-load-source').onclick=()=>{const id=$('harness-source').value.trim();if(id)return loadSource(id);else notify('请填写原动作编号。',true);};
 $('harness-use-current').onclick=()=>{if(!state.current?.motion_id)return notify('请先在动作库中查看一个动作。',true);const start=Math.max(0,Math.floor(state.frame));loadSource(state.current.motion_id,{start_frame:start,end_frame:state.clip?.frames||start+1});};
 $('harness-new').onclick=()=>{if(busy||pendingCreate)return notify('请先确认当前提交结果。',true);current=null;candidate=null;candidateEpoch++;detailEpoch++;$('harness-detail').hidden=true;$('harness-goal').value='';$('harness-goal').focus();notify('');};
 $('harness-create').onsubmit=async event=>{
  event.preventDefault();if(busy)return;const session=state.session,seq=epoch;let body;
  try{const mid=$('harness-source').value.trim(),goal=$('harness-goal').value.trim(),attempts=Number($('harness-budget').value);if(!goal||!Number.isInteger(attempts)||attempts<1||attempts>20)throw new Error('请填写动作要求和 1 至 20 次的尝试上限。');const cost=Number($('harness-cost').value);if(!Number.isFinite(cost)||cost<0||cost>1000)throw new Error('请填写 0 至 1000 美元的费用上限。');body={goal,budget:{max_attempts:attempts,max_cost_usd:cost}};if(mid){body.source_motion_id=mid;body.selection=selection();}const fingerprint=JSON.stringify(body);if(pendingCreate&&pendingCreate.fingerprint!==fingerprint)throw new Error('上次提交结果尚未确认，请先重试原请求并刷新任务列表。');if(!pendingCreate)pendingCreate={fingerprint,body:{...body,request_id:uid()}};body=pendingCreate.body;
  }catch(error){return notify(error.message,true);}setBusy(true);
  try{const result=await api('/api/harness/tasks',{method:'POST',body:JSON.stringify(body)});if(!valid(session,seq))return;pendingCreate=null;render(result);notify('任务已保存。选中片段后可点击“修复选中片段”，或补充修改意见。');await refresh();return current;}
  catch(error){if(valid(session,seq)){if(error.status>=400&&error.status<500)pendingCreate=null;notify(error.message+(pendingCreate?' · 提交结果未确认，重试会沿用同一请求编号。':''),true);}}finally{if(valid(session,seq))setBusy(false);}
 };
 $('harness-feedback-form').onsubmit=event=>{event.preventDefault();mutate('feedback',{message:$('harness-feedback').value.trim()});};
 $('harness-run-repair').onclick=async()=>{
  if(busy||!editingEnabled)return;if(hasPendingRepair(current)){await pollRepair();return;}
  const session=state.session,seq=epoch;
  try{
   let mid=$('harness-source').value.trim();const prompt=$('harness-goal').value.trim(),range=selection();
   if(!source||!mid)throw new Error('请先载入原动作并选择片段');
   if(range.end_frame-range.start_frame<12||range.end_frame-range.start_frame>300)throw new Error('请选择 0.4–10 秒的片段；过短的片段无法容纳平滑过渡');
   if(prompt.length<3||prompt.length>2000)throw new Error('请填写 3–2000 字的修复描述');
   if(!current||currentSource(current)!==mid||current.goal!==prompt||JSON.stringify(current.selection)!==JSON.stringify(range)||['accepted','cancelled','failed'].includes(current.status)){const created=await $('harness-create').onsubmit({preventDefault(){}});if(!created||!valid(session,seq))return;mid=currentSource(created);await loadSource(mid,range);}
   if(!valid(session,seq)||!current||currentSource(current)!==mid||current.goal!==prompt||JSON.stringify(current.selection)!==JSON.stringify(range))return;
   const id=idOf(current),selected=$('harness-rewrite-model').value||'exact_english';
   const body={prompt,prompt_mode:selected==='exact_english'?'exact_english':'translate',rewrite_model:selected==='exact_english'?'':selected};
   const fingerprint=JSON.stringify({id,...body});if(pendingRepair&&pendingRepair.fingerprint!==fingerprint)throw new Error('上一修复请求尚未确认，请先查看原任务或按原参数重试');
   if(!pendingRepair)pendingRepair={fingerprint,body:{...body,request_id:uid()}};
   setBusy(true);$('harness-repair-progress').textContent='正在提交修复请求…';
   const result=await api('/api/harness/tasks/'+encodeURIComponent(id)+'/repair',{method:'POST',body:JSON.stringify(pendingRepair.body)});
   if(!valid(session,seq)||idOf(current)!==id)return;
   if(result.decision_receipt?.action_applied)pendingRepair=null;
   render(result);notify(hasPendingRepair(current)?'修复已排队。完成后会显示候选，原动作保持不变。':result.operations?.at(-1)?.result?.message||'请查看任务执行结果。');
  }catch(error){if(valid(session,seq)){if(error.status>=400&&error.status<500)pendingRepair=null;notify(error.message+(pendingRepair?' · 重试会沿用同一修复编号。':''),true);}}
  finally{if(valid(session,seq))setBusy(false);}
 };
 $('harness-advance').onclick=()=>{if(!current)return;if(Number(current.budget?.max_cost_usd||0)<=0)return notify('模型费用上限为 0。如需自动决策，请以明确的费用上限新建任务；也可直接导入片段。',true);mutate('advance');};$('harness-cancel').onclick=()=>mutate('cancel');$('harness-branch').onclick=()=>{const sourceId=currentSource(current);if(sourceId)return branch(sourceId);};

 $('harness-import-form').onsubmit=async event=>{
  event.preventDefault();if(busy||!current)return;const session=state.session,seq=epoch,id=idOf(current),file=$('harness-import-file').files?.[0];
  try{if(!file||file.size>4*1024*1024||!file.name.toLowerCase().endsWith('.npz'))throw new Error('请选择不超过 4 MiB 的数值 NPZ 文件。');
   const raw=new Uint8Array(await file.arrayBuffer());if(!valid(session,seq)||idOf(current)!==id)return;let binary='';for(let offset=0;offset<raw.length;offset+=32768)binary+=String.fromCharCode(...raw.subarray(offset,offset+32768));const encoded=btoa(binary),fingerprint=id+':'+encoded;
   if(pendingImport&&pendingImport.fingerprint!==fingerprint)throw new Error('上一片段导入尚未确认，请先重试同一个文件。');if(!pendingImport)pendingImport={fingerprint,body:{request_id:uid(),npz_base64:encoded}};
   setBusy(true);const result=await api('/api/harness/tasks/'+encodeURIComponent(id)+'/import-patch',{method:'POST',body:JSON.stringify(pendingImport.body)});if(!valid(session,seq)||idOf(current)!==id)return;pendingImport=null;render(result);notify('已用你提供的片段创建候选。请先对比和检查，再决定是否接受。');await refresh();
  }catch(error){if(valid(session,seq)){if(error.status>=400&&error.status<500)pendingImport=null;notify(error.message,true);}}finally{if(valid(session,seq))setBusy(false);}
 };
 for(const id of ['harness-start','harness-end'])$(id).oninput=updateRange;
 $('harness-mark-start').onclick=()=>{if(!source||rangeLocked())return;const a=Math.floor(frame);changeRange(a,Math.max(a+1,Number($('harness-end').value)));};$('harness-mark-end').onclick=()=>{if(!source||rangeLocked())return;const b=Math.floor(frame)+1;changeRange(Math.min(b-1,Number($('harness-start').value)),b);};
 $('harness-play').onclick=()=>{if(source||candidate)playing=!playing;$('harness-play').textContent=playing?'暂停':'播放';};$('harness-timeline').oninput=()=>seek(Number($('harness-timeline').value));
 function paint(){
  if($('harness').hidden)return;const all=[source,candidate].filter(Boolean),min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
  for(const clip of all)for(let i=0;i<3;i++){min[i]=Math.min(min[i],clip.bounds.min[i]);max[i]=Math.max(max[i],clip.bounds.max[i]);}const center=min.map((v,i)=>Number.isFinite(v)?(v+max[i])/2:0),radius=Math.max(1.5,...min.map((v,i)=>Number.isFinite(v)?max[i]-v:0))*.7;
  for(const [id,clip] of [['harness-source-canvas',source],['harness-candidate-canvas',candidate]]){const canvas=$(id),ctx=canvas.getContext('2d'),rect=canvas.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,2),w=rect.width,h=rect.height;if(!w||!h)continue;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);studioLight(ctx,w,h);if(!clip)continue;
   const p=clip.joints[Math.min(clip.frames-1,Math.floor(frame))],project=value=>{const x=value[0]-center[0],y=value[1]-center[1],z=value[2]-center[2],a=x*Math.cos(.55)+z*Math.sin(.55),b=-x*Math.sin(.55)+z*Math.cos(.55),scale=Math.min(w,h)/radius*.39;return [w/2+a*scale,h*.48-(y*Math.cos(.3)-b*Math.sin(.3))*scale,y*Math.sin(.3)+b*Math.cos(.3)];};litSkeleton(ctx,p,clip.parents,clip.names,project);
  }
  syncPlayhead();
 }
 function tick(now){try{const delta=Math.min(.1,(now-last)/1000||0);last=now;if(!$('harness').hidden)autoScrollTimeline(delta);if(!$('harness').hidden&&playing&&!document.hidden){const clip=source||candidate;if(clip){let start=0,end=clip.frames;if($('harness-loop').checked&&source){const range=selection();start=Math.max(0,range.start_frame-10);end=Math.min(clip.frames,range.end_frame+10);}frame+=delta*clip.fps;if(frame>=end||frame<start)frame=start;paint();}}}catch(error){playing=false;notify(error.message,true);}finally{requestAnimationFrame(tick);}}
 requestAnimationFrame(tick);window.addEventListener('resize',()=>{drawTimeline();paint();});
 const repair=el('button','修复选中片段');repair.type='button';repair.onclick=async()=>{const id=state.current?.motion_id,start=Math.max(0,Math.floor(state.frame)),end=state.clip?.frames;if(!id)return;await open();await loadSource(id,{start_frame:start,end_frame:end||start+1});$('harness-goal').focus();};$('next-motion').after(repair);
})();
