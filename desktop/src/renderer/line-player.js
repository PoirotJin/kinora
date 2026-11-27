'use strict';
(() => {
  const el = id => document.getElementById(id);
  const T = window.THREE;
  const colors = [0x0b0b0b,0x8a6416,0x2e5b3c,0x57544e,0x7a5c3e,0x7d1e1e];
  const eventNames = [['fan','风机到位'],['seated','模块落座'],['clear','料车放行'],['receive','接收 A'],['ship','成品出站']];
  const viewport = el('view');
  let renderer, scene, camera, pack, parts, materialProps, machinery, labels = [], actorTags = [], playing = false;
  let machine = null, junction = null;
  let frame = 0, previous = 0, turn = -0.10, zoom = 1, focus = 'all';
  const matrix = T ? new T.Matrix4() : null;
  const pos = T ? new T.Vector3() : null, rot = T ? new T.Quaternion() : null, unit = T ? new T.Vector3(1,1,1) : null;

  function unpack(text, Type) {
    const data = atob(text), bytes = Uint8Array.from(data, c => c.charCodeAt(0));
    return new Type(bytes.buffer);
  }
  function loadPack(data) {
    const crossing=data.format==='kinora-junction-1', handoff=data.format==='kinora-machine-exchange-1',cell=data.format==='kinora-machine-cell-1',shortScene=crossing||handoff||cell;
    if (!T || (!shortScene && !['kinora-factory-line-1','kinora-factory-line-2'].includes(data.format)) || data.fps !== 30 || (cell?(!Number.isInteger(data.duration)||data.duration<1||data.duration>120):data.duration!==(shortScene?15:90)) ||
        !Array.isArray(data.actors) || !data.actors.length || data.actors.length > 50) throw Error('不支持的协作回放格式');
    const n = data.model.geoms.length;
    if (n < 1 || n > 128 || (!shortScene && (!Array.isArray(data.stations) || data.stations.length !== 6))) throw Error('无效工位或模型');
    const actors = data.actors.map(a => {
      if (!Number.isInteger(a.frames) || a.frames < 2 || a.frames > 3600 || a.poses.length !== Math.ceil(a.frames*n*14/3)*4 ||
          !Number.isInteger(a.station) || a.station < 0 || a.station >= 6 || !a.starts.every(Number.isFinite)) throw Error('角色轨道无效');
      return {...a, samples:unpack(a.poses,Int16Array)};
    });
    if(crossing && (actors.length!==4 || actors.some(a=>a.frames!==450) || data.objects.length!==2 ||
       data.objects.some(o=>o.trajectory.length!==450 || o.trajectory.some(p=>p.length!==4 || !p.every(Number.isFinite))) ||
       !['clear','release','stop'].every(k=>Number.isInteger(data.events[k]) && data.events[k]>=0 && data.events[k]<450))) throw Error('路口轨迹无效');
    if(handoff && (actors.some(a=>a.frames!==450) || !Array.isArray(data.object_track) || data.object_track.length!==450 ||
       data.object_track.some(row=>!Array.isArray(row)||row.length!==4||!row.every(Number.isFinite)) ||
       !Array.isArray(data.cues)||!data.cues.length||!Array.isArray(data.props)||!Array.isArray(data.floor))) throw Error('设备交接轨迹无效');
    if(cell){
      const last=data.duration*30;
      const rows=(v,width)=>Array.isArray(v)&&v.length===last&&v.every(p=>Array.isArray(p)&&p.length===width&&p.every(Number.isFinite));
      if(!rows(data.camera_track,3)||!Array.isArray(data.equipment)||!data.equipment.length||
         data.equipment.some(o=>!rows(o.track,4)||!Array.isArray(o.parts)||(o.height&&(o.height.length!==last||!o.height.every(Number.isFinite))))||
         !Array.isArray(data.floor)||!Array.isArray(data.cues)||!data.cues.length||
         actors.some(a=>!Number.isInteger(a.start_frame)||a.start_frame<0||a.start_frame+a.frames>last))throw Error('联试区场景轨道无效');
    }
    if (!renderer) {
      renderer = new T.WebGLRenderer({canvas:viewport,antialias:true});
      renderer.setPixelRatio(Math.min(2,devicePixelRatio)); renderer.setClearColor(0xebe8e1);
      camera = new T.OrthographicCamera(-18,18,12,-12,.01,180); camera.up.set(0,0,1);
    }
    if (scene) scene.traverse(o => {o.geometry?.dispose(); o.material?.dispose?.();});
    scene = new T.Scene(); scene.add(new T.HemisphereLight(0xffffff,0xd9d3c8,1.35));
    const light = new T.DirectionalLight(0xffffff,1.45); light.position.set(-6,-5,18);scene.add(light);
    pack = {...data,actors};
    const geo = {};
    for (const [k,m] of Object.entries(data.model.meshes)) {
      const g=new T.BufferGeometry(), v=unpack(m.vertices,Int16Array);
      g.setAttribute('position',new T.BufferAttribute(Float32Array.from(v,x => x/1000),3));
      g.setIndex(new T.BufferAttribute(unpack(m.indices,Uint16Array),1)); g.computeVertexNormals(); geo[k]=g;
    }
    parts=data.model.geoms.map(g => {
      const mesh=new T.InstancedMesh(geo[g.mesh],new T.MeshStandardMaterial({color:new T.Color(...g.color),roughness:.68,metalness:.17}),actors.length);
      mesh.frustumCulled=false; mesh.instanceMatrix.setUsage(T.DynamicDrawUsage);scene.add(mesh);return mesh;
    });
    function box(x,y,z,w,d,h,color) {
      const m=new T.Mesh(new T.BoxGeometry(w,d,h),new T.MeshStandardMaterial({color,roughness:.86}));
      m.position.set(x,y,z);scene.add(m);return m;
    }
    el('variant').parentElement.style.display=shortScene?'none':'';
    el('variant').selectedIndex=0;el('time').max=data.duration*data.fps-1;
    el('speed').replaceChildren(...(shortScene?[.5,1,2]:[1,2,4]).map(v=>new Option(String(v),String(v))));el('speed').value='1';
    document.querySelector('h1').textContent=(handoff||cell)?data.title:crossing?'中央路口 · 双机协同转运':'空调总装车间';
    document.title=document.querySelector('h1').textContent+' · Kinora';
    machine=null;junction=null;
    if(handoff||cell){
      machine=setupMachine(box);frame=0;focus='all';zoom=1;turn=-.46;playing=false;
      el('play').disabled=false;update();return;
    }
    if(crossing){
      junction=setupJunction(box);frame=0;focus='all';zoom=1;turn=-.57;playing=false;
      el('play').disabled=false;update();return;
    }
    turn=-.10;
    box(0,0,-.13,32,18,.18,0xe2ded5);
    box(0,0,-.027,29,1.35,.025,0xd6d1c7);
    const tags=el('labels');tags.replaceChildren();labels=[];
    el('focus').replaceChildren(new Option('全车间','all'));
    data.stations.forEach(([name,x,z],s) => {
      box(x,-z,-.055,7.7,6.0,.09,0xf3f1ec);box(x,-z+2.94,.005,7.7,.075,.025,colors[s]);
      for(let pane=0;pane<2;pane++){
        const tag=document.createElement('span');tag.className='tag';tag.textContent=name;tags.append(tag);
        labels.push([tag,new T.Vector3(x,-z+2.9,.2),s,pane]);
      }
      el('focus').add(new Option(name,String(s)));
    });
    if(data.format==='kinora-factory-line-2')el('focus').add(new Option('机组 A 交接','dock'));
    actorTags=actors.map(a=>[0,1].map(()=>{
      const tag=document.createElement('span');tag.className='tag person';tag.textContent=a.id+' '+a.label;tag.hidden=true;tags.append(tag);return tag;
    }));
    function product(x,y,color) {
      const g=new T.Group();scene.add(g);g.position.set(x,y,0);
      function piece(w,d,h,xx,yy,zz,shade) {
        const b=new T.Mesh(new T.BoxGeometry(w,d,h),new T.MeshStandardMaterial({color:shade}));b.position.set(xx,yy,zz);g.add(b);
      }
      piece(1.65,.83,.12,0,0,.15,0x51616b);piece(1.43,.68,.55,0,0,.48,color);piece(1.45,.71,.05,0,0,.78,0xc7d6da);
      for(const xx of [-.69,.69]) for(const yy of [-.31,.31])piece(.09,.10,.14,xx,yy,.075,0x32414b);
      for(let k=0;k<6;k++)piece(.04,.025,.29,-.43+.085*k,-.35,.51,0x566e77);
      piece(.25,.025,.07,.42,-.35,.61,0xd9e9e9);
      return g;
    }
    if(data.format==='kinora-factory-line-2'){
      machinery={};
      for(const eq of data.equipment){
        const group=new T.Group();scene.add(group);machinery[eq.id]=group;
        const [w,d,h]=eq.size;
        const piece=(x,y,z,sx,sy,sz,color)=>{
          const m=box(x,y,z,sx,sy,sz,color);group.add(m);return m;
        };
        const steel=0x576e78, white=0xc0d3d3, copper=0xc98039;
        for(const x of [-w*.43,w*.43])for(const y of [-d*.36,d*.36])
          piece(x,y,.13,.10,.09,.24,steel);
        piece(0,0,.24,w,d,.09,steel);
        if(eq.kind==='frame'||eq.kind==='bench'){
          for(const x of [-w*.43,w*.43])for(const y of [-d*.36,d*.36])
            piece(x,y,(h+.24)/2,.055,.055,h-.24,steel);
          if(eq.kind==='bench')piece(0,0,h-.025,w,d,.05,white);
          else {
            piece(0,0,.59,w,d,.045,steel);
            for(const x of [-w*.43,w*.43])piece(x,0,h-.025,.08,d,.05,white);
          }
        } else {
          piece(0,0,(h+.25)/2,w*.98,d*.97,h-.25,eq.kind==='crate'?0xba9c72:0x8aafb3);
          piece(0,0,h,w,d,.035,white);
          for(let k=0;k<5;k++)piece(w*.22+k*.055,-d*.49,h*.60,.024,.012,.13,steel);
        }
        for(const port of eq.ports){
          const [x,y,z]=port.local,[nx,ny]=port.normal;
          const thick=.018, width=port.handle?.061:.15, height=port.handle?.048:.15;
          const reach=Math.max(.07,Math.abs(x*nx+y*ny)-(Math.abs(nx)*w+Math.abs(ny)*d)/2+.015);
          piece(x-nx*reach/2,y-ny*reach/2,z,Math.abs(nx)*reach+Math.abs(ny)*.047,
                Math.abs(ny)*reach+Math.abs(nx)*.047,.045,steel);
          if(z>h+.02)piece(x-nx*reach,y-ny*reach,(h+z)/2,.035,.035,z-h,steel);
          piece(x-nx*thick/2,y-ny*thick/2,z,Math.abs(nx)*thick+Math.abs(ny)*width,
                Math.abs(ny)*thick+Math.abs(nx)*width,height,copper);
        }
        if(eq.driver){
          const handle=eq.ports.filter(p=>p.handle);
          if(handle.length===2){
            const [p,q]=handle.map(p=>p.local),[nx,ny]=handle[0].normal;
            piece((p[0]+q[0])/2-nx*.018,(p[1]+q[1])/2-ny*.018,p[2],
                  Math.abs(p[0]-q[0])+.026,Math.abs(p[1]-q[1])+.026,.032,steel);
          }
        }
        group.position.set(...eq.origin,0);
      }
      const receiving={crossbeam:'frame',drain_pan:'rail',fan_A:'A',panel_A:'A'};
      for(const o of data.objects){
        const eq=data.equipment.find(e=>e.id===receiving[o.id]),dest=o.trajectory.at(-1);
        const dock=machinery[eq.id],motion=eq.travel?.at(-1)||[0,0];
        const delta=o.id==='panel_A'?motion:[0,0];
        for(const [stop,isDock] of [[o.trajectory[0],false],[dest,true]]){
          const top=stop[3]-o.size[2]/2;
          const pad=box(stop[1],stop[2],top-.026,o.size[0]*1.02,o.size[1]*.76,.052,0x617f87);
          pad.rotation.z=o.heading*Math.PI/180;
          if(isDock){
            dock.add(pad);pad.position.x-=eq.origin[0]+delta[0];pad.position.y-=eq.origin[1]+delta[1];
            const y=pad.position.y, edge=Math.sign(y)*Math.min(Math.abs(y),eq.size[1]*.45);
            const bracket=box(pad.position.x,(y+edge)/2,top-.077,.12,Math.max(.12,Math.abs(y-edge)+.08),.07,0x576e78);
            dock.add(bracket);
          }
          else box(stop[1],stop[2],top/2-.025,.18,.18,Math.max(.06,top-.05),0x82999c);
        }
      }
      // The previous unit leaves on the adjacent powered outfeed.
      machinery.outfeed=product(12.35,-2.3,0xaebebe);machinery.outfeed.scale.set(.75,.72,.8);
      box(13.15,-2.3,.05,3.15,.8,.08,0x657c85);
    }else{
    machinery={A:product(0,0,0x7faeb1),B:product(0,0,0xb7c6c4),C:product(0,0,0xbc9868),cart:product(0,0,0xafb99c)};
    machinery.cart.scale.set(.56,.65,.7);
    product(-9,4,0xb1b9ae);product(0,-4,0xc2c9bb);
    // Open jig for the frame station and a low parts rack behind each work cell.
    function rack(x,y,w,levels) {
      for (const dx of [-w/2,w/2]) for (const dy of [-.21,.21]) box(x+dx,y+dy,.68,.055,.055,1.36,0x506571);
      for(let i=0;i<levels;i++)box(x,y,.20+i*.51,w+.08,.50,.055,0x8b9fa4);
    }
    rack(0,4,1.45,2);
    data.stations.forEach(([,x,z],s)=>{
      const rx=x-2.7,ry=-z+(s<3?2.48:-2.48);
      rack(rx,ry,1.45,3);
      for(let k=0;k<3;k++)box(rx-.44+k*.44,ry,.88,.35,.32,.25,colors[s]);
      for(let k=0;k<4;k++)box(x+1.1+k*.28,-z+(s<3?-2.9:2.9),.006,.16,.32,.026,0xc7a454);
    });
    }
    for(let i=-13;i<=13;i+=2)box(i,.68,-.003,.82,.06,.02,0xd3be83);
    materialProps=data.objects.map(o=>box(0,0,0,...o.size,0xd68637));
    el('events').replaceChildren();
    for(const [key,label] of eventNames){
      const b=document.createElement('button');b.textContent=label;
      b.onclick=()=>seek(events()[key]*30);el('events').append(b);
    }
    el('summary').textContent=actors.length+' 台 Unitree G1 · 六个工位 · 90 秒 · motionfarm 重定向回放';
    el('play').disabled=false;focus='all';zoom=1;frame=0;playing=false;update();
  }
  function setupMachine(box){
    labels=[];actorTags=[];const tags=el('labels');tags.replaceChildren();
    const lift=new T.Group();scene.add(lift);
    function add(part,parent,wheels){
      const [x,y,z]=part.p,s=part.size;
      let m;
      if(part.shape==='box')m=box(x,y,z,...s,part.color);
      else{
        m=new T.Mesh(new T.CylinderGeometry(s[0],s[0],s[1],32),new T.MeshStandardMaterial({color:part.color,roughness:.58,metalness:.22}));
        m.position.set(x,y,z);
        if(part.axis==='z')m.rotation.x=Math.PI/2;
        if(part.axis==='x')m.rotation.z=Math.PI/2;
      }
      parent.add(m);
      if(part.spin && s[0]>.09){
        const pivot=new T.Group();parent.add(pivot);pivot.position.copy(m.position);
        for(const side of [-1,1]){
          const marker=box(.035,side*(s[1]/2+.002),.061,.019,.005,.030,0xacb9b8);pivot.add(marker);
        }
        wheels.push(pivot);
      }
    }
    pack.floor.forEach(p=>add(p,p.lift?lift:scene,[]));
    const equipment=(pack.equipment||[{id:pack.key,track:pack.object_track,parts:pack.props}]).map(asset=>{
      const group=new T.Group(),wheels=[],distance=[0];scene.add(group);
      asset.parts.forEach(p=>add(p,group,wheels));
      for(let j=1;j<asset.track.length;j++){
        const a=asset.track[j],b=asset.track[j-1];
        distance.push(distance[j-1]+Math.hypot(a[0]-b[0],a[1]-b[1]));
      }
      return {asset,group,wheels,distance};
    });
    const shadow=pack.actors.map(()=>{
      const m=new T.Mesh(new T.CircleGeometry(1,28),new T.MeshBasicMaterial({color:0x57544e,transparent:true,opacity:.22,depthWrite:false}));
      m.scale.set(.24,.18,1);scene.add(m);return m;
    });
    const tagsByActor=pack.actors.map((a,i)=>{
      const e=document.createElement('span');e.className='tag';e.textContent=a.label;
      e.style.borderLeft='4px solid '+((pack.key==='pump'?i>=2:pack.key==='cabinet'&&i===1)?'#8a6416':'#2e5b3c');
      tags.append(e);return e;
    });
    el('summary').textContent=pack.actors.length+' 台 Unitree G1 · 机器转运 · '+pack.duration+' 秒';
    el('focus').replaceChildren(new Option('协作全景','all'),new Option('设备近景','machine'),new Option('俯视交接','top'));
    if(pack.camera_track)el('focus').replaceChildren(new Option('跟随工序','all'),new Option('全场俯视','top'),new Option('跟随电机','motor'),new Option('跟随泵组','pump'),new Option('跟随电柜','cabinet'));
    el('events').replaceChildren();
    for(const cue of pack.cues){
      const b=document.createElement('button');b.textContent=cue.label;b.onclick=()=>seek(cue.frame);el('events').append(b);
    }
    return {equipment,lift,shadow,tagsByActor};
  }
  function renderMachine(left,w,h){
    const j=Math.min(pack.duration*30-1,Math.round(frame));
    for(const {asset,group,wheels,distance} of machine.equipment){
      const [x,y,z,a]=asset.track[j];group.position.set(x,y,z);group.rotation.z=a;
      if(asset.height)group.scale.z=asset.height[j];
      wheels.forEach(wheel=>wheel.rotation.y=distance[j]/.105);
    }
    const raise=Math.max(0,Math.min(1,(j/30-8.85)/.90));
    machine.lift.scale.z=Math.max(.001,raise*raise*(3-2*raise));
    machine.lift.position.z=-.035*(1-machine.lift.scale.z);
    const samples=pack.actors.map(a=>Math.max(0,Math.min(a.frames-1,j-(a.start_frame||0))));
    const roots=pack.actors.map((a,i)=>{
      const u=samples[i]*parts.length*7;return [a.samples[u]/1000,a.samples[u+1]/1000];
    });
    const selected=machine.equipment.find(e=>e.asset.id===focus);
    const [ox,oy,,yaw]=(selected||machine.equipment[0]).asset.track[j];
    const xs=[ox,...roots.map(r=>r[0])],ys=[oy,...roots.map(r=>r[1])];
    const detail=focus==='machine'||!!selected,top=focus==='top',aspect=w/Math.max(h,1);
    let cx=detail?ox:(Math.min(...xs)+Math.max(...xs))/2,cy=detail?oy:(Math.min(...ys)+Math.max(...ys))/2;
    const diag=Math.hypot(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys));
    let half=detail?Math.max(1.05,1.72/aspect):Math.max(1.56,1.08+diag*.24,(.9+diag*.52)/aspect);
    if(pack.camera_track){
      if(top){cx=2.2;cy=1.6;half=Math.max(6.5,7.7/aspect);}
      else if(selected)half=Math.max(1.55,2.35/aspect);
      else{
        [cx,cy,half]=pack.camera_track[j];
        half=Math.max(half,half*1.55/aspect);
      }
    }
    half/=zoom;
    camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;
    const tilt=top?.022:1;
    const viewAngle=turn+(pack.key==='cabinet'&&!top?.99+.58*yaw:0);
    camera.position.set(cx+tilt*Math.sin(viewAngle)*10,cy-tilt*Math.cos(viewAngle)*10,top?21:6.8);
    camera.lookAt(cx,cy,.63);camera.updateProjectionMatrix();camera.updateMatrixWorld();
    for(const [i,a] of pack.actors.entries()){
      const s=a.samples;
      for(let k=0;k<parts.length;k++){
        const u=(samples[i]*parts.length+k)*7;
        pos.set(s[u]/1000,s[u+1]/1000,s[u+2]/1000);
        rot.set(s[u+3]/32767,s[u+4]/32767,s[u+5]/32767,s[u+6]/32767).normalize();
        matrix.compose(pos,rot,unit);parts[k].setMatrixAt(i,matrix);
      }
      machine.shadow[i].position.set(...roots[i],.002);
      const tag=machine.tagsByActor[i],overview=top&&!!pack.camera_track;
      let labelPoint=[...roots[i],1.57],show=!detail;
      if(overview){
        const asset=machine.equipment[a.station].asset;
        labelPoint=asset.track[j].slice(0,3);
        show=i===pack.actors.findIndex(other=>other.station===a.station);
        tag.textContent=['电机','泵组','电控柜'][a.station];
      }else{
        tag.textContent=a.label;
        if(pack.camera_track){
          const stage=machine.equipment[a.station].asset.track[j];
          show=show&&Math.hypot(stage[0]-cx,stage[1]-cy)<2.1&&j>80&&j<pack.duration*30-75;
        }
      }
      const v=pos.set(...labelPoint).project(camera);
      tag.hidden=!show||Math.abs(v.x)>.95||Math.abs(v.y)>.95;
      tag.style.left=(left+(v.x+1)*w/2)+'px';tag.style.top=((1-v.y)*h/2-(overview?25:0))+'px';
    }
    parts.forEach(m=>m.instanceMatrix.needsUpdate=true);renderer.render(scene,camera);
    let cue=pack.cues[0];for(const c of pack.cues)if(c.frame<=j)cue=c;
    return cue.text;
  }

  function setupJunction(box) {
    const tags=el('labels');tags.replaceChildren();labels=[];actorTags=[];
    const steel=0x57544e, pale=0xd6d1c7, green=0x2e5b3c, amber=0x8a6416;
    box(-1,0,-.14,18,18,.20,0xe2ded5);
    box(-3.1,0,-.046,6.3,3.2,.065,0x8e8a82);
    box(0,.2,-.045,3.3,15,.065,0x8e8a82);
    for(const y of [-1.57,1.57])box(-4.03,y,-.009,4.9,.06,.012,0xeee8ba);
    for(const x of [-1.61,1.61])
      for(const [y,length] of [[-4.47,5.8],[4.47,5.8]])box(x,y,-.009,.06,length,.012,0xeee8ba);
    // Low protective rails frame the intersection without hiding the feet.
    for(const side of [-1,1])for(const sign of [-1,1]){
      const x=side*1.95,y=sign*3.22;
      if(side<0 && sign>0)continue;
      for(let k=-1;k<=1;k++)box(x,y+k*1.05,.27,.075,.075,.55,steel);
      box(x,y,.50,.075,2.25,.07,0xd3a246);
    }
    const stop=box(0,-.78,-.002,2.5,.12,.012,0xf5deb2);
    const route=(a,b,color)=>{
      const x=b[0]-a[0],y=b[1]-a[1],m=box((a[0]+b[0])/2,(a[1]+b[1])/2,-.005,Math.hypot(x,y),.037,.010,color);
      m.rotation.z=Math.atan2(y,x);
    };
    for(const [i,obj] of pack.objects.entries())
      for(let j=0;j<obj.trajectory.length-9;j+=16){
        const a=obj.trajectory[j],b=obj.trajectory[j+8];
        if(Math.hypot(a[0]-b[0],a[1]-b[1])>.025)route(a,b,i?0xf3bf74:0x9bddce);
      }
    const loads=pack.objects.map((obj,i)=>{
      const group=new T.Group();scene.add(group);
      const c=i?amber:green;
      const piece=(x,y,z,w,d,h,color)=>{
        const m=box(x,y,z,w,d,h,color);group.add(m);return m;
      };
      piece(0,0,-.125,1.32,.68,.065,steel);
      for(const y of [-.32,.32])piece(0,y,-.035,1.35,.045,.15,c);
      for(const x of [-.65,.65])piece(x,0,-.035,.045,.64,.15,c);
      for(const x of [-.40,0,.40]){
        piece(x,0,-.051,.29,.55,.09,pale);
        piece(x,.01,.002,.035,.54,.013,steel);
      }
      for(const end of [-1,1]){
        for(const y of [-.14,.14])piece(end*.70,.10*end+y,-.004,.12,.025,.032,steel);
        piece(end*.751,.10*end,0,.028,.44,.034,0xced9d3);
      }
      const tag=document.createElement('span');tag.className='tag';tag.textContent=i?'B 组':'A 组';
      tag.style.borderLeft='4px solid '+(i?'#8a6416':'#2e5b3c');tags.append(tag);
      return {group,tag};
    });
    const shadows=pack.actors.map(()=>{
      const mesh=new T.Mesh(new T.CircleGeometry(1,32),new T.MeshBasicMaterial({color:0x44575d,transparent:true,opacity:.26,depthWrite:false}));
      mesh.scale.set(.30,.19,1);mesh.position.z=.003;scene.add(mesh);return mesh;
    });
    el('summary').textContent='4 台 Unitree G1 · 两组共搬 · 15 秒';
    el('focus').replaceChildren(new Option('路口全景','all'),new Option('跟随 A 组','A'),new Option('跟随 B 组','B'),new Option('俯视路权','top'));
    el('events').replaceChildren();
    for(const [label,j] of [['开始转弯',100],['停车让行',pack.events.stop],['框尾清出',pack.events.clear],['B 组起步',pack.events.release]]){
      const button=document.createElement('button');button.textContent=label;button.onclick=()=>seek(j);el('events').append(button);
    }
    return {loads,shadows,stop};
  }
  function drawJunction(left,w,h) {
    const j=Math.min(449,Math.round(frame)), paths=pack.objects.map(o=>o.trajectory[j]);
    const detail=focus==='A'||focus==='B',top=focus==='top',aspect=w/Math.max(1,h);
    const cx=detail?paths[focus==='A'?0:1][0]:(paths[0][0]+paths[1][0])/2;
    const cy=detail?paths[focus==='A'?0:1][1]:(paths[0][1]+paths[1][1])/2;
    const half=(detail?Math.max(1.50,2.18/aspect):Math.max(2.7,4.7/aspect,1.3+Math.abs(paths[0][1]-paths[1][1])*.45))/zoom;
    camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;
    const tilt=top?.02:1;
    camera.position.set(cx+tilt*Math.sin(turn)*11,cy-tilt*Math.cos(turn)*11,top?21:9.3);
    camera.lookAt(cx,cy,.52);camera.updateProjectionMatrix();camera.updateMatrixWorld();
    for(const [i,a] of pack.actors.entries()){
      const s=a.samples;
      for(let k=0;k<parts.length;k++){
        const u=(j*parts.length+k)*7;
        pos.set(s[u]/1000,s[u+1]/1000,s[u+2]/1000);
        rot.set(s[u+3]/32767,s[u+4]/32767,s[u+5]/32767,s[u+6]/32767).normalize();
        matrix.compose(pos,rot,unit);parts[k].setMatrixAt(i,matrix);
        if(k===0){junction.shadows[i].position.set(pos.x,pos.y,.003);junction.shadows[i].rotation.z=paths[Math.floor(i/2)][3];}
      }
    }
    parts.forEach(m=>m.instanceMatrix.needsUpdate=true);
    paths.forEach(([x,y,z,heading],i)=>{
      const {group,tag}=junction.loads[i];group.position.set(x,y,z);group.rotation.z=heading;
      const v=pos.set(x,y,z+.47).project(camera);tag.hidden=Math.abs(v.x)>.96||Math.abs(v.y)>.96;
      tag.style.left=(left+(v.x+1)*w/2)+'px';tag.style.top=((1-v.y)*h/2)+'px';
    });
    junction.stop.material.color.setHex(j>=pack.events.release?0x97ddbd:0xf5deb2);
    renderer.render(scene,camera);
    if(j<80)return 'A 组进入弯道，B 组从南侧接近停止线';
    if(j<pack.events.stop)return 'A 组转弯，B 组减速';
    if(j<pack.events.clear)return 'B 组停车让行，等待 A 组尾端清出路口';
    if(j<pack.events.release)return 'A 组尾端已清出，确认放行';
    return 'B 组恢复通行，沿北向通道跟进';
  }

  function events(variant=el('variant').selectedIndex){return pack.events[variant===1?'late':'baseline'];}
  function clipFrame(a,time,variant) {
    const elapsed=time-a.starts[variant]*30;
    if (elapsed<=0) return 0;
    if (!a.repeat) return Math.min(a.frames-1,elapsed);
    // Back-and-forth loop for stationary bench work preserves endpoints and stance.
    const n=a.frames-1, k=elapsed%(2*n);return k<=n?k:2*n-k;
  }
  function update() {
    if (!pack)return;
    const width=viewport.clientWidth,h=viewport.clientHeight;
    if (viewport.width!==Math.round(width*renderer.getPixelRatio())||viewport.height!==Math.round(h*renderer.getPixelRatio())) renderer.setSize(width,h,false);
    const compare=el('variant').selectedIndex===2;
    const views=compare?[0,1]:[el('variant').selectedIndex];
    renderer.setScissorTest(compare);
    for(const [tag,,,pane] of labels)if(!compare&&pane)tag.hidden=true;
    if(!compare)actorTags.forEach(pair=>pair[1].hidden=true);
    const messages=views.map((variant,pane)=>{
      const left=compare?Math.floor(width*pane/2):0;
      const w=compare?(pane===1?width-left:Math.floor(width/2)):width;
      renderer.setViewport(left,0,w,h);renderer.setScissor(left,0,w,h);
      return renderOne(variant,pane,left,w,h);
    });
    const timeInput = el('time');
    const rounded = Math.round(frame);
    timeInput.value = rounded;
    const maxFrame = Number(timeInput.max) || 0;
    timeInput.style?.setProperty?.('--p', (maxFrame > 0 ? (100 * rounded / maxFrame) : 0) + '%');
    el('clock').textContent=(frame/30).toFixed(1)+' s';el('play').textContent=playing?'暂停':'播放';
    el('status').textContent=compare?'左 · 正常：'+messages[0]+'　|　右 · 晚到：'+messages[1]:messages[0]+'  ·  拖动画面旋转，滚轮缩放';
  }
  function renderOne(variant,pane,left,w,h) {
    const aspect=w/Math.max(1,h);
    if(machine)return renderMachine(left,w,h);
    if(junction)return drawJunction(left,w,h);
    const dock=focus==='dock'?pack.equipment.find(e=>e.id==='A'):null;
    const selected=dock?null:pack.stations[Number(focus)];
    const move=dock?dock.travel[Math.round(clipFrame(pack.actors.find(a=>a.id===dock.driver),frame,variant))]:[0,0];
    const cx=dock?dock.origin[0]+move[0]:selected?selected[1]:0;
    const cy=dock?dock.origin[1]+move[1]:selected?-selected[2]:0;
    const half=(dock?Math.max(2.25,3.5/aspect):selected?Math.max(4.6,5.5/aspect):Math.max(7.9,16.2/aspect))/zoom;
    camera.left=-half*aspect;camera.right=half*aspect;camera.top=half;camera.bottom=-half;
    camera.position.set(cx+Math.sin(turn)*26,cy-Math.cos(turn)*26,23);camera.lookAt(cx,cy,.25);camera.updateProjectionMatrix();camera.updateMatrixWorld();
    const count=parts.length,t=frame/30, ev=events(variant);
    pack.actors.forEach((a,i)=>{
      const j=Math.round(clipFrame(a,frame,variant));
      for(let k=0;k<count;k++){
        const q=(j*count+k)*7,s=a.samples;
        pos.set(s[q]/1000,s[q+1]/1000,s[q+2]/1000);
        rot.set(s[q+3]/32767,s[q+4]/32767,s[q+5]/32767,s[q+6]/32767).normalize();
        matrix.compose(pos,rot,unit);parts[k].setMatrixAt(i,matrix);
      }
      const tag=actorTags[i][pane];tag.hidden=dock?!['R15','R16','R26','R27','R28'].includes(a.id):!selected||a.station!==Number(focus);
      if(!tag.hidden){
        const k=j*count*7,s=a.samples;
        const v=pos.set(s[k]/1000,s[k+1]/1000,s[k+2]/1000+.85).project(camera);
        tag.style.left=(left+(v.x+1)*w/2)+'px';tag.style.top=((1-v.y)*h/2)+'px';
      }
    });
    parts.forEach(m=>m.instanceMatrix.needsUpdate=true);
    const mix=(a,b,u)=>a+(b-a)*Math.max(0,Math.min(1,u));
    if(pack.format==='kinora-factory-line-2'){
      for(const eq of pack.equipment){
        if(!eq.driver)continue;
        const driver=pack.actors.find(a=>a.id===eq.driver);
        const [dx,dy]=eq.travel[Math.round(clipFrame(driver,frame,variant))];
        machinery[eq.id].position.set(eq.origin[0]+dx,eq.origin[1]+dy,0);
      }
      machinery.outfeed.position.x=mix(12.35,13.95,(t-48)/6);
    }else{
    const seated=pack.objects.find(o=>o.id==='fan_A')?.trajectory.at(-1);
    const dockX=seated?.[1]??9.1,dockY=seated?.[2]??0;
    const move=(t-ev.transfer)/4, endY=-3.4;
    machinery.A.position.set(dockX,mix(dockY,endY,move),0);
    machinery.A.scale.set(1,.70,.78);
    machinery.B.position.set(mix(dockX,3.1,(t-48)/6),endY,0);
    machinery.C.position.set(mix(-11.6,-13.1,(t-ev.ship)/6),-5.15,0);
    machinery.cart.position.set(mix(6.23,7.70,(t-ev.clear)/6),0,0);
    }
    pack.objects.forEach((o,i)=>{
      const a=pack.actors.find(x=>x.id===o.actor),j=clipFrame(a,frame,variant),path=o.trajectory;
      let k=0;while(k+1<path.length-1&&path[k+1][0]<j)k++;
      const lo=path[k],hi=path[k+1]||lo,u=(j-lo[0])/Math.max(1,hi[0]-lo[0]);
      const m=materialProps[i];m.position.set(mix(lo[1],hi[1],u),mix(lo[2],hi[2],u),mix(lo[3],hi[3],u));m.rotation.z=o.heading*Math.PI/180;
      if(o.id==='fan_A' && t>=ev.transfer){
        if(pack.format==='kinora-factory-line-2'){
          const eq=pack.equipment.find(e=>e.id==='A');
          m.position.x+=machinery.A.position.x-eq.origin[0];m.position.y+=machinery.A.position.y-eq.origin[1];
        }else{
          const dockY=o.trajectory.at(-1)[2];
          m.position.y+=mix(0,-3.4-dockY,(t-ev.transfer)/4);
        }
      }
    });
    renderer.render(scene,camera);
    for(const [tag,world,s,labelPane] of labels){
      if(labelPane!==pane)continue;
      const point=world.clone().project(camera);tag.hidden=!!dock||!!selected&&s!==Number(focus);
      tag.style.left=(left+(point.x+1)*w/2)+'px';tag.style.top=((1-point.y)*h/2)+'px';
    }
    let msg='各工位加工在制品';
    if(t>=ev.fan)msg='R15 / R16 共搬风机模块';
    if(t>=ev.seated)msg='R17 检查模块落座';
    if(t>=ev.clear)msg='R18 放行，R05 推送料车';
    if(t>=ev.arrival && t<ev.receive)msg='机组 A 已到，等待 B 腾出接收位';
    if(t>=ev.bayFree && t<ev.arrival)msg='B 已离开，接收位等待机组 A';
    if(t>=ev.receive)msg='R27 / R28 接入机组 A';
    if(t>=ev.panel)msg='R33 / R34 搬运侧板';
    return msg;
  }
  function seek(value){frame=Math.max(0,Math.min(pack.duration*30-1,value));update();}
  function animate(ts) {
    if(playing&&pack){frame+=Math.min(.25,(ts-previous)/1000)*30*Number(el('speed').value);if(frame>=pack.duration*30-1){frame=pack.duration*30-1;playing=false;}update();}
    previous=ts;requestAnimationFrame(animate);
  }
  el('play').onclick=()=>{if(frame>=pack.duration*30-1)frame=0;playing=!playing;previous=performance.now();update();};
  el('time').oninput=e=>seek(Number(e.target.value));
  el('variant').onchange=update;
  el('focus').onchange=e=>{focus=e.target.value;zoom=1;update();};
  let drag;
  viewport.onpointerdown=e=>{drag=e.clientX;viewport.setPointerCapture(e.pointerId);};
  viewport.onpointerup=viewport.onpointercancel=()=>{drag=null;};
  viewport.onpointermove=e=>{if(drag!=null){turn+=(e.clientX-drag)/220;drag=e.clientX;update();}};
  viewport.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.6,Math.min(2.8,zoom*Math.exp(-e.deltaY*.001)));update();},{passive:false});
  async function open(file){
    try{if(!file||file.size>64*1024*1024)throw Error('请选择 64 MiB 以内的协作场景');loadPack(JSON.parse(await file.text()));}
    catch(e){el('status').textContent=e.message;}
  }
  el('import').onchange=e=>open(e.target.files[0]);
  window.addEventListener('resize',update);
  const initial=el('packaged-scene');
  if(initial){try{loadPack(JSON.parse(initial.textContent));}catch(e){el('status').textContent=e.message;}}
  requestAnimationFrame(animate);
})();
