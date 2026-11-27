'use strict';
const ScenePresets = (() => {
  const FPS = 30;
  const MAX_PARTS = 16;
  const standStill = instruction => /^(站立|站住|站定|stand|stand still|standing)$/i.test(instruction.trim());
  const ARMS_DOWN = {16:[0,0,-1.4], 17:[0,0,1.4]};
  const rotations = angles => Array.from({length:21}, (_, i) => angles[i+1] || [0,0,0]);
  const pointActions = new Map([
    ['挥手','waves one hand'], ['举起左手','raises the left hand'], ['举起右手','raises the right hand'],
    ['蹲下','squats down'], ['转身','turns around'], ['跳跃','jumps in place'], ['拍手','claps both hands']
  ]);
  function actionText(item) {
    const action = (item.instruction || '').trim();
    return pointActions.get(action) || action;
  }
  const round6 = v => Math.round(v * 1e6) / 1e6;
  const wrapHeading = deg => {
    let d = (((Number(deg) || 0) + 180) % 360 + 360) % 360 - 180;
    if (d === -180 && Number(deg) > 0) d = 180;
    return round6(d);
  };
  const baseObjectId = partId => String(partId || '').replace(/-(marker|box|seat|back|leg\d+|step\d+)$/, '');
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const num = (v, fallback, min = -Infinity, max = Infinity) => {
    const n = typeof v === 'number' ? v : (v === null || v === undefined || v === '' ? NaN : Number(v));
    return Number.isFinite(n) ? clamp(n, min, max) : fallback;
  };
  const waypoint = (frame, item, z, heading_deg = 0) => ({frame, xz_m:[round6(item.x), round6(item.z + z)], heading_deg});
  const pose = (frame, object_id, pelvis_offset_m, angles, heading_deg = 0) =>
    ({frame, object_id, pelvis_offset_m:pelvis_offset_m.map(round6), heading_deg:round6(heading_deg), body_rotvecs:rotations(angles).map(r => r.map(round6))});
  const contact = (start_frame, end_frame, object_id, joint_index, anchor_m, tolerance_m) =>
    ({start_frame, end_frame, object_id, joint_index, anchor_m:anchor_m.map(round6), tolerance_m:round6(tolerance_m)});
  const assetHand = item => item.asset.hand === 20 ? 'left' : 'right';

  // Human52 rest chain up to wrists (0..21) for adaptive collision-free reach/crouch keyframes.
  const PARENTS = [-1,0,0,0,1,2,3,4,5,6,7,8,9,9,9,12,13,14,16,17,18,19];
  const OFFSETS = [
    [0,0,0],[.095,-.055,0],[-.095,-.055,0],[0,.13,0],[0,-.415,0],[0,-.415,0],
    [0,.13,0],[0,-.415,0],[0,-.415,0],[0,.16,0],[0,-.025,.16],[0,-.025,.16],
    [0,.145,0],[.085,.07,0],[-.085,.07,0],[0,.13,0],[.105,0,0],[-.105,0,0],
    [.285,0,0],[-.285,0,0],[.255,0,0],[-.255,0,0]
  ];
  const vnorm = v => Math.hypot(v[0], v[1], v[2]);
  const vsub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const vadd = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const vscale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const vdot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const vcross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const vunit = v => { const n = vnorm(v); return n > 1e-10 ? vscale(v, 1 / n) : [1, 0, 0]; };
  function rotMat(rv) {
    const th = vnorm(rv);
    if (th < 1e-12) return [[1,0,0],[0,1,0],[0,0,1]];
    const [x, y, z] = vscale(rv, 1 / th), c = Math.cos(th), s = Math.sin(th), t = 1 - c;
    return [
      [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
      [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
      [t * x * z - s * y, t * y * z + s * x, t * z * z + c]
    ];
  }
  function matToRotvec(R) {
    const tr = R[0][0] + R[1][1] + R[2][2];
    const cos = clamp((tr - 1) / 2, -1, 1);
    const th = Math.acos(cos);
    if (th < 1e-9) return [0, 0, 0];
    if (Math.PI - th < 1e-5) {
      let x = Math.sqrt(Math.max(0, (R[0][0] + 1) / 2));
      let y = Math.sqrt(Math.max(0, (R[1][1] + 1) / 2));
      let z = Math.sqrt(Math.max(0, (R[2][2] + 1) / 2));
      if (x >= y && x >= z) { y = (R[0][1] + R[1][0]) / (4 * x); z = (R[0][2] + R[2][0]) / (4 * x); }
      else if (y >= z) { x = (R[0][1] + R[1][0]) / (4 * y); z = (R[1][2] + R[2][1]) / (4 * y); }
      else { x = (R[0][2] + R[2][0]) / (4 * z); y = (R[1][2] + R[2][1]) / (4 * z); }
      return vscale(vunit([x, y, z]), th);
    }
    const s = 2 * Math.sin(th);
    return [(R[2][1] - R[1][2]) * th / s, (R[0][2] - R[2][0]) * th / s, (R[1][0] - R[0][1]) * th / s];
  }
  const mmul = (A, B) => A.map(r => [0,1,2].map(j => r[0] * B[0][j] + r[1] * B[1][j] + r[2] * B[2][j]));
  const mmulT = (A, B) => [0,1,2].map(i => [0,1,2].map(j => A[0][i] * B[0][j] + A[1][i] * B[1][j] + A[2][i] * B[2][j]));
  const mmulV = (A, v) => A.map(r => r[0] * v[0] + r[1] * v[1] + r[2] * v[2]);
  function alignMat(before, after) {
    const a = vunit(before), b = vunit(after), cr = vcross(a, b), s = vnorm(cr), c = vdot(a, b);
    if (s < 1e-8) {
      if (c > 0) return [[1,0,0],[0,1,0],[0,0,1]];
      const ax = vunit(vcross(a, Math.abs(a[0]) < 0.9 ? [1,0,0] : [0,1,0]));
      return rotMat(vscale(ax, Math.PI));
    }
    return rotMat(vscale(cr, Math.atan2(s, c) / s));
  }
  function fkState(angles, pelvis) {
    const pos = Array.from({length:22}, () => [0,0,0]);
    const rot = Array.from({length:22}, () => [[1,0,0],[0,1,0],[0,0,1]]);
    for (let i = 0; i < 22; i++) {
      const p = PARENTS[i], R = rotMat(angles[i] || [0,0,0]);
      if (p < 0) { rot[i] = R; pos[i] = [...pelvis]; }
      else { rot[i] = mmul(rot[p], R); pos[i] = vadd(pos[p], mmulV(rot[p], OFFSETS[i])); }
    }
    return {pos, rot};
  }
  function sampleBoxPenetration(pos, boxCenter, boxSize) {
    const half = boxSize.map(v => v / 2);
    const pts = [...pos];
    for (let i = 1; i < 22; i++) {
      const p = PARENTS[i];
      for (const f of [0.25, 0.5, 0.75]) pts.push(vadd(pos[p], vscale(vsub(pos[i], pos[p]), f)));
    }
    let maxPen = 0;
    for (const pt of pts) {
      const d = [0,1,2].map(k => half[k] - Math.abs(pt[k] - boxCenter[k]));
      const pen = Math.max(0, Math.min(...d));
      if (pen > maxPen) maxPen = pen;
    }
    return maxPen;
  }
  function solveBoxPose(pelvisXZ, targetL, targetR, boxCenter, boxSize) {
    let best = null;
    for (const poleY of [-0.15, 0.30, -0.45]) {
      for (let crouch = 0; crouch <= 1.001; crouch += 0.2) {
        for (let lean = 0; lean <= 1.001; lean += 0.2) {
          const hip = -1.05 * crouch, knee = 2.05 * crouch, ankle = -1.00 * crouch;
          const abd = 0.25 * crouch, sp = 0.55 * lean;
          const a = {
            ...ARMS_DOWN,
            1:[hip,0,abd], 2:[hip,0,-abd],
            4:[knee,0,0], 5:[knee,0,0],
            7:[ankle,0,-abd], 8:[ankle,0,abd],
            3:[sp,0,0], 6:[sp,0,0], 9:[sp,0,0]
          };
          let st = fkState(a, [pelvisXZ[0], 0, pelvisXZ[1]]);
          const minFootY = Math.min(st.pos[10][1], st.pos[11][1]);
          const pelvis = [pelvisXZ[0], 0.005 - minFootY, pelvisXZ[1]];
          for (const [wrist, elbow, shoulder, target, side] of [[20,18,16,targetL,1], [21,19,17,targetR,-1]]) {
            if (!target) continue;
            st = fkState(a, pelvis);
            const sPos = st.pos[shoulder], upper = 0.285, lower = 0.255;
            const diff = vsub(target, sPos);
            const reach = clamp(vnorm(diff), Math.abs(upper - lower) + 1e-4, upper + lower - 1e-4);
            const dir = vunit(diff);
            const along = (upper * upper - lower * lower + reach * reach) / (2 * reach);
            let pole = [side, poleY, -0.35];
            pole = vunit(vsub(pole, vscale(dir, vdot(dir, pole))));
            const newElbow = vadd(sPos, vadd(vscale(dir, along), vscale(pole, Math.sqrt(Math.max(0, upper * upper - along * along)))));
            const newWrist = vadd(sPos, vscale(dir, reach));
            const sRot = mmul(alignMat(vsub(st.pos[elbow], sPos), vsub(newElbow, sPos)), st.rot[shoulder]);
            const eRot = mmul(alignMat(vsub(st.pos[wrist], st.pos[elbow]), vsub(newWrist, newElbow)), st.rot[elbow]);
            a[shoulder] = matToRotvec(mmulT(st.rot[PARENTS[shoulder]], sRot));
            a[elbow] = matToRotvec(mmulT(sRot, eRot));
          }
          st = fkState(a, pelvis);
          const err = Math.max(targetL ? vnorm(vsub(targetL, st.pos[20])) : 0, targetR ? vnorm(vsub(targetR, st.pos[21])) : 0);
          const maxPen = sampleBoxPenetration(st.pos, boxCenter, boxSize);
          const cost = err * 10 + Math.max(0, maxPen - 0.015) * 30 + 0.02 * crouch + 0.01 * lean;
          if (!best || cost < best.cost) best = {cost, pelvisY: pelvis[1], angles: a, err, maxPen};
        }
      }
    }
    return best;
  }

  const DEFAULTS = {
    point: null,
    box: { x: 0.65, y: 0.55, z: 0.50, baseY: 0, mode: 'touch', liftM: 0.32, toX: null, toZ: null },
    stairs: { width: 1.0, stepHeight: 0.16, stepDepth: 0.42, steps: 3 },
    chair: { width: 0.54, seatHeight: 0.42, depth: 0.48 }
  };

  function defaultDims(type) {
    const def = DEFAULTS[type];
    return def ? { ...def } : null;
  }

  function boxConfig(item) {
    const d = item?.dims;
    const base = d ? DEFAULTS.box : { x: 0.65, y: 1.05, z: 0.50, baseY: 0, mode: 'touch', liftM: 0.32, toX: null, toZ: null };
    const x = num(d?.x, base.x, 0.05, 4);
    const y = num(d?.y, base.y, 0.05, 3);
    const z = num(d?.z, base.z, 0.05, 4);
    const baseY = num(d?.baseY, base.baseY, 0, 2);
    const rawMode = d?.mode || base.mode;
    const mode = ['touch', 'lift', 'carry', 'co_carry'].includes(rawMode) ? rawMode : 'touch';
    const liftM = num(d?.liftM, base.liftM, 0.05, 1.2);
    const parseOpt = v => (v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v))) ? Number(v) : null;
    const toX = parseOpt(d?.toX);
    const toZ = parseOpt(d?.toZ);
    return { x, y, z, baseY, mode, liftM, toX, toZ };
  }

  function stairsConfig(item) {
    const d = item?.dims;
    return {
      width: num(d?.width, DEFAULTS.stairs.width, 0.3, 5),
      stepHeight: num(d?.stepHeight, DEFAULTS.stairs.stepHeight, 0.05, 0.45),
      stepDepth: num(d?.stepDepth, DEFAULTS.stairs.stepDepth, 0.18, 1.5),
      steps: Math.round(num(d?.steps, DEFAULTS.stairs.steps, 2, 5))
    };
  }

  function chairConfig(item) {
    const d = item?.dims;
    return {
      width: num(d?.width, DEFAULTS.chair.width, 0.3, 2),
      seatHeight: num(d?.seatHeight, DEFAULTS.chair.seatHeight, 0.25, 0.8),
      depth: num(d?.depth, DEFAULTS.chair.depth, 0.28, 1.5)
    };
  }

  function carryDest(item, cfg) {
    let destX = cfg.toX ?? (item.x + 1.2);
    let destZ = cfg.toZ ?? (item.z + 1.2);
    if (Math.hypot(destX - item.x, destZ - item.z) < 0.05) {
      destX = item.x + 1.2;
      destZ = item.z + 1.2;
    }
    const dx = destX - item.x, dz = destZ - item.z;
    const heading = round6(Math.atan2(dx, dz) * 180 / Math.PI);
    return { destX: round6(destX), destZ: round6(destZ), heading };
  }

  function boxFrames(frames, mode) {
    const last = Math.max(29, frames - 1);
    if (mode === 'carry') {
      const approachFrame = Math.round(last * 0.18);
      const graspFrame = Math.max(approachFrame + 1, Math.round(last * 0.28));
      const liftFrame = Math.max(graspFrame + 1, Math.round(last * 0.44));
      const carryFrame = Math.max(liftFrame + 1, Math.round(last * 0.80));
      return { approachFrame, graspFrame, liftFrame, carryFrame, last };
    }
    const approachFrame = Math.round(last * 0.28);
    const graspFrame = Math.max(approachFrame + 1, Math.round(last * 0.48));
    const liftFrame = Math.max(graspFrame + 1, Math.round(last * 0.76));
    return { approachFrame, graspFrame, liftFrame, last };
  }

  function boxTrajectory(item, frames) {
    const cfg = boxConfig(item);
    if (cfg.mode !== 'lift' && cfg.mode !== 'carry') return null;
    const cy0 = round6(cfg.baseY + cfg.y / 2);
    const cy1 = round6(cy0 + cfg.liftM);
    const f = boxFrames(frames, cfg.mode);
    if (cfg.mode === 'lift') {
      return [
        { frame: 0, center_m: [round6(item.x), cy0, round6(item.z)] },
        { frame: f.graspFrame, center_m: [round6(item.x), cy0, round6(item.z)] },
        { frame: f.liftFrame, center_m: [round6(item.x), cy1, round6(item.z)] },
        { frame: f.last, center_m: [round6(item.x), cy1, round6(item.z)] }
      ];
    }
    const { destX, destZ } = carryDest(item, cfg);
    return [
      { frame: 0, center_m: [round6(item.x), cy0, round6(item.z)] },
      { frame: f.graspFrame, center_m: [round6(item.x), cy0, round6(item.z)] },
      { frame: f.liftFrame, center_m: [round6(item.x), cy1, round6(item.z)] },
      { frame: f.carryFrame, center_m: [destX, cy1, destZ] },
      { frame: f.last, center_m: [destX, cy1, destZ] }
    ];
  }

  function partsFor(item) {
    if (item?.type === 'asset') {
      const size = item.asset.size_m;
      return [['asset', [0, size[1] / 2, 0], size]];
    }
    if (!item || item.type === 'point') return [['marker', [0, 0.005, 0], [0.32, 0.01, 0.32]]];
    if (item.type === 'box') {
      const cfg = boxConfig(item);
      return [['box', [0, round6(cfg.baseY + cfg.y / 2), 0], [round6(cfg.x), round6(cfg.y), round6(cfg.z)]]];
    }
    if (item.type === 'stairs') {
      const cfg = stairsConfig(item);
      return Array.from({length: cfg.steps}, (_, idx) => {
        const step = idx + 1;
        return ['step' + step, [0, round6(0.5 * cfg.stepHeight * step), round6(cfg.stepDepth * idx)], [round6(cfg.width), round6(cfg.stepHeight * step), round6(cfg.stepDepth)]];
      });
    }
    if (item.type === 'chair') {
      const cfg = chairConfig(item);
      const seatThick = round6(Math.min(0.06, Math.max(0.02, cfg.seatHeight * (0.06 / 0.42))));
      const legH = round6(Math.max(0.02, cfg.seatHeight - seatThick / 2));
      const backThick = round6(Math.min(0.05, cfg.depth * (0.05 / 0.48)));
      const backH = round6(Math.max(0.15, cfg.seatHeight * (0.52 / 0.42)));
      const legThick = round6(Math.min(0.05, cfg.width * (0.05 / 0.54), cfg.depth * (0.05 / 0.48)));
      const legX = round6(cfg.width * (0.215 / 0.54));
      const legZ = round6(cfg.depth * (0.18 / 0.48));
      return [
        ['seat', [0, round6(cfg.seatHeight), 0], [round6(cfg.width), seatThick, round6(cfg.depth)]],
        ['back', [0, round6(cfg.seatHeight + seatThick / 2 + backH / 2), round6(cfg.depth / 2 - backThick / 2)], [round6(cfg.width), backH, backThick]],
        ...[-1, 1].flatMap(x => [-1, 1].map(z =>
          ['leg' + (x + 1) + (z + 1), [round6(x * legX), round6(legH / 2), round6(z * legZ)], [legThick, legH, legThick]]))
      ];
    }
    return types[item.type]?.parts || [];
  }

  // Part centers are relative to the placement on the floor. Poses use the
  // canonical Human52 T-pose, with +Z forward and joint positions in metres.
  const types = {
    point: {
      label:'目标点', action:'经过目标点或到点执行动作', duration:3,
      text:'A person moves through the marked position and continues forward with a natural stride.',
      parts:[['marker',[0,.005,0],[.32,.01,.32]]],
      plan(item, frames, first=true, last=true) {
        const action = (item.instruction || '').trim();
        const arrival = Math.round((frames-1)*.62);
        const waypoints = first ? [waypoint(0,item,-1)] : [];
        waypoints.push({...waypoint(arrival,item,0), tolerance_m:.12});
        const standing = standStill(action);
        const keyframes = first ? [pose(0, item.id+'-marker', [0, .915, -1], ARMS_DOWN)] : [];
        if (action) {
          waypoints.push({...waypoint(frames-1,item,0), tolerance_m:.12});
          waypoints.find(point => point.frame === arrival).through_frame = frames-1;
          if (standing) {
            for (const frame of [arrival,frames-1]) keyframes.push(pose(frame,item.id+'-marker',[0,.915,0],ARMS_DOWN));
          }
        } else if (last) {
          waypoints.push(waypoint(frames-1,item,.65));
        }
        return {waypoints,keyframes,contacts:[]};
      }
    },
    box: {
      label:'箱子', action:'走近并交互箱子', duration:4,
      text:'A person walks up to a waist-high box, stops with both feet apart, and reaches out with their left hand to touch the front.',
      parts:[['box',[0,.525,0],[.65,1.05,.5]]],
      plan(item, frames, first=true) {
        const id = item.id + '-box';
        const cfg = boxConfig(item);
        const cy0 = round6(cfg.baseY + cfg.y / 2);
        const mode = cfg.mode === 'co_carry' ? 'lift' : cfg.mode;
        if (mode === 'touch') {
          const approachZ = round6(-(cfg.z / 2 + 0.39));
          const startZ = round6(-(cfg.z / 2 + 1.30));
          const approachFrame = Math.round((frames - 1) * 0.45);
          const touch = Math.max(approachFrame + 1, Math.min(frames - 2, frames - 18));
          const waypoints = [waypoint(0, item, startZ), waypoint(approachFrame, item, approachZ)];
          const keyframes = first ? [pose(0, id, [0, round6(0.915 - cy0), startZ], ARMS_DOWN)] : [];
          if (!item.dims) {
            const reach = {...ARMS_DOWN, 16:[0,-1,-1.05], 20:[0,1,1.05]};
            const offY = round6(0.915 - cy0);
            keyframes.push(pose(touch, id, [0, offY, approachZ], reach), pose(frames - 1, id, [0, offY, approachZ], reach));
            return { waypoints, keyframes, contacts:[contact(touch, frames, id, 20, [.255, .478, -.25], .06)] };
          }
          const anchorX = cfg.x >= 0.08 ? round6(Math.min(cfg.x / 2 - 0.02, 0.255)) : 0;
          const targetWorldY = clamp(1.00, cfg.baseY + 0.02, cfg.baseY + cfg.y);
          const anchorY = round6(targetWorldY - cy0);
          const anchorZ = round6(-cfg.z / 2);
          const solved = solveBoxPose([0, approachZ], [anchorX, targetWorldY, anchorZ], null, [0, cy0, 0], [cfg.x, cfg.y, cfg.z]);
          const offY = round6(solved.pelvisY - cy0);
          keyframes.push(pose(touch, id, [0, offY, approachZ], solved.angles), pose(frames - 1, id, [0, offY, approachZ], solved.angles));
          return { waypoints, keyframes, contacts:[contact(touch, frames, id, 20, [anchorX, anchorY, anchorZ], .06)] };
        }
        const az = round6(Math.max(-(cfg.z / 2 - 0.01), -0.05));
        const ax = round6(cfg.x / 2);
        const cy1 = round6(cy0 + cfg.liftM);
        const approachZ = round6(-(cfg.z / 2 + 0.34));
        const startZ = round6(-(cfg.z / 2 + 1.30));
        const f = boxFrames(frames, mode);
        const graspSolved = solveBoxPose([0, approachZ], [ax, cy0, az], [-ax, cy0, az], [0, cy0, 0], [cfg.x, cfg.y, cfg.z]);
        const liftSolved = solveBoxPose([0, approachZ], [ax, cy1, az], [-ax, cy1, az], [0, cy1, 0], [cfg.x, cfg.y, cfg.z]);
        const anchors = [[ax, 0, az], [-ax, 0, az]];
        const contacts = [20, 21].map((joint, idx) => contact(f.graspFrame, frames, id, joint, anchors[idx], 0.08));
        const attachments = [{ object_id: id, start_frame: f.graspFrame, end_frame: frames, mode, joints: [20, 21], anchors_m: anchors }];
        if (mode === 'lift') {
          const waypoints = [waypoint(0, item, startZ), waypoint(f.approachFrame, item, approachZ)];
          const keyframes = first ? [pose(0, id, [0, round6(0.915 - cy0), startZ], ARMS_DOWN)] : [];
          keyframes.push(
            pose(f.graspFrame, id, [0, round6(graspSolved.pelvisY - cy0), approachZ], graspSolved.angles),
            pose(f.liftFrame, id, [0, round6(liftSolved.pelvisY - cy1), approachZ], liftSolved.angles),
            pose(frames - 1, id, [0, round6(liftSolved.pelvisY - cy1), approachZ], liftSolved.angles)
          );
          return { waypoints, keyframes, contacts, attachments };
        }
        const { destX, destZ, heading } = carryDest(item, cfg);
        const rad = heading * Math.PI / 180, sin = Math.sin(rad), cos = Math.cos(rad);
        const worldXZ = (cx, cz, localZ) => ({ xz_m: [round6(cx + sin * localZ), round6(cz + cos * localZ)], heading_deg: heading });
        const waypoints = [
          { frame: 0, ...worldXZ(item.x, item.z, startZ) },
          { frame: f.approachFrame, ...worldXZ(item.x, item.z, approachZ) },
          { frame: f.liftFrame, ...worldXZ(item.x, item.z, approachZ) },
          { frame: f.carryFrame, ...worldXZ(destX, destZ, approachZ) }
        ];
        const keyframes = first ? [pose(0, id, [0, round6(0.915 - cy0), startZ], ARMS_DOWN, 0)] : [];
        keyframes.push(
          pose(f.graspFrame, id, [0, round6(graspSolved.pelvisY - cy0), approachZ], graspSolved.angles, 0),
          pose(f.liftFrame, id, [0, round6(liftSolved.pelvisY - cy1), approachZ], liftSolved.angles, 0),
          pose(f.carryFrame, id, [0, round6(liftSolved.pelvisY - cy1), approachZ], liftSolved.angles, 0),
          pose(frames - 1, id, [0, round6(liftSolved.pelvisY - cy1), approachZ], liftSolved.angles, 0)
        );
        return { waypoints, keyframes, contacts, attachments };
      }
    },
    asset: {
      label:'个人物体', action:'走近物体并按所选接触点交互', duration:4,
      text:'A person walks toward the selected object, slows down, and stops in front of it with both feet stable.',
      plan(item, frames, first = true) {
        const {size_m: [width, height, depth], heading_deg: objectHeading, anchor} = item.asset;
        const [x, , z] = anchor?.position_m || [0, 0, -depth / 2];
        const faces = [
          {distance:depth / 2 + z, heading:0, center:[clamp(x, -width / 2, width / 2), -depth / 2], out:[0, -1]},
          {distance:depth / 2 - z, heading:180, center:[clamp(x, -width / 2, width / 2), depth / 2], out:[0, 1]},
          {distance:width / 2 - x, heading:-90, center:[width / 2, clamp(z, -depth / 2, depth / 2)], out:[1, 0]},
          {distance:width / 2 + x, heading:90, center:[-width / 2, clamp(z, -depth / 2, depth / 2)], out:[-1, 0]}
        ];
        const face = faces.reduce((nearest, face) => face.distance < nearest.distance ? face : nearest);
        const heading = wrapHeading(objectHeading + face.heading);
        const rad = objectHeading * Math.PI / 180;
        const cos = Math.cos(rad), sin = Math.sin(rad);
        const local = clearance => face.center.map((v, axis) => v + clearance * face.out[axis]);
        const point = (frame, clearance) => {
          const [dx, dz] = local(clearance);
          return {frame, xz_m:[round6(item.x + cos * dx + sin * dz), round6(item.z - sin * dx + cos * dz)], heading_deg:heading};
        };
        const stop = .52, startDistance = stop + .95;
        const approach = Math.round((frames-1)*.6);
        const end = frames-1;
        const waypoints = [point(0,startDistance), {...point(approach,stop),through_frame:end,tolerance_m:.13}, point(end,stop)];
        const [startX, startZ] = local(startDistance);
        const keyframes = first ? [
          pose(0,item.id+'-asset',[startX,.915-height/2,startZ],ARMS_DOWN,face.heading)
        ] : [];
        return {waypoints,keyframes,contacts:anchor ? [
          {...contact(approach,end+1,item.id+'-asset',item.asset.hand,anchor.position_m,.08),
            anchor_m: [...anchor.position_m]}
        ] : []};
      }
    },
    stairs: {
      label:'楼梯', action:'走上台阶', duration:6,
      text:'A person walks to a short staircase, climbs three steps in order, and comes to a balanced stop on the top step.',
      parts:[1,2,3].map(step => ['step'+step,[0,.08*step,.42*(step-1)],[1,.16*step,.42]]),
      plan(item, frames, first=true) {
        const cfg = stairsConfig(item);
        const last = Math.max(29, frames - 1);
        const startZ = round6(-0.5 * cfg.stepDepth - 1.29);
        const approachZ = round6(-0.5 * cfg.stepDepth - 0.28);
        const baseFrame = Math.round(last * 0.24);
        const step1Id = item.id + '-step1';
        const floorPelvisY = round6(0.915 - 0.5 * cfg.stepHeight);
        const keyframes = first ? [pose(0, step1Id, [0, floorPelvisY, startZ], ARMS_DOWN)] : [];
        keyframes.push(pose(baseFrame, step1Id, [0, floorPelvisY, approachZ], ARMS_DOWN));
        const steps = Array.from({length: cfg.steps}, (_, i) => {
          const step = i + 1;
          const frac = 0.42 + (0.84 - 0.42) * i / Math.max(1, cfg.steps - 1);
          return {
            frame: Math.round(last * frac),
            id: item.id + '-step' + step,
            halfHeight: round6(0.5 * cfg.stepHeight * step)
          };
        });
        for (const s of steps) {
          keyframes.push(pose(s.frame, s.id, [0, round6(0.915 + s.halfHeight), -0.12], ARMS_DOWN));
        }
        keyframes.push({ ...keyframes.at(-1), frame: frames - 1 });
        const footX = Math.min(0.095, Math.max(0.02, cfg.width / 2 - 0.02));
        const footZ = clamp(0.04, -cfg.stepDepth / 2 + 0.02, cfg.stepDepth / 2 - 0.02);
        const contacts = steps.flatMap(({frame, id, halfHeight}, i) =>
          [[10, footX], [11, -footX]].map(([joint, x]) =>
            contact(frame, i === steps.length - 1 ? frames : frame + 1, id, joint, [x, halfHeight, footZ], 0.055)));
        return {
          waypoints:[waypoint(0, item, startZ), waypoint(baseFrame, item, approachZ)],
          keyframes, contacts
        };
      }
    },
    chair: {
      label:'椅子', action:'走近、转身坐下', duration:6,
      text:'A person walks to a chair, turns around in front of it, and sits down slowly with both feet resting on the ground.',
      parts:[
        ['seat',[0,.42,0],[.54,.06,.48]],
        ['back',[0,.71,.215],[.54,.52,.05]],
        ...[-1,1].flatMap(x => [-1,1].map(z =>
          ['leg'+(x+1)+(z+1),[x*.215,.195,z*.18],[.05,.39,.05]]))
      ],
      plan(item, frames, first=true) {
        const cfg = chairConfig(item);
        const id = item.id + '-seat', seated = Math.round((frames - 1) * 0.82);
        const seatThick = Math.min(0.06, Math.max(0.02, cfg.seatHeight * (0.06 / 0.42)));
        const backThick = Math.min(0.05, cfg.depth * (0.05 / 0.48));
        const startZ = round6(-(cfg.depth / 2 + 1.31));
        const approachZ = round6(-(cfg.depth / 2 + 0.20));
        const seatPelvisY = round6(Math.max(seatThick / 2 + 0.065, 0.495 - cfg.seatHeight));
        const seatZ = round6(Math.min(0.025, cfg.depth / 2 - backThick - 0.08));
        const contactTol = round6(Math.min(0.15, Math.max(0.095, seatPelvisY - seatThick / 2 + 0.03)));
        const sit = {...ARMS_DOWN, 1:[-Math.PI/2,0,0], 2:[-Math.PI/2,0,0], 4:[Math.PI/2,0,0], 5:[Math.PI/2,0,0]};
        const keyframes = first ? [pose(0, id, [0, round6(0.915 - cfg.seatHeight), startZ], ARMS_DOWN)] : [];
        keyframes.push(pose(seated, id, [0, seatPelvisY, seatZ], sit, 180), pose(frames - 1, id, [0, seatPelvisY, seatZ], sit, 180));
        return {
          waypoints:[waypoint(0, item, startZ), waypoint(Math.round((frames - 1) * 0.4), item, approachZ),
            waypoint(Math.round((frames - 1) * 0.58), item, approachZ, 180)],
          keyframes,
          contacts:[contact(seated, frames, id, 0, [0, round6(seatThick / 2), seatZ], contactTol)]
        };
      }
    }
  };

  function geometry(items, frames = 120) {
    return (items || []).flatMap(item => {
      const boxCfg = item.type === 'box' ? boxConfig(item) : null;
      const heading = boxCfg && boxCfg.mode === 'carry' ? carryDest(item, boxCfg).heading : 0;
      const traj = item.type === 'box' ? boxTrajectory(item, frames) : null;
      return partsFor(item).map(([name, center, size]) => ({
        id: item.id + '-' + name,
        center_m: [round6(item.x + center[0]), item.type === 'asset' ? center[1] : round6(center[1]),
          round6(item.z + center[2])],
        size_m: size,
        heading_deg: item.type === 'asset' ? item.asset.heading_deg : heading,
        ...(item.type === 'asset' ? {asset_version:item.asset.id} : {}),
        ...(traj ? { trajectory: traj } : {})
      }));
    });
  }

  function description(item) {
    if (item.type === 'asset' && item.asset.anchor) {
      return 'A person walks to the marked side of the selected object, stops, and touches the marked spot with the ' + assetHand(item) + ' hand.';
    }
    if (item.type === 'point' && item.instruction?.trim()) {
      if (standStill(item.instruction)) return 'A person walks to the marked position and stands upright with both feet apart.';
      return 'A person reaches the marked position and performs the following action with controlled movement: ' + actionText(item);
    }
    if (item.type === 'box') {
      const mode = boxConfig(item).mode;
      if (mode === 'lift') return 'A person walks up to a box, crouches down to grasp both sides, and lifts it steadily with both hands.';
      if (mode === 'carry' || mode === 'co_carry') return 'A person walks up to a box, lifts it with both hands, carries it to the target position, and holds it steadily.';
    }
    if (item.type === 'stairs') {
      const steps = stairsConfig(item).steps;
      if (steps !== 3) return 'A person walks to a staircase, climbs ' + steps + ' steps in order, and comes to a balanced stop on the top step.';
    }
    return types[item.type].text;
  }

  function request(items, target, duration) {
    const frames = Math.round(duration * FPS);
    const ordered = items.filter(item => item.order > 0).sort((a,b) => a.order - b.order);
    if (ordered.length > 1) {
      const objects = geometry(items, frames);
      const orderedIds = new Set(ordered.map(o => o.id));
      for (const obj of objects) {
        if (!orderedIds.has(baseObjectId(obj.id))) delete obj.trajectory;
      }
      const scene = {kind:'human_interaction_v1', objects, waypoints:[], keyframes:[], contacts:[]};
      const attachments = [];
      const weight = ordered.reduce((total,item) => total + types[item.type].duration, 0);
      let elapsed = 0;
      const boxActions = { touch: 'touches the box', lift: 'lifts the box', carry: 'carries the box', co_carry: 'carries the box' };
      const actions = {chair:'sits down', stairs:'climbs the stairs'};
      const prompts = [];
      for (const item of ordered) {
        const preset = types[item.type];
        const start = Math.round(frames * elapsed / weight);
        elapsed += preset.duration;
        const length = Math.round(frames * elapsed / weight) - start;
        if (length < 30) throw new Error('请延长场景时长，每段交互至少需要 1 秒。');
        const segment = preset.plan(item, length, item === ordered[0], item === ordered.at(-1));
        const approach = start ? Math.round(length * .32) : 0;
        const stamp = frame => start + approach + Math.round(frame * (length - 1 - approach) / (length - 1));
        for (const field of ['waypoints','keyframes']) {
          scene[field].push(...segment[field].map(point => ({...point, frame:stamp(point.frame),
            ...(point.through_frame !== undefined ? {through_frame:stamp(point.through_frame)} : {})})));
        }
        scene.contacts.push(...segment.contacts.map(point => ({...point,
          start_frame:stamp(point.start_frame), end_frame:stamp(point.end_frame - 1) + 1})));
        if (segment.attachments?.length) {
          attachments.push(...segment.attachments.map(att => ({...att,
            start_frame:stamp(att.start_frame), end_frame:stamp(att.end_frame - 1) + 1})));
        }
        if (item.type === 'box') {
          const rawTrack = boxTrajectory(item, length);
          const boxObj = objects.find(o => o.id === item.id + '-box');
          if (boxObj && rawTrack) {
            boxObj.trajectory = start > 0
              ? [{ frame: 0, center_m: boxObj.center_m }, ...rawTrack.map(pt => ({ frame: stamp(pt.frame), center_m: pt.center_m }))]
              : rawTrack.map(pt => ({ frame: stamp(pt.frame), center_m: pt.center_m }));
          }
        }
        const instruction = actionText(item);
        const act = item.type === 'asset' ?
          (item.asset.anchor ? 'touches the marked spot with the ' + assetHand(item) + ' hand' : 'stops in front of the object') :
          item.type === 'box' ? boxActions[boxConfig(item).mode] : actions[item.type];
        prompts.push(act || (standStill(instruction) ? 'stands still' :
          instruction ? 'performs this action: ' + instruction : 'passes through'));
      }
      if (attachments.length) scene.attachments = attachments;
      return {text:'A person follows the marked route in order and ' + prompts.join(', then ') + '.', duration, scene};
    }
    target = ordered[0] || target;
    const preset = types[target.type];
    const objects = geometry(items, frames);
    for (const obj of objects) {
      if (baseObjectId(obj.id) !== target.id) delete obj.trajectory;
    }
    return {text:description(target), duration, scene:{
      kind:'human_interaction_v1',
      objects,
      ...preset.plan(target, frames, true, true)
    }};
  }

  function rebaseActorFrame0(scene, startXZ, headingDeg) {
    if (!scene?.waypoints?.length) return scene;
    const out = JSON.parse(JSON.stringify(scene));
    const [sx, sz] = startXZ;
    const wrapped = wrapHeading(headingDeg);
    out.waypoints[0] = { frame: 0, xz_m: [round6(sx), round6(sz)], heading_deg: wrapped };
    if (out.keyframes?.length && out.keyframes[0].frame === 0) {
      const kf = out.keyframes[0];
      const obj = out.objects?.find(o => o.id === kf.object_id);
      if (obj) {
        const rad = (obj.heading_deg || 0) * Math.PI / 180;
        const c = Math.cos(rad), s = Math.sin(rad);
        const worldY = obj.center_m[1] + kf.pelvis_offset_m[1];
        const dx = sx - obj.center_m[0], dy = worldY - obj.center_m[1], dz = sz - obj.center_m[2];
        kf.pelvis_offset_m = [round6(c * dx - s * dz), round6(dy), round6(s * dx + c * dz)];
        kf.heading_deg = wrapHeading(wrapped - (obj.heading_deg || 0));
      }
    }
    return out;
  }

  function multiRequest(actors = [], items = [], duration = 4, peerInteractions = []) {
    const dur = Math.max(1, Math.min(120, Math.round(Number(duration) || 4)));
    const frames = Math.round(dur * FPS);
    const objects = geometry(items || [], frames);
    const actorIds = new Map();
    const actorList = (actors || []).map((a, idx) => {
      const id = a.id || ('R' + (idx + 1));
      actorIds.set(id, idx);
      const start_xz_m = Array.isArray(a.start_xz_m) && a.start_xz_m.length === 2
        ? [round6(Number(a.start_xz_m[0]) || 0), round6(Number(a.start_xz_m[1]) || 0)]
        : [round6(Number(a.x) || 0), round6(Number(a.z) || 0)];
      const heading_deg = wrapHeading(Number.isFinite(Number(a.heading_deg)) ? Number(a.heading_deg) : 0);
      const entry = {
        id,
        label: a.label || ('机器人 ' + (idx + 1)),
        robot: a.robot || null,
        start_xz_m,
        heading_deg
      };
      if (a.scene && typeof a.scene === 'object') {
        entry.scene = a.scene;
        if (a.text) entry.text = a.text;
      } else if (Array.isArray(a.steps) && a.steps.length && items?.length) {
        const actorItems = items.map(it => ({ ...it, order: 0 }));
        let order = 1;
        for (const step of a.steps) {
          const sid = typeof step === 'string' ? step : step?.id;
          const match = actorItems.find(it => it.id === sid);
          if (match) match.order = order++;
        }
        const firstOrdered = actorItems.find(it => it.order === 1);
        if (firstOrdered) {
          const built = request(actorItems, firstOrdered, dur);
          entry.text = (a.text && a.text.trim()) || built.text;
          entry.scene = a.start_xz_m || a.x !== undefined || a.z !== undefined || a.heading_deg !== undefined
            ? rebaseActorFrame0(built.scene, start_xz_m, heading_deg)
            : built.scene;
        }
      } else if (a.targetId && items?.length) {
        const targetItem = items.find(it => it.id === a.targetId);
        if (targetItem) {
          const built = request(items.map(it => ({ ...it, order: 0 })), targetItem, dur);
          entry.text = (a.text && a.text.trim()) || built.text;
          entry.scene = a.start_xz_m || a.x !== undefined || a.z !== undefined || a.heading_deg !== undefined
            ? rebaseActorFrame0(built.scene, start_xz_m, heading_deg)
            : built.scene;
        }
      } else if (a.text && a.text.trim()) {
        entry.text = a.text.trim();
      }
      return entry;
    });

    const resolveActorIdx = ref => (typeof ref === 'number' ? ref : (actorIds.has(ref) ? actorIds.get(ref) : 0));
    const interactions = (peerInteractions || []).map(inter => {
      const kind = inter.kind || inter.type || 'handshake';
      const pair = Array.isArray(inter.actors) ? inter.actors.map(resolveActorIdx) : [0, 1];
      if (kind === 'co_carry') {
        const boxItem = (items || []).find(it => it.type === 'box' && (inter.object_id === it.id + '-box' || inter.object_id === it.id))
          || (items || []).find(it => it.type === 'box');
        const cfg = boxItem ? boxConfig(boxItem) : null;
        const object_id = inter.object_id ? (inter.object_id.endsWith('-box') ? inter.object_id : inter.object_id + '-box') : (boxItem ? boxItem.id + '-box' : 'shared-box');
        const size_m = inter.size_m || (cfg ? [round6(cfg.x), round6(cfg.y), round6(cfg.z)] : [0.58, 0.38, 0.42]);
        const to_xz_m = inter.to_xz_m || (boxItem && cfg ? [carryDest(boxItem, cfg).destX, carryDest(boxItem, cfg).destZ] : [1.1, 0]);
        const lift_m = inter.lift_m ?? cfg?.liftM ?? 0.32;
        return {
          kind: 'co_carry',
          actors: pair,
          object_id,
          size_m,
          to_xz_m,
          lift_m,
          ...(inter.start_frame !== undefined ? { start_frame: inter.start_frame } : {}),
          ...(inter.end_frame !== undefined ? { end_frame: inter.end_frame } : {})
        };
      }
      const xz_m = inter.xz_m || [Number(inter.x) || 0, Number(inter.z) || 0];
      return {
        kind: 'handshake',
        actors: pair,
        xz_m,
        ...(inter.start_frame !== undefined ? { start_frame: inter.start_frame } : {}),
        ...(inter.end_frame !== undefined ? { end_frame: inter.end_frame } : {})
      };
    });

    if (actorList.length >= 2) {
      for (const item of items || []) {
        if (item.type !== 'box') continue;
        const cfg = boxConfig(item);
        if (cfg.mode !== 'co_carry') continue;
        const oid = item.id + '-box';
        if (interactions.some(inter => inter.kind === 'co_carry' && inter.object_id === oid)) continue;
        const dest = carryDest(item, cfg);
        interactions.push({
          kind: 'co_carry',
          actors: Array.isArray(item.coActors) ? item.coActors.map(resolveActorIdx) : [0, 1],
          object_id: oid,
          size_m: [round6(cfg.x), round6(cfg.y), round6(cfg.z)],
          to_xz_m: [dest.destX, dest.destZ],
          lift_m: cfg.liftM
        });
      }
    }

    return { duration: dur, objects, actors: actorList, interactions };
  }

  return {types, geometry, description, request, MAX_PARTS, defaultDims, partsFor, multiRequest};
})();
