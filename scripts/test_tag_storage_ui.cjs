// Production frontend functions; isolated DOM/API fixtures, no network or solver.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const read=n=>fs.readFileSync(path.join(root,'web/review',n),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
function fixture(){
 const nodes=new Map(),calls=[],messages=[];
 function matches(e,s){return s.split(',').some(t=>t==='article[data-job]'?e.tagName==='article'&&e.dataset.job:t==='[data-job]'?!!e.dataset.job:e.tagName===t);}
 class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this._value='';this.checked=false;this.open=false;this.hidden=false;this.disabled=false;this.textContent='';this.dataset={};this.style={};this.listeners={};this.classList={toggle(){}};}
  set id(v){this._id=v;nodes.set(v,this);}get id(){return this._id;}get value(){return this._value;}set value(v){v=String(v);this._value=this.tagName==='select'&&!this.options.some(o=>o.value===v)?'':v;}
  get options(){return this.children;}get firstChild(){return this.children[0];}get lastChild(){return this.children.at(-1);}
  append(...xs){for(let x of xs){if(typeof x!=='object')x={textContent:String(x)};this.children.push(x);x.parentElement=this;x.parentNode=this;}}
  replaceChildren(...xs){this.children=[];this.append(...xs);if(this.tagName==='select')this._value=this.children[0]?.value||'';}
  replaceWith(el){const i=this.parentNode.children.indexOf(this);this.parentNode.children[i]=el;el.parentNode=this.parentNode;el.parentElement=this.parentElement;}
  insertBefore(el){this.append(el);}after(){}before(){}setAttribute(){}add(o){this.append(o);if(this.tagName==='select'&&this.children.length===1)this._value=o.value;}
  querySelectorAll(s){const results=[];for(const c of this.children){if(matches(c,s))results.push(c);if(c.querySelectorAll)results.push(...c.querySelectorAll(s));}return results;}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}closest(s){return matches(this,s)?this:this.parentElement?.closest?.(s)||null;}
  addEventListener(n,f){(this.listeners[n]??=[]).push(f);}dispatch(n){for(const f of this.listeners[n]||[])f({target:this});return this['on'+n]?.({target:this});}
  reset(){}close(){this.open=false;}showModal(){this.open=true;}remove(){this.parentNode.children=this.parentNode.children.filter(x=>x!==this);}focus(){sandbox.document.activeElement=this;}click(){return this.onclick?.({target:this,stopPropagation(){}});}
 }
 const $=id=>{if(!nodes.has(id)){const e=new Element(id==='retarget-robot'?'select':'div');e.id=id;const label=new Element('label'),parent=new Element('div');label.append({textContent:''},e);parent.append(label);}return nodes.get(id);};
 const sandbox={$,document:{createElement:t=>new Element(t),createTextNode:t=>({textContent:t}),activeElement:null,hidden:false},state:{session:1,user:{username:'researcher',role:'user'},dirty:false,saving:false,items:[],after:null},crypto:require('node:crypto').webcrypto,URLSearchParams,URL,Blob,Option:function(t,v){const o=new Element('option');o.textContent=t;o.value=v;return o;},FormData:class{constructor(e){this.rows=Object.entries(e.formValues||{});}[Symbol.iterator](){return this.rows[Symbol.iterator]();}},requestAnimationFrame(){},setInterval(){},setTimeout(){},clearTimeout(){},addEventListener(){},notice:t=>messages.push(t),leave:()=>true,fillReview(){},closeRetarget(){},switchPage(){},selectMotion:async()=>{},robotViewer:{clear(){},load:async()=>true},api:async(url,options)=>{calls.push({url,body:options?.body?JSON.parse(options.body):null});if(url==='/api/retarget')return{items:[],robots:[{id:'unitree_g1_23dof',label:'G1'}],running:0,queued:0};return{items:[],versions:[],next_after:null,parameter_schema:{parameters:{}}};}};
 sandbox.window=sandbox;vm.createContext(sandbox);
 return {sandbox,$,nodes,calls,messages,Element,loadRetarget:()=>vm.runInContext(read('retarget.js'),sandbox),loadResearch:()=>vm.runInContext(read('research.js'),sandbox)};
}
const results=[];
const text=node=>[node.textContent,...node.children.map(text)].join(' ');
async function test(name,fn){await fn();results.push({name,passed:true});}
(async()=>{
 for(const lateError of [false,true])await test('Successful tag save wins over older initial '+(lateError?'error':'snapshot'),async()=>{
  const f=fixture(),initial=deferred();
  f.sandbox.api=async(url,opts)=>url.startsWith('/api/tags?')?{items:[],next_after:null}:opts?{tags:[{group:'用途',tag:'精选',username:'researcher',created_at:1}],events:[],next_after:null,changed:true}:initial.promise;
  vm.runInContext(read('tags.js'),f.sandbox);
  const open=f.sandbox.openMotionTags('m00000001');f.$('tag-edit-form').onsubmit({preventDefault(){}});await tick();
  assert(text(f.$('tag-current')).includes('精选'));assert(f.$('tag-message').textContent.includes('已保存'));
  if(lateError)initial.reject(new Error('old read failed'));else initial.resolve({tags:[],events:[],next_after:null});
  await open;assert(text(f.$('tag-current')).includes('精选'));assert(f.$('tag-message').textContent.includes('已保存'));
  assert.equal(f.$('tag-save').disabled,false);
 });
 await test('Account reset discards pending tag write response',async()=>{
  const f=fixture(),write=deferred();f.sandbox.api=async(url,opts)=>opts?write.promise:{tags:[],events:[],next_after:null};vm.runInContext(read('tags.js'),f.sandbox);
  await f.sandbox.openMotionTags('m00000001');f.$('tag-edit-form').onsubmit({preventDefault(){}});f.sandbox.state.session++;f.sandbox.resetMotionTags();
  write.resolve({tags:[{group:'old',tag:'old',username:'researcher',created_at:1}],events:[],next_after:null,changed:true});await tick();
  assert.equal(f.$('tag-current').children.length,0);assert.equal(f.$('tag-dialog').open,false);assert.equal(f.$('tag-save').disabled,false);
  await f.sandbox.openMotionTags('m00000002');assert.equal(f.$('tag-save').disabled,false);assert.equal(f.$('tag-history-more').disabled,false);
 });
 for(const role of ['admin','user'])await test('Storage retry is actionable once for '+role,async()=>{
  const f=fixture(),write=deferred();let submits=0;f.sandbox.AbortSignal=AbortSignal;f.sandbox.state.user.role=role;
  f.sandbox.api=async(url,opts)=>{if(opts?.method==='POST'){submits++;assert.equal(url,'/api/storage/retry');assert.equal(JSON.parse(opts.body).job_id,'failed-job');return write.promise;}return{items:[],jobs:[{id:'failed-job',topic:'walking',shard:0,kind:'export',status:'failed',created:1,message:'请重试',retryable:true}],next_after:null,days:7,total_shards:0,usage:null};};
  vm.runInContext(read('storage.js'),f.sandbox);await f.sandbox.refreshStorage();
  const buttons=f.$('storage-jobs').querySelectorAll('button');assert.equal(buttons.length,role==='admin'?1:0);
  if(role==='admin'){const first=buttons[0].click();await buttons[0].click();assert.equal(submits,1);write.resolve({job_id:'new'});await first;assert(f.messages.some(x=>x.includes('原失败记录保留')));}
 });
 console.log(JSON.stringify({tests:results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
