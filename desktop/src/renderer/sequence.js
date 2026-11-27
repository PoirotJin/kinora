'use strict';
// Pure model for the two-clip sequence editor. World is Y-up metres; the
// ground plane is X/Z. A clip transform is a rigid rotation about the vertical
// axis through the clip's in-frame root, followed by a ground-plane offset.
// Body poses are never changed: only root position and heading move.
const Sequence = (() => {
  const MAX_CLIPS = 2, MIN_CLIP = 12;
  const TRANSITION = { min: 0.3, max: 18, auto_min: 0.6, auto_max: 18, walk_m_s: 1.2, max_m_s: 1.6, max_turn_deg_s: 120 };
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;
  const wrap = a => { a = (a + Math.PI) % (2 * Math.PI); return (a < 0 ? a + 2 * Math.PI : a) - Math.PI; };

  // turn: degrees, positive = the clip turns to its right (clockwise seen from above).
  const yawOf = clip => -rad(clip.turn || 0);
  const point = (src, f, j) => { const o = (f * src.count + j) * 3; return [src.data[o], src.data[o + 1], src.data[o + 2]]; };
  const pivotOf = (clip, at = clip.in) => { const p = point(clip.src, at, 0); return [p[0], p[2]]; };

  function transform(clip, p) {
    const y = yawOf(clip), c = Math.cos(y), s = Math.sin(y), [px, pz] = pivotOf(clip);
    const x = p[0] - px, z = p[2] - pz;
    return [c * x + s * z + px + clip.dx, p[1], -s * x + c * z + pz + clip.dz];
  }

  function create(src, id) {
    if (!src || !Number.isInteger(src.frames) || src.frames < MIN_CLIP || !src.count || !(src.data instanceof Float32Array)) throw new Error('素材至少需要 ' + MIN_CLIP + ' 帧');
    return { id, src, in: 0, out: src.frames, turn: 0, dx: 0, dz: 0 };
  }

  // Changing the in-point moves the rotation pivot; compensate the offset so
  // that the visible trajectory stays exactly where it was.
  function trim(clip, start, end) {
    const frames = clip.src.frames;
    start = clamp(Math.round(start), 0, frames - MIN_CLIP);
    end = clamp(Math.round(end), start + MIN_CLIP, frames);
    if (start !== clip.in) {
      const [ax, az] = pivotOf(clip), [bx, bz] = pivotOf(clip, start), y = yawOf(clip), c = Math.cos(y), s = Math.sin(y);
      const ux = ax - bx, uz = az - bz;
      clip.dx += ux - (c * ux + s * uz); clip.dz += uz - (-s * ux + c * uz);
    }
    clip.in = start; clip.out = end;
    return clip;
  }

  function hips(src) {
    const names = src.meta?.names || [], find = re => names.findIndex(n => re.test(String(n)));
    const l = find(/^(l_|left_?)(hip|up_?leg|thigh)/i), r = find(/^(r_|right_?)(hip|up_?leg|thigh)/i);
    if (l >= 0 && r >= 0) return [l, r];
    // SMPL/SMPL-H order (pelvis, left hip, right hip) when names are bare indices.
    return src.count >= 22 && names.slice(0, 3).join() === '0,1,2' ? [1, 2] : null;
  }

  // Facing direction on the ground plane, as an angle atan2(x, z) (0 = +Z).
  function heading(clip, f) {
    const pair = hips(clip.src);
    if (pair) {
      const l = transform(clip, point(clip.src, f, pair[0])), r = transform(clip, point(clip.src, f, pair[1]));
      const ax = l[0] - r[0], az = l[2] - r[2];
      if (Math.hypot(ax, az) > 1e-4) return Math.atan2(-az, ax);
    }
    const a = transform(clip, point(clip.src, clamp(f - 5, clip.in, clip.out - 1), 0)), b = transform(clip, point(clip.src, clamp(f + 5, clip.in, clip.out - 1), 0));
    return Math.hypot(b[0] - a[0], b[2] - a[2]) > 1e-3 ? Math.atan2(b[0] - a[0], b[2] - a[2]) : 0;
  }

  const root = (clip, f) => transform(clip, point(clip.src, f, 0));

  function gap(a, b) {
    const p = root(a, a.out - 1), q = root(b, b.in), ha = heading(a, a.out - 1), hb = heading(b, b.in);
    return { distance: Math.hypot(q[0] - p[0], q[2] - p[2]), turn_deg: Math.abs(deg(wrap(hb - ha))), from: p, to: q, ha, hb };
  }

  function suggest(a, b, fps) {
    const g = gap(a, b);
    const seconds = clamp(Math.max(g.distance / TRANSITION.walk_m_s, g.turn_deg / 90),
      TRANSITION.auto_min, TRANSITION.auto_max);
    return Math.round(seconds * fps);
  }

  // Feasibility gate for a natural stepping transition. Errors block a future
  // splice submission; they are kinematic plausibility checks only.
  function check(seq) {
    const issues = [];
    for (const c of seq.clips) if (c.out - c.in < MIN_CLIP) issues.push({ level: 'error', text: c.id + ' 至少保留 ' + MIN_CLIP + ' 帧' });
    if (seq.clips.length < 2) return { ok: !issues.length, issues, gap: null };
    const g = gap(seq.clips[0], seq.clips[1]), seconds = seq.transition / seq.fps;
    const need = Math.max(g.distance / TRANSITION.max_m_s, g.turn_deg / TRANSITION.max_turn_deg_s, TRANSITION.min);
    if (seconds < TRANSITION.min) issues.push({ level: 'error', text: `过渡至少 ${TRANSITION.min} 秒` });
    if (seconds > TRANSITION.max) issues.push({ level: 'error', text: `过渡最长 ${TRANSITION.max} 秒` });
    if (g.distance / seconds > TRANSITION.max_m_s) issues.push({ level: 'error', text: `间距 ${g.distance.toFixed(2)} 米需要至少 ${need.toFixed(1)} 秒，否则超过自然步行速度` });
    if (g.turn_deg / seconds > TRANSITION.max_turn_deg_s) issues.push({ level: 'error', text: `转向 ${g.turn_deg.toFixed(0)}° 过急，至少需要 ${need.toFixed(1)} 秒` });
    if (g.distance < 0.05 && g.turn_deg < 5 && seconds > 2) issues.push({ level: 'warn', text: '两段几乎重合，过渡以原地收势、站稳和接入后段为主' });
    return { ok: !issues.some(i => i.level === 'error'), issues, gap: g, need_seconds: need };
  }

  // Layout of the assembled sequence in output frames.
  function spans(seq) {
    const out = []; let t = 0;
    seq.clips.forEach((c, i) => {
      if (i) { out.push({ kind: 'transition', start: t, end: t + seq.transition }); t += seq.transition; }
      out.push({ kind: 'clip', index: i, clip: c, start: t, end: t + c.out - c.in }); t += c.out - c.in;
    });
    return { spans: out, frames: t };
  }

  function locate(seq, frame) {
    const { spans: list } = spans(seq);
    return list.find(s => frame >= s.start && frame < s.end) || list.at(-1) || null;
  }

  // Assemble transformed clips plus a labelled placeholder transition (root
  // eased between the cut frames, pose blended about the turning heading).
  // The placeholder is preview only; The server computes the final Cubic Hermite transition.
  function assemble(seq) {
    const clips = seq.clips; if (!clips.length) return null;
    const count = clips[0].src.count, { spans: list, frames } = spans(seq);
    if (clips.some(c => c.src.count !== count)) throw new Error('两段素材骨架不一致，无法拼接');
    const data = new Float32Array(frames * count * 3);
    const put = (f, j, p) => { const o = (f * count + j) * 3; data[o] = p[0]; data[o + 1] = p[1]; data[o + 2] = p[2]; };
    for (const s of list) {
      if (s.kind === 'clip') { const c = s.clip; for (let f = c.in; f < c.out; f++) for (let j = 0; j < count; j++) put(s.start + f - c.in, j, transform(c, point(c.src, f, j))); continue; }
      const a = clips[0], b = clips[1], fa = a.out - 1, fb = b.in, ra = root(a, fa), rb = root(b, fb), ha = heading(a, fa), dh = wrap(heading(b, fb) - ha);
      const n = s.end - s.start;
      for (let k = 0; k < n; k++) {
        const t = (k + 1) / (n + 1), e = t * t * (3 - 2 * t), r = [ra[0] + (rb[0] - ra[0]) * e, ra[1] + (rb[1] - ra[1]) * e, ra[2] + (rb[2] - ra[2]) * e];
        const turnA = dh * e, turnB = dh * (e - 1);
        for (let j = 0; j < count; j++) {
          const pa = transform(a, point(a.src, fa, j)), pb = transform(b, point(b.src, fb, j));
          const la = spin([pa[0] - ra[0], pa[1] - ra[1], pa[2] - ra[2]], turnA), lb = spin([pb[0] - rb[0], pb[1] - rb[1], pb[2] - rb[2]], turnB);
          put(s.start + k, j, [r[0] + la[0] * (1 - e) + lb[0] * e, r[1] + la[1] * (1 - e) + lb[1] * e, r[2] + la[2] * (1 - e) + lb[2] * e]);
        }
      }
    }
    const meta = { ...clips[0].src.meta, frames, joint_count: count, fps: seq.fps };
    return { meta, data, spans: list, frames };
  }
  // Rotate a ground vector so that heading atan2(x,z) increases by a.
  function spin(v, a) { const c = Math.cos(a), s = Math.sin(a); return [c * v[0] + s * v[2], v[1], -s * v[0] + c * v[2]]; }

  // Body of POST /api/harness/splice (g2h/motion_splice.py uses the same
  // pivot: the root at each clip's start frame; Y up; turn right positive).
  function spec(seq) {
    return {
      clips: seq.clips.map(c => ({ motion_id: c.id, start_frame: c.in, end_frame: c.out,
        transform: { turn_right_deg: +(c.turn || 0).toFixed(3), offset_x_m: +c.dx.toFixed(4), offset_z_m: +c.dz.toFixed(4) } })),
      transition_frames: seq.transition
    };
  }

  return { MAX_CLIPS, MIN_CLIP, TRANSITION, create, trim, transform, root, heading, gap, suggest, check, spans, locate, assemble, spec, wrap, rad, deg };
})();
if (typeof module !== 'undefined') module.exports = Sequence;
