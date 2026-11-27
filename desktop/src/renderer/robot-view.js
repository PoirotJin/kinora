'use strict';
// Robot mesh preview for completed retarget jobs, adapted from web/review/robot-viewer.js.
// MuJoCo compiled vertices already include mesh centring/scaling; geom poses are Z-up.
const RobotView = (() => {
  function interpolateTrajectory(object, frame) {
    const track = object?.trajectory;
    if (!Array.isArray(track) || !track.length) return object.center_m;
    if (frame <= track[0].frame) return track[0].center_m;
    if (frame >= track[track.length - 1].frame) return track[track.length - 1].center_m;
    const next = track.findIndex(point => point.frame > frame);
    if (next <= 0) return track[0].center_m;
    const a = track[next - 1], b = track[next];
    const span = b.frame - a.frame;
    const t = span > 0 ? Math.max(0, Math.min(1, (frame - a.frame) / span)) : 0;
    return [
      a.center_m[0] + (b.center_m[0] - a.center_m[0]) * t,
      a.center_m[1] + (b.center_m[1] - a.center_m[1]) * t,
      a.center_m[2] + (b.center_m[2] - a.center_m[2]) * t
    ];
  }

  class View {
    constructor(canvas, loaders) {
      this.canvas = canvas; this.loaders = loaders;
      this.request = 0; this.renderer = null; this.meshes = []; this.version = null; this.robot = null;
      this.clip = null; this.frame = 0; this.playing = false; this.speed = 1; this.loop = true; this.onFrame = null;
      this.sceneObjects = []; this.sceneObjectGroup = null; this._sceneMeshes = [];
      this.suspended = false; this.raf = 0;
      this.drag = null;
      this.resetCamera(); this.bindInput();
      this.last = 0; this.tick = this.tick.bind(this); this.schedule();
    }
    schedule() {
      if (!this.suspended && !this.raf && typeof requestAnimationFrame === 'function') {
        this.raf = requestAnimationFrame(this.tick);
      }
    }
    suspend() {
      this.suspended = true;
      this.stopDrag();
      if (this.playing) this.setPlaying(false);
      if (this.raf && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
    resume() {
      this.suspended = false;
      this.last = 0;
      this.dirty = true;
      this.schedule();
    }
    cameraState() {
      return { yaw: this.yaw, pitch: this.pitch, zoom: this.zoom, speed: this.speed, loop: this.loop, frame: this.frame };
    }
    applyCameraState(state) {
      if (!state) return;
      if (Number.isFinite(state.yaw)) this.yaw = state.yaw;
      if (Number.isFinite(state.pitch)) this.pitch = state.pitch;
      if (Number.isFinite(state.zoom)) this.zoom = state.zoom;
      if (Number.isFinite(state.speed) && state.speed > 0) this.speed = state.speed;
      if (typeof state.loop === 'boolean') this.loop = state.loop;
      if (this.clip && Number.isFinite(state.frame)) this.frame = Math.max(0, Math.min(this.clip.frames - 1, state.frame));
      this.dirty = true;
    }
    resetCamera() { this.yaw = 0.6; this.pitch = 0.25; this.zoom = 1; this.dirty = true; }
    stopDrag() {
      const drag = this.drag;
      this.drag = null;
      if (drag && this.canvas.hasPointerCapture(drag.id)) this.canvas.releasePointerCapture(drag.id);
    }
    bindInput() {
      const c = this.canvas;
      c.addEventListener('pointerdown', e => {
        if (this.suspended || this.drag || !e.buttons) return;
        c.setPointerCapture(e.pointerId);
        this.drag = { id: e.pointerId, x: e.clientX, y: e.clientY };
      });
      c.addEventListener('pointermove', e => {
        const drag = this.drag;
        if (drag?.id !== e.pointerId) return;
        if (!e.buttons) { this.stopDrag(); return; }
        this.yaw += (e.clientX - drag.x) * 0.008;
        this.pitch = Math.max(-0.3, Math.min(1.4, this.pitch + (e.clientY - drag.y) * 0.008));
        drag.x = e.clientX; drag.y = e.clientY;
        this.dirty = true;
      });
      const endDrag = e => { if (this.drag?.id === e.pointerId) this.stopDrag(); };
      c.addEventListener('pointerup', endDrag);
      c.addEventListener('pointercancel', endDrag);
      c.addEventListener('lostpointercapture', endDrag);
      c.addEventListener('wheel', e => { e.preventDefault(); this.zoom = Math.max(0.3, Math.min(4, this.zoom * Math.exp(-e.deltaY * 0.001))); this.dirty = true; }, { passive: false });
      c.addEventListener('dblclick', () => this.resetCamera());
    }
    async load(clip, { getPlayback = null } = {}) {
      const request = ++this.request, robot = clip.metadata?.robot || 'unitree_g1_23dof';
      if (robot !== this.robot || clip.version !== this.version) await this.initialize(robot, request);
      if (request !== this.request) return false;
      if (clip.version !== this.version || clip.poses[0]?.length !== this.meshes.length) throw new Error('机器人模型与动作版本不匹配');
      const playback = getPlayback?.();
      this.clip = clip;
      this.frame = Math.max(0, Math.min(clip.frames - 1, playback?.frame ?? 0));
      this.playing = playback?.playing ?? true;
      this.dirty = true;
      this.setSceneObjects(clip.scene?.objects || clip.metadata?.scene_objects || []);
      this.emit();
      return true;
    }
    setSceneObjects(objects) {
      this.sceneObjects = Array.isArray(objects) ? objects : [];
      if (!this.scene || typeof THREE === 'undefined') return;
      if (this.sceneObjectGroup) {
        this.scene.remove(this.sceneObjectGroup);
        this.sceneObjectGroup.traverse(o => {
          if (o.geometry) o.geometry.dispose();
          if (o.material) o.material.dispose();
        });
        this.sceneObjectGroup = null;
      }
      this._sceneMeshes = [];
      if (!this.sceneObjects.length) {
        this.dirty = true;
        this.schedule();
        return;
      }
      const group = new THREE.Group();
      group.name = 'scene-objects';
      for (const obj of this.sceneObjects) {
        if (!Array.isArray(obj?.center_m) || obj.center_m.length !== 3 || !Array.isArray(obj?.size_m) || obj.size_m.length !== 3) continue;
        const [sx, sy, sz] = obj.size_m;
        const geometry = new THREE.BoxGeometry(sx, sz, sy);
        const material = new THREE.MeshStandardMaterial({
          color: 0x57544e, transparent: true, opacity: 0.24, roughness: 0.85, metalness: 0.05, depthWrite: false
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.name = obj.id || '';
        const edges = new THREE.EdgesGeometry(geometry);
        const wire = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: 0x0b0b0b, transparent: true, opacity: 0.85 }));
        mesh.add(wire);
        const center = interpolateTrajectory(obj, this.frame || 0);
        mesh.position.set(center[0], -center[2], center[1]);
        mesh.rotation.z = (Number(obj.heading_deg) || 0) * Math.PI / 180;
        group.add(mesh);
        this._sceneMeshes.push({ obj, mesh });
      }
      this.sceneObjectGroup = group;
      this.scene.add(group);
      this.dirty = true;
      this.schedule();
    }
    async initialize(robot, request) {
      if (typeof THREE === 'undefined') throw new Error('三维渲染组件未加载');
      const manifest = await this.loaders.model(robot);
      const binary = await this.loaders.meshData(robot);
      if (request !== this.request) return;
      if (this.scene) {
        const geometries = new Set(), materials = new Set();
        this.scene.traverse(o => { if (o.geometry) geometries.add(o.geometry); if (o.material) materials.add(o.material); });
        geometries.forEach(g => g.dispose()); materials.forEach(m => m.dispose()); this.light?.shadow?.map?.dispose();
      }
      if (!this.renderer) {
        const renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true });
        renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2)); renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
        renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
        renderer.setClearColor(0x000000, 0);
        this.renderer = renderer;
      }
      const scene = new THREE.Scene(); scene.fog = new THREE.Fog(0xebe8e1, 8, 26);
      const camera = new THREE.PerspectiveCamera(38, 1, 0.03, 100); camera.up.set(0, 0, 1);
      const ambient = new THREE.HemisphereLight(0xffffff, 0xd9d3c8, 1.2); ambient.position.set(0, 0, 1); scene.add(ambient);
      const light = new THREE.DirectionalLight(0xffffff, 2.2); light.castShadow = true; light.shadow.mapSize.set(1024, 1024);
      Object.assign(light.shadow.camera, { left: -2.5, right: 2.5, top: 2.5, bottom: -2.5, near: 0.1, far: 12 });
      light.shadow.bias = -0.0002; light.shadow.normalBias = 0.01; light.shadow.radius = 3; scene.add(light, light.target);
      const rim = new THREE.DirectionalLight(0xffffff, 0.4); rim.position.set(-3, 4, 3); scene.add(rim);
      const floor = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.18 }));
      floor.position.z = -0.005; floor.receiveShadow = true; scene.add(floor);
      const grid = new THREE.GridHelper(40, 80, 0xc4beb3, 0xdad5cb); grid.rotation.x = Math.PI / 2; grid.position.z = -0.004;
      grid.material.transparent = true; grid.material.opacity = 0.55; scene.add(grid);
      const geometries = {};
      for (const [key, info] of Object.entries(manifest.meshes)) {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(binary, info.vertex_offset, info.vertices * 3), 3));
        geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(binary, info.index_offset, info.indices), 1));
        geometry.computeVertexNormals(); geometry.computeBoundingSphere(); geometries[key] = geometry;
      }
      const meshes = manifest.geoms.map(info => {
        // Consistent contrast for built-in and imported models, regardless of source STL/material colors.
        const base = new THREE.Color(0x57544e);
        let geometry;
        if (info.mesh !== null) geometry = geometries[info.mesh];
        else {
          const [x, y, z] = info.size;
          switch (info.primitive || 'box') {
            case 'sphere': geometry = new THREE.SphereGeometry(x, 24, 16); break;
            case 'ellipsoid': geometry = new THREE.SphereGeometry(1, 24, 16); geometry.scale(x, y, z); break;
            case 'cylinder': geometry = new THREE.CylinderGeometry(x, x, y * 2, 24); geometry.rotateX(Math.PI / 2); break;
            case 'capsule': geometry = new THREE.CapsuleGeometry(x, y * 2, 8, 24); geometry.rotateX(Math.PI / 2); break;
            default: geometry = new THREE.BoxGeometry(...info.size.map(x => x * 2));
          }
        }
        const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: base, metalness: 0.08, roughness: 0.7 }));
        mesh.name = info.body; mesh.castShadow = true; mesh.receiveShadow = true; scene.add(mesh); return mesh;
      });
      Object.assign(this, { scene, camera, light, grid, meshes, version: manifest.version, robot });
      this.sceneObjectGroup = null; this._sceneMeshes = [];
      if (this.sceneObjects?.length) this.setSceneObjects(this.sceneObjects);
    }
    seek(frame) { if (!this.clip) return; this.frame = Math.max(0, Math.min(this.clip.frames - 1, frame)); this.dirty = true; this.schedule(); this.emit(); }
    seekTime(seconds) { if (!this.clip) return; this.seek(Math.max(0, seconds) * this.clip.fps); }
    setPlaying(value) {
      this.playing = !!value && !!this.clip && !this.suspended;
      if (this.playing && !this.loop && this.frame >= this.clip.frames - 1) {
        this.seek(0);
        return;
      }
      if (this.playing) this.schedule();
      this.emit();
    }
    emit() { this.onFrame?.(this); }
    clear() { this.request++; this.clip = null; this.playing = false; this.setSceneObjects([]); this.renderer?.clear(); this.emit(); }
    tick(now) {
      this.raf = 0;
      if (this.suspended) return;
      const dt = Math.min(0.1, (now - this.last) / 1000 || 0); this.last = now;
      if (this.clip && this.renderer && this.canvas.clientWidth) {
        if (this.playing) {
          let next = this.frame + dt * this.clip.fps * this.speed;
          if (next >= this.clip.frames - 1 + 0.999) {
            if (this.loop) next %= this.clip.frames;
            else { next = this.clip.frames - 1; this.playing = false; }
          }
          this.frame = next; this.dirty = true; this.emit();
        }
        if (this.dirty || this.canvas.clientWidth !== this.width || this.canvas.clientHeight !== this.height) { this.dirty = false; this.render(this.frame); }
      }
      this.schedule();
    }
    render(frame) {
      const width = this.canvas.clientWidth, height = this.canvas.clientHeight; if (!width || !height) return;
      // setSize reallocates the drawing buffer; only do it when the canvas actually resized.
      if (width !== this.width || height !== this.height) {
        this.width = width; this.height = height;
        this.renderer.setSize(width, height, false);
      }
      // Interpolate between samples (position lerp, shortest-arc quaternion nlerp); no blend across the loop seam.
      const frames = this.clip.frames, i = Math.max(0, Math.min(frames - 1, Math.floor(frame))), n = Math.min(frames - 1, i + 1), t = Math.min(1, Math.max(0, frame - i));
      const A = this.clip.poses[i], B = this.clip.poses[n] || A;
      for (let k = 0; k < this.meshes.length; k++) {
        const a = A[k], b = B[k], s = a[3] * b[3] + a[4] * b[4] + a[5] * b[5] + a[6] * b[6] < 0 ? -1 : 1;
        this.meshes[k].position.set(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t);
        this.meshes[k].quaternion.set(a[3] + (s * b[3] - a[3]) * t, a[4] + (s * b[4] - a[4]) * t, a[5] + (s * b[5] - a[5]) * t, a[6] + (s * b[6] - a[6]) * t).normalize();
      }
      const ra = this.clip.roots[i] || this.clip.roots[0], rb = this.clip.roots[n] || ra, root = [ra[0] + (rb[0] - ra[0]) * t, ra[1] + (rb[1] - ra[1]) * t], x = root[0], y = root[1], z = this.clip.centerZ ?? 0.8;
      const aspect = width / height;
      let d = Math.max(0.1, (this.clip.cameraDistance || 3.2) / this.zoom);
      let near = 0.03, far = 100;
      const radius = this.clip.cameraBoundsRadius;
      if (Number.isFinite(radius) && radius > 0) {
        const vertical = this.camera.fov * Math.PI / 360;
        const horizontal = Math.atan(Math.tan(vertical) * aspect);
        d = 1.1 * radius / (Math.sin(Math.min(vertical, horizontal)) * this.zoom);
        near = Math.max(radius * 0.0001, d - radius * 1.25);
        far = d + radius * 4;
      }
      if (this.camera.aspect !== aspect || this.camera.near !== near || this.camera.far !== far) {
        Object.assign(this.camera, { aspect, near, far });
        this.camera.updateProjectionMatrix();
      }
      if (this._sceneMeshes.length) {
        for (const entry of this._sceneMeshes) {
          const center = interpolateTrajectory(entry.obj, frame);
          entry.mesh.position.set(center[0], -center[2], center[1]);
        }
      }
      this.camera.position.set(x + Math.cos(this.yaw) * Math.cos(this.pitch) * d, y + Math.sin(this.yaw) * Math.cos(this.pitch) * d, z + Math.sin(this.pitch) * d);
      this.camera.lookAt(x, y, z); this.light.position.set(x + 3, y - 2, 6); this.light.target.position.set(x, y, 0.6);
      this.grid.position.set(Math.round(x * 2) / 2, Math.round(y * 2) / 2, -0.004);
      this.renderer.render(this.scene, this.camera);
    }
  }
  return { View };
})();
