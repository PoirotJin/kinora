// Actual personal.js handlers with a bounded DOM/API fixture: no network or paid work.
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
const p={id:'a'.repeat(32),version:1,name:'Research scheme',robot:'unitree_g1_23dof',notes:'',parameters:{smooth_weight:.4},execution_mode:'supported',updated:1,owner:'researcher',archived:0,archive_revision:0};
const robots=[{id:p.robot,label:'G1 23DoF'}];
const page=(items=[p],next_after=null)=>({items,robots,next_after,active_count:items.filter(p=>!p.archived).length,archived_count:items.filter(p=>p.archived).length,active_limit:500});
function setup(){const f=fixture();const robot=new f.Element('select');robot.id='preset-robot';f.$('personal-preset-view').value='active';f.sandbox.confirm=()=>true;const code=read('personal.js');vm.runInContext(code.slice(0,code.indexOf('/* Presentation-only')),f.sandbox);return f;}
function api(f,fn){f.sandbox.api=async(url,o)=>url.startsWith('/api/personal/motions')?{items:[],next_after:null}:fn(url,o);}
const action=(f,label,index=0)=>f.$('personal-presets').children[index].querySelectorAll('button').find(b=>b.textContent===label);
const submit=f=>f.$('preset-form').onsubmit({preventDefault(){}});
const begin=async f=>{await f.$('show-personal').onclick();action(f,'编辑方案').click();};
async function test(name,fn){await fn();results.push(name);}
(async()=>{
 await test('Edits during save remain dirty and next save uses returned base version',async()=>{
  const f=setup(),first=deferred();let stored=p;const posts=[];
  api(f,(url,o)=>{if(!o)return page([stored]);const body=JSON.parse(o.body);posts.push(body);if(posts.length===1)return first.promise.then(v=>(stored=v));return stored={...stored,...body,version:body.version+1};});
  await begin(f);f.$('preset-parameters').value='{"smooth_weight":0.8}';f.$('preset-form').oninput();const work=submit(f);f.$('preset-parameters').value='{"smooth_weight":1.2}';f.$('preset-name').value='Newer name';f.$('preset-form').oninput();first.resolve({...p,version:2,parameters:{smooth_weight:.8}});await work;
  assert.equal(f.$('preset-editor').open,true);assert.equal(f.$('preset-parameters').value,'{"smooth_weight":1.2}');assert.match(f.$('preset-message').textContent,/尚未保存/);assert.equal(f.$('preset-save').disabled,false);
  let asked=false;f.sandbox.confirm=()=>{asked=true;return false;};f.$('preset-editor-close').onclick();assert.equal(asked,true);assert.equal(f.$('preset-editor').open,true);
  await submit(f);assert.equal(posts[1].version,2);assert.equal(posts[1].parameters.smooth_weight,1.2);assert.equal(posts[1].name,'Newer name');assert.equal(f.$('preset-editor').open,false);
 });
 await test('Save failure unlocks without clearing draft; validation failure sends no POST',async()=>{
  const f=setup();let posts=0;api(f,(url,o)=>{if(!o)return page();posts++;throw new Error('暂时不可用');});await begin(f);f.$('preset-name').value='Keep me';f.$('preset-form').oninput();await submit(f);assert.equal(f.$('preset-save').disabled,false);assert.equal(f.$('preset-editor').open,true);assert.equal(f.$('preset-name').value,'Keep me');assert.match(f.$('preset-message').textContent,/暂时不可用/);
  f.$('preset-parameters').value='{broken';await submit(f);assert.equal(posts,1);assert.equal(f.$('preset-save').disabled,false);
 });
 await test('New saved scheme is immediately reachable even outside first page; pagination deduplicates',async()=>{
  const f=setup(),items=Array.from({length:50},(_,i)=>({...p,id:i.toString(16).padStart(32,'0')})),newp={...p,id:'f'.repeat(32),name:'Newest'},extra={...p,id:'e'.repeat(32)};
  api(f,(url,o)=>o?newp:url.includes('after=')?page([extra,newp]):page(items,'cursor'));await f.$('show-personal').onclick();f.$('personal-new-preset').onclick();f.$('preset-name').value='Newest';f.$('preset-parameters').value='{}';await submit(f);assert.equal(f.$('personal-presets').children[0].dataset.preset,newp.id);assert.equal(f.$('personal-presets').children.length,51);assert.equal(f.sandbox.document.activeElement,f.$('personal-presets').children[0]);
  await f.$('personal-preset-more').onclick();const ids=f.$('personal-presets').children.map(c=>c.dataset.preset);assert.equal(ids.length,52);assert.equal(new Set(ids).size,52);
 });
 await test('Account reset discards old save response and restores new user controls',async()=>{
  const f=setup(),pending=deferred();api(f,(url,o)=>o?pending.promise:page());await begin(f);const work=submit(f);f.sandbox.state.session++;f.sandbox.state.user={username:'bob',role:'user'};f.sandbox.resetPersonal();api(f,()=>page([{...p,id:'b'.repeat(32),name:'Bob'}]));await begin(f);f.$('preset-name').value='Bob draft';pending.resolve({...p,version:2});await work;assert.equal(f.$('preset-editor').open,true);assert.equal(f.$('preset-name').value,'Bob draft');assert.equal(f.$('preset-save').disabled,false);assert.equal(f.$('personal-notice').textContent,'');
 });
 await test('Closed same-ID history response cannot duplicate or hide newer pagination',async()=>{
  const f=setup(),old=deferred();let reads=0;const versions=Array.from({length:50},(_,i)=>({...p,version:i+1}));api(f,url=>url.includes('?id=')?(++reads===1?old.promise:{preset:{...p,name:'Current'},versions,next_after:50}):page());await f.$('show-personal').onclick();action(f,'版本记录').click();await tick();f.$('preset-history-close').onclick();action(f,'版本记录').click();await tick();old.resolve({preset:p,versions,next_after:null});await tick();assert.equal(f.$('preset-versions').children.length,50);assert.equal(f.$('preset-history-more').hidden,false);assert.equal(f.$('preset-history-message').textContent,'Current');
 });
 await test('Escape invalidates pending history including its errors',async()=>{
  const f=setup(),old=deferred();api(f,url=>url.includes('?id=')?old.promise:page());await f.$('show-personal').onclick();action(f,'版本记录').click();f.$('preset-history').dispatch('cancel');f.$('preset-history').close();f.$('preset-history-message').textContent='closed';old.reject(new Error('stale'));await tick();assert.equal(f.$('preset-history-message').textContent,'closed');assert.equal(f.$('preset-versions').children.length,0);
 });
 await test('History pagination suppresses repeated requests and account reset clears old responses',async()=>{
  const f=setup(),more=deferred();let pages=0;api(f,url=>url.includes('?id=')?(++pages===1?{preset:p,versions:[p],next_after:1}:more.promise):page());await f.$('show-personal').onclick();action(f,'版本记录').click();await tick();const first=f.$('preset-history-more').onclick();await f.$('preset-history-more').onclick();assert.equal(pages,2);f.sandbox.state.session++;f.sandbox.resetPersonal();more.resolve({preset:p,versions:[{...p,version:2}],next_after:null});await first;assert.equal(f.$('preset-versions').children.length,0);assert.equal(f.$('preset-history-message').textContent,'');
 });
 await test('Archive and restore expose reversible controls, version references and quota',async()=>{
  const f=setup();let stored=p;const posts=[];api(f,(url,o)=>{if(o){const body=JSON.parse(o.body);posts.push(body);return stored={...stored,archived:body.action==='archive'?1:0,archive_revision:stored.archive_revision+1};}if(url.includes('?id='))return{preset:stored,versions:[p],next_after:null};return page((url.includes('archived=1')?stored.archived:!stored.archived)?[stored]:[]);});
  await f.$('show-personal').onclick();await action(f,'归档方案').click();assert.deepEqual(posts[0],{id:p.id,version:1,archive_revision:0,action:'archive'});assert.equal(f.$('personal-presets').children.length,0);
  f.$('personal-preset-view').value='archived';await f.$('personal-preset-view').onchange();assert.equal(action(f,'编辑方案'),undefined);assert(action(f,'恢复方案'));action(f,'版本记录').click();await tick();assert.equal(f.$('preset-versions').children[0].querySelector('button').disabled,true);f.$('preset-history-close').onclick();await action(f,'恢复方案').click();assert.equal(posts[1].archive_revision,1);assert.equal(f.$('personal-preset-view').value,'active');assert(action(f,'用于重定向 v1'));
 });
 await test('Archive response and errors from old account never mutate new account',async()=>{
  const f=setup(),pending=deferred();api(f,(url,o)=>o?pending.promise:page());await f.$('show-personal').onclick();const work=action(f,'归档方案').click();f.sandbox.state.session++;f.sandbox.state.user.username='bob';f.sandbox.resetPersonal();api(f,()=>page([{...p,id:'b'.repeat(32),name:'Bob'}]));await f.$('show-personal').onclick();pending.resolve({...p,archived:1});await work;assert.equal(f.$('personal-notice').textContent,'');assert.equal(f.$('personal-presets').children[0].dataset.preset,'b'.repeat(32));assert.equal(action(f,'归档方案').disabled,false);
 });
 await test('Full-capacity restore keeps archived scheme and unlocks retry',async()=>{
  const f=setup(),archived={...p,archived:1,archive_revision:1};api(f,(url,o)=>{if(o)throw new Error('活跃方案已达 500 个，请先归档其他方案再恢复');return page([archived]);});f.$('personal-preset-view').value='archived';await f.$('show-personal').onclick();const button=action(f,'恢复方案');await button.click();assert.equal(button.disabled,false);assert.match(f.$('personal-notice').textContent,/先归档其他方案/);assert.equal(f.$('personal-preset-view').value,'archived');assert.equal(f.$('personal-presets').children.length,1);
 });
 await test('Catalog filter changes discard old page and reset cursor',async()=>{
  const f=setup(),old=deferred();const urls=[];let hold=false;api(f,url=>{urls.push(url);if(hold&&!url.includes('archived=1'))return old.promise;return page(url.includes('archived=1')?[{...p,id:'b'.repeat(32),archived:1}]:[p],'first-cursor');});await f.$('show-personal').onclick();hold=true;const pending=f.$('personal-preset-more').onclick();f.$('personal-preset-view').value='archived';await f.$('personal-preset-view').onchange();old.resolve(page([{...p,id:'c'.repeat(32)}]));await pending;assert.deepEqual(f.$('personal-presets').children.map(x=>x.dataset.preset),['b'.repeat(32)]);assert.equal(urls.at(-1),'/api/personal/presets?archived=1');
 });
 await test('Explicit history version application retains selected robot and version',async()=>{
  const f=setup();let selection;f.sandbox.showRetarget=(mid,options)=>(selection={mid,options});api(f,url=>url.includes('?id=')?{preset:{...p,version:2},versions:[p,{...p,version:2}],next_after:null}:page());await f.$('show-personal').onclick();action(f,'版本记录').click();await tick();f.$('preset-versions').children[0].querySelector('button').click();assert.equal(selection.options.presetVersion,1);assert.equal(selection.options.presetId,p.id);assert.equal(selection.options.robot,p.robot);assert.equal(f.$('preset-history').open,false);
 });
 console.log(JSON.stringify({passed:results.length,tests:results,scope:'Candidate personal.js business handlers, executed without rewriting; isolated DOM/API fixtures, no production or model calls.'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
