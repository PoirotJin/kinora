'use strict';
(()=>{
 let current=null,historyAfter=null,version=0,busy=false,vocabulary=[],vocabularyRequest=0,historyRequest=0;
 const el=(name,text)=>{const node=document.createElement(name);if(text!==undefined)node.textContent=text;return node;};
 const date=value=>new Date(value*1000).toLocaleString('zh-CN');
 function message(text,error=false){$('tag-message').textContent=text;$('tag-message').classList.toggle('error',error);}
 function suggestions(group=''){
  $('tag-group-options').replaceChildren(...[...new Set(vocabulary.map(x=>x.group))].map(x=>{const o=el('option');o.value=x;return o;}));
  $('tag-name-options').replaceChildren(...[...new Set(vocabulary.filter(x=>!group||x.group===group).map(x=>x.tag))].map(x=>{const o=el('option');o.value=x;return o;}));
 }
 window.refreshTagVocabulary=async()=>{
  const account=state.session,request=++vocabularyRequest;
  try{
   let after=0;const items=[];
   do{const result=await api('/api/tags?limit=200&after='+after);if(state.session!==account||request!==vocabularyRequest)return;items.push(...result.items);after=result.next_after;}while(after!==null);
   vocabulary=items;suggestions();
  }catch(error){if(state.session===account&&request===vocabularyRequest)notice('标签建议加载失败：'+error.message,true);}
 };
 window.resetMotionTags=()=>{version++;vocabularyRequest++;historyRequest++;busy=false;current=null;vocabulary=[];$('tag-save').disabled=false;$('tag-history-more').disabled=false;$('tag-dialog').close();$('tag-current').replaceChildren();$('tag-history').replaceChildren();suggestions();$('tag-edit-form').reset();$('tag-filter-form').reset();};
 function history(result,append=false){
  if(!append)$('tag-history').replaceChildren();
  for(const e of result.events)$('tag-history').append(el('p',`${date(e.at)} · ${e.username} · ${e.action==='add'?'添加':'撤回'} · ${e.group} / ${e.tag}`));
  historyAfter=result.next_after;$('tag-history-more').hidden=historyAfter===null;
 }
 function render(result){
  $('tag-current').replaceChildren();
  if(!result.tags.length)$('tag-current').append(el('p','还没有标注。'));
  for(const t of result.tags){
   const row=el('div');row.className='tag-assignment';row.append(el('span',`${t.group} / ${t.tag} · ${t.username} · ${date(t.created_at)}`));
   if(t.username===state.user?.username){const remove=el('button','撤回我的标注');remove.type='button';remove.disabled=busy;remove.onclick=()=>save({group:t.group,tag:t.tag,action:'remove'});row.append(remove);}
   $('tag-current').append(row);
  }
  history(result);
 }
 async function save(value){
  if(!current||busy)return;
  // A write supersedes every earlier read, including the dialog's initial GET.
  const mid=current,seq=++version;historyRequest++;busy=true;$('tag-save').disabled=true;
  $('tag-current').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
   const result=await api('/api/motions/'+mid+'/tags',{method:'POST',body:JSON.stringify(value)});
   if(seq!==version)return;
   render(result);message(result.changed?'标注已保存，用户名和时间已记录。':'标注状态未改变。');
   for(const container of ['gallery-grid','personal-motions'])for(const card of $(container).children)if(card.dataset.motion===mid)renderMotionTags(card,{motion_id:mid,tags:result.tags});
   await refreshTagVocabulary();
  }catch(error){if(seq===version)message(error.message,true);}
  finally{if(seq===version){busy=false;$('tag-save').disabled=false;$('tag-history-more').disabled=false;$('tag-current').querySelectorAll('button').forEach(b=>b.disabled=false);}}
 }
 window.openMotionTags=async mid=>{
  if(busy)return;
  current=mid;const seq=++version;$('tag-save').disabled=false;$('tag-history-more').disabled=false;$('tag-dialog-title').textContent=mid+' · 动作标签';$('tag-current').replaceChildren();$('tag-history').replaceChildren();$('tag-history-more').hidden=true;$('tag-edit-form').reset();message('正在加载…');$('tag-dialog').showModal();
  try{const result=await api('/api/motions/'+mid+'/tags');if(seq!==version)return;render(result);message('');}catch(error){if(seq===version)message(error.message,true);}
 };
 window.renderMotionTags=(card,item)=>{
  card.querySelector('.motion-tags')?.remove();card.querySelector('.motion-tag-action')?.remove();
  const wrap=el('div');wrap.className='motion-tags';wrap.onclick=e=>e.stopPropagation();
  const grouped=new Map();
  for(const t of item.tags||[]){const key=t.group+' / '+t.tag;if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(t.username);}
  for(const [label,users] of grouped){const chip=el('span',label+' · '+users.join('、'));chip.className='tag-chip';wrap.append(chip);}
  const button=el('button','标签 / 标注记录');button.className='motion-tag-action';button.type='button';button.onclick=e=>{e.stopPropagation();openMotionTags(item.motion_id);};card.append(wrap);(card.querySelector('.production-actions')||wrap).append(button);
 };
 $('tag-edit-form').onsubmit=e=>{e.preventDefault();save({group:$('tag-edit-group').value,tag:$('tag-edit-name').value,action:'add'});};
 $('tag-close').onclick=()=>{if(!busy){version++;current=null;$('tag-dialog').close();}};
 $('tag-dialog').addEventListener('cancel',e=>{if(busy)e.preventDefault();else{version++;current=null;}});
 $('tag-history-more').onclick=async()=>{
  if(!current||historyAfter===null||busy)return;const seq=version,request=++historyRequest;$('tag-history-more').disabled=true;
  try{const result=await api('/api/motions/'+current+'/tags?after='+historyAfter);if(seq===version&&request===historyRequest)history(result,true);}catch(error){if(seq===version&&request===historyRequest)message(error.message,true);}finally{if(seq===version&&request===historyRequest)$('tag-history-more').disabled=false;}
 };
 $('tag-filter-form').onsubmit=e=>{e.preventDefault();loadGallery();};
 $('tag-filter-reset').onclick=()=>{$('tag-filter-form').reset();loadGallery();};
 for(const id of ['tag-edit-group','tag-filter-group'])$(id).oninput=()=>suggestions($(id).value);
})();
