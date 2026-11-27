const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeNode(id) {
  return {
    id,
    value: id === 'downloads-format' ? 'npz' : '',
    textContent: '',
    hidden: false,
    disabled: false,
    open: false,
    children: [],
    attrs: {},
    listeners: new Map(),
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    addEventListener(type, fn) { this.listeners.set(type, fn); },
    replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); },
    showModal() { this.open = true; },
    close() {
      if (!this.open) return;
      this.open = false;
      this.listeners.get('close')?.();
    },
    focus() {}
  };
}

function testDownloadsAcceptsButtonAndOptions() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  const S = { session: 1 };
  const cached = new Map([
    ['m1', { status: 'quarantined', label: '缓存动作 1' }],
    ['m2', { status: 'ready', label: '缓存动作 2' }]
  ]);
  const windowObj = {
    previewExportedMotion() {}
  };
  const context = vm.createContext({
    $,
    S,
    window: windowObj,
    h: (tag, props = {}, ...children) => ({ tag, ...props, children: children.flat().filter(Boolean) }),
    cloud: async () => ({ items: [] }),
    MotionLoader: {
      getCachedMeta: id => cached.get(id) || null,
      isManagedAsset: id => id.startsWith('ma_')
    },
    downloadFile: async () => ({ saved: '/tmp/out.npz' }),
    selectMotion() {},
    toast() {},
    MutationObserver: class { observe() {} },
    document: { body: {} }
  });
  const code = fs.readFileSync(path.join(__dirname, '../src/renderer/downloads.js'), 'utf8');
  vm.runInContext(code, context);

  let focused = 0;
  const btnEl = {
    tagName: 'BUTTON',
    disabled: false,
    isConnected: true,
    focus() { focused += 1; }
  };

  windowObj.downloadMotion('m1', btnEl);
  assert.equal($('downloads-dialog').open, true);
  assert.equal($('downloads-count').textContent, '已选 1 / 20');
  assert.match(JSON.stringify($('downloads-list').children), /未通过 · 隔离候选/);
  $('downloads-dialog').close();
  assert.equal(focused, 1, 'Closing downloads dialog restores focus to trigger button');

  windowObj.downloadMotion('m2', { resultStatus: 'ready' });
  assert.equal($('downloads-dialog').open, true);
  assert.equal($('downloads-count').textContent, '已选 1 / 20');
  assert.match(JSON.stringify($('downloads-list').children), /缓存动作 2/);
  $('downloads-dialog').close();

  const disabledBtn = { tagName: 'BUTTON', disabled: true };
  windowObj.downloadMotion('m1', disabledBtn);
  assert.equal($('downloads-dialog').open, false, 'Disabled trigger button does not reopen dialog');
}

async function testCustomRobotRetargetVersion() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  const submissions = [];
  const versionHex = 'a'.repeat(64);
  const S = {
    session: 1,
    user: { username: 'alice' },
    robots: [
      { id: 'robot_' + '1'.repeat(32), label: 'Custom V', version: versionHex },
      { id: 'robot_' + '2'.repeat(32), label: 'Custom Unset' },
      { id: 'unitree_g1_23dof', label: 'Humanoid', version: 'ignored' }
    ],
    rt: {
      tabKey: 'default',
      robot: 'robot_' + '1'.repeat(32),
      submittingTabs: new Set(),
      submissionErrors: new Map()
    }
  };
  const appSource = fs.readFileSync(path.join(__dirname, '../src/renderer/app.js'), 'utf8');
  const start = appSource.indexOf('function syncRetargetSubmission()');
  const end = appSource.indexOf("$('rt-live').addEventListener");
  const context = vm.createContext({
    $,
    S,
    window: {},
    MotionLoader: { getCachedMeta: () => null },
    syncRetargetSource() {},
    syncRetargetSubmission: () => false,
    submitOnce: async (_key, _path, body) => { submissions.push(body); return { id: 'job1' }; },
    jobListAfterSubmit: () => async () => {},
    refreshRetarget: async () => {},
    toast() {}
  });
  vm.runInContext(appSource.slice(start, end), context);

  const form = $('rt-form').listeners.get('submit');
  $('rt-motion').value = 'm00000001';
  await form({ preventDefault() {} });
  assert.equal(submissions[0].robot_version, versionHex);

  S.rt.robot = 'robot_' + '2'.repeat(32);
  await form({ preventDefault() {} });
  assert.equal(Object.hasOwn(submissions[1], 'robot_version'), false);

  S.rt.robot = 'unitree_g1_23dof';
  await form({ preventDefault() {} });
  assert.equal(Object.hasOwn(submissions[2], 'robot_version'), false);
}

(async () => {
  testDownloadsAcceptsButtonAndOptions();
  await testCustomRobotRetargetVersion();
  console.log('PASS: downloadMotion accepts button or options and retarget attaches custom robot_version');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
