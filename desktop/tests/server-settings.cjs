const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/../src/renderer/app.js', 'utf8');
const nodes = new Map();
function $(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, value: '', hidden: false, disabled: false, open: false, dataset: {}, textContent: '', handlers: {}, children: [],
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    async fire(type, event = {}) { for (const fn of this.handlers[type] || []) await fn(event); },
    showModal() { this.open = true; },
    close() { this.open = false; this.fire('close'); },
    focus() {}, replaceChildren(...children) { this.children = children; },
  });
  return nodes.get(id);
}
const dialogs = ['settings', 'server-settings', 'release-notes', 'password', 'installations'].map($);
let requests = 0, reloads = 0, savedAddress, workspaceSaves = 0;
const screens = [];
const S = { status: { serverConfigured: false, releaseNotesUnread: true, releaseNotes: {version: '0.4.16', items: ['note']} }, session: 0, user: null };
const desktop = {
  configureServer: async address => { savedAddress = address; },
  acknowledgeRelease: async () => {},
};
const cleared = [];
const context = vm.createContext({
  $, S, window: { desktop, addEventListener() {}, location: { reload() { reloads++; } },
    Workspace: { flushSave() { workspaceSaves++; } } },
  document: { body: { dataset: {} },
    querySelector: selector => dialogs.find(d => d.open &&
      !(selector.includes('[data-forced]') && d.id === 'server-settings' && d.dataset.forced)),
    querySelectorAll: selector => selector === 'dialog' ? dialogs
      : selector === 'dialog:not(#release-notes)' ? dialogs.filter(d => d.id !== 'release-notes') : [],
  },
  cloud: async () => { requests++; throw Object.assign(new Error('login required'), {status: 401}); },
  screen: name => screens.push(name), enter: async () => {},
  toLogin: () => screens.push('login'),
  clearSavedEditDrafts: () => cleared.push('edits'),
  stopPolling() {}, startPolling() {}, quiet() {}, toast() {}, h(tag, props, ...children) { return {tag, props, children}; },
  credentialRevision: 0, setTimeout: fn => fn(),
});
vm.runInContext(source.slice(source.indexOf('function showServerPreferenceError()'), source.indexOf('function toLogin()')), context);
vm.runInContext(source.slice(source.indexOf('// ---------------- Settings & updates'), source.indexOf('// ---------------- Keyboard')), context);
(async () => {
  await context.boot();
  assert.equal(requests, 0, 'First launch must not contact a default server');
  assert.equal($('server-settings').open, true);
  assert.equal($('server-cancel').hidden, true);
  let prevented = false;
  await $('server-settings').fire('cancel', {preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal($('release-notes').open, false);
  $('server-address').value = 'localhost:8765';
  await $('server-form').fire('submit', {preventDefault() {}});
  assert.equal(savedAddress, 'localhost:8765');
  assert.equal(reloads, 1, 'Changing server must discard old renderer state');
  assert.equal(S.session, 1);

  $('server-settings').close();
  S.status.serverPreferenceUnavailable = true;
  await context.boot();
  assert.equal($('server-settings').open, false, 'An unreadable preference must not reopen setup');
  assert.equal(screens.at(-1), 'offline');
  context.renderStatus(S.status);
  assert.equal($('upd-check').disabled, false, 'Server recovery must allow checking for updates');
  await $('offline-server').fire('click');
  assert.equal($('server-settings').open, true, 'The offline gate can open server settings directly');
  $('server-settings').close();
  S.status.serverPreferenceUnavailable = false;
  S.status.serverConfigured = true; S.status.server = 'https://example.com';
  await context.boot();
  assert.equal($('server-settings').open, false, 'An upgraded client should reuse its existing origin');
  assert.equal(requests, 1);
  S.status.releaseNotesUnread = false;
  $('server-settings').close();
  await $('open-settings').fire('click');
  for (const action of ['click', 'cancel']) {
    await $('set-release-notes').fire('click');
    assert.equal($('settings').open, true, 'Settings stays beneath release notes');
    assert.equal($('release-notes').open, true);
    if (action === 'click') await $('release-dismiss').fire('click');
    else {
      await $('release-notes').fire('cancel', {preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal($('release-notes').open, false);
    assert.equal($('settings').open, true, 'Dismiss and Escape both return to settings');
  }
  context.openServerSettings();
  assert.equal($('server-address').value, 'https://example.com');
  assert.equal($('server-cancel').hidden, false);
  desktop.configureServer = async () => { throw '地址格式无效'; };
  const sessionBeforeRejection = S.session;
  await $('server-form').fire('submit', {preventDefault() {}});
  assert.equal(reloads, 1, 'Rejected configuration must not reload or replace connection');
  assert.equal(S.session, sessionBeforeRejection, 'Rejected configuration keeps requests in the current session');
  assert.match($('server-error').textContent, /地址格式无效/);
  assert.equal($('server-save').disabled, false);

  S.user = {username: 'signed-in-account'};
  desktop.configureServer = async address => {
    assert.equal(workspaceSaves, 1, 'The old workspace is saved before changing the native connection');
    savedAddress = address;
  };
  for (const phase of ['checking', 'downloading', 'downloaded', 'installing', 'error']) {
    context.renderStatus({...S.status, updatePhase: phase});
    assert.equal($('set-server').disabled, phase === 'installing', `Server settings in ${phase}`);
    assert.equal($('offline-server').disabled, phase === 'installing', `Offline recovery in ${phase}`);
    assert.equal($('upd-install').hidden, phase !== 'downloaded');
  }
  context.renderStatus({...S.status, updatePhase: 'downloaded'});
  $('server-address').value = 'https://alternate.example';
  await $('server-form').fire('submit', {preventDefault() {}});
  assert.equal(savedAddress, 'https://alternate.example');
  assert.equal(reloads, 2);
  let workspaceSavesWhenInstalling;
  desktop.installUpdate = async () => { workspaceSavesWhenInstalling = workspaceSaves; };
  await $('upd-install').fire('click');
  assert.equal(workspaceSavesWhenInstalling, 2, 'The current workspace is saved before update restart');

  const clearEvent = {currentTarget: $('set-clear')};
  desktop.clearSession = async () => { throw new Error('Native confirmation failed'); };
  await $('set-clear').fire('click', clearEvent);
  assert.equal($('settings').open, true);
  assert.match($('set-clear-error').textContent, /无法清除/);
  assert.deepEqual(cleared, []);
  desktop.clearSession = async () => ({cleared: true});
  context.window.clearSavedWorkspace = () => { throw new Error('Storage denied'); };
  context.window.clearGadgetSavedPaths = () => cleared.push('paths');
  await $('set-clear').fire('click', clearEvent);
  assert.deepEqual(cleared, ['paths', 'edits']);
  assert.equal(screens.at(-1), 'login');
  assert.match($('login-error').textContent, /部分本机/);
  S.user = null;

  $('server-settings').close();
  $('settings').close();
  S.status.platform = 'darwin';
  context.renderStatus(S.status);
  assert.equal($('set-installations-row').hidden, false);
  const runningCopy = {path: '/Applications/Kinora.app', version: '0.10.1', runningCopy: true};
  const oldCopy = {path: '/Users/test/Downloads/Kinora.app', version: '0.4.8', runningCopy: false};
  let copies = [runningCopy, oldCopy], revealed;
  desktop.listInstallations = async () => copies;
  desktop.revealInstallation = async path => { revealed = path; };
  await context.checkInstallations();
  assert.equal($('installations').open, true, 'Duplicate installation opens guidance');
  assert.equal($('installations-list').children[0].children[0].children[0].props.text, 'v0.10.1 · 当前运行');
  await $('installations-list').children[1].children[1].props.onclick();
  assert.equal(revealed, oldCopy.path, 'Finder receives the user-selected copy');
  $('installations').close();
  await context.checkInstallations();
  assert.equal($('installations').open, false, 'Closing guidance allows continued use with the same copies');
  copies = [runningCopy];
  await $('set-installations').fire('click');
  await context.checkInstallations();
  assert.equal($('installations-status').textContent, '只找到一份 Kinora。');

  const calls = [];
  let onStatus;
  const native = { core: { invoke: async (name, args) => {
    calls.push({name, args});
    if (name === 'open_workbench') throw '请先配置服务器地址';
    if (name === 'clear_session') throw '无法显示确认窗口';
    return name === 'status' ? {server: 'https://first.example'} : {ok: true};
  } }, event: {listen: async (_, callback) => { onStatus = callback; return () => {}; }} };
  const bridgeContext = vm.createContext({window: {__TAURI__: native}});
  vm.runInContext(fs.readFileSync(__dirname + '/../src/renderer/bridge.js', 'utf8'), bridgeContext);
  const bridge = bridgeContext.window.desktop;
  await bridge.status();
  bridge.onStatus(() => {});
  onStatus({payload: {server: 'https://second.example'}});
  await bridge.cloud({path: '/api/auth/login', server: 'https://untrusted.example'});
  await bridge.saveCredentials({username: 'synthetic', password: 'synthetic'});
  await bridge.download({path: '/api/retarget/download'});
  await assert.rejects(bridge.openWorkbench(), error => error === '请先配置服务器地址');
  await assert.rejects(bridge.clearSession(), error => error === '无法显示确认窗口');
  assert.equal(calls.find(c => c.name === 'cloud').args.request.server, 'https://first.example');
  assert.equal(calls.find(c => c.name === 'save_credentials').args.server, 'https://first.example');
  assert.equal(calls.find(c => c.name === 'download').args.request.server, 'https://first.example');
  console.log('PASS: first launch offline gate, origin change reset, validation failure, settings/release dialog return');
  console.log('PASS: requests and credentials remain bound to the renderer server until reload');
})().catch(error => { console.error(error); process.exitCode = 1; });
