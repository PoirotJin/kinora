// Actual frontend handlers with deferred responses; no network or inference.
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const project=path.resolve(__dirname,'..');
const fixtureSource=fs.readFileSync(path.join(__dirname,'test_tag_storage_ui.cjs'),'utf8').split('\nconst results=[];')[0];
const host={require,__dirname,setImmediate,AbortSignal,URLSearchParams,URL,Blob};vm.createContext(host);vm.runInContext(fixtureSource+'\nglobalThis.fixtureFactory=fixture;',host);
const tick=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}};
const results=[];async function test(name,fn){await fn();results.push({name,passed:true})}
function setup(){
 const f=host.fixtureFactory(),calls=[];let submitted=false;
 const listing=()=>({items:submitted?[{id:'b'.repeat(32),motion_id:'m00000001',owner:'researcher',robot:'unitree_g1_23dof',status:'queued',created:1,done:0,total:0,message:'waiting',queue_position:1}]:[],robots:[{id:'unitree_g1_23dof',label:'G1'}],queued:submitted?1:0,running:0,next_after:null});
 f.sandbox.api=async(url,opts)=>{calls.push({url,method:opts?.method||'GET'});if(url.startsWith('/api/personal/presets'))return{items:[],robots:listing().robots,next_after:null,parameter_schema:{parameters:{}}};if(opts?.method==='POST'){submitted=true;return{id:'b'.repeat(32),status:'queued'}};return listing()};
 return{f,calls,listing};
}
(async()=>{
 await test('Submit from retarget history restores live polling and focuses new task',async()=>{
  const {f,calls}=setup();f.loadRetarget();await f.sandbox.showRetarget('m00000001');await tick();f.$('retarget-history').click();await tick();await f.$('retarget-form').onsubmit({preventDefault(){}});
  assert.equal(f.$('retarget-history').textContent,'全部历史');assert(!f.$('retarget-summary').textContent.includes('全部历史'));assert.equal(f.sandbox.document.activeElement.dataset.job,'b'.repeat(32));
  const n=calls.length;await f.sandbox.refreshRetarget();assert.equal(calls.length,n+1);assert.equal(calls.at(-1).url,'/api/retarget');
 });
 for(const reject of [false,true])await test('Late history '+(reject?'error':'response')+' cannot replace submitted live task',async()=>{
  const {f}=setup(),old=deferred(),api=f.sandbox.api;f.sandbox.api=async(url,opts)=>url.includes('history=1')?old.promise:api(url,opts);
  f.loadRetarget();await f.sandbox.showRetarget('m00000001');await tick();f.$('retarget-history').click();await tick();await f.$('retarget-form').onsubmit({preventDefault(){}});
  const before=f.$('retarget-notice').textContent;
  if(reject)old.reject(new Error('old history error'));else old.resolve({items:[],robots:[],queued:0,running:0});await tick();
  assert.equal(f.$('retarget-jobs').children.length,1);assert.equal(f.$('retarget-notice').textContent,before);assert(!f.$('retarget-summary').textContent.includes('全部历史'));assert.equal(f.$('retarget-history').disabled,false);
 });
 for(const owner of ['other','researcher'])await test('Replay shared source by motion identity; private parent only for own job '+owner,async()=>{
  const f=host.fixtureFactory();let posted;
  f.sandbox.api=async(url,o)=>{if(o?.body){posted=JSON.parse(o.body);return{request_id:posted.request_id}}return{motion_id:'m00000001',quality:{metrics:{duration_s:2}},provenance:{owner,request_id:'prior_request_01',seed:7,effective_prompt:'A person walks slowly forwards with relaxed arms and separate foot tracks.'}}};
  f.loadResearch();const card=new f.Element('article');f.sandbox.motionResearchActions(card,{motion_id:'m00000001'});card.children[0].children.find(x=>x.textContent==='质量 / 来源').click();await tick();await f.$('research-replay-form').onsubmit({preventDefault(){}});
  assert.equal(posted.source_motion_id,'m00000001');assert.equal(posted.parent_request_id,owner==='researcher'?'prior_request_01':undefined);assert.equal(posted.seed,7);assert.equal(posted.prompt_mode,'exact_english');
 });
 console.log(JSON.stringify({tests:results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1});
