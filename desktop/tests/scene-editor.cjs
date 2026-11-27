const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
function $(id) {
  if (!nodes.has(id)) nodes.set(id, {
    value:id==='scene-duration' ? '4' : '', disabled:false, hidden:false, open:false, checked:false,
    showModal() { this.open=true; }, close() { this.open=false; },
    reset() { $('scene-manual').checked=false; $('scene-text').value=$('scene-spec').value=''; },
    addEventListener() {}, setAttribute(key, value) { this[key]=String(value); }, focus() {},
    replaceChildren(...children) { this.children=children; }
  });
  return nodes.get(id);
}

const S={session:1};
const calls=[], submissions=[], fullSubmissions=[], retargetCalls=[];
const scene={objects:[],targets:[{frame:29}],events:[{frame:21,reason:'接触超差'}],quality:{passed:false}};
let caps={editing_enabled:true}, taskId, view;
class View {
  constructor() { view=this; }
  setClip(clip) { this.clip=clip; }
  setScene(objects, focus, selectedId, actorDrafts) { this.clip=null; this.draft={objects,focus,selectedId,actorDrafts}; }
  setPlaying() {}
  seek(frame) { this.frame=frame; }
}
const task={id:'task_scene',candidates:[{motion_id:'ma_scene',status:'pending_review'}],operations:[]};
const multiClip = idx => ({
  motion_id:'ma_actor_' + (idx + 1),
  fps:30,
  frames:30,
  parents:Array(52).fill(-1),
  names:Array(52).fill('joint'),
  joints:Array.from({length:30},()=>Array.from({length:52},()=>[idx * 0.8,0,0])),
  scene:{objects:[],targets:[],events:[],quality:{passed:true}}
});
let activeTask = task;
let deferredMultiPreview;
async function cloud(path) {
  calls.push(path);
  if (path.endsWith('/capabilities')) return caps;
  if (path.endsWith('/multi-preview')) return deferredMultiPreview?.() || {
    actors:[multiClip(0), multiClip(1)],
    quality:{passed:true},
    meta:{actors:[{label:'R1',robot:'unitree_g1_23dof'},{label:'R2',robot:'unitree_g1_23dof'}]}
  };
  if (path.endsWith('/preview')) return {fps:30,parents:Array(52).fill(-1),names:Array(52).fill('joint'),
    joints:Array.from({length:30},()=>Array.from({length:52},()=>[0,0,0])),scene};
  return activeTask;
}
const scripts=['scene-presets.js','scene-editor.js'].map(name=>fs.readFileSync(__dirname+'/../src/renderer/'+name,'utf8'));
const scope = {
  $, S, cloud, MotionView:{View}, Float32Array,
  Workspace:{
    openMotionInTab:(kind,motionId,opts)=>retargetCalls.push({kind,motionId,opts}),
    registerHarnessTask(){}, markTabDirty(){}, clearTabDirty(){}
  },
  pendingRecord:()=>({get:()=>taskId,set:value=>{taskId=value;}}),
  h:(tag,props,...children)=>({tag,...props,children:children.flat().filter(Boolean)}),
  submitOnce:async (kind,path,value,idField)=>{
    assert.equal(idField,'request_id');
    fullSubmissions.push({kind,path,value});
    if (path==='/api/harness/scene') {
      assert.equal(kind,'scene');
      assert.equal(value.scene.kind,'human_interaction_v1');
      submissions.push(value);
      activeTask = task;
      return task;
    }
    if (path==='/api/harness/multi-scene') {
      activeTask = {
        id:'task_multi',
        goal:'Multi actor scene',
        status:'succeeded',
        candidates:[{motion_id:'ma_actor_1',status:'ready'}],
        actors_group:[
          {actor_index:0,task_id:'task_multi',label:'R1',robot:'unitree_g1_23dof',status:'succeeded',candidates:[{motion_id:'ma_actor_1',status:'ready'}],operations:[]},
          {actor_index:1,task_id:'task_multi_2',label:'R2',robot:'unitree_g1_23dof',status:'succeeded',candidates:[{motion_id:'ma_actor_2',status:'ready'}],operations:[]}
        ],
        operations:[]
      };
      return activeTask;
    }
    if (path==='/api/harness/object-response') {
      activeTask = {id:'task_objresp',candidates:[{motion_id:'ma_objresp',status:'ready'}],operations:[]};
      return activeTask;
    }
    return task;
  },
  document:{body:{dataset:{}}}, MutationObserver:class {observe(){}}
};
vm.runInNewContext(scripts.join('\n') + '\nthis.ScenePresets = ScenePresets;', scope);

(async ()=>{
  await $('scene-open').onclick();
  assert.equal($('scene-submit').disabled,true);
  assert.match($('scene-capability').textContent,/请升级/);
  assert.equal(calls.length,1);
  $('scene-editor').close();
  caps={scene_interaction:true};
  await $('scene-open').onclick();
  const submit=()=>$('scene-form').onsubmit({preventDefault(){}});
  assert.equal($('scene-submit').disabled,true);
  $('scene-add-box').onclick();
  assert.equal(view.draft.objects.length,1);
  assert.match($('scene-action').textContent,/箱子/);
  assert.equal($('scene-submit').disabled,false);
  await submit();
  assert.equal(submissions[0].duration,4);
  assert.match(submissions[0].text,/left hand/);
  assert.equal(taskId,task.id);
  assert.equal(view.clip.scene,scene);
  assert.equal($('scene-result').hidden,false);
  const savedScene=scope.SceneController.getState();
  await scope.SceneController.setState(savedScene,'default',true);
  assert.equal(taskId,task.id,'Restoring the draft keeps its task ID before the preview loads');
  await scope.SceneController.restoreTask();
  assert.ok(calls.includes('/api/harness/tasks/'+task.id+'/scene-status'));
  assert.equal($('scene-submit').disabled,false,'Restored scene stays usable');
  const error=$('scene-markers').children.find(item=>item.text.includes('接触超差'));
  error.onclick();
  assert.equal(view.frame,21);

  $('scene-add-stairs').onclick();
  $('scene-add-chair').onclick();
  assert.equal(view.draft.objects.length,10);
  assert.equal(view.draft.selectedId,'object3');
  assert.match($('scene-action').textContent,/坐下/);
  await submit();
  assert.equal(submissions[1].scene.objects.length,10);
  assert.equal(submissions[1].scene.contacts[0].object_id,'object3-seat');
  const [box,stairs,chair]=$('scene-objects').children;
  const zInput=stairs.children[3].children[1];
  zInput.oninput({currentTarget:{valueAsNumber:2.75}});
  assert.equal(view.draft.objects.find(part=>part.id==='object2-step1').center_m[2],2.75);
  assert.equal(scope.SceneController.getState().objects.find(item=>item.id==='object2').z,2.75);
  stairs.children[0].onclick();
  assert.equal(view.draft.selectedId,'object2');
  await submit();
  const stairScene=submissions[2].scene;
  assert.equal(stairScene.waypoints[0].xz_m[1],1.25);
  assert.equal(stairScene.keyframes.length,6,'6-keyframe stairs plan [0, base, step1, step2, step3, end]');
  assert.equal(stairScene.keyframes[0].frame,0,'Frame 0 ground keyframe emitted');
  assert.deepEqual(Array.from(stairScene.contacts, row=>row.object_id),
    ['object2-step1','object2-step1','object2-step2','object2-step2','object2-step3','object2-step3']);

  $('scene-add-chair').onclick();
  assert.equal(view.draft.objects.length,16);
  assert.equal($('scene-add-box').disabled,true);
  $('scene-objects').children[3].children[1].onclick();
  assert.equal($('scene-add-chair').disabled,false);
  box.children[0].onclick();
  $('scene-duration').value='5';
  chair.children[1].onclick();
  assert.equal($('scene-duration').value,'5');
  $('scene-manual').checked=true;
  $('scene-manual').onchange();
  assert.equal($('scene-add-box').disabled,true);
  assert.equal(JSON.parse($('scene-spec').value).objects.length,4);
  assert.match($('scene-text').value,/^A person /);
  const manualScene=JSON.parse($('scene-spec').value);
  manualScene.keyframes=[{...manualScene.keyframes[0],frame:29}];
  manualScene.waypoints=manualScene.waypoints.slice(0,1);
  manualScene.contacts=[];
  $('scene-spec').value=JSON.stringify(manualScene);
  $('scene-duration').value='1';
  await submit();
  assert.equal(submissions[3].duration,1);
  $('scene-new').onclick();
  assert.equal(view.draft.objects.length,0);
  assert.equal($('scene-submit').disabled,true);
  assert.equal($('scene-refresh').disabled,false);
  $('scene-add-point').onclick();
  const passPoint=$('scene-objects').children[0];
  passPoint.children[4].onclick();
  $('scene-add-box').onclick();
  $('scene-objects').children[1].children[4].onclick();
  $('scene-add-point').onclick();
  const standingPoint=$('scene-objects').children[2];
  standingPoint.children[5].oninput({currentTarget:{value:'站立'}});
  standingPoint.children[4].onclick();
  await submit();
  const ordered=submissions.at(-1);
  assert.equal(ordered.duration,10);
  assert.match(ordered.text,/^A person .*through.*then.*box.*then.*stands still/);
  const arrivals=ordered.scene.waypoints.filter(p=>p.tolerance_m);
  assert.equal(arrivals.length,3);
  assert.equal(arrivals[0].through_frame,undefined);
  assert.ok(arrivals[1].through_frame>arrivals[1].frame);
  assert.equal(arrivals[1].through_frame,arrivals[2].frame);
  assert.ok(ordered.scene.contacts.every(c=>c.start_frame>arrivals[0].frame && c.end_frame<=arrivals[1].frame));
  assert.ok(ordered.scene.keyframes.some(k=>k.object_id==='object1-marker' && k.frame===0), 'Point route emits frame 0 ground keyframe');
  assert.ok(ordered.scene.keyframes.filter(k=>k.object_id==='object1-marker').every(k=>k.frame===0), 'Pass-through point emits no arrival keyframe');

  // Custom object dimension UI controls (box/stairs/chair) & dimensionless point + ScenePresets dims
  assert.equal(scope.ScenePresets.defaultDims('point'), null, 'Point has null defaultDims');
  assert.equal(passPoint.children[5].tag, 'input', 'Point only renders arrival action text input (no size controls)');
  $('scene-new').onclick();
  $('scene-add-box').onclick();
  $('scene-add-stairs').onclick();
  $('scene-add-chair').onclick();
  const [boxItem, stairsItem, chairItem] = $('scene-objects').children;
  assert.equal(boxItem.children[5].class, 'scene-dims');
  assert.equal(stairsItem.children[5].class, 'scene-dims');
  assert.equal(chairItem.children[5].class, 'scene-dims');

  // Verify custom dimensions propagate to ScenePresets geometry & plans
  const findDimInput = (container, ariaSub) => {
    for (const lbl of container.children || []) {
      for (const ch of lbl?.children || []) {
        if (ch && typeof ch === 'object' && ch['aria-label']?.includes(ariaSub)) return ch;
      }
    }
    return null;
  };
  findDimInput(boxItem.children[5], '宽 X').oninput({ currentTarget: { valueAsNumber: 0.50 } });
  findDimInput(boxItem.children[5], '高 Y').oninput({ currentTarget: { valueAsNumber: 0.40 } });
  findDimInput(boxItem.children[5], '深 Z').oninput({ currentTarget: { valueAsNumber: 0.45 } });
  findDimInput(boxItem.children[5], '离地 Y').oninput({ currentTarget: { valueAsNumber: 0.15 } });
  findDimInput(stairsItem.children[5], '级数').oninput({ currentTarget: { valueAsNumber: 4 } });
  findDimInput(chairItem.children[5], '座高').oninput({ currentTarget: { valueAsNumber: 0.50 } });

  const draftBoxPart = view.draft.objects.find(p => p.id === 'object1-box');
  assert.deepEqual(Array.from(draftBoxPart.size_m), [0.50, 0.40, 0.45]);
  assert.ok(Math.abs(draftBoxPart.center_m[1] - 0.35) < 1e-6, 'Box center Y = baseY + y/2');
  assert.equal(view.draft.objects.filter(p => p.id.startsWith('object2-step')).length, 4, '4-step stairs emits 4 parts');
  const draftSeatPart = view.draft.objects.find(p => p.id === 'object3-seat');
  assert.ok(Math.abs(draftSeatPart.center_m[1] - 0.50) < 1e-6, 'Custom chair seatHeight applied');

  // Verify box lift & carry dynamic trajectory + attachments + contacts
  const boxModeSelect = findDimInput(boxItem.children[5], '交互模式');
  boxModeSelect.onchange({ currentTarget: { value: 'lift' } });
  boxItem.children[0].onclick();
  await submit();
  const liftReq = submissions.at(-1);
  assert.ok(liftReq.scene.objects.find(o => o.id === 'object1-box').trajectory.length >= 3, 'Box lift emits trajectory');
  assert.equal(liftReq.scene.attachments.length, 1, 'Box lift emits attachments');
  assert.equal(liftReq.scene.attachments[0].mode, 'lift');
  assert.deepEqual(Array.from(liftReq.scene.attachments[0].joints), [20, 21]);
  assert.equal(liftReq.scene.contacts.length, 2, 'Box lift emits two-hand wrist contacts');

  boxModeSelect.onchange({ currentTarget: { value: 'carry' } });
  const boxRowUpdated = $('scene-objects').children[0];
  findDimInput(boxRowUpdated.children[5], '搬运目标 X').oninput({ currentTarget: { valueAsNumber: 1.5, value: '1.5' } });
  findDimInput(boxRowUpdated.children[5], '搬运目标 Z').oninput({ currentTarget: { valueAsNumber: 2.0, value: '2.0' } });
  await submit();
  const carryReq = submissions.at(-1);
  const carryTraj = carryReq.scene.objects.find(o => o.id === 'object1-box').trajectory;
  assert.equal(carryReq.scene.attachments[0].mode, 'carry');
  assert.equal(carryTraj.at(-1).center_m[0], 1.5);
  assert.equal(carryTraj.at(-1).center_m[2], 2.0);

  // Verify Option 4B getAttachmentRequest & runObjectResponse
  $('gen-attach-object').value = boxItem.children[0].id.replace('scene-pick-', '') + '-box';
  $('gen-attach-mode').value = 'manual';
  $('gen-attach-start').value = '0.5';
  $('gen-attach-end').value = '2.5';
  const attachReq = scope.SceneController.getAttachmentRequest();
  assert.equal(attachReq.attachment.object_id, 'object1-box');
  assert.equal(attachReq.attachment.start_frame, 15);
  assert.equal(attachReq.attachment.end_frame, 75);
  assert.equal(attachReq.attachment.auto_detect, false);
  await scope.SceneController.runObjectResponse('ma_' + '4'.repeat(32), attachReq);
  const objRespCall = fullSubmissions.find(s => s.path === '/api/harness/object-response');
  assert.ok(objRespCall, 'runObjectResponse submits POST /api/harness/object-response');
  assert.equal(objRespCall.value.attachment.object_id, 'object1-box');
  $('gen-attach-mode').value = 'auto';
  const autoAttach = scope.SceneController.getAttachmentRequest();
  assert.equal(autoAttach.attachment.auto_detect, true);
  assert.equal(autoAttach.attachment.start_frame, null);
  assert.equal(autoAttach.attachment.end_frame, null);

  // Verify co_carry box mode spawns second actor if needed and submits co_carry interaction
  boxModeSelect.onchange({ currentTarget: { value: 'co_carry' } });
  assert.equal(view.draft.actorDrafts.length, 2, 'Selecting co_carry auto-adds second actor track');
  await submit();
  const coCarrySub = fullSubmissions.filter(s => s.path === '/api/harness/multi-scene').at(-1);
  assert.ok(coCarrySub.value.interactions.some(it => it.kind === 'co_carry' && it.object_id === 'object1-box' && it.actors[0] !== it.actors[1]));

  // Removing R2 downgrades co_carry back to default box mode and never emits actors: [0, 0]
  const removeR2Btn = $('actor-tracks-list').children[1].children.find(c => c.text === '移除轨道');
  assert.ok(removeR2Btn, 'R2 track exposes remove button');
  removeR2Btn.onclick();
  assert.equal($('actor-tracks-list').children.length, 1);
  const downgradedState = scope.SceneController.getState();
  assert.equal(downgradedState.objects[0].dims.mode, 'touch');
  assert.equal(downgradedState.objects[0].dims.coActorId, '');
  assert.equal(scope.SceneController.canSubmit(), true);
  const sceneCountBeforeDowngradeSubmit = fullSubmissions.filter(s => s.path === '/api/harness/scene').length;
  const multiCountBeforeDowngradeSubmit = fullSubmissions.filter(s => s.path === '/api/harness/multi-scene').length;
  await submit();
  assert.equal(fullSubmissions.filter(s => s.path === '/api/harness/scene').length, sceneCountBeforeDowngradeSubmit + 1);
  assert.equal(fullSubmissions.filter(s => s.path === '/api/harness/multi-scene').length, multiCountBeforeDowngradeSubmit);

  // Legacy/corrupt 1-actor co_carry state cannot be submitted with invalid [0, 0] actor pair
  await scope.SceneController.setState({
    ...downgradedState,
    objects: [{ ...downgradedState.objects[0], dims: { ...downgradedState.objects[0].dims, mode: 'co_carry', coActorId: 'actor2' } }]
  }, 'default', true);
  assert.equal(scope.SceneController.canSubmit(), false, '1-actor co_carry cannot submit');

  // Multi-robot track append (#actor-track-add), onActorMove, handshake, /multi-scene, /multi-preview & per-actor retarget
  $('scene-new').onclick();
  $('scene-add-box').onclick();
  assert.equal($('actor-tracks-list').children.length, 1);
  assert.equal($('scene-add-handshake').hidden, true);
  $('actor-track-add').onclick();
  assert.equal($('actor-tracks-list').children.length, 2);
  assert.equal($('scene-add-handshake').hidden, false);
  assert.equal(view.draft.actorDrafts.length, 2);
  assert.equal(view.draft.actorDrafts[0].label, 'R1');
  assert.equal(view.draft.actorDrafts[1].label, 'R2');
  $('scene-add-handshake').onclick();
  await submit();
  const multiSub = fullSubmissions.filter(s => s.path === '/api/harness/multi-scene').at(-1);
  assert.ok(multiSub, 'Multi-actor tracks submit to /api/harness/multi-scene');
  assert.equal(multiSub.value.actors.length, 2);
  assert.equal(multiSub.value.interactions[0].kind, 'handshake');
  assert.ok(calls.includes('/api/harness/tasks/task_multi/multi-preview'));
  assert.equal(view.clip.scene.actors.length, 2, 'Multi-preview passes clip.scene.actors to MotionView');
  assert.equal(view.clip.scene.actors[0].primary, true);
  const retargetButtons = [];
  for (const chip of $('gen-strip-items').children) {
    for (const child of chip.children || []) {
      if (child.text === '重定向') retargetButtons.push(child);
    }
  }
  assert.equal(retargetButtons.length, 2, 'Candidate strip renders per-actor 重定向 buttons');
  retargetButtons[1].onclick();
  assert.equal(retargetCalls[0].kind, 'retarget');
  assert.equal(retargetCalls[0].motionId, 'ma_actor_2');
  assert.equal(retargetCalls[0].opts.robotId, 'unitree_g1_23dof');

  const completedTask = activeTask;
  activeTask = {...activeTask, actors_group:[
    activeTask.actors_group[0], {...activeTask.actors_group[1], status:'ready', candidates:[], operations:[]}
  ]};
  await scope.SceneController.pollTask(activeTask.id, {manual:true});
  assert.match($('scene-status').textContent,/1 个角色尚未提交/);
  assert.ok($('gen-strip-items').children.some(chip => chip.children[0].text === 'R2 · 待续交'));
  activeTask = completedTask;

  const existingClip = view.clip;
  let resolvePreview;
  let previewRequested;
  const requested = new Promise(resolve => { previewRequested = resolve; });
  deferredMultiPreview = () => new Promise(resolve => {
    resolvePreview = resolve;
    previewRequested();
  });
  const polling = scope.SceneController.pollTask(activeTask.id, {manual:true});
  await requested;
  await scope.SceneController.applyTaskUpdates([{
    ...activeTask, status:'cancelled',
    actors_group:activeTask.actors_group.map(actor => ({...actor,status:'cancelled'}))
  }]);
  resolvePreview({actors:[multiClip(0),multiClip(1)],quality:{passed:true},meta:{}});
  await polling;
  assert.equal($('scene-status').textContent,'场景任务已取消');
  assert.equal(view.clip,existingClip,'A pending preview must not replace the view after cancellation.');
  deferredMultiPreview = null;

  // Adding a personal prop keeps the current scene and its native presets.
  $('scene-new').onclick();
  $('scene-add-box').onclick();
  const assetId = 'a'.repeat(32);
  const height = 19.87654321;
  const anchor = {name:'前把手',position_m:[0,1.1-height/2,-.25]};
  scope.SceneController.addPersonalObject({
    id:assetId, name:'扫描箱', data:{size_m:[.7,height,.5],heading_deg:90}
  },anchor,21);
  assert.equal(view.draft.objects.length,2);
  const object = view.draft.objects.find(o => o.asset_version === assetId);
  assert.ok(object);
  assert.equal(object.heading_deg,90);
  assert.equal(object.center_m[1],height/2);
  await submit();
  const personal = fullSubmissions.filter(s => s.path === '/api/harness/scene').at(-1).value.scene;
  const contact = personal.contacts.find(c => c.object_id === object.id);
  assert.equal(contact.joint_index,21);
  assert.equal(contact.anchor_m.join(','),anchor.position_m.join(','));
  assert.equal(personal.keyframes[0].frame,0);
  assert.ok(Math.abs(object.center_m[1]+personal.keyframes[0].pelvis_offset_m[1]-.915) < 1e-6);

  console.log('PASS: scene presets, target selection, preview, capacity, manual mode, custom dims, multi-actor tracks, object-response, and old-server fallback');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Approach the selected face in object-local coordinates, including after rotation.
{
  const presets = vm.runInNewContext(scripts[0]+'\nScenePresets');
  const asset = {id:'b'.repeat(32),size_m:[2,2,1.2],heading_deg:90,hand:21};
  const item = {id:'prop',type:'asset',x:2,z:3,asset};
  for (const [anchor, xz, heading] of [
    [null,[.88,3],90],
    [[.2,0,-.6],[.88,2.8],90],
    [[-.2,0,.6],[3.12,3.2],-90],
    [[1,.25,.3],[2.3,1.48],0],
    [[-1,.25,-.3],[1.7,4.52],180],
    [[.9,1,.1],[2.1,1.48],0]
  ]) {
    asset.anchor = anchor && {position_m:anchor};
    const scene = presets.request([item],item,4).scene;
    const arrival = scene.waypoints[1];
    assert.equal(arrival.xz_m.join(','),xz.join(','));
    assert.equal(arrival.heading_deg,heading);
    const [x,,z] = scene.keyframes[0].pelvis_offset_m;
    assert.ok(Math.hypot(scene.waypoints[0].xz_m[0]-2-z,scene.waypoints[0].xz_m[1]-3+x) < 1e-6);
    if (anchor) assert.equal(scene.contacts[0].anchor_m.join(','),anchor.join(','));
    else assert.equal(scene.contacts.length,0);
  }
}

// Exercise the renderer's projected handle, pointer capture and floor-plane inverse at Retina scale.
{
  const events = new Map();
  const ctx = new Proxy({}, {get: (target,key) => target[key] ?? (() => {})});
  let capturedPointer = null;
  const canvas = {clientWidth:800,clientHeight:480,getContext:() => ctx,
    getBoundingClientRect:() => ({left:100,top:80}),
    setPointerCapture(id) { capturedPointer = id; },
    hasPointerCapture(id) { return capturedPointer === id; },
    releasePointerCapture() { capturedPointer = null; },
    addEventListener:(name,handler) => events.set(name,handler)};
  const world = {Path2D:class { moveTo() {} lineTo() {} },window:{devicePixelRatio:2},document:{body:{}},getComputedStyle:() => ({fontFamily:'sans-serif'}),
    ResizeObserver: class { observe() {} }};
  const Renderer = vm.runInNewContext(fs.readFileSync('src/renderer/motion-view.js','utf8')+'\nMotionView.View',world);
  const marker = {id:'object3-marker',center_m:[1,0,2],size_m:[.32,.01,.32],heading_deg:0};
  const view = new Renderer(canvas,{onSceneMove(id,dx,dz) {
    assert.equal(id,'object3');
    marker.center_m[0]+=dx;
    marker.center_m[2]+=dz;
    return true;
  }});
  view.setScene([marker],[1,.5,2],'object3');
  view.draw();
  const hit = view.sceneHandles[0];
  const pointer = {clientX:hit.x+100,clientY:hit.y+80,isPrimary:true,button:0,buttons:1,pointerId:4};
  events.get('pointerdown')(pointer);
  events.get('pointermove')({...pointer,clientX:pointer.clientX+60,clientY:pointer.clientY+15});
  events.get('pointerup')({...pointer,buttons:0});
  view.draw();
  const moved = view.sceneHandles[0];
  assert.ok(Math.abs(moved.x-hit.x-60)<1e-6 && Math.abs(moved.y-hit.y-15)<1e-6);
  assert.notEqual(marker.center_m[0],1);
  assert.notEqual(marker.center_m[2],2);

  // Verify MotionView actorHandles + onActorMove dragging R2 handle
  const drafts = [{id:'actor1',label:'R1',x:0,z:0,heading_deg:0,active:false},{id:'actor2',label:'R2',x:1.4,z:0.5,heading_deg:90,active:true}];
  const actorMoved = [];
  const actorView = new Renderer(canvas, {
    onActorMove(id, dx, dz) {
      const target = drafts.find(a => a.id === id);
      if (!target) return false;
      target.x += dx; target.z += dz;
      actorMoved.push(id);
      return true;
    }
  });
  actorView.setScene([], [0.7, 0.5, 0.25], null, drafts);
  actorView.draw();
  assert.equal(actorView.actorHandles.length, 2);
  const r2Hit = actorView.actorHandles.find(h => h.id === 'actor2');
  const aPtr = {clientX: r2Hit.x + 100, clientY: r2Hit.y + 80, isPrimary: true, button: 0, buttons: 1, pointerId: 7};
  events.get('pointerdown')(aPtr);
  events.get('pointermove')({...aPtr, clientX: aPtr.clientX + 40, clientY: aPtr.clientY - 10});
  events.get('pointerup')({...aPtr,buttons:0});
  assert.equal(actorMoved[0], 'actor2');
  assert.notEqual(drafts[1].x, 1.4);
}
