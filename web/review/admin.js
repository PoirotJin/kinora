'use strict';
const adminView={peopleEpoch:0,trashEpoch:0,after:null,busy:false,ids:new Set()};
const trashSearch=document.createElement('form'),trashInput=document.createElement('input'),trashSubmit=document.createElement('button'),trashAll=document.createElement('button'),trashStatus=document.createElement('p'),trashMore=document.createElement('button');
trashSearch.id='trash-search-form';trashInput.id='trash-search';trashInput.placeholder='完整动作编号，例如 m00000001';trashInput.setAttribute('aria-label','按完整动作编号搜索回收站');trashSubmit.textContent='搜索';trashSubmit.type='submit';trashAll.textContent='全部';trashAll.type='button';trashStatus.id='trash-status';trashStatus.setAttribute('role','status');trashMore.id='trash-more';trashMore.textContent='加载更早';trashMore.type='button';trashMore.hidden=true;
trashSearch.append(trashInput,trashSubmit,trashAll);$('trash-items').before(trashSearch,trashStatus);$('trash-items').after(trashMore);
const adminActive=(account,session)=>state.user===account&&state.session===session&&account?.role==='admin';
function invalidateTrash(message=''){
 adminView.trashEpoch++;adminView.after=null;adminView.busy=false;adminView.ids.clear();$('trash-items').replaceChildren();trashStatus.textContent=message;trashMore.hidden=true;trashMore.disabled=false;
}
window.resetAdmin=()=>{
 adminView.peopleEpoch++;invalidateTrash();trashInput.value='';$('admin-users').replaceChildren();$('new-user-secret').textContent='';$('create-user-form').reset();$('create-user-form').querySelector('button').disabled=false;
};
async function loadTrash(append=false){
 const account=state.user,session=state.session;if(!adminActive(account,session))return;
 if(append&&(adminView.busy||!adminView.after))return;
 const q=trashInput.value.trim();if(!append)invalidateTrash();
 const epoch=++adminView.trashEpoch,after=append?adminView.after:null;adminView.busy=true;trashMore.disabled=true;trashStatus.textContent='正在加载回收站…';
 const active=()=>adminActive(account,session)&&epoch===adminView.trashEpoch;
 try{
  const params=new URLSearchParams();if(q)params.set('q',q);if(after)params.set('after',after);
  const result=await api('/api/removed'+(params.size?'?'+params.toString():''));if(!active())return;
  for(const item of result.items){
   if(adminView.ids.has(item.motion_id))continue;adminView.ids.add(item.motion_id);
   const p=document.createElement('p'),button=document.createElement('button');p.textContent=item.motion_id+' · '+item.username+' · '+new Date(item.removed_at*1000).toLocaleString('zh-CN')+' '+item.reason;button.textContent='恢复 '+item.motion_id;
   button.onclick=async()=>{
    if(!adminActive(account,session)||button.disabled)return;button.disabled=true;
    try{await api('/api/motions/'+item.motion_id+'/restore',{method:'POST',body:'{}'});if(!adminActive(account,session))return;await loadTrash();if(adminActive(account,session))notice(item.motion_id+' 已恢复');}
    catch(error){if(adminActive(account,session))notice(error.message,true);}
    finally{if(adminActive(account,session))button.disabled=false;}
   };p.append(button);$('trash-items').append(p);
  }
  adminView.after=result.next_after||null;trashMore.hidden=!adminView.after;
  trashStatus.textContent=adminView.ids.size?'已载入 '+adminView.ids.size+' 个已删除动作'+(adminView.after?'，可继续加载更早记录。':'，已全部载入。'):(q?'未找到这个动作；请核对完整编号或查看全部。':'回收站为空。');
 }catch(error){if(active()){trashStatus.textContent='加载失败：'+error.message;notice(error.message,true);}}
 finally{if(active()){adminView.busy=false;trashMore.disabled=false;}}
}
async function loadAdminUsers(){
 const account=state.user,session=state.session;if(!adminActive(account,session))return;
 const epoch=++adminView.peopleEpoch,active=()=>adminActive(account,session)&&epoch===adminView.peopleEpoch;
 try{
  const people=await api('/api/users');if(!active())return;$('admin-users').replaceChildren();
  for(const person of people.users){
   const wrap=document.createElement('div');wrap.className='admin-user';const p=document.createElement('p');
   p.textContent=person.username+' · '+(person.role==='admin'?'管理员':'普通用户')+(person.must_change?' · 首次登录需修改密码':'')+(person.disabled?' · 已停用':'')+' · '+person.sessions+' 个会话';wrap.append(p);
   const actions=document.createElement('div');
   const act=async(path,label,extra)=>{
    if(!adminActive(account,session)||!confirm(label+' '+person.username+'？'))return;
    try{const result=await api(path,{method:'POST',body:'{}'});if(!adminActive(account,session))return;if(result.temporary_password)$('new-user-secret').textContent='用户名：'+result.username+'\n临时密码：'+result.temporary_password+'\n请私下交给该用户；首次登录必须修改密码。';await loadAdminUsers();if(adminActive(account,session))notice(extra);}
    catch(error){if(adminActive(account,session))notice(error.message,true);}
   };
   if(person.username!=='jfc'){const toggle=document.createElement('button');toggle.textContent=person.disabled?'启用':'停用';toggle.onclick=()=>act('/api/users/'+person.username+'/'+(person.disabled?'enable':'disable'),person.disabled?'启用':'停用',person.disabled?'账户已启用':'账户已停用并踢出会话');actions.append(toggle);}
   const kick=document.createElement('button');kick.textContent='踢出全部会话';kick.onclick=()=>act('/api/users/'+person.username+'/kick','踢出全部会话','已踢出会话');actions.append(kick);
   const reset=document.createElement('button');reset.textContent='重置密码';reset.onclick=()=>act('/api/users/'+person.username+'/reset','重置密码','已重置为临时密码');actions.append(reset);
   wrap.append(actions);$('admin-users').append(wrap);
  }
 }catch(error){if(active())notice(error.message,true);}
}
async function refreshAdmin(){await Promise.all([loadAdminUsers(),loadTrash()]);}
$('trash-refresh').onclick=()=>loadTrash();trashMore.onclick=()=>loadTrash(true);
trashSearch.onsubmit=event=>{event.preventDefault();return loadTrash();};
trashInput.oninput=()=>invalidateTrash('搜索条件已改变，请点击搜索。');trashAll.onclick=()=>{trashInput.value='';return loadTrash();};
$('create-user-form').onsubmit=async event=>{
 event.preventDefault();const account=state.user,session=state.session;if(!adminActive(account,session))return;
 const button=event.target.querySelector('button');button.disabled=true;
 try{const result=await api('/api/users',{method:'POST',body:JSON.stringify({username:$('new-username').value.trim()})});if(!adminActive(account,session))return;$('new-user-secret').textContent='用户名：'+result.username+'\n临时密码：'+result.temporary_password+'\n请私下交给该用户；首次登录必须修改密码。此密码只显示本次，刷新后隐藏。';$('create-user-form').reset();await loadAdminUsers();}
 catch(error){if(adminActive(account,session))notice(error.message,true);}finally{if(adminActive(account,session))button.disabled=false;}
};
$('remove-motion').onclick=async()=>{
 const account=state.user,session=state.session;if(!adminActive(account,session)||!state.current||state.saving||!leave())return;
 const id=state.current.motion_id;if(!confirm('将 '+id+' 移入回收站？此动作将从画廊隐藏，可在生产首页恢复。'))return;
 $('remove-motion').disabled=true;
 try{await api('/api/motions/'+id+'/remove',{method:'POST',body:JSON.stringify({reason:'管理员在动作页面移入回收站'})});if(!adminActive(account,session))return;clearClip();await loadList();if(adminActive(account,session))notice(id+' 已移入回收站，原始分包保留。');}
 catch(error){if(adminActive(account,session))notice(error.message,true);}finally{if(adminActive(account,session))$('remove-motion').disabled=false;}
};
