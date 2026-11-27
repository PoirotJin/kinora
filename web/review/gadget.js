'use strict';
let gadgetEpoch=0;
window.closeGadget=()=>{gadgetEpoch++;$('gadget').hidden=true;$('show-gadget').setAttribute('aria-pressed','false');$('gadget-secret').value='';$('gadget-secret-box').hidden=true;$('gadget-keys').replaceChildren();$('gadget-notice').textContent='';};
async function refreshGadget(){
 const epoch=gadgetEpoch;const data=await api('/api/gadget/keys');
 if(epoch!==gadgetEpoch||$('gadget').hidden)return;
 $('gadget-keys').replaceChildren();
 for(const key of data.keys){
  const row=document.createElement('article');row.className='gadget-key-row';
  const current=key.status||(key.revoked?'revoked':key.expires*1000<Date.now()?'expired':'active');
  const states={active:'有效',revoked:'已撤销',expired:'已过期',password_changed:'密码已变更，此密钥已失效',password_change_required:'需先修改临时密码',account_unavailable:'账户当前不可用'};
  const label=document.createElement('span');label.textContent=key.name+' · '+JSON.parse(key.scopes).join(', ')+' · '+(states[current]||current)+' · 到期 '+new Date(key.expires*1000).toLocaleString();
  const button=document.createElement('button');button.textContent=current==='active'?'撤销':states[current]||'不可用';button.disabled=current!=='active';
  button.onclick=async()=>{button.disabled=true;try{await api('/api/gadget/keys/revoke',{method:'POST',body:JSON.stringify({id:key.id})});if(epoch===gadgetEpoch)await refreshGadget();}catch(error){if(epoch===gadgetEpoch)$('gadget-notice').textContent=error.message;button.disabled=false;}};
  row.append(label,button);$('gadget-keys').append(row);
 }
 if(!data.keys.length)$('gadget-keys').textContent='还没有创建密钥。';
}
$('show-gadget').onclick=async()=>{
 if(state.saving||!leave())return;if(state.dirty)fillReview(state.current?.review);
 closeRetarget();state.playing=false;state.request++;
 for(const id of ['production','workspace','gallery','monitor','tasks','storage','retarget'])$(id).hidden=true;
 for(const button of $('page-nav').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.id==='show-gadget'));
 $('gadget').hidden=false;$('gadget-server').textContent=location.origin;
 try{await refreshGadget();}catch(error){$('gadget-notice').textContent=error.message;}
};
$('page-nav').addEventListener('click',event=>{if(event.target.closest('button')?.id!=='show-gadget')window.closeGadget();},true);
$('gadget-key-form').onsubmit=async event=>{
 event.preventDefault();const epoch=gadgetEpoch;$('gadget-create').disabled=true;$('gadget-notice').textContent='';
 try{
  const scopes=[...document.querySelectorAll('input[name="gadget-scope"]:checked')].map(x=>x.value);
  const result=await api('/api/gadget/keys',{method:'POST',body:JSON.stringify({name:$('gadget-key-name').value,days:Number($('gadget-key-days').value),scopes})});
  if(epoch!==gadgetEpoch)return;$('gadget-secret').value=result.key;$('gadget-secret-box').hidden=false;await refreshGadget();
 }catch(error){if(epoch===gadgetEpoch)$('gadget-notice').textContent=error.message;}
 finally{$('gadget-create').disabled=false;}
};
$('gadget-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('gadget-secret').value);$('gadget-notice').textContent='已复制，请存入本地环境变量。';}catch(error){$('gadget-notice').textContent='请手动选择并复制密钥。';}};
$('gadget-dismiss').onclick=()=>{$('gadget-secret').value='';$('gadget-secret-box').hidden=true;};
