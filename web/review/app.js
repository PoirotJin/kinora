'use strict';
const $=id=>document.getElementById(id);
function closeRetarget(){window.closeHarness?.();window.closePersonal?.();$('retarget').hidden=true;$('show-retarget').setAttribute('aria-pressed','false');window.closeGadget?.();}
const labels={pending:'未审核',accepted:'已通过',rejected:'不通过',uncertain:'待复核'};
const issueLabels={direction:'方向不符',gait:'步态不符',timing:'顺序 / 节奏',self_penetration:'肢体穿透',root_gait_mismatch:'步态与根轨迹不一致',foot_slide:'滑步',floor_penetration:'穿地',jitter:'抖动',unnatural_pose:'姿态异常',action_mismatch:'动作不符',start_end_pose:'起止姿态不符',support_contact:'支撑 / 接触不合理',other:'其他'};
const state={session:0,user:null,items:[],current:null,clip:null,dirty:false,revision:0,after:null,request:0,playing:false,frame:0,yaw:.55,pitch:.3,zoom:1,center:[0,1,0],radius:2,saving:false,listing:0,listLoading:false,listContext:null,historyAfter:null};
sessionStorage.removeItem('mf_token');localStorage.removeItem('mf_reviewer');
if(location.hash)history.replaceState(null,'',location.pathname);
function notice(message,error=false){$('notice').textContent=message;$('notice').classList.toggle('error',error);}
function generationDate(value){
 if(!value)return '生成时间：未记录';
 const date=new Date(value);
 if(Number.isNaN(date.getTime()))return '生成时间：未记录';
 return '生成时间：'+new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date)+'（北京时间 UTC+8）';
}
async function api(path,options={}){
 const session=state.session;
 const response=await fetch(path,{...options,credentials:'same-origin',headers:{'X-MotionFarm-Request':'1',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
 const body=await response.json();
 if(session!==state.session)throw new DOMException("会话已切换", "AbortError");
 if(!response.ok){if(response.status===401)showLogin();if(body.must_change)showPassword(true);const error=new Error(body.error||`请求失败 ${response.status}`);error.status=response.status;error.current=body.current;throw error;}
 return body;
}
function leave(){return !state.dirty||confirm('当前审核尚未保存，确定放弃修改吗？');}
async function motionPreview(id,options={}){
 const session=state.session;
 if(!('Worker' in window))return api('/api/motions/'+id+'/preview');
 const response=await fetch('/api/motions/'+id+'/preview.bin',{credentials:'same-origin',signal:options.signal});
 if(session!==state.session)throw new DOMException('会话已切换','AbortError');
 if(!response.ok){const body=await response.json();if(response.status===401)showLogin();throw new Error(body.error||'动作读取失败');}
 const buffer=await response.arrayBuffer();
 const decoded=await new Promise((resolve,reject)=>{
  const worker=new Worker('/preview-worker.js');
  const finish=(error,result)=>{clearTimeout(timer);worker.terminate();error?reject(error):resolve(result);};
  const timer=setTimeout(()=>finish(new Error('动作解码超时')),10000);
  worker.onmessage=event=>finish(event.data.error?new Error(event.data.error):null,event.data);
  worker.onerror=()=>finish(new Error('动作解码失败'));
  worker.postMessage(buffer,[buffer]);
 });
 const clip=decoded.meta,flat=new Float32Array(decoded.buffer),count=clip.joint_count;
 clip.joints=Array.from({length:clip.frames},(_,frame)=>Array.from({length:count},(_,joint)=>{
  const offset=(frame*count+joint)*3;return flat.subarray(offset,offset+3);
 }));
 return clip;
}
function clearClip(message='请选择一个动作'){
 $('motion-status').textContent='—';$('clip-info').textContent='骨架预览 · Y 轴向上';fillReview(null);
 state.playing=false;state.clip=null;state.current=null;state.dirty=false;state.request++;$('fields').disabled=true;$('play').textContent='播放';$('loading').hidden=false;$('loading').textContent=message;$('prompt').textContent='选择动作后显示原始英文描述。';$('metadata').replaceChildren();$('quality').textContent='';$('history').replaceChildren();$('history-count').textContent='';$('motion-title').textContent='选择一个动作';$('saved').textContent='';$('timeline').max=0;$('timeline').value=0;$('timecode').textContent='0.00 / 0.00 s';
}
function renderList(){
 $('motions').replaceChildren();
 if(!state.items.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='暂无符合筛选条件的动作。';$('motions').append(empty);}
 for(const item of state.items){
  const button=document.createElement('button');button.className='motion-card'+(state.current?.motion_id===item.motion_id?' active':'');
  const top=document.createElement('div');top.className='card-top';const id=document.createElement('strong');id.textContent=item.motion_id;
  const badge=document.createElement('span');const status=item.review?.status||'pending';badge.className='badge '+status;badge.textContent=labels[status];top.append(id,badge);
  button.append(top);button.onclick=()=>selectMotion(item.motion_id);$('motions').append(button);
 }
 $('list-caption').textContent=`已载入 ${state.items.length} 个动作`+(state.listContext?.source?' · 来源：'+state.listContext.source:'');$('more').hidden=state.after===null;
}
async function loadList(append=false){
 if(append&&state.listContext?.loadMore)return loadContextMore();
 const seq=++state.listing,request=state.request;state.listLoading=true;
 if(!append)$('fields').disabled=true;
 try{
  const query=new URLSearchParams({topic:$('topic').value,status:$('filter').value,owner:$('owner-filter').value,after:append?state.after||0:0});
  const result=await api('/api/motions?'+query);if(seq!==state.listing||request!==state.request)return;
  if(!append)state.listContext=null;state.items=append?[...state.items,...result.items]:result.items;state.after=result.next_after;renderList();
  if($('filter').value.startsWith('direction_')&&result.direction_progress){const p=result.direction_progress;$('list-caption').textContent+=` · 历史补检 ${p.scanned}/${p.target||'待统计'}`+(p.state==='paused'?'（生成优先，已暂停）':p.state==='complete'?'（完成）':'');}
  if(!append){clearClip();if(state.items.length)await selectMotion(state.items[0].motion_id,true);}
 }catch(error){if(seq===state.listing)notice(error.message,true);}
 finally{if(seq===state.listing){state.listLoading=false;$('fields').disabled=state.saving||!state.clip||Boolean(state.current?.read_only);}}
}
async function loadContextMore(){
 if(state.listLoading||!state.listContext?.loadMore)return;
 const seq=state.listing,context=state.listContext,session=state.session;state.listLoading=true;
 try{const result=await context.loadMore(state.after);if(seq!==state.listing||session!==state.session||context!==state.listContext)return;
  const ids=new Set(state.items.map(x=>x.motion_id));state.items.push(...result.items.filter(x=>!ids.has(x.motion_id)));state.after=result.next_after;renderList();
 }catch(error){if(session===state.session)notice(error.message,true);}finally{if(seq===state.listing)state.listLoading=false;}
}
window.openMotionFromResults=async(id,{items=[],after=null,source='所选动作',loadMore=null}={})=>{
 if(state.saving||!leave())return false;
 state.listing++;state.listLoading=false;state.listContext={source,loadMore};
 state.items=[...new Map(items.map(x=>[x.motion_id,x])).values()];
 if(!state.items.some(x=>x.motion_id===id))state.items.unshift({motion_id:id});
 state.after=typeof loadMore==='function'?after:null;renderList();switchPage(false);
 await selectMotion(id,true);return state.current?.motion_id===id;
};
function fillReview(review){
 state.revision=review?.revision||0;$('reviewer').value=state.user?.username||'';
 $('semantic').value=review?.semantic||'unsure';$('quality-status').value=review?.quality||'unsure';$('corrected').value=review?.corrected_text||'';$('notes').value=review?.notes||'';$('decision').value=review?.status||'uncertain';
 for(const input of $('issues').querySelectorAll('input'))input.checked=(review?.issues||[]).includes(input.value);
 $('saved').textContent=review?`已保存 · v${review.revision}`:'未审核';if($('review-last'))$('review-last').textContent=review?`最后审核：${review.reviewer} · ${new Date(review.updated_at).toLocaleString()}`:'暂无人工审核记录';state.dirty=false;$('review-conflict')?.remove();
}
async function selectMotion(id,force=false){
 if(state.saving||(!force&&!leave()))return;
 if(!state.items.some(x=>x.motion_id===id)){state.listing++;state.listLoading=false;state.listContext={source:'所选动作',loadMore:null};state.after=null;state.items=[{motion_id:id}];}
 const seq=++state.request;state.playing=false;state.clip=null;state.dirty=false;$('fields').disabled=true;$('loading').hidden=false;$('loading').textContent='正在读取动作…';$('play').textContent='播放';
 try{
  const detail=await api('/api/motions/'+id);if(seq!==state.request)return;
  state.current=detail;renderList();$('motion-title').textContent=id;
  const status=detail.review?.status||'pending';if(detail.storage_tier==='cold')$('loading').textContent='正在解压冷归档中的动作…';$('motion-status').textContent=labels[status];$('motion-status').className='badge '+status;
  $('prompt').textContent=detail.prompt.text;$('metadata').replaceChildren();
  const generationState=detail.state==='done'?'已生成':(detail.state|| (detail.location?'已生成':'尚未生成'));
  const bits=[detail.topic,'生成状态：'+generationState];
  if(detail.owner)bits.push(detail.owner===state.user?.username?'我的作业 · '+detail.owner:'提交人 '+detail.owner);
  if(detail.seed!==undefined)bits.push('Seed '+detail.seed);
  if(detail.production_variant)bits.push('随机变体 · 复用合格描述 · '+detail.production_variant.day+' · 每条描述每日最多 '+detail.production_variant.daily_description_cap+' 次采样');
  if(detail.text_id)bits.push('文本 '+String(detail.text_id).slice(0,12)+'…');
  for(const text of bits){const span=document.createElement('span');span.textContent=text;$('metadata').append(span);}
  if(detail.screening){const info=document.createElement('span');info.textContent='筛查提示（待人工确认）：'+(detail.screening.flags.map(f=>screenLabels[f]||f).join('、')||'未触发阈值，不代表已合格');$('metadata').append(info);if(detail.screening.nearest){const neighbor=document.createElement('button');neighbor.textContent='查看近邻 '+detail.screening.nearest;neighbor.onclick=()=>selectMotion(detail.screening.nearest);$('metadata').append(neighbor);}}
  fillReview(detail.review);state.historyAfter=Object.hasOwn(detail,'history_next_after')?detail.history_next_after:(detail.history.length>=20?detail.history.at(-1).revision:null);renderHistory(detail.history);heartbeatReview();
  if(!detail.location){$('loading').textContent='动作尚未生成，暂不可审核';return;}
  const clip=await motionPreview(id);if(seq!==state.request)return;
  const generated=document.createElement('span');generated.textContent=generationDate(clip.created_utc);$('metadata').append(generated);
  state.clip=clip;state.frame=0;fit();$('timeline').max=clip.frames-1;$('timeline').value=0;$('timecode').textContent=`0.00 / ${clip.duration_s.toFixed(2)} s`;$('clip-info').textContent=`${clip.frames} 帧 · ${clip.fps} FPS · ${clip.duration_s.toFixed(1)} 秒`;
  if(detail.machine_review){const mr=detail.machine_review,el=document.createElement('span');el.textContent='机器审核：'+({pending:'待评分',needs_review:'待复核 / 校准中',auto_passed:'机器通过（非人工审核）',quarantined:'已隔离'}[mr.state]||mr.state)+(mr.score==null?'':' · TMR 相似度 '+mr.score.toFixed(3))+' · 不代表物理安全';$('metadata').append(el);}
  const bodyEvidence=detail.machine_review?.evidence?.body_quality||detail.screening?.metrics?.body_quality;
  if(bodyEvidence){const names={left_foot:'左脚',right_foot:'右脚',left_shin:'左小腿',right_shin:'右小腿',left_thigh:'左大腿',right_thigh:'右大腿'};const pairs=[...(bodyEvidence.events?.self_collision||[])].sort((a,b)=>Number(b.parts.some(x=>x.endsWith('foot')))-Number(a.parts.some(x=>x.endsWith('foot'))));for(const hit of pairs.slice(0,3)){const t=hit.intervals[0];if(!t)continue;const b=document.createElement('button');b.textContent=hit.parts.map(x=>names[x]||x).join(' / ')+' 疑似穿透 · '+t.start_s.toFixed(2)+' 秒';b.onclick=()=>{state.frame=Math.min(t.start_frame,clip.frames-1);state.playing=false;$('play').textContent='播放';};$('metadata').append(b);}for(const [key,frames] of Object.entries(bodyEvidence.issue_frames||{})){if(!frames.length)continue;const b=document.createElement('button');b.textContent=({self_collision_proxy:'疑似肢体穿透',stance_slide:'接地滑移',root_gait_consistency:'根轨迹不一致'}[key]||key)+' · 定位 '+(frames[0]/clip.fps).toFixed(2)+' 秒';b.onclick=()=>{state.frame=Math.min(frames[0],clip.frames-1);state.playing=false;$('play').textContent='播放';};$('metadata').append(b);}}
  const direction=clip.quality?.direction_semantics;
  const directionLabel=document.createElement('span');directionLabel.textContent='方向语义：'+(!direction?'未检查':({pass:'基础方向通过；完整语义待审核',fail:'不符合',review:'待审核'}[direction.status]||'待审核')+' · '+[...(direction.reasons||[]),...(direction.warnings||[])].join('；'));$('metadata').append(directionLabel);
  $('quality').textContent=JSON.stringify({machine_review:detail.machine_review,motion_id:id,text_id:detail.text_id,quality:clip.quality,screening:detail.screening,screening_note:'启发式筛查，仅供人工复核；不是自动合格判定'},null,2);
  $('loading').hidden=true;$('fields').disabled=Boolean(detail.read_only);if(detail.read_only)$('saved').textContent='本地导入动作：可预览、质量检查及重定向';state.playing=true;$('play').textContent='暂停';resize();
 }catch(error){if(seq===state.request){$('loading').textContent='预览加载失败';notice(error.message,true);}}
}
function renderHistory(historyItems){
 $('history').replaceChildren();$('history-count').textContent=`(${historyItems.length}${state.historyAfter!==null?'，还有更早记录':''})`;
 const semanticLabels={match:'匹配',partial:'部分匹配',mismatch:'不匹配',unsure:'无法确定'},qualityLabels={good:'良好',issues:'存在问题',unsure:'无法确定'};
 for(const item of historyItems){const div=document.createElement('details');div.className='history-entry';const title=document.createElement('summary');title.textContent=`v${item.revision} · ${labels[item.status]} · ${item.reviewer} · ${new Date(item.updated_at).toLocaleString()}`;
  const fields=[['文本匹配',semanticLabels[item.semantic]||item.semantic],['动作质量',qualityLabels[item.quality]||item.quality],['问题标签',(item.issues||[]).map(x=>issueLabels[x]||x).join('、')||'无'],['修正描述',item.corrected_text||'无'],['备注',item.notes||'无'],['动作源校验',item.motion_sha256||'未记录']];div.append(title);for(const [label,value] of fields){const p=document.createElement('p');p.textContent=label+'：'+value;div.append(p);}$('history').append(div);
 }
 if(state.historyAfter!==null){const more=document.createElement('button');more.type='button';more.textContent='加载更早审核记录';more.onclick=async()=>{const id=state.current?.motion_id,session=state.session,request=state.request;more.disabled=true;try{const data=await api('/api/motions/'+id+'/review-history?after='+state.historyAfter);if(session!==state.session||request!==state.request)return;state.current.history.push(...data.items);state.historyAfter=data.next_after;renderHistory(state.current.history);}catch(e){if(session===state.session&&request===state.request){notice(e.message,true);more.disabled=false;}};};$('history').append(more);}
}
for(const [key,label] of Object.entries(issueLabels)){const wrap=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.value=key;wrap.append(input,document.createTextNode(label));$('issues').append(wrap);}
$('review-form').addEventListener('input',()=>{state.dirty=true;$('saved').textContent='有未保存修改';});
$('review-form').onsubmit=async event=>{
 event.preventDefault();if(!state.current||state.current.read_only||state.saving||state.listLoading)return;
 const id=state.current.motion_id,session=state.session,request=state.request;let saved=false;
 const body={revision:state.revision,status:$('decision').value,semantic:$('semantic').value,quality:$('quality-status').value,issues:[...$('issues').querySelectorAll('input:checked')].map(x=>x.value),corrected_text:$('corrected').value.trim(),notes:$('notes').value.trim(),reviewer:$('reviewer').value.trim()};
 state.saving=true;$('fields').disabled=true;$('save').textContent='保存中…';
 try{const review=await api('/api/motions/'+id+'/review',{method:'POST',body:JSON.stringify(body)});if(session!==state.session||request!==state.request)return;saved=true;state.current.review=review;fillReview(review);const item=state.items.find(x=>x.motion_id===id);if(item)item.review=review;renderList();$('motion-status').textContent=labels[review.status];$('motion-status').className='badge '+review.status;state.current.history=[review,...state.current.history];renderHistory(state.current.history);notice('审核已保存。原始文本与动作保持不变。');}
 catch(error){if(session===state.session&&request===state.request){notice(error.message,true);if(error.status===409)showReviewConflict();}}finally{state.saving=false;if(session===state.session&&request===state.request){$('fields').disabled=!state.clip;$('save').textContent='保存审核';}}
 if(saved&&session===state.session&&request===state.request&&$('review-auto-next').checked)await navigateReview(1);
};
$('next-motion').onclick=()=>navigateReview(1);
async function navigateReview(direction){
 if(state.saving||state.listLoading||!leave())return;
 let at=state.items.findIndex(x=>x.motion_id===state.current?.motion_id);
 if(direction>0&&at+1>=state.items.length&&state.after!==null)await loadList(true);
 const next=state.items[at+direction];
 if(next)await selectMotion(next.motion_id,true);else notice(direction>0?'已到'+(state.listContext?.source||'当前列表')+'末尾。':'已到当前列表开头。');
}
function showReviewConflict(){
 $('review-conflict')?.remove();const box=document.createElement('div');box.id='review-conflict';box.setAttribute('role','alert');
 const text=document.createElement('p');text.textContent='其他审核者已保存新版本。你的输入仍保留，请先复制草稿或查看最新版本；不会覆盖对方结论。';
 const copy=document.createElement('button');copy.type='button';copy.textContent='下载我的审核草稿';copy.onclick=()=>{const value={motion_id:state.current.motion_id,base_revision:state.revision,decision:$('decision').value,semantic:$('semantic').value,quality:$('quality-status').value,notes:$('notes').value,corrected_text:$('corrected').value,issues:[...$('issues').querySelectorAll('input:checked')].map(x=>x.value)};const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=value.motion_id+'-review-draft.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
 const reload=document.createElement('button');reload.type='button';reload.textContent='载入最新版本（放弃当前输入）';reload.onclick=()=>selectMotion(state.current.motion_id);
 box.append(text,copy,reload);$('review-form').after(box);
}
const reviewClient=(crypto.randomUUID?.()||Date.now().toString(36)+'_'+Math.random().toString(36).slice(2));let presenceMotion=null,presenceSession=-1,presenceBusy=false;
const lastReview=document.createElement('p');lastReview.id='review-last';lastReview.className='muted';$('review-form').before(lastReview);
const presenceLabel=document.createElement('p');presenceLabel.className='muted';presenceLabel.setAttribute('aria-live','polite');$('review-form').before(presenceLabel);
async function heartbeatReview(){
 if(presenceBusy)return;presenceBusy=true;const session=state.session;
 const id=state.user&&!state.current?.read_only&&!$('workspace').hidden&&!document.hidden?state.current?.motion_id:null;
 try{
  if(presenceMotion&&(presenceMotion!==id||presenceSession!==session)){
   const previous=presenceMotion;presenceMotion=null;if(presenceSession===session)await api('/api/motions/'+previous+'/presence',{method:'POST',body:JSON.stringify({client:reviewClient,active:false})});
  }
  if(!id){presenceLabel.textContent='';return;}
  presenceMotion=id;presenceSession=session;
  const data=await api('/api/motions/'+id+'/presence',{method:'POST',body:JSON.stringify({client:reviewClient,active:true})});
  if(session===state.session&&state.current?.motion_id===id){const others=data.reviewers.filter(x=>x!==state.user.username);presenceLabel.textContent=others.length?'正在查看 / 审核：'+others.join('、')+'。保存时检查版本，避免互相覆盖。':'暂无其他在线审核者（在线状态仅供参考）';}
 }catch(error){if(session===state.session)presenceLabel.textContent='在线审核状态暂不可用；保存仍受版本冲突保护。';}finally{presenceBusy=false;}
}
setInterval(heartbeatReview,20000);document.addEventListener('visibilitychange',heartbeatReview);
const reviewPrev=document.createElement('button');reviewPrev.type='button';reviewPrev.className='quiet wide';reviewPrev.textContent='← 上一个动作';reviewPrev.onclick=()=>navigateReview(-1);$('next-motion').before(reviewPrev);
const autoLabel=document.createElement('label'),autoInput=document.createElement('input');autoInput.type='checkbox';autoInput.id='review-auto-next';autoInput.checked=localStorage.getItem('mf-review-auto-next')==='1';autoInput.onchange=()=>localStorage.setItem('mf-review-auto-next',autoInput.checked?'1':'0');autoLabel.append(autoInput,document.createTextNode('保存成功后自动下一条'));$('review-form').after(autoLabel);
const pendingButton=document.createElement('button');pendingButton.type='button';pendingButton.className='quiet wide';pendingButton.textContent='只看未审核';pendingButton.onclick=()=>{if(state.saving||!leave())return;$('filter').value='pending';$('filter').dataset.previous='pending';loadList();};$('list-caption').before(pendingButton);
const shortcuts=document.createElement('p');shortcuts.className='muted footnote';shortcuts.textContent='快捷键：Alt + ↑ / ↓ 切换动作；Ctrl / ⌘ + Enter 保存；输入时不切换动作。当前结论以最新版本为准，各人的历史意见保留在修改历史中。';$('next-motion').after(shortcuts);
window.addEventListener('keydown',event=>{
 if($('workspace').hidden||document.querySelector('dialog[open]'))return;
 if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();if(!$('fields').disabled&&!state.saving)$('review-form').requestSubmit();return;}
 if(event.target.closest('input,textarea,select,[contenteditable="true"]'))return;
 if(event.altKey&&['ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();navigateReview(event.key==='ArrowUp'?-1:1);}
});

for(const id of ['topic','filter','owner-filter']){$(id).dataset.previous=$(id).value;$(id).onchange=()=>{if(state.saving||!leave()){$(id).value=$(id).dataset.previous;return;}$(id).dataset.previous=$(id).value;loadList();};}
$('refresh').onclick=()=>{if(!state.saving&&leave())loadList();};$('more').onclick=()=>loadList(true);
$('lock').onclick=async()=>{if(state.saving||!leave())return;try{await api('/api/auth/logout',{method:'POST',body:'{}'});showLogin();notice('已退出登录');}catch(error){notice(error.message,true);}};
$('export').onclick=async()=>{try{const data=await api('/api/export');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='motionfarm-reviews.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notice(data.next_after===null?'审核记录已导出。':'已导出前100条修订记录；更多记录可用 /api/export?after='+data.next_after+' 分页获取。');}catch(error){notice(error.message,true);}};
async function start(){const session=state.session;try{const {topics,generation}=await api('/api/topics');if(session!==state.session)return;for(const id of ['production-duration','custom-duration','research-replay-duration']){const input=$(id);if(input)input.max=String(id==='production-duration'?Math.min(12,generation?.duration_max_seconds||12):(generation?.duration_max_seconds||12));}$('topic').replaceChildren(new Option('全部主题',''));for(const topic of topics)$('topic').add(new Option(topic.name,topic.id));setupGallery(topics);setupProduction(topics);$('page-nav').hidden=false;$('login').hidden=true;showProduction();$('connection').textContent='服务器已连接';notice('');}catch(error){if(session===state.session){$('login').hidden=false;notice(error.message,true);}}}
function showLogin(){window.resetHarness?.();state.session++;presenceLabel.textContent='';presenceMotion=null;window.resetResearch?.();window.resetPersonal?.();
 window.resetMotionTags?.();window.resetAdmin?.();
 closeRetarget();window.resetRetarget?.();
 document.body.dataset.role='';for(const id of ['custom-jobs','custom-result-items','new-user-secret','admin-users','trash-items','custom-notice','custom-queue-summary','production-history','production-current'])$(id).replaceChildren();$('custom-results').hidden=true;$('custom-form').reset();
 if(typeof resetCustom==='function')resetCustom();
 state.user=null;state.listing++;state.listLoading=false;state.listContext=null;state.historyAfter=null;galleryState.items=[];galleryState.version++;galleryState.queue=[];galleryObserver.disconnect();clearClip();state.items=[];$('motions').replaceChildren();$('gallery-grid').replaceChildren();
 for(const id of ['storage','production','workspace','gallery','monitor','tasks','page-nav','password-panel','change-password','lock','export'])$(id).hidden=true;
 $('login').hidden=false;$('connection').textContent='未登录';$('password-form').reset();
}
function showPassword(required=false){closeRetarget();
 for(const id of ['storage','production','workspace','gallery','monitor','tasks','page-nav','login'])$(id).hidden=true;
 state.playing=false;state.request++;$('password-panel').hidden=false;$('cancel-password').hidden=required;
 $('password-help').textContent=required?'首次登录，请设置自己的密码。':'为当前账户设置新的登录密码。';$('password-form').reset();
}
async function enterAccount(user){
 const session=++state.session;state.user=user;document.body.dataset.role=user.role||'user';$('connection').textContent=user.username;$('lock').hidden=false;
 $('show-home').textContent=user.role==='admin'?'生产首页':'定制生成';
 $('show-monitor').textContent='运行监控';$('show-tasks').textContent='任务进度';
 $('monitor-title').textContent=$('show-monitor').textContent;
 $('task-progress-items').replaceChildren();$('task-progress-status').textContent='';
 $('home-title').textContent=user.role==='admin'?'生产首页':'定制生成';
 $('home-description').textContent=user.role==='admin'?'管理自动生产、用户作业与排队状态。':'描述需要的动作，提交作业后可查看排队与生成进度。';
 if(user.must_change){$('export').hidden=true;$('change-password').hidden=true;showPassword(true);return;}
 $('password-panel').hidden=true;$('change-password').hidden=false;$('export').hidden=user.role!=='admin';await start();if(session!==state.session)return;$('connection').textContent=user.username+' · '+(user.role==='admin'?'管理员':'普通用户');
}
$('login-form').onsubmit=async event=>{
 event.preventDefault();$('unlock').disabled=true;notice('');
 try{const result=await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('username').value.trim(),password:$('password').value})});$('password').value='';await enterAccount(result.user);}
 catch(error){$('password').value='';notice(error.message,true);}finally{$('unlock').disabled=false;}
};
$('change-password').onclick=()=>{if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);showPassword(false);};
$('cancel-password').onclick=()=>enterAccount(state.user);
$('password-form').onsubmit=async event=>{
 event.preventDefault();if($('new-password').value!==$('confirm-password').value){notice('两次输入的新密码不一致',true);return;}
 $('save-password').disabled=true;
 try{const result=await api('/api/auth/password',{method:'POST',body:JSON.stringify({old_password:$('old-password').value,new_password:$('new-password').value})});$('password-form').reset();notice('密码已修改');await enterAccount(result.user);notice('密码已修改');}
 catch(error){notice(error.message,true);}finally{$('save-password').disabled=false;}
};
window.addEventListener('beforeunload',e=>{if(state.dirty||state.saving){e.preventDefault();e.returnValue='';}});

// Dependency-free orthographic 3D skeleton renderer, Y-up and metre grid.
const canvas=$('stage'),ctx=canvas.getContext('2d');let w=0,h=0,last=0,drag=null,renderError=false;
function resize(){const r=canvas.getBoundingClientRect();w=r.width;h=r.height;const dpr=Math.min(devicePixelRatio||1,2);if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}ctx.setTransform(dpr,0,0,dpr,0,0);paintPreview();}
new ResizeObserver(resize).observe(canvas);
function fit(){state.yaw=.55;state.pitch=.3;state.zoom=1;const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const frame of state.clip.joints)for(const p of frame.slice(0,22))for(let i=0;i<3;i++){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i]);}state.center=min.map((v,i)=>(v+max[i])/2);state.radius=Math.max(1.5,...min.map((v,i)=>max[i]-v))*.7;}
function project(p){const x=p[0]-state.center[0],y=p[1]-state.center[1],z=p[2]-state.center[2];const a=x*Math.cos(state.yaw)+z*Math.sin(state.yaw),b=-x*Math.sin(state.yaw)+z*Math.cos(state.yaw),v=y*Math.cos(state.pitch)-b*Math.sin(state.pitch);const scale=Math.min(w,h)/state.radius*.39*state.zoom;return [w/2+a*scale,h*.48-v*scale,y*Math.sin(state.pitch)+b*Math.cos(state.pitch)];}
function line(a,b,color,width=1){const pa=project(a),pb=project(b);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(pa[0],pa[1]);ctx.lineTo(pb[0],pb[1]);ctx.stroke();}
function studioLight(context,width,height){
 const light=context.createRadialGradient(width*.34,height*.18,0,width*.5,height*.45,Math.max(width,height)*.75);
 light.addColorStop(0,'#ffffff');light.addColorStop(.55,'#f5f6f7');light.addColorStop(1,'#e9edf0');
 context.fillStyle=light;context.fillRect(0,0,width,height);
}
function litSkeleton(context,points,parents,names,projection,thickness=1){
 const bones=[];
 for(let i=1;i<points.length;i++){const parent=parents[i];if(parent>=0&&parent<points.length&&parent!==i)bones.push([i,parent]);}
 // Approximate a soft directional-light shadow on the actual Y=0 ground plane.
 context.save();context.lineCap='round';context.filter='blur(3px)';context.strokeStyle='rgba(64,78,90,.14)';context.lineWidth=5*thickness;context.beginPath();
 const floor=p=>projection([p[0]+Math.max(0,p[1])*.24,0,p[2]+Math.max(0,p[1])*.14]);
 for(const [i,parent] of bones){if(i>=22)continue;const a=floor(points[parent]),b=floor(points[i]);context.moveTo(a[0],a[1]);context.lineTo(b[0],b[1]);}context.stroke();context.restore();
 bones.sort((a,b)=>((projection(points[a[0]])[2]||0)+(projection(points[a[1]])[2]||0))-((projection(points[b[0]])[2]||0)+(projection(points[b[1]])[2]||0)));
 context.save();context.lineCap='round';
 for(const [i,parent] of bones){
  const a=projection(points[parent]),b=projection(points[i]),name=String(names[i]||'').toLowerCase();
  const colors=name.includes('left')||name.startsWith('l_')?['#139e9a','#65ded1','#25b9ad']:name.includes('right')||name.startsWith('r_')?['#ce8b26','#ffdb87','#f3b448']:['#718493','#becdd5','#91a6b4'];
  const width=(i<22?4.5:1.6)*thickness,dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy)||1;
  const mx=(a[0]+b[0])/2,my=(a[1]+b[1])/2,nx=-dy/len*width/2,ny=dx/len*width/2;
  const material=context.createLinearGradient(mx-nx,my-ny,mx+nx,my+ny);colors.forEach((color,index)=>material.addColorStop(index/2,color));
  context.strokeStyle=material;context.lineWidth=width;context.beginPath();context.moveTo(a[0],a[1]);context.lineTo(b[0],b[1]);context.stroke();
  if(i<22){context.fillStyle=colors[2];context.beginPath();context.arc(b[0],b[1],width*.62,0,Math.PI*2);context.fill();context.fillStyle=colors[1];context.beginPath();context.arc(b[0]-width*.12,b[1]-width*.15,width*.23,0,Math.PI*2);context.fill();}
 }
 context.restore();
}
function draw(){if($('workspace').hidden||w<=0||h<=0)return;ctx.clearRect(0,0,w,h);studioLight(ctx,w,h);if(!state.clip)return;const cx=Math.round(state.center[0]),cz=Math.round(state.center[2]),extent=Math.min(30,Math.ceil(state.radius*2));for(let i=-extent;i<=extent;i++){line([cx+i,0,cz-extent],[cx+i,0,cz+extent],'#dde2e7',.6);line([cx-extent,0,cz+i],[cx+extent,0,cz+i],'#dde2e7',.6);}const frames=state.clip.joints;for(let i=2;i<frames.length;i+=2)line([frames[i-2][0][0],.008,frames[i-2][0][2]],[frames[i][0][0],.008,frames[i][0][2]],'#86aaa1',1.5);
 const n=Math.min(frames.length-1,Math.floor(state.frame)),points=frames[n];
 litSkeleton(ctx,points,state.clip.parents,state.clip.names,project);
 $('timeline').value=n;$('timecode').textContent=`${(n/state.clip.fps).toFixed(2)} / ${state.clip.duration_s.toFixed(2)} s`;
}
// Draw immediately on user/data/layout changes, even when Safari suspends RAF.
function paintPreview(){try{draw();if(renderError&&state.clip)$('loading').hidden=true;renderError=false;}catch(error){state.playing=false;$('play').textContent='播放';$('loading').hidden=false;$('loading').textContent='预览绘制失败，请重新选择动作或刷新页面';if(!renderError)notice('预览绘制失败：'+error.message,true);renderError=true;}}
function tick(now){try{const delta=Math.min(.1,(now-last)/1000||0);last=now;if(state.playing&&state.clip&&!document.hidden)state.frame=(state.frame+delta*state.clip.fps*Number($('speed').value))%state.clip.frames;paintPreview();}finally{requestAnimationFrame(tick);}}requestAnimationFrame(tick);
function toggle(){if(!state.clip)return;state.playing=!state.playing;$('play').textContent=state.playing?'暂停':'播放';}
function step(delta){if(!state.clip)return;state.playing=false;$('play').textContent='播放';state.frame=Math.max(0,Math.min(state.clip.frames-1,Math.floor(state.frame)+delta));paintPreview();}
$('play').onclick=toggle;$('prev-frame').onclick=()=>step(-1);$('next-frame').onclick=()=>step(1);$('timeline').oninput=()=>{state.frame=Number($('timeline').value);state.playing=false;$('play').textContent='播放';paintPreview();};
$('front').onclick=()=>{state.yaw=0;state.pitch=.05;paintPreview();};$('side').onclick=()=>{state.yaw=Math.PI/2;state.pitch=.05;paintPreview();};$('top').onclick=()=>{state.yaw=0;state.pitch=1.5;paintPreview();};$('reset').onclick=()=>{if(state.clip){fit();paintPreview();}};
canvas.onpointerdown=e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(!drag)return;state.yaw+=(e.clientX-drag[0])*.008;state.pitch=Math.max(-.3,Math.min(1.5,state.pitch+(e.clientY-drag[1])*.008));drag=[e.clientX,e.clientY];paintPreview();};canvas.onpointerup=()=>drag=null;canvas.onpointercancel=()=>drag=null;canvas.addEventListener('wheel',e=>{e.preventDefault();state.zoom=Math.max(.3,Math.min(5,state.zoom*Math.exp(-e.deltaY*.001)));paintPreview();},{passive:false});
window.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT','BUTTON'].includes(e.target.tagName)||$('workspace').hidden)return;if(e.code==='Space'){e.preventDefault();toggle();}if(e.code==='ArrowLeft'){e.preventDefault();step(-1);}if(e.code==='ArrowRight'){e.preventDefault();step(1);}});
const galleryState={items:[],topic:'',owner:'',after:null,version:0,count:0,busy:false,queue:[],active:0};
function switchPage(gallery){closeRetarget();$('storage').hidden=true;$('show-storage').setAttribute('aria-pressed','false');$('production').hidden=true;$('show-home').setAttribute('aria-pressed','false');$('tasks').hidden=true;$('show-tasks').setAttribute('aria-pressed','false');$('monitor').hidden=true;$('show-monitor').setAttribute('aria-pressed','false');$('gallery').hidden=!gallery;$('workspace').hidden=gallery;$('show-gallery').setAttribute('aria-pressed',String(gallery));$('show-review').setAttribute('aria-pressed',String(!gallery));if(gallery)state.playing=false;else resize();heartbeatReview();}
function setupGallery(topics){
 window.refreshTagVocabulary?.();
 $('gallery-topics').replaceChildren();
 const mine=document.createElement('button');mine.textContent='我的作业';mine.dataset.mine='1';mine.onclick=()=>{galleryState.owner=galleryState.owner==='me'?'':'me';loadGallery();};$('gallery-topics').append(mine);
 for(const topic of [{id:'',name:'全部主题'},...topics]){const button=document.createElement('button');button.textContent=topic.name;button.dataset.topic=topic.id;button.onclick=()=>{galleryState.topic=topic.id;loadGallery();};$('gallery-topics').append(button);}
}
$('show-gallery').onclick=()=>{if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);switchPage(true);loadGallery();};
$('show-review').onclick=()=>{switchPage(false);if(!state.items.length)loadList();};
$('gallery-more').onclick=()=>loadGallery(true);
const galleryObserver=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting){galleryObserver.unobserve(entry.target);galleryState.queue.push({card:entry.target,version:galleryState.version});}}pumpThumbnails();},{rootMargin:'100px'});
async function pumpThumbnails(){
 while(galleryState.active<2&&galleryState.queue.length){
  const job=galleryState.queue.shift();if(job.version!==galleryState.version||!job.card.isConnected)continue;
  galleryState.active++;
  api('/api/motions/'+job.card.dataset.motion+'/thumbnail').then(clip=>{
   if(job.version!==galleryState.version||!job.card.isConnected)return;
   drawThumbnail(job.card.querySelector('canvas'),clip);
   job.card.querySelector('.gallery-date').textContent=generationDate(clip.created_utc);
   job.card.querySelector('.gallery-duration').textContent=clip.duration_s.toFixed(1)+' 秒 · '+clip.fps+' FPS · 中间帧';
  }).catch(()=>{if(job.card.isConnected)job.card.querySelector('.gallery-duration').textContent='缩略图暂不可用，点击查看详情';}).finally(()=>{galleryState.active--;pumpThumbnails();});
 }
}
function drawThumbnail(canvas,clip){
 canvas.width=640;canvas.height=368;
 const context=canvas.getContext('2d');context.setTransform(2,0,0,2,0,0);
 const projected=clip.joints.map(p=>[p[0]*.85+p[2]*.53,p[1]-.18*p[2]]);
 const xs=projected.map(p=>p[0]),ys=projected.map(p=>p[1]),lo=[Math.min(...xs),Math.min(...ys)],hi=[Math.max(...xs),Math.max(...ys)];
 const scale=Math.min(240/Math.max(.5,hi[0]-lo[0]),132/Math.max(.5,hi[1]-lo[1]));
 const project=p=>[160+(p[0]*.85+p[2]*.53-(lo[0]+hi[0])/2)*scale,86-(p[1]-.18*p[2]-(lo[1]+hi[1])/2)*scale,p[2]];
 studioLight(context,320,184);
 litSkeleton(context,clip.joints,clip.parents,clip.names,project,.75);
}

async function loadGallery(append=false){
 if(append&&galleryState.busy)return;
 const version=append?galleryState.version:++galleryState.version;galleryState.busy=true;$('gallery-more').disabled=true;
 if(!append){galleryObserver.disconnect();galleryState.queue=[];galleryState.count=0;galleryState.after=null;galleryState.items=[];$('gallery-grid').replaceChildren();}
 for(const button of $('gallery-topics').children){
  if(button.dataset.mine)button.setAttribute('aria-pressed',String(galleryState.owner==='me'));
  else button.setAttribute('aria-pressed',String(button.dataset.topic===galleryState.topic));
 }
 $('gallery-caption').textContent='正在加载动作…';
 try{
  const result=await api('/api/motions?'+new URLSearchParams({topic:galleryState.topic,owner:galleryState.owner,after:append?galleryState.after||0:0,tag_group:$('tag-filter-group').value,tag:$('tag-filter-name').value,tagged_by:$('tag-filter-user').value}));if(version!==galleryState.version)return;
  for(const item of result.items){
   const card=document.createElement('article');card.className='gallery-card';card.dataset.motion=item.motion_id;
   const canvas=document.createElement('canvas');canvas.width=320;canvas.height=184;canvas.setAttribute('aria-label','动作中间帧骨架缩略图');
   const title=document.createElement('strong');title.textContent=item.motion_id+' · '+([...$('topic').options].find(o=>o.value===item.topic)?.textContent||item.topic)+(item.owner?(item.owner===state.user?.username?' · 我的作业':' · '+item.owner):'');
   if(item.production_variant)title.textContent+=' · 随机变体';
   const text=document.createElement('p');text.textContent=item.text;
   const badge=document.createElement('span');badge.className='badge '+(item.review?.status||'pending');badge.textContent=labels[item.review?.status||'pending'];
   const duration=document.createElement('div');duration.className='gallery-duration';duration.textContent=item.state==='done'?'正在加载缩略图…':'动作尚未生成';
   const date=document.createElement('div');date.className='gallery-date';
   const retarget=document.createElement('button');retarget.textContent='重定向';retarget.onclick=e=>{e.stopPropagation();showRetarget(item.motion_id);};card.append(canvas,title,text,badge,duration,date,retarget);window.motionResearchActions?.(card,item);card.onclick=()=>window.openMotionFromResults(item.motion_id,{items:galleryState.items,source:'Gallery · 当前已载入结果'});$('gallery-grid').append(card);
   const view=document.createElement('button');view.textContent='查看动作';view.onclick=e=>{e.stopPropagation();window.openMotionFromResults(item.motion_id,{items:galleryState.items,source:'Gallery · 当前已载入结果'});};card.append(view);
   if(state.user?.role==='admin'||(item.owner&&item.owner===state.user?.username)){
    const remove=document.createElement('button');remove.textContent='删除';remove.setAttribute('aria-label','删除动作 '+item.motion_id);
    remove.onclick=async e=>{
     e.stopPropagation();
     if(!confirm('将 '+item.motion_id+' 移入回收站？需要恢复时请联系管理员。'))return;
     remove.disabled=true;
     try{await api('/api/motions/'+item.motion_id+'/remove',{method:'POST',body:JSON.stringify({reason:'Gallery 删除'})});
      state.items=state.items.filter(x=>x.motion_id!==item.motion_id);renderList();
      if(state.current?.motion_id===item.motion_id)clearClip();
      notice('动作已移入回收站。');await loadGallery();
     }catch(error){notice(error.message,true);remove.disabled=false;}
    };card.append(remove);
   }
   window.renderMotionTags?.(card,item);
   if(item.state==='done')galleryObserver.observe(card);
  }
  galleryState.items.push(...result.items);galleryState.count+=result.items.length;galleryState.after=result.next_after;$('gallery-more').hidden=result.next_after===null;
  $('gallery-caption').textContent=galleryState.count?`已载入 ${galleryState.count} 个动作`+(galleryState.owner==='me'?' · 仅显示你提交的作业':'')+' · 缩略图展示动作中间帧，点击播放完整动作':(galleryState.owner==='me'?'还没有你提交的动作。':'此类别暂时没有动作。');
 }catch(error){if(version===galleryState.version)$('gallery-caption').textContent='加载失败：'+error.message;}
 finally{if(version===galleryState.version){galleryState.busy=false;$('gallery-more').disabled=false;}}
}
const initialSession=state.session;
api('/api/auth/me').then(result=>enterAccount(result.user)).catch(()=>{if(state.session===initialSession)showLogin();});

// Read-only monitoring; the server owns a single bounded sampler across clients.
let monitorTimer=null,monitorBusy=false;
async function refreshTaskProgress(){
 const account=state.user;
 const [custom,retarget]=await Promise.all(['/api/custom','/api/retarget'].map(path=>api(path,{signal:AbortSignal.timeout(8000)})));
 if(state.user!==account||$('tasks').hidden)return;
 const labels={queued:'排队中',preparing:'准备提示词',ready:'等待推理',running:'运行中',stopping:'停止中',completed:'完成',done:'完成',failed:'失败',cancelled:'已取消'};
 const active=new Set(['queued','preparing','ready','running','stopping']);
 const rows=[...(custom.jobs||[]).map(j=>({...j,kind:'定制生成',total:j.n})),...(retarget.items||[]).map(j=>({...j,kind:'动作重定向'}))];
 rows.sort((a,b)=>Number(active.has(b.status))-Number(active.has(a.status))||b.created-a.created);
 $('task-progress-status').textContent=(account.role==='admin'?'全部用户':'我的')+' · '+rows.filter(j=>active.has(j.status)).length+' 个进行中任务 · 最近记录（每类最多 100 条）';
 $('tasks-updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN')+' · 每 10 秒刷新';
 $('task-progress-items').replaceChildren();
 if(!rows.length)$('task-progress-items').textContent='暂无任务，可在定制生成或动作重定向页面提交。';
 for(const job of rows){
  const card=document.createElement('article'),title=document.createElement('strong'),detail=document.createElement('p'),bar=document.createElement('progress');
  title.textContent=job.kind+' · '+(labels[job.status]||job.status)+(account.role==='admin'?' · '+job.owner:'');
  const total=Number(job.total)||0,done=Number(job.done)||0;
  detail.textContent=job.id+' · '+(total?done+' / '+total:'进度待更新')+(job.queue_position?' · 队列位置 '+job.queue_position:'')+' · '+(job.message||'');
  bar.max=100;if(total)bar.value=Math.min(100,100*done/total);else if(['done','completed'].includes(job.status))bar.value=100;
  bar.setAttribute('aria-label',job.kind+' '+job.id+' 进度');card.append(title,detail,bar);$('task-progress-items').append(card);
 }
}
const numberText=v=>v===null||v===undefined?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:1});
const byteText=v=>v===null||v===undefined?'—':v>=1073741824?numberText(v/1073741824)+' GiB':v>=1048576?numberText(v/1048576)+' MiB':v>=1024?numberText(v/1024)+' KiB':numberText(v)+' B';
const mib=v=>v===null||v===undefined?'—':byteText(v)+'/s';
const gib=v=>v===null||v===undefined?'—':numberText(v/1073741824)+' GiB';
function monitorRows(target,rows){$(target).replaceChildren();for(const values of rows){const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value;tr.append(td);}$(target).append(tr);}}
function monitorMessages(target,items,empty){
 $(target).replaceChildren();if(!items.length){const p=document.createElement('p');p.className='muted';p.textContent=empty;$(target).append(p);}
 for(const item of items){const div=document.createElement('div');div.className='monitor-alert '+item.severity;
  const prefix=item.time?new Date(item.time*1000).toLocaleTimeString('zh-CN')+' · '+item.state+' · ':'';
  div.textContent=prefix+({critical:'异常',warning:'注意',info:'提示'}[item.severity]||'')+'：'+item.message;$(target).append(div);}
}
function renderMonitor(data){
 try{window.MotionFarmRolling?.render(data.rolling_24h);}catch(error){window.MotionFarmRolling?.connectionError('最近 24 小时数据格式异常，请刷新或检查服务日志。');}
 if(!data.ready){$('monitor-health').textContent=data.error||'采样正在初始化，请稍候…';return;}
 if(typeof renderProductionHealth==='function')renderProductionHealth(data.production_health,'monitor');
 const age=Date.now()/1000-data.sampled_at,stale=age>35;
 $('monitor-updated').textContent='最近采样：'+new Date(data.sampled_at*1000).toLocaleString('zh-CN')+' · 每 10 秒刷新';
 $('monitor-health').textContent=stale?'数据已过期，请检查监控采样服务':data.alerts.some(a=>a.severity==='critical')?'检测到异常，请查看故障详情':data.alerts.length?'有需要关注的状态':'当前已检查项未发现异常';
 $('monitor-health').className=stale?'monitor-alert critical':data.alerts.length?'monitor-alert warning':'monitor-alert healthy';
 const sum=key=>data.topics.some(t=>t[key]==null)?null:data.topics.reduce((n,t)=>n+t[key],0);
 const reviewed=data.reviews?Object.values(data.reviews).reduce((a,b)=>a+b,0):null;
 const values=[['全库已存动作',numberText(sum('motions')),'已审核 '+numberText(reviewed)],['全库英文文本',numberText(sum('texts')),'向量 '+numberText(sum('vectors'))],['CPU',numberText(data.cpu)+'%','1 分钟负载 '+numberText(data.load[0])+' / '+data.cpu_count+' 逻辑核'],['内存',numberText(data.memory.percent)+'%',gib(data.memory.used)+' / '+gib(data.memory.total)],['磁盘剩余',gib(data.disk.free),'总容量 '+gib(data.disk.total)],['GPU',data.gpu.available?numberText(data.history.at(-1)?.gpu)+'%':'不可用',data.gpu.items.length+' 张 GPU · 多卡取最高值']];
 $('monitor-kpis').replaceChildren();for(const [label,value,note] of values){const card=document.createElement('article');const title=document.createElement('div');title.textContent=label;const strong=document.createElement('strong');strong.textContent=value;const p=document.createElement('p');p.textContent=note;card.append(title,strong,p);$('monitor-kpis').append(card);}
 monitorRows('monitor-topics',data.topics.map(t=>[t.name,numberText(t.texts),numberText(t.vectors),numberText(t.motions),t.tasks?numberText(t.tasks.pending||0):'—',t.tasks?numberText(t.tasks.failed||0):'—',byteText(t.bytes),t.collector]));
 $('monitor-hardware').replaceChildren();
 const hardware=[...data.gpu.items.map(g=>g.name+' · GPU '+g.index+' · '+numberText(g.load)+'% · 显存 '+numberText(g.used_mb)+' / '+numberText(g.total_mb)+' MiB · '+numberText(g.temperature)+'°C'),...data.network.map(n=>n.name+' · 接收 '+mib(n.rx_bps)+' · 发送 '+mib(n.tx_bps))];
 for(const value of hardware){const p=document.createElement('p');p.textContent=value;$('monitor-hardware').append(p);}
 $('monitor-coverage').textContent=data.usage.coverage+(data.usage.available?'':' 当前暂无可读取的用量记录。');
 monitorRows('monitor-usage',data.usage.models.map(m=>[m.model+' / '+m.kind,...['calls','errors','input','output','total','unknown'].map(k=>numberText(m[k]))]));
 window.renderApiCosts?.(data.usage.costs);
 monitorMessages('monitor-alerts',data.alerts,'当前无告警。');monitorMessages('monitor-events',data.events,'暂时没有告警记录。');$('monitor-scope').textContent=data.scope;
}
async function refreshMonitor(manual=false){
 clearTimeout(monitorTimer);if(monitorBusy||$('monitor').hidden||state.user?.role!=='admin')return;
 if(document.hidden&&!manual){monitorTimer=setTimeout(refreshMonitor,10000);return;}
 const session=state.session;monitorBusy=true;$('monitor-refresh').disabled=true;
 try{const data=await api('/api/monitor',{signal:AbortSignal.timeout(8000)});if(session!==state.session||$('monitor').hidden)return;renderMonitor(data);await refreshAgent();}
 catch(error){if(session===state.session&&!$('monitor').hidden){$('monitor-health').className='monitor-alert critical';$('monitor-health').textContent='监控连接中断，以下为上次结果：'+error.message;window.MotionFarmRolling?.connectionError(error.message);}}
 finally{monitorBusy=false;$('monitor-refresh').disabled=false;if(!$('monitor').hidden)monitorTimer=setTimeout(refreshMonitor,10000);}
}
let tasksTimer=null,tasksBusy=false;
async function refreshTasks(manual=false){
 clearTimeout(tasksTimer);if(tasksBusy||$('tasks').hidden||!state.user)return;
 if(document.hidden&&!manual){tasksTimer=setTimeout(refreshTasks,10000);return;}
 const session=state.session;tasksBusy=true;$('tasks-refresh').disabled=true;
 try{await refreshTaskProgress();if(session===state.session&&!$('tasks').hidden&&state.user?.role==='admin')await refreshPipeline();}
 catch(error){if(session===state.session&&!$('tasks').hidden)$('task-progress-status').textContent='连接中断，以下为上次结果：'+error.message;}
 finally{tasksBusy=false;$('tasks-refresh').disabled=false;if(!$('tasks').hidden)tasksTimer=setTimeout(refreshTasks,10000);}
}
function showOperations(page){
 if(!state.user||(page==='monitor'&&state.user.role!=='admin')||state.saving||!leave())return;
 closeRetarget();window.closePersonal?.();window.closeGadget?.();if(state.dirty)fillReview(state.current?.review);state.playing=false;state.request++;
 for(const id of ['storage','production','workspace','gallery','monitor','tasks'])$(id).hidden=id!==page;
 for(const button of $('page-nav').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.id==='show-'+page));
 if(page==='monitor')refreshMonitor(true);else refreshTasks(true);
}
$('show-monitor').onclick=()=>showOperations('monitor');
$('show-tasks').onclick=()=>showOperations('tasks');
$('monitor-refresh').onclick=()=>refreshMonitor(true);
$('tasks-refresh').onclick=()=>refreshTasks(true);
async function refreshPipeline(){
 const session=state.session;try{
  const data=await api('/api/pipeline',{signal:AbortSignal.timeout(5000)});
  if(session!==state.session||state.user?.role!=='admin'||$('tasks').hidden)return;
  $('pipeline-status').textContent=!data.available?'流水线状态尚未就绪':(data.stale?'工作服务心跳已过期，请检查服务':'工作服务运行中')+' · '+new Date(data.sampled_at*1000).toLocaleTimeString('zh-CN');
  const names={control:'自动生产调度',prepare:'定制提示词准备',gpu:'定制 GPU 调度',retarget:'CPU 重定向'};
  $('pipeline-stages').replaceChildren();
  for(const stage of data.stages||[]){const card=document.createElement('article'),title=document.createElement('div'),value=document.createElement('strong'),note=document.createElement('p');title.textContent=names[stage.name]||stage.name;value.textContent=stage.running+' / '+stage.capacity;note.textContent='执行线程 / 上限 · 待执行 '+stage.pending+' · 调度异常 '+stage.failed+(stage.last_error?' · '+stage.last_error:'');card.append(title,value,note);$('pipeline-stages').append(card);}
  const labels={custom:'定制作业',retarget:'重定向',storage:'归档 / 导出',production:'自动生产'},states={queued:'排队',preparing:'准备',ready:'就绪',running:'运行',stopping:'停止中'};
  $('pipeline-queues').textContent=Object.entries(data.queues||{}).map(([name,counts])=>(labels[name]||name)+'：'+(counts.unavailable?'暂不可读':Object.entries(counts).map(([s,n])=>(states[s]||s)+' '+n).join(' / ')||'空闲')).join('；')+'。自动生产进程独立运行；GPU 总并发上限 1，归档上限 1。';
 }catch(error){if(session===state.session&&!$('tasks').hidden)$('pipeline-status').textContent='流水线状态读取失败：'+error.message;}
}
const screenLabels={possible_self_penetration:'疑似肢体穿透',possible_stance_sliding:'疑似接地滑移',possible_root_gait_mismatch:'疑似步态与根轨迹不一致',body_geometry_unavailable:'肢体几何检查不可用',possible_foot_slide:'疑似滑步',possible_direction_mismatch:'疑似方向不符',possible_duplicate:'疑似近似动作',speed_outlier:'速度异常',acceleration_outlier:'加速度异常',possible_ground_speed_outlier:'疑似地面动作速度异常',possible_floor_excursion:'疑似地面高度异常（参考面未校准）',possible_rise_posture_mismatch:'疑似起身姿态不符',possible_descent_posture_mismatch:'疑似下地姿态不符',possible_ground_posture_mismatch:'疑似地面姿态不符',possible_squat_range_mismatch:'疑似下蹲幅度不足'};
async function refreshAgent(){
 const session=state.session;try{
  const data=await api('/api/agent',{signal:AbortSignal.timeout(8000)}),h=data.hour;
  if(session!==state.session||$('monitor').hidden||state.user?.role!=='admin')return;
  const reviewed=data.topics.reduce((s,t)=>s+t.reviewed,0),accepted=data.topics.reduce((s,t)=>s+t.accepted,0);
  const decisions=Object.values(data.decisions).reduce((s,n)=>s+n,0);
  $('agent-summary').textContent='最近入库：'+(data.last_saved?new Date(data.last_saved*1000).toLocaleString('zh-CN'):'暂无索引')+'。仅统计已建立筛查索引的动作；历史数据浏览时补建。';
  const descriptions=[['近 1 小时生成',h.motions+' 个 / '+(h.seconds/60).toFixed(1)+' 分钟'],['其中已审核通过',(h.accepted_seconds/60).toFixed(1)+' 分钟'],['已审核样本通过率',reviewed?(100*accepted/reviewed).toFixed(1)+'%（'+reviewed+' 条）':'等待人工审核'],['近 1 小时文本重复判定',decisions?(100*(data.decisions.duplicate||0)/decisions).toFixed(1)+'%':'暂无判定'],['近 1 小时已记录 Token',h.tokens===null?'未知':h.tokens.toLocaleString()],['用量未知请求',h.unknown_usage??'未知']];
  $('agent-throughput').replaceChildren();for(const [label,value] of descriptions){const p=document.createElement('p');p.textContent=label+'：'+value;$('agent-throughput').append(p);}
  const cost=document.createElement('p');cost.textContent='窗口 Token / 已通过新动作：'+(h.tokens_per_accepted===null?'暂不可算（无通过样本或用量未知）':Math.round(h.tokens_per_accepted).toLocaleString())+'；窗口运营比率，非逐条成本归因';$('agent-throughput').append(cost);
  renderMachineReview(data.machine_review);
  $('agent-priority').replaceChildren();for(const item of data.priority){const button=document.createElement('button');const flags=JSON.parse(item.flags).map(f=>screenLabels[f]||f).join('、');button.textContent=item.motion_id+' · '+item.topic+' · '+(flags||'新运动特征')+(item.nearest?' · 近邻 '+item.nearest:'');button.onclick=async()=>{if(!leave())return;state.items=data.priority.map(p=>({motion_id:p.motion_id,topic:p.topic,state:'done',text:JSON.parse(p.flags).map(f=>screenLabels[f]||f).join('、')||'新运动特征待复核'}));state.after=null;switchPage(false);await selectMotion(item.motion_id,true);};$('agent-priority').append(button);}
  if(!data.priority.length)$('agent-priority').textContent='暂无待复核候选。';
  $('agent-coverage').replaceChildren();for(const [topic,m] of Object.entries(data.coverage)){const block=document.createElement('details'),title=document.createElement('summary');title.textContent=topic+' · '+m.sampled_texts+' 条文本 / '+(m.human_feedback.reviewed||0)+' 条近期审核';block.append(title);for(const [axis,counts] of Object.entries(m.counts)){const p=document.createElement('p');p.textContent=axis+'：'+(Object.entries(counts).map(([k,v])=>k+' '+v).join(' · ')||'未识别显式词语');block.append(p);}const p=document.createElement('p');p.textContent='人工反馈：'+JSON.stringify(m.human_feedback);block.append(p);$('agent-coverage').append(block);}
 }catch(error){if(session===state.session&&!$('monitor').hidden)$('agent-summary').textContent='Agent 指标暂不可用：'+error.message;}
}

function renderMachineReview(data){
 const summary=$('machine-review-summary'),topics=$('machine-review-topics'),queue=$('machine-review-queue');
 if(!summary)return;topics.replaceChildren();queue.replaceChildren();
 if(!data){summary.textContent='机器评分暂未就绪';return;}
 const rt=data.runtime||{},last=Number(rt.completed||0);
 summary.textContent='TMR 文本—动作相似度，不是合格概率。最近批次：'+(last?new Date(last*1000).toLocaleString('zh-CN')+' / '+rt.last_batch+' 条':'尚未完成')+(rt.error?'；评分异常：'+rt.error:'')+'。阈值需通过独立人工抽样验证后才能自动放行。';
 for(const t of data.topics||[]){const p=document.createElement('p'),c=(data.calibration||[]).find(c=>c.topic===t.topic&&c.version===t.version),e=c?JSON.parse(c.evidence):{};
 p.textContent=t.topic+'：已评分 '+t.scored+' / '+t.n+'；机器通过 '+t.auto_passed+' · 待复核 '+(t.n-t.auto_passed-t.quarantined)+' · 标记隔离 '+t.quarantined+'；'+(c?(c.state==='eligible'?'已达到校准要求':'独立验证中')+'，抽查 '+(e.groups||0)+' / '+(e.selected||0)+' 组'+(e.error_upper_95!=null?'，误放率 95% 上界 '+(100*e.error_upper_95).toFixed(1)+'%':''):'待积累人工样本（至少 30 组，含 5 组拒绝；不足时不自动通过）');topics.append(p);}
 const items=data.queue||[];
 for(const item of items){const b=document.createElement('button');b.textContent=item.motion_id+' · '+item.topic+' · '+({random_sample:'随机抽查',validation_sample:'独立校准抽查',flagged_sample:'疑难复核'}[item.selection]||'抽查');b.onclick=async()=>{if(!leave())return;state.items=items.map(p=>({motion_id:p.motion_id,topic:p.topic,state:'done',text:''}));state.after=null;switchPage(false);await selectMotion(item.motion_id,true);};queue.append(b);}
 if(!items.length)queue.textContent='暂无抽查候选。';
}
