// Exercise real workspace event handlers without a browser, GPU or API calls.
const assert=require('assert'),fs=require('fs'),vm=require('vm');
class Element {
 constructor(id=''){this.id=id;this.value='';this.textContent='';this.children=[];this.hidden=false;this.disabled=false;this.checked=false;this.files=[];this.style={};this.clientWidth=600;this.scrollWidth=9600;this.scrollLeft=0;this.attributes={};this.classList={toggle(){}};}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[...nodes];}
 after(node){this.afterNode=node;}
 setAttribute(key,value){this.attributes[key]=String(value);}
 setPointerCapture(id){this.pointer=id;}
 hasPointerCapture(id){return this.pointer===id;}
 releasePointerCapture(){this.pointer=null;}
 focus(){Element.focused=this.id;}
 reset(){}
 addEventListener(){}
 querySelectorAll(){return [];}
 getContext(){return {setTransform(){},clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}};}
 getBoundingClientRect(){return {left:0,right:this.id==='harness-track-scroll'?600:900,width:this.id==='harness-track'?900:0,height:0};}
}
const ids=new Map([...fs.readFileSync('web/review/index.html','utf8').matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element(m[1])]));
const $=id=>{assert(ids.has(id),'Missing DOM element: '+id);return ids.get(id);};
$('harness').hidden=true;$('harness-start').value='0';$('harness-end').value='1';$('harness-budget').value='3';$('harness-cost').value='0';
const state={session:1,user:{username:'alice'},saving:false,dirty:false,current:null,frame:0,request:0};
const calls=[];let loseCreate=true,createBodies=[],task=null,pendingPreview=null,editingAvailable=false,loseRepair=true;
const repairBodies=[],timers=new Map();let timerId=0;
const clip={fps:30,frames:8,parents:[-1,0],names:['root','head'],joints:Array.from({length:8},()=>[[0,1,0],[0,2,0]])};
const api=async(path,options={})=>{
 const body=options.body?JSON.parse(options.body):null;calls.push({path,body});
 if(path==='/api/harness/capabilities')return {backends:[{id:'kimodo',label:'Kimodo',available:false}],driver:{available:false},editing_enabled:editingAvailable};
 if(path==='/api/rewrite/models')return {models:[],default:'exact_english'};
 if(path==='/api/harness/tasks'&&!body)return {items:task?[task]:[]};
 if(path==='/api/harness/tasks/task_1'&&!body)return task;
 if(path.endsWith('/preview')){if(pendingPreview)return pendingPreview.promise;return clip;}
 if(path==='/api/harness/tasks'&&body){createBodies.push(body);if(loseCreate){loseCreate=false;throw new Error('connection lost');}task={...body,source_motion_id:body.source_motion_id==='library_motion'?'ma_library_import':body.source_motion_id,id:'task_1',status:'active',candidates:[],evidence:[],events:[]};return task;}
 if(path.endsWith('/import-patch')){task={...task,candidates:[{id:'candidate_1',motion_id:'ma_edited',status:'pending'}]};return task;}
 if(path.endsWith('/accept')){task={...task,status:'accepted',current_motion_id:'ma_edited',candidates:[{...task.candidates[0],status:'accepted'}]};return task;}
 if(path.endsWith('/repair')){repairBodies.push(body);task={...task,status:'waiting',operations:[{tool:'edit_motion',status:'waiting',result:{message:'正在修复'}}]};if(loseRepair){loseRepair=false;throw new Error('connection lost');}return {...task,decision_receipt:{action_applied:true}};}
 if(path.endsWith('/repair-status')){task={...task,status:'ready',operations:[{tool:'edit_motion',status:'succeeded',result:{message:'候选已就绪'}}],candidates:[{id:'candidate_repair',motion_id:'ma_repaired',status:'pending_review'}]};return task;}
 throw new Error('Unexpected API request: '+path);
};
const context={$,document:{getElementById:$,createElement:tag=>new Element(tag),hidden:false},window:{addEventListener(){}},state,api,
 leave:()=>true,fillReview(){},closeRetarget(){context.window.closeHarness?.();},studioLight(){},litSkeleton(){},requestAnimationFrame(){},devicePixelRatio:1,
 crypto:{randomUUID:()=> '12345678-1234-1234-1234-123456789abc'},console,Number,Math,JSON,Error,Array,Uint8Array,
 setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),
 btoa:value=>Buffer.from(value,'binary').toString('base64')};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/review/harness.js','utf8'),context);
const submit={preventDefault(){}};
function findText(node,text){if(node.textContent===text)return node;for(const child of node.children||[]){const found=findText(child,text);if(found)return found;}return null;}
(async()=>{
 await $('show-harness').onclick();assert.equal($('harness').hidden,false);assert($('harness-capabilities').children.some(x=>x.textContent==='Kimodo · 待接入'));
 $('harness-source').value='ma_source';await $('harness-load-source').onclick();assert.equal(Number($('harness-end').value),8);
 $('harness-start').value='2';$('harness-end').value='5';$('harness-goal').value='把这一段的抬手幅度降低';
 await $('harness-create').onsubmit(submit);assert.match($('harness-notice').textContent,/未确认/);
 await $('harness-create').onsubmit(submit);assert.equal(createBodies.length,2);assert.deepEqual(createBodies[0],createBodies[1]);
 assert.deepEqual(createBodies[1].selection,{start_frame:2,end_frame:5});assert.equal(createBodies[1].budget.max_cost_usd,0);
 $('harness-advance').onclick();assert(!calls.some(x=>x.path.endsWith('/advance')),'Zero budget must not invoke paid decisions');
 $('harness-import-file').files=[{name:'edited.npz',size:4,arrayBuffer:async()=>Uint8Array.from([80,75,3,4]).buffer}];
 await $('harness-import-form').onsubmit(submit);assert.equal(task.candidates.length,1);assert(!calls.some(x=>x.path.endsWith('/accept')),'Import must not accept for the user');
 assert(findText($('harness-candidates'),'接受此版本'));await findText($('harness-candidates'),'接受此版本').onclick();assert.equal(task.status,'accepted');
 assert.equal(task.source_motion_id,'ma_source','The original provenance source must be retained');assert.equal($('harness-source').value,'ma_edited','Accepted version becomes the next editing base');assert.equal($('harness-task-title').textContent,'任务详情');
 await $('harness-task-list').children[0].onclick();assert.equal($('harness-source').value,'ma_edited','Reopening a task must load its current version');
 await $('harness-branch').onclick();assert.equal($('harness-source').value,'ma_edited','Version branching must use the accepted current version');
 editingAvailable=true;await $('harness-refresh').onclick();clip.frames=90;clip.joints=Array.from({length:90},()=>[[0,1,0],[0,2,0]]);
 $('harness-source').value='library_motion';await $('harness-load-source').onclick();$('harness-start').value='20';$('harness-end').value='70';$('harness-goal').value='A person raises the left arm gently.';

 const bounds=()=>[Number($('harness-start').value),Number($('harness-end').value)];
 const pointer=(id,x,pid=1)=>({currentTarget:$(id),target:$(id),pointerId:pid,clientX:x,button:0,isPrimary:true,preventDefault(){},stopPropagation(){}});
 const drag=(id,a,b)=>{$(id).onpointerdown(pointer(id,a));$(id).onpointermove(pointer(id,b));$(id).onpointerup(pointer(id,b));};
 const key=(id,key,shiftKey=false)=>$(id).onkeydown({key,shiftKey,target:$(id),preventDefault(){},stopPropagation(){}});
 drag('harness-track',200,700);assert.deepEqual(bounds(),[20,70],'Track drag selects frame cuts, end exclusive');assert.equal(Element.focused,'harness-track-stage','Dragging focuses timeline shortcuts');
 drag('harness-track',600,300);assert.deepEqual(bounds(),[30,60],'Reverse drag is supported');
 $('harness-trim-start').onpointerdown(pointer('harness-trim-start',300));$('harness-trim-start').onpointermove(pointer('harness-trim-start',800));
 assert.deepEqual(bounds(),[59,60],'Handles cannot cross');assert($('harness-run-repair').disabled,'Too-short selection cannot run');
 $('harness-trim-start').onpointercancel();assert.deepEqual(bounds(),[30,60],'Cancelled gesture restores range');
 drag('harness-trim-end',600,1200);assert.deepEqual(bounds(),[30,90],'End handle may reach the final cut');
 key('harness-trim-end','ArrowLeft',true);assert.deepEqual(bounds(),[30,80],'Shift nudges ten frames');
 drag('harness-move-selection',500,1200);assert.deepEqual(bounds(),[40,90],'Moving range keeps length and clamps to motion');
 drag('harness-move-selection',500,-1000);assert.deepEqual(bounds(),[0,50]);
 drag('harness-ruler',400,500);assert.deepEqual(bounds(),[0,50],'Scrubbing must not modify selection');assert.equal(Number($('harness-timeline').value),50);
 $('harness-frame-prev').onclick();assert.equal(Number($('harness-timeline').value),49);
 drag('harness-track',200,700);$('harness-trim-start').onpointerdown(pointer('harness-trim-start',200));$('harness-trim-start').onpointermove(pointer('harness-trim-start',250));key('harness-trim-start','Escape');assert.deepEqual(bounds(),[20,70]);
 $('harness-zoom-in').onclick();assert.equal($('harness-track-stage').style.width,'200%');assert.deepEqual(bounds(),[20,70]);
 $('harness-zoom-fit').onclick();assert.equal($('harness-track-stage').style.width,'100%');assert.equal($('harness-track-scroll').scrollLeft,0);
 const beforeClick=bounds();$('harness-track').onpointerdown(pointer('harness-track',400));$('harness-track').onpointerup(pointer('harness-track',400));assert.deepEqual(bounds(),beforeClick,'Click seeks without discarding selection');
 $('harness-rewrite-model').value='exact_english';const acceptedBefore=calls.filter(c=>c.path.endsWith('/accept')).length;
 await $('harness-run-repair').onclick();assert.match($('harness-notice').textContent,/同一修复编号/);drag('harness-track',0,100);assert.deepEqual(bounds(),[20,70],'Uncertain submission pins selection');
 await $('harness-run-repair').onclick();assert.equal(repairBodies.length,2);assert.deepEqual(repairBodies[0],repairBodies[1],'Uncertain submit must reuse the same edit id');
 assert.equal($('harness-source').value,'ma_library_import','Library source must resolve to the imported immutable asset before repair');assert.deepEqual(createBodies.at(-1).selection,{start_frame:20,end_frame:70},'Dragged frame cuts must reach the backend unchanged');assert.equal(repairBodies[1].prompt_mode,'exact_english');assert(timers.size>0,'A queued edit must schedule a status poll');assert.equal($('harness-track-stage').attributes['aria-disabled'],'false','Scrubbing stays accessible while repair waits');assert.equal($('harness-trim-start').attributes['aria-disabled'],'true');
 await [...timers.values()].at(-1)();assert(calls.some(c=>c.path.endsWith('/repair-status')));
 assert.equal(calls.filter(c=>c.path.endsWith('/accept')).length,acceptedBefore,'Generated edits must still await explicit acceptance');
 assert.match($('harness-candidate-label').textContent,/ma_repaired/);$('harness-trim-start').onpointerdown(pointer('harness-trim-start',200));
 let resolve;pendingPreview={promise:new Promise(r=>{resolve=r;})};$('harness-source').value='ma_slow';const inFlight=$('harness-load-source').onclick();
 context.window.resetHarness();state.session++;state.user={username:'bob'};resolve(clip);await inFlight;assert.equal($('harness-source-label').textContent,'尚未载入','Old account preview must not reappear');assert($('harness-selection').hidden,'Account reset clears timeline selection');$('harness-trim-start').onpointermove(pointer('harness-trim-start',600));assert($('harness-selection').hidden);
 console.log('harness workspace: pointer selection, trim/move, cancellation, keyboard, zoom, exact frame submission, selected repair, stable retry id, automatic status polling, zero paid decision budget, explicit candidate acceptance, version branching, stale-account guard passed');
})().catch(error=>{console.error(error);process.exit(1);});
