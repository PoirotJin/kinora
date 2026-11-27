// Browser-independent behavioral tests using exact production frontend functions.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),path=require('path');
const app=fs.readFileSync(path.resolve(__dirname,'../web/review/app.js'),'utf8'),cal=fs.readFileSync(path.resolve(__dirname,'../web/review/calibration.js'),'utf8');
function section(start,end){return app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));}
class E{constructor(tag,text=''){this.tag=tag;this.textContent=text;this.children=[];this.listeners={};this.value='';this.disabled=false;this.hidden=false;}append(...x){this.children.push(...x);if(this.tag==='select'&&!this.value)this.value=this.children[0]?.value||'';}replaceChildren(...x){this.children=x;}setAttribute(){}get text(){return [this.textContent,...this.children.map(x=>typeof x==='string'?x:x.text)].join(' ');}}
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 let finish;const nodes={topic:{value:''},filter:{value:''},'owner-filter':{value:''},fields:{disabled:false},notes:{value:'unchanged'}};
 const state={request:0,listing:0,saving:false,clip:{},items:[],session:1,listContext:null};
 const context=vm.createContext({state,$:id=>nodes[id],URLSearchParams,api:()=>new Promise(r=>finish=r),renderList(){},clearClip(){state.clip=null;},selectMotion:async()=>{},notice(){},window:{},leave:()=>true,switchPage(){}});
 vm.runInContext(section('async function loadList(','function fillReview(')+'\nthis.loadList=loadList;',context);
 const pending=context.loadList();assert.equal(nodes.fields.disabled,true,'Refresh locks fields immediately');finish({items:[],next_after:null});await pending;assert.equal(state.listLoading,false);
 await context.window.openMotionFromResults('b',{items:[{motion_id:'a'},{motion_id:'b'},{motion_id:'c'}],source:'My results'});assert.equal(state.items.map(x=>x.motion_id).join(','),'a,b,c');assert.equal(state.after,null);assert.equal(state.listContext.source,'My results');

 nodes.loading={};nodes.play={};state.items=[{motion_id:'m00000001'}];state.listLoading=true;state.listing=4;vm.runInContext(section('async function selectMotion(','function renderHistory(')+'\nthis.selectMotion=selectMotion;',context);context.selectMotion('m00000002');assert.equal(state.listLoading,false,'Direct neighbour switch cancels old list loading lock');assert.equal(state.listing,5);

 const hnodes={history:new E('div'),'history-count':new E('span')};const hs={historyAfter:2};
 const hc=vm.createContext({state:hs,$:id=>hnodes[id],document:{createElement:t=>new E(t)},labels:{rejected:'不通过'},issueLabels:{foot_slide:'滑步'}});
 vm.runInContext(section('function renderHistory(','for(const [key,label]')+'\nthis.renderHistory=renderHistory;',hc);
 hc.renderHistory([{revision:3,status:'rejected',reviewer:'alice',updated_at:new Date().toISOString(),semantic:'mismatch',quality:'issues',issues:['foot_slide'],corrected_text:'A person walks.',notes:''}]);
 assert(hnodes.history.text.includes('滑步'));assert(hnodes.history.text.includes('A person walks.'));assert(hnodes.history.text.includes('加载更早审核记录'));

 const uiState={session:1,user:{username:'alice'}};const saved=[];let id=0,serverSha='sha',postCount=0;
 const api=async(url,options)=>{if(options){postCount++;const v=JSON.parse(options.body);if(v.source_sha256!==serverSha){const e=new Error('Source changed');e.status=409;e.current={source_sha256:serverSha};throw e;}const event={...v,id:++id,check_name:v.check,reviewer:uiState.user.username,created:1};saved.unshift(event);return {event,current:[{check_name:v.check,verdict:v.verdict,opinions:[event]}],source_sha256:serverSha};}return {labels:[...saved],next_after:null,current:saved.length?[{check_name:'penetration',verdict:saved[0].verdict,opinions:[saved[0]]}]:[],source_sha256:serverSha};};
 const ui=vm.createContext({state:uiState,document:{createElement:t=>new E(t)},window:{},Option:function(t,v){const e=new E('option',t);e.value=v;return e;},api,URLSearchParams,confirm:()=>true,$:()=>({close(){}})});vm.runInContext(cal,ui);
 const create=async()=>{const body=new E('div');ui.window.motionCalibrationUI(body,'m00000001',{});await settle();return body;};
 let body=await create(),box=body.children[0],notes=box.children[3],verdict=box.children[2];notes.value='2.5 秒确认正常';notes.oninput();verdict.value='normal';verdict.onchange();ui.window.preserveMotionCalibration();
 body=await create();box=body.children[0];assert.equal(box.children[3].value,'2.5 秒确认正常','Report reopen preserves draft');assert.equal(box.children[2].value,'normal');
 await box.children[4].onclick();assert(body.text.includes('alice'),'Saved event appears immediately in history');assert(body.text.includes('审核记录已更新'));
 box.children[3].value='Alice private draft';box.children[3].oninput();serverSha='sha-v2';await box.children[4].onclick();assert.equal(box.children[4].disabled,true,'Changed source blocks draft submission');await box.children[6].onclick();assert.equal(box.children[4].disabled,true,'Rebase cannot silently migrate old source draft');const attempts=postCount;await box.children[4].onclick();assert.equal(postCount,attempts,'Local guard prevents repeated stale-source POST');assert.equal(box.children[3].value,'Alice private draft');uiState.session=2;uiState.user={username:'bob'};body=await create();assert.equal(body.children[0].children[3].value,'','Account switch does not expose prior draft');
 console.log('PASS: refresh lock, neighbour cancellation, changed-source draft blocking, result context, full history, pagination control, specialty draft retention, immediate feedback history, account isolation');
})();
