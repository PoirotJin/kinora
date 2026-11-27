'use strict';
(()=>{
 const el=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
 const btn=(text,fn)=>{const b=el('button',text);b.type='button';b.onclick=fn;return b;};
 const post=(url,body)=>api(url,{method:'POST',body:JSON.stringify(body)});
 const section=el('section');section.className='admin-only';
 const status=el('p');status.setAttribute('role','status');
 const summary=el('div'),batchList=el('div'),releaseList=el('div'),coverage=el('div');
 let epoch=0,batchCursor='',releaseCursor='',batchNext=null,releaseNext=null;
 const labels={pending:'待提交',submitted:'已提交',queued:'排队',running:'运行',stopping:'停止中',done:'成功',failed:'失败',removed:'已移除',version_mismatch:'版本不一致',source_changed:'源文件变化',cancelled:'已取消',migrated:'已迁移'};
 const states={ready:'准备提交',running:'运行中',paused:'已暂停',waiting:'等待条件',complete:'完成',complete_with_failures:'完成，存在失败项',complete_with_migration:'剩余项已迁移'};
 const releaseStates={queued:'等待打包',waiting_batch:'等待批次完成',processing:'正在打包',complete:'已发布',failed:'发布失败',cancelled:'已取消'};
 const verdicts={normal:'正常',defect:'存在缺陷',uncertain:'无法确定',disputed:'有争议'};
 const opened=new Map();
 function preserveDetails(container){for(const d of container.querySelectorAll('details[data-key]'))opened.set(d.dataset.key,d.open);}
 function details(key,title){const d=el('details');d.dataset.key=key;d.open=opened.get(key)||false;d.append(el('summary',title));return d;}
 async function action(button,url,body,success){const session=state.session;button.disabled=true;try{const result=await post(url,body);if(session!==state.session)return;if(body.action==='migrate_remaining')batchCursor='';await load();if(session===state.session)status.textContent=typeof success==='function'?success(result):success;}catch(e){if(session===state.session)status.textContent=e.message;}finally{button.disabled=false;}}
 const toggle=btn('读取自动增量状态…',()=>{});toggle.disabled=true;
 const incrementalInfo=el('p');
 const form=el('form');form.className='tag-form';
 const name=el('input');name.required=true;name.pattern='[A-Za-z0-9-]{1,80}';name.placeholder='发布版本，例如 g1-v2';name.setAttribute('aria-label','发布版本');
 const batch=el('select');batch.required=true;batch.setAttribute('aria-label','发布批次');
 const base=el('select');base.setAttribute('aria-label','增量基线');
 const submit=el('button','创建不可覆盖的数据集版本');submit.type='submit';form.append(name,batch,base,submit);
 function options(select,items,placeholder){if(document.activeElement===select)return;const previous=select.value;const old=Array.from(select.options).find(x=>x.value===previous);const next=items.map(x=>new Option(x.text,x.value));if(placeholder)next.unshift(new Option(placeholder,''));if(previous&&old&&!items.some(x=>x.value===previous))next.push(new Option(old.textContent,previous));select.replaceChildren(...next);if(previous)select.value=previous;}
 form.onsubmit=async e=>{e.preventDefault();if(submit.disabled)return;if(!batch.value){status.textContent='请选择自己拥有的批次后再发布。';return;}const session=state.session;const body={id:name.value,batch:batch.value,base:base.value};for(const field of [name,batch,base,submit])field.disabled=true;try{const result=await post('/api/operations/releases',body);if(session!==state.session)return;name.value='';releaseCursor='';await load();if(session===state.session)status.textContent=`版本 ${result.id} 已提交。若批次未完成，将显示等待原因；可取消等待请求。`;}catch(err){if(session===state.session)status.textContent=err.message;}finally{if(session===state.session)for(const field of [name,batch,base,submit])field.disabled=false;}};
 const bLatest=btn('最新批次',()=>{batchCursor='';load();});const bOlder=btn('更早批次',()=>{if(batchNext){batchCursor=batchNext;load();}});
 const rLatest=btn('最新发布',()=>{releaseCursor='';load();});const rOlder=btn('更早发布',()=>{if(releaseNext){releaseCursor=releaseNext;load();}});
 const pages=el('p');pages.append(bLatest,bOlder);const releasePages=el('p');releasePages.append(rLatest,rOlder);
 async function load(){if(state.user?.role!=='admin')return;const session=state.session,e=++epoch;status.textContent='正在读取批次与审核记录…';try{
  const query=new URLSearchParams();if(batchCursor)query.set('batches_cursor',batchCursor);if(releaseCursor)query.set('releases_cursor',releaseCursor);
  const data=await api('/api/operations'+(query.size?'?'+query.toString():''));if(session!==state.session||e!==epoch)return;
  status.textContent='已更新 '+new Date().toLocaleTimeString();summary.replaceChildren(el('p',`重定向槽位上限 ${data.slot_limit}；工作进程占用 ${data.occupied_slots.length}；运行作业 ${data.active.length}。空闲不代表服务异常。`));
  for(const error of data.maintenance_errors||[])summary.append(el('p',`维护提示：${error.component} · ${error.error}`));
  toggle.textContent=data.incremental.enabled?'停止发现新动作':'启用新动作发现';toggle.disabled=false;
  toggle.onclick=()=>action(toggle,'/api/operations/incremental',{enabled:!data.incremental.enabled},data.incremental.enabled?'已停止发现新动作；已有批次继续提交。需暂停已有批次时，请使用该批次的“暂停提交”。':'已启用新动作发现。执行账户和参数保持原配置。');
  incrementalInfo.textContent=`执行账户：${data.incremental.owner||'首次启用时使用当前管理员'}。只控制是否创建后续 G1 23DoF 批次；已有批次继续运行。历史尝试按执行账户保留，失败项需显式重试。`;
  preserveDetails(batchList);batchList.replaceChildren();
  if(!data.batches.length)batchList.append(el('p','此页没有批次。首次升级后的历史索引会由维护任务分批建立。'));
  for(const b of data.batches){const group=el('article');group.className='operations-batch';
   group.append(el('h3',b.id),el('p',`${states[b.state]||b.state||'待同步'} · 执行账户 ${b.owner}`),el('p',Object.entries(b.counts).map(([k,v])=>(labels[k]||k)+' '+v).join(' · ')),el('p',b.message||''));
   group.append(el('p',`历史平均吞吐 ${b.throughput_per_hour?b.throughput_per_hour.toFixed(1)+' 个/小时':'待测'} · 运行中剩余估时 ${b.eta_seconds?(b.eta_seconds/3600).toFixed(1)+' 小时':'—'}（暂停与等待期间不估时）`));
   if(b.remaining){const pause=btn(b.operator_paused?'恢复提交':'暂停提交',()=>action(pause,'/api/operations/batch',{batch:b.id,action:b.operator_paused?'resume':'pause'},b.operator_paused?'已恢复提交；版本保护和容量检查仍生效。':'已暂停提交；排队和正在运行的作业继续完成。'));group.append(pause);}
   if(b.counts.failed){const retry=btn('重试失败项（最多 100 条）',()=>action(retry,'/api/operations/batch',{batch:b.id,action:'retry_failed'},'已请求重试失败项；旧作业和失败证据保留。'));group.append(retry);}
   if(b.blocked_reason==='version')group.append(el('p','这是版本保护，不是用户暂停。恢复原版本环境后可继续；也可以保留旧结果，将未完成项迁入当前版本。'));
   if(b.owner===state.user.username&&!b.migration_target&&(b.remaining||b.counts.failed||b.counts.version_mismatch||b.counts.source_changed||b.counts.cancelled)){
    const migrate=details('migrate:'+b.id,'迁移未完成项到当前算法的新批次');migrate.append(el('p','使用当前算法与默认参数重新计算未完成/失败项。原清单、成功结果和旧作业保留。请先暂停提交并等待排队及运行作业结束。'));
    const button=btn('确认创建新版本批次',()=>action(button,'/api/operations/batch',{batch:b.id,action:'migrate_remaining'},r=>`已迁移 ${r.total} 项到 ${r.batch}，原结果保留。`));button.disabled=['submitted','queued','running','stopping'].some(k=>b.counts[k]);migrate.append(button);group.append(migrate);
   }
   if(b.migration_target)group.append(el('p','后续批次：'+b.migration_target));
   if(b.failures.length){const d=details('failures:'+b.id,'失败与异常明细（前 30 条）');for(const f of b.failures)d.append(el('p',`${f.motion_id} · ${f.message}`));group.append(d);}batchList.append(group);
  }
  batchNext=data.batches_next;releaseNext=data.releases_next;bOlder.disabled=!batchNext;rOlder.disabled=!releaseNext;bLatest.disabled=!batchCursor;rLatest.disabled=!releaseCursor;
  options(batch,data.batches.filter(b=>b.owner===state.user.username).map(b=>({value:b.id,text:`${b.id} · 成功 ${b.counts.done||0} / 总计 ${b.total}`})),'选择自己的发布批次');
  options(base,data.releases.filter(r=>r.status==='complete'&&(!r.owner||r.owner===state.user.username)).map(r=>({value:r.id,text:'相对 '+r.id+' 的新增文件'})),'完整版本（不排除已有结果）');
  preserveDetails(coverage);coverage.replaceChildren();const cov=details('coverage','实测动作覆盖与审核校准');const table=el('table');const head=el('tr');for(const t of ['主题','已测动作','近原地','中速移动','快速移动'])head.append(el('th',t));table.append(head);for(const r of data.coverage){const tr=el('tr');for(const v of [r.topic,r.sampled,r.stationary,r.moderate,r.fast])tr.append(el('td',String(v)));table.append(tr);}cov.append(table,el('p','有限根轨迹统计；相似候选需人工比较，不作为自动删除或通过依据。'),el('p',data.calibration.policy));for(const r of data.calibration.groups)cov.append(el('p',`${r.topic} / ${r.check_name} / ${verdicts[r.verdict]||r.verdict}：${r.n}；可信样本 ${r.trusted||0}，匹配当前检测 ${r.scored}，其中机器标记 ${r.machine_flagged}`));coverage.append(cov);
  releaseList.replaceChildren();if(!data.releases.length)releaseList.append(el('p','此页暂无我的发布版本。仅自己的批次可发布；等待中的请求可以取消。'));
  for(const r of data.releases){const line=el('p',`${r.id} · ${releaseStates[r.status]||r.status} · ${r.batch||''} ${r.error||''}`);if(r.status==='complete'){const a=el('a','下载数据与清单');a.href='/api/operations/release?id='+encodeURIComponent(r.id);line.append(' ',a);}if(['queued','waiting_batch'].includes(r.status)){const cancel=btn('取消此次发布',()=>action(cancel,'/api/operations/releases/cancel',{id:r.id},'已取消发布请求；批次和结果保留。'));line.append(cancel);}releaseList.append(line);}
 }catch(err){if(session===state.session&&e===epoch)status.textContent=err.message;}}
 section.append(el('h2','批次、增量生产与版本发布'),btn('刷新批次状态',load),status,summary,toggle,incrementalInfo,batchList,pages,coverage,el('h3','创建数据集版本'),el('p','请选择自己的批次。未完成批次的发布会等待，并显示等待原因；不会阻塞其他就绪版本。'),form,releaseList,releasePages);$('tasks').append(section);
 const previousReset=window.resetResearch;window.resetResearch=()=>{previousReset?.();epoch++;batchCursor=releaseCursor='';batchNext=releaseNext=null;opened.clear();for(const box of [summary,batchList,releaseList,coverage])box.replaceChildren();name.value='';batch.replaceChildren();base.replaceChildren();for(const field of [name,batch,base,submit])field.disabled=false;toggle.disabled=true;status.textContent='';};
 const observer=new MutationObserver(()=>{if(!$('tasks').hidden)load();});observer.observe($('tasks'),{attributes:true,attributeFilter:['hidden']});
 setInterval(()=>{if(!$('tasks').hidden&&!document.hidden)load();},30000);
 if(!$('tasks').hidden)load();
})();
