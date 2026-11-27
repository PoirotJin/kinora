const assert = require('node:assert/strict');
const Seq = require('../src/renderer/sequence.js');
const near = (a, b, e = 1e-4) => assert.ok(Math.abs(a - b) < e, `${a} ≈ ${b}`);

// Synthetic walker: pelvis + left/right hips, walking +Z at 1 m/s, 30 fps.
function walker(frames, speed = 1) {
  const data = new Float32Array(frames * 3 * 3);
  for (let f = 0; f < frames; f++) {
    const z = f / 30 * speed;
    data.set([0, 0.9, z, 0.1, 0.85, z, -0.1, 0.85, z], f * 9);
  }
  return { frames, count: 3, fps: 30, data, meta: { frames, joint_count: 3, fps: 30, parents: [-1, 0, 0], names: ['Pelvis', 'L_Hip', 'R_Hip'] } };
}

const a = Seq.create(walker(90), 'A');
near(Seq.heading(a, 0), 0);
// Turning right by 90° makes a +Z walker head toward -X; the start frame is the pivot.
a.turn = 90;
const end = Seq.root(a, 89);
near(end[0], -89 / 30); near(end[2], 0);
near(Seq.heading(a, 89), -Math.PI / 2);
const startBefore = Seq.root(a, 0);
near(startBefore[0], 0); near(startBefore[2], 0);

// Trimming the in-point moves the pivot but must not move the visible path.
a.dx = 0.3; a.dz = -0.2;
const before = Seq.root(a, 60);
Seq.trim(a, 30, 90);
const after = Seq.root(a, 60);
near(before[0], after[0]); near(before[2], after[2]);
assert.equal(a.in, 30);
// Trim keeps the minimum clip length.
Seq.trim(a, 85, 86); assert.equal(a.out - a.in, Seq.MIN_CLIP);
Seq.trim(a, 0, 90);

// Sequence assembly: frame counts, exact clip frames, placeholder in between.
const b = Seq.create(walker(60), 'B');
const seq = { clips: [Seq.create(walker(90), 'A'), b], transition: 30, auto: false, fps: 30 };
b.dz = 4; // B starts 4 m ahead of A's origin; A ends at 2.97 m.
const out = Seq.assemble(seq);
assert.equal(out.frames, 90 + 30 + 60);
assert.deepEqual(out.spans.map(s => s.kind), ['clip', 'transition', 'clip']);
near(out.data[(89 * 3) * 3 + 2], 89 / 30, 1e-5);
near(out.data[(120 * 3) * 3 + 2], 4, 1e-5);
const mid = out.data[(105 * 3) * 3 + 2]; assert.ok(mid > 89 / 30 && mid < 4);
assert.equal(Seq.locate(seq, 100).kind, 'transition');
assert.equal(Seq.locate(seq, 150).index, 1);

// Short transitions pass when distance and turning fit the time budget.
assert.equal(Seq.check(seq).ok, true);
seq.transition = 150; assert.equal(Seq.check(seq).ok, true);
b.dz = 15; assert.equal(Seq.check(seq).ok, false);
assert.ok(Seq.suggest(seq.clips[0], b, 30) > 150);
b.dz = 4; assert.ok(Seq.suggest(seq.clips[0], b, 30) < 30);
b.dz = 3.2; b.turn = 180;
seq.transition = 270; assert.equal(Seq.check(seq).ok, true);
seq.transition = 570; assert.equal(Seq.check(seq).ok, false);
seq.transition = 150;

// Body for POST /api/harness/splice: exactly the fields the server accepts.
const spec = Seq.spec(seq);
assert.deepEqual(Object.keys(spec), ['clips', 'transition_frames']);
assert.deepEqual(Object.keys(spec.clips[0]), ['motion_id', 'start_frame', 'end_frame', 'transform']);
assert.deepEqual(Object.keys(spec.clips[1].transform), ['turn_right_deg', 'offset_x_m', 'offset_z_m']);
assert.equal(spec.clips[1].transform.turn_right_deg, 180);
assert.equal(spec.transition_frames, 150);
console.log('PASS: clip rotation pivot, trim compensation, assembly layout, transition feasibility and splice spec');
