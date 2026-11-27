const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{
 constructor(tag,text=''){this.tagName=tag.toUpperCase();this.textContent=text;this.children=[];this.hidden=false;this.disabled=false;this.value='';this.dataset={};this.open=false;}
 append(...nodes){for(const n of nodes){this.children.push(n);if(n&&typeof n==='object')n.parent=this;}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 setAttribute(){}
 contains(node){for(let n=node;n;n=n.parent)if(n===this)return true;return false;}
 get options(){return this.children.filter(x=>x.tagName==='OPTION');}
 querySelectorAll(selector){const found=[];const visit=x=>{if(!x||typeof x!=='object')return;if(selector==='details[data-key]'&&x.tagName==='DETAILS'&&x.dataset.key)found.push(x);for(const n of x.children||[])visit(n);};for(const n of this.children)visit(n);return found;}
}
const document={activeElement:null,hidden:false,createElement:t=>new Element(t)},tasks=new Element('section');tasks.hidden=true;
let timer;const calls=[];
const fixture={slot_limit:16,occupied_slots:[],active:[],incremental:{enabled:true,owner:'admin-a'},maintenance_errors:[],batches:[{id:'own',owner:'admin-a',total:1,remaining:0,state:'complete_with_failures',counts:{failed:1},failures:[{motion_id:'m1',message:'test'}]},{id:'other',owner:'admin-b',total:1,remaining:0,state:'complete',counts:{done:1},failures:[]}],batches_next:null,releases_next:null,coverage:[],calibration:{policy:'fixture',groups:[]},releases:[]};
const ctx={document,state:{user:{role:'admin',username:'admin-a'},session:1},URLSearchParams,Option:function(text,value){const e=new Element('option',text);e.value=value;return e;},MutationObserver:class{observe(){}},setInterval(fn){timer=fn;},$:()=>tasks,api:async(url,args)=>{calls.push({url,args});return args?{id:'release-one',status:'queued'}:fixture;}};ctx.window=ctx;
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../web/review/operations.js'),'utf8'),ctx);
const section=tasks.children[0],find=(tag,text)=>section.children.find(x=>x.tagName===tag&&(!text||x.textContent===text));
const refresh=find('BUTTON','刷新批次状态'),form=find('FORM');
(async()=>{
 await refresh.onclick();const [name,batch,base,submit]=form.children;
 assert(batch.options.some(o=>o.value==='own'));assert(!batch.options.some(o=>o.value==='other'));
 name.value='release-one';batch.value='own';document.activeElement=name;
 const before=calls.length;await form.onsubmit({preventDefault(){}});
 assert.deepEqual(calls.slice(before).map(x=>x.url),['/api/operations/releases','/api/operations']);assert.equal(submit.disabled,false);assert.equal(name.value,'');
 name.value='unfinished-draft';base.value='';const detail=section.querySelectorAll('details[data-key]').find(x=>x.dataset.key==='failures:own');detail.open=true;
 await refresh.onclick();assert.equal(name.value,'unfinished-draft');assert.equal(batch.value,'own');assert.equal(section.querySelectorAll('details[data-key]').find(x=>x.dataset.key==='failures:own').open,true);
 tasks.hidden=false;timer();await new Promise(resolve=>setImmediate(resolve));assert.equal(name.value,'unfinished-draft');assert.equal(batch.value,'own');
 document.activeElement=batch;const focusedOptions=batch.children;await refresh.onclick();assert.equal(batch.children,focusedOptions);assert.equal(batch.value,'own');
 assert.equal(typeof ctx.motionCalibrationUI,'undefined');
 console.log('PASS: owner options; Enter POST refresh/finally; manual refresh while focused; polling preserves draft and details; calibration UI delegated.');
})().catch(e=>{console.error(e);process.exitCode=1;});
