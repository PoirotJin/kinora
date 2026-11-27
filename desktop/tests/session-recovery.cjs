const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const source = fs.readFileSync(__dirname + '/../src/renderer/app.js', 'utf8');
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));

function createRenderer(storage = new Map()) {
  const nodes = new Map();
  const requests = [];
  const errors = [];
  function $(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', textContent: '', hidden: false, disabled: false, children: [],
      setAttribute() {},
      removeAttribute(name) { delete this[name]; },
      replaceChildren(...children) { this.children = children; },
      append(...children) { this.children.push(...children); },
    });
    return nodes.get(id);
  }
  const context = vm.createContext({
    $, DOMException, TextEncoder, crypto:webcrypto,
    randomId:()=>webcrypto.randomUUID(),
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    window: { desktop: { cloud: request => new Promise(resolve => requests.push({ request, resolve })) } },
    document: { querySelectorAll: () => [] },
    IntersectionObserver: class { disconnect() {} },
    clearTimeout,
    motionView: { clear() { this.clip = null; } }, robotView: { clear() {} }, previewCache: new Map(),
    setTransport() {}, viewportMessage() {}, renderInspector(_detail, id) { $('inspector').textContent = id || ''; },
    stopPolling() {}, screen() {}, setOnline() {}, openPassword() {},
    updateChecks() {},
    renderRobots() {}, setBadge() {},
    quiet(error) { if (error.name !== 'AbortError') errors.push(error); },
    h: (tag, props) => ({ tag, ...props }), jobCard: job => job, rtCard: job => job,
    ACTIVE_CUSTOM: ['queued', 'running'],
  });
  vm.runInContext(section('const S = {', '// ---------------- Cloud bridge'), context);
  vm.runInContext(section('let passwordRotation = null;', 'const quiet ='), context);
  vm.runInContext(section('function resetSession()', 'async function enter('), context);
  vm.runInContext(section('let nextTopicsRequest', 'const thumbQueue'), context);
  vm.runInContext(section('const thumbQueue', 'function motionCard('), context);
  vm.runInContext(section('function syncGeneration(', 'async function loadModels('), context);
  vm.runInContext(section('async function selectMotion(', 'function refreshLibraryCardBadges('), context);
  vm.runInContext(section('function applyLibraryScope(', 'function setLibraryScope('), context);
  vm.runInContext(section('async function rankedSearch(', 'let searchTimer ='), context);
  vm.runInContext('let searchTimer = null, composing = false;', context);
  vm.runInContext(section('function setSeg(', "$('gen-live').addEventListener"), context);
  vm.runInContext(section('function beginJobListRequest(', 'function jobCard('), context);
  vm.runInContext(section('function updateRecentRetargetJobs(', 'function rtCard('), context);
  const S = vm.runInContext('S', context);
  S.user = { username: 'first-account' };
  return { context, S, $, requests, errors };
}

function seedPrivateState(renderer) {
  const { S, $ } = renderer;
  Object.assign(S.lib, { selected: 'm00000001', previewId: 'm00000001',
    previewRequest: { id: 'm00000001' }, items: ['private-motion'], detail: { motion_id: 'm00000001' } });
  renderer.context.motionView.clip = {};
  $('inspector').textContent = 'm00000001';
  renderer.context.applyLibraryScope('results');
  $('search-q').value = 'private search';
  $('f-topic').value = 'walk';
  $('f-status').value = 'rejected';
  $('f-sort').value = 'created_asc';
  $('f-results-sort').value = 'created_asc';
  Object.assign(S.gen, {
    submitting: true, retryParent: 'old-parent',
  });
  S.gen.results.set('private-job', ['private-result']);
  S.gen.open.add('private-job');
  $('gen-feed').replaceChildren({ id: 'private-job' });
  $('rt-jobs').replaceChildren({ id: 'private-retarget-job' });
  $('gen-note').textContent = 'previous account submission error';
  $('gen-quota').textContent = 'previous account quota';
  $('rt-error').textContent = 'previous account retarget error';
}

function assertPrivateStateCleared({ S, $ }) {
  assert.equal(S.lib.selected, null);
  assert.equal(S.lib.previewId, null);
  assert.equal(S.lib.previewRequest, null);
  assert.equal(S.lib.items.length, 0);
  assert.equal(S.lib.detail, null);
  assert.equal($('inspector').textContent, '');
  assert.equal(S.lib.scope, 'all');
  for (const id of ['search-q', 'f-topic', 'f-status']) assert.equal($(id).value, '');
  assert.equal($('f-sort').value, 'relevance');
  assert.equal($('f-results-sort').value, 'created_desc');
  assert.equal($('f-results-sort').hidden, true);
  assert.equal($('lib-filters').hidden, false);
  assert.equal(S.gen.submitting, false, 'A pending submission must not disable the next account');
  assert.equal(S.gen.retryParent, null);
  assert.equal(S.gen.results.size, 0);
  assert.equal(S.gen.open.size, 0);
  assert.equal($('gen-feed').children.length, 0, 'Previous account generation cards must be removed');
  assert.equal($('rt-jobs').children.length, 0, 'Previous account retarget cards must be removed');
  assert.equal($('gen-note').textContent, '');
  assert.equal($('gen-quota').textContent, '');
  assert.equal($('rt-error').textContent, '');
}

async function pendingRequestsAcrossAccounts() {
  const renderer = createRenderer();
  const { context, S, requests, $ } = renderer;
  seedPrivateState(renderer);
  const oldCustom = context.refreshCustom();
  const oldRetarget = context.refreshRetarget();
  assert.equal(requests.length, 2);
  assert.ok(S.gen.busy);
  assert.ok(S.rt.busy);

  context.toLogin();
  assert.equal(S.user, null);
  assert.equal(S.gen.busy, false);
  assert.equal(S.rt.busy, false);
  assertPrivateStateCleared(renderer);

  // Authentication completes while the previous account's network requests are still pending.
  S.user = { username: 'second-account' };
  const newCustom = context.refreshCustom();
  const newRetarget = context.refreshRetarget();
  assert.equal(requests.length, 4, 'The new account must be allowed to start both refreshes');
  requests[0].resolve({ ok: true, data: { jobs: [{ id: 'old-job' }] } });
  requests[1].resolve({ ok: false, status: 401, error: 'previous account expired' });
  await Promise.all([oldCustom, oldRetarget]);
  assert.equal(S.user.username, 'second-account', 'A stale 401 must not log out the new account');
  assert.ok(S.gen.busy, 'An old response must not release the new generation request');
  assert.ok(S.rt.busy, 'An old response must not release the new retarget request');
  assertPrivateStateCleared(renderer);

  requests[2].resolve({ ok: true, data: { jobs: [{ id: 'new-job' }] } });
  requests[3].resolve({ ok: true, data: { items: [{ id: 'new-retarget' }], dispatch_notice: '调度器离线' } });
  await Promise.all([newCustom, newRetarget]);
  assert.equal(S.gen.busy, false);
  assert.equal(S.rt.busy, false);
  assert.equal($('gen-feed').children[0].id, 'new-job');
  assert.equal($('rt-jobs').children[0].id, 'new-retarget');
  assert.equal($('rt-dispatch-notice').textContent, '调度器离线');
  assert.equal($('rt-dispatch-notice').hidden, false);
  assert.equal(renderer.errors.length, 0);
}

async function unauthorizedPollRecovers() {
  const renderer = createRenderer();
  const { context, S, requests, $ } = renderer;
  seedPrivateState(renderer);
  const poll = context.refreshCustom();
  requests[0].resolve({ ok: false, status: 401, error: 'expired-account-error' });
  await poll;
  assert.equal(S.user, null, 'A 401 must return the renderer to login');
  assert.equal(S.gen.busy, false, 'The request causing logout must not leave its refresh locked');
  assertPrivateStateCleared(renderer);

  S.user = { username: 'first-account' };
  const retry = context.refreshCustom();
  assert.equal(requests.length, 2, 'Logging in again must resume generation requests');
  assert.equal($('gen-quota').textContent, '', 'The expired request error must not leak into the new session');
  requests[1].resolve({ ok: true, data: { jobs: [{ id: 'after-relogin' }] } });
  await retry;
  assert.equal($('gen-feed').children[0].id, 'after-relogin');
  assert.equal(S.gen.busy, false);
  assert.equal(renderer.errors.length, 0);
}

async function restartKeepsSubmissionId() {
  const storage = new Map();
  async function send(renderer, kind, path, value, idField, response) {
    const count = renderer.requests.length;
    const result = renderer.context.submitOnce(kind,path,value,idField);
    while (renderer.requests.length === count) await new Promise(resolve => setImmediate(resolve));
    const {request,resolve} = renderer.requests[count];
    resolve(response);
    await result.catch(error=>assert.equal(error.message,response.error));
    return request.body[idField];
  }
  for (const [kind,path,value,idField] of [
    ['generate','/api/custom',{description:'A person walks.',n:1},'request_id'],
    ['retarget','/api/retarget',{motion_id:'m00000001',parameters:{smooth_weight:2}},'id'],
    ['asset-snapshot','/api/harness/personal-assets/snapshots',
      {collection_id:'test', name:'Snapshot', expected_basis:'membership'},'id'],
  ]) {
    const timeout = {ok:false,status:408,error:'request timed out'};
    const ok = {ok:true,data:{}};
    const first=createRenderer(storage);first.S.status={server:'https://original.example'};
    const id=await send(first,kind,path,value,idField,timeout);
    const other=createRenderer(storage);other.S.status={server:'https://other.example'};
    assert.notEqual(await send(other,kind,path,value,idField,ok),id);
    other.S.status=first.S.status;other.S.user={username:'another-account'};
    assert.notEqual(await send(other,kind,path,value,idField,ok),id);
    const restarted=createRenderer(storage);restarted.S.status=first.S.status;
    assert.equal(await send(restarted,kind,path,value,idField,ok),id);
    assert.notEqual(await send(restarted,kind,path,value,idField,ok),id);
    assert.equal(storage.size,0);
  }
}

async function lostSubmissionResponse() {
  for (const [path, idField, status] of [
    ['/api/custom', 'request_id', 0],
    ['/api/retarget', 'id', 0],
    ['/api/harness/multi-scene', 'request_id', 0],
    ['/api/harness/multi-scene', 'request_id', 503],
  ]) {
    const storage = new Map();
    const renderer = createRenderer(storage);
    const {context, requests} = renderer;
    const submitted = context.submitOnce('test', path, {description:'A person walks.'}, idField);
    while (requests.length < 1) await new Promise(resolve => setImmediate(resolve));
    const original = requests[0].request;
    // The server accepted the first submission, but its response was lost.
    requests[0].resolve({ok:false,status,error:'云端响应超时，请稍后重试'});
    while (requests.length < 2) await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(requests[1].request, original, 'Automatic replay must use identical inputs and ID');
    requests[1].resolve({ok:true,data:{request_id:original.body[idField]}});
    await submitted;
    assert.equal(requests.length, 2);
    assert.equal(storage.size, 0);

    const failed = context.submitOnce('test', path, {description:'A person walks.'}, idField);
    while (requests.length < 3) await new Promise(resolve => setImmediate(resolve));
    requests[2].resolve({ok:false,status:0,error:'offline'});
    while (requests.length < 4) await new Promise(resolve => setImmediate(resolve));
    requests[3].resolve({ok:false,status:0,error:'offline'});
    await assert.rejects(failed, /offline/);
    assert.equal(requests.length, 4, 'Only one automatic replay is allowed');
    assert.equal(storage.size, 1, 'Keep the ID for manual retry after two failures');
  }
  const renderer = createRenderer();
  const submitted = renderer.context.submitOnce('test','/api/custom',{},'request_id');
  while (!renderer.requests.length) await new Promise(resolve => setImmediate(resolve));
  renderer.context.toLogin();
  renderer.requests[0].resolve({ok:false,status:0,error:'offline'});
  await assert.rejects(submitted, error => error.name === 'AbortError');
  assert.equal(renderer.requests.length, 1, 'Never replay after an account change');
}

async function rankedFallbackIsolation() {
  const { context, S, requests, $ } = createRenderer();
  S.lib.version = 4;
  const stale = context.rankedSearch({ q: 'old' }, 4);
  S.lib.version++;
  requests[0].resolve({ ok: false, status: 404, error: 'not found' });
  assert.equal(await stale, null);
  assert.equal(requests.length, 1);
  assert.equal($('f-sort').disabled, false);

  const current = context.rankedSearch({ q: 'current', after: 0 }, 5);
  requests[1].resolve({ ok: false, status: 404, error: 'not found' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests[2].request.path, '/api/library/search');
  requests[2].resolve({ ok: true, data: { items: ['legacy item'], next_after: null } });
  assert.equal((await current).items[0], 'legacy item');
  assert.equal($('f-sort').disabled, true);

  const repeated = context.legacySearch({ q: 'current', after: 7 }, 5);
  requests[3].resolve({ ok: true, data: { items: ['last item'], next_after: '7' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 4, 'Legacy search stops before reusing a cursor');
  const page = await repeated;
  assert.equal(page.next_after, null);
  assert.equal(page.paginationStalled, true);

  context.toLogin();
  assert.equal(S.lib.legacy, false);
  assert.equal($('f-sort').disabled, false);
  assert.equal($('f-sort').title, '');
}

async function unauthorizedDownloadRecoversWithoutLoggingOutNewAccount() {
  const renderer = createRenderer();
  const {context,S} = renderer;
  const downloads = [];
  const messages = [];
  context.toast = message => messages.push(message);
  context.window.desktop.download = () => new Promise(resolve => downloads.push(resolve));
  vm.runInContext(section('window.downloadMotion = async function(', "$('motion-download').addEventListener"),context);

  seedPrivateState(renderer);
  const expired = context.window.downloadMotion('m00000001');
  downloads.shift()({status:401,error:'请登录后继续'});
  await expired;
  assert.equal(S.user,null);
  assertPrivateStateCleared(renderer);
  assert.deepEqual(messages,[]);

  S.user = {username:'first-account'};
  const stale = context.window.downloadMotion('m00000001');
  context.toLogin();
  S.user = {username:'second-account'};
  downloads.shift()({status:401,error:'请登录后继续'});
  await stale;
  assert.equal(S.user.username,'second-account');
  assert.deepEqual(messages,[]);
}

async function staleAuthenticationAfterPasswordChangePreservesWorkspace() {
  const renderer = createRenderer();
  const {context,S,requests} = renderer;
  seedPrivateState(renderer);
  const pending = context.cloud('/api/auth/me');
  requests[0].resolve({ok:false,stale_auth:true,error:'会话已切换，请重试'});
  await assert.rejects(pending, error => error.name === 'AbortError');
  context.window.desktop.download = async () => ({ok:false,stale_auth:true});
  await assert.rejects(context.downloadFile({path:'/api/harness/export'}), error => error.name === 'AbortError');
  assert.equal(S.user.username,'first-account');
  assert.equal(S.lib.selected,'m00000001');
}

async function unauthorizedDuringPasswordChangeWaitsForTheOutcome() {
  for (const succeeds of [true,false]) {
    const renderer = createRenderer();
    const {context,S,requests} = renderer;
    seedPrivateState(renderer);
    const old = context.cloud('/api/auth/me').catch(error => error);
    const rotation = context.cloud('/api/auth/password',{method:'POST',body:{}}).catch(error => error);
    requests[0].resolve({ok:false,status:401,error:'旧会话已失效'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(S.user?.username,'first-account','A pending password change has not resolved the 401 yet');
    requests[1].resolve(succeeds ? {ok:true,data:{user:{username:'first-account'}}}
                                  : {ok:false,status:400,error:'修改密码未完成'});
    await rotation;
    assert.equal((await old).name,'AbortError');
    if (succeeds) assert.equal(S.lib.selected,'m00000001');
    else assertPrivateStateCleared(renderer);
  }
}

async function rankedFallbackKeepsPagingMode() {
  const { context, S, requests } = createRenderer();
  const keyword = { used: false, reason: 'offline' };
  const semantic = { used: true };
  async function page(after, result, keywordOnly = false) {
    const loading = context.rankedSearch({ q: 'turning', after }, S.lib.version, undefined, keywordOnly);
    const request = requests.at(-1);
    request.resolve({ ok: true, data: { items: [], semantic: result } });
    return { query: request.request.query, result: await loading };
  }

  assert.equal((await page(0, keyword)).query.keyword_only, undefined);
  assert.equal((await page(1, { used: false })).query.keyword_only, 1);
  assert.equal((await page(0, semantic)).query.keyword_only, undefined, 'A fresh search retries semantic matching');
  const outage = await page(1, keyword);
  assert.equal(outage.result.restartInRankedMode, true);
  assert.equal(outage.result.keywordOnly, true);
  assert.equal((await page(0, { used: false }, true)).query.keyword_only, 1);
  const olderServer = await page(1, semantic);
  assert.equal(olderServer.result.restartInRankedMode, true, 'A server that ignores the flag must restart the page stream');
  assert.equal(olderServer.result.keywordOnly, false);
}

async function pendingPreviewKeepsRefreshedReview() {
  const id = 'm00000001';
  for (const retry of [false, true]) {
    const { context, S } = createRenderer();
    const oldDetail = {motion_id: id, review: {status: 'pending'}, quality_status: 'not_checked',
      tags: [{tag: 'outdated'}], storage_tier: 'cold'};
    if (retry) Object.assign(S.lib, {selected: id, detail: oldDetail});
    let completeDetail;
    context.MotionLoader = {
      has: () => false,
      fetchDetail: () => new Promise(resolve => { completeDetail = resolve; }),
    };
    const clip = {meta: {frames: 60}};
    context.motionView.setClip = value => { context.motionView.clip = value; };
    context.motionView.setPlaying = () => {};
    const opening = context.selectMotion(id);
    context.refreshOpenPreviewDetails([{motion_id: id, review: {status: 'accepted'}, tags: [], quality_status: 'pass'}]);
    completeDetail({detail: oldDetail, preview: clip});
    await opening;

    assert.equal(S.lib.detail.review.status, 'accepted');
    assert.equal(S.lib.detail.quality_status, 'pass');
    assert.equal(S.lib.detail.tags.length, 0);
    assert.equal(S.lib.detail.storage_tier, 'cold');
    assert.equal(context.motionView.clip, clip);
  }
}

async function overlappingTopicRefreshPreservesTheSelectedFilter() {
  const { context, S, requests, $ } = createRenderer();
  context.document.body = { dataset: { screen: 'ready' } };
  const selectedDuringReload = [];
  context.searchLibrary = () => selectedDuringReload.push($('f-topic').value);
  const walking = { id: 'walk', name: '行走' };
  const turn = { id: 'turn', name: '转身' };
  context.applyTopics([walking, turn]);
  $('f-topic').value = 'turn';

  const stale = context.loadTopics();
  const current = context.loadTopics();
  requests[0].resolve({ ok: true, data: { topics: [walking], generation: { duration_max_seconds: 10 } } });
  await stale;
  requests[1].resolve({ ok: true, data: { topics: [walking, turn], generation: { duration_max_seconds: 120 } } });
  await current;
  assert.equal($('f-topic').value, 'turn', 'An older response cannot clear a filter that is present in the current refresh');
  assert.deepEqual(selectedDuringReload, []);
  assert.equal(S.gen.durationMax, 120);

  const removed = context.loadTopics();
  requests[2].resolve({ ok: true, data: { topics: [walking] } });
  await removed;
  assert.equal($('f-topic').value, '');
  assert.deepEqual(selectedDuringReload, ['']);
}

async function snapshotListTracksTerminalStatusFromDetail() {
  const assets = fs.readFileSync(__dirname + '/../src/renderer/personal-assets.js', 'utf8');
  const definition = assets.slice(assets.indexOf('  async function showSnapshotJob('),
                                  assets.indexOf('  function showSnapshot(snapshot)'));
  for (const [status, delayedJobs, newlySubmitted] of [['failed', true], ['failed', false], ['done', false], ['failed', false, true]]) {
    const queued = { id: '1'.repeat(32), name: 'Waiting snapshot', status: 'queued' };
    const snapshot = { id: queued.id, name: queued.name, created: 1, collection_name: 'Test', items: ['motion'] };
    const items = newlySubmitted ? [] : [queued];
    let timer;
    let resolveList;
    const context = vm.createContext({
      items, detailSerial: 1, detailId: '', jobPoll: null, hasRequestedMore: false, pageLoad: null,
      group: 'snapshots', after: null, session: 1, nav: 0, retryFirstPage: false, collectionId: '',
      pageCursors: new Set(), pageKey: () => 'snapshots', assetId: row => row.id,
      renderList() {}, showSnapshot() {}, load: async () => {}, note() {},
      details: { replaceChildren() {} },
      h: () => ({ setAttribute() {} }), button: () => ({}),
      setTimeout(fn) { timer = fn; return 1; }, clearTimeout() {},
      request: async endpoint => {
        if (endpoint === 'snapshots') return new Promise(resolve => { resolveList = resolve; });
        return endpoint.startsWith('snapshot-jobs/')
          ? { ...queued, status, error: 'Fixture could not read an asset' } : snapshot;
      },
    });
    vm.runInContext(definition, context);
    let staleList;
    if (status === 'failed') {
      vm.runInContext(assets.slice(assets.indexOf('  async function load('),
                                   assets.indexOf('  function navigate(')), context);
      staleList = context.load();
    }
    await context.showSnapshotJob(queued);
    assert.deepEqual(context.items, [queued], 'A newly submitted job is selectable while the first list request is pending');
    if (newlySubmitted) {
      resolveList({jobs: [], items: []});
      await staleList;
      assert.equal(context.items[0], queued, 'A delayed list response must keep the new job visible');
      await timer();
      assert.equal(context.items[0].status, 'failed');
      continue;
    }
    await timer();
    assert.equal(items.length, 1);
    if (status === 'failed') {
      assert.equal(items[0].status, 'failed');
      assert.equal(items[0].error, 'Fixture could not read an asset');
      resolveList({ jobs: delayedJobs ? [queued] : [], items: [] });
      await staleList;
      assert.equal(context.items.length, 1);
      assert.equal(context.items[0].status, 'failed', 'A delayed list must keep the observed failure');
    } else {
      assert.equal(items[0].status, undefined);
      assert.equal(items[0].count, 1);
    }
  }
}

async function snapshotPollingResumesAfterRepeatedOutages() {
  const assets = fs.readFileSync(__dirname + '/../src/renderer/personal-assets.js', 'utf8');
  const definition = assets.slice(assets.indexOf('  async function showSnapshotJob('),
                                  assets.indexOf('  function showSnapshot(snapshot)'));
  const queued = { id: '1'.repeat(32), name: 'Waiting snapshot', status: 'queued' };
  const snapshot = { id: queued.id, name: queued.name, items: [] };
  for (const manualRetry of [false, true]) {
    const statuses = [503, 503, ...(manualRetry ? [] : [429]), 200];
    const timers = new Map();
    const delays = [];
    let shown, retry;
    const context = vm.createContext({
      items: [queued], detailSerial: 1, detailId: '', jobPoll: null, hasRequestedMore: false, pageLoad: null,
      renderList() {}, showSnapshot(value) { shown = value; }, load: async () => {},
      details: { replaceChildren() {} }, h: () => ({setAttribute() {}}),
      button: (_, click) => { retry = {click}; return retry; },
      setTimeout(fn, delay) {
        delays.push(delay);
        timers.set(delays.length, fn);
        return delays.length;
      },
      clearTimeout(id) { timers.delete(id); },
      request: async endpoint => {
        if (endpoint.startsWith('snapshots/')) return snapshot;
        const status = statuses.shift();
        if (status !== 200) throw Object.assign(new Error('Temporarily unavailable'), {status});
        return {...queued, status: 'done'};
      },
    });
    vm.runInContext(definition, context);
    await context.showSnapshotJob(queued);
    for (let attempt = 0; attempt < (manualRetry ? 2 : 4); attempt++) {
      assert.equal(timers.size, 1, 'A queued snapshot must keep a single poll scheduled during an outage');
      const [[id, poll]] = timers;
      timers.delete(id);
      await poll();
    }
    if (manualRetry) {
      assert.equal(retry.hidden, false);
      await Promise.all([retry.click(), retry.click()]);
    }
    assert.deepEqual(delays, manualRetry ? [2000, 2000, 4000] : [2000, 2000, 4000, 8000]);
    assert.equal(shown, snapshot);
    assert.equal(timers.size, 0);
  }
}

async function retargetSourceIsolation() {
  const nodes = new Map(), loads = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', hidden:false}); return nodes.get(id); };
  const S = { session:1, rt:{playing:'old-job',playingJob:{id:'old-job',motion_id:'old'},viewMode:'compare'}, lib:{items:[]} };
  const robotView = {clip:{},clear(){this.clip=null;}}, rtHumanView = {clip:{},clear(){this.clip=null;},setClip(c){this.clip=c;}};
  const context = vm.createContext({$,S,robotView,rtHumanView,window:{Workspace:{}},
    MotionLoader:{isValidMotionId:()=>true,getCachedMeta:()=>null,isManagedAsset:()=>false},
    viewportMessage(){},updateRetargetTransport(){},
    fullPreview:id=>new Promise(resolve=>loads.push({id,resolve}))});
  vm.runInContext(section("let rtHumanLoadedId =", 'function renderRobots('), context);
  vm.runInContext(section('function syncRetargetSource()', 'window.syncRetargetSource ='), context);
  const old = context.ensureRetargetHumanLoaded('old');
  $('rt-motion').value='new';context.syncRetargetSource();
  assert.equal(S.rt.playing,null);assert.equal(S.rt.playingJob,null);
  assert.equal(robotView.clip,null);assert.equal($('rt-acceptance-bar').hidden,true);
  assert.equal(loads[1].id,'new');loads[1].resolve({id:'new'});
  await new Promise(resolve=>setImmediate(resolve));
  loads[0].resolve({id:'old'});await old;
  assert.equal(rtHumanView.clip.id,'new','A late prior-source response cannot replace the new source');
  S.rt.playing='new-job';S.rt.playingJob={id:'new-job',motion_id:'new'};
  robotView.clip={id:'matching'};context.syncRetargetSource();
  assert.equal(robotView.clip.id,'matching','An unchanged source keeps its selected result');
}

async function retargetCancellationReportsTheServerOutcome() {
  const notifications = [];
  const refreshes = [];
  let status;
  const S = { user: {username: 'tester'}, rt: {}, taskCancellations: new Map() };
  const context = vm.createContext({
    S, window: {}, document: {querySelectorAll: () => []},
    cloud: async (path, request) => {
      assert.equal(path, '/api/retarget/cancel');
      assert.equal(request.method, 'POST');
      assert.equal(request.body.id, status);
      return {status};
    },
    refreshRetarget: async () => refreshes.push(status),
    toast: message => notifications.push(message),
    quiet: error => { throw error; }
  });
  vm.runInContext(section('function refreshAfterCancellation(', 'function cancelCustom('), context);
  vm.runInContext(section('function cancelRetarget(', '// ---------------- Polling'), context);
  for (status of ['done', 'failed', 'cancelled', 'stopping']) await context.cancelRetarget(status);
  assert.deepEqual(notifications, ['重定向已完成，无法取消', '重定向已结束，无法取消', '重定向已取消', '正在取消重定向']);
  assert.deepEqual(refreshes, ['done', 'failed', 'cancelled', 'stopping']);
}

(async () => {
  await retargetSourceIsolation();
  await retargetCancellationReportsTheServerOutcome();
  await rankedFallbackIsolation();
  await rankedFallbackKeepsPagingMode();
  await pendingPreviewKeepsRefreshedReview();
  await overlappingTopicRefreshPreservesTheSelectedFilter();
  await snapshotListTracksTerminalStatusFromDetail();
  await snapshotPollingResumesAfterRepeatedOutages();
  await pendingRequestsAcrossAccounts();
  await unauthorizedPollRecovers();
  await unauthorizedDownloadRecoversWithoutLoggingOutNewAccount();
  await staleAuthenticationAfterPasswordChangePreservesWorkspace();
  await unauthorizedDuringPasswordChangeWaitsForTheOutcome();
  await restartKeepsSubmissionId();
  await lostSubmissionResponse();
  console.log('PASS: pending polls survive account changes, stale responses cannot unlock new requests, and 401 recovery clears private state');
})().catch(error => { console.error(error); process.exitCode = 1; });
