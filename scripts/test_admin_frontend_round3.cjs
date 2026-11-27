// Actual admin frontend in a small isolated DOM; no network, accounts, or real removals.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const source=path.resolve(__dirname,'../web/review/admin.js');
const tick=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const item=(id='m00000001')=>({motion_id:id,removed_at:1000,username:'jfc',reason:'fixture'});
function fixture(){
 const nodes=new Map(),calls=[],messages=[];
 class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this.hidden=false;this.disabled=false;this.textContent='';}
  set id(v){this._id=v;nodes.set(v,this);}get id(){return this._id;}
  append(...xs){this.children.push(...xs);for(const x of xs)x.parentNode=this;}
  replaceChildren(...xs){this.children=[];this.textContent='';this.append(...xs);}before(){}after(){}setAttribute(){}
  querySelector(tag){for(const x of this.children){if(x.tagName===tag)return x;const y=x.querySelector?.(tag);if(y)return y;}return null;}
  reset(){for(const x of this.children)if(x.tagName==='input')x.value='';}
  click(){return this.onclick?.({target:this,preventDefault(){}});}
 }
 const $=id=>{if(!nodes.has(id)){const e=new Element();e.id=id;}return nodes.get(id);};
 $('create-user-form').append(new Element('button'));const sandbox={$,document:{createElement:t=>new Element(t)},state:{session:1,user:{username:'jfc',role:'admin'}},URLSearchParams,notice:(text,error)=>messages.push({text,error}),confirm:()=>true,leave:()=>true,clearClip(){},loadList:async()=>{}};
 sandbox.api=async(url,o)=>{calls.push({url,body:o?.body});if(url==='/api/users')return {users:[]};return {items:[],next_after:null};};sandbox.window=sandbox;vm.createContext(sandbox);vm.runInContext(fs.readFileSync(source,'utf8'),sandbox);
 return {sandbox,$,nodes,calls,messages,Element};
}
const results=[];
async function test(name,fn){await fn();results.push({name,passed:true});}
(async()=>{
 await test('loads earlier pages then exact ID search resets cursor and result list',async()=>{
  const f=fixture();f.sandbox.api=async url=>{f.calls.push(url);const q=new URL(url,'https://fixture').searchParams;return q.get('q')?{items:[item('m00000001')],next_after:null}:q.get('after')?{items:[item('m00000002')],next_after:null}:{items:[item('m00000003')],next_after:'cursor+id'};};
  await f.sandbox.loadTrash();assert.equal(f.$('trash-more').hidden,false);await f.$('trash-more').click();assert.equal(f.$('trash-items').children.length,2);assert.equal(f.calls[1],'/api/removed?after=cursor%2Bid');assert.equal(f.$('trash-more').hidden,true);
  f.$('trash-search').value='m00000001';await f.$('trash-search-form').onsubmit({preventDefault(){}});assert.equal(f.calls[2],'/api/removed?q=m00000001');assert.equal(f.$('trash-items').children.length,1);assert.match(f.$('trash-items').children[0].textContent,/m00000001/);
 });
 await test('editing search invalidates pending response before submit',async()=>{
  const f=fixture(),old=deferred();f.sandbox.api=()=>old.promise;const pending=f.sandbox.loadTrash();f.$('trash-search').value='m00000008';f.$('trash-search').oninput();old.resolve({items:[item()],next_after:'old'});await pending;
  assert.equal(f.$('trash-items').children.length,0);assert.equal(f.$('trash-more').hidden,true);assert.match(f.$('trash-status').textContent,/点击搜索/);
 });
 await test('same-account refresh keeps newest response',async()=>{
  const f=fixture(),first=deferred();let n=0;f.sandbox.api=()=>++n===1?first.promise:Promise.resolve({items:[item('m00000002')],next_after:null});
  const pending=f.sandbox.loadTrash();await f.sandbox.loadTrash();first.resolve({items:[item('m00000001')],next_after:'stale'});await pending;
  assert.equal(f.$('trash-items').children.length,1);assert.match(f.$('trash-items').children[0].textContent,/m00000002/);assert.equal(f.$('trash-more').hidden,true);
 });
 await test('reset clears account state and discards old directory pages and user list',async()=>{
  const f=fixture(),pending=deferred();f.sandbox.api=()=>pending.promise;f.$('new-user-secret').textContent='old secret';f.$('trash-search').value='m00000001';const work=f.sandbox.refreshAdmin();
  f.sandbox.state.session++;f.sandbox.state.user={username:'alice',role:'user'};f.sandbox.resetAdmin();pending.resolve({items:[item()],next_after:'old',users:[{username:'old',role:'admin',sessions:1}]});await work;
  assert.equal(f.$('trash-items').children.length,0);assert.equal(f.$('admin-users').children.length,0);assert.equal(f.$('trash-search').value,'');assert.equal(f.$('new-user-secret').textContent,'');assert.equal(f.$('trash-more').hidden,true);assert.equal(f.messages.length,0);
 });
 await test('restore from exact search refreshes current search and reports empty result',async()=>{
  const f=fixture();let restored=false;f.sandbox.api=async(url,o)=>{f.calls.push({url,body:o?.body});if(o){restored=true;return {ok:true};}return {items:restored?[]:[item()],next_after:null};};
  f.$('trash-search').value='m00000001';await f.sandbox.loadTrash();await f.$('trash-items').children[0].children[0].click();
  assert.deepEqual(f.calls.map(x=>x.url),['/api/removed?q=m00000001','/api/motions/m00000001/restore','/api/removed?q=m00000001']);assert.equal(f.$('trash-items').children.length,0);assert.match(f.$('trash-status').textContent,/未找到/);assert.match(f.messages[0].text,/m00000001 已恢复/);
 });
 await test('old restore completion cannot refresh or notify another account',async()=>{
  const f=fixture(),old=deferred();f.sandbox.api=async(url,o)=>{f.calls.push(url);return o?old.promise:{items:[item()],next_after:null};};await f.sandbox.loadTrash();const work=f.$('trash-items').children[0].children[0].click();
  f.sandbox.state.session++;f.sandbox.state.user={username:'newadmin',role:'admin'};f.sandbox.resetAdmin();old.resolve({ok:true});await work;assert.equal(f.calls.length,2);assert.equal(f.messages.length,0);assert.equal(f.$('trash-items').children.length,0);
 });
 await test('older pagination failure leaves loaded rows and supports retry',async()=>{
  const f=fixture();let n=0;f.sandbox.api=async()=>{n++;if(n===2)throw Error('temporary network error');return n===1?{items:[item('m00000002')],next_after:'after'}:{items:[item('m00000001')],next_after:null};};
  await f.sandbox.loadTrash();await f.$('trash-more').click();assert.equal(f.$('trash-items').children.length,1);assert.equal(f.$('trash-more').hidden,false);assert.equal(f.$('trash-more').disabled,false);await f.$('trash-more').click();assert.equal(f.$('trash-items').children.length,2);
 });
 await test('failed restore re-enables only its original action',async()=>{
  const f=fixture();f.sandbox.api=async(_,o)=>{if(o)throw Error('restore failed');return {items:[item()],next_after:null};};await f.sandbox.loadTrash();const button=f.$('trash-items').children[0].children[0];await button.click();assert.equal(button.disabled,false);assert.equal(f.$('trash-items').children.length,1);assert.equal(f.messages[0].text,'restore failed');
 });
 await test('delayed account creation secret never appears after logout',async()=>{
  const f=fixture(),old=deferred();f.sandbox.api=()=>old.promise;f.$('new-username').value='someone';const form=f.$('create-user-form');const work=form.onsubmit({target:form,preventDefault(){}});
  f.sandbox.state.session++;f.sandbox.state.user=null;f.sandbox.resetAdmin();old.resolve({username:'someone',temporary_password:'SECRET'});await work;assert.equal(f.$('new-user-secret').textContent,'');assert.equal(f.messages.length,0);assert.equal(form.querySelector('button').disabled,false);
 });
 console.log(JSON.stringify({source,scope:'Actual admin.js, isolated DOM/API promises; no real users or tombstones touched.',tests:results},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
