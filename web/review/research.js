'use strict';
(()=>{
 let epoch=0,searchEpoch=0,collectionsEpoch=0,searchKey=null,searchItems=[],after=null,selected=new Map(),report=null,replayBody=null,comparison=null,playing=true,seconds=0,last=0;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
 const button=(text,fn)=>{const b=el('button',text);b.type='button';b.onclick=e=>{e.stopPropagation();fn();};return b;};
 const uid=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
 const labels={self_collision_proxy:'肢体体积相交代理检查',stance_slide:'接地滑移',root_gait_consistency:'步态与根轨迹一致性',done:'已完成',completed:'已完成',queued:'排队中',running:'运行中',failed:'失败',cancelled:'已取消',accepted:'通过',rejected:'不通过',uncertain:'待复核',pending:'未审核',incomplete:'检查不完整',issues:'发现问题',screened:'已完成已支持的检查',not_checked:'未检查',passed:'通过',kinematic_pass:'离线运动学检查通过',root_quaternion:'根节点旋转有效性',joint_limits:'机器人关节限位',wrist_neutral:'腕部中立约束',ground_surface:'机器人表面穿地检查',finite:'数值有效性',floor_joint_proxy:'脚部关节穿地代理检查',foot_slide_proxy:'接地脚滑动代理检查',root_acceleration_proxy:'根节点加速度',robot_joint_limits:'机器人关节限位',surface_collision:'表面碰撞',dynamics:'动力学'};
 const human=v=>labels[v]||v||'未知';
 const valid=(s,e)=>s===state.session&&e===epoch&&state.user;
 const message=text=>{$('research-message').textContent=text;notice(text);};
 window.resetResearch=()=>{epoch++;searchEpoch++;collectionsEpoch++;searchKey=null;searchItems=[];after=null;selected.clear();report=null;replayBody=null;comparison=null;for(const id of ['research-report','research-comparison'])$(id).close();$('research-results').replaceChildren();$('research-report-body').replaceChildren();$('research-filter').reset();$('research-replay-submit').disabled=false;$('research-more').disabled=false;$('research-report-message').textContent='';$('research-message').textContent='';$('research-replay-form').reset();$('research-collection').replaceChildren(new Option('全部',''),new Option('收藏','favorites'));};
 async function collect(mid){
  const session=state.session;
  const id=prompt('集合 ID（英文、数字、下划线；favorites 表示收藏）','favorites');if(!id)return;
  try{await api('/api/library/collections',{method:'POST',body:JSON.stringify({id,motion_id:mid,action:'add'})});if(session===state.session)message('已保存到私人集合 '+id);}catch(e){if(session===state.session)message(e.message);}
 }
 window.motionResearchActions=(container,item)=>{
  const controls=el('div');controls.className='production-actions';controls.append(button('质量 / 来源',()=>showReport(item.motion_id)),button('收藏 / 加入集合',()=>collect(item.motion_id)));container.append(controls);
 };
 window.retargetResearchActions=(container,job)=>{
  container.append(button('质量 / 执行记录',()=>showReport(job.id,true)));
  if(['done','failed','cancelled'].includes(job.status))container.append(button('以新 ID 重试 / 比较',async()=>{
   const execution=typeof job.execution==='string'?JSON.parse(job.execution):job.execution||{};
   if(await showRetarget(job.motion_id,{robot:job.robot,parameters:execution.parameters||{},parentJob:job.id,forceNew:true}))$('retarget-notice').textContent='已载入原参数并选择重新计算新版本；可修改后提交，原作业保持不变。';
  }));
 };
 async function showReport(id,isRetarget=false){
  window.preserveMotionCalibration?.();const session=state.session,seq=++epoch;report=null;replayBody=null;$('research-replay').hidden=true;$('research-report-body').replaceChildren();$('research-report-message').textContent='正在加载检查与来源…';$('research-report').showModal();
  try{
   const data=await api('/api/research/'+(isRetarget?'retarget':'report')+'?id='+encodeURIComponent(id));if(!valid(session,seq))return;
   report=data;const q=data.quality||{};const body=$('research-report-body');
   if(!isRetarget){
    const assessmentBox=el('section');body.append(assessmentBox);
    const drawAssessment=assessment=>{assessmentBox.replaceChildren();assessmentBox.append(el('h3','分项评分与待复核原因'),el('p','机器评估：'+(assessment.state==='needs_review'?'待复核':assessment.state==='human_defect'?'人工标注明确缺陷':'专项人工标注已完成')+'；已有最终人审结论保留。'),el('p',assessment.score_note));
     const scores=el('table'),header=el('tr');for(const title of ['维度','检查分 / 相似度','机器结论','人工意见'])header.append(el('th',title));scores.append(header);
     const states={pass:'代理检查通过',flagged:'可疑缺陷',uncertain:'待校准',unknown:'未检查'},opinions={normal:'正常 / 符合',defect:'缺陷 / 不符合',uncertain:'无法确定',disputed:'有争议'};
     for(const item of assessment.dimensions){const row=el('tr');row.append(el('th',item.title),el('td',item.score===null?'—':String(item.score)),el('td',states[item.state]||item.state),el('td',opinions[item.human_verdict]||'未标注'));scores.append(row);}assessmentBox.append(scores);for(const reason of assessment.reasons)assessmentBox.append(el('p',reason));
    };
    window.refreshMotionAssessment=async mid=>{if(mid!==id||!valid(session,seq))return;try{const assessment=await api('/api/research/assessment?id='+encodeURIComponent(id));if(valid(session,seq))drawAssessment(assessment);}catch(e){if(valid(session,seq))assessmentBox.append(el('p','分项评分刷新失败：'+e.message));}};
    await window.refreshMotionAssessment(id);if(!valid(session,seq))return;
   }
   if(window.motionCalibrationUI&&!isRetarget)window.motionCalibrationUI(body,id,data);
   if(data.levels){const levels=el('table');for(const level of data.levels){const row=el('tr');row.append(el('th',level.level),el('td',level.status),el('td',level.note||''));levels.append(row);}body.append(levels);}
   body.append(el('p','生成 / 作业状态：'+human(data.job?.status||data.generation_status)),el('p','自动检查：'+human(q.status||'not_checked')),el('p','人审结论：'+human(data.human_review?.status||'未审核')),el('p',isRetarget?'机器人离线检查的具体覆盖范围见测量与阈值；未检查不代表通过。':'人体骨架代理检查：关节中心不等于碰撞表面，不能替代机器人限位、表面碰撞或动力学验证。'));
   const table=el('table');for(const [key,value] of Object.entries(q.checks||{})){const row=el('tr');row.append(el('th',human(key)),el('td',value===true?'通过':value===false?'未通过':'未知 / 未检查'));table.append(row);}body.append(table);
   for(const [key,frames] of Object.entries(q.issue_frames||{})){
    if(!frames.length)continue;const line=el('p',human(key)+'：'+frames.length+' 个局部超阈值帧（总体判定另见上表）。');
    for(const frame of frames.slice(0,8))line.append(button('定位 '+(frame+1)+' 帧',async()=>{
     const currentSession=state.session;window.preserveMotionCalibration?.();$('research-report').close();switchPage(false);await selectMotion(id,true);
     if(currentSession===state.session&&state.current?.motion_id===id&&state.clip){state.frame=Math.min(frame,state.clip.frames-1);state.playing=false;$('play').textContent='播放';}
    }));if(frames.length>8)line.append(document.createTextNode('其余帧见测量与阈值。'));body.append(line);
   }
   for(const [label,value] of [['测量与阈值',q],['来源与执行参数',data.provenance],['执行快照',data.execution]]){const detail=el('details');detail.append(el('summary',label),el('pre',JSON.stringify(value,null,2)));body.append(detail);}
   $('research-report-message').textContent=id+' · 离线检查不能证明真实机器人安全。';
   const p=data.provenance;
   if(!isRetarget&&p?.request_id&&Number.isInteger(p.seed)&&p.effective_prompt){$('research-replay').hidden=false;$('research-replay-prompt').value=p.effective_prompt;$('research-replay-seed').value=p.seed;$('research-replay-duration').value=Math.round(q.metrics?.duration_s||1);}
  }catch(e){if(valid(session,seq))$('research-report-message').textContent=e.message;}
 }
 $('research-report-close').onclick=()=>{window.preserveMotionCalibration?.();epoch++;$('research-report').close();};
 $('research-report').addEventListener('cancel',()=>{window.preserveMotionCalibration?.();epoch++;});
 $('research-replay-form').onsubmit=async e=>{
  e.preventDefault();if(!report)return;const session=state.session,seq=epoch;
  const body={description:$('research-replay-prompt').value,seed:Number($('research-replay-seed').value),duration:Number($('research-replay-duration').value),n:1,prompt_mode:'exact_english',source_motion_id:report.motion_id};
  if(report.provenance.owner===state.user.username)body.parent_request_id=report.provenance.request_id;
  if(!replayBody||JSON.stringify(replayBody.input)!==JSON.stringify(body))replayBody={input:body,request_id:'replay_'+uid()};
  $('research-replay-submit').disabled=true;
  try{const result=await api('/api/custom',{method:'POST',body:JSON.stringify({...body,request_id:replayBody.request_id})});if(valid(session,seq))$('research-report-message').textContent='新任务已提交：'+result.request_id+'；可在定制生成查看进度。';}
  catch(error){if(valid(session,seq))$('research-report-message').textContent=error.message;}
  finally{if(valid(session,seq))$('research-replay-submit').disabled=false;}
 };
 async function refreshCollections(){const session=state.session,request=++collectionsEpoch;const current=()=>session===state.session&&request===collectionsEpoch;try{
  const data=await api('/api/library/collections');if(!current())return;
  const selected=$('research-collection').value;$('research-collection').replaceChildren(new Option('全部',''),new Option('收藏','favorites'),...data.collections.filter(c=>c.id!=='favorites').map(c=>new Option(c.name,c.id)));$('research-collection').value=selected;
  const presets=await api('/api/personal/presets');if(!current())return;const robot=$('research-robot').value;$('research-robot').replaceChildren(new Option('全部',''),...presets.robots.map(r=>new Option(r.label,r.id)));if(presets.robots.some(r=>r.id===robot))$('research-robot').value=robot;
 }catch(e){if(current())message(e.message);}}
 const saved=el('select');saved.setAttribute('aria-label','已保存的检索条件');saved.add(new Option('选择已保存的检索',''));
 let savedItems=[];
 async function savedRefresh(){const session=state.session;try{const data=await api('/api/library/saved-searches');if(session!==state.session)return;savedItems=data.items;saved.replaceChildren(new Option('选择已保存的检索',''),...savedItems.map(s=>new Option(s.name,s.name)));}catch(e){if(session===state.session)message(e.message);}}
 saved.onchange=async()=>{const entry=savedItems.find(s=>s.name===saved.value);if(!entry)return;const session=state.session;await refreshCollections();if(session!==state.session)return;
  if(entry.filters.collection&&![...$('research-collection').options].some(o=>o.value===entry.filters.collection)){message('这组检索引用的集合已删除，请重新选择集合并保存筛选。');return;}
  $('research-filter').reset();for(const [k,v] of Object.entries(entry.filters)){const field=$('research-filter').elements.namedItem(k);if(field)field.value=v;}search();};
 const saveSearch=button('保存当前筛选',async()=>{const name=prompt('检索名称');if(!name)return;const session=state.session;try{await api('/api/library/saved-searches',{method:'POST',body:JSON.stringify({name,filters:Object.fromEntries([...new FormData($('research-filter'))].filter(([,v])=>v!==''))})});if(session===state.session)await savedRefresh();}catch(e){if(session===state.session)message(e.message);}});
 const deleteSearch=button('删除所选检索',async()=>{if(!saved.value)return;const session=state.session;try{await api('/api/library/saved-searches',{method:'POST',body:JSON.stringify({name:saved.value,action:'delete'})});if(session===state.session)await savedRefresh();}catch(e){if(session===state.session)message(e.message);}});
 const exportButton=button('导出所选集合',async()=>{const id=$('research-collection').value;if(!id){message('请先选择私人集合。');return;}const session=state.session;exportButton.disabled=true;try{
  const response=await fetch('/api/library/export?id='+encodeURIComponent(id),{credentials:'same-origin'});if(!response.ok){const error=await response.json();throw new Error(error.error||'导出失败');}const blob=await response.blob();if(session!==state.session)return;const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='motionfarm-collection.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);message('已导出。请查看 manifest.json：明确列出所有动作及未包含的原因；机器人结果文件不包含在此包内。');
 }catch(e){if(session===state.session)message(e.message);}finally{exportButton.disabled=false;}});
 const deleteCollection=button('删除所选集合',async()=>{const id=$('research-collection').value;if(!id){message('请先选择私人集合。');return;}
  if(!confirm('删除私人集合“'+id+'”及其中的收藏记录？动作库中的原动作会保留。'))return;
  const session=state.session;deleteCollection.disabled=true;
  try{await api('/api/library/collections',{method:'POST',body:JSON.stringify({id,action:'delete'})});if(session!==state.session)return;
   if($('research-collection').value===id){$('research-collection').value='';invalidateSearch();}
   await refreshCollections();if(session===state.session)message('集合已删除，名额已释放；原动作保持不变。');
  }catch(e){if(session===state.session)message(e.message);}finally{if(session===state.session)deleteCollection.disabled=false;}
 });

 const audit=el('details');audit.className='active-review';const auditStatus=el('p'),auditRows=el('div'),auditLane=el('select');let auditEpoch=0;
 auditLane.append(new Option('主动审核：争议、缺陷与不确定','active'),new Option('独立随机抽查候选','random'));auditLane.setAttribute('aria-label','审核队列类型');auditStatus.setAttribute('role','status');
 const reasons={missing_evidence:'证据不足',detector_flag:'检测到可疑缺陷',semantic_boundary:'语义待校准',random_audit:'随机抽查',evidence_conflict:'证据冲突 / 人工争议'};
 const auditRefresh=button('加载优先复核动作',async()=>{const session=state.session,seq=++auditEpoch;auditRefresh.disabled=true;auditRows.replaceChildren();auditStatus.textContent='正在选择可访问的复核候选…';try{
  const topic=$('research-filter').elements.namedItem('topic')?.value||'';const result=await api('/api/research/active-queue?'+new URLSearchParams({lane:auditLane.value,limit:20,topic}));if(session!==state.session||seq!==auditEpoch)return;
  auditStatus.textContent=result.items.length?result.scope:'当前候选中没有需要复核的可访问动作；可切换随机抽查或稍后刷新。';
  const table=el('table');const head=el('tr');for(const title of ['动作','主题','入选原因','操作'])head.append(el('th',title));table.append(head);
  for(const item of result.items){const row=el('tr');row.append(el('th',item.motion_id),el('td',item.topic),el('td',reasons[item.selection]||item.selection));const action=el('td');action.append(button('观看动作',async()=>{if(session!==state.session)return;await window.openMotionFromResults(item.motion_id,{items:result.items,source:'主动审核队列'});}),button('分项标注',()=>{if(session===state.session)showReport(item.motion_id);}));row.append(action);table.append(row);}auditRows.append(table);
 }catch(e){if(session===state.session&&seq===auditEpoch)auditStatus.textContent='队列加载失败：'+e.message;}finally{if(session===state.session&&seq===auditEpoch)auditRefresh.disabled=false;}});
 audit.append(el('summary','优先复核 · 少量人工标注'),el('p','评分不确定或证据冲突的动作进入待复核；保存专项意见不会覆盖最终审核。每次最多20条，按描述减少重复。'),auditLane,auditRefresh,auditStatus,auditRows);$('research-search').before(audit);
 const resetBeforeAudit=window.resetResearch;window.resetResearch=()=>{auditEpoch++;auditRows.replaceChildren();auditStatus.textContent='';auditRefresh.disabled=false;resetBeforeAudit();};
 $('research-filter').after(saved,saveSearch,deleteSearch,exportButton,deleteCollection);
 const previousReset=window.resetResearch;window.resetResearch=()=>{previousReset();savedItems=[];saved.replaceChildren(new Option('选择已保存的检索',''));exportButton.disabled=false;deleteCollection.disabled=false;};
 $('research-collections-refresh').onclick=refreshCollections;
 $('research-search').ontoggle=()=>{if($('research-search').open){refreshCollections();savedRefresh();}};
 async function search(append=false){const session=state.session,seq=++searchEpoch;
  const current=()=>session===state.session&&seq===searchEpoch&&state.user;
  const query=new URLSearchParams();for(const [k,v] of new FormData($('research-filter')))if(v!=='')query.set(k,v);
  const key=query.toString();if(key!==searchKey)append=false;searchKey=key;
  if(!append){after=null;searchItems=[];selected.clear();$('research-results').replaceChildren();$('research-more').hidden=true;}
  query.set('after',append?after||0:0);$('research-more').disabled=true;
  try{const data=await api('/api/library/search?'+query);if(!current())return;
   searchItems.push(...data.items);
   for(const item of data.items){const row=el('article');const label=el('label');const check=el('input');check.type='checkbox';check.onchange=()=>{if(check.checked){if(selected.size>=2){check.checked=false;message('每次选择两个动作');return;}selected.set(item.motion_id,item);}else selected.delete(item.motion_id);};label.append(check,document.createTextNode(item.motion_id+' · '+(item.duration??'?')+' 秒 · '+human(item.quality_status)));row.append(label,el('p',item.text),button('播放',()=>window.openMotionFromResults(item.motion_id,{items:searchItems,source:'高级检索 · 当前已载入结果'})),button('重定向',()=>showRetarget(item.motion_id)));window.motionResearchActions(row,item);
   const collection=query.get('collection');if(collection)row.append(button('从集合移除',async()=>{try{await api('/api/library/collections',{method:'POST',body:JSON.stringify({id:collection,motion_id:item.motion_id,action:'remove'})});if(current()){selected.delete(item.motion_id);searchItems=searchItems.filter(x=>x.motion_id!==item.motion_id);row.remove();}}catch(e){if(current())message(e.message);}}));$('research-results').append(row);}
   after=data.next_after;$('research-more').hidden=after===null;message('本页扫描 '+data.scanned+' 个候选；找到 '+data.items.length+' 个。'+(after!==null?'可继续检索。':''));
  }catch(e){if(current())message(e.message);}finally{if(current())$('research-more').disabled=false;}
 }
 const invalidateSearch=()=>{searchEpoch++;searchKey=null;after=null;searchItems=[];selected.clear();$('research-results').replaceChildren();$('research-more').hidden=true;$('research-more').disabled=false;message('筛选已更改，请点击检索。');};
 $('research-filter').addEventListener('input',invalidateSearch);$('research-filter').addEventListener('change',invalidateSearch);
 $('research-filter').onsubmit=e=>{e.preventDefault();search();};$('research-more').onclick=()=>search(true);
 $('research-alias').onsubmit=async e=>{e.preventDefault();const session=state.session;try{await api('/api/library/aliases',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});if(session===state.session)message('同义词映射已保存，原标注保留。');}catch(e){if(session===state.session)message(e.message);}};
 $('research-compare').onclick=async()=>{
  if(selected.size!==2){message('请选择两个动作。');return;}
  const ids=[...selected.keys()],session=state.session,seq=++epoch;comparison=null;$('research-comparison').showModal();$('research-compare-message').textContent='正在加载两个动作…';
  try{const clips=await Promise.all(ids.map(id=>api('/api/research/preview?id='+encodeURIComponent(id))));if(!valid(session,seq))return;
   comparison=clips;seconds=0;playing=true;$('research-compare-play').textContent='暂停';$('research-compare-time').max=Math.max(...clips.map(c=>c.frames/c.fps));$('research-left-label').textContent=ids[0];$('research-right-label').textContent=ids[1];$('research-compare-message').textContent='按真实秒数同步播放，较短动作结束后保持末帧。';
  }catch(e){if(valid(session,seq))$('research-compare-message').textContent=e.message;}
 };
 $('research-compare-close').onclick=()=>{epoch++;comparison=null;$('research-comparison').close();};$('research-comparison').addEventListener('cancel',()=>{epoch++;comparison=null;});
 $('research-compare-play').onclick=()=>{playing=!playing;$('research-compare-play').textContent=playing?'暂停':'播放';};$('research-compare-time').oninput=()=>{seconds=Number($('research-compare-time').value);playing=false;$('research-compare-play').textContent='播放';};
 function tick(now){const dt=Math.min(.1,(now-last)/1000||0);last=now;
  if(comparison&&$('research-comparison').open&&!document.hidden){const duration=Math.max(...comparison.map(c=>c.frames/c.fps));if(playing)seconds=(seconds+dt)%duration;
   comparison.forEach((clip,i)=>{const canvas=$(i?'research-right':'research-left'),ctx=canvas.getContext('2d');canvas.width=520;canvas.height=320;studioLight(ctx,520,320);const points=clip.joints[Math.min(clip.frames-1,Math.floor(seconds*clip.fps))];const base=clip.joints[0][0];const project=p=>[260+(p[0]-base[0]+(p[2]-base[2])*.4)*105,280-p[1]*105,p[2]];litSkeleton(ctx,points,clip.parents,clip.names,project);});
   $('research-compare-time').value=seconds;$('research-compare-clock').textContent=seconds.toFixed(2)+' 秒';
  }requestAnimationFrame(tick);
 }requestAnimationFrame(tick);
 const recovery=el('details');recovery.className='admin-only';recovery.append(el('summary','容量预警与备份覆盖'));const out=el('div');recovery.append(out);
 const gb=v=>v==null?'未知':(v/1e9).toFixed(1)+' GB';const when=v=>v?new Date(v*1000).toLocaleString():'尚无记录';
 recovery.ontoggle=async()=>{if(!recovery.open||state.user?.role!=='admin')return;const session=state.session;out.textContent='正在读取备份状态…';try{const data=await api('/api/recovery');if(session!==state.session)return;out.replaceChildren();
  for(const tier of data.tiers||[])out.append(el('p',(tier.tier==='hot'?'热存储':'冷存储')+'：已计入 '+gb(tier.accounted_bytes)+' / 上限 '+gb(tier.quota_bytes)+'，磁盘可用 '+gb(tier.free_bytes)+'。'+(tier.warning?'容量预警。':'容量正常。')+(tier.ledger_stale?'用量统计待刷新。':'')));
  const db=data.backup||{},motion=data.motion_backup||{},latest=motion.latest,attempt=motion.last_attempt,evidence=motion['restore-evidence'];
  out.append(el('h3','数据库备份'),el('p',!db.configured?'尚未配置':!db.available?'备份目录不可用':'最近成功：'+when(db.last_success)+'；恢复验证：'+(db.restore_verified?'已验证':'未验证')+'；保留 '+db.retention+' 份。'));
  if(db.warning)out.append(el('p',db.age_hours>36?'数据库备份已超过 36 小时，请检查备份任务。':'尚无已完成且通过恢复验证的数据库备份。'));
  out.append(el('h3','动作文件备份'),el('p',!motion.configured?'尚未配置':!motion.available?'备份目录不可用':'同机数据盘独立备份目录。覆盖动作分片、冷归档和重定向结果；不含模型权重。'));
  if(motion.configured){out.append(el('p',latest?'最近完整快照：'+when(latest.created)+'，'+latest.file_count+' 个文件。':'尚无完整动作备份快照。'));
   if(attempt&&attempt.complete===false)out.append(el('p','最近一次备份未完成：'+when(attempt.created)+'，'+attempt.skipped_count+' 项未备份。此前完整快照仍保留。'));
   out.append(el('p',evidence?'恢复验证：'+(evidence.scope==='full'?'全量':'抽样')+' '+evidence.verified_files+' / '+evidence.total_files+' 个文件，'+when(evidence.created)+'。'+(latest&&evidence.snapshot!==latest.id?'验证对应较早快照，最新快照尚未验证。':''):'动作文件尚无恢复验证记录。'));
  }
  const check=motion.verification;
  if(check){const r=check.references;out.append(el('p','定期恢复检查：'+when(check.created)+' · '+(check.complete?'已执行':'失败')+'；'+(r?`数据库分片引用已检查 ${r.checked_shards} 项，未匹配 ${r.missing.length} 项${r.truncated?'（范围不完整）':''}`:'引用检查尚未完成')+'。两个快照时间不同，未匹配项需复核，不直接判定数据损坏。'));}
  const raw=el('details');raw.append(el('summary','查看技术详情'),el('pre',JSON.stringify(data,null,2)));out.append(raw);
 }catch(e){if(session===state.session)out.textContent=e.message;}};$('storage').append(recovery);
 const resetWithRecovery=window.resetResearch;window.resetResearch=()=>{resetWithRecovery();recovery.open=false;out.replaceChildren();};
})();
