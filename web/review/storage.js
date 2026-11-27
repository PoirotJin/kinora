'use strict';
const storageState={busy:false,after:0,next:null,policyLoaded:false,session:null,request:0};
const storageSize=n=>n==null?'—':n>=1e9?(n/1e9).toFixed(2)+' GB':n>=1e6?(n/1e6).toFixed(2)+' MB':(n/1e3).toFixed(1)+' KB';
function showStorage(){
 if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);
 state.playing=false;state.request++;
 closeRetarget();
 for(const id of ['production','workspace','gallery','monitor','tasks'])$(id).hidden=true;
 $('storage').hidden=false;
 for(const id of ['show-home','show-review','show-gallery','show-monitor','show-tasks','show-storage'])$(id).setAttribute('aria-pressed',String(id==='show-storage'));
 refreshStorage(true);
}
$('show-storage').onclick=showStorage;
async function storageAction(path,value,button){
 if(button?.disabled)return;if(button)button.disabled=true;
 const session=state.session;
 try{await api('/api/storage/'+path,{method:'POST',body:JSON.stringify(value),signal:AbortSignal.timeout(10000)});if(session!==state.session)return;notice(path==='settings'?'归档策略已保存。':path==='retry'?'已创建新的打包尝试；原失败记录保留。':'已提交后台打包任务，完成后可下载。');await refreshStorage(true);}
 catch(error){if(session===state.session)notice(error.message,true);}
 finally{if(button&&session===state.session)button.disabled=false;}
}
$('storage-save-policy').onclick=()=>storageAction('settings',{days:Number($('storage-days').value)});
$('storage-refresh').onclick=()=>refreshStorage(true);
$('storage-first').onclick=()=>{storageState.after=0;refreshStorage(true);};
$('storage-next').onclick=()=>{if(storageState.next!==null){storageState.after=storageState.next;refreshStorage(true);}};
async function refreshStorage(manual=false){
 if($('storage').hidden||!state.user||(document.hidden&&!manual))return;
 const session=state.session;
 if(storageState.session!==session){
  Object.assign(storageState,{session,busy:false,after:0,next:null,policyLoaded:false});
  $('storage-shards').replaceChildren();$('storage-jobs').replaceChildren();$('storage-summary').textContent='正在读取…';
 }
 if(storageState.busy&&!manual)return;
 const request=++storageState.request;storageState.busy=true;
 try{
  const data=await api('/api/storage?after='+storageState.after,{signal:AbortSignal.timeout(8000)});
  if(session!==state.session||request!==storageState.request||!state.user||$('storage').hidden)return;
  if(!storageState.policyLoaded){$('storage-days').value=data.days;storageState.policyLoaded=true;}
  const u=data.usage;const c=u?.cold_tier;
  $('storage-summary').textContent=data.error||(!u?'正在统计…':`${c?'系统盘热层':'已用'} ${storageSize(u.total)} / ${storageSize(u.limit)}${c?' · 数据盘冷层 '+storageSize(c.total)+' / '+storageSize(c.limit):''} · 热分包 ${storageSize(u.hot)} · 冷分包 ${storageSize((u.cold||0)+(c?.cold||0))} · 导出副本 ${storageSize(u.exports)} · ${data.total_shards} 个动作分包 · 统计于 ${new Date(u.sampled_at*1000).toLocaleTimeString('zh-CN')}`);
  $('storage-capacity').max=u?(u.limit+(c?.limit||0))/1e9:1600;
  $('storage-capacity').value=u?(u.total+(c?.total||0))/1e9:0;
  if(u&&(Date.now()/1000-u.sampled_at>90||(c&&(c.stale||!c.ready))))$('storage-summary').textContent+=' · 统计已过期';
  $('storage-shards').replaceChildren();
  for(const item of data.items||[]){
   const tr=document.createElement('tr');
   for(const value of [item.name+' / '+String(item.shard).padStart(6,'0'),item.tier==='cold'?'冷 · 按需解压':'热 · 直接读取',storageSize(item.raw_bytes),storageSize(item.bytes)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}
   const td=document.createElement('td');
   if(item.tier==='cold'){const a=document.createElement('a');a.textContent='下载压缩包';a.href='/api/storage/archive?'+new URLSearchParams({topic:item.topic,shard:item.shard});a.download='';td.append(a);}
   else if(state.user.role==='admin'){for(const [kind,title] of [['export','打包导出'],['archive','冷归档']]){const button=document.createElement('button');button.textContent=title;button.disabled=(data.jobs||[]).some(j=>j.status==='running');button.onclick=()=>storageAction('package',{topic:item.topic,shard:item.shard,kind});td.append(button);}}
   else td.textContent='热数据可在动作画廊查看；打包请联系管理员';
   tr.append(td);$('storage-shards').append(tr);
  }
  storageState.next=data.next_after;$('storage-next').disabled=data.next_after===null;
  $('storage-jobs').replaceChildren();
  if(!data.jobs?.length)$('storage-jobs').textContent='暂无打包任务。';
  for(const job of data.jobs||[]){
   const p=document.createElement('p');p.textContent=`${new Date(job.created*1000).toLocaleString('zh-CN')} · ${job.topic} / ${job.shard} · ${job.kind==='archive'?'冷归档':'导出'} · ${{running:'处理中',completed:'完成',failed:'失败'}[job.status]} · ${job.message} `;
   if(job.download){const a=document.createElement('a');a.href='/api/storage/download?id='+encodeURIComponent(job.id);a.textContent='下载压缩包';a.download='';p.append(a);}
   if(job.retryable&&state.user.role==='admin'){
    const button=document.createElement('button');button.type='button';button.textContent='重试';button.disabled=(data.jobs||[]).some(j=>j.status==='running');
    button.onclick=()=>storageAction('retry',{job_id:job.id},button);p.append(button);
   }
   $('storage-jobs').append(p);
  }
 }catch(error){if(session===state.session&&request===storageState.request)$('storage-summary').textContent='读取失败：'+error.message;}
 finally{if(session===state.session&&request===storageState.request)storageState.busy=false;}
}
setInterval(()=>refreshStorage(),5000);
