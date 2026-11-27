'use strict';
// Compiled MuJoCo local transforms. Quaternion order is w,x,y,z until the render boundary.
const RobotFK=(()=>{
 const add=(a,b)=>a.map((v,i)=>v+b[i]),sub=(a,b)=>a.map((v,i)=>v-b[i]);
 const mul=(a,b)=>[a[0]*b[0]-a[1]*b[1]-a[2]*b[2]-a[3]*b[3],a[0]*b[1]+a[1]*b[0]+a[2]*b[3]-a[3]*b[2],a[0]*b[2]-a[1]*b[3]+a[2]*b[0]+a[3]*b[1],a[0]*b[3]+a[1]*b[2]-a[2]*b[1]+a[3]*b[0]];
 const rotate=(q,v)=>mul(mul(q,[0,...v]),[q[0],-q[1],-q[2],-q[3]]).slice(1);
 function forward(rig,q){
  if(q.length!==rig.nq||!q.every(Number.isFinite))throw Error('机器人姿态数据无效');
  const bodies=[];
  rig.bodies.forEach((b,i)=>{
   let r=i?mul(bodies[b.parent].quat,b.quat):[1,0,0,0],p=i?add(bodies[b.parent].pos,rotate(bodies[b.parent].quat,b.pos)):[0,0,0];
   for(const j of b.joints){
    if(j.type===0){p=q.slice(j.index,j.index+3);r=q.slice(j.index+3,j.index+7);const norm=Math.hypot(...r);if(norm<1e-8)throw Error('根旋转无效');r=r.map(x=>x/norm);continue;}
    const value=q[j.index]-j.ref;
    if(j.type===2)p=add(p,rotate(r,j.axis).map(x=>x*value));
    else if(j.type===3){const anchor=add(p,rotate(r,j.pos));r=mul(r,[Math.cos(value/2),...j.axis.map(x=>x*Math.sin(value/2))]);p=sub(anchor,rotate(r,j.pos));}
    else throw Error('不支持的机器人关节');
   }
   bodies.push({pos:p,quat:r});
  });
  return {bodies,poses:rig.geoms.map(g=>{const b=bodies[g.body_id],p=add(b.pos,rotate(b.quat,g.pos)),r=mul(b.quat,g.quat);return [...p,r[1],r[2],r[3],r[0]];})};
 }
 function offsets(keys,start,end,frame){
  const result={joint_offsets:{},body_offsets:{},root_offset:[0,0,0]};if(frame<start+2||frame>end-3)return result;
  const blank=f=>({frame:f,joint_offsets:{},body_offsets:{},root_offset:[0,0,0]});const points=[blank(start+2),...keys.slice().sort((a,b)=>a.frame-b.frame),blank(end-3)];
  if(points.length<3)return result;
  let i=0;while(i<points.length-2&&frame>=points[i+1].frame)i++;
  const a=points[i],b=points[i+1],u=Math.max(0,Math.min(1,(frame-a.frame)/(b.frame-a.frame))),t=u*u*u*(10+u*(-15+6*u));
  for(const n of new Set(keys.flatMap(k=>Object.keys(k.joint_offsets))))result.joint_offsets[n]=(a.joint_offsets[n]||0)*(1-t)+(b.joint_offsets[n]||0)*t;
  for(const n of new Set(keys.flatMap(k=>Object.keys(k.body_offsets))))result.body_offsets[n]=[0,1,2].map(i=>(a.body_offsets[n]?.[i]||0)*(1-t)+(b.body_offsets[n]?.[i]||0)*t);
  result.root_offset=[0,1,2].map(i=>a.root_offset[i]*(1-t)+b.root_offset[i]*t);return result;
 }
 return {forward,offsets,rotate,add};
})();
if(typeof module!=='undefined')module.exports=RobotFK;
