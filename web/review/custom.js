'use strict';
const customState={busy:false,submitting:false,key:null,signature:null,resultId:null,items:[],after:null,resultBusy:false,resultEpoch:0,refreshEpoch:0,history:false,jobsAfter:null,focusId:null};
const customLabels={queued:'等待文本转换',preparing:'转换英文提示词',ready:'等待 GPU',running:'生成中',completed:'已完成',failed:'失败',cancelled:'已停止'};
function resetCustom(){customState.refreshEpoch++;customState.resultEpoch++;customState.focusId=null;customState.busy=false;customState.submitting=false;customState.resultBusy=false;customState.history=false;customState.jobsAfter=null;$('custom-submit').disabled=false;$('custom-history').textContent='全部历史';$('custom-history').disabled=false;$('custom-jobs-more').disabled=false;$('custom-jobs-more').hidden=true;customState.key=null;customState.signature=null;customState.retryParent=null;customState.retryMode=null;customState.resultId=null;customState.items=[];customState.after=null;}
$('custom-form').onsubmit=async event=>{
 event.preventDefault();if(customState.submitting)return;const session=state.session;
 const selected=$('custom-rewrite-model').value;
 const value={prompt_mode:selected==='exact_english'?'exact_english':'translate',rewrite_model:selected==='exact_english'?'':selected,description:$('custom-description').value.trim(),n:Number($('custom-count').value),duration:Number($('custom-duration').value)};
 if(customState.retryParent){value.parent_request_id=customState.retryParent;}
 const signature=JSON.stringify(value);
 if(customState.signature!==signature||!customState.key){customState.signature=signature;customState.key=Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');}
 value.request_id=customState.key;customState.submitting=true;$('custom-submit').disabled=true;
 try{const accepted=await api('/api/custom',{method:'POST',body:JSON.stringify(value),signal:AbortSignal.timeout(10000)});if(session!==state.session)return;$('custom-notice').textContent=' 已提交，已切换实时队列；可关闭网页，任务会在后台执行。';customState.key=null;customState.retryParent=null;customState.retryMode=null;customState.history=false;customState.jobsAfter=null;customState.focusId=accepted.request_id||value.request_id;$('custom-history').textContent='全部历史';await refreshCustom(false,true);}
 catch(error){if(session!==state.session)return;$('custom-notice').textContent=error.message+'；超时后重试相同内容会复用请求编号，避免重复提交。';}
 finally{if(session===state.session){customState.submitting=false;$('custom-submit').disabled=false;}}
};
async function customResults(id,append=false){
 if(!id||(append&&(customState.resultBusy||id!==customState.resultId||customState.after===null)))return;
 const account=state.session,request=++customState.resultEpoch,after=append?customState.after:0;
 customState.resultBusy=true;customState.resultId=id;$('custom-results').hidden=false;$('custom-result-more').disabled=true;
 if(!append){customState.items=[];customState.after=null;$('custom-result-items').replaceChildren();$('custom-result-more').hidden=true;$('custom-result-label').textContent='请求 '+id+' · 正在加载结果…';}
 const current=()=>state.session===account&&customState.resultEpoch===request;
 try{
  const result=await api('/api/custom/results?'+new URLSearchParams({id,after}),{signal:AbortSignal.timeout(10000)});
  if(!current())return;
  customState.resultId=id;customState.items=append?[...customState.items,...result.items]:result.items;customState.after=result.next_after;
  $('custom-results').hidden=false;$('custom-result-label').textContent='请求 '+id+' · 已载入 '+customState.items.length+' 个结果（生成过程中可点击“查看结果”刷新）';
  $('custom-result-items').replaceChildren();
  for(const item of customState.items){const button=document.createElement('button');button.textContent=item.motion_id+' · '+(item.state==='done'?(state.user.role==='admin'?'查看 / 审核':'查看动作'):'待完成');button.disabled=item.state!=='done';button.onclick=async()=>{if(state.saving||!leave())return;state.items=customState.items.filter(i=>i.state==='done');state.after=null;switchPage(false);await selectMotion(item.motion_id,true);};$('custom-result-items').append(button);}
  if(!customState.items.length)$('custom-result-items').textContent='尚无动作结果，任务仍可在后台继续。';
  $('custom-result-more').hidden=customState.after===null;
 }catch(error){if(current()){$('custom-result-label').textContent='请求 '+id+' · 结果加载失败，请再次点击“查看结果”重试';notice(error.message,true);}}finally{if(current()){customState.resultBusy=false;$('custom-result-more').disabled=false;}}
}
$('custom-result-more').onclick=()=>customResults(customState.resultId,true);
async function refreshCustom(append=false,manual=false){
 if((customState.history&&!manual)||(customState.busy&&!manual)||$('production').hidden||!state.user||(document.hidden&&!manual))return;
 customState.busy=true;$('custom-history').disabled=true;$('custom-jobs-more').disabled=true;
 const account=state.session,request=++customState.refreshEpoch,history=customState.history;
 const current=()=>state.session===account&&customState.refreshEpoch===request;
 try{
  const query=history?'?history=1'+(append?'&after='+encodeURIComponent(customState.jobsAfter):''):'';
  const data=await api('/api/custom'+query,{signal:AbortSignal.timeout(8000)});if(!current())return;
  const quota=data.quota||{};
  if(quota.limited){$('custom-quota').textContent='普通用户服务器 API 额度：近 5 小时 '+quota.window_5h.used.toLocaleString()+' / '+quota.window_5h.limit.toLocaleString()+' token，本周 '+quota.week.used.toLocaleString()+' / '+quota.week.limit.toLocaleString()+' token。实际用量取决于重写模型和 Embedding；本地 agent 重写用量不计入此处。';}
  else $('custom-quota').textContent='管理员账户不限制服务器 API Token 额度。费用估算见运行监控。';
  $('custom-queue-summary').textContent=(state.user.role==='admin'?'显示所有用户作业 · GPU 队列 '+(data.queue.gpu_jobs??0)+' 项 · 文本队列 '+(data.queue.preparing_jobs??0)+' 项。':'仅显示你的作业；队列人数不对普通用户展示。')+data.queue.policy+'。每用户最多 5 个未完成作业、合计 10000 个待生成动作。';
  $('custom-updated').textContent=(customState.history?'全部历史 · 按创建时间倒序':'每 5 秒刷新 · '+new Date().toLocaleTimeString('zh-CN'));if(!append)$('custom-jobs').replaceChildren();
  customState.jobsAfter=data.next_after??null;$('custom-jobs-more').hidden=!customState.history||customState.jobsAfter===null;
  for(const job of data.jobs){
   const article=document.createElement('article');article.className='custom-job';article.setAttribute('data-request-id',job.id);
   const title=document.createElement('h4');title.textContent=(customLabels[job.status]||job.status)+(job.cancel&&['running','preparing'].includes(job.status)?'（本批完成后停止）':'')+' · '+job.done+' / '+job.n+' 个';article.append(title);
   if(job.queue_position){const position=document.createElement('p');position.textContent=(job.queue_phase==='gpu'?'GPU':'文本转换')+' 队列第 '+job.queue_position+' 位'+(job.status==='running'||job.status==='preparing'?' · 当前处理中':' · 等待轮转');article.append(position);if(['queued','ready'].includes(job.status)){const waiting=document.createElement('progress');waiting.setAttribute('aria-label','等待资源，队列第 '+job.queue_position+' 位');article.append(waiting);}}
   const progress=document.createElement('progress');progress.max=job.n;progress.value=job.done;progress.setAttribute('aria-label','生成完成 '+job.done+' / '+job.n);article.append(progress);
   for(const text of [job.description,job.prompt?'英文提示词：'+job.prompt:'',job.message,new Date(job.created*1000).toLocaleString('zh-CN')+' · '+job.owner+' · '+job.duration+' 秒/动作']){if(text){const p=document.createElement('p');p.textContent=text;article.append(p);}}
   const view=document.createElement('button');view.textContent='查看结果';view.onclick=()=>customResults(job.id);article.append(view);
   if(['queued','preparing','ready','running'].includes(job.status)&&!job.cancel){const stop=document.createElement('button');stop.textContent='停止定制任务';stop.onclick=async()=>{stop.disabled=true;try{await api('/api/custom/cancel',{method:'POST',body:JSON.stringify({request_id:job.id})});if(state.session===account)await refreshCustom(false,true);}catch(error){if(state.session===account)notice(error.message,true);}finally{if(state.session===account)stop.disabled=false;}};article.append(stop);}
   if(['failed','cancelled'].includes(job.status)&&job.done<job.n){const retry=document.createElement('button');retry.textContent='以新任务重试';retry.onclick=()=>{$('custom-description').value=job.description;$('custom-count').value=job.n-job.done;$('custom-duration').value=job.duration;customState.retryParent=job.id;customState.retryMode=job.prompt_mode;$('custom-rewrite-model').value=job.prompt_mode==='exact_english'?'exact_english':(job.rewrite_model||'qwen-default');customState.key=null;$('custom-notice').textContent='已载入失败任务，可修改后提交；保留父任务链接。';};article.append(retry);}
   $('custom-jobs').append(article);
   if(job.id===customState.focusId){article.tabIndex=-1;article.focus({preventScroll:true});article.scrollIntoView?.({block:'nearest'});customState.focusId=null;}
  }
  if(!append&&!data.jobs.length)$('custom-jobs').textContent='暂无定制任务。';
 }catch(error){if(current())$('custom-updated').textContent='定制状态暂不可用：'+error.message;}finally{if(current()){customState.busy=false;$('custom-history').disabled=false;$('custom-jobs-more').disabled=false;}}
}
$('custom-history').onclick=()=>{customState.history=!customState.history;$('custom-history').textContent=customState.history?'返回实时队列':'全部历史';refreshCustom(false,true);};
$('custom-jobs-more').onclick=()=>refreshCustom(true,true);
setInterval(refreshCustom,5000);

let rewriteModelsSession=-1;
async function loadRewriteModels(){
 if(!state.user||$('production').hidden||rewriteModelsSession===state.session)return;
 const session=state.session;rewriteModelsSession=session;
 try{
  const data=await api('/api/rewrite/models');if(session!==state.session)return;
  const select=$('custom-rewrite-model'),previous=select.value;select.replaceChildren();
  for(const model of data.models){const option=document.createElement('option');option.value=model.id;option.textContent=model.label+(model.configured?'':'（未配置密钥）');option.disabled=!model.configured;select.append(option);}
  const exact=document.createElement('option');exact.value='exact_english';exact.textContent='英文原文直传（不重写）';select.append(exact);
  if([...select.options].some(o=>o.value===previous&&!o.disabled))select.value=previous;
  else select.value=[...select.options].find(o=>!o.disabled)?.value||'exact_english';
  $('custom-rewrite-help').textContent=data.scope+' 英文直传须以 A person 开头，12–70 个英文词。本地 Codex / Cursor 任务请通过 Gadget 提交。';
 }catch(error){if(session===state.session){rewriteModelsSession=-1;$('custom-rewrite-help').textContent='模型列表暂不可用：'+error.message;}}
}
setInterval(loadRewriteModels,5000);
