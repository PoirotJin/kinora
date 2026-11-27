'use strict';
((state,api,studioLight)=>{
 const $e=id=>document.getElementById('re-'+id),node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
 const button=(text,fn)=>{const n=node('button',text);n.type='button';n.className='btn sm subtle';n.onclick=fn;return n;};
 const uid=()=>crypto.randomUUID().replaceAll('-','');
 const dialog=document.getElementById('robot-editor-dialog');
 const view=new RobotEditView($e('canvas'),{});view.suspend();let liveDraft=false,targetDraft=null;
 let s=null,clip=null,original=null,frame=0,keys=[],pins=[],frozen=[],undo=[],redo=[],pending=null,repairPending=null,busy=false,epoch=0,timer=null,playing=false,playbackFrame=0;
 let playbackStart=null,elapsedFrames=0;
 let modelReady=false,retryLoad=null,versionConflict=false,viewedVersion=null;
 let draftVersion=null,draftStorage=null,draftStorageFailed=false,editedRange=null,rangeEdit=null;
 const draftsByVersion=new Map();
 const pendingRepairs=new Map();
 const pendingDownloads=new Set();
 const labels={edit_joint_velocity:'修改速度过快',edit_joint_acceleration:'修改加速度过大',edit_joint_jerk:'关节衔接不平滑',edit_root_velocity:'根移动过快',edit_root_acceleration:'根加速度过大',edit_root_jerk:'根轨迹衔接不平滑',collision:'碰撞',ground:'穿地',foot_slide:'足滑',root_step:'根位置突跳',root_acceleration:'根加速度异常',root_rotation:'根朝向突变',joint_step:'关节突变',joint_limit:'关节越限',tracking_rms:'跟踪误差',tracking_max:'局部跟踪误差',edit_target:'目标未达到',pinned_body:'锁定部位移动'};
 const dynamicsLabels={
  dynamics_root_force:'根节点所需外力超限',dynamics_root_torque:'根节点所需外力矩超限',
  dynamics_actuator_limit:'执行器输出超限',dynamics_velocity_limit:'关节速度超限',
  dynamics_root_position_error_m:'回放根位置误差超限',dynamics_root_rotation_error_rad:'回放根朝向误差超限',
  dynamics_joint_position_error:'回放关节位置误差超限',dynamics_joint_limit:'回放关节越限',
  dynamics_self_collision:'回放自碰撞',dynamics_ground_penetration:'回放穿地',
  dynamics_simulation_warning:'动力学仿真警告'
 };
 const notice=(text,error=false)=>{$e('notice').textContent=text;$e('notice').classList.toggle('error',error);};
 function setLoadRetry(action,label='重试加载'){
  retryLoad=action;$e('retry').hidden=!action;$e('retry').textContent=label;
 }
 function reportWriteError(error){
  notice(error.message,true);
  const legacyConflict=error.status===400&&['当前版本已改变，请刷新后再编辑','版本已改变，请刷新'].includes(error.message);
  if(error.status===409||legacyConflict){
   versionConflict=true;
   setLoadRetry(()=>load(),'刷新版本');
  }
 }
 $e('retry').onclick=()=>{if(!busy)retryLoad?.();};
 const savePending=value=>{if(window.desktop)pendingRecord('robot-edit:'+s.id).set(value);pending=value;};
 const repairRecord=()=>pendingRecord('robot-repair:'+s.id);
 function saveRepairPending(value){
  if(window.desktop){
   const metadata=value&&{request_id:value.request_id,parent:value.parent,fileDigest:value.fileDigest};
   repairRecord().set(metadata);
  }
  if(value)pendingRepairs.set(s.id,value);
  else pendingRepairs.delete(s.id);
  repairPending=value;
 }
 const candidateInProgress=version=>['pending','running'].includes(version?.status);
 const missingTrajectory=version=>version?.trajectory_available===false;
 const unsuccessfulCandidate=version=>['failed','quarantined'].includes(version?.status);
 const busyVersion=()=>s?.versions.some(v=>v.id===s.head&&candidateInProgress(v));
 const submittedCandidate=()=>pending&&s?.versions.find(version=>version.request_id===pending.body.request_id);
 const importedCandidate=()=>repairPending&&s?.versions.find(version=>version.request_id===repairPending.request_id);
 const draftLocked=()=>busy||busyVersion()||versionConflict||Boolean(pending||repairPending);
 const canReselectRepair=()=>repairPending&&!repairPending.npz_base64&&!pending&&!busy&&!busyVersion()&&!versionConflict;
 const atHead=()=>Boolean(clip&&clip.version===s?.head&&(!viewedVersion||viewedVersion===s?.head));
 const selectedJoint=()=>clip?.joint_controls.find(j=>j.name===$e('joint').value);
 const jointLocked=j=>!j||j.locked||frozen.includes(j.name);
 const canDownload=()=>Boolean(s?.accepted&&s.accepted_exportable!==false);
 const downloadKey=()=>state.session+':'+s.id+':'+s.accepted;
 const rejected=error=>error.status>=400&&error.status<500&&error.status!==408&&error.status!==429;
 const valid=(e,session)=>dialog.open&&e===epoch&&session===state.session&&state.user;
 const request=(path,body)=>api('/api/harness/robot-edits'+path,body?{method:'POST',body:JSON.stringify(body)}:{});
 const draft=(range=editedRange)=>JSON.stringify({keys,pins,frozen,editedRange,
  start:range?.start??$e('start').value,end:range?.end??$e('end').value});
 const draftSnapshot=()=>({keys,pins,frozen,undo,redo,start:$e('start').value,end:$e('end').value,editedRange});
 function persistDrafts(){
  if(!draftStorage)return;
  const drafts=new Map(draftsByVersion);
  if(draftVersion!==null)drafts.set(draftVersion,draftSnapshot());
  const stored=[];
  for(const [version,{keys,pins,frozen,start,end,editedRange}] of drafts){
   if(keys.length||pins.length||frozen.length||editedRange){
    stored.push([version,{keys,pins,frozen,start:editedRange?.start??start,end:editedRange?.end??end,editedRange}]);
   }
  }
  try{
   draftStorage.set(stored.length?stored:null);draftStorageFailed=false;
  }catch(error){
   if(!draftStorageFailed)toast('无法在本机保存精修草稿。',true);
   draftStorageFailed=true;
  }
 }
 const draftObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
 const draftVector=value=>Array.isArray(value)&&value.length===3&&value.every(Number.isFinite);
 function validDraftKey(key){
  return draftObject(key)&&Number.isInteger(key.frame)&&key.frame>=0&&draftVector(key.root_offset)
   &&draftObject(key.joint_offsets)&&Object.values(key.joint_offsets).every(Number.isFinite)
   &&draftObject(key.body_offsets)&&Object.values(key.body_offsets).every(draftVector);
 }
 function validStoredDraft(draft){
  if(!draftObject(draft))return false;
  const {keys,pins,frozen,start,end,editedRange}=draft;
  const names=value=>value==null||(Array.isArray(value)&&value.every(name=>typeof name==='string'));
  const rangeValue=value=>value==null||typeof value==='string'||Number.isFinite(value);
  return (keys==null||(Array.isArray(keys)&&keys.every(validDraftKey)))&&names(pins)&&names(frozen)
   &&rangeValue(start)&&rangeValue(end)&&(editedRange==null||(draftObject(editedRange)
    &&Number.isInteger(editedRange.start)&&Number.isInteger(editedRange.end)));
 }
 function readStoredDrafts(){
  let stored;
  try{stored=draftStorage.get();}
  catch(error){
   if(error.name!=='SyntaxError')throw error;
   toast('本机精修草稿无法读取',true);
   return;
  }
  if(stored==null)return;
  let damaged=!Array.isArray(stored);
  for(const entry of Array.isArray(stored)?stored:[]){
   if(!Array.isArray(entry)||typeof entry[0]!=='string'||!entry[0]||!validStoredDraft(entry[1])){
    damaged=true;continue;
   }
   draftsByVersion.set(entry[0],entry[1]);
  }
  if(damaged)toast('部分本机精修草稿无法读取，已恢复可用草稿',true);
 }
 function switchDraft(version){
  if(version===draftVersion)return;
  if(draftVersion!==null){
   draftsByVersion.set(draftVersion,draftSnapshot());
  }
  const saved=draftsByVersion.get(version);
  draftsByVersion.delete(version);
  draftVersion=version;
  keys=saved?.keys||[];pins=saved?.pins||[];frozen=saved?.frozen||[];undo=saved?.undo||[];redo=saved?.redo||[];
  editedRange=saved?.editedRange??null;rangeEdit=null;
  $e('start').value=saved?.start??0;$e('end').value=saved?.end??'';
  liveDraft=false;
  readFrame();renderDraft();
 }
 function rememberUndo(snapshot){
  undo.push(snapshot);if(undo.length>50)undo.shift();redo=[];
 }
 function change(fn,refreshFrame=false){
  if(draftLocked()||!atHead())return;
  rangeEdit=null;rememberUndo(draft());fn();
  persistDrafts();
  if(refreshFrame)readFrame();
  renderDraft();
 }
 function restore(raw){
  const snapshot=JSON.parse(raw);
  ({keys,pins,frozen,editedRange}=snapshot);
  rangeEdit=null;$e('start').value=snapshot.start;$e('end').value=snapshot.end;
  persistDrafts();renderDraft();readFrame();
 }
 function confirmPending(version){
  const parent=pending.body.parent;
  finishDraft(parent,version,true);
  savePending(null);
 }
 function confirmRepair(version){
  const parent=repairPending.parent;
  finishDraft(parent,version,false);
  saveRepairPending(null);
 }
 function finishDraft(parent,version,keepLocks){
  const parentDraft=draftVersion===parent?draftSnapshot():draftsByVersion.get(parent);
  draftsByVersion.delete(parent);
  if(draftVersion===parent||draftVersion===version||draftVersion===null){
   keys=[];undo=[];redo=[];liveDraft=false;
   pins=keepLocks?(parentDraft?.pins||[]):[];frozen=keepLocks?(parentDraft?.frozen||[]):[];
   editedRange=parentDraft?.editedRange??null;rangeEdit=null;
   $e('start').value=parentDraft?.start??0;$e('end').value=parentDraft?.end??'';
   draftVersion=version;
   readFrame();renderDraft();
  }
  persistDrafts();
 }
 function syncSlider(el){if(!el?.style?.setProperty)return;const min=Number(el.min)||0,max=Number(el.max),v=Number(el.value),span=(Number.isFinite(max)?max:100)-min;el.style.setProperty('--p',span>0&&Number.isFinite(v)?Math.max(0,Math.min(100,((v-min)/span)*100)).toFixed(2)+'%':'0%');}
 function resetPlaybackClock(){playbackStart=null;elapsedFrames=0;}
 function setPlaying(value){
  playing=value;resetPlaybackClock();$e('play').textContent=playing?'暂停':'播放';
  cancelAnimationFrame(playbackFrame);
  playbackFrame=playing?requestAnimationFrame(tick):0;
 }
 function reset(){
  view.suspend();view.clear();liveDraft=false;targetDraft=null;epoch++;setPlaying(false);clearTimeout(timer);
  s=null;clip=null;original=null;frame=0;keys=[];pins=[];frozen=[];undo=[];redo=[];pending=null;repairPending=null;busy=false;
  modelReady=false;viewedVersion=null;draftVersion=null;draftStorage=null;draftStorageFailed=false;editedRange=null;rangeEdit=null;draftsByVersion.clear();versionConflict=false;setLoadRetry(null);
  for(const id of ['quality','clock','draft-state','issue-detail','issues','marks','versions','joint','body','keys','locks'])$e(id).replaceChildren();
  $e('start').value=0;$e('end').value='';$e('timeline').max=0;$e('timeline').value=0;syncSlider($e('timeline'));syncSlider($e('joint-slider'));paintTrack();
  $e('import-file').value='';$e('panel').hidden=true;dialog.close();
 }
 function resetSession(){pendingRepairs.clear();reset();}
 const oldReset=window.resetRetarget;window.resetRetarget=()=>{resetSession();oldReset?.();};
 window.resetRobotEditor=resetSession;window.openRobotEditor=open;
 window.robotEditorActions=(actions,job)=>{if(job.repair_available || (job.status==='done' && job.owner===state.user?.username))actions.append(button(job.repair_available?'打开精修':'精修与检查',()=>open(job.id)));};
 async function open(job){
  reset();dialog.showModal();$e('panel').hidden=false;
  await initialize(job);
 }
 async function initialize(job){
  const e=epoch,session=state.session;
  busy=true;setLoadRetry(null);controls();notice('正在读取机器人轨迹…');
  try{
   const key='kinora-robot-editor:'+state.user.username+':'+job;let rid=localStorage.getItem(key);if(!rid){rid=uid();localStorage.setItem(key,rid);}
   const result=await request('',{job_id:job,request_id:rid});if(!valid(e,session))return;s=result;
   repairPending=pendingRepairs.get(s.id)??null;
   if(window.desktop){
    repairPending??=repairRecord().get();
    pending=pendingRecord('robot-edit:'+s.id).get();
    draftStorage=pendingRecord('robot-edit-drafts:'+s.id);
    readStoredDrafts();
   }
   notice('正在加载机器人实体模型…');
   const model=await request('/'+s.id+'/model');if(!valid(e,session))return;await view.setup(model);if(!valid(e,session))return;
   modelReady=true;view.resume();
   await load();if(!valid(e,session))return;$e('panel').scrollIntoView({block:'start'});
  }catch(error){if(valid(e,session)){notice(error.message,true);setLoadRetry(()=>initialize(job));}}
  finally{if(valid(e,session)){busy=false;controls();}}
 }
 function requirePreview(preview,version){
  const {frames,fps,qpos,joint_controls,names,quality}=preview||{};
  const width=qpos?.[0]?.length;
  if(preview?.version!==version||!Number.isInteger(frames)||frames<1||!Number.isFinite(fps)||fps<=0
   ||!Array.isArray(qpos)||qpos.length!==frames||!width
   ||!Array.isArray(joint_controls)||!Array.isArray(names)||!quality||typeof quality!=='object'||Array.isArray(quality)
   ||!qpos.every(row=>Array.isArray(row)&&row.length===width&&row.every(Number.isFinite))
   ||!joint_controls.every(joint=>typeof joint?.name==='string'&&Number.isInteger(joint.index)&&joint.index>=0&&joint.index<width)
   ||!names.every(name=>typeof name==='string')
   ||(quality.events!=null&&(!Array.isArray(quality.events)||quality.events.some(event=>!event||typeof event!=='object')))){
   throw Error('云端返回的机器人轨迹预览不完整，请重试');
  }
  return preview;
 }
 async function load(version=null,refreshOnMissing=true){
  if(!modelReady||!s)return;
  const e=epoch,session=state.session;
  busy=true;setLoadRetry(null);controls();notice('正在读取机器人轨迹…');
  try{
   const detail=await request('/'+s.id);if(!valid(e,session))return;
   versionConflict=false;s=detail;renderVersions();
   const submitted=submittedCandidate(),imported=importedCandidate();
   const failedAttempt=submitted?.status==='failed'?submitted:imported?.status==='failed'?imported:null;
   if(submitted?.status==='ready')confirmPending(submitted.id);
   else if(unsuccessfulCandidate(submitted))savePending(null);
   if(imported?.status==='ready')confirmRepair(imported.id);
   else if(unsuccessfulCandidate(imported))saveRepairPending(null);
   switchDraft(s.head);
   if(busyVersion()){notice('正在求解和检查候选…');poll();return;}
   let v=version||s.head;
   const versions=new Map(s.versions.map(candidate=>[candidate.id,candidate]));
   const selected=versions.get(v);
   const unavailable=selected?.status==='failed'||missingTrajectory(selected);
   if(unavailable){
    while(v!=='original'){
     const candidate=versions.get(v);
     if((candidate?.status==='ready'||candidate?.status==='quarantined')&&!missingTrajectory(candidate))break;
     v=candidate?.parent||'original';
    }
   }
   const result=await request('/'+s.id+'/preview?version='+encodeURIComponent(v));
   if(!valid(e,session))return;
   const nextClip=requirePreview(result,v);
   if(!original){
    if(v==='original')original=nextClip;
    else{
     const source=await request('/'+s.id+'/preview?version=original');
     if(!valid(e,session))return;
     original=requirePreview(source,'original');
    }
   }
   view.stopDrag();clip=nextClip;viewedVersion=selected?.status==='failed'?selected.id:nextClip.version;resetPlaybackClock();
   frame=Math.min(frame,clip.frames-1);$e('timeline').max=clip.frames-1;
   if(atHead()){
    $e('end').max=clip.frames;$e('start').max=clip.frames-1;
    if(editedRange&&!editRange().valid){
     $e('start').value=editedRange.start;
     $e('end').value=editedRange.end;
    }
    if(!$e('end').value||Number($e('end').value)>clip.frames){$e('start').value=0;$e('end').value=Math.min(clip.frames,120);frame=Math.floor(Number($e('end').value)/2);}
   }
   const previousJoint=$e('joint').value,previousBody=$e('body').value;
   $e('joint').replaceChildren(...clip.joint_controls.map(j=>{const o=new Option(j.name+(j.locked?'（锁定）':''),j.name);o.disabled=j.locked;return o;}));
   if(clip.joint_controls.some(j=>j.name===previousJoint&&!j.locked))$e('joint').value=previousJoint;
   $e('body').replaceChildren(...clip.names.map(n=>new Option(n,n)));
   const wrist=clip.mapping?.L_Wrist||clip.names.find(n=>/left.*(wrist|hand)|L_Wrist/i.test(n));
   if(clip.names.includes(previousBody))$e('body').value=previousBody;
   else if(wrist)$e('body').value=wrist;
   issues();readFrame();renderDraft();
   const quality=clip.quality;
   if(unavailable){
    const reason=missingTrajectory(selected)?(selected.trajectory_error||'候选轨迹文件不可用')+'。':selected.error||'候选处理失败。';
    notice(reason+' 当前显示上一条可用轨迹。',true);
   }else if(failedAttempt){
    notice((failedAttempt.error||'候选处理失败。')+' 当前显示上一条可用轨迹。',true);
   }else if(quality.export_allowed){
    notice(quality.checks?.dynamics==null?'运动学复检通过，动力学未验证。':'当前版本已通过服务端验收。');
   }else{
    const reason=quality.reason||'当前版本不可导出。';
    const dynamics=quality.dynamics;
    notice(dynamics?.status==='unverified'&&dynamics.reason?reason+' '+dynamics.reason+'。':reason,true);
   }
  }catch(error){
   if(valid(e,session)){
    if(error.status===410&&refreshOnMissing)await load(version,false);
    else{notice(error.message,true);setLoadRetry(()=>load(version));}
   }
  }finally{
   if(valid(e,session)){
    if(clip)$e('versions').value=viewedVersion||clip.version;
    busy=false;controls();
   }
  }
 }
 function poll(){clearTimeout(timer);const e=epoch,session=state.session;timer=setTimeout(()=>{if(valid(e,session))load();},1500);}
 function controls(){
  const actions=$e('pending');
  if(actions){
   actions.hidden=!(pending||repairPending)||candidateInProgress(submittedCandidate()||importedCandidate());
   actions.replaceChildren(...(pending?[
    button('刷新对账',()=>{if(!busy)load();}),
    button('重试上次提交',()=>{if(!busy)applyPending();}),
    button('放弃跟踪此请求',()=>{if(!busy){savePending(null);controls();notice('已清除本机待确认记录，已提交的作业可在版本列表查看。');}}),
   ]:repairPending?[
    button('刷新复检状态',()=>{if(!busy)load();}),
    repairPending.npz_base64?button('重试上次复检',()=>{if(!busy)submitRepair();}):
     button('重新选择原复检文件',()=>{if(canReselectRepair())$e('import-file').click();}),
    button('放弃复检重试',()=>{if(!busy){saveRepairPending(null);controls();notice('已清除本机重试记录，已提交的复检可在版本列表查看。');}}),
   ]:[]));
   for(const action of actions.children)action.disabled=busy;
  }
  const editing=atHead(),lock=draftLocked()||!editing;
  for(const n of $e('panel').querySelectorAll('input:not([type=range]),select'))n.disabled=Boolean(lock);
  for(const id of ['joint','body','speed','overlay'])$e(id).disabled=!clip||busy;
  $e('versions').disabled=Boolean(!modelReady||busy||busyVersion());
  $e('joint-slider').disabled=$e('joint-value').disabled=$e('freeze').disabled=Boolean(lock||jointLocked(selectedJoint()));
  for(const id of ['prev','play','next','timeline'])$e(id).disabled=!clip;
  for(const id of ['key','pin'])$e(id).disabled=Boolean(lock);
  for(const chip of $e('panel').querySelectorAll('.re-chip button[aria-label]'))chip.disabled=Boolean(lock);
  const historyLocked=!modelReady||draftLocked();
  $e('undo').disabled=Boolean(historyLocked||(undo.length?!editing:!s?.can_undo));$e('redo').disabled=Boolean(historyLocked||(redo.length?!editing:!s?.can_redo));
  const v=s?.versions.find(v=>v.id===s.head);
  $e('accept').disabled=Boolean(lock||v?.status!=='ready'||missingTrajectory(v)||!clip?.quality.export_allowed||s.accepted===s.head);
  const rangeError=editing?rangeProblem(editRange()):'';
  $e('apply').disabled=Boolean(lock||rangeError||!keys.length||clip?.version!==s?.head);
  $e('import-row').hidden=!s?.repair_available;
  $e('import').disabled=historyLocked;
  $e('import-file').disabled=historyLocked&&!canReselectRepair();
  const selectedView=s?.versions.find(c=>c.id===($e('versions')?.value||viewedVersion));
  // Say why the main action is unavailable instead of leaving a silent grey button.
  const hint=$e('foot-hint');if(hint)hint.textContent=busyVersion()?'正在求解和检查候选…':repairPending&&!busy?'复检提交尚未确认。可以刷新状态或重试原文件。':selectedView?.status==='failed'?'所选候选求解失败；切回当前有效版本后可继续修整。':clip&&!editing?'正在查看历史候选；切回最新候选后才能继续修整。':rangeError||(
   s?.repair_available?'导入时请保留待修复 NPZ 的帧率、关节顺序和元数据。复检通过后，接受该候选即可正式导出。':!clip?'':!keys.length?'先在第 4 步保存至少一个关键帧，再求解并检查。':'提交后检查完整轨迹的速度、加速度与衔接突变；不通过的候选禁止导出。');
  renderDownload();
 }
 function renderDownload(){
  const link=$e('download'),available=canDownload();
  const downloading=available&&pendingDownloads.has(downloadKey());
  link.hidden=!available;
  link.textContent=downloading?'正在下载…':'下载已接受轨迹';
  if(downloading)link.setAttribute('aria-disabled','true');
  else link.removeAttribute('aria-disabled');
  if(available)link.href='/api/harness/robot-edits/'+s.id+'/download?version='+s.accepted;
  else link.removeAttribute('href');
 }
 function renderVersions(){
  $e('versions').replaceChildren(new Option('原始版本','original'),...s.versions.map((v,i)=>{
   const label=missingTrajectory(v)?'轨迹文件不可用':({pending:'待处理',running:'处理中',ready:'检查通过',quarantined:'未通过',failed:'失败'}[v.status]||v.status);
   return new Option('候选 '+(i+1)+' · '+label+(s.accepted===v.id?' · 已接受':''),v.id);
  }));
  $e('versions').value=viewedVersion||clip?.version||s.head;
 }
 function issues(){
  $e('issue-detail').textContent='';
  const list=$e('issues'),marks=$e('marks');list.replaceChildren();marks.replaceChildren();
  const events=clip.quality.events||[];$e('quality').textContent=(clip.quality.export_allowed?'可供验收':'需修整')+' · '+events.length+' 条问题记录（最多显示前 1000 条） · 原动作跟踪：'+(clip.quality.source_tracking?.status==='not_recomputed'?'编辑后未重算':'查看记录');
  for(const issue of events.slice(0,1000)){
   const label=labels[issue.kind]||dynamicsLabels[issue.kind]||issue.kind;
   const lastFrame=clip.frames-1;
   const targetFrame=Number.isInteger(issue.frame)&&issue.frame>=0&&issue.frame<=lastFrame?issue.frame:null;
   const subject=issue.part==='root'?'根节点':issue.part==='robot'?'机器人':issue.part;
   const position=targetFrame===null?'无可定位帧':'第 '+targetFrame+' 帧';
   const text=label+' · '+position+' · '+subject;
   const go=()=>{
    if(targetFrame!==null){
     const part=String(issue.part).split(' / ')[0];
     if(clip.joint_controls.some(joint=>joint.name===part)){
      $e('joint').value=part;syncBodyForJoint();
     }else if(clip.names.includes(part))$e('body').value=part;
     seekFrame(targetFrame);
    }
    const details=[text];
    if(Number.isFinite(issue.value))details.push('测量 '+issue.value.toFixed(4));
    else if(issue.value!==undefined)details.push('测量值无效');
    if(Number.isFinite(issue.limit))details.push('阈值 '+issue.limit.toFixed(4));
    $e('issue-detail').textContent=details.join(' · ');
   };
   const b=button(text,go);b.className='btn re-issue '+issue.severity;list.append(b);
   if(targetFrame!==null){
    const m=button('',go);m.className='re-mark '+issue.severity;m.style.left=100*targetFrame/Math.max(1,lastFrame)+'%';m.title=text;m.setAttribute('aria-label',text);marks.append(m);
   }
  }
  if(!events.length)list.append(node('p','当前报告没有定位到异常；是否通过验收以服务端检查结果为准。'));
 }
 function number(id){
  const value=$e(id).valueAsNumber;
  if(!Number.isFinite(value))throw new Error('请填写有效的关节调整和位移数值。');
  return value;
 }
 function editRange(){
  const start=$e('start').valueAsNumber,end=$e('end').valueAsNumber;
  const valid=Number.isInteger(start)&&Number.isInteger(end)&&start>=0&&end<=clip.frames&&end-start>=12&&end-start<=300;
  return {start,end,valid};
 }
 const inside=(range,f)=>f>=range.start+3&&f<range.end-3;
 function rangeProblem(range){
  if(!range.valid)return '请输入有效的起止帧，选区长度需为 12–300 帧。';
  if(keys.some(key=>!inside(range,key.frame)))return '有关键帧超出选区内部。请调整选区或删除冲突的关键帧，边界需各保留 3 帧。';
  return '';
 }
 function draftOffsets(){
  if(!atHead())return RobotFK.offsets([],0,0,frame);
  const range=editRange();
  return range.valid?RobotFK.offsets(keys.filter(key=>inside(range,key.frame)),range.start,range.end,frame):RobotFK.offsets([],0,0,frame);
 }
 function jointAdjustment(j){
  const linear=j.type==='metre',scale=linear?1:180/Math.PI,limit=linear?.1:30;
  const base=clip.qpos[frame][j.index];
  return {scale,min:Math.max(-limit,(j.min-base)*scale),max:Math.min(limit,(j.max-base)*scale),step:linear?.001:.1};
 }
 function readFrame(){
  if(!clip)return;liveDraft=false;targetDraft=null;$e('timeline').value=frame;syncSlider($e('timeline'));$e('clock').textContent=frame+' 帧 · '+(frame/clip.fps).toFixed(2)+' 秒';
  const k=draftOffsets(),j=selectedJoint();
  $e('joint-value').value=(k?.joint_offsets?.[j?.name]?(j.type==='radian'?k.joint_offsets[j.name]*180/Math.PI:k.joint_offsets[j.name]):0).toFixed(j?.type==='metre'?4:2);
  $e('joint-unit').textContent=j?.type==='metre'?'米':'度';
  if(j){
   const {min,max,step}=jointAdjustment(j);
   for(const input of [$e('joint-value'),$e('joint-slider')]){input.min=min;input.max=max;input.step=step;}
   $e('joint-slider').value=$e('joint-value').value;
  }
  syncSlider($e('joint-slider'));
  $e('joint-slider').disabled=$e('joint-value').disabled=$e('freeze').disabled=jointLocked(j)||draftLocked()||!atHead();
  ['x','y','z'].forEach((axis,i)=>{$e('offset-'+axis).value=k?.body_offsets?.[$e('body').value]?.[i]||0;$e('root-'+axis).value=k?.root_offset?.[i]||0;});paintTrack();draw();
 }
 function seekFrame(target){
  if(!clip||!Number.isInteger(target))return;
  if(playing)setPlaying(false);
  frame=Math.max(0,Math.min(clip.frames-1,target));
  readFrame();
 }
 function keyFromControls(){
  const range=editRange();if(!range.valid||!inside(range,frame))throw new Error('选区需为 12–300 帧，关键帧距离两端至少 3 帧。');
  const old=keys.find(k=>k.frame===frame),k=old?JSON.parse(JSON.stringify(old)):{frame,joint_offsets:{},body_offsets:{},root_offset:[0,0,0]};
  const j=selectedJoint(),delta=number('joint-value');
  if(delta){
   if(jointLocked(j))throw new Error('这个关节已锁定');
   const {min,max,scale}=jointAdjustment(j);
   if(delta<min-1e-9||delta>max+1e-9)throw new Error('关节调整超出当前帧的允许范围。');
   k.joint_offsets[j.name]=delta/scale;
  }else if(j&&!j.locked)delete k.joint_offsets[j.name];
  const offset=['x','y','z'].map(a=>number('offset-'+a)),body=$e('body').value;
  if(offset.some(Boolean)){if(pins.includes(body))throw new Error('锁定部位不能同时设置位移');k.body_offsets[body]=offset;}else delete k.body_offsets[body];
  k.root_offset=['x','y','z'].map(a=>number('root-'+a));
  if(Math.hypot(...offset)>.5||Math.hypot(...k.root_offset)>.5)throw new Error('部位和根节点的位移各不得超过 0.5 米。');
  return k;
 }
 function saveKey(){
  if(!atHead()||draftLocked())return;
  try{
   if(keys.length>=16&&!keys.some(key=>key.frame===frame))throw new Error('最多保存 16 个关键帧。请先删除一个，或更新已有关键帧。');
   const k=keyFromControls();
   change(()=>{keys=keys.filter(x=>x.frame!==frame);keys.push(k);keys.sort((a,b)=>a.frame-b.frame);},true);
   notice('关键帧已加入草稿；点击“求解并检查”生成候选。');
  }catch(error){notice(error.message,true);}
 }
 function renderDraft(){
  if(!atHead()){
   $e('keys').replaceChildren();$e('locks').replaceChildren();
   controls();paintTrack();draw();return;
  }
  const chip=(text,go,remove,label)=>{const box=node('span');box.className='re-chip';const a=node('button',text);a.type='button';a.className='text-btn';if(go)a.onclick=go;else a.disabled=true;const x=node('button','×');x.type='button';x.className='text-btn';x.setAttribute('aria-label','删除'+label);x.onclick=remove;box.append(a,x);return box;};
  $e('keys').replaceChildren(...keys.map(k=>chip('关键帧 '+k.frame,
   ()=>seekFrame(k.frame),
   ()=>change(()=>{keys=keys.filter(x=>x.frame!==k.frame);},!liveDraft||frame===k.frame),
   '第 '+k.frame+' 帧关键帧')));
  $e('locks').replaceChildren(...pins.map(n=>chip('固定 '+n,null,()=>change(()=>{pins=pins.filter(x=>x!==n);}),'固定 '+n)),...frozen.map(n=>chip('保持 '+n,null,()=>change(()=>{frozen=frozen.filter(x=>x!==n);}),'保持 '+n)));
  controls();paintTrack();draw();
 }
 // The timeline shows what will change: the edit selection and the saved keyframes.
 function paintTrack(){
  const range=$e('range'),ticks=$e('keyticks');if(!range||!ticks)return;
  if(!atHead()){range.hidden=true;ticks.replaceChildren();return;}
  const selection=editRange(),n=clip.frames,a=selection.start,z=selection.end;
  range.hidden=!(a>=0&&a<n&&z>a);
  if(!range.hidden){range.style.left=100*a/n+'%';range.style.width=100*(Math.min(z,n)-a)/n+'%';range.classList.toggle('invalid',Boolean(rangeProblem(selection)));}
  ticks.replaceChildren(...keys.map(k=>{const t=node('span');t.style.left=100*k.frame/n+'%';t.classList.toggle('current',k.frame===frame);t.title='关键帧 · 第 '+k.frame+' 帧';return t;}));
 }
 async function history(action){
  if(!s||draftLocked())return;
  const e=epoch,session=state.session,head=s.head;
  busy=true;controls();
  try{
   const result=await request('/'+s.id+'/history',{head,action});
   if(!valid(e,session))return;
   s=result;
   if(action==='accept'&&s.head===head){
    renderVersions();notice('已接受当前候选，可以下载轨迹。');
   }else{
    liveDraft=false;
    await load();
   }
  }catch(error){
   if(valid(e,session)){
    if(error.status===410)await load();
    else reportWriteError(error);
   }
  }
  finally{if(valid(e,session)){busy=false;controls();}}
 }
 $e('undo').onclick=()=>{if(!undo.length)history('undo');else if(atHead()){redo.push(draft());restore(undo.pop());}};
 $e('redo').onclick=()=>{if(!redo.length)history('redo');else if(atHead()){undo.push(draft());restore(redo.pop());}};
 async function applyPending(){
  const e=epoch,session=state.session;if(!s||busy)return;
  busy=true;controls();
  try{
   const {body}=pending;
   const r=await request('/'+s.id+'/edit',body);if(!valid(e,session))return;
   s=r;await load();
  }catch(error){if(valid(e,session)){if(rejected(error))savePending(null);reportWriteError(error);}}finally{if(valid(e,session)){busy=false;controls();}}
 }
 $e('apply').onclick=async()=>{
  if(!s||draftLocked()||!clip||!keys.length||clip.version!==s.head)return;
  try{
   const range=editRange(),problem=rangeProblem(range);
   if(problem)throw new Error(problem);
   const body={parent:s.head,start_frame:range.start,end_frame:range.end,keyframes:keys,pinned_bodies:pins,frozen_joints:frozen};
   savePending({body:{...body,request_id:uid()}});
   await applyPending();
  }catch(error){notice(error.message,true);}
 };
 $e('import').onclick=()=>$e('import-file').click();
 async function submitRepair(){
  if(!repairPending?.npz_base64||busy)return;
  const e=epoch,session=state.session;
  busy=true;controls();notice('正在上传并提交复检…');
  try{
   const {request_id,parent,npz_base64}=repairPending;
   const result=await request('/'+s.id+'/import-repair',{request_id,parent,npz_base64});
   if(!valid(e,session))return;
   s=result;await load();
  }catch(error){if(valid(e,session)){if(rejected(error))saveRepairPending(null);reportWriteError(error);}}
  finally{if(valid(e,session)){busy=false;controls();}}
 }
 $e('import-file').onchange=async event=>{
  const input=event.currentTarget,file=input.files[0];
  input.value='';
  if(!file||!s?.repair_available||(draftLocked()&&!canReselectRepair()))return;
  if(!/\.npz$/i.test(file.name)||!file.size||file.size>12*1024*1024){notice('请选择 12 MiB 以内的修复 NPZ。',true);return;}
  const e=epoch,session=state.session,parent=s.head,retry=repairPending;
  busy=true;controls();notice('正在读取修复 NPZ…');
  try{
   const hash=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
   if(!valid(e,session))return;
   const fileDigest=Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');
   if(retry&&fileDigest!==retry.fileDigest)throw Error('所选文件与上次复检文件不同，请重新选择原文件。');
   const data=await new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);
    reader.onerror=()=>reject(new Error('读取 NPZ 失败，请重试。'));
    reader.readAsDataURL(file);
   });
   if(!valid(e,session))return;
   saveRepairPending({request_id:retry?.request_id??uid(),parent:retry?.parent??parent,fileDigest,npz_base64:data});
  }catch(error){if(valid(e,session))notice(error.message,true);return;}
  finally{if(valid(e,session)){busy=false;controls();}}
  if(valid(e,session))await submitRepair();
 };
 $e('accept').onclick=()=>history('accept');$e('key').onclick=saveKey;
 $e('pin').onclick=()=>{
  const name=$e('body').value;if(!name||pins.includes(name))return;
  if(keys.some(key=>Object.hasOwn(key.body_offsets,name))){notice('此部位已有关键帧位移。请先清除这些位移，再固定部位。',true);return;}
  change(()=>pins.push(name));
 };
 $e('freeze').onclick=()=>{
  const joint=selectedJoint();if(jointLocked(joint))return;
  const name=joint.name;
  if(keys.some(key=>Object.hasOwn(key.joint_offsets,name))){notice('此关节已有关键帧调整。请先清除这些调整，再保持关节。',true);return;}
  change(()=>frozen.push(name));
 };
 $e('versions').onchange=()=>load($e('versions').value);
 function syncBodyForJoint(){
  const name=$e('joint').value;
  const body=view.rig?.bodies?.find(body=>body.joints.some(joint=>joint.name===name));
  if(body&&clip?.names.includes(body.name))$e('body').value=body.name;
 }
 $e('timeline').oninput=()=>seekFrame(Number($e('timeline').value));
 $e('joint').onchange=()=>{syncBodyForJoint();readFrame();};$e('body').onchange=readFrame;
 $e('prev').onclick=()=>seekFrame(frame-1);$e('next').onclick=()=>seekFrame(frame+1);
 for(const id of ['offset-x','offset-y','offset-z','root-x','root-y','root-z'])$e(id).oninput=()=>{setPlaying(false);liveDraft=true;draw();};
 for(const id of ['overlay','plane'])$e(id).oninput=draw;
 for(const id of ['joint-value','joint-slider'])$e(id).oninput=event=>{
  setPlaying(false);
  const input=event.currentTarget,other=id==='joint-value'?'joint-slider':'joint-value';
  if(Number.isFinite(input.valueAsNumber))$e(other).value=input.value;
  syncSlider($e('joint-slider'));
  liveDraft=true;draw();
 };
 $e('camera').onclick=()=>view.resetCamera();
 for(const id of ['start','end']){
  const field=$e(id);
  field.onfocus=()=>{rangeEdit={id,before:draft(),recorded:false};};
  field.onchange=field.onblur=()=>{rangeEdit=null;};
  field.oninput=()=>{
   if(atHead()&&!draftLocked()){
    const {start,end,valid}=editRange();
    const previous=editedRange??{start:0,end:Math.min(clip.frames,120)};
    if(valid&&(start!==previous.start||end!==previous.end)){
     if(rangeEdit?.id!==id)rangeEdit={id,before:draft(previous),recorded:false};
     if(!rangeEdit.recorded){rememberUndo(rangeEdit.before);rangeEdit.recorded=true;}
     editedRange={start,end};persistDrafts();
    }
   }
   controls();paintTrack();draw();
  };
 }
 // Esc closes the native dialog; end playback and requests the same way the close button does.
 dialog.addEventListener('close',()=>{if(!dialog.open&&!$e('panel').hidden)reset();});
 dialog.addEventListener('keydown',ev=>{
  if(!clip||ev.defaultPrevented||ev.isComposing||ev.metaKey||ev.ctrlKey||ev.altKey||
     ev.target.closest('button,a,input,select,textarea,[contenteditable]'))return;
  if(ev.key===' '){ev.preventDefault();if(!ev.repeat)$e('play').click();}
  else if(ev.key==='ArrowLeft'||ev.key==='ArrowRight'){ev.preventDefault();seekFrame(frame+(ev.key==='ArrowRight'?1:-1)*(ev.shiftKey?10:1));}
 });
 $e('speed').onchange=resetPlaybackClock;
 view.onBody=name=>{
  if(!clip?.names.includes(name))return;
  $e('body').value=name;
  const bodies=view.rig.bodies;
  const controls=new Map(clip.joint_controls.map(joint=>[joint.name,joint]));
  let body=bodies.find(body=>body.name===name),editable,locked;
  while(body){
   for(const joint of body.joints){
    const control=controls.get(joint.name);
    if(!control)continue;
    if(!control.locked){editable=control;break;}
    locked??=control;
   }
   if(editable||!body.parent)break;
   body=bodies[body.parent];
  }
  $e('joint').value=(editable??locked)?.name??'';
  readFrame();
 };
 view.onTarget=target=>{
  if(!atHead()||draftLocked())return false;
  const index=view.rig.bodies.findIndex(b=>b.name===$e('body').value),base=view.baseFK?.bodies[index]?.pos;
  if(!base)return false;
  const offsets=['x','y','z'].map(axis=>$e('offset-'+axis));
  targetDraft??={liveDraft,offsets:offsets.map(input=>input.value)};
  setPlaying(false);
  offsets.forEach((input,i)=>input.value=Math.max(-.5,Math.min(.5,target[i]-base[i])).toFixed(4));
  liveDraft=true;draw();return true;
 };
 view.onTargetEnd=()=>{targetDraft=null;if(liveDraft)saveKey();};
 view.onCancel=()=>{
  if(!targetDraft)return;
  ['x','y','z'].forEach((axis,i)=>$e('offset-'+axis).value=targetDraft.offsets[i]);
  liveDraft=targetDraft.liveDraft;targetDraft=null;draw();
 };
 function draw(){
  if(!clip||!view.rig||$e('panel').hidden)return;
  try{
   const editing=atHead(),q=clip.qpos[frame].slice();let change=draftOffsets();
   if(editing&&liveDraft)change=keyFromControls();
   for(const j of clip.joint_controls)if(change.joint_offsets[j.name])q[j.index]+=change.joint_offsets[j.name];
   change.root_offset.forEach((v,i)=>q[i]+=v);
   const j=clip.joint_controls.find(j=>j.name===$e('joint').value),factor=j?.type==='metre'?1:180/Math.PI;
   $e('angle').textContent=j?(editing?'修改前 '+(clip.qpos[frame][j.index]*factor).toFixed(2)+' → 草稿 ':'')+(q[j.index]*factor).toFixed(2)+(j.type==='metre'?' m':'°'):'';
   const targets=Object.values(change.body_offsets).some(v=>v.some(Boolean));
   $e('draft-state').textContent=targets?'手脚目标为待求解位置；实体显示当前关节草稿，保存后求解并检查。':editing&&(keys.length||liveDraft)?'实体显示本地关节草稿；保存关键帧后可连续播放，提交后才会进行完整验收。':'实体显示所选版本；拖动旋转，滚轮缩放，点击模型选择部位。';
   view.pose(q,clip.qpos[frame],$e('joint').value,editing?$e('body').value:'',change.body_offsets[$e('body').value]||[0,0,0],$e('overlay').checked,$e('plane').value,original?.qpos[frame]);
  }catch(error){notice(error.message,true);}
 }
 if(window.desktop)$e('download').onclick=async ev=>{
  ev.preventDefault();if(!canDownload())return;
  const key=downloadKey();
  if(pendingDownloads.has(key))return;
  const e=epoch,session=state.session;
  const current=()=>valid(e,session)&&canDownload()&&downloadKey()===key;
  pendingDownloads.add(key);renderDownload();
  try{
   const r=await downloadFile({path:'/api/harness/robot-edits/'+s.id+'/download',query:{version:s.accepted},name:'kinora-robot-edit.npz'});
   if(!current())return;
   if(r?.cancelled)notice('已取消下载。');
   else if(r?.saved)notice('已保存 '+r.saved);
   else if(r?.status===410){const accepted=s.accepted;await load(accepted);}
   else throw Error(r?.error||'下载失败，请重试');
  }catch(error){if(current())notice(error.message||'下载失败，请重试',true);}
  finally{
   pendingDownloads.delete(key);
   if(state.session===session&&canDownload()&&downloadKey()===key)renderDownload();
  }
 };
 $e('play').onclick=()=>{if(clip)setPlaying(!playing);};
 function tick(t){
  playbackFrame=0;
  if(!playing||!clip||!dialog.open)return;
  playbackStart??=t;
  const total=Math.floor((t-playbackStart)*clip.fps*Number($e('speed').value)/1000);
  const advance=total-elapsedFrames;
  if(advance>0){frame=(frame+advance)%clip.frames;elapsedFrames=total;readFrame();}
  playbackFrame=requestAnimationFrame(tick);
 }
 window.addEventListener('resize',draw);$e('close').onclick=reset;
})(typeof S==='undefined'?state:S,typeof S==='undefined'?api:((url,options={})=>{const [path,qs]=url.split('?');return cloud(path,{method:options.method||'GET',query:Object.fromEntries(new URLSearchParams(qs||'')),body:options.body?JSON.parse(options.body):undefined});}),typeof S==='undefined'?studioLight:((ctx,w,h)=>{ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--paper-2').trim();ctx.fillRect(0,0,w,h);}));
