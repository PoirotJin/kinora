'use strict';
const productionState={busy:false,command:false,active:false,clip:null,motion:null,requested:null,version:0,start:0,bounds:null};
const productionLabels={queued:'等待启动',running:'运行中',stopping:'正在停止',stopped:'已停止',completed:'已完成',failed:'失败'};
function productionSettings(){return {continuous:$('production-continuous').checked,topics:[...$('production-topics').querySelectorAll('input:checked')].map(x=>x.value),per_topic:Number($('production-count').value),k:Number($('production-k').value),rounds:Number($('production-rounds').value),duration:Number($('production-duration').value),queue_low:Number($('queue-low').value),queue_high:Number($('queue-high').value)};}
function productionEstimate(){$('production-rounds').disabled=$('production-continuous').checked;const s=productionSettings();if(s.continuous){$('production-estimate').textContent='持续生产：每轮最多 '+s.topics.length*s.per_topic+' 条新文本、'+s.topics.length*s.per_topic*s.k+' 个候选动作；容量保护或故障时停止。';return;}const texts=s.topics.length*s.per_topic*s.rounds;$('production-estimate').textContent=`目标新增 ${texts} 条英文文本、${texts*s.k} 个候选动作，合计 ${(texts*s.k*s.duration/60).toFixed(1)} 分钟动作数据。实际数量可能因去重或失败而减少。`;}
function setupProduction(topics){
 $('production-topics').replaceChildren();
 for(const topic of topics.filter(t=>!t.manual_only)){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=topic.id;input.checked=true;label.append(input,document.createTextNode(topic.name));$('production-topics').append(label);}
 productionEstimate();
}
function showProduction(){closeRetarget();
 if(state.saving||!leave())return;
 if(state.dirty)fillReview(state.current?.review);
 state.playing=false;state.request++;
 for(const id of ['storage','workspace','gallery','monitor','tasks'])$(id).hidden=true;
 $('production').hidden=false;
 for(const id of ['show-storage','show-home','show-review','show-gallery','show-monitor','show-tasks'])$(id).setAttribute('aria-pressed',String(id==='show-home'));
 refreshProduction();
 if(typeof refreshCustom==='function')refreshCustom();
 if(state.user?.role==='admin'&&typeof refreshAdmin==='function')refreshAdmin();
}
$('show-home').onclick=showProduction;
$('production-settings').oninput=productionEstimate;
function productionButtons(){
 $('production-start').disabled=productionState.command||productionState.active;
 $('production-stop').disabled=productionState.command||!productionState.active;
 $('schedule-enable').disabled=productionState.command;
 $('schedule-disable').disabled=productionState.command;
}
async function productionCommand(path,value){
 if(productionState.command)return;
 productionState.command=true;productionButtons();
 try{await api('/api/production/'+path,{method:'POST',body:JSON.stringify(value),signal:AbortSignal.timeout(10000)});notice(path==='start'?'生产已提交到后台，可关闭网页。':path==='stop'?'停止请求已提交；定时调度保持原设置。':'调度设置已保存。');await refreshProduction();}
 catch(error){notice(error.message+'；如连接超时，请先查看任务状态再操作。',true);}
 finally{productionState.command=false;productionButtons();}
}
$('production-form').onsubmit=event=>{event.preventDefault();productionCommand('start',productionSettings());};
$('production-stop').onclick=()=>productionCommand('stop',{});
$('schedule-enable').onclick=()=>productionCommand('schedule',{enabled:true,interval_minutes:Number($('schedule-interval').value),settings:productionSettings()});
$('schedule-disable').onclick=()=>productionCommand('schedule',{enabled:false});
async function productionPreview(item){
 if(productionState.requested===item.motion_id)return;
 const version=++productionState.version;productionState.requested=item.motion_id;
 $('production-preview-text').textContent=item.motion_id+' · 正在异步读取动作…';
 try{
  const clip=await motionPreview(item.motion_id,{signal:AbortSignal.timeout(15000)});
  if(version!==productionState.version||!state.user)return;
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for(const frame of clip.joints)for(const p of frame){const x=p[0]*.85+p[2]*.53,y=p[1]-.18*p[2];minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
  productionState.bounds={x:(minX+maxX)/2,y:(minY+maxY)/2,scale:Math.min(250/Math.max(.5,maxX-minX),140/Math.max(.5,maxY-minY))};
  productionState.clip=clip;productionState.motion=item;productionState.start=performance.now();
  $('production-preview-text').textContent=item.motion_id+' · '+item.topic+' · '+clip.duration_s.toFixed(1)+' 秒\n'+item.text;
  $('production-review').disabled=false;
 }catch(error){if(version===productionState.version){productionState.requested=null;$('production-preview-text').textContent='预览加载失败，将自动重试：'+error.message;}}
}
$('production-review').onclick=async()=>{if(productionState.motion){switchPage(false);await selectMotion(productionState.motion.motion_id);}};
async function refreshProduction(){
 if(productionState.busy||$('production').hidden||state.user?.role!=='admin'||document.hidden)return;
 productionState.busy=true;
 try{
  const result=await api('/api/production',{signal:AbortSignal.timeout(8000)});
  if(!state.user||$('production').hidden)return;
  $('production-updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN')+' · 每 3 秒刷新';
  if(typeof renderProductionHealth==='function')renderProductionHealth(result.health,'production');
  const job=result.jobs[0];productionState.active=!!job&&['queued','running','stopping'].includes(job.status);productionButtons();
  $('production-state').textContent=job?`${productionLabels[job.status]||job.status} · ${job.message||'后台进程准备中'}`:'尚未启动生产';
  $('production-state').className=job?.status==='failed'?'error':'';
  $('production-current').replaceChildren();
  for(const item of result.current){const p=document.createElement('p');p.textContent='当前批次 · Seed '+item.seed+'\n'+item.text;$('production-current').append(p);}
  const schedule=result.schedule;
  let scheduleText=schedule?.enabled?'已启用 · 每 '+schedule.interval_s/60+' 分钟 · 下次 '+new Date(schedule.next_at*1000).toLocaleString('zh-CN'):'调度已暂停';
  if(schedule?.enabled){const settings=JSON.parse(schedule.settings);scheduleText+=' · 主题 '+settings.topics.join('、')+' · 每主题 '+settings.per_topic+' 条 × K '+settings.k+' × '+settings.rounds+' 轮';}
  $('schedule-status').textContent=scheduleText+(schedule?.last_message?'\n'+schedule.last_message:'')+(result.scheduler_error?'\n'+result.scheduler_error:'');
  $('production-history').replaceChildren();
  for(const record of result.jobs){const tr=document.createElement('tr');for(const value of [new Date(record.created*1000).toLocaleString('zh-CN'),record.source==='scheduler'?'定时':'立即启动',productionLabels[record.status]||record.status,record.owner,record.message||'等待进程启动']){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('production-history').append(tr);}
  $('production-latest').replaceChildren();
  for(const item of result.latest){const button=document.createElement('button');button.textContent=item.motion_id+' · '+item.topic;button.onclick=()=>productionPreview(item);$('production-latest').append(button);}
  if(result.latest.length&&(!productionState.motion||result.latest[0].motion_id!==productionState.newest)){productionState.newest=result.latest[0].motion_id;productionPreview(result.latest[0]);}
  if(!productionState.clip&&result.latest.length)productionPreview(result.latest[0]);
  if(result.error)$('production-updated').textContent+=' · '+result.error;
 }catch(error){$('production-updated').textContent='连接异常，当前显示上次结果：'+error.message;}
 finally{productionState.busy=false;}
}
setInterval(refreshProduction,3000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshProduction();});
function productionAnimation(now){
 if(!$('production').hidden&&!document.hidden&&productionState.clip&&state.user){
  const clip=productionState.clip,b=productionState.bounds,canvas=$('production-preview'),context=canvas.getContext('2d');
  context.setTransform(2,0,0,2,0,0);studioLight(context,320,184);
  const frame=clip.joints[Math.floor(Math.max(0,now-productionState.start)*clip.fps/1000)%clip.frames];
  const project=p=>[160+(p[0]*.85+p[2]*.53-b.x)*b.scale,90-(p[1]-.18*p[2]-b.y)*b.scale,p[2]];
  litSkeleton(context,frame,clip.parents,clip.names,project,.75);
 }
 requestAnimationFrame(productionAnimation);
}
requestAnimationFrame(productionAnimation);
