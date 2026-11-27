'use strict';
// MuJoCo compiled vertices already include mesh centering/scaling. The matching
// geom_xpos / geom_xmat transforms are exported by the same model, in Z-up space.
const robotViewer={request:0,renderer:null,meshes:[],version:null,robot:null,
 async load(clip){
  const request=++this.request,robot=clip.metadata.robot||'unitree_g1_23dof';
  if(robot!==this.robot||clip.version!==this.version)await this.initialize(robot,request);
  if(request!==this.request)return false;
  if(clip.version!==this.version||clip.poses[0]?.length!==this.meshes.length)throw new Error('机器人模型与动作版本不匹配');
  return true;
 },
 async initialize(robot,request){
  const query='?robot='+encodeURIComponent(robot);
  const manifest=await api('/api/retarget/model'+query);
  const response=await fetch('/api/retarget/mesh-data'+query,{credentials:'same-origin'});
  if(!response.ok)throw new Error('实体模型加载失败，请刷新后重试');
  const binary=await response.arrayBuffer();
  if(request!==this.request)return;
  if(this.scene){
   const geometries=new Set(),materials=new Set();
   this.scene.traverse(object=>{if(object.geometry)geometries.add(object.geometry);if(object.material)materials.add(object.material);});
   geometries.forEach(item=>item.dispose());materials.forEach(item=>item.dispose());this.light?.shadow?.map?.dispose();
  }
  if(!this.renderer){
   const renderer=new THREE.WebGLRenderer({canvas:$('retarget-canvas'),antialias:true,alpha:false});
   renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
   renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1;
   this.renderer=renderer;
  }
  const scene=new THREE.Scene();scene.background=new THREE.Color('#fafafa');scene.fog=new THREE.Fog('#fafafa',12,40);
  const camera=new THREE.PerspectiveCamera(38,1,.03,100);camera.up.set(0,0,1);
  const ambient=new THREE.HemisphereLight(0xffffff,0xdfe4e8,1.1);ambient.position.set(0,0,1);scene.add(ambient);
  const light=new THREE.DirectionalLight(0xffffff,2);light.castShadow=true;light.shadow.mapSize.set(1024,1024);
  light.shadow.camera.left=-2.5;light.shadow.camera.right=2.5;light.shadow.camera.top=2.5;light.shadow.camera.bottom=-2.5;
  light.shadow.camera.near=.1;light.shadow.camera.far=12;light.shadow.bias=-.0002;light.shadow.normalBias=.01;light.shadow.radius=3;
  scene.add(light,light.target);
  const fill=new THREE.DirectionalLight(0xddeaff,.7);fill.position.set(-3,-4,5);scene.add(fill);
  const floor=new THREE.Mesh(new THREE.PlaneGeometry(200,200),new THREE.MeshStandardMaterial({color:0xffffff,roughness:1,metalness:0}));
  floor.position.z=-.005;floor.receiveShadow=true;scene.add(floor);
  const geometries={};
  for(const [key,info] of Object.entries(manifest.meshes)){
   const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(new Float32Array(binary,info.vertex_offset,info.vertices*3),3));
   geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(binary,info.index_offset,info.indices),1));geometry.computeVertexNormals();geometry.computeBoundingSphere();geometries[key]=geometry;
  }
  const meshes=manifest.geoms.map(info=>{
   // Match desktop: every robot uses a dark gray preview material.
   const color=new THREE.Color(0x454b52);
   const geometry=info.mesh===null?new THREE.BoxGeometry(...info.size.map(x=>x*2)):geometries[info.mesh];
   const mesh=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color,metalness:.08,roughness:.7}));
   mesh.name=info.body;mesh.castShadow=true;mesh.receiveShadow=true;scene.add(mesh);return mesh;
  });
  Object.assign(this,{scene,camera,light,meshes,version:manifest.version,robot});
 },
 render(clip,index,s){
  if(!this.renderer)return;
  const canvas=$('retarget-canvas'),width=canvas.clientWidth,height=canvas.clientHeight;if(!width||!height)return;
  this.renderer.setSize(width,height,false);this.camera.aspect=width/height;this.camera.updateProjectionMatrix();
  const poses=clip.poses[index];
  for(let i=0;i<this.meshes.length;i++){
   const p=poses[i];this.meshes[i].position.set(p[0],p[1],p[2]);this.meshes[i].quaternion.set(p[3],p[4],p[5],p[6]);
  }
  // Follow translation, keeping the full robot large enough to inspect.
  const root=clip.roots[index],x=root[0],y=root[1],z=.8,distance=Math.max(1.5,3.2/s.zoom);
  this.camera.position.set(x+Math.cos(s.yaw)*Math.cos(s.pitch)*distance,y+Math.sin(s.yaw)*Math.cos(s.pitch)*distance,z+Math.sin(s.pitch)*distance);
  this.camera.lookAt(x,y,z);this.light.position.set(x+3,y-2,6);this.light.target.position.set(x,y,.6);
  this.renderer.render(this.scene,this.camera);
 },
 clear(){this.request++;if(this.renderer){this.renderer.clear();}}
};
