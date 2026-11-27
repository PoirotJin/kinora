const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeElement(id) {
  return {
    id,
    value: '',
    textContent: '',
    hidden: false,
    disabled: false,
    open: true,
    children: [],
    attrs: {},
    style: {},
    classList: {
      classes: new Set(),
      toggle(name, flag) {
        if (flag) this.classes.add(name);
        else this.classes.delete(name);
      }
    },
    get valueAsNumber() {
      return this.value === '' ? Number.NaN : Number(this.value);
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren(...items) {
      this.children = items;
      if (['re-versions', 're-joint', 're-body'].includes(this.id) && items.length && !items.some(i => i.value === this.value)) {
        this.value = items[0].value ?? '';
      }
    },
    append(...items) { this.children.push(...items); },
    scrollIntoView() {},
    showModal() { this.open = true; },
    close() { this.open = false; }
  };
}

(async () => {
  const nodes = new Map();
  const getEl = id => {
    if (!nodes.has(id)) nodes.set(id, makeElement(id));
    return nodes.get(id);
  };
  const storage = new Map();
  const pendingStore = new Map();
  const failedId = 'f'.repeat(32);
  let sessionState = {
    id: 'e'.repeat(32),
    head: 'original',
    accepted: null,
    accepted_exportable: true,
    can_undo: false,
    can_redo: false,
    repair_available: false,
    versions: []
  };
  const previewPayload = version => ({
    version,
    frames: 30,
    fps: 30,
    qpos: Array.from({ length: 30 }, () => [0, 0, 0, 0]),
    joint_controls: [{ name: 'hip', index: 3, type: 'radian', min: -1, max: 1, locked: false }],
    names: ['pelvis'],
    quality: { export_allowed: true, checks: { dynamics: true }, events: [] }
  });
  let editRequestId = null;

  const S = { session: 1, user: { username: 'alice' } };
  const api = async (url, opts = {}) => {
    if (url === '/api/harness/robot-edits') {
      return sessionState;
    }
    if (url === `/api/harness/robot-edits/${sessionState.id}/model`) {
      return { bodies: [{ name: 'pelvis', parent: 0, joints: [{ name: 'hip' }] }] };
    }
    if (url.startsWith(`/api/harness/robot-edits/${sessionState.id}/preview?version=`)) {
      const version = url.split('version=')[1];
      return previewPayload(version);
    }
    if (url === `/api/harness/robot-edits/${sessionState.id}`) {
      return sessionState;
    }
    if (url === `/api/harness/robot-edits/${sessionState.id}/edit`) {
      const body = JSON.parse(opts.body);
      editRequestId = body.request_id;
      sessionState = {
        ...sessionState,
        head: failedId,
        versions: [{ id: failedId, parent: 'original', status: 'running', request_id: editRequestId }]
      };
      return sessionState;
    }
    throw new Error('Unexpected API call: ' + url);
  };

  class FakeRobotEditView {
    constructor() { this.rig = { bodies: [{ name: 'pelvis', parent: 0, joints: [{ name: 'hip' }] }] }; }
    suspend() {}
    resume() {}
    clear() {}
    stopDrag() {}
    async setup() {}
    pose() {}
    resetCamera() {}
  }

  let scheduledPoll = null;
  const context = vm.createContext({
    state: S,
    api,
    studioLight() {},
    window: {
      desktop: true,
      addEventListener() {}
    },
    document: {
      getElementById: id => getEl(id),
      createElement: tag => makeElement(tag)
    },
    Option: function(text, value) { return { text, value }; },
    RobotEditView: FakeRobotEditView,
    RobotFK: { offsets: () => ({ joint_offsets: {}, body_offsets: {}, root_offset: [0, 0, 0] }) },
    pendingRecord: key => ({
      get: () => pendingStore.get(key) ?? null,
      set: value => {
        if (value == null) pendingStore.delete(key);
        else pendingStore.set(key, value);
      }
    }),
    toast() {},
    crypto: require('node:crypto').webcrypto,
    localStorage: {
      getItem: k => storage.get(k) ?? null,
      setItem: (k, v) => storage.set(k, v)
    },
    setTimeout: fn => { scheduledPoll = fn; return 1; },
    clearTimeout() {},
    requestAnimationFrame() { return 1; },
    cancelAnimationFrame() {},
    getComputedStyle: () => ({ getPropertyValue: () => '#fff' }),
    console
  });

  const code = fs.readFileSync(path.join(__dirname, '../src/renderer/robot-editor.js'), 'utf8');
  vm.runInContext(code, context);

  await context.window.openRobotEditor('job1');
  assert.equal(getEl('re-versions').value, 'original');
  assert.equal(getEl('re-apply').disabled, true, 'Cannot apply before saving a keyframe');

  // Save one keyframe at frame 10 in [0, 30] and submit edit
  getEl('re-timeline').value = '10';
  getEl('re-timeline').oninput();
  getEl('re-joint-value').value = '5';
  getEl('re-key').onclick();
  assert.equal(getEl('re-apply').disabled, false);

  await getEl('re-apply').onclick();
  assert.ok(editRequestId);
  assert.ok(scheduledPoll, 'Running candidate schedules poll');

  // Backend fails the candidate and rolls head back to parent ('original')
  sessionState = {
    ...sessionState,
    head: 'original',
    versions: [{
      id: failedId,
      parent: 'original',
      status: 'failed',
      error: '求解目标不可达',
      request_id: editRequestId
    }]
  };
  await scheduledPoll();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(getEl('re-notice').textContent, '求解目标不可达 当前显示上一条可用轨迹。');
  assert.equal(getEl('re-versions').value, 'original');
  assert.equal(getEl('re-apply').disabled, false, 'Rolled back to head keeps apply enabled immediately');

  // Explicitly selecting the failed candidate in #re-versions displays the failed-view hint and locks apply
  getEl('re-versions').value = failedId;
  await getEl('re-versions').onchange();
  assert.equal(getEl('re-versions').value, failedId);
  assert.equal(getEl('re-apply').disabled, true);
  assert.equal(getEl('re-foot-hint').textContent, '所选候选求解失败；切回当前有效版本后可继续修整。');

  // Switching back to the valid head unlocks apply cleanly
  getEl('re-versions').value = 'original';
  await getEl('re-versions').onchange();
  assert.equal(getEl('re-versions').value, 'original');
  assert.equal(getEl('re-apply').disabled, false);

  console.log('PASS: robot-editor surfaces failed rollback notice while staying editable and handles viewing failed versions');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
