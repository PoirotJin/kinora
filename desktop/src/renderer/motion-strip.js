'use strict';
// Timeline strip for a motion clip, drawn on one canvas: evenly spaced poses at a
// single shared scale on a common ground line (chronophotography style), each
// turned to a three-quarter side view of its own heading, over a band showing
// how fast the body moves. Proportions never depend on the strip's size.
const MotionStrip = (() => {
  const INK = '11,11,11', STONE = '163,158,148';
  const cache = new WeakMap();
  const side = n => /^(l_|left)/i.test(n) ? 'L' : /^(r_|right)/i.test(n) ? 'R' : 'C';
  const finger = n => /index|middle|ring|pinky|thumb|finger/i.test(n);

  function analyse(src) {
    let a = cache.get(src); if (a) return a;
    const { data, count, frames } = src, names = src.meta.names, parents = src.meta.parents, fps = src.fps || src.meta.fps || 30;
    const body = names.map((n, i) => !finger(n) && i < count);
    const sides = names.map(side);
    const hip = s => names.findIndex((n, i) => sides[i] === s && /hip|upleg|thigh/i.test(n));
    const lh = hip('L'), rh = hip('R');
    let floor = Infinity, top = 0;
    const lows = new Float32Array(frames), speed = new Float32Array(frames), heading = new Float32Array(frames * 2);
    for (let f = 0; f < frames; f++) {
      let lo = Infinity, hi = -Infinity;
      for (let j = 0; j < count; j++) if (body[j]) { const y = data[(f * count + j) * 3 + 1]; lo = Math.min(lo, y); hi = Math.max(hi, y); }
      lows[f] = lo; floor = Math.min(floor, lo); top = Math.max(top, hi);
    }
    for (let f = 0; f < frames; f++) {
      // Mean body-joint speed (m/s), the activity band.
      if (f) {
        let s = 0, n = 0;
        for (let j = 0; j < count; j++) if (body[j]) {
          const o = (f * count + j) * 3, p = o - count * 3;
          s += Math.hypot(data[o] - data[p], data[o + 1] - data[p + 1], data[o + 2] - data[p + 2]); n++;
        }
        speed[f] = n ? s / n * fps : 0;
      }
      // Facing from the hip line, averaged over ±6 frames so a sample never flips.
      let rx = 0, rz = 0;
      if (lh >= 0 && rh >= 0) for (let g = Math.max(0, f - 6); g <= Math.min(frames - 1, f + 6); g++) {
        const l = (g * count + lh) * 3, r = (g * count + rh) * 3; rx += data[r] - data[l]; rz += data[r + 2] - data[l + 2];
      }
      const len = Math.hypot(rx, rz);
      // right = L→R hip; forward = up × right.
      if (len > 1e-6) { heading[f * 2] = rz / len; heading[f * 2 + 1] = -rx / len; } else { heading[f * 2] = 0; heading[f * 2 + 1] = 1; }
    }
    if (frames > 1) speed[0] = speed[1];
    const smooth = new Float32Array(frames);
    for (let f = 0; f < frames; f++) { let s = 0, n = 0; for (let g = Math.max(0, f - 3); g <= Math.min(frames - 1, f + 3); g++) { s += speed[g]; n++; } smooth[f] = s / n; }
    const order = names.map((_, i) => i).filter(i => body[i] && parents[i] >= 0 && body[parents[i]]);
    const head = names.findIndex(n => /^head$/i.test(n));
    a = { fps, floor: Number.isFinite(floor) ? floor : 0, height: Math.max(1, top - floor), speed: smooth, heading, sides, parents, order, head };
    cache.set(src, a); return a;
  }

  function size(canvas) {
    const w = canvas.clientWidth, h = canvas.clientHeight; if (!w || !h) return null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2, 16000 / w);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  // Draw frames [from, to) of src across the whole canvas.
  // top: px reserved for an overlaid label; band: activity band height in px.
  function draw(canvas, src, from = 0, to = src.frames, { top = 16, band = 16 } = {}) {
    const box = size(canvas); if (!box || !src || to <= from) return;
    const { ctx, w, h } = box, a = analyse(src), { data, count } = src;
    const ground = h - band - 3, figure = Math.max(12, ground - top - 2), scale = figure / a.height;
    const yaw = 0.5, cy = Math.cos(yaw), sy = Math.sin(yaw);
    // Ground rule and activity band.
    ctx.fillStyle = `rgba(${INK},.18)`; ctx.fillRect(0, ground, w, 1);
    const peak = Math.max(1.2, ...Array.from(a.speed.subarray(from, to)));
    ctx.beginPath(); ctx.moveTo(0, h);
    const steps = Math.min(w, (to - from) * 2);
    for (let i = 0; i <= steps; i++) { const f = Math.min(to - 1, from + Math.floor(i / steps * (to - from))); ctx.lineTo(i / steps * w, h - 1 - a.speed[f] / peak * (band - 3)); }
    ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = `rgba(${INK},.10)`; ctx.fill();
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) { const f = Math.min(to - 1, from + Math.floor(i / steps * (to - from))), x = i / steps * w, y = h - 1 - a.speed[f] / peak * (band - 3); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
    ctx.strokeStyle = `rgba(${INK},.45)`; ctx.lineWidth = 1; ctx.stroke();
    // Poses: one every ~0.5 figure heights, never closer than 14 px.
    const slots = Math.max(1, Math.min(400, to - from, Math.floor(w / Math.max(14, figure * 0.5))));
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    const line = Math.max(1, Math.min(2.2, figure * 0.022));
    for (let k = 0; k < slots; k++) {
      const f = Math.min(to - 1, from + Math.floor((k + .5) / slots * (to - from))), cx = (k + .5) / slots * w;
      const fx = a.heading[f * 2], fz = a.heading[f * 2 + 1], o0 = f * count * 3, root = [data[o0], data[o0 + 2]];
      const proj = j => {
        const o = (f * count + j) * 3, x = data[o] - root[0], z = data[o + 2] - root[1];
        const along = x * fx + z * fz, across = x * fz - z * fx;
        return [cx + (along * cy + across * sy) * scale, ground - (data[o + 1] - a.floor) * scale, along * sy - across * cy];
      };
      const pts = new Map(); const at = j => { let p = pts.get(j); if (!p) pts.set(j, p = proj(j)); return p; };
      const bones = a.order.slice().sort((i, j) => at(j)[2] - at(i)[2]);
      for (const j of bones) {
        const p = at(j), q = at(a.parents[j]), s = a.sides[j] === 'C' ? a.sides[a.parents[j]] : a.sides[j];
        ctx.strokeStyle = `rgb(${s === 'R' ? STONE : INK})`; ctx.lineWidth = line;
        ctx.beginPath(); ctx.moveTo(q[0], q[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
      }
      if (a.head >= 0) { const p = at(a.head); ctx.fillStyle = `rgb(${INK})`; ctx.beginPath(); ctx.arc(p[0], p[1], Math.max(1.6, figure * 0.045), 0, Math.PI * 2); ctx.fill(); }
    }
  }

  return { draw, analyse };
})();
