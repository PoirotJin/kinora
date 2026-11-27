'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const rendererDir = path.join(__dirname, '..', 'src', 'renderer');
const readRenderer = name => fs.readFileSync(path.join(rendererDir, name), 'utf8');

// 1. HTML & CSS Structural / Visual Policy Audit
function verifyMarkupAndStyles() {
  const html = readRenderer('index.html');
  const css = readRenderer('app.css');

  // Every id attribute in index.html must be globally unique.
  const ids = [];
  const idRegex = /\bid="([^"]+)"/g;
  let m;
  while ((m = idRegex.exec(html)) !== null) ids.push(m[1]);
  const seen = new Set(), dupes = [];
  for (const id of ids) {
    if (seen.has(id)) dupes.push(id);
    seen.add(id);
  }
  assert.deepEqual(dupes, [], 'Duplicate DOM IDs found in index.html: ' + dupes.join(', '));

  for (const required of [
    'ws-left', 'ws-center', 'ws-right', 'ws-taskbar', 'ws-tabs', 'tab-preview',
    'ws-source-bar', 'stage-preview', 'stage-generate', 'stage-editor', 'stage-retarget',
    'rt-human-canvas', 'robot-canvas', 'rt-mode-compare', 'gadget-dialog',
    'gadget-tab-config', 'gadget-tab-keys', 'gadget-close-guard', 'draft-guard-dialog',
    'actor-tracks-bar', 'actor-track-add', 'actor-tracks-list',
    'gen-modality-text', 'gen-modality-video', 'gen-text-pane', 'gen-video-pane',
    'gen-attach-object', 'gen-attach-mode', 'gen-attach-start', 'gen-attach-end',
    'video-motion-file', 'video-motion-status', 'video-motion-jobs', 'scene-add-handshake'
  ]) {
    assert.ok(seen.has(required), 'Missing required unified workspace element #' + required);
  }

  // Simplified unified generation workbench invariants
  assert.match(html, /id="new-scene-btn"[^>]*\bhidden\b/, 'Legacy #new-scene-btn must be hidden in unified titlebar');
  assert.match(html, /id="scene-submit"[^>]*\bhidden\b/, 'Duplicate right-pane #scene-submit must remain hidden');
  const paramGenMatch = html.match(/id="param-generate"[\s\S]*?id="gen-feed"/);
  assert.ok(paramGenMatch && !/<details\b/i.test(paramGenMatch[0]), 'Right generate pane must not contain nested <details>');

  // Editorial 0-radius discipline: no legacy 10px rounded gadget cards.
  assert.ok(!css.includes('border-radius:10px') && !css.includes('border-radius: 10px'),
    'Legacy rounded gadget card CSS must be removed to match 0-radius editorial system');
  assert.ok(css.includes('--paper: #f3f1ec') && css.includes('--ink: #0b0b0b'));
}

// 2. Viewport rAF Suspend / Resume & Synchronized Time Seeking
function verifyViewportLifecycle() {
  let nextRaf = 1;
  const activeRafs = new Set();
  const ctx = vm.createContext({
    window: { devicePixelRatio: 1 },
    ResizeObserver: class { observe() {} },
    Path2D: class { moveTo() {} lineTo() {} },
    Float32Array, Uint8Array, DataView, TextDecoder,
    getComputedStyle: () => ({ fontFamily: 'sans-serif' }),
    document: { body: {} },
    requestAnimationFrame: fn => { const id = nextRaf++; activeRafs.add(id); return id; },
    cancelAnimationFrame: id => { activeRafs.delete(id); },
    setTimeout, clearTimeout
  });
  vm.runInContext(readRenderer('motion-view.js') + '\nthis.MotionView = MotionView;', ctx);
  vm.runInContext(readRenderer('robot-view.js') + '\nthis.RobotView = RobotView;', ctx);
  vm.runInContext(readRenderer('plan-view.js') + '\nthis.PlanView = PlanView;', ctx);

  const mockCanvas = () => ({
    clientWidth: 640, clientHeight: 400, isConnected: true, style: {},
    getContext: () => ({ setTransform() {}, clearRect() {}, save() {}, restore() {}, beginPath() {}, stroke() {}, fill() {}, arc() {}, moveTo() {}, lineTo() {}, setLineDash() {}, fillText() {}, createRadialGradient: () => ({ addColorStop() {} }), closePath() {} }),
    addEventListener() {}, setPointerCapture() {}
  });

  const mv = new ctx.MotionView.View(mockCanvas(), {});
  mv.setClip({
    meta: { frames: 90, joint_count: 2, fps: 30, parents: [-1, 0], names: ['Pelvis', 'Head'] },
    data: new Float32Array(90 * 2 * 3)
  });
  mv.seekTime(1.5);
  assert.equal(mv.frame, 45, 'seekTime(1.5s) at 30fps seeks to frame 45');
  mv.yaw = 1.1;
  mv.setPlaying(true);
  assert.ok(activeRafs.size > 0);
  mv.suspend();
  assert.equal(mv.playing, false, 'Suspending pauses playback');
  assert.equal(mv.raf, 0, 'Suspending cancels rAF');
  assert.equal(mv.frame, 45, 'Suspending preserves frame position');
  const savedCam = mv.cameraState();
  mv.yaw = 0;
  mv.applyCameraState(savedCam);
  assert.ok(Math.abs(mv.yaw - 1.1) < 1e-6, 'Camera yaw restored across tab switches');
  mv.resume();
  assert.ok(mv.raf > 0, 'Resuming schedules rAF again');

  const rv = new ctx.RobotView.View(mockCanvas(), {});
  rv.clip = { frames: 60, fps: 60, poses: [], roots: [[0, 0, 0]] };
  rv.seekTime(0.5);
  assert.equal(rv.frame, 30, 'RobotView seekTime(0.5s) at 60fps seeks to frame 30 (time-normalized sync)');
  rv.suspend();
  assert.equal(rv.raf, 0);
  rv.resume();
  assert.ok(rv.raf > 0);

  const pv = new ctx.PlanView.View(mockCanvas(), { change() {}, select() {}, commit() {} });
  pv.suspend();
  assert.equal(pv.raf, 0);
  assert.equal(activeRafs.size, 2, 'Only resumed views hold active rAF handles');
}

// 3. MotionLoader Unified m* Binary + ma_* JSON Decoding & LRU Cache
async function verifyMotionLoader() {
  function encodeMfprv001(frames, joints, fps = 30) {
    let meta = JSON.stringify({
      frames, joint_count: joints, fps,
      parents: Array.from({ length: joints }, (_, i) => i - 1),
      names: Array.from({ length: joints }, (_, i) => 'j' + i)
    });
    while (meta.length % 4 !== 0) meta += ' ';
    const metaBytes = new TextEncoder().encode(meta);
    const padded = metaBytes.length;
    const total = 12 + padded + frames * joints * 12;
    const buf = new ArrayBuffer(total);
    const u8 = new Uint8Array(buf);
    u8.set(new TextEncoder().encode('MFPRV001'), 0);
    new DataView(buf).setUint32(8, padded, true);
    u8.set(metaBytes, 12);
    const f32 = new Float32Array(buf, 12 + padded, frames * joints * 3);
    f32.fill(0.25);
    return buf;
  }

  let networkCalls = 0;
  const ctx = vm.createContext({
    ArrayBuffer, Float32Array, Uint8Array, DataView, TextDecoder,
    S: { user: { username: 'alice' } },
    cloud: async path => {
      networkCalls++;
      if (path.endsWith('/preview.bin')) return encodeMfprv001(30, 2, 30);
      if (path.startsWith('/api/harness/motions/')) {
        return {
          fps: 30,
          parents: [-1, 0],
          names: ['Pelvis', 'Spine'],
          joints: Array.from({ length: 45 }, () => [[0, 0.9, 0], [0, 1.2, 0]]),
          scene: { objects: [{ id: 'box1', center_m: [0, 0.5, 1], size_m: [0.5, 1, 0.5], heading_deg: 0 }], targets: [], events: [], quality: { passed: true } }
        };
      }
      return { motion_id: 'm00000001', prompt: { text: 'A person stands.' }, owner: 'alice' };
    },
    ResizeObserver: class { observe() {} },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {}
  });
  vm.runInContext(readRenderer('motion-view.js') + '\nthis.MotionView = MotionView;', ctx);
  vm.runInContext(readRenderer('motion-loader.js') + '\nthis.MotionLoader = MotionLoader;', ctx);
  const ML = ctx.MotionLoader;

  const mId = 'm00000001';
  const maId = 'ma_' + 'a'.repeat(32);
  assert.equal(ML.isValidMotionId(mId), true);
  assert.equal(ML.isValidMotionId(maId), true);
  assert.equal(ML.isValidMotionId('bad_id'), false);

  const legacy = await ML.fetchPreview(mId);
  assert.equal(legacy.frames, 30);
  const managed = await ML.fetchPreview(maId);
  assert.equal(managed.frames, 45);
  assert.equal(managed.scene.objects[0].id, 'box1');
  const before = networkCalls;
  await ML.fetchPreview(mId);
  await ML.fetchPreview(maId);
  assert.equal(networkCalls, before, 'Cached m* and ma_* previews must not re-fetch');
  ML.clear();
  assert.equal(ML.has(mId), false);
}

// 4. Multi-Instance Workspace Tabs, Draft Guard, TaskBar Reopen, Restart Persistence & Gadget Close Guard
async function verifyWorkspaceAndGadgetModal() {
  const storage = new Map();
  const nodes = new Map();
  const stages = [], params = [];

  function makeNode(id = '') {
    const listeners = {};
    const classSet = new Set();
    const node = {
      id,
      value: id === 'gen-duration' ? '8' : id === 'gen-count' ? '2' : id === 'rt-speed' ? '1' : '',
      textContent: '',
      hidden: id.includes('guard') || id.includes('secret-box') || id.includes('drawer') || id.includes('completion'),
      disabled: false,
      checked: id === 'scope-read',
      open: false,
      readOnly: false,
      dataset: {},
      style: { setProperty() {} },
      attrs: {},
      children: [],
      options: [{ value: 'exact_english', disabled: false }],
      classList: {
        add: (...c) => c.forEach(x => classSet.add(x)),
        remove: (...c) => c.forEach(x => classSet.delete(x)),
        toggle: (c, force) => {
          const on = force === undefined ? !classSet.has(c) : Boolean(force);
          if (on) classSet.add(c); else classSet.delete(c);
          return on;
        },
        contains: c => classSet.has(c)
      },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] ?? null; },
      removeAttribute(k) { delete this.attrs[k]; },
      replaceChildren(...c) { this.children = c.filter(Boolean); },
      append(...c) { this.children.push(...c.filter(Boolean)); },
      addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
      removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter(x => x !== fn); },
      async fire(type, ev = {}) {
        for (const fn of [...(listeners[type] || [])]) await fn({ currentTarget: this, target: this, preventDefault() {}, stopPropagation() {}, ...ev });
      },
      async click(ev = {}) {
        if (this.onclick) await this.onclick({ currentTarget: this, target: this, preventDefault() {}, stopPropagation() {}, ...ev });
        await this.fire('click', ev);
      },
      showModal() { this.open = true; this.hidden = false; },
      close() { this.open = false; this.hidden = true; this.fire('close'); },
      focus() {},
      select() {},
      reset() {},
      querySelector: () => makeNode(),
      querySelectorAll: () => []
    };
    return node;
  }

  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  $('gen-model').querySelector = () => null;

  for (const s of ['preview', 'generate', 'editor', 'retarget']) {
    const st = $('stage-' + s); st.dataset.stage = s; stages.push(st);
    const pr = $('param-' + s); pr.dataset.param = s; params.push(pr);
  }
  const gadgetNav = $('open-gadget');
  const scopeRead = makeNode('scope-read'); scopeRead.value = 'read'; scopeRead.checked = true;

  let gadgetKeys = [];
  let harnessTaskState = {
    id: 'task_rep_1',
    source_motion_id: 'm00000001',
    goal: 'Adjust left arm height',
    status: 'waiting',
    candidates: [],
    operations: [{ id: 'op1', tool: 'edit_motion', status: 'waiting' }]
  };

  const S = {
    user: { username: 'alice', role: 'user' },
    session: 1,
    view: 'library',
    status: { server: 'https://server-a.example', serverConfigured: true },
    robots: [{ id: 'unitree_g1_23dof', label: 'Humanoid 02' }, { id: 'unitree_g1_23dof', label: 'Unitree G1' }],
    lib: { selected: 'm00000001', scope: 'all', items: [{ motion_id: 'm00000001', text: 'A person walks.' }] },
    gen: { jobs: [], open: new Set(), results: new Map(), resultData: new Map(), pendingAttach: new Map(), retryParent: null, tabKey: 'default' },
    rt: { items: [], robot: 'unitree_g1_23dof', viewMode: 'robot', playing: null, sourceSnapshot: null, tabKey: 'default' }
  };

  let repairMockState = { sourceId: '', prompt: '', dirty: false, taskId: null };
  let seqMockState = { mode: 'sequence', binIds: [], clips: [], dirty: false };
  let sceneMockState = { objects: [], duration: 4 };

  const windowObj = {
    innerWidth: 1480,
    desktop: { download: async () => ({ saved: 'Kinora-Gadget.zip' }) },
    addEventListener() {},
    removeEventListener() {},
    applyLibraryScope(scope) { S.lib.scope = scope; },
    applyStageView(view) {
      S.view = view;
      const map = { library: 'preview', preview: 'preview', generate: 'generate', editor: 'editor', retarget: 'retarget' };
      const st = map[view] || 'preview';
      documentObj.body.dataset.stage = st;
      for (const n of stages) n.hidden = n.dataset.stage !== st;
      for (const p of params) p.hidden = p.dataset.param !== st;
    },
    updateChecks() {},
    setRetargetViewMode(m) { S.rt.viewMode = m; },
    syncRetargetSource() {},
    ensureRetargetHumanLoaded() {},
    refreshLibraryCardBadges() {},
    selectMotion(id) { S.lib.selected = id; },
    loadCustomResults: async () => {},
    playRetarget: async job => { S.rt.playing = job.id; },
    clearRetargetPlayback: () => { S.rt.playing = null; S.rt.playingJob = null; },
    motionView: { clip: null, clear() {}, suspend() {}, resume() {} },
    robotView: { clip: null, speed: 1, loop: true, clear() {}, suspend() {}, resume() {}, cameraState: () => ({ yaw: 0.5 }), applyCameraState() {} },
    rtHumanView: { clip: null, speed: 1, loop: true, clear() {}, suspend() {}, resume() {}, cameraState: () => ({ yaw: 0.6 }), applyCameraState() {} },
    SceneController: {
      view: { clear() {}, cameraState: () => ({ yaw: 0.4 }), applyCameraState() {} },
      open: async () => {},
      close() {},
      isDirty: (snapshot = sceneMockState) => snapshot.objects.length > 0,
      clearDraft() { sceneMockState = { objects: [], duration: 4 }; },
      getState: () => JSON.parse(JSON.stringify(sceneMockState)),
      setState: async s => { sceneMockState = JSON.parse(JSON.stringify(s || { objects: [], duration: 4 })); },
      pollTask: async () => null,
      sceneActorAwaitingSubmission: actor => actor.status === 'ready' && !actor.operations?.length
    },
    RepairEditor: {
      isDirty: () => Boolean(repairMockState.dirty),
      getLoadedSource: () => repairMockState.sourceId,
      clearDraft() { repairMockState = { sourceId: '', prompt: '', dirty: false, taskId: null }; },
      loadMotion: async id => { repairMockState.sourceId = id; },
      openTask: async tid => { repairMockState.taskId = tid; repairMockState.sourceId = harnessTaskState.source_motion_id; },
      getState: () => JSON.parse(JSON.stringify(repairMockState)),
      setState: async s => { repairMockState = { sourceId: '', prompt: '', dirty: false, taskId: null, ...(s || {}) }; },
      pollTask: async () => harnessTaskState
    },
    SequenceEditor: {
      getMode: () => seqMockState.mode,
      setMode: m => { seqMockState.mode = m; },
      importMotion: async id => { if (!seqMockState.binIds.includes(id)) seqMockState.binIds.push(id); seqMockState.clips = [{ id }]; },
      getClipIds: () => seqMockState.clips.map(c => c.id),
      isDirty: () => Boolean(seqMockState.dirty),
      clearDraft() { seqMockState = { mode: 'sequence', binIds: [], clips: [], dirty: false }; },
      getState: () => JSON.parse(JSON.stringify(seqMockState)),
      setState: async s => { seqMockState = { mode: 'sequence', binIds: [], clips: [], dirty: false, ...(s || {}) }; },
      showResult: async () => {}
    }
  };

  const documentObj = {
    body: { dataset: { screen: 'ready', stage: 'preview', genMode: 'text' }, append() {} },
    activeElement: null,
    querySelector: sel => sel === '[data-nav=gadget]' ? gadgetNav : null,
    querySelectorAll: sel => {
      if (sel === '[name=gadget-scope]:checked') return [scopeRead];
      if (sel === '.ws-stage') return stages;
      if (sel === '.ws-param') return params;
      return [];
    }
  };

  const ctx = vm.createContext({
    $, S, window: windowObj, document: documentObj,
    h: (tag, props = {}, ...children) => {
      const n = makeNode(); n.tag = tag; Object.assign(n, props); n.children = children.flat().filter(Boolean); return n;
    },
    randomId: () => webcrypto.randomUUID().replaceAll('-', ''),
    fmtDate: () => '11-17 12:00',
    toast() {},
    quiet(e) { if (e) throw e; },
    ACTIVE_CUSTOM: ['queued', 'running'],
    ACTIVE_RT: ['queued', 'running', 'stopping'],
    MotionLoader: {
      getCachedMeta: id => id.startsWith('ma_') ? { label: 'Edited motion', parent_id: 'm00000001' } : { label: 'Library motion' },
      isManagedAsset: id => id.startsWith('ma_'),
      isValidMotionId: () => true
    },
    localStorage: {
      getItem: k => storage.get(k) ?? null,
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: k => storage.delete(k)
    },
    navigator: { clipboard: { writeText: async () => {} } },
    cloud: async (path, opt = {}) => {
      if (path === '/api/gadget/keys' && opt.method === 'POST') return { key: 'mfg_one_time_secret_123' };
      if (path === '/api/gadget/keys') return { keys: gadgetKeys };
      if (path === '/api/harness/tasks') return { items: [harnessTaskState] };
      return {};
    },
    setTimeout: fn => { fn(); return 1; },
    clearTimeout() {},
    console
  });
  ctx.globalThis = ctx;

  vm.runInContext(readRenderer('gadget.js'), ctx);
  vm.runInContext(readRenderer('video-motion.js'), ctx);
  vm.runInContext(readRenderer('workspace.js'), ctx);

  const WS = windowObj.Workspace;
  await WS.restoreSession();
  assert.equal(WS.isPreviewActive(), true, 'Initial state starts on library preview');

  const sourceTab = await WS.activateOrCreateTab('generate', { forceNew: true });
  $('gen-text').value = 'Submitted prompt';
  WS.syncTabDirty();
  const otherTab = await WS.activateOrCreateTab('generate', { forceNew: true });
  $('gen-text').value = 'Unsubmitted prompt in a separate tab';
  WS.syncTabDirty();
  sourceTab.state.selectedCandidateId = 'm00000001';
  WS.recordGenerateSubmission(sourceTab.id, 'job-1', 'Submitted prompt', null);
  assert.equal(sourceTab.state.boundJobId, 'job-1');
  assert.equal(sourceTab.state.selectedCandidateId, null);
  assert.equal(sourceTab.dirty, false);
  assert.equal(otherTab.state.boundJobId, null);
  assert.equal(otherTab.dirty, true);
  assert.equal($('gen-text').value, 'Unsubmitted prompt in a separate tab');
  const sceneSnapshot = { fingerprint: 'submitted scene', objects: [] };
  WS.recordSceneSubmission(sourceTab.id, 'task_scene_tab_a', sceneSnapshot, 'Submitted prompt');
  assert.equal(sourceTab.state.scene.taskId, 'task_scene_tab_a');
  assert.equal(sourceTab.state.scene.submittedSnapshot, sceneSnapshot);
  assert.equal(otherTab.state.scene.taskId, null);
  assert.equal(otherTab.dirty, true);
  await WS.activateTab(sourceTab.id);
  $('gen-text').value = 'Edited again before the response returned';
  WS.recordGenerateSubmission(sourceTab.id, 'job-2', 'Submitted prompt', null);
  assert.equal(sourceTab.dirty, true);
  assert.equal($('gen-text').value, 'Edited again before the response returned');

  // Video file selection must remain isolated per generate tab across switches and closes
  $('gen-modality-video').click();
  $('video-motion-file').files = [{ name: 'tab-a.mp4', size: 2048 }];
  await $('video-motion-file').fire('change');
  assert.equal(windowObj.VideoMotion.canSubmit(), true);
  assert.match($('video-drop-hint').textContent, /tab-a\.mp4/);
  await WS.activateTab(otherTab.id);
  $('gen-modality-video').click();
  assert.equal(windowObj.VideoMotion.canSubmit(), false, 'Second generate tab must not inherit first tab video file');
  assert.doesNotMatch($('video-drop-hint').textContent, /tab-a\.mp4/);
  await WS.activateTab(sourceTab.id);
  assert.equal(windowObj.VideoMotion.canSubmit(), true, 'Switching back restores first tab video selection');
  assert.match($('video-drop-hint').textContent, /tab-a\.mp4/);
  const closeVideoTab = WS.closeTab(sourceTab.id);
  await new Promise(r => setImmediate(r));
  await $('draft-guard-discard').fire('click');
  assert.equal(await closeVideoTab, true);
  await WS.activateTab(otherTab.id);
  assert.equal(windowObj.VideoMotion.canSubmit(), false);

  WS.onSessionReset();
  windowObj.clearSavedWorkspace();

  const handoff = await WS.openMotionInTab('editor', 'm00000008', { subMode: 'repair' });
  await WS.activateTab('preview');
  assert.equal(await WS.openMotionInTab('editor', 'm00000008', { subMode: 'sequence' }), handoff);
  assert.equal(seqMockState.mode, 'sequence');
  assert.equal(seqMockState.clips.length, 1);
  assert.equal(seqMockState.clips[0].id, 'm00000008');
  assert.equal(repairMockState.sourceId, 'm00000008');
  await WS.openMotionInTab('editor', 'm00000008', { subMode: 'sequence' });
  assert.equal(seqMockState.clips.length, 1, 'An exact timeline match must not add a duplicate clip');
  assert.equal(await WS.closeTab(handoff.id), true);

  // Multi-instance tabs of the same type maintain isolated state across switches
  const edTab1 = await WS.openMotionInTab('editor', 'm00000001', { label: 'Walk', subMode: 'repair', forceNew: true });
  repairMockState.prompt = 'Raise left hand';
  repairMockState.dirty = true;
  WS.syncTabDirty();

  const edTab2 = await WS.openMotionInTab('editor', 'm00000002', { label: 'Run', subMode: 'repair', forceNew: true });
  assert.notEqual(edTab1.id, edTab2.id, 'Multiple editor tabs have distinct IDs');
  assert.equal(WS.state.tabs.filter(t => t.kind === 'editor').length, 2);
  assert.equal(repairMockState.sourceId, 'm00000002');
  assert.equal(repairMockState.prompt, '', 'Second editor tab starts with clean prompt');

  await WS.activateTab(edTab1.id);
  assert.equal(repairMockState.sourceId, 'm00000001');
  assert.equal(repairMockState.prompt, 'Raise left hand', 'Switching back to first editor tab restores its exact draft');

  // Core flow handoff: edited ma_* candidate -> Retarget tab with source lineage + compare mode
  const maEdited = 'ma_' + 'f'.repeat(32);
  const rtTab1 = await WS.openMotionInTab('retarget', maEdited, { label: 'Edited walk', parentId: 'm00000001', forceNew: true });
  $('rt-params').value = '{"smooth_weight": 3}';
  windowObj.setRetargetViewMode('compare');
  const rtTab2 = await WS.openMotionInTab('retarget', 'm00000009', { label: 'Jump', forceNew: true });
  assert.equal(WS.state.tabs.filter(t => t.kind === 'retarget').length, 2, 'Supports multiple Retarget tabs');
  await WS.activateTab(rtTab1.id);
  assert.equal($('rt-motion').value, maEdited);
  assert.equal(S.rt.sourceSnapshot.parentId, 'm00000001', 'Retarget tab preserves exact parent source lineage');
  assert.equal(S.rt.viewMode, 'compare', 'Retarget tab preserves side-by-side compare mode');
  assert.equal($('rt-params').value, '{"smooth_weight": 3}');

  // Earlier betas could persist a selected old result after replacing its source.
  await WS.activateTab(rtTab2.id);
  S.rt.items = [{id:'stale-job',status:'done',motion_id:'m00000009'}];
  rtTab1.state.playingJobId = 'stale-job';
  await WS.activateTab(rtTab1.id);
  assert.equal(S.rt.playing,null,'Restoring a tab must not play a result for another source');
  await WS.activateTab(rtTab2.id);
  S.rt.items.push({id:'matching-job',status:'done',motion_id:maEdited});
  rtTab1.state.playingJobId = 'matching-job';
  await WS.activateTab(rtTab1.id);
  assert.equal(S.rt.playing,'matching-job','A saved result for the exact source still restores');

  $('rt-params').value = '{"smooth_weight": 7}';
  WS.syncTabDirty();
  WS.recordRetargetSubmission(rtTab2.id, '{"smooth_weight": 2}', 'retarget-bg');
  assert.equal(rtTab2.state.baselineParams, '{"smooth_weight": 2}');
  assert.equal(rtTab2.state.jobId, 'retarget-bg');
  assert.equal(rtTab1.state.jobId, null);
  assert.equal(rtTab1.state.baselineParams, '');
  assert.equal(rtTab1.dirty, true, 'Another tab’s retarget response must not mark this draft as submitted');
  assert.equal($('rt-params').value, '{"smooth_weight": 7}');
  $('rt-params').value = '{"smooth_weight": 3}';
  WS.recordRetargetSubmission(rtTab1.id, '{"smooth_weight": 4}');
  assert.equal(rtTab1.dirty, true, 'Parameter edits made while the request is pending remain a draft');

  // Draft Guard: closing dirty edTab1 prompts -> Save Draft -> Reopen from Saved Drafts
  const closePromise = WS.closeTab(edTab1.id);
  await new Promise(r => setImmediate(r));
  assert.equal($('draft-guard-dialog').open, true, 'Closing a dirty tab opens draft guard dialog');
  await $('draft-guard-cancel').fire('click');
  assert.equal(await closePromise, false, 'Choosing continue editing keeps tab open');
  assert.ok(WS.state.tabs.some(t => t.id === edTab1.id));

  const saveClosePromise = WS.closeTab(edTab1.id);
  await new Promise(r => setImmediate(r));
  await $('draft-guard-save').fire('click');
  assert.equal(await saveClosePromise, true);
  assert.ok(!WS.state.tabs.some(t => t.id === edTab1.id));
  assert.equal(WS.state.savedDrafts.length, 1, 'Saved draft stored in workspace drafts');

  // Background task completion while tab is closed -> quiet bottom bar notification -> reopen task
  const sceneTask = {id:'task_scene_regression', goal:'Touch the box', status:'waiting',
    operations:[{tool:'generate_motion',status:'waiting'}]};
  WS.registerHarnessTask(sceneTask, 'scene');
  WS.registerHarnessTask(sceneTask, 'repair');
  WS.registerHarnessTask({id:sceneTask.id,goal:sceneTask.goal,status:'waiting'}, 'repair');
  assert.equal(WS.state.harnessTasks.get(sceneTask.id).kind, 'scene',
    'The editor task list must not reclassify a scene and poll its repair endpoint');
  WS.state.harnessTasks.delete(sceneTask.id);
  WS.registerHarnessTask(harnessTaskState, 'repair');
  harnessTaskState = {
    ...harnessTaskState,
    status: 'succeeded',
    candidates: [{ id: 'c1', motion_id: maEdited, status: 'ready' }],
    operations: [{ id: 'op1', tool: 'edit_motion', status: 'succeeded' }]
  };
  await WS.pollHarnessTasks();
  assert.equal($('taskbar-completion').hidden, false, 'Completed background task surfaces non-intrusive bottom bar prompt');
  await $('taskbar-completion-open').click();
  await new Promise(r => setImmediate(r));
  assert.equal(documentObj.body.dataset.stage, 'editor', 'TaskBar reopens editor stage for completed background task');
  assert.equal(repairMockState.taskId, 'task_rep_1');

  // Restart persistence & strict account isolation
  S.lib.scope = 'results';
  WS.toggleLeftPane(true);
  S.gen.pendingAttach.set('text_job', {
    tabId: 'original_tab',
    spec: { objects: [{ id: 'box' }], attachment: { object_id: 'box' } },
    requestId: 'stable_object_request',
    sourceMotionId: 'm00000001',
    started: true,
    loading: true
  });
  WS.flushSave();
  const aliceOpenCount = WS.state.tabs.length;
  assert.ok(aliceOpenCount >= 3);

  // Switch to bob: must not see alice's tabs
  WS.onSessionReset();
  S.user = { username: 'bob', role: 'user' };
  await WS.restoreSession();
  assert.equal(WS.state.tabs.length, 0, 'Account B must not inherit Account A tabs');
  assert.equal(S.lib.scope, 'all', 'Account B starts with its default library scope');
  assert.equal(S.gen.pendingAttach.size, 0, 'Account B must not resume Account A object bindings');

  // Log back in as alice (simulating app restart): restores tabs, collapse state, and saved drafts
  WS.onSessionReset();
  S.user = { username: 'alice', role: 'user' };
  await WS.restoreSession();
  assert.equal(WS.state.tabs.length, aliceOpenCount, 'Restarting as Alice restores all open tabs');
  assert.equal(WS.state.leftCollapsed, true, 'Restores collapsed left panel preference');
  assert.equal(S.lib.scope, 'results', 'Account A restores its saved library scope');
  assert.equal(WS.state.savedDrafts.length, 1, 'Restores saved drafts across restart');
  const attachment = S.gen.pendingAttach.get('text_job');
  assert.equal(attachment?.requestId, 'stable_object_request', 'Resumes with the original object-response request ID');
  assert.equal(attachment?.sourceMotionId, 'm00000001');
  assert.equal(attachment?.spec.attachment.object_id, 'box');
  assert.equal(attachment?.started, undefined, 'Restart releases the in-flight request lock');
  assert.equal(attachment?.loading, undefined, 'Restart releases the candidate lookup lock');

  windowObj.clearSavedWorkspace();
  WS.onSessionReset();
  await WS.restoreSession();
  assert.equal(WS.state.tabs.length, 0, 'Clearing local session removes persisted workspace');
  assert.equal(S.gen.pendingAttach.size, 0);

  for (let index = 0; index < 26; index++) {
    const tab = await WS.activateOrCreateTab('generate', { forceNew: true });
    $('gen-text').value = 'Saved draft ' + index;
    const closing = WS.closeTab(tab.id);
    await $('draft-guard-save').fire('click');
    assert.equal(await closing, true);
  }
  for (let index = 0; index < 13; index++) {
    await WS.activateOrCreateTab('generate', { forceNew: true });
    $('gen-text').value = 'Open draft ' + index;
  }
  const lastTab = WS.state.activeTabId;
  WS.flushSave();
  WS.onSessionReset();
  await WS.restoreSession();
  assert.equal(WS.state.tabs.length, 13);
  assert.equal(WS.state.activeTabId, lastTab);
  assert.equal($('gen-text').value, 'Open draft 12');
  assert.equal(WS.state.savedDrafts.length, 26);
  assert.equal(WS.state.savedDrafts.at(-1).state.description, 'Saved draft 0');
  windowObj.clearSavedWorkspace();
  WS.onSessionReset();

  const workspaceKey = 'kinora-workspace:v1:' + JSON.stringify([S.status.server, S.user.username]);
  const usableTab = {
    id: 'tab_usable', kind: 'retarget', state: { sourceId: 'm00000001', paramsText: '{"smooth_weight":0.3}' }
  };
  storage.set(workspaceKey, JSON.stringify({
    version: 1, activeTabId: 'tab_damaged',
    tabs: [null, { id: 'tab_damaged', kind: 'retarget', state: null }, usableTab, usableTab],
    savedDrafts: [null, { id: 'draft_usable', kind: 'generate', state: { description: 'Preserved draft' } }]
  }));
  await WS.restoreSession();
  assert.equal(WS.isPreviewActive(), true, 'A broken saved active tab cannot block login');
  assert.deepEqual(Array.from(WS.state.tabs, tab => tab.id), ['tab_usable']);
  assert.equal(WS.state.savedDrafts.length, 1);
  assert.equal(WS.state.savedDrafts[0].state.description, 'Preserved draft');
  await WS.activateTab('tab_usable');
  assert.equal($('rt-motion').value, 'm00000001');
  assert.equal($('rt-params').value, '{"smooth_weight":0.3}');
  windowObj.clearSavedWorkspace();
  WS.onSessionReset();

  storage.set(workspaceKey, JSON.stringify({
    version: 1, activeTabId: 'tab_retryable',
    tabs: [{ id: 'tab_retryable', kind: 'editor', state: { subMode: 'repair', repair: { sourceId: 'm00000001' } } },
      usableTab]
  }));
  const restoreRepairState = windowObj.RepairEditor.setState;
  const restoreQuiet = ctx.quiet;
  const restoreWarnings = [];
  ctx.quiet = error => restoreWarnings.push(error.message);
  windowObj.RepairEditor.setState = async () => { throw Error('Saved source is temporarily unavailable'); };
  try {
    await WS.restoreSession();
    assert.equal(WS.isPreviewActive(), true, 'A failing editor controller must leave the workspace usable');
    assert.equal(WS.state.tabs[0].state.repair.sourceId, 'm00000001', 'The tab can be retried later');
    assert.deepEqual(restoreWarnings, ['Saved source is temporarily unavailable']);
    await WS.activateTab('tab_usable');
    assert.equal(S.view, 'retarget');
  } finally {
    windowObj.RepairEditor.setState = restoreRepairState;
    ctx.quiet = restoreQuiet;
    windowObj.clearSavedWorkspace();
    WS.onSessionReset();
  }

  storage.set(workspaceKey, JSON.stringify({
    version: 1, activeTabId: 'preview',
    savedDrafts: [{ id: 'draft_retryable', kind: 'generate', title: 'Retryable draft', savedAt: 1,
      state: { description: 'Keep this draft', scene: { objects: [], duration: 4 } } }]
  }));
  await WS.restoreSession();
  await $('taskbar-drafts-btn').click();
  const restoreButton = () => $('taskbar-list').children[0].children[3].children[0];
  const restoreSceneState = windowObj.SceneController.setState;
  const setQuiet = ctx.quiet;
  ctx.quiet = () => {};
  const retryableDraft = WS.state.savedDrafts[0];
  let finishRestore;
  let restoreCalls = 0;
  windowObj.SceneController.setState = async state => {
    restoreCalls++;
    state.objects.push({ id: 'partial-restore' });
    await new Promise(resolve => { finishRestore = resolve; });
    throw Error('The draft failed to load');
  };
  try {
    const button = restoreButton();
    const opening = button.click();
    const repeated = button.click();
    await new Promise(resolve => setImmediate(resolve));
    try {
      assert.equal(restoreButton().disabled, true);
      WS.flushSave();
      const savedDuringRestore = JSON.parse(storage.get(workspaceKey));
      assert.deepEqual(savedDuringRestore.tabs, []);
      assert.equal(savedDuringRestore.savedDrafts[0].id, 'draft_retryable');
    } finally {
      finishRestore?.();
    }
    await Promise.all([opening, repeated]);
    assert.equal(restoreCalls, 1);
    assert.equal(WS.isPreviewActive(), true);
    assert.equal(WS.state.tabs.length, 0);
    assert.equal(WS.state.savedDrafts.length, 1);
    assert.equal(WS.state.savedDrafts[0], retryableDraft);
    assert.equal(WS.state.savedDrafts[0].state.scene.objects.length, 0);
    WS.flushSave();
    assert.equal(JSON.parse(storage.get(workspaceKey)).savedDrafts[0].id, 'draft_retryable');
    windowObj.SceneController.setState = restoreSceneState;
    await restoreButton().click();
    assert.equal(WS.state.savedDrafts.length, 0);
    assert.equal(WS.state.tabs.length, 1);
    assert.equal($('gen-text').value, 'Keep this draft');
  } finally {
    windowObj.SceneController.setState = restoreSceneState;
    ctx.quiet = setQuiet;
    windowObj.clearSavedWorkspace();
    WS.onSessionReset();
  }

  // Gadget Modal: smart default tab & unsaved one-time secret close guard
  gadgetKeys = [];
  await gadgetNav.click();
  await new Promise(r => setImmediate(r));
  assert.equal($('gadget-dialog').open, true);
  assert.equal($('gadget-panel-config').hidden, false, 'Account with no active keys defaults to Setup Config tab');

  gadgetKeys = [{ id: 'k1', name: 'Cursor', scopes: '["read"]', expires: Date.now() / 1000 + 3600, status: 'active' }];
  await gadgetNav.click();
  await new Promise(r => setImmediate(r));
  assert.equal($('gadget-panel-keys').hidden, false, 'Account with active keys defaults to My Keys tab');

  $('gadget-name').value = 'New CLI';
  $('gadget-days').value = '30';
  await $('gadget-form').onsubmit({ preventDefault() {} });
  assert.equal($('gadget-secret').value, 'mfg_one_time_secret_123');

  // Attempt to close while secret is displayed -> blocked by guard!
  await $('gadget-close').click();
  assert.equal($('gadget-dialog').open, true, 'Gadget modal must block close when one-time secret has not been acknowledged');
  assert.equal($('gadget-close-guard').hidden, false);

  let escPrevented = false;
  await $('gadget-dialog').fire('cancel', { preventDefault() { escPrevented = true; } });
  assert.equal(escPrevented, true, 'Escape key is also intercepted while secret is unacknowledged');

  await $('gadget-guard-confirm').click();
  assert.equal($('gadget-secret').value, '', 'Confirming guard clears secret from memory');
  assert.equal($('gadget-dialog').open, false, 'Modal closes cleanly after confirmation');

  // Peer [文本指令 | 动作视频] modality switch + tab persistence + actors_group state
  const genTab = await WS.activateOrCreateTab('generate', { forceNew: true });
  assert.equal(documentObj.body.dataset.genModality, 'text');
  assert.equal($('gen-text-pane').hidden, false);
  assert.equal($('gen-video-pane').hidden, true);
  await $('gen-modality-video').fire('click');
  assert.equal(documentObj.body.dataset.genModality, 'video');
  assert.equal($('gen-text-pane').hidden, true);
  assert.equal($('gen-video-pane').hidden, false);
  assert.equal($('gen-model').hidden, true);
  assert.equal($('gen-count-wrap').hidden, true);
  await $('gen-modality-text').fire('click');
  assert.equal(documentObj.body.dataset.genModality, 'text');
  assert.equal($('gen-text-pane').hidden, false);

  S.gen.jobs = [
    {id:'hgen_op_for_first_actor',status:'running',description:'Underlying actor worker'},
    {id:'ordinary-text-generation',status:'running',description:'Independent text job'}
  ];
  const pendingGroupTask = {
    id: 'task_group_1',
    goal: 'Multi-actor carry',
    status: 'succeeded',
    candidates: [{ motion_id: 'ma_' + '1'.repeat(32), status: 'ready' }],
    actors_group: [
      { actor_index: 0, task_id: 'task_group_1', label: 'R1', status: 'succeeded', candidates: [{ motion_id: 'ma_' + '1'.repeat(32), status: 'ready' }],
        operations: [{id:'op_for_first_actor',tool:'generate_motion',arguments:{scene:{}}}] },
      { actor_index: 1, task_id: 'task_group_2', label: 'R2', status: 'ready', candidates: [], operations: [] }
    ]
  };
  WS.state.drawerFilter = 'all';
  WS.state.drawerOpen = true;
  WS.registerHarnessTask(pendingGroupTask, 'scene');
  const firstPoll = await WS.pollHarnessTasks().catch(() => {});
  void firstPoll;
  // Re-check via collect/taskbar: while actor 2 has no candidate, group task remains running
  assert.ok($('taskbar-active-pill').textContent.includes('后台运行中'), 'Multi-actor group remains running until all actors finish');
  const rows = $('taskbar-list').children;
  assert.ok(rows.some(row => row.dataset?.taskbarRow === 'custom:ordinary-text-generation'));
  assert.ok(rows.every(row => row.dataset?.taskbarRow !== 'custom:hgen_op_for_first_actor'));
  assert.match(rows.find(row => row.dataset?.taskbarRow === 'harness:task_group_1').children[0].text, /有角色待续交/);
  const stoppedGroup = {...pendingGroupTask, status:'cancelled'};
  WS.registerHarnessTask(stoppedGroup, 'scene');
  assert.equal(WS.registerHarnessTask(pendingGroupTask, 'scene'), stoppedGroup,
    'A response captured before cancellation must not restore the running group.');
  assert.equal(WS.state.harnessTasks.get(stoppedGroup.id).task, stoppedGroup);
  const stoppedSummary = {id:'cancelled-from-server',scene_group:true,status:'cancelled'};
  WS.registerHarnessTask(stoppedSummary, 'scene');
  const stoppedDetail = {...stoppedSummary, actors_group:stoppedGroup.actors_group};
  windowObj.SceneController.pollTask = async id => id === stoppedSummary.id ? stoppedDetail : null;
  await WS.pollHarnessTasks();
  assert.equal(WS.state.harnessTasks.get(stoppedSummary.id).task, stoppedDetail,
    'Load the actors of a cancelled group restored from the task list.');
  await WS.closeTab(genTab.id);

  const savedRetarget = {
    id:'retarget-historical', status:'queued', motion_id:maEdited, robot:'unitree_g1_23dof',
    execution:JSON.stringify({parameters:{smooth_weight:1.4,root_orientation_weight:64}}),
    requested_execution:JSON.stringify({parameters:{smooth_weight:9}})
  };
  S.rt.items.push(savedRetarget);
  async function openSavedRetarget() {
    WS.state.drawerOpen = false;
    await $('taskbar-open-btn').fire('click');
    const row = $('taskbar-list').children.find(item => item.dataset?.taskbarRow === 'retarget:' + savedRetarget.id);
    assert.ok(row, 'The saved retarget job is listed in the task drawer');
    await row.children[3].children.find(button => button.dataset?.taskbarAction === 'open').onclick();
    return WS.state.tabs.find(tab => tab.id === WS.state.activeTabId);
  }
  const playingOnly = await WS.openMotionInTab('retarget', maEdited, {
    robotId:savedRetarget.robot, forceNew:true
  });
  $('rt-params').value = '{"smooth_weight":2}';
  WS.recordRetargetSubmission(playingOnly.id, $('rt-params').value, 'different-job', savedRetarget.robot, maEdited);
  S.rt.playing = savedRetarget.id;
  const historical = await openSavedRetarget();
  assert.notEqual(historical.id, playingOnly.id, 'A playback selection cannot reuse another job’s form');
  const parameters = JSON.parse(savedRetarget.execution).parameters;
  assert.deepEqual(JSON.parse($('rt-params').value), parameters, 'Taskbar opens the effective execution parameters');
  assert.equal(historical.state.baselineParams, $('rt-params').value);
  assert.equal(historical.dirty, false);
  $('rt-params').value = '{"smooth_weight":7}';
  WS.syncTabDirty();
  assert.equal(historical.dirty, true);
  const reopened = await openSavedRetarget();
  assert.notEqual(reopened.id, historical.id);
  assert.equal(historical.state.paramsText, '{"smooth_weight":7}', 'History never overwrites a dirty tab');
  assert.deepEqual(JSON.parse($('rt-params').value), parameters);

  // Older workspaces could bind a job while leaving the form and its baseline empty.
  $('rt-params').value = '';
  reopened.state.baselineParams = '';
  WS.syncTabDirty();
  assert.equal((await openSavedRetarget()).id, reopened.id);
  assert.deepEqual(JSON.parse($('rt-params').value), parameters);
}

// 5. Video Modality + /api/harness/object-response Auto-Attachment & Retarget Preview Sync
async function verifyVideoObjectResponseAndRetargetSync() {
  const nodes = new Map();
  function makeEl(id = '') {
    const listeners = {};
    return {
      id,
      value: '',
      textContent: '',
      hidden: false,
      disabled: false,
      files: [],
      children: [],
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
      async fire(t, ev = {}) { for (const fn of listeners[t] || []) await fn({ currentTarget: this, target: this, preventDefault() {}, ...ev }); },
      replaceChildren(...c) { this.children = c.filter(Boolean); },
      setAttribute(k, v) { this[k] = String(v); },
      querySelectorAll: () => []
    };
  }
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, makeEl(id));
    return nodes.get(id);
  };

  let videoStatus = 'tracking';
  let videoMotionId = null;
  const calls = [];
  const attachedObjects = [{ id: 'object1-box', center_m: [0, 0.275, 0.8], size_m: [0.5, 0.4, 0.4], heading_deg: 0 }];
  const coupledResults = [];
  const setSceneCalls = [];

  class MockFileReader {
    readAsDataURL() {
      this.result = 'data:video/mp4;base64,AAAA';
      setImmediate(() => this.onload?.());
    }
  }

  const ctx = vm.createContext({
    $,
    S: { session: 1, view: 'retarget', user: { username: 'alice' }, robots: [], rt: { playing: null, playingJob: null, previewRequest: 0, viewMode: 'robot', quarantinedSources: new Set() } },
    FileReader: MockFileReader,
    document: { body: { dataset: { genModality: 'video', stage: 'generate' } }, querySelectorAll: () => [] },
    window: null,
    h: (tag, props = {}, ...children) => ({ tag, ...props, children: children.flat().filter(Boolean) }),
    setTimeout,
    clearTimeout,
    updateChecks() {},
    previewGeneratedCandidate: async () => {},
    submitOnce: async (kind, path, body, idField) => {
      calls.push({ kind, path, body, idField });
      if (path === '/api/harness/video-motion') {
        return { id: 'vjob_1', status: 'queued' };
      }
      if (path === '/api/harness/object-response') {
        coupledResults.push(body);
        return { id: 'task_objresp_1', candidates: [{ motion_id: 'ma_' + '9'.repeat(32), status: 'ready' }] };
      }
      return {};
    },
    cloud: async (path, opt = {}) => {
      calls.push({ path, opt });
      if (path === '/api/harness/video-motion') {
        return { items: [{ id: 'vjob_1', status: videoStatus, motion_id: videoMotionId, frames: 90 }] };
      }
      if (path === '/api/retarget/mesh-preview') {
        return { version: 1, poses: [[]], roots: [[0, 0]], frames: 30, fps: 30 };
      }
      if (path.startsWith('/api/harness/motions/') && path.endsWith('/preview')) {
        return { scene: { objects: attachedObjects } };
      }
      return {};
    },
    robotView: {
      clear() {},
      load: async () => true,
      setSceneObjects: objs => setSceneCalls.push(objs)
    },
    rtHumanView: { clip: null, seekTime() {} },
    viewportMessage() {},
    renderRetargetAcceptanceBar() {},
    ensureRetargetHumanLoaded() {},
    refreshRetarget() {},
    SceneController: {
      getAttachmentRequest: () => ({
        objects: attachedObjects,
        attachment: { object_id: 'object1-box', start_frame: null, end_frame: null, auto_detect: true, mode: 'lift', auto_place: true }
      }),
      runObjectResponse: async (sourceMotionId, spec) => {
        return ctx.submitOnce('objresp:' + sourceMotionId, '/api/harness/object-response', {
          source_motion_id: sourceMotionId,
          objects: spec.objects,
          attachment: spec.attachment
        }, 'request_id');
      }
    }
  });
  ctx.window = ctx;
  ctx.globalThis = ctx;

  vm.runInContext(readRenderer('video-motion.js'), ctx);
  $('video-motion-file').files = [{ name: 'lift.mp4', size: 1024 }];
  await $('video-motion-file').fire('change');
  assert.equal(ctx.VideoMotion.canSubmit(), true, 'VideoMotion becomes submittable when valid video file is chosen');

  await ctx.VideoMotion.submit();
  assert.equal(calls.some(c => c.path === '/api/harness/video-motion'), true);
  assert.equal(coupledResults.length, 0, 'Object-response waits until video extraction completes');

  videoStatus = 'done';
  videoMotionId = 'ma_' + '7'.repeat(32);
  await ctx.VideoMotion.refresh();
  assert.equal(coupledResults.length, 1, 'Completed video extraction automatically triggers POST /api/harness/object-response');
  assert.equal(coupledResults[0].source_motion_id, videoMotionId);
  assert.equal(coupledResults[0].attachment.object_id, 'object1-box');
  assert.equal(coupledResults[0].attachment.auto_detect, true);

  // Verify playRetarget ma_* scene object preview sync
  const appSrc = readRenderer('app.js');
  const quarantineStart = appSrc.indexOf('function retargetSourceKey(');
  const quarantineEnd = appSrc.indexOf('function renderRetargetRows(');
  vm.runInContext(appSrc.slice(quarantineStart, quarantineEnd), ctx);
  const availability = appSrc.indexOf('function unavailableRetargetResult(');
  vm.runInContext(appSrc.slice(availability, appSrc.indexOf('function retargetAcceptance(', availability)), ctx);
  const playStart = appSrc.indexOf('async function playRetarget(');
  const playEnd = appSrc.indexOf('window.playRetarget = playRetarget;');
  vm.runInContext(appSrc.slice(playStart, playEnd), ctx);
  await ctx.playRetarget({ id: 'rt_job_1', motion_id: 'ma_' + '9'.repeat(32), robot: 'unitree_g1_23dof' });
  assert.equal(setSceneCalls.length, 1, 'playRetarget fetches scene objects for ma_* motion when clip.scene.objects is absent');
  assert.equal(setSceneCalls[0][0].id, 'object1-box');
}

(async () => {
  verifyMarkupAndStyles();
  verifyViewportLifecycle();
  await verifyMotionLoader();
  await verifyWorkspaceAndGadgetModal();
  await verifyVideoObjectResponseAndRetargetSync();
  console.log('PASS: unified 5-zone DOM, MotionLoader m*/ma_* LRU cache, rAF suspend/resume, multi-tab isolation, draft guard, TaskBar reopen, restart persistence, modality switch, object-response auto-attach, retarget preview sync, and Gadget modal security');
})().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
