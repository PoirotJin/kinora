const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes={};const $=id=>nodes[id]||=( {value:'',children:[],disabled:false,append(...c){this.children.push(...c)},replaceChildren(...c){this.children=c},addEventListener(t,f){this[t]=f},reset(){}} );
const h=(tag,props={},...children)=>Object.assign({tag,children,append(...c){this.children.push(...c)},setAttribute(name,value){this[name]=value}},props);
let observer,calls=[],repairState='timeout';const S={session:1,user:{username:'alice'},status:{server:'https://example.test'},view:'editor',lib:{selected:'m00000001'}};
const clip={joints:Array.from({length:60},()=>[[0,0,0],[0,1,0]]),parents:[-1,0],fps:30};
const task={id:'task_123',source_motion_id:'m00000001',goal:'A person walks.',status:'waiting',candidates:[],operations:[]};
class View{constructor(canvas,options={}){this.options=options;this.frame=0}setClip(d){this.clip={frames:d.meta.frames};this.options.onFrame?.()}clear(){this.clip=null;this.options.onFrame?.()}seek(f){this.frame=f}setPlaying(p){this.playing=p}}
async function cloud(path,opt={}){calls.push([path,opt]);if(path.endsWith('capabilities'))return {editing_enabled:true};if(path==='/api/rewrite/models')return {models:[]};if(path.endsWith('/preview'))return clip;if(path.endsWith('/tasks'))return opt.method==='POST'?task:[task];if(path.endsWith('/repair')){if(repairState==='timeout')throw Object.assign(Error('network unavailable'),{status:408});return {...task,decision_receipt:{state:repairState,action_applied:repairState==='recorded'}}}return task;}
const storage=new Map();
const app=fs.readFileSync(__dirname+'/../src/renderer/app.js','utf8');
const preview = vm.runInNewContext(fs.readFileSync(__dirname+'/../src/renderer/motion-view.js','utf8') + '\nMotionView;');
function startEditor(){
  const context=vm.createContext({$,h,S,cloud,EditTimeline:{Timeline:class{setFrame(){}setRange(){}setClip(){}sync(){}cancel(){}draw(){}}},MotionView:{...preview,View},crypto:require('node:crypto').webcrypto,document:{querySelector:()=>$('nav'),body:{}},MutationObserver:class{constructor(f){observer=f}observe(){}},Float32Array,console,localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}});
  vm.runInContext(app.slice(app.indexOf('function pendingRecord('),app.indexOf('// ---------------- Cloud bridge')),context);
  vm.runInContext(fs.readFileSync(__dirname+'/../src/renderer/editor.js','utf8'),context);
}
const pendingAction=text=>$('edit-pending').children.find(node=>node.text===text).onclick();
startEditor();
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{await $('nav').click();await $('edit-current').onclick();assert.equal($('edit-end').value,60);$('edit-start').value=0;$('edit-end').value=5;$('edit-prompt').value='A person walks.';$('edit-model').value='exact_english';$('edit-form').onsubmit({preventDefault(){}});await flush();assert.match($('edit-notice').textContent,/12–300/);assert.equal(calls.filter(x=>x[1].method==='POST').length,0);
$('edit-end').value=30;$('edit-form').onsubmit({preventDefault(){}});await flush();assert.match($('edit-notice').textContent,/network/);const first=calls.find(x=>x[0].endsWith('/repair'))[1].body.request_id;
startEditor();await $('nav').click();
repairState='uncertain';await pendingAction('重试上次提交');
assert.equal(calls.filter(x=>x[0].endsWith('/repair')).at(-1)[1].body.request_id,first);
assert.ok(calls.some(x=>x[0].endsWith('/repair-status')));assert.equal($('edit-pending').hidden,false);
assert.equal(calls.filter(x=>x[0].endsWith('/tasks')&&x[1].method==='POST').length,1);
await pendingAction('放弃跟踪此请求');assert.equal($('edit-pending').hidden,true);assert.equal(storage.size,0);
repairState='recorded';await $('edit-current').onclick();$('edit-end').value=30;$('edit-form').onsubmit({preventDefault(){}});await flush();
assert.notEqual(calls.filter(x=>x[0].endsWith('/repair')).at(-1)[1].body.request_id,first);assert.equal(storage.size,0);
S.session++;observer();assert.equal($('edit-result').children.length,0);console.log('PASS: correction selection bounds, cloud submission, idempotent retry and session reset');})().catch(e=>{console.error(e);process.exitCode=1});
