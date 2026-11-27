// Actual custom.js handlers with a bounded DOM/API fixture: no network or paid work.
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
const status=(jobs=[])=>({jobs,quota:{limited:false},queue:{policy:'fixture'},next_after:null});
const job=(id,status='queued',done=0,n=4)=>({id,status,done,n,description:'缓慢下蹲再站起',duration:8,created:1,owner:'researcher',prompt_mode:'translate',rewrite_model:'qwen-default'});
function setup(){const f=fixture();f.sandbox.AbortSignal=AbortSignal;vm.runInContext(read('custom.js'),f.sandbox);f.$('custom-rewrite-model').value='qwen-default';f.$('custom-description').value='缓慢下蹲再站起';f.$('custom-count').value='4';f.$('custom-duration').value='8';return f;}
const submit=f=>f.$('custom-form').onsubmit({preventDefault(){}});
async function test(name,fn){await fn();results.push(name);}
(async()=>{
 await test('Last selected result wins when older request finishes later',async()=>{
  const f=setup(),a=deferred(),b=deferred();f.sandbox.api=url=>url.includes('id=job_A')?a.promise:b.promise;
  const pa=f.sandbox.customResults('job_A'),pb=f.sandbox.customResults('job_B');
  assert.match(f.$('custom-result-label').textContent,/job_B.*正在加载/);
  b.resolve({items:[{motion_id:'B',state:'done'}],next_after:null});await pb;
  a.resolve({items:[{motion_id:'A',state:'done'}],next_after:50});await pa;
  assert.match(f.$('custom-result-label').textContent,/job_B/);assert.match(f.$('custom-result-items').children[0].textContent,/^B/);assert.equal(f.$('custom-result-more').hidden,true);
 });
 await test('Old pagination and errors cannot replace newer result selection',async()=>{
  const f=setup(),page=deferred();f.sandbox.api=async url=>url.includes('after=50')?page.promise:{items:[{motion_id:url.includes('id=A')?'A':'B',state:'done'}],next_after:50};
  await f.sandbox.customResults('A');const paging=f.sandbox.customResults('A',true);await f.sandbox.customResults('B');page.reject(new Error('old page unavailable'));await paging;
  assert.match(f.$('custom-result-label').textContent,/请求 B/);assert.equal(f.messages.length,0);assert.equal(f.$('custom-result-more').disabled,false);
 });
 await test('Result pagination appends once, latest retry refresh replaces',async()=>{
  const f=setup(),page=deferred();let calls=0;f.sandbox.api=async url=>{calls++;return url.includes('after=50')?page.promise:{items:[{motion_id:'A',state:'done'}],next_after:50};};
  await f.sandbox.customResults('A');const first=f.sandbox.customResults('A',true);await f.sandbox.customResults('A',true);assert.equal(calls,2);page.resolve({items:[{motion_id:'B',state:'done'}],next_after:null});await first;assert.equal(f.$('custom-result-items').children.length,2);
  await f.sandbox.customResults('A');assert.equal(f.$('custom-result-items').children.length,1);
 });
 await test('Submitting from history returns to live queue and follows progress',async()=>{
  const f=setup();let complete=false;f.sandbox.api=async(url,o)=>o?.body?{request_id:'new_job'}:status([job('new_job',complete?'completed':'queued',complete?4:0)]);
  f.$('custom-history').onclick();await tick();await submit(f);assert.equal(f.$('custom-history').textContent,'全部历史');assert.match(f.$('custom-updated').textContent,/每 5 秒刷新/);
  complete=true;await f.sandbox.refreshCustom();assert.match(f.$('custom-jobs').children[0].children[0].textContent,/已完成.*4 \/ 4/);
 });
 await test('Inflight old history response cannot replace new submission live list',async()=>{
  const f=setup(),old=deferred();f.sandbox.api=async(url,o)=>o?.body?{request_id:'new_job'}:url.includes('history=1')?old.promise:status([job('new_job')]);
  f.$('custom-history').onclick();await tick();await submit(f);old.resolve(status([job('old_job','failed')]));await tick();
  assert.match(f.$('custom-jobs').children[0].children[0].textContent,/等待文本转换/);assert.match(f.$('custom-updated').textContent,/每 5 秒刷新/);
  assert.equal(f.sandbox.document.activeElement,f.$('custom-jobs').children[0]);
 });
 await test('Partial failure retry fills only missing count and preserves parent',async()=>{
  const f=setup();let posted;f.sandbox.api=async(url,o)=>{if(o?.body){posted=JSON.parse(o.body);return{request_id:posted.request_id};}return status([job('partial_job','failed',1,4)]);};
  await f.sandbox.refreshCustom(false,true);await f.$('custom-jobs').children[0].children.find(x=>x.textContent==='以新任务重试').click();assert.equal(Number(f.$('custom-count').value),3);
  await submit(f);assert.equal(posted.n,3);assert.equal(posted.parent_request_id,'partial_job');
 });
 await test('Fully committed terminal snapshot never offers an extra one-item retry',async()=>{
  const f=setup();f.sandbox.api=async()=>status([job('complete_after_cancel','cancelled',4,4)]);await f.sandbox.refreshCustom(false,true);
  assert.equal(f.$('custom-jobs').children[0].children.some(x=>x.textContent==='以新任务重试'),false);
 });
 await test('Double submission is suppressed and timeout replay retains ID',async()=>{
  const f=setup(),pending=deferred();const posts=[];let count=0;f.sandbox.api=async(url,o)=>{if(!o?.body)return status();posts.push(JSON.parse(o.body));return ++count===1?pending.promise:{request_id:posts.at(-1).request_id};};
  const first=submit(f);await submit(f);assert.equal(posts.length,1);pending.reject(new Error('timeout'));await first;await submit(f);assert.equal(posts[0].request_id,posts[1].request_id);
 });
 await test('Session changes discard result and submission responses',async()=>{
  const f=setup(),old=deferred();f.sandbox.api=()=>old.promise;const pending=f.sandbox.customResults('old_user');f.sandbox.state.session++;f.sandbox.resetCustom();f.$('custom-result-label').textContent='new session';old.resolve({items:[{motion_id:'private',state:'done'}],next_after:null});await pending;assert.equal(f.$('custom-result-label').textContent,'new session');
  const accepted=deferred();f.sandbox.api=()=>accepted.promise;const posting=submit(f);f.sandbox.state.session++;f.sandbox.resetCustom();f.$('custom-notice').textContent='new session';accepted.resolve({request_id:'private'});await posting;assert.equal(f.$('custom-notice').textContent,'new session');
 });
 await test('Reset invalidates pending requests even within same session',async()=>{
  const f=setup(),old=deferred();f.sandbox.api=()=>old.promise;const pending=f.sandbox.customResults('old');f.sandbox.resetCustom();f.$('custom-result-label').textContent='reset';old.resolve({items:[{motion_id:'private',state:'done'}],next_after:null});await pending;assert.equal(f.$('custom-result-label').textContent,'reset');
 });
 await test('English direct input continues to bypass server rewriter selection',async()=>{
  const f=setup();let posted;f.sandbox.api=async(url,o)=>{if(!o?.body)return status();posted=JSON.parse(o.body);return{request_id:posted.request_id};};f.$('custom-rewrite-model').value='exact_english';f.$('custom-description').value='A person slowly crouches down, then rises to stand while lifting both arms above their head.';await submit(f);assert.equal(posted.prompt_mode,'exact_english');assert.equal(posted.rewrite_model,'');
 });
 console.log(JSON.stringify({tests:results.length,passed:results,scope:'Production custom.js handlers, isolated DOM/API; no live requests.'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
