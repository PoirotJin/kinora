'use strict';
// Run actual review renderer functions in a deterministic browser-like sandbox.
// This is not a browser/GPU acceptance test.
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/review/app.js'),'utf8');
const part=source.slice(source.indexOf('// Dependency-free orthographic'),source.indexOf('function toggle()'));
let gradients=0,fail=false,scheduled=[];
const elements=new Map();
const context=new Proxy({createRadialGradient(){gradients++;if(fail)throw Error('transient canvas failure');return {addColorStop(){}};},createLinearGradient(){return {addColorStop(){}};}},{get:(o,k)=>o[k]||(()=>{})});
const canvas={width:0,height:0,getContext:()=>context,getBoundingClientRect:()=>({width:0,height:0})};
function $(id){if(id==='stage')return canvas;if(!elements.has(id))elements.set(id,{hidden:false,textContent:'',value:1});return elements.get(id);}
const scope={$ ,state:{playing:false,clip:null,frame:0,center:[0,1,0],radius:2},devicePixelRatio:1,ResizeObserver:class{observe(){}},requestAnimationFrame:fn=>scheduled.push(fn),document:{hidden:false},notice(){},console};
vm.createContext(scope);vm.runInContext(part,scope);
// Zero-size layout during hidden→visible transition must never touch Canvas gradients.
vm.runInContext('draw()',scope);assert.equal(gradients,0);
vm.runInContext('w=400;h=300',scope);fail=true;
const tick=scheduled.shift();assert.doesNotThrow(()=>tick(100));assert.equal(scheduled.length,1,'a Canvas exception must not permanently kill RAF');
fail=false;scheduled.shift()(200);assert.equal(scheduled.length,1);assert.ok(gradients>=2,'rendering recovers on the next frame');
// A loaded paused clip still updates timeline and the visible timecode.
scope.state.clip={joints:[[[0,1,0],[0,2,0]],[[0,1,1],[0,2,1]]],parents:[-1,0],names:['root','head'],fps:30,frames:2,duration_s:2/30};
vm.runInContext('draw()',scope);assert.equal($('timecode').textContent,'0.00 / 0.07 s');
// Do not execute queued RAF: resize and direct controls must paint synchronously.
canvas.getBoundingClientRect=()=>({width:400,height:300});
const before=gradients;scope.state.frame=1;vm.runInContext('resize()',scope);
assert.ok(gradients>before,'layout changes paint without a RAF callback');
assert.equal($('timecode').textContent,'0.03 / 0.07 s');
canvas.addEventListener=()=>{};scope.window={addEventListener(){}};
vm.runInContext(source.slice(source.indexOf('function toggle()'),source.indexOf('const galleryState=')),scope);
vm.runInContext('step(-1)',scope);assert.equal($('timecode').textContent,'0.00 / 0.07 s');
$('timeline').value=1;$('timeline').oninput();assert.equal($('timecode').textContent,'0.03 / 0.07 s');
const beforeCamera=gradients;$('front').onclick();assert.ok(gradients>beforeCamera,'camera controls draw synchronously');
console.log('PASS: zero-size layout, render recovery, paused timecode, synchronous resize/frame/timeline/camera drawing');
