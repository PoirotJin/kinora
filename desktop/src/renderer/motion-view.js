'use strict';
// Canvas 2D perspective renderer for MotionFarm skeleton previews.
// Input is the server's MFPRV001 world-coordinate transport (Y up, metres).
const MotionView = (() => {
  // Monochrome on warm paper: ink for the centre and left side, stone grey for the right.
  const COLORS = { C: [11, 11, 11], L: [11, 11, 11], R: [163, 158, 148] };
  const ACCENT = [11, 11, 11];
  const ACTOR_COLORS = ['#0b0b0b', '#8a6416', '#2e5b3c', '#57544e'];

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
    return a.center_m.map((value, i) => value + (b.center_m[i] - value) * t);
  }

  function drawPill(ctx, x, y, text, fill, outline) {
    const pw = Math.max(22, String(text).length * 7 + 12), h = 18;
    const lx = x - pw / 2, ty = y - h / 2;
    ctx.fillStyle = fill;
    ctx.fillRect(lx, ty, pw, h);
    if (outline) {
      ctx.strokeStyle = outline;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(lx, ty, pw, h);
    }
    ctx.fillStyle = '#f3f1ec';
    ctx.font = '600 10px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(text), x, y + 0.5);
  }

  function validatePreviewParents(parents, count) {
    if (!Array.isArray(parents) || parents.length !== count ||
      parents.some(parent => !Number.isInteger(parent) || parent < -1 || parent >= count)) {
      throw new Error('预览骨架错误');
    }
    const visited = new Int32Array(count);
    for (let joint = 0; joint < count; joint++) {
      const chain = joint + 1;
      let parent = joint;
      while (parent !== -1 && !visited[parent]) {
        visited[parent] = chain;
        parent = parents[parent];
      }
      if (parent !== -1 && visited[parent] === chain) throw new Error('预览骨架错误');
    }
  }

  function decodePreviewCoordinates(joints, count) {
    if (!Array.isArray(joints)) throw new Error('预览坐标无效');
    const data = new Float32Array(joints.length * count * 3);
    let offset = 0;
    for (const frame of joints) {
      if (!Array.isArray(frame) || frame.length !== count) throw new Error('预览坐标无效');
      for (const point of frame) {
        if (!Array.isArray(point) || point.length !== 3 || !point.every(Number.isFinite)) throw new Error('预览坐标无效');
        data.set(point, offset);
        offset += 3;
      }
    }
    if (!data.every(Number.isFinite)) throw new Error('预览坐标无效');
    return data;
  }

  function decodePreview(buffer) {
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 16 || buffer.byteLength > 40000000) throw new Error('预览数据长度错误');
    const bytes = new Uint8Array(buffer), text = new TextDecoder();
    if (text.decode(bytes.subarray(0, 8)) !== 'MFPRV001') throw new Error('不支持的预览版本');
    const headerSize = new DataView(buffer).getUint32(8, true);
    if (headerSize > 65536 || headerSize % 4 || 12 + headerSize > buffer.byteLength) throw new Error('预览头损坏');
    const meta = JSON.parse(text.decode(bytes.subarray(12, 12 + headerSize)));
    const frames = meta.frames, count = meta.joint_count;
    // Same bound as the server's preview encoder (120 s at 30 fps).
    if (!Number.isInteger(frames) || frames < 1 || frames > 3600 || !Number.isInteger(count) || count < 1 || count > 128 || !Number.isFinite(meta.fps) || meta.fps < 1 || meta.fps > 240) throw new Error('预览尺寸错误');
    validatePreviewParents(meta.parents, count);
    if (!Array.isArray(meta.names) || meta.names.length !== count) throw new Error('预览骨架错误');
    if (buffer.byteLength !== 12 + headerSize + frames * count * 12) throw new Error('预览坐标长度错误');
    // Copy into an aligned array; the header length is a multiple of four.
    const data = new Float32Array(buffer.slice(12 + headerSize));
    for (let i = 0; i < data.length; i++) if (!Number.isFinite(data[i])) throw new Error('预览包含无效坐标');
    return { meta, data };
  }

  function decodeJsonPreview(clip) {
    const frames = clip?.joints?.length, count = clip?.parents?.length;
    if (!Number.isInteger(frames) || frames < 1 || frames > 3600 || !Number.isInteger(count) || count < 1 || count > 128 || !Number.isFinite(clip?.fps) || clip.fps < 1 || clip.fps > 240) {
      throw new Error('预览尺寸无效');
    }
    validatePreviewParents(clip.parents, count);
    const data = decodePreviewCoordinates(clip.joints, count);
    const names = Array.isArray(clip.names) && clip.names.length === count ? clip.names : clip.parents.map((_, i) => String(i));
    const meta = { frames, joint_count: count, fps: clip.fps, duration_s: frames / clip.fps, parents: clip.parents, names, quality: clip.quality || null };
    return { meta, data, scene: clip.scene || null, quality: clip.quality || null };
  }

  function previewQualityEvents(quality, frames) {
    if (!Array.isArray(quality?.events)) return [];
    return quality.events.filter(event => Number.isInteger(event?.frame) &&
      event.frame >= 0 && event.frame < frames && typeof event.reason === 'string' && event.reason.trim());
  }

  function sideOf(name) {
    const n = String(name).toLowerCase();
    if (/^(l_|left)/.test(n)) return 'L';
    if (/^(r_|right)/.test(n)) return 'R';
    return 'C';
  }
  const isFinger = name => /index|middle|ring|pinky|thumb|finger/i.test(name);
  const isHead = name => /^head$/i.test(name);

  function makeSkeleton(parents, names) {
    return names.map((name, i) => ({ i, parent: parents[i], side: sideOf(name), finger: isFinger(name), head: isHead(name) }));
  }

  let GIZMO_FONT = '';
  function basis(target, yaw, pitch, distance) {
    const cp = Math.cos(pitch);
    const eye = [target[0] + Math.sin(yaw) * cp * distance, target[1] + Math.sin(pitch) * distance, target[2] + Math.cos(yaw) * cp * distance];
    let f = [target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]];
    const fl = Math.hypot(...f); f = f.map(v => v / fl);
    let r = [-f[2], 0, f[0]]; // forward × world up
    const rl = Math.hypot(...r) || 1; r = r.map(v => v / rl);
    const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]];
    return { eye, f, r, u };
  }

  class View {
    constructor(canvas, options = {}) {
      this.canvas = canvas; this.ctx = canvas.getContext('2d');
      this.thumbnail = !!options.thumbnail;
      this.clip = null; this.frame = 0; this.playing = false; this.speed = 1; this.loop = true;
      this.follow = true; this.trail = true;
      this.onSceneMove = options.onSceneMove;
      this.onActorMove = options.onActorMove || null;
      this.sceneHandles = [];
      this.actorHandles = [];
      this.drag = null;
      this.onFrame = options.onFrame || null;
      this.resetCamera();
      this.target = [0, 0.9, 0];
      if (!this.thumbnail) this.bindInput();
      this.last = 0; this.dirty = true; this.suspended = false; this.raf = 0;
      this.tick = this.tick.bind(this);
      this.schedule();
    }
    schedule() {
      if (!this.thumbnail && !this.suspended && !this.raf && typeof requestAnimationFrame === 'function') {
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
      return { yaw: this.yaw, pitch: this.pitch, distance: this.distance, follow: this.follow, trail: this.trail, speed: this.speed, loop: this.loop, frame: this.frame };
    }
    applyCameraState(state) {
      if (!state) return;
      if (Number.isFinite(state.yaw)) this.yaw = state.yaw;
      if (Number.isFinite(state.pitch)) this.pitch = state.pitch;
      if (Number.isFinite(state.distance)) this.distance = state.distance;
      if (typeof state.follow === 'boolean') this.follow = state.follow;
      if (typeof state.trail === 'boolean') this.trail = state.trail;
      if (Number.isFinite(state.speed) && state.speed > 0) this.speed = state.speed;
      if (typeof state.loop === 'boolean') this.loop = state.loop;
      if (this.clip && Number.isFinite(state.frame)) this.frame = Math.max(0, Math.min(this.clip.frames - 1, state.frame));
      this.dirty = true;
    }
    resetCamera() { this.yaw = 0.62; this.pitch = this.draftScene ? 0.42 : 0.2; this.distance = 4.2; this.dirty = true; if (this.clip) this.target = this.focusPoint(Math.floor(this.frame)); }
    stopDrag() {
      const drag = this.drag;
      this.drag = null;
      if (drag && this.canvas.hasPointerCapture(drag.id)) this.canvas.releasePointerCapture(drag.id);
    }
    bindInput() {
      const c = this.canvas;
      const local = e => {
        const rect = c.getBoundingClientRect();
        return [e.clientX-rect.left,e.clientY-rect.top];
      };
      const floorPoint = (x,y,height,cam) => {
        const w = c.clientWidth, h = c.clientHeight, focal = Math.min(w,h)*1.25;
        const ray = cam.f.map((v,i) => v + cam.r[i]*(x-w/2)/focal + cam.u[i]*(h*.47-y)/focal);
        const t = (height-cam.eye[1])/ray[1];
        return Number.isFinite(t) && t > 0 && t < 200 ? [cam.eye[0]+t*ray[0],cam.eye[2]+t*ray[2]] : null;
      };
      c.addEventListener('pointerdown', e => {
        if (this.suspended || this.drag || !e.isPrimary || e.button !== 0) return;
        const [x,y] = local(e);
        const actorHit = this.draftScene && this.onActorMove && this.actorHandles.find(
          hit => Math.hypot(hit.x - x, hit.y - y) <= 16);
        const sceneHit = this.draftScene && this.onSceneMove && this.sceneHandles.find(
          hit => Math.hypot(hit.x - x, hit.y - y) <= 14);
        const pick = actorHit && sceneHit
          ? (actorHit.depth < sceneHit.depth ? { handle: actorHit, kind: 'actor' } : { handle: sceneHit, kind: 'scene' })
          : actorHit ? { handle: actorHit, kind: 'actor' }
          : sceneHit ? { handle: sceneHit, kind: 'scene' } : null;
        let moving = null;
        if (pick) {
          const cam = basis(this.target, this.yaw, this.pitch, this.distance);
          const point = floorPoint(x, y, pick.handle.height, cam);
          if (point) moving = { handle: pick.handle, kind: pick.kind, cam, point };
        }
        c.setPointerCapture(e.pointerId);
        this.drag = { id: e.pointerId, x: e.clientX, y: e.clientY, moving };
      });
      c.addEventListener('pointermove', e => {
        const drag = this.drag;
        if (drag?.id !== e.pointerId) return;
        if (!e.buttons) { this.stopDrag(); return; }
        const { moving } = drag;
        if (moving && this.draftScene) {
          const { handle, kind, cam, point } = moving;
          const next = floorPoint(...local(e), handle.height, cam);
          const cb = kind === 'actor' ? this.onActorMove : this.onSceneMove;
          if (next && cb && cb(handle.id, next[0] - point[0], next[1] - point[1])) moving.point = next;
          return;
        }
        this.yaw -= (e.clientX - drag.x) * 0.008;
        this.pitch = Math.max(-0.05, Math.min(1.35, this.pitch + (e.clientY - drag.y) * 0.006));
        drag.x = e.clientX; drag.y = e.clientY; this.dirty = true;
      });
      const end = e => { if (this.drag?.id === e.pointerId) this.stopDrag(); };
      c.addEventListener('pointerup', end); c.addEventListener('pointercancel', end);
      c.addEventListener('lostpointercapture', end);
      c.addEventListener('wheel', e => { e.preventDefault(); this.distance = Math.max(1.2, Math.min(14, this.distance * Math.exp(e.deltaY * 0.0012))); this.dirty = true; }, { passive: false });
      c.addEventListener('dblclick', () => this.resetCamera());
      new ResizeObserver(() => { this.dirty = true; }).observe(c);
    }
    setClip(decoded, keep = false, { startFrame = 0 } = {}) {
      if (!keep || this.draftScene) this.stopDrag();
      this.draftScene = null;
      this.actorHandles = [];
      const { meta, data, scene } = decoded;
      const count = meta.joint_count, frames = meta.frames;
      const root = new Float32Array(frames * 3);
      for (let f = 0; f < frames; f++) for (let k = 0; k < 3; k++) root[f * 3 + k] = data[(f * count) * 3 + k];
      this.clip = { meta, data, count, frames, fps: meta.fps, bones: makeSkeleton(meta.parents, meta.names), floor: 0, root, scene };
      if (keep) this.frame = Math.min(this.frame, frames - 1);
      else {
        this.frame = Math.max(0, Math.min(startFrame, frames - 1));
        this.target = this.focusPoint(this.frame);
      }
      this.dirty = true;
      this.emit();
    }
    clear() { this.stopDrag(); this.clip = null; this.draftScene = null; this.sceneHandles = []; this.actorHandles = []; this.playing = false; this.dirty = true; this.emit(); }
    setScene(objects, focus, selectedId, actorDrafts = []) {
      const entering = !this.draftScene;
      if (entering) this.stopDrag();
      this.clip = null;
      this.playing = false;
      this.frame = 0;
      this.draftScene = {
        objects: objects || [],
        targets: [],
        selectedId,
        actorDrafts: Array.isArray(actorDrafts) ? actorDrafts : []
      };
      this.target = focus || [0, 0.5, 0];
      if (entering) this.resetCamera();
      this.dirty = true;
      this.emit();
    }
    joint(f, j) { const c = this.clip, o = (f * c.count + j) * 3; return [c.data[o], c.data[o + 1], c.data[o + 2]]; }
    // Poses between samples are interpolated, so playback is smooth on displays faster
    // than the clip rate and when a frame arrives late. Never blends across the loop seam.
    pose(frame) {
      const c = this.clip, n = c.count * 3, i = Math.max(0, Math.min(c.frames - 1, Math.floor(frame))), j = Math.min(c.frames - 1, i + 1), t = Math.min(1, Math.max(0, frame - i));
      const out = c.pose || (c.pose = new Float32Array(n)), a = i * n, b = j * n, d = c.data;
      for (let k = 0; k < n; k++) out[k] = d[a + k] + (d[b + k] - d[a + k]) * t;
      return out;
    }
    focusPoint(frame) {
      const c = this.clip; if (!c) return [0, 0.9, 0];
      const i = Math.max(0, Math.min(c.frames - 1, Math.floor(frame))), j = Math.min(c.frames - 1, i + 1), t = Math.min(1, Math.max(0, frame - i)), r = c.root;
      return [r[i * 3] + (r[j * 3] - r[i * 3]) * t, c.floor + 0.9, r[i * 3 + 2] + (r[j * 3 + 2] - r[i * 3 + 2]) * t];
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
    tick(now) {
      this.raf = 0;
      if (this.suspended) return;
      const dt = Math.min(0.1, (now - this.last) / 1000 || 0); this.last = now;
      if (this.clip && this.playing) {
        let next = this.frame + dt * this.clip.fps * this.speed;
        if (next >= this.clip.frames - 1 + 0.999) { if (this.loop) next %= this.clip.frames; else { next = this.clip.frames - 1; this.playing = false; } }
        this.frame = next; this.dirty = true; this.emit();
      }
      if (this.dirty && this.canvas.isConnected && this.canvas.clientWidth) { this.dirty = false; this.draw(); }
      this.schedule();
    }

    // ---------- drawing ----------
    setup() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) { this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr); }
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0); this.ctx.clearRect(0, 0, w, h);
      return [w, h];
    }
    draw() {
      const [w, h] = this.setup(), ctx = this.ctx;
      if (!this.clip && !this.draftScene) return;
      const index = Math.floor(this.frame);
      if (this.clip && this.follow) {
        const goal = this.focusPoint(this.frame);
        const k = this.playing ? 0.12 : 1;
        this.target = this.target.map((v, i) => v + (goal[i] - v) * k);
        if (k < 1 && Math.hypot(goal[0] - this.target[0], goal[2] - this.target[2]) > 0.002) this.dirty = true;
      }
      const cam = basis(this.target, this.yaw, this.pitch, this.distance);
      const focal = Math.min(w, h) * 1.25;
      const project = p => {
        const d = [p[0] - cam.eye[0], p[1] - cam.eye[1], p[2] - cam.eye[2]];
        const z = d[0] * cam.f[0] + d[1] * cam.f[1] + d[2] * cam.f[2];
        if (z < 0.08) return null;
        return [w / 2 + (d[0] * cam.r[0] + d[1] * cam.r[1] + d[2] * cam.r[2]) * focal / z, h * 0.47 - (d[0] * cam.u[0] + d[1] * cam.u[1] + d[2] * cam.u[2]) * focal / z, z];
      };
      this.drawGrid(ctx, project);
      if (this.clip) {
        const pose = this.pose(this.frame);
        if (this.trail) this.drawTrail(ctx, project, index);
        if (this.clip.scene) this.drawScene(ctx, project, index, this.clip.scene, this.clip.bones);
        this.drawShadow(ctx, project, pose);
        this.drawBones(ctx, project, pose, focal);
      } else {
        this.drawScene(ctx, project, index, this.draftScene, []);
        this.sceneHandles = [];
        this.actorHandles = [];
        if (this.onSceneMove) {
          const seen = new Set();
          ctx.save();
          ctx.font = '12px sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          for (const part of this.draftScene.objects) {
            const id = part.id.split('-')[0];
            if (seen.has(id)) continue;
            seen.add(id);
            const [x,y,z] = part.center_m;
            const height = y+part.size_m[1]/2+.1;
            const hit = project([x,height,z]);
            if (!hit) continue;
            this.sceneHandles.push({id,x:hit[0],y:hit[1],height,depth:hit[2]});
          }
          this.sceneHandles.sort((a,b) => a.depth-b.depth);
          for (const hit of [...this.sceneHandles].reverse()) {
            ctx.fillStyle = '#0b0b0b';
            ctx.fillRect(hit.x - 10, hit.y - 10, 20, 20);
            ctx.fillStyle = '#f3f1ec';
            ctx.fillText(hit.id.replace(/^object/,''),hit.x,hit.y);
          }
          ctx.restore();
        }
        const drafts = this.draftScene.actorDrafts || [];
        if (drafts.length) {
          ctx.save();
          for (let idx = 0; idx < drafts.length; idx++) {
            const actor = drafts[idx];
            const ax = Number(actor.x) || 0, az = Number(actor.z) || 0;
            const rad = (Number(actor.heading_deg) || 0) * Math.PI / 180;
            const color = actor.color || ACTOR_COLORS[idx % ACTOR_COLORS.length];
            const label = actor.label || ('R' + (idx + 1));
            const active = Boolean(actor.active);
            ctx.strokeStyle = color;
            ctx.lineWidth = active ? 2.5 : 1.5;
            ctx.beginPath();
            let started = false;
            for (let s = 0; s <= 20; s++) {
              const th = (s / 20) * Math.PI * 2;
              const p = project([ax + Math.cos(th) * 0.22, 0.01, az + Math.sin(th) * 0.22]);
              if (!p) { started = false; continue; }
              if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
            }
            ctx.stroke();
            if (active) {
              ctx.lineWidth = 1.2;
              ctx.setLineDash([3, 3]);
              ctx.beginPath();
              started = false;
              for (let s = 0; s <= 24; s++) {
                const th = (s / 24) * Math.PI * 2;
                const p = project([ax + Math.cos(th) * 0.30, 0.01, az + Math.sin(th) * 0.30]);
                if (!p) { started = false; continue; }
                if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
              }
              ctx.stroke();
              ctx.setLineDash([]);
            }
            const dirX = Math.sin(rad), dirZ = Math.cos(rad);
            const base = project([ax, 0.015, az]);
            const tip = project([ax + dirX * 0.48, 0.015, az + dirZ * 0.48]);
            const left = project([ax + dirX * 0.36 - dirZ * 0.09, 0.015, az + dirZ * 0.36 + dirX * 0.09]);
            const right = project([ax + dirX * 0.36 + dirZ * 0.09, 0.015, az + dirZ * 0.36 - dirX * 0.09]);
            if (base && tip) {
              ctx.lineWidth = active ? 2.5 : 2;
              ctx.beginPath();
              ctx.moveTo(base[0], base[1]); ctx.lineTo(tip[0], tip[1]);
              if (left) { ctx.moveTo(tip[0], tip[1]); ctx.lineTo(left[0], left[1]); }
              if (right) { ctx.moveTo(tip[0], tip[1]); ctx.lineTo(right[0], right[1]); }
              ctx.stroke();
            }
            const height = 0.25;
            const hit = project([ax, height, az]);
            if (hit) this.actorHandles.push({ id: actor.id, x: hit[0], y: hit[1], height, depth: hit[2], color, label, active });
          }
          this.actorHandles.sort((a, b) => a.depth - b.depth);
          for (const hit of [...this.actorHandles].reverse()) {
            drawPill(ctx, hit.x, hit.y, hit.label, hit.color, hit.active ? '#ffffff' : null);
          }
          ctx.restore();
        }
      }
      this.drawGizmo(ctx, cam, h);
    }
    drawScene(ctx, project, index, scene, bones) {
      const line = (a,b) => { if (a && b) { ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); } };
      ctx.save();
      for (const object of scene.objects || []) {
        const selected = scene.selectedId && object.id.startsWith(scene.selectedId+'-');
        ctx.lineWidth = selected ? 2.5 : 1.5;
        ctx.strokeStyle = selected ? '#0b0b0b' : '#57544e';
        const center = interpolateTrajectory(object, this.frame);
        const angle = (object.heading_deg || 0) * Math.PI / 180;
        const sin = Math.sin(angle), cos = Math.cos(angle);
        const points = Array.from({length:8}, (_,corner) => {
          const [x,y,z] = object.size_m.map((size,axis) => size * (corner & (1 << axis) ? .5 : -.5));
          return project([center[0]+cos*x+sin*z,center[1]+y,center[2]-sin*x+cos*z]);
        });
        ctx.beginPath();
        for (let corner=0; corner<8; corner++) for (const axis of [1,2,4]) {
          if (!(corner & axis)) line(points[corner],points[corner|axis]);
        }
        ctx.stroke();
      }
      if (scene.partner?.[index]) {
        const points = scene.partner[index].map(project);
        ctx.lineWidth = 3; ctx.strokeStyle = '#8a6416'; ctx.beginPath();
        for (const bone of bones) if (bone.parent >= 0 && !bone.finger) line(points[bone.i],points[bone.parent]);
        ctx.stroke();
      }
      if (Array.isArray(scene.actors)) {
        const floorY = this.clip?.floor ?? 0;
        for (let idx = 0; idx < scene.actors.length; idx++) {
          const actor = scene.actors[idx];
          const frames = Array.isArray(actor) ? actor : actor?.joints;
          if (!Array.isArray(frames) || !frames.length) continue;
          const i = Math.max(0, Math.min(frames.length - 1, Math.floor(this.frame)));
          const n = Math.min(frames.length - 1, i + 1);
          const t = Math.max(0, Math.min(1, this.frame - i));
          const pa = frames[i], pb = frames[n] || pa;
          if (!Array.isArray(pa) || !pa.length) continue;
          const joints3d = pa.map((p, j) => {
            const q = pb[j] || p;
            return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t];
          });
          const color = actor?.color || ACTOR_COLORS[idx % ACTOR_COLORS.length];
          const label = actor?.label || ('R' + (idx + 1));
          let cx = 0, cz = 0;
          for (const p of joints3d) { cx += p[0]; cz += p[2]; }
          cx /= joints3d.length; cz /= joints3d.length;
          if (!actor?.primary) {
            const pts = [];
            for (let a = 0; a < 18; a++) {
              const th = a / 18 * Math.PI * 2;
              const sp = project([cx + Math.cos(th) * 0.34, floorY, cz + Math.sin(th) * 0.34]);
              if (!sp) { pts.length = 0; break; }
              pts.push(sp);
            }
            const centre = project([cx, floorY, cz]);
            if (pts.length && centre) {
              const radius = Math.max(1, ...pts.map(p => Math.hypot(p[0] - centre[0], p[1] - centre[1])));
              const g = ctx.createRadialGradient(centre[0], centre[1], 0, centre[0], centre[1], radius);
              g.addColorStop(0, 'rgba(11,11,11,.10)'); g.addColorStop(1, 'rgba(0,0,0,0)');
              ctx.fillStyle = g; ctx.beginPath();
              pts.forEach((p, k) => k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
              ctx.closePath(); ctx.fill();
            }
            const points = joints3d.map(project);
            ctx.lineCap = 'round';
            ctx.lineWidth = 2.6; ctx.strokeStyle = color; ctx.beginPath();
            for (const bone of bones) if (bone.parent >= 0 && !bone.finger) line(points[bone.i], points[bone.parent]);
            ctx.stroke();
            const headBone = bones.find(b => b.head);
            const hp = headBone ? points[headBone.i] : null;
            if (hp) {
              ctx.fillStyle = color;
              ctx.beginPath(); ctx.arc(hp[0], hp[1], 4.5, 0, Math.PI * 2); ctx.fill();
            }
          }
          const headBone = bones.find(b => b.head);
          const headPos = headBone && joints3d[headBone.i] ? joints3d[headBone.i] : joints3d[0];
          const badge = project([headPos[0], headPos[1] + (headBone && joints3d[headBone.i] ? 0.24 : 0.95), headPos[2]]);
          if (badge) drawPill(ctx, badge[0], badge[1], label, color, '#f3f1ec');
        }
      }
      const target = (scene.targets || []).find(pose => pose.frame === index);
      if (target) {
        const points = target.joints.map(project);
        ctx.strokeStyle = '#8a6416'; ctx.setLineDash([4,3]); ctx.beginPath();
        for (const bone of bones) if (!bone.finger && bone.parent >= 0) line(points[bone.i],points[bone.parent]);
        ctx.stroke();
      }
      ctx.restore();
    }
    drawGrid(ctx, project) {
      const y = this.clip?.floor ?? 0, cx = Math.round(this.target[0] * 2) / 2, cz = Math.round(this.target[2] * 2) / 2;
      const R = 7, step = 0.5, seg = 0.5;
      // Segments are grouped into a few opacity levels and stroked as one path per level:
      // ~16 draw calls per frame instead of ~1600, with the same radial fade.
      const LEVELS = 8, paths = Array.from({ length: 2 * LEVELS }, () => new Path2D()), used = new Uint8Array(2 * LEVELS);
      for (let axis = 0; axis < 2; axis++) {
        for (let o = -R; o <= R + 1e-6; o += step) {
          const major = Math.abs(((axis ? cz : cx) + o) % 1) < 1e-6;
          for (let s = -R; s < R; s += seg) {
            const a = axis ? [cx + s, y, cz + o] : [cx + o, y, cz + s];
            const b = axis ? [cx + s + seg, y, cz + o] : [cx + o, y, cz + s + seg];
            const fade = Math.max(0, 1 - Math.hypot((a[0] + b[0]) / 2 - this.target[0], (a[2] + b[2]) / 2 - this.target[2]) / R);
            if (fade <= 0) continue;
            const pa = project(a), pb = project(b); if (!pa || !pb) continue;
            const k = (major ? LEVELS : 0) + Math.min(LEVELS - 1, Math.floor(fade * fade * LEVELS));
            paths[k].moveTo(pa[0], pa[1]); paths[k].lineTo(pb[0], pb[1]); used[k] = 1;
          }
        }
      }
      ctx.lineWidth = 1;
      for (let k = 0; k < paths.length; k++) {
        if (!used[k]) continue;
        const level = ((k % LEVELS) + 0.5) / LEVELS;
        ctx.strokeStyle = `rgba(11,11,11,${(k >= LEVELS ? 0.13 : 0.05) * level})`; ctx.stroke(paths[k]);
      }
    }
    drawTrail(ctx, project, index) {
      const c = this.clip, y = c.floor + 0.002;
      const point = f => project([c.root[f * 3], y, c.root[f * 3 + 2]]);
      const stride = Math.max(1, Math.floor(c.frames / 400));
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.setLineDash([3, 5]); ctx.lineWidth = 1.4; ctx.strokeStyle = 'rgba(11,11,11,.2)';
      ctx.beginPath(); let started = false;
      for (let f = 0; f < c.frames; f += stride) { const p = point(f); if (!p) { started = false; continue; } if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]); }
      ctx.stroke(); ctx.setLineDash([]);
      ctx.lineWidth = 1.6; ctx.strokeStyle = `rgba(${ACCENT},.85)`;
      ctx.beginPath(); started = false;
      for (let f = 0; f <= index; f += stride) { const p = point(f); if (!p) { started = false; continue; } if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]); }
      const now = point(index); if (now && started) ctx.lineTo(now[0], now[1]);
      ctx.stroke();
      const start = point(0); if (start) { ctx.fillStyle = 'rgba(11,11,11,.4)'; ctx.beginPath(); ctx.arc(start[0], start[1], 3, 0, Math.PI * 2); ctx.fill(); }
    }
    drawShadow(ctx, project, pose) {
      const c = this.clip, y = c.floor;
      let x = 0, z = 0; for (let j = 0; j < c.count; j++) { x += pose[j * 3]; z += pose[j * 3 + 2]; }
      x /= c.count; z /= c.count;
      const pts = []; for (let a = 0; a < 24; a++) { const t = a / 24 * Math.PI * 2; const p = project([x + Math.cos(t) * 0.38, y, z + Math.sin(t) * 0.38]); if (!p) return; pts.push(p); }
      const centre = project([x, y, z]); if (!centre) return;
      const radius = Math.max(...pts.map(p => Math.hypot(p[0] - centre[0], p[1] - centre[1])));
      const g = ctx.createRadialGradient(centre[0], centre[1], 0, centre[0], centre[1], radius);
      g.addColorStop(0, 'rgba(11,11,11,.12)'); g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g; ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.closePath(); ctx.fill();
    }
    drawBones(ctx, project, pose, focal) {
      const c = this.clip, pts = [];
      for (let j = 0; j < c.count; j++) pts.push(project([pose[j * 3], pose[j * 3 + 1], pose[j * 3 + 2]]));
      const segments = [];
      for (const b of c.bones) {
        if (b.parent < 0) continue;
        const a = pts[b.i], p = pts[b.parent]; if (!a || !p) continue;
        segments.push({ a, p, z: (a[2] + p[2]) / 2, side: b.side === 'C' ? c.bones[b.parent].side : b.side, finger: b.finger });
      }
      segments.sort((s, t) => t.z - s.z);
      ctx.lineCap = 'round';
      const zs = pts.filter(Boolean).map(p => p[2]), zmin = Math.min(...zs), zmax = Math.max(...zs);
      const shade = z => 1 - 0.35 * ((z - zmin) / Math.max(1e-3, zmax - zmin));
      for (const s of segments) {
        const radius = s.finger ? 0.007 : 0.024;
        const col = COLORS[s.side] || COLORS.C, k = shade(s.z);
        ctx.strokeStyle = `rgba(${col.map((v, i) => Math.round(v + ([235, 232, 225][i] - v) * (1 - k))).join(',')},${s.finger ? 0.7 : 0.95})`;
        ctx.lineWidth = Math.max(s.finger ? 0.8 : 2, radius * 2 * focal / s.z);
        ctx.beginPath(); ctx.moveTo(s.p[0], s.p[1]); ctx.lineTo(s.a[0], s.a[1]); ctx.stroke();
      }
      for (const b of c.bones) {
        const p = pts[b.i]; if (!p || b.finger) continue;
        const r = b.head ? Math.max(5, 0.1 * focal / p[2]) : Math.max(1.6, 0.022 * focal / p[2]);
        const col = b.head ? COLORS.C : COLORS[b.side];
        ctx.fillStyle = b.head ? 'rgb(11,11,11)' : `rgb(${col.join(',')})`;
        ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, Math.PI * 2); ctx.fill();
        if (!b.head) { ctx.fillStyle = 'rgb(235,232,225)'; ctx.beginPath(); ctx.arc(p[0], p[1], r * 0.45, 0, Math.PI * 2); ctx.fill(); }
      }
    }
    drawGizmo(ctx, cam, h) {
      if (this.thumbnail) return;
      const o = [34, 46], L = 18; // top-left, clear of the floating transport
      const axes = [['X', [1, 0, 0], '#57544e'], ['Y', [0, 1, 0], '#57544e'], ['Z', [0, 0, 1], '#57544e']];
      // Reading computed style forces a style recalculation; do it once, not every frame.
      ctx.font = GIZMO_FONT || (GIZMO_FONT = '600 10px ' + getComputedStyle(document.body).fontFamily); ctx.lineWidth = 1.6;
      for (const [label, v, color] of axes) {
        const x = v[0] * cam.r[0] + v[1] * cam.r[1] + v[2] * cam.r[2], y = v[0] * cam.u[0] + v[1] * cam.u[1] + v[2] * cam.u[2];
        ctx.strokeStyle = color; ctx.fillStyle = color;
        ctx.beginPath(); ctx.moveTo(o[0], o[1]); ctx.lineTo(o[0] + x * L, o[1] - y * L); ctx.stroke();
        ctx.fillText(label, o[0] + x * (L + 7) - 3, o[1] - y * (L + 7) + 3);
      }
    }
  }

  // Static mid-frame thumbnail from /api/motions/{id}/thumbnail (joints[count][3]).
  function drawThumbnail(canvas, clip) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2), w = canvas.clientWidth || 84, h = canvas.clientHeight || 96;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const joints = clip.joints, bones = makeSkeleton(clip.parents, clip.names);
    const yaw = 0.62, cy = Math.cos(yaw), sy = Math.sin(yaw);
    const flat = joints.map(p => [p[0] * cy - p[2] * sy, p[1], p[0] * sy + p[2] * cy]);
    const xs = flat.map(p => p[0]), ys = flat.map(p => p[1]);
    const lo = [Math.min(...xs), Math.min(...ys)], hi = [Math.max(...xs), Math.max(...ys)];
    const scale = Math.min((w - 16) / Math.max(0.4, hi[0] - lo[0]), (h - 18) / Math.max(0.9, hi[1] - lo[1]));
    const proj = p => [w / 2 + (p[0] - (lo[0] + hi[0]) / 2) * scale, h - 9 - (p[1] - lo[1]) * scale];
    ctx.fillStyle = 'rgba(0,0,0,.12)'; ctx.beginPath(); ctx.ellipse(w / 2, h - 9, 18, 3.5, 0, 0, Math.PI * 2); ctx.fill();
    ctx.lineCap = 'round';
    const order = bones.filter(b => b.parent >= 0).sort((a, b) => flat[b.i][2] - flat[a.i][2]);
    for (const b of order) {
      const a = proj(flat[b.i]), p = proj(flat[b.parent]); const side = b.side === 'C' ? bones[b.parent].side : b.side;
      ctx.strokeStyle = `rgba(${COLORS[side]},${b.finger ? 0.6 : 0.95})`; ctx.lineWidth = b.finger ? 0.6 : 2;
      ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(a[0], a[1]); ctx.stroke();
    }
    const head = bones.find(b => b.head); if (head) { const p = proj(flat[head.i]); ctx.fillStyle = 'rgb(11,11,11)'; ctx.beginPath(); ctx.arc(p[0], p[1], Math.max(3, 0.09 * scale), 0, Math.PI * 2); ctx.fill(); }
  }

  return { View, decodePreview, decodeJsonPreview, decodePreviewCoordinates, validatePreviewParents,
    previewQualityEvents, drawThumbnail };
})();
