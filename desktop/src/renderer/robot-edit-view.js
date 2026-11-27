'use strict';
// The same physical mesh renderer as the retarget workspace; FK runs locally on slider input.
class RobotEditView extends RobotView.View {
 bindInput(){
  const c=this.canvas;
  c.addEventListener('pointerdown',e=>{
   if(e.button!==0||!e.buttons||this.suspended||this.drag||!this.camera)return;
   const target=this.target?.visible&&this.ray(e).intersectObject(this.target).length;
   c.setPointerCapture(e.pointerId);
   this.drag={id:e.pointerId,x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,
    moved:false,target,targetUpdated:false,plane:target?this.targetPlane():null};
  });
  const cancelDrag=e=>{
   const drag=this.drag;if(drag?.id!==e.pointerId)return;
   this.stopDrag();if(drag.targetUpdated)this.onCancel?.();
  };
  c.addEventListener('pointermove',e=>{
   const drag=this.drag;if(drag?.id!==e.pointerId)return;
   if(!(e.buttons&1)){cancelDrag(e);return;}
   const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
   drag.moved=drag.moved||Math.abs(e.clientX-drag.startX)+Math.abs(e.clientY-drag.startY)>2;
   if(drag.target){
    if(!drag.moved)return;
    const hit=this.ray(e).ray.intersectPlane(drag.plane,new THREE.Vector3());
    if(hit&&this.onTarget?.(hit.toArray()))drag.targetUpdated=true;
   }
   else{this.yaw+=dx*.008;this.pitch=Math.max(-.3,Math.min(1.4,this.pitch+dy*.008));this.dirty=true;}
   drag.x=e.clientX;drag.y=e.clientY;
  });
  c.addEventListener('pointerup',e=>{
   const drag=this.drag;if(drag?.id!==e.pointerId)return;
   this.stopDrag();
   if(drag.target){if(drag.targetUpdated)this.onTargetEnd?.();}
   else if(!drag.moved){const hit=this.ray(e).intersectObjects(this.meshes)[0];if(hit)this.onBody?.(hit.object.name);}
  });
  c.addEventListener('pointercancel',cancelDrag);
  c.addEventListener('lostpointercapture',cancelDrag);
  c.addEventListener('wheel',e=>{e.preventDefault();this.zoom=Math.max(.3,Math.min(4,this.zoom*Math.exp(-e.deltaY*.001)));this.dirty=true;},{passive:false});
  c.addEventListener('dblclick',()=>this.resetCamera());
 }
 ray(e){const r=this.canvas.getBoundingClientRect(),ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2((e.clientX-r.left)/r.width*2-1,1-(e.clientY-r.top)/r.height*2),this.camera);return ray;}
 targetPlane(){const normal=this.plane==='01'?new THREE.Vector3(0,0,1):this.plane==='12'?new THREE.Vector3(1,0,0):new THREE.Vector3(0,1,0);return new THREE.Plane().setFromNormalAndCoplanarPoint(normal,this.target.position);}
 async setup(data){
  this.rig=data.rig;this.modelData=data;
  this.rootBody=Math.max(0,this.rig.bodies.findIndex(body=>body.joints.some(joint=>joint.type===0)));
  this.loaders={model:async()=>data.manifest,meshData:async()=>Uint8Array.from(atob(data.binary),c=>c.charCodeAt(0)).buffer};
  const request=++this.request;await this.initialize('editor',request);if(request!==this.request)return;
  this.ghosts=this.meshes.map(mesh=>{const ghost=new THREE.Mesh(mesh.geometry,new THREE.MeshBasicMaterial({color:0x8e8a82,transparent:true,opacity:.25,wireframe:true,depthWrite:false}));this.scene.add(ghost);return ghost;});
  this.target=new THREE.Mesh(new THREE.SphereGeometry(.025,16,12),new THREE.MeshBasicMaterial({color:0x8a6416,depthTest:false}));this.target.renderOrder=5;this.scene.add(this.target);
  this.axis=new THREE.ArrowHelper(new THREE.Vector3(0,0,1),new THREE.Vector3(),.22,0x8a6416);this.scene.add(this.axis);
  this.resetCamera();
 }
 fitModel(poses,root){
  const bounds=new THREE.Box3(),part=new THREE.Box3(),transform=new THREE.Matrix4();
  const position=new THREE.Vector3(),rotation=new THREE.Quaternion(),scale=new THREE.Vector3(1,1,1);
  this.meshes.forEach((mesh,index)=>{
   const [x,y,z,qx,qy,qz,qw]=poses[index],geometry=mesh.geometry;
   if(!geometry.boundingBox)geometry.computeBoundingBox();
   transform.compose(position.set(x,y,z),rotation.set(qx,qy,qz,qw),scale);
   bounds.union(part.copy(geometry.boundingBox).applyMatrix4(transform));
  });
  const center=bounds.getCenter(new THREE.Vector3()),radius=bounds.getSize(new THREE.Vector3()).length()/2;
  this.framing={offset:center.toArray().map((value,index)=>value-root[index]),radius};
  this.target.scale.setScalar(radius/.8);
  this.axis.scale.setScalar(radius/.8);
 }
 pose(q,original,selectedJoint,body,targetOffset,overlay,plane,ghostQ){
  if(!this.rig||!this.target)return;const fk=RobotFK.forward(this.rig,q),base=RobotFK.forward(this.rig,original);this.lastFK=fk;this.baseFK=base;this.plane=plane;
  const root=fk.bodies[this.rootBody].pos;
  // Keep the fit stable during playback while the camera follows translation of the root.
  if(!this.framing)this.fitModel(fk.poses,root);
  const center=RobotFK.add(root,this.framing.offset);
  this.clip={frames:1,fps:30,poses:[fk.poses],roots:[center],centerZ:center[2],cameraBoundsRadius:this.framing.radius,metadata:{robot:'editor'}};
  const ghost=ghostQ?RobotFK.forward(this.rig,ghostQ):base;this.frame=0;this.playing=false;
  this.ghosts.forEach((g,i)=>{const p=(ghostQ?ghost:base).poses[i];g.position.set(...p.slice(0,3));g.quaternion.set(...p.slice(3));g.visible=overlay&&p.some((v,k)=>Math.abs(v-fk.poses[i][k])>1e-5);});
  const b=this.rig.bodies.findIndex(b=>b.name===body);this.target.visible=b>=0;
  if(b>=0)this.target.position.fromArray(RobotFK.add(base.bodies[b].pos,targetOffset));
  const index=this.rig.bodies.findIndex(b=>b.joints.some(j=>j.name===selectedJoint)),joint=this.rig.bodies[index]?.joints.find(j=>j.name===selectedJoint);this.axis.visible=!!joint;
  if(joint){const transform=fk.bodies[index];this.axis.position.fromArray(RobotFK.add(transform.pos,RobotFK.rotate(transform.quat,joint.pos)));this.axis.setDirection(new THREE.Vector3(...RobotFK.rotate(transform.quat,joint.axis)).normalize());}
  this.meshes.forEach((m,i)=>{m.material.color.setHex(this.rig.geoms[i].body_id===index?0x8a6416:0x57544e);});this.dirty=true;
 }
 clear(){super.clear();this.rig=null;this.framing=null;}
}
