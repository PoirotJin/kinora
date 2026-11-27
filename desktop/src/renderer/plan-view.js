'use strict';
// Top-down monitor: world X/Z ground plane, +Z up the screen. Drag a clip's
// root path to move it; drag the ring knob (or rotate two fingers on a
// trackpad) to turn the whole clip about its first frame. Shift snaps.
const PlanView = (() => {
  const INK = '11,11,11', STONE = '140,134,124', BAD = '125,30,30';
  class View {
    constructor(canvas, { change, select, commit }) {
      this.canvas = canvas; this.ctx = canvas.getContext('2d');
      this.change = change; this.select = select; this.commit = commit;
      this.seq = null; this.assembled = null; this.selected = -1; this.frame = 0; this.status = null;
      this.k = 70; this.ox = 0; this.oz = 0; this.drag = null; this.dirty = true; this.fitted = false;
      canvas.addEventListener('pointerdown', e => this.down(e));
      canvas.addEventListener('pointermove', e => this.moveTo(e));
      canvas.addEventListener('pointerup', e => this.up(e));
      canvas.addEventListener('pointercancel', e => this.abort(e));
      canvas.addEventListener('lostpointercapture', e => this.up(e));
      canvas.addEventListener('wheel', e => this.wheel(e), { passive: false });
      canvas.addEventListener('dblclick', () => this.fit());
      canvas.addEventListener('keydown', e => this.key(e));
      // WebKit (macOS WKWebView) trackpad rotation; other WebViews use the knob or Shift+wheel.
      canvas.addEventListener('gesturestart', e => {
        if (this.suspended || this.locked?.()) return;
        e.preventDefault();
        const clip = this.current();
        if (clip) this.gesture = { clip, turn: clip.turn, moved: false };
      });
      canvas.addEventListener('gesturechange', e => {
        e.preventDefault();
        const gesture = this.gesture;
        if (!gesture || gesture.clip !== this.current() || this.locked?.()) return;
        gesture.clip.turn = this.snap(gesture.turn + e.rotation, e.shiftKey);
        gesture.moved = true;
        this.change(gesture.clip);
      });
      canvas.addEventListener('gestureend', e => {
        e.preventDefault();
        const gesture = this.gesture;
        this.gesture = null;
        if (gesture?.moved && gesture.clip === this.current()) this.commit();
      });
      new ResizeObserver(() => { this.dirty = true; this.schedule(); }).observe(canvas);
      this.suspended = false; this.raf = 0;
      this._tick = () => {
        this.raf = 0;
        if (this.suspended) return;
        if (this.dirty && canvas.isConnected && canvas.clientWidth) { this.dirty = false; this.draw(); }
        this.schedule();
      };
      this.schedule();
    }
    schedule() {
      if (!this.suspended && !this.raf && typeof requestAnimationFrame === 'function') {
        this.raf = requestAnimationFrame(this._tick);
      }
    }
    suspend() {
      this.suspended = true;
      this.stopInput();
      if (this.raf && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
    resume() {
      this.suspended = false;
      this.dirty = true;
      this.schedule();
    }
    set(seq, assembled, selected, status) {
      if (seq !== this.seq || (this.drag?.clip && !seq?.clips.includes(this.drag.clip))) this.stopInput();
      if (this.gesture && this.gesture.clip !== seq?.clips[selected]) this.gesture = null;
      this.seq = seq; this.assembled = assembled; this.selected = selected; this.status = status;
      if (!this.fitted && seq?.clips.length) this.fit();
      this.dirty = true;
    }
    setFrame(frame) { this.frame = frame; this.dirty = true; }
    current() { return this.seq?.clips[this.selected] || null; }
    snap(turn, on) { turn = ((turn + 180) % 360 + 360) % 360 - 180; return on ? Math.round(turn / 15) * 15 : Math.round(turn * 10) / 10; }
    size() { return [this.canvas.clientWidth, this.canvas.clientHeight]; }
    screen(x, z) { const [w, h] = this.size(); return [w / 2 - (x - this.ox) * this.k, h / 2 - (z - this.oz) * this.k]; }
    world(sx, sy) { const [w, h] = this.size(); return [this.ox - (sx - w / 2) / this.k, this.oz - (sy - h / 2) / this.k]; }
    path(clip) {
      const out = [], stride = Math.max(1, Math.floor((clip.out - clip.in) / 300));
      for (let f = clip.in; f < clip.out; f += stride) { const p = Sequence.root(clip, f); out.push(this.screen(p[0], p[2])); }
      const p = Sequence.root(clip, clip.out - 1); out.push(this.screen(p[0], p[2]));
      return out;
    }
    knob(clip) {
      const p = Sequence.root(clip, clip.in), h = Sequence.heading(clip, clip.in), [px, py] = this.screen(p[0], p[2]), r = 44;
      return { px, py, x: px - Math.sin(h) * r, y: py - Math.cos(h) * r, r };
    }
    fit() {
      if (!this.seq?.clips.length) return;
      let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
      for (const c of this.seq.clips) for (let f = c.in; f < c.out; f += 4) { const p = Sequence.root(c, f); lo = [Math.min(lo[0], p[0]), Math.min(lo[1], p[2])]; hi = [Math.max(hi[0], p[0]), Math.max(hi[1], p[2])]; }
      const [w, h] = this.size(); if (!w || !h) return;
      this.ox = (lo[0] + hi[0]) / 2; this.oz = (lo[1] + hi[1]) / 2;
      this.k = Math.max(8, Math.min(240, Math.min((w - 120) / Math.max(1, hi[0] - lo[0]), (h - 120) / Math.max(1, hi[1] - lo[1]))));
      this.fitted = true; this.dirty = true;
    }
    hit(sx, sy) {
      const clips = this.seq?.clips || [], near = (pts, d = 9) => pts.some((p, i) => i && segment(sx, sy, pts[i - 1], p) <= d);
      const c = this.current();
      if (c) { const k = this.knob(c); if (Math.hypot(sx - k.x, sy - k.y) <= 11) return { kind: 'rotate', index: this.selected }; }
      const order = clips.map((_, i) => i).sort((a, b) => (b === this.selected) - (a === this.selected));
      for (const i of order) if (near(this.path(clips[i]))) return { kind: 'move', index: i };
      return { kind: 'pan' };
    }
    point(e) { const r = this.canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
    down(e) {
      if (e.button !== 0 || this.suspended || this.drag || !this.seq || this.locked?.()) return;
      e.preventDefault(); this.canvas.focus({ preventScroll: true });
      const [sx, sy] = this.point(e), target = this.hit(sx, sy);
      if (target.index !== undefined && target.index !== this.selected) this.select(target.index);
      const c = this.seq.clips[target.index];
      this.canvas.setPointerCapture(e.pointerId);
      this.drag = { ...target, clip: c, id: e.pointerId, sx, sy, ox: this.ox, oz: this.oz, turn: c?.turn, dx: c?.dx, dz: c?.dz, moved: false };
      if (target.kind === 'rotate') { const k = this.knob(c); this.drag.a0 = Math.atan2(sy - k.py, sx - k.px); }
      this.cursor(target.kind);
    }
    moveTo(e) {
      const [sx, sy] = this.point(e), d = this.drag;
      if (!d) { if (this.seq) this.cursor(this.hit(sx, sy).kind, true); return; }
      if (e.pointerId !== d.id) return;
      if (!e.buttons) { this.up(e); return; }
      d.moved ||= Math.hypot(sx - d.sx, sy - d.sy) > 2;
      if (d.kind === 'pan') { this.ox = d.ox + (sx - d.sx) / this.k; this.oz = d.oz + (sy - d.sy) / this.k; this.dirty = true; return; }
      const c = d.clip; if (!c) return;
      if (d.kind === 'move') { c.dx = d.dx - (sx - d.sx) / this.k; c.dz = d.dz - (sy - d.sy) / this.k; }
      else { const k = this.knob({ ...c, turn: d.turn }); c.turn = this.snap(d.turn + (Math.atan2(sy - k.py, sx - k.px) - d.a0) * 180 / Math.PI, e.shiftKey); }
      this.change(c);
    }
    releaseDrag() {
      const drag = this.drag;
      this.drag = null;
      if (drag && this.canvas.hasPointerCapture(drag.id)) this.canvas.releasePointerCapture(drag.id);
      this.cursor('');
    }
    stopInput(commit = false) {
      const changed = this.drag?.clip && this.drag.moved || this.gesture?.moved || this.wheelCommit;
      this.releaseDrag();
      this.gesture = null;
      clearTimeout(this.wheelCommit);
      this.wheelCommit = null;
      if (commit && changed) this.commit();
    }
    up(e) {
      const drag = this.drag;
      if (!drag || drag.id !== e.pointerId) return;
      this.releaseDrag();
      if (drag.clip && drag.moved) this.commit();
    }
    abort(e) {
      const drag = this.drag;
      if (!drag || drag.id !== e.pointerId) return;
      this.releaseDrag();
      if (drag.clip) {
        Object.assign(drag.clip, { turn: drag.turn, dx: drag.dx, dz: drag.dz });
        this.change(drag.clip);
      }
    }
    cursor(kind, hover) { this.canvas.style.cursor = kind === 'rotate' ? (hover ? 'grab' : 'grabbing') : kind === 'move' ? (hover ? 'move' : 'grabbing') : kind === 'pan' && !hover ? 'grabbing' : ''; }
    wheel(e) {
      e.preventDefault(); const c = this.current();
      if (e.shiftKey && !e.ctrlKey && c) {
        if (this.suspended || this.locked?.()) return;
        c.turn = this.snap(c.turn + Math.sign(e.deltaY || e.deltaX) * 5, false);
        this.change(c);
        clearTimeout(this.wheelCommit);
        this.wheelCommit = setTimeout(() => { this.wheelCommit = null; this.commit(); }, 250);
        return;
      }
      const [sx, sy] = this.point(e), before = this.world(sx, sy);
      this.k = Math.max(8, Math.min(400, this.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))));
      const after = this.world(sx, sy); this.ox += before[0] - after[0]; this.oz += before[1] - after[1]; this.dirty = true;
    }
    key(e) {
      if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
      const c = this.current(); if (!c || this.locked?.()) return;
      const step = e.shiftKey ? 0.25 : 0.05, turn = e.shiftKey ? 15 : 5;
      const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (moves[e.key]) { c.dx += moves[e.key][0]; c.dz += moves[e.key][1]; }
      else if (e.key === '[' || e.key === ']') c.turn = this.snap(c.turn + (e.key === ']' ? turn : -turn), false);
      else return;
      e.preventDefault(); e.stopPropagation(); this.change(c); this.commit();
    }
    draw() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2), [w, h] = this.size(), ctx = this.ctx;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) { this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr); }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      this.grid(ctx, w, h);
      const clips = this.seq?.clips || []; if (!clips.length) return;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      if (clips.length > 1) {
        const a = Sequence.root(clips[0], clips[0].out - 1), b = Sequence.root(clips[1], clips[1].in), p = this.screen(a[0], a[2]), q = this.screen(b[0], b[2]);
        ctx.setLineDash([5, 5]); ctx.lineWidth = 1.5; ctx.strokeStyle = `rgba(${this.status && !this.status.ok ? BAD : INK},.65)`;
        ctx.beginPath(); ctx.moveTo(...p); ctx.lineTo(...q); ctx.stroke(); ctx.setLineDash([]);
      }
      clips.forEach((c, i) => {
        const pts = this.path(c), color = i ? STONE : INK, chosen = i === this.selected;
        ctx.strokeStyle = `rgba(${color},${chosen ? 1 : .7})`; ctx.lineWidth = chosen ? 3 : 2;
        ctx.beginPath(); pts.forEach((p, n) => n ? ctx.lineTo(...p) : ctx.moveTo(...p)); ctx.stroke();
        const start = pts[0], end = pts.at(-1), he = Sequence.heading(c, c.out - 1);
        ctx.fillStyle = `rgb(${color})`; ctx.beginPath(); ctx.arc(...start, 4.5, 0, Math.PI * 2); ctx.fill();
        arrow(ctx, end, -Math.sin(he), -Math.cos(he), color);
        ctx.font = '600 11px ' + getComputedStyle(document.body).fontFamily; ctx.fillText(String.fromCharCode(65 + i), start[0] + 8, start[1] - 8);
      });
      const c = this.current();
      if (c) {
        const k = this.knob(c);
        ctx.strokeStyle = `rgba(${INK},.25)`; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(k.px, k.py, k.r, 0, Math.PI * 2); ctx.stroke();
        ctx.strokeStyle = `rgba(${INK},.6)`; ctx.beginPath(); ctx.moveTo(k.px, k.py); ctx.lineTo(k.x, k.y); ctx.stroke();
        ctx.fillStyle = 'rgb(11,11,11)'; ctx.beginPath(); ctx.arc(k.x, k.y, 7, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = 'rgb(243,241,236)'; ctx.beginPath(); ctx.arc(k.x, k.y, 2.5, 0, Math.PI * 2); ctx.fill();
      }
      const a = this.assembled; if (!a) return;
      const f = Math.max(0, Math.min(a.frames - 1, Math.floor(this.frame))), o = f * a.meta.joint_count * 3, [fx, fy] = this.screen(a.data[o], a.data[o + 2]);
      ctx.strokeStyle = 'rgb(11,11,11)'; ctx.lineWidth = 1.5; ctx.fillStyle = 'rgba(243,241,236,.9)';
      ctx.beginPath(); ctx.arc(fx, fy, 8, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
    grid(ctx, w, h) {
      const step = this.k < 25 ? 5 : this.k < 60 ? 1 : 0.5, [x0, z0] = this.world(w, h), [x1, z1] = this.world(0, 0);
      ctx.lineWidth = 1;
      for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) { const [sx] = this.screen(x, 0); ctx.strokeStyle = `rgba(${INK},${Math.abs(x % (step * 2)) < 1e-6 ? .1 : .05})`; ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke(); }
      for (let z = Math.floor(z0 / step) * step; z <= z1; z += step) { const [, sy] = this.screen(0, z); ctx.strokeStyle = `rgba(${INK},${Math.abs(z % (step * 2)) < 1e-6 ? .1 : .05})`; ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(w, sy); ctx.stroke(); }
      const [ax, ay] = this.screen(0, 0); ctx.strokeStyle = `rgba(${INK},.3)`; ctx.beginPath(); ctx.moveTo(ax - 6, ay); ctx.lineTo(ax + 6, ay); ctx.moveTo(ax, ay - 6); ctx.lineTo(ax, ay + 6); ctx.stroke();
      ctx.fillStyle = `rgba(${INK},.55)`; ctx.font = '10px ' + getComputedStyle(document.body).fontFamily;
      ctx.fillText(`网格 ${step} 米 · 上方为 +Z`, 12, h - 12);
    }
  }
  function segment(x, y, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1], l = dx * dx + dy * dy, t = l ? Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / l)) : 0;
    return Math.hypot(x - a[0] - t * dx, y - a[1] - t * dy);
  }
  function arrow(ctx, p, ux, uy, color) {
    const n = Math.hypot(ux, uy) || 1; ux /= n; uy /= n; const s = 9;
    ctx.fillStyle = `rgb(${color})`; ctx.beginPath(); ctx.moveTo(p[0] + ux * s, p[1] + uy * s);
    ctx.lineTo(p[0] - uy * s * .55, p[1] + ux * s * .55); ctx.lineTo(p[0] + uy * s * .55, p[1] - ux * s * .55); ctx.closePath(); ctx.fill();
  }
  return { View, segment };
})();
