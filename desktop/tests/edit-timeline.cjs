const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes={};function $(id){return nodes[id]||={value:'',style:{},children:[],attrs:{},scrollLeft:0,clientWidth:600,classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v},replaceChildren(...c){this.children=c},append(c){this.children.push(c)},addEventListener(k,f){this[k]=f},getBoundingClientRect(){return {left:0,right:600,width:600}},focus(){},setPointerCapture(){},hasPointerCapture(){return false}};}
let locked=false,selected,frame;
const c=vm.createContext({$,h:(tag,p)=>({style:{},...p}),ResizeObserver:class{observe(){}},MotionView:{drawThumbnail(){}},MotionStrip:{draw(){}},requestAnimationFrame:()=>1,cancelAnimationFrame(){}});
vm.runInContext(fs.readFileSync(__dirname+'/../src/renderer/edit-timeline.js','utf8')+'\nthis.T=EditTimeline;',c);
const t=new c.T.Timeline({seek:f=>frame=f,changed:(a,b)=>selected=[a,b],locked:()=>locked});
t.setClip({frames:600,fps:30,count:1,data:new Float32Array(1800),meta:{names:['root'],parents:[-1]}});
function e(x,node='edit-track'){return {clientX:x,currentTarget:$(node),button:0,pointerId:1,preventDefault(){},stopPropagation(){}}}
t.begin(e(80),'select');t.move(e(200));t.finish(e(200));assert.deepEqual(selected,[80,200]);
t.begin(e(200,'edit-trim-end'),'end');t.move(e(700));t.finish(e(700));assert.deepEqual(selected,[80,600]);assert.equal(frame,599);
t.setRange(100,200);t.begin(e(120,'edit-selection'),'move');t.move(e(590));t.finish(e(590));assert.deepEqual(selected,[500,600]);
t.begin(e(500,'edit-trim-start'),'start');t.move(e(599));t.cancel();assert.deepEqual(selected,[500,600]);
t.reviewRange={start_frame:100,end_frame:550};t.setRange(100,550);assert.match($('edit-range-label').textContent,/任务过渡区预览/);t.setRange(100,560);assert.match($('edit-range-label').textContent,/请选择 12–300/);t.setRange(500,600);
locked=true;t.begin(e(550),'select');assert.equal(t.drag,null);t.mark('start');assert.deepEqual(selected,[500,600]);locked=false;
t.setFrame(599);t.mark('end');assert.equal(t.end,600);t.setClip(null);assert.equal($('edit-selection').hidden,true);assert.equal($('edit-playhead').hidden,true);
console.log('PASS: drag selection, exclusive end, clamped move, cancel restoration, submission lock and reset');

vm.runInContext(fs.readFileSync(__dirname+'/../src/renderer/motion-view.js','utf8')+'\nthis.Preview=MotionView;',c);
for(const height of [-0.05,0.45]){
  const preview=new c.Preview.View({getContext:()=>({})},{thumbnail:true});
  preview.setClip({meta:{joint_count:2,frames:1,fps:30,parents:[-1,0],names:['root','foot']},
    data:new Float32Array([0,height+1,0,0,height,0])});
  assert.equal(preview.clip.floor,0,'The grid stays at world Y=0');
  assert.ok(Math.abs(preview.joint(0,1)[1]-height)<1e-6,'The clip retains its world-space joint height');
}
