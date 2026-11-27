'use strict';
const $ = id => document.getElementById(id);
const h = (tag, props = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'copy') continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style') el.style.cssText = v; // CSSOM: allowed by the page CSP
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : String(c));
  if (props.copy) makeCopyable(el, props.copy);
  return el;
};
const randomId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
const fmtTime = s => (Math.max(0, s) || 0).toFixed(2);
const fmtDate = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';

const REVIEW = { accepted: ['已通过', 'ok'], rejected: ['未通过', 'bad'], quarantined: ['未通过 · 隔离候选', 'bad'], uncertain: ['不确定', 'warn'], pending: ['待审核', ''] };
const QUALITY = { not_checked: ['质量未检查', ''], screened: ['已筛查', 'info'], incomplete: ['检查不完整', 'warn'], issues: ['有疑点', 'warn'], failed: ['检查失败', 'bad'] };
const CUSTOM = { queued: ['等待文本转换', ''], preparing: ['转换提示词', 'info'], ready: ['等待 GPU', 'info'], running: ['生成中', 'accent'], completed: ['已完成', 'ok'], failed: ['失败', 'bad'], cancelled: ['已停止', ''] };
const RETARGET = {
  queued: ['排队中', ''], running: ['运行中', 'accent'], stopping: ['正在取消', 'warn'], done: ['已完成', 'ok'],
  unverified: ['待重新验收', 'warn'], unconfirmed: ['验收状态未知', 'warn'],
  source_quarantined: ['源动作已隔离', 'bad'], source_removed: ['源动作已移入回收站', 'bad'],
  result_missing: ['重定向结果文件缺失', 'warn'],
  result_corrupt: ['重定向结果完整性校验失败', 'bad'],
  failed: ['失败', 'bad'], repair: ['待修复', 'warn'], cancelled: ['已取消', '']
};
const RETARGET_SOURCE_QUARANTINE_REASON = '源动作已标记为隔离候选，无法正式导出此轨迹。';
const FLAG_LABEL = {
  possible_direction_mismatch: '方向疑似与描述相反', possible_path_reversal: '路线折返', possible_in_place_travel: '要求原地但发生位移', possible_narrow_stance: '步宽过窄', possible_crossed_feet: '双脚交叉',
  possible_stance_sliding: '支撑脚滑动', possible_foot_slide: '脚底滑动', possible_self_penetration: '肢体穿插',
  possible_root_gait_mismatch: '步态与位移不符', possible_duplicate: '疑似重复', speed_outlier: '速度异常', acceleration_outlier: '加速度异常',
  possible_floor_excursion: '低于地面', possible_ground_speed_outlier: '地面动作速度异常'
};
const ACTIVE_CUSTOM = ['queued', 'preparing', 'ready', 'running'], ACTIVE_RT = ['queued', 'running', 'stopping'];
const pill = (map, key, extra) => { const [label, tone] = map[key] || [key || '未知', '']; return h('span', { class: 'pill ' + tone + (extra ? ' ' + extra : ''), text: label }); };

const S = {
  user: null, session: 0, view: 'library', topics: new Map(), robots: [], online: true, status: null, capabilities: null,
  taskCancellations: new Map(),
  lib: { version: 0, items: [], after: null, pageQueryKey: null, pageCursors: new Set(), busy: false, selected: null, detail: null,
    previewSeq: 0, selectionSeq: 0, rankedMode: null,
    previewId: null, previewRequest: null, scanned: 0, scope: 'all', harnessResults: [] },
  gen: { durationMax: null, history: false, filterRevision: 0, after: null, retryParent: null, open: new Set(), results: new Map(), resultData: new Map(), pendingAttach: new Map(), jobs: [], busy: false, submitting: false, tabKey: 'default' },
  rt: { history: false, filterRevision: 0, after: null, busy: false, playing: null, playingJob: null, previewRequest: 0, retryPreview: null, robot: '', items: [], downloads: new Set(), quarantinedSources: new Set(), submittingTabs: new Set(), submissionErrors: new Map(), viewMode: 'robot', tabKey: 'default', sourceSnapshot: null }
};

function pendingRecord(kind) {
  const key = pendingStorageKey(kind);
  return {
    get() {
      const raw = localStorage.getItem(key);
      if (raw === 'undefined') { localStorage.removeItem(key); return null; }
      return JSON.parse(raw);
    },
    set(value) { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value)); }
  };
}

function pendingStorageKey(kind) {
  return 'kinora-pending:' + JSON.stringify([S.status?.server, S.user?.username, kind]);
}

function clearSavedEditDrafts() {
  if (!S.status?.server || !S.user?.username) return;
  const defaultDraft = pendingStorageKey('human-edit');
  // The last quote and bracket delimit the persisted kind in the storage key.
  const prefixes = ['human-edit:', 'robot-edit:', 'robot-edit-drafts:']
    .map(kind => pendingStorageKey(kind).slice(0, -2));
  const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
  let failed = false;
  for (const key of keys) {
    if (key !== defaultDraft && !prefixes.some(prefix => key?.startsWith(prefix))) continue;
    try { localStorage.removeItem(key); }
    catch (_) { failed = true; }
  }
  if (failed) throw new Error('未能删除所有编辑草稿');
}

function definitiveRejection(error) {
  return error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429;
}

async function submitOnce(kind, path, value, idField, { acknowledge = () => true } = {}) {
  const session = S.session;
  const canonical = JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical));
  if (session !== S.session) throw new DOMException('会话已切换', 'AbortError');
  const record = pendingRecord(kind + ':' + Array.from(new Uint8Array(hash), x => x.toString(16).padStart(2, '0')).join(''));
  const id = record.get() || randomId();
  record.set(id);
  try {
    const request = {method:'POST', body:{...value, [idField]:id}};
    let result;
    for (let attempt = 0; ; attempt++) {
      try {
        result = await cloud(path, request);
        break;
      } catch (error) {
        // These queue endpoints deduplicate by the persisted ID. A lost
        // response may already have created the job; never allocate a new ID.
        const multiScene = path === '/api/harness/multi-scene' && idField === 'request_id';
        const replayable = multiScene || (path === '/api/custom' && idField === 'request_id') ||
          (path === '/api/retarget' && idField === 'id');
        const interrupted = error.status === 0 || (multiScene && error.status === 503);
        if (attempt !== 0 || !replayable || !interrupted || session !== S.session) throw error;
      }
    }
    if (result?.decision_receipt?.action_applied !== false && acknowledge() && record.get() === id) record.set(null);
    return result;
  } catch (error) {
    if (definitiveRejection(error) && record.get() === id) record.set(null);
    throw error;
  }
}

// ---------------- Cloud bridge ----------------
let passwordRotation = null;
async function handleCloudAuthentication(res, path, session) {
  if (session !== S.session || res?.stale_auth) throw new DOMException('会话已切换', 'AbortError');
  const expired = res?.status === 401 && path !== '/api/auth/login' && S.user;
  const rotation = passwordRotation;
  if ((expired || res?.must_change) && path !== '/api/auth/password' && rotation?.session === session) {
    // The server can invalidate the outgoing cookie before its replacement arrives.
    const replacement = await rotation.request.catch(() => null);
    if (session !== S.session || replacement?.ok) throw new DOMException('会话已切换', 'AbortError');
  }
  if (expired) {
    window.Workspace?.flushSave?.();
    toLogin();
    throw new DOMException('会话已失效', 'AbortError');
  }
  if (res?.must_change) openPassword(true);
}

async function cloud(path, { method = 'GET', query, body } = {}) {
  const session = S.session;
  const request = window.desktop.cloud({ method, path, query, body });
  if (path === '/api/auth/password' && method === 'POST') passwordRotation = {session, request};
  let res;
  try { res = await request; }
  finally { if (passwordRotation?.request === request) passwordRotation = null; }
  if (session !== S.session) throw new DOMException('会话已切换', 'AbortError');
  if (res?.ok) { setOnline(true); return res.buffer ?? res.data; }
  if (res?.status === 0) setOnline(false); else if (res?.status) setOnline(true);
  await handleCloudAuthentication(res, path, session);
  const error = new Error(res?.error || '请求失败'); error.status = res?.status; throw error;
}

async function downloadFile(request) {
  const session = S.session;
  const result = await window.desktop.download(request);
  await handleCloudAuthentication(result, request.path, session);
  return result;
}
const quiet = error => { if (error?.name !== 'AbortError') console.warn(error); };

function setOnline(value) {
  if (S.online === value) return;
  S.online = value;
  $('banner').hidden = value || document.body.dataset.screen !== 'ready';
}

function toast(message, bad = false) {
  const el = h('div', { class: 'toast' + (bad ? ' bad' : ''), role: bad ? 'alert' : 'status' }, h('span', { text: message }));
  $('toasts').append(el); setTimeout(() => el.remove(), bad ? 6000 : 3500);
}

// Copy displayed values only; editable fields retain normal selection and editing.
function makeCopyable(el, label, value) {
  if (!el) return el;
  el.classList.add('click-copy');
  el.setAttribute('role', 'button'); el.tabIndex = 0;
  el.title = '点击复制' + label;
  el.setAttribute('aria-label', '复制' + label);
  const copy = async event => {
    event.preventDefault(); event.stopPropagation();
    const text = typeof value === 'function' ? value() : value ?? el.textContent;
    if (!text) return;
    const focused = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text; field.className = 'copy-buffer'; field.setAttribute('aria-hidden', 'true');
    (el.closest('dialog') || document.body).append(field);
    field.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } catch (_) { /* use Clipboard API below */ }
    field.remove(); focused?.focus({ preventScroll: true });
    if (!copied) {
      try { await navigator.clipboard.writeText(text); copied = true; } catch (_) { /* report failure */ }
    }
    toast(copied ? label + '已复制' : '复制失败，请选中文字后按 Ctrl/Cmd+C', !copied);
  };
  el.addEventListener('click', copy);
  el.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') copy(event);
  });
  return el;
}

// ---------------- Screens ----------------
function screen(name) {
  document.body.dataset.screen = name;
  for (const id of ['connecting', 'offline', 'login']) $('gate-' + id).hidden = id !== name;
  if (name === 'login') { restoreCredentials(); setTimeout(() => $('login-user').focus(), 30); }
  $('banner').hidden = true;
}

function showServerPreferenceError() {
  screen('offline');
  $('offline-reason').hidden = false;
  $('offline-reason').textContent = '暂时无法读取本机保存的服务器地址。请重试或重新填写地址。';
}

async function boot() {
  if (!S.status || S.status.serverPreferenceUnavailable) { showServerPreferenceError(); return; }
  if (!S.status?.serverConfigured) { screen('connecting'); openServerSettings(); return; }
  const attempt = S.session;
  screen('connecting');
  try {
    const { user } = await cloud('/api/auth/me');
    await enter(user);
  } catch (error) {
    if (attempt !== S.session || error.name === 'AbortError') return;
    if (error.status === 401) {
      screen('login');
    }
    else {
      $('offline-reason').hidden = !error.status; $('offline-reason').textContent = error.status ? error.message : '';
      screen('offline');
    }
  }
}

function resetSession() {
  window.invalidateTextPreview?.();
  window.PersonalAssets?.reset();
  window.resetGadget?.({ clearPathFields: true });
  window.resetRobotEditor?.();
  window.VideoMotion?.reset?.();
  window.Workspace?.onSessionReset?.();
  S.session++; S.user = null; S.capabilities = null; stopPolling();
  if ($('password').open) {
    $('password-form').reset();
    $('password').close();
    setPasswordSaving(false);
  }
  S.taskCancellations.clear();
  // Old requests keep their session number and cannot release a new login's locks.
  resetLibrary();
  applyTopics([]);
  Object.assign(S.gen, { busy: false, submitting: false, history: false, after: null, listHistory: null, retryParent: null, jobs: [] });
  S.gen.recentJobs = null; S.gen.recentRequest = null;
  S.gen.open.clear(); S.gen.results.clear(); S.gen.resultData?.clear?.(); S.gen.pendingAttach?.clear?.();
  S.gen.modelListOwner = null;
  $('gen-model').replaceChildren();
  Object.assign(S.rt, { busy: false, history: false, after: null, listHistory: null, playing: null, playingJob: null, retryPreview: null, items: [], sourceSnapshot: null });
  S.rt.downloads.clear();
  S.rt.quarantinedSources.clear();
  S.rt.submittingTabs.clear();
  S.rt.submissionErrors.clear();
  S.rt.recentJobs = null; S.rt.recentRequest = null;
  setSeg('gen', false);
  setSeg('rt', false);
  $('gen-feed').replaceChildren(); $('rt-jobs').replaceChildren();
  for (const id of ['gen-list-error', 'rt-list-error']) {
    $(id).hidden = true;
    $(id).replaceChildren();
  }
  $('rt-dispatch-notice').textContent = ''; $('rt-dispatch-notice').hidden = true;
  $('gen-quota').textContent = ''; $('gen-note').textContent = ''; $('rt-error').textContent = '';
  $('gen-more').hidden = true; $('rt-more').hidden = true;
  $('rt-submit').disabled = false;
  setBadge('badge-generate', 0); setBadge('badge-retarget', 0);
  syncGeneration(null);
  robotView.clear(); window.rtHumanView?.clear?.(); previewCache.clear(); window.MotionLoader?.clear?.();
}

function toLogin() {
  S.reconnectLibrary = null;
  resetSession();
  screen('login');
}

async function enter(user) {
  const reconnect = S.reconnectLibrary?.username === user.username ? S.reconnectLibrary : null;
  S.reconnectLibrary = user.must_change ? reconnect : null;
  S.user = user; S.online = true; setOnline(true);
  $('avatar').textContent = user.username || '';
  $('set-user').textContent = user.username;
  if (user.must_change) {
    screen('ready');
    openPassword(true);
    return;
  }
  screen('connecting');
  const session = S.session;
  await Promise.allSettled([loadTopics(), loadModels(), loadCapabilities(), refreshRetarget()]);
  if (session !== S.session) return;
  await window.Workspace?.restoreSession?.();
  if (session !== S.session) return;
  setLegacyLibrarySearch(false);
  for (const [id, value] of reconnect?.fields || []) {
    const control = $(id);
    control.value = value;
    if (control.tagName === 'SELECT' && control.selectedIndex < 0) control.selectedIndex = 0;
  }
  screen('ready');
  searchLibrary(false, undefined, false, { restoreMotion: reconnect?.selected });
  refreshCustom();
  startPolling();
}

async function loadCapabilities() {
  const session = S.session;
  try {
    S.capabilities = await cloud('/api/harness/capabilities');
  } catch (error) {
    if (session === S.session) S.capabilities = null;
    quiet(error);
  }
}

// ---------------- Navigation ----------------
function go(view) {
  if (view === 'gadget') {
    window.openGadgetModal?.();
    return;
  }
  if (window.Workspace?.routeLegacyNav) {
    window.Workspace.routeLegacyNav(view);
    return;
  }
  applyStageView(view);
}

function applyStageView(view) {
  const previousView = S.view;
  if (previousView === 'retarget' && view !== 'retarget') clearRetargetPlayback();
  S.view = view;
  document.body.dataset.view = view;
  const stageMap = { library: 'preview', preview: 'preview', generate: 'generate', editor: 'editor', retarget: 'retarget', assets: 'assets' };
  const stage = stageMap[view] || 'preview';
  document.body.dataset.stage = stage;
  for (const sec of document.querySelectorAll('.ws-stage')) sec.hidden = sec.dataset.stage !== stage;
  for (const panel of document.querySelectorAll('.ws-param')) panel.hidden = panel.dataset.param !== stage;
  if (previousView !== view && $('ws-right-body')) $('ws-right-body').scrollTop = 0;
  for (const b of document.querySelectorAll('[data-nav]')) {
    if (b.dataset.nav === view) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  }
  window.PersonalAssets?.setActive(stage === 'assets');
  // Suspend hidden viewport loops and resume only visible ones.
  if (stage === 'preview') {
    motionView.resume?.();
    window.SceneController?.view?.suspend?.();
    window.SequenceEditor?.suspend?.();
    window.RepairEditor?.suspend?.();
    robotView.suspend?.();
    rtHumanView.suspend?.();
  } else if (stage === 'generate') {
    motionView.suspend?.();
    window.SceneController?.view?.resume?.();
    window.SequenceEditor?.suspend?.();
    window.RepairEditor?.suspend?.();
    robotView.suspend?.();
    rtHumanView.suspend?.();
    loadTopics().catch(quiet); refreshCustom(); updateChecks();
  } else if (stage === 'editor') {
    motionView.suspend?.();
    window.SceneController?.view?.suspend?.();
    robotView.suspend?.();
    rtHumanView.suspend?.();
    if (window.SequenceEditor?.getMode?.() === 'repair') {
      window.SequenceEditor?.suspend?.();
      window.RepairEditor?.resume?.();
    } else {
      window.RepairEditor?.suspend?.();
      window.SequenceEditor?.resume?.();
    }
  } else if (stage === 'assets') {
    motionView.suspend?.();
    window.SceneController?.view?.suspend?.();
    window.SequenceEditor?.suspend?.();
    window.RepairEditor?.suspend?.();
    robotView.suspend?.();
    rtHumanView.suspend?.();
  } else if (stage === 'retarget') {
    motionView.suspend?.();
    window.SceneController?.view?.suspend?.();
    window.SequenceEditor?.suspend?.();
    window.RepairEditor?.suspend?.();
    syncRetargetViewportActive();
    refreshRetarget(); syncRetargetSource();
  }
}
window.applyStageView = applyStageView;
for (const b of document.querySelectorAll('.sr-nav [data-nav]')) b.addEventListener('click', () => go(b.dataset.nav));

// ---------------- Library ----------------
let nextTopicsRequest = 0;

async function loadTopics() {
  const session = S.session;
  const request = ++nextTopicsRequest;
  const { topics, generation } = await cloud('/api/topics');
  if (session !== S.session || request !== nextTopicsRequest) return;
  syncGeneration(generation);
  const filterChanged = applyTopics(topics);
  if (filterChanged && S.lib.scope !== 'results' && document.body.dataset.screen === 'ready') searchLibrary();
}

function applyTopics(topics) {
  S.topics = new Map(topics.map(t => [t.id, t.name || t.id]));
  const select = $('f-topic'), current = select.value;
  select.replaceChildren(h('option', { value: '', text: '全部主题' }), ...topics.map(t => h('option', { value: t.id, text: t.name || t.id })));
  select.value = S.topics.has(current) ? current : '';
  return select.value !== current;
}

const thumbQueue = [];
let thumbBatch = { active: 0 };
const thumbnailsAwaitingExit = new WeakSet();
const thumbObserver = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const canvas = entry.target;
    if (!canvas.isConnected) continue;
    if (!entry.isIntersecting) {
      thumbnailsAwaitingExit.delete(canvas);
      continue;
    }
    if (thumbnailsAwaitingExit.has(canvas)) continue;
    thumbObserver.unobserve(canvas);
    thumbQueue.push({ canvas, version: S.lib.version });
  }
  pumpThumbs();
}, { rootMargin: '120px' });

function resetThumbnails() {
  thumbObserver.disconnect();
  thumbQueue.length = 0;
  thumbBatch = { active: 0 };
}

function pumpThumbs() {
  if (previewLoading || MotionLoader.hasForegroundLoad) return;
  const batch = thumbBatch;
  while (batch.active < 3 && thumbQueue.length) {
    const job = thumbQueue.shift();
    if (job.version !== S.lib.version || !job.canvas.isConnected) continue;
    const mid = job.canvas.dataset.motion;
    batch.active++;
    const loader = MotionLoader.isManagedAsset(mid)
      ? MotionLoader.fetchPreview(mid, { background: true }).then(src => {
          const midFrame = Math.floor(src.frames / 2);
          const joints = Array.from({ length: src.count }, (_, j) => Array.from(src.data.subarray((midFrame * src.count + j) * 3, (midFrame * src.count + j) * 3 + 3)));
          return { joints, parents: src.meta.parents, names: src.meta.names };
        })
      : cloud(`/api/motions/${mid}/thumbnail`);
    loader
      .then(clip => {
        if (!job.canvas.isConnected) return;
        MotionView.drawThumbnail(job.canvas, clip);
        job.canvas.removeAttribute('title');
      })
      .catch(() => {
        if (!job.canvas.isConnected) return;
        job.canvas.title = '缩略图暂不可用';
        // Retry after the card leaves the lazy-load area and returns.
        thumbnailsAwaitingExit.add(job.canvas);
        thumbObserver.observe(job.canvas);
      })
      .finally(() => {
        batch.active--;
        if (batch === thumbBatch) pumpThumbs();
      });
  }
}
if (typeof MotionLoader !== 'undefined') MotionLoader.onLoadChange(() => pumpThumbs());

function motionCard(item) {
  const canvas = h('canvas', { class: 'thumb', width: 72, height: 88, 'aria-hidden': 'true', dataset: { motion: item.motion_id } });
  const quarantined = item.status === 'quarantined';
  const status = quarantined ? 'quarantined' : item.review?.status || (item.accepted ? 'accepted' : 'pending');
  const bindings = window.Workspace?.getMotionUsageLabels?.(item.motion_id) || [];
  const card = h('button', {
    class: 'motion-card',
    role: 'option',
    type: 'button',
    'aria-selected': String(S.lib.selected === item.motion_id),
    dataset: { motion: item.motion_id },
    title: '单击选择浏览 · 双击在中央预览 · 可直接拖入拼接时间线',
    onclick: () => {
      const inPreview = !window.Workspace || window.Workspace.isPreviewActive();
      selectMotion(item.motion_id, { previewOnlySelection: !inPreview });
    },
    ondblclick: () => selectMotion(item.motion_id, { reveal: true })
  },
    canvas,
    h('div', { class: 'mc-body' },
      h('p', { class: 'mc-text', text: item.text || item.label || '（无描述）' }),
      h('div', { class: 'mc-meta' },
        h('span', { class: 'mono', text: item.motion_id.length > 14 ? item.motion_id.slice(0, 12) + '…' : item.motion_id, title: item.motion_id }),
        item.duration ? h('span', { text: Number(item.duration).toFixed(0) + ' s' }) : null,
        pill(REVIEW, status),
        item.managed ? h('span', { class: 'pill plain accent', text: item.accepted && !quarantined ? '已选用新结果' : '编辑/新结果' })
          : (item.owner && item.owner === S.user?.username ? h('span', { class: 'pill plain accent', text: '我的' }) : null),
        ...bindings.map(lbl => h('span', { class: 'pill plain ws-use-badge', text: lbl })))));
  card.addEventListener('pointerdown', e => window.Workspace?.beginLibraryCardDrag?.(e, item));
  thumbObserver.observe(canvas);
  return card;
}

async function searchHarnessResults(q, sort, version) {
  const data = await cloud('/api/harness/results');
  if (version !== S.lib.version) return null;
  const raw = (data.items || []).map(r => {
    const quarantined = r.status === 'quarantined';
    const accepted = !quarantined && Boolean(r.accepted);
    MotionLoader.rememberMeta(r.motion_id, { ...r, accepted, kind_label: '编辑与场景结果' });
    return {
      motion_id: r.motion_id,
      text: r.label || r.motion_id,
      owner: S.user?.username || '',
      managed: true,
      accepted,
      status: r.status,
      created: r.created || 0,
      task_id: r.task_id,
      review: { status: quarantined ? 'rejected' : accepted ? 'accepted' : 'pending' }
    };
  });
  S.lib.harnessResults = raw;
  const needle = q.toLowerCase();
  let filtered = needle ? raw.filter(x => (x.text + ' ' + x.motion_id).toLowerCase().includes(needle)) : raw;
  if (sort === 'created_asc') filtered = filtered.slice().sort((a, b) => (a.created || 0) - (b.created || 0));
  else filtered = filtered.slice().sort((a, b) => (b.created || 0) - (a.created || 0));
  return filtered;
}

async function searchLibrary(append = false, selectionSeq = S.lib.selectionSeq, retrying = false,
                             { keywordOnly = false, restoreMotion = null } = {}) {
  const lib = S.lib;
  if (!S.user || composing) return;
  const selectFirstResult = () => {
    if (selectionSeq !== lib.selectionSeq) return;
    if (restoreMotion) {
      const pending = selectMotion(restoreMotion, { previewOnlySelection: true });
      const restoredSeq = lib.selectionSeq;
      pending.then(error => {
        if (version !== lib.version || restoredSeq !== lib.selectionSeq || lib.detail ||
            (error?.status !== 403 && error?.status !== 404)) return;
        const previewActive = !window.Workspace || window.Workspace.isPreviewActive();
        const first = lib.items.find(item => item.motion_id !== restoreMotion);
        selectMotion(previewActive && first ? first.motion_id : null);
      }).catch(quiet);
      return;
    }
    if (!lib.items.length || lib.items.some(i => i.motion_id === lib.selected)) return;
    if (!window.Workspace || window.Workspace.isPreviewActive()) selectMotion(lib.items[0].motion_id);
  };
  const q = $('search-q').value.trim();
  const sort = $(lib.scope === 'results' ? 'f-results-sort' : 'f-sort').value;
  const query = {
    q, sort, topic: $('f-topic').value, status: $('f-status').value,
    author: lib.scope === 'mine' ? S.user.username : '',
    limit: 30
  };
  const queryKey = JSON.stringify([lib.scope, query]);
  if (append && lib.pageQueryKey !== queryKey) append = false;
  if (append && (lib.busy || lib.after === null || lib.scope === 'results')) return;
  const version = append ? lib.version : ++lib.version;
  const more = $('list-more');
  const retry = $('list-retry');
  const list = $('motion-list');
  const finishLoading = () => {
    if (version !== lib.version) return;
    lib.busy = false;
    more.removeAttribute('aria-disabled');
    retry.removeAttribute('aria-disabled');
  };
  const finishRetry = () => {
    if (!retrying) return;
    const restoreFocus = document.activeElement === retry;
    retry.hidden = true;
    if (restoreFocus) {
      const nextFocus = list.querySelector('.motion-card') || (more.hidden ? $('search-q') : more);
      nextFocus.focus();
    }
  };
  lib.busy = true;
  more.setAttribute('aria-disabled', 'true');
  retry.setAttribute('aria-disabled', 'true');
  showLibraryPageError('', retrying);
  if (!append) {
    clearTimeout(searchTimer);
    searchTimer = null;
    lib.pageQueryKey = queryKey;
    lib.pageCursors.clear();
    lib.items = []; lib.after = null;
    resetThumbnails();
    more.textContent = '更多';
    more.hidden = true;
    $('list-count').textContent = '';
    list.replaceChildren(...Array.from({ length: 5 }, () => h('div', { class: 'skeleton-card' })));
  }
  if (lib.scope === 'results') {
    try {
      const items = await searchHarnessResults(q, sort, version);
      if (!items || version !== lib.version) return;
      lib.items = items;
      lib.after = null;
      more.hidden = true;
      $('list-count').textContent = items.length + ' 个结果';
      if (items.length) list.replaceChildren(...items.map(motionCard));
      else list.replaceChildren(h('div', { class: 'empty', text: q ? '无匹配的新结果' : '暂无拼接、修正或场景生成结果' }));
      refreshOpenPreviewDetails(lib.harnessResults);
      selectFirstResult();
      finishRetry();
    } catch (error) {
      if (version === lib.version && error.name !== 'AbortError') {
        list.replaceChildren();
        showLibraryPageError('加载失败：' + error.message, true);
        if (restoreMotion) selectFirstResult();
      }
    } finally {
      finishLoading();
    }
    return;
  }

  query.after = append ? lib.after : 0;
  // Legacy search may consume multiple cursors. Only commit them after the load succeeds.
  const pageCursors = new Set(lib.pageCursors);
  pageCursors.add(String(query.after));
  try {
    const result = lib.legacy ? await legacySearch(query, version, pageCursors) : await rankedSearch(query, version, pageCursors, keywordOnly);
    if (!result || version !== lib.version) return;
    if (result.restartInLegacyMode) {
      if (document.activeElement === more) $('search-q').focus();
      return searchLibrary(false, selectionSeq, false, { restoreMotion });
    }
    if (result.restartInRankedMode) {
      if (document.activeElement === more) $('search-q').focus();
      return searchLibrary(false, selectionSeq, false, { keywordOnly: result.keywordOnly, restoreMotion });
    }
    const seen = new Set(lib.items.map(item => item.motion_id));
    const added = result.items.filter(item => {
      if (seen.has(item.motion_id)) return false;
      seen.add(item.motion_id);
      return true;
    });
    const hadItems = lib.items.length > 0;
    if (!append || !hadItems) list.replaceChildren();
    const next = result.next_after ?? null;
    const stalled = result.paginationStalled || (next !== null && pageCursors.has(String(next)));
    lib.pageCursors = pageCursors;
    lib.items.push(...added);
    lib.after = stalled ? null : next;
    for (const item of added) MotionLoader?.rememberMeta?.(item.motion_id, { label: item.text, owner: item.owner, duration: item.duration });
    const addedCards = added.map(motionCard);
    const focusWasOnMore = append && document.activeElement === more;
    list.append(...addedCards);
    more.textContent = '更多';
    more.hidden = lib.after === null;
    if (stalled) showLibraryPageError('服务端返回了相同的分页位置，请重新加载。', true);
    $('list-count').textContent = typeof result.total === 'number' ? result.total + ' 个' : '';
    if (query.status.startsWith('direction_') && result.direction_progress) {
      const p = result.direction_progress;
      $('list-count').textContent += ` · 历史补检 ${p.scanned}/${p.target || '待统计'}` + (p.state === 'paused' ? '（生成优先，已暂停）' : p.state === 'complete' ? '（完成）' : '');
    }
    if (!lib.items.length) list.replaceChildren(h('div', { class: 'empty', text: result.semantic?.reason || (lib.after !== null ? '本页暂无匹配，点击更多继续查找' : '无匹配') }));
    refreshOpenPreviewDetails(result.items);
    if (!append || (!hadItems && !lib.selected)) selectFirstResult();
    if (!stalled) finishRetry();
    if (focusWasOnMore && (addedCards.length || more.hidden)) {
      const nextFocus = addedCards[0] || list.querySelector('.motion-card:last-child') || (stalled ? $('list-retry') : $('search-q'));
      nextFocus.focus();
    }
  } catch (error) {
    if (version !== lib.version || error.name === 'AbortError') return;
    if (append) {
      more.textContent = '重试加载更多';
      showLibraryPageError('加载更多失败：' + error.message);
    } else {
      list.replaceChildren();
      showLibraryPageError('加载失败：' + error.message, true);
      if (restoreMotion) selectFirstResult();
    }
  } finally { finishLoading(); }
}

function applyLibraryScope(scope) {
  S.lib.scope = scope;
  const results = scope === 'results';
  $('lib-scope-all')?.setAttribute('aria-pressed', String(scope === 'all'));
  $('f-mine')?.setAttribute('aria-pressed', String(scope === 'mine'));
  $('lib-scope-results')?.setAttribute('aria-pressed', String(results));
  if ($('lib-filters')) $('lib-filters').hidden = results;
  $('f-sort').hidden = results;
  $('f-results-sort').hidden = !results;
}
window.applyLibraryScope = applyLibraryScope;

function setLibraryScope(scope) {
  applyLibraryScope(scope);
  searchLibrary();
  window.Workspace?.saveWorkspaceDebounced?.();
}
window.setLibraryScope = setLibraryScope;

async function rankedSearch(query, version, pageCursors, keywordOnly = false) {
  const session = S.session;
  const lib = S.lib;
  const continuing = query.after > 0;
  const useKeywords = keywordOnly || (continuing && lib.rankedMode === 'keyword');
  try {
    const result = await cloud('/api/library/find', { query: useKeywords ? { ...query, keyword_only: 1 } : query });
    if (session !== S.session || version !== lib.version) return null;
    let mode = null;
    if (result.semantic?.used) mode = 'semantic';
    else if (useKeywords || result.semantic?.reason) mode = 'keyword';
    if (continuing && mode && lib.rankedMode && mode !== lib.rankedMode) {
      return { restartInRankedMode: true, keywordOnly: mode === 'keyword' };
    }
    lib.rankedMode = mode;
    return result;
  }
  catch (error) {
    if (error.status !== 404) throw error;
    if (session !== S.session || version !== S.lib.version) return null;
    setLegacyLibrarySearch(true);
    // Ranked search uses an offset. Legacy search uses the last motion number.
    if (query.after > 0) return { restartInLegacyMode: true };
    return legacySearch(query, version, pageCursors);
  }
}

function setLegacyLibrarySearch(legacy) {
  S.lib.legacy = legacy;
  $('f-sort').disabled = legacy;
  $('f-sort').title = legacy ? '云端尚未支持排序' : '';
}

async function legacySearch(query, version, visited = new Set()) {
  const { sort, ...rest } = query;
  let after = query.after, found = [], next = null, windows = 0, directionProgress;
  do {
    visited.add(String(after));
    const result = await cloud('/api/library/search', { query: { ...rest, after } });
    if (version !== S.lib.version) return null;
    directionProgress = result.direction_progress;
    found.push(...result.items); next = result.next_after ?? null;
    if (next !== null && visited.has(String(next))) {
      return { items: found, next_after: null, direction_progress: directionProgress, paginationStalled: true };
    }
    after = next; windows++;
  } while (found.length < 12 && next !== null && windows < 6);
  return { items: found, next_after: next, direction_progress: directionProgress };
}
let searchTimer = null, composing = false;
const invalidateSearch = () => {
  clearTimeout(searchTimer);
  searchTimer = null;
  ++S.lib.version;
};
const scheduleSearch = delay => {
  invalidateSearch();
  const selectionSeq = S.lib.selectionSeq;
  searchTimer = setTimeout(() => searchLibrary(false, selectionSeq), delay);
};
$('search-form').addEventListener('submit', e => { e.preventDefault(); searchLibrary(); });
$('search-q').addEventListener('compositionstart', () => { composing = true; invalidateSearch(); });
$('search-q').addEventListener('compositionend', () => { composing = false; scheduleSearch(500); });
$('search-q').addEventListener('input', e => { if (!composing && !e.isComposing) scheduleSearch(600); });
$('f-sort').addEventListener('change', () => searchLibrary());
$('f-results-sort').addEventListener('change', () => searchLibrary());
$('f-topic').addEventListener('change', () => searchLibrary());
$('f-status').addEventListener('change', () => searchLibrary());
$('lib-scope-all')?.addEventListener('click', () => setLibraryScope('all'));
$('f-mine').addEventListener('click', () => setLibraryScope(S.lib.scope === 'mine' ? 'all' : 'mine'));
$('lib-scope-results')?.addEventListener('click', () => setLibraryScope(S.lib.scope === 'results' ? 'all' : 'results'));
$('list-retry').addEventListener('click', () => { if (!S.lib.busy) searchLibrary(false, undefined, true); });
$('list-more').addEventListener('click', () => { if (!S.lib.busy) searchLibrary(true); });

function viewportMessage(id, text, kind = '') {
  const el = $(id);
  if (!el) return;
  if (id === 'viewport-msg') {
    $('preview-empty-actions').hidden = kind !== 'idle';
    $('preview-error-actions').hidden = kind !== 'error';
  }
  if (id === 'rt-human-msg') $('rt-human-retry-actions').hidden = kind !== 'error';
  if (id === 'rt-msg') $('rt-robot-retry-actions').hidden = kind !== 'error';
  if (text === null) { el.hidden = true; return; }
  el.hidden = false; el.className = 'viewport-msg ' + kind; el.querySelector('p').textContent = text;
}

const previewCache = typeof MotionLoader !== 'undefined' ? MotionLoader.rawCache : new Map();
let previewLoading = 0;
async function fullPreview(id) {
  if (typeof MotionLoader !== 'undefined') return MotionLoader.fetchPreview(id);
  previewLoading++;
  try {
    return MotionView.decodePreview(await cloud(`/api/motions/${id}/preview.bin`));
  } finally { previewLoading--; pumpThumbs(); }
}

async function selectMotion(id, { reveal = false, previewOnlySelection = false } = {}) {
  const lib = S.lib;
  lib.selectionSeq++;
  const reuse = id && id === lib.selected &&
    (lib.previewRequest?.id === id || (lib.previewId === id && motionView.clip));
  const seq = reuse ? lib.previewSeq : ++lib.previewSeq;
  if (!reuse) {
    if (id !== lib.selected) lib.detail = null;
    lib.selected = id;
    lib.previewRequest = null;
    $('motion-download').disabled = !id;
    if ($('tab-preview-sub')) $('tab-preview-sub').textContent = id ? (id.length > 10 ? id.slice(0, 9) + '…' : id) : '';
    for (const c of document.querySelectorAll('.motion-card')) c.setAttribute('aria-selected', String(c.dataset.motion === id));
  }
  if (reveal) {
    if (window.Workspace) window.Workspace.activateTab('preview');
    else go('library');
  }
  if (reuse) return lib.previewRequest?.promise;
  window.Workspace?.onLibrarySelectionChanged?.(id);
  if (!id) {
    lib.previewId = null;
    motionView.clear(); setTransport();
    viewportMessage('viewport-msg', '请在动作库选择动作', 'idle');
    renderInspector(null, null);
    return;
  }
  if (previewOnlySelection && window.Workspace && !window.Workspace.isPreviewActive()) {
    // Load detail quietly in background for bridge bar labels without disrupting active tab stage.
    return MotionLoader.fetchDetail(id).then(({ detail }) => {
      if (seq === lib.previewSeq) { lib.detail = detail; window.Workspace?.onLibrarySelectionChanged?.(id); }
    }).catch(error => {
      quiet(error);
      return error;
    });
  }
  const pending = { id };
  lib.previewRequest = pending;
  pending.promise = loadLibraryPreview(lib, pending, seq);
  return pending.promise;
}
window.selectMotion = selectMotion;

async function loadLibraryPreview(lib, pending, seq) {
  const { id } = pending;
  lib.previewId = null;
  motionView.clear(); setTransport();
  viewportMessage('viewport-msg', MotionLoader.has(id) ? '' : '正在下载完整动作…', 'loading');
  renderInspector(lib.detail, id);
  try {
    const { detail: loadedDetail, preview: preloaded } = await MotionLoader.fetchDetail(id);
    if (seq !== lib.previewSeq) return;
    const detail = pending.summary ? mergePreviewDetail(loadedDetail, pending.summary) : loadedDetail;
    lib.detail = detail; renderInspector(detail, id);
    if (detail.storage_tier === 'cold' && !MotionLoader.has(id)) viewportMessage('viewport-msg', '正在从冷归档解压并下载完整动作…', 'loading');
    const decoded = preloaded || await fullPreview(id);
    if (seq !== lib.previewSeq) return;
    const previewActive = !window.Workspace || window.Workspace.isPreviewActive();
    motionView.setClip(decoded);
    lib.previewId = id;
    motionView.setPlaying(previewActive);
    viewportMessage('viewport-msg', null); setTransport();
    renderInspector(lib.detail, id, decoded.meta);
  } catch (error) {
    if (seq !== lib.previewSeq || error.name === 'AbortError') return;
    viewportMessage('viewport-msg', error.message, 'error');
    if (!lib.detail) renderInspector(null, id, null, error.message);
    return error;
  } finally {
    if (lib.previewRequest === pending) lib.previewRequest = null;
  }
}

function ensureLibraryPreview() {
  const lib = S.lib;
  if (!lib.selected || lib.previewRequest?.id === lib.selected) return;
  if (lib.previewId !== lib.selected || !motionView.clip) selectMotion(lib.selected);
}
window.ensureLibraryPreview = ensureLibraryPreview;

function mergePreviewDetail(detail, summary) {
  if (detail.managed) return { ...detail, ...MotionLoader.getManagedMetadata(summary.motion_id) };
  const updated = { ...detail };
  for (const field of ['review', 'tags', 'quality_status']) {
    if (Object.hasOwn(summary, field)) updated[field] = summary[field];
  }
  return updated;
}

function refreshOpenPreviewDetails(items) {
  const lib = S.lib, id = lib.selected;
  if (!id || (lib.previewId !== id && lib.previewRequest?.id !== id)) return;
  const item = items.find(row => row.motion_id === id);
  if (!item) return;
  const pending = lib.previewRequest;
  if (pending?.id === id) pending.summary = { ...pending.summary, ...item };
  if (!lib.detail) return;
  const detail = mergePreviewDetail(lib.detail, item);
  if (JSON.stringify(detail) === JSON.stringify(lib.detail)) return;
  lib.detail = detail;
  renderInspector(detail, id, motionView.clip?.meta);
}

function resetLibrary() {
  const lib = S.lib;
  lib.version++;
  Object.assign(lib, { items: [], after: null, pageQueryKey: null, busy: false, detail: null, harnessResults: [], rankedMode: null });
  lib.pageCursors.clear();
  clearTimeout(searchTimer);
  searchTimer = null;
  composing = false;
  $('search-q').value = '';
  $('f-topic').value = '';
  $('f-status').value = '';
  $('f-sort').value = 'relevance';
  $('f-results-sort').value = 'created_desc';
  setLegacyLibrarySearch(false);
  applyLibraryScope('all');
  resetThumbnails();
  $('motion-list').replaceChildren();
  $('list-count').textContent = '';
  $('list-more').textContent = '更多';
  $('list-more').hidden = true;
  $('list-more').removeAttribute('aria-disabled');
  $('list-retry').removeAttribute('aria-disabled');
  showLibraryPageError('');
  selectMotion(null);
}

function showLibraryPageError(message, canRetry = false) {
  const error = $('list-page-error');
  error.textContent = message;
  error.hidden = !message;
  $('list-retry').hidden = !canRetry;
}

function refreshLibraryCardBadges() {
  for (const card of document.querySelectorAll('.motion-card')) {
    const mid = card.dataset.motion;
    const meta = card.querySelector('.mc-meta');
    if (!meta) continue;
    for (const old of meta.querySelectorAll('.ws-use-badge')) old.remove();
    const labels = window.Workspace?.getMotionUsageLabels?.(mid) || [];
    for (const lbl of labels) meta.append(h('span', { class: 'pill plain ws-use-badge', text: lbl }));
  }
}
window.refreshLibraryCardBadges = refreshLibraryCardBadges;

function renderInspector(detail, id, meta, errorMessage) {
  const box = $('inspector');
  if (!id) {
    box.replaceChildren(h('div', { class: 'empty-note' }, h('p', { class: 'muted', text: '在左侧动作库选择素材查看详情与操作' })));
    return;
  }
  if (!detail) {
    box.replaceChildren(h('div', { class: 'empty-note' },
      errorMessage
        ? h('p', { class: 'muted', role: 'status', text: '无法加载动作详情 · ' + errorMessage })
        : h('div', { class: 'spinner' })));
    return;
  }
  const item = S.lib.items.find(i => i.motion_id === id);
  const text = detail.prompt?.text || item?.text || '';
  const review = detail.review, mr = detail.machine_review;
  const quality = detail.quality_status || item?.quality_status;
  const quarantined = detail.status === 'quarantined';
  const reviewStatus = quarantined ? 'quarantined' : review?.status || 'pending';
  const direction = meta?.quality?.direction_semantics;
  const directionText = !direction ? '未检查' : ({ pass: '基础方向通过；完整语义待审核', fail: '方向不符', review: '待审核' }[direction.status] || '待审核');
  const tags = [...(detail.tags || item?.tags || []).reduce((m, t) => { const k = t.group + '\u0000' + t.tag; (m.get(k) || m.set(k, { ...t, users: [] }).get(k)).users.push(t.username); return m; }, new Map()).values()];
  const row = (label, ...value) => [h('dt', { text: label }), h('dd', {}, ...value)];
  const flags = detail.screening?.flags || [];
  box.replaceChildren(
    h('div', { class: 'insp-head' }, h('span', { class: 'insp-id selectable', copy: '动作序号', text: id }), h('span', { class: 'row' }, review?.reviewer ? h('span', { class: 'muted', text: review.reviewer }) : null, pill(REVIEW, reviewStatus))),
    h('p', { class: 'insp-text', copy: '文本提示词', text: text }),
    meta ? h('div', { class: 'stats mono' }, h('span', { text: (meta.duration_s ?? meta.frames / meta.fps).toFixed(2) + ' s' }), h('span', { text: Math.round(meta.fps) + ' fps' }), h('span', { text: meta.frames + ' 帧' })) : null,
    h('dl', { class: 'kv' },
      row('主题', S.topics.get(detail.topic) || detail.topic || '—'),
      row('作者', detail.owner || '—'),
      detail.parent_id ? row('来源版本', h('span', { class: 'mono', copy: '父版本编号', text: detail.parent_id })) : null,
      detail.storage_tier === 'cold' ? row('存储', '冷归档') : null,
      !detail.managed ? row('TMR', !mr || mr.state === 'pending' ? h('span', { class: 'pill', text: '未评分' }) : [h('span', { class: 'pill info', text: mr.state }), typeof mr.score === 'number' ? h('span', { class: 'mono', text: mr.score.toFixed(3) }) : null]) : null,
      row('方向语义', h('span', { text: directionText }), direction ? h('p', { class: 'job-msg', text: [...(direction.reasons || []), ...(direction.warnings || [])].join('；') }) : null),
      !detail.managed ? row('质量', quality ? pill(QUALITY, quality) : h('span', { class: 'pill', text: '未知' })) : null,
      row('检查', flags.length ? flags.map(f => h('span', { class: 'pill warn', text: FLAG_LABEL[f] || f })) : h('span', { class: 'pill', text: detail.screening ? '无提示' : '未筛查' })),
      tags.length ? row('标签', h('div', { class: 'tags' }, tags.map(t => h('span', { class: 'tag', title: t.users.join(', '), text: `${t.group} · ${t.tag}` + (t.users.length > 1 ? ` ×${t.users.length}` : '') })))) : null),
    h('div', { class: 'actions' },
      h('button', {
        class: 'btn primary block', type: 'button',
        onclick: e => window.Workspace
          ? window.Workspace.openMotionInTab('editor', id, { label: text, subMode: 'repair', forceNew: e.altKey || e.shiftKey })
          : go('editor')
      }, '编辑修正'),
      h('div', { class: 'insp-grid' },
        h('button', {
          class: 'btn subtle block', type: 'button',
          onclick: e => window.Workspace
            ? window.Workspace.openMotionInTab('editor', id, { label: text, subMode: 'sequence', forceNew: e.altKey || e.shiftKey })
            : go('editor')
        }, '加入拼接'),
        h('button', {
          class: 'btn subtle block', type: 'button', disabled: quarantined,
          onclick: e => window.Workspace
            ? window.Workspace.openMotionInTab('retarget', id, { label: text, parentId: detail.parent_id || null, forceNew: e.altKey || e.shiftKey })
            : (($('rt-motion').value = id), go('retarget'))
        }, '重定向')),
      h('button', { class: 'btn subtle block', type: 'button', onclick: e => useAsPrompt(text, e.altKey || e.shiftKey) }, '以此描述生成'),
      h('div', { class: 'link-row' },
        h('button', { class: 'text-btn', type: 'button', onclick: () => window.Workspace?.openMotionInTab('editor', id, { label: text, subMode: 'repair', forceNew: true }) }, '＋ 新建编辑标签页'),
        h('button', { class: 'text-btn', type: 'button', disabled: quarantined, onclick: () => window.Workspace?.openMotionInTab('retarget', id, { label: text, parentId: detail.parent_id || null, forceNew: true }) }, '＋ 新建重定向标签页')))
  );
}

function useAsPrompt(text, forceNew = false) {
  if (window.Workspace) {
    window.Workspace.openGenerateWithPrompt(text, { forceNew });
    return;
  }
  go('generate');
  window.Workspace?.setGenerateModality?.('text');
  $('gen-text').value = text;
  if (/^A person /.test(text) && [...$('gen-model').options].some(o => o.value === 'exact_english')) $('gen-model').value = 'exact_english';
  updateChecks(); $('gen-text').focus();
}

// ---------------- Transport ----------------
const motionView = new MotionView.View($('motion-canvas'), { onFrame: () => setTransport() });
window.motionView = motionView;
let transportKey = '';
function setTransport() {
  const clip = motionView.clip, f = Math.floor(motionView.frame);
  const key = (clip ? clip.frames : 0) + ':' + f + ':' + motionView.playing;
  if (key === transportKey) return; transportKey = key;
  $('play').disabled = !clip; $('scrub').disabled = !clip;
  $('play').querySelector('use').setAttribute('href', motionView.playing ? '#i-pause' : '#i-play');
  $('play').setAttribute('aria-label', motionView.playing ? '暂停' : '播放');
  if (!clip) { $('time').textContent = '0.00 / 0.00 s'; $('scrub').max = 0; $('scrub').value = 0; $('scrub').style.setProperty('--p', '0%'); return; }
  $('scrub').max = clip.frames - 1; $('scrub').value = f; $('scrub').style.setProperty('--p', (100 * f / Math.max(1, clip.frames - 1)) + '%');
  $('time').textContent = `${fmtTime(f / clip.fps)} / ${fmtTime(clip.frames / clip.fps)} s`;
}
$('play').addEventListener('click', () => motionView.setPlaying(!motionView.playing));
$('scrub').addEventListener('input', e => {
  const frame = Number(e.target.value);
  motionView.setPlaying(false);
  motionView.seek(frame);
});
$('speed').addEventListener('change', e => { motionView.speed = Number(e.target.value); });
$('loop').addEventListener('click', e => { motionView.loop = !motionView.loop; e.currentTarget.setAttribute('aria-pressed', String(motionView.loop)); });
$('tool-follow').addEventListener('click', e => { motionView.follow = !motionView.follow; e.currentTarget.setAttribute('aria-pressed', String(motionView.follow)); motionView.dirty = true; });
$('tool-trail').addEventListener('click', e => { motionView.trail = !motionView.trail; e.currentTarget.setAttribute('aria-pressed', String(motionView.trail)); motionView.dirty = true; });
$('tool-reset').addEventListener('click', () => motionView.resetCamera());

window.downloadMotion = async function(id, button) {
  if (!id) { toast('请先选择动作', true); return; }
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '正在下载…'; }
  try {
    const result = await downloadFile({ path: '/api/harness/motions/' + encodeURIComponent(id) + '/download', name: id + '.npz' });
    if (result?.error) throw new Error(result.error);
    if (result?.cancelled) return;
    if (!result?.saved) throw new Error('下载失败，请重试');
    toast('已保存 ' + result.saved);
  } catch (error) { if (error.name !== 'AbortError') toast(error.message || '下载失败，请重试', true); }
  finally { if (button) { button.disabled = false; button.textContent = label; } }
};
$('motion-download').addEventListener('click', e => window.downloadMotion(S.lib.selected, e.currentTarget));
$('preview-retry').addEventListener('click', () => {
  if (S.lib.selected) selectMotion(S.lib.selected);
});

// ---------------- Generate ----------------
function syncGeneration(generation) {
  const max = generation?.duration_max_seconds;
  S.gen.durationMax = Number.isSafeInteger(max) && max >= 1 ? max : null;
  const input = $('gen-duration');
  if (S.gen.durationMax === null) input.removeAttribute('max');
  else input.max = String(S.gen.durationMax);
  // Never silently shorten an entered or retried motion when the model changes.
  $('gen-duration-hint').textContent = S.gen.durationMax ? `当前云端模型支持 1–${S.gen.durationMax} 秒 / 动作` : '时长范围由云端模型决定，提交时由服务器校验';
  updateChecks();
}

async function loadModels() {
  const session = S.session;
  const owner = JSON.stringify([S.status?.server, S.user?.username]);
  const select = $('gen-model');
  const sameOwner = S.gen.modelListOwner === owner;
  const exact = h('option', { value: 'exact_english', text: '英文直传' });
  if (!sameOwner) select.replaceChildren(exact);
  let data;
  try {
    data = await cloud('/api/rewrite/models');
  } catch (error) {
    if (session !== S.session) return;
    quiet(error);
    updateChecks();
    return;
  }
  if (session !== S.session) return;
  const previous = sameOwner ? select.value : '';
  select.replaceChildren(
    ...(data.models || []).map(m => h('option', { value: m.id, disabled: !m.configured, text: m.label })),
    exact);
  S.gen.modelListOwner = owner;
  const usable = [...select.options].filter(o => !o.disabled);
  select.value = usable.some(o => o.value === previous) ? previous : (usable[0]?.value || 'exact_english');
  updateChecks();
}

function promptChecks() {
  const model = $('gen-model');
  const selectedModel = [...model.options].find(option => option.value === model.value);
  const text = $('gen-text').value.trim(), exact = model.value === 'exact_english';
  const duration = Number($('gen-duration').value), n = Number($('gen-count').value);
  const words = text ? text.split(/\s+/).length : 0;
  const latin = !text || [...text.normalize('NFC')].every(c => (c >= ' ' && c <= '~') || '‘’“”–—…'.includes(c) || /\p{Script=Latin}/u.test(c));
  const checks = [
    ['请选择可用的提示词改写方式', Boolean(selectedModel && !selectedModel.disabled)],
    ['描述长度 2–2000 字符', text.length >= 2 && text.length <= 2000],
    [S.gen.durationMax ? `时长为 1–${S.gen.durationMax} 秒整数` : '时长为正整数', Number.isSafeInteger(duration) && duration >= 1 && (S.gen.durationMax === null || duration <= S.gen.durationMax)],
    ['数量为 1–10000 整数', Number.isInteger(n) && n >= 1 && n <= 10000],
    ['以 “A person ” 开头', exact ? text.startsWith('A person ') : null],
    [`12–70 个英文词 · ${words}`, exact ? words >= 12 && words <= 70 : null],
    ['单段英文', exact ? latin && !/\n/.test($('gen-text').value.trim()) : null]
  ];
  return { checks, ok: checks.every(([, v]) => v !== false), exact, words };
}
function updateChecks() {
  const videoMode = document.body?.dataset?.genModality === 'video';
  if (videoMode) {
    $('gen-checks')?.replaceChildren();
    const canVideo = Boolean(window.VideoMotion?.canSubmit?.());
    if ($('gen-submit')) {
      $('gen-submit').disabled = !canVideo || S.gen.submitting;
      $('gen-submit').textContent = $('gen-attach-object')?.value ? '提取动作并绑定物体' : '提取动作';
    }
    window.Workspace?.syncTabDirty?.();
    return;
  }
  if (window.SceneController?.shouldHandleComposerSubmit?.()) {
    const sceneOk = window.SceneController.canSubmit();
    $('gen-checks')?.replaceChildren();
    if ($('gen-count-wrap')) $('gen-count-wrap').hidden = true;
    if ($('gen-submit')) {
      $('gen-submit').disabled = !sceneOk || S.gen.submitting;
      $('gen-submit').textContent = window.SceneController.submitLabel?.() || '生成场景动作';
    }
    window.Workspace?.syncTabDirty?.();
    return;
  }
  if ($('gen-count-wrap')) $('gen-count-wrap').hidden = Boolean(window.SceneController?.isMultiOrStepMode?.());
  const { checks, ok } = promptChecks();
  const typed = ($('gen-text')?.value?.trim().length || 0) > 0;
  $('gen-checks')?.replaceChildren(...(typed ? checks.filter(([, v]) => v === false) : []).map(([label]) => h('li', { class: 'fail', text: label })));
  if ($('gen-submit')) {
    $('gen-submit').disabled = !ok || S.gen.submitting;
    $('gen-submit').textContent = $('gen-attach-object')?.value ? '生成并绑定物体' : '生成';
  }
  window.Workspace?.syncTabDirty?.();
}
window.updateChecks = updateChecks;

for (const id of ['gen-text', 'gen-duration', 'gen-count']) $(id).addEventListener('input', () => {
  if (id === 'gen-text') window.SceneController?.onComposerTextInput?.($('gen-text').value);
  if (id === 'gen-duration') {
    if ($('scene-duration')) $('scene-duration').value = $('gen-duration').value;
    window.SceneController?.onComposerDurationChange?.(Number($('gen-duration').value));
  }
  updateChecks();
});
$('gen-model').addEventListener('change', updateChecks);
for (const b of document.querySelectorAll('.stepper button')) b.addEventListener('click', () => {
  const i = $('gen-duration');
  i.value = Math.max(1, Math.min(S.gen.durationMax ?? Number.MAX_SAFE_INTEGER, (Number(i.value) || 8) + Number(b.dataset.step)));
  if ($('scene-duration')) $('scene-duration').value = i.value;
  window.SceneController?.onComposerDurationChange?.(Number(i.value));
  updateChecks();
});
$('gen-text').addEventListener('keydown', e => {
  if (e.defaultPrevented || e.isComposing || e.repeat || e.altKey) return;
  if (e.key !== 'Enter' || (!e.metaKey && !e.ctrlKey)) return;
  e.preventDefault();
  $('composer').requestSubmit();
});

$('composer').addEventListener('submit', async e => {
  e.preventDefault();
  if (document.body?.dataset?.genModality === 'video') {
    await window.VideoMotion?.submit?.();
    return;
  }
  if (window.SceneController?.shouldHandleComposerSubmit?.()) {
    await window.SceneController.submitFromComposer();
    return;
  }
  const gen = S.gen; if (gen.submitting || !promptChecks().ok) return;
  const model = $('gen-model').value, session = S.session;
  const tabId = gen.tabKey, retryParent = gen.retryParent;
  const attachSpec = window.SceneController?.getAttachmentRequest?.() || null;
  const value = {
    prompt_mode: model === 'exact_english' ? 'exact_english' : 'translate',
    rewrite_model: model === 'exact_english' ? '' : model,
    description: $('gen-text').value.trim(),
    n: Number($('gen-count').value),
    duration: Number($('gen-duration').value)
  };
  if (retryParent) value.parent_request_id = retryParent;
  const submitKey = tabId && tabId !== 'default' ? 'generate:' + tabId : 'generate';
  const refreshAfterSubmit = jobListAfterSubmit('gen', refreshCustom);
  gen.submitting = true; updateChecks(); $('gen-note').textContent = '';
  try {
    const created = await submitOnce(submitKey, '/api/custom', value, 'request_id');
    if (session !== S.session) return;
    if (gen.tabKey === tabId && gen.retryParent === retryParent) gen.retryParent = null;
    const jobId = created?.id || created?.request_id || null;
    if (jobId) {
      gen.open.add(jobId);
      if (attachSpec?.attachment?.object_id) {
        gen.pendingAttach.set(jobId, { tabId, spec: attachSpec, requestId: randomId() });
      }
    }
    window.Workspace?.recordGenerateSubmission?.(tabId, jobId, value.description, retryParent, {
      model, duration: value.duration, count: value.n
    });
    if (gen.pendingAttach.has(jobId)) window.Workspace?.flushSave?.();
    toast(attachSpec?.attachment?.object_id ? '已提交动作生成，完成后将自动绑定物体响应' : '已提交动作生成');
    await refreshAfterSubmit();
  } catch (error) {
    if (session !== S.session || gen.tabKey !== tabId) return;
    $('gen-note').textContent = error.message;
  } finally { if (session === S.session) { gen.submitting = false; updateChecks(); } }
});

function setSeg(kind, history) {
  $(kind + '-live').setAttribute('aria-pressed', String(!history));
  $(kind + '-history').setAttribute('aria-pressed', String(history));
}
function chooseJobList(kind, history, refresh) {
  const state = S[kind];
  ++state.filterRevision;
  state.history = history;
  setSeg(kind, history);
  refresh();
}

function jobListAfterSubmit(kind, refresh) {
  const state = S[kind];
  const { tabKey, filterRevision } = state;
  const view = S.view;
  return () => {
    const showLive = view === S.view && tabKey === state.tabKey && filterRevision === state.filterRevision;
    if (showLive) {
      state.history = false;
      setSeg(kind, false);
    }
    return refresh(false, { replace: showLive, includeRecent: true });
  };
}

$('gen-live').addEventListener('click', () => chooseJobList('gen', false, refreshCustom));
$('gen-history').addEventListener('click', () => chooseJobList('gen', true, refreshCustom));
$('gen-more').addEventListener('click', () => refreshCustom(true));

function beginJobListRequest(state, append, replace) {
  const history = state.history;
  if (!S.user || (!replace && state.busy?.history === history) || (append && (!history || state.after == null))) return null;
  const request = { session: S.session, history };
  state.busy = request;
  if (!history) state.recentRequest = null;
  return request;
}

async function refreshRecentJobs(state, path, onUpdated) {
  if (!S.user || state.recentRequest) return;
  const request = { session: S.session };
  state.recentRequest = request;
  const isCurrent = () => state.recentRequest === request && request.session === S.session;
  try {
    const data = await cloud(path);
    if (isCurrent() && state.history) onUpdated(data);
  } catch (error) {
    if (isCurrent()) quiet(error);
  } finally {
    if (isCurrent()) state.recentRequest = null;
  }
}

function isCurrentJobListRequest(state, request) {
  return state.busy === request && request.session === S.session;
}

function prepareJobList(state, history, kind, feed) {
  if (state.listHistory === history) return;
  state.listHistory = history;
  state.historyExpanded = false;
  state.pageCursors = new Set();
  state.paginationStalled = false;
  state.listErrors = {};
  state.after = null;
  $(feed).replaceChildren();
  $(kind + '-more').hidden = true;
  $(kind + '-list-error').hidden = true;
}

function mergeJobListPage(state, previous, page, nextAfter, { append, replace }) {
  if (replace) {
    state.historyExpanded = false;
    state.pageCursors.clear();
    state.paginationStalled = false;
  }
  if (append) {
    state.pageCursors.add(String(state.after));
    state.paginationStalled = nextAfter != null && state.pageCursors.has(String(nextAfter));
  }
  if (append || !state.historyExpanded) state.after = state.paginationStalled ? null : nextAfter ?? null;
  if (!append && !state.historyExpanded) return page;
  state.historyExpanded = true;
  const jobs = new Map((append ? previous : page).map(job => [job.id, job]));
  for (const job of (append ? page : previous)) {
    if (append || !jobs.has(job.id)) jobs.set(job.id, job);
  }
  return [...jobs.values()];
}

function updateJobListError(state, kind, refresh, { append, replace }, error) {
  const errors = state.listErrors;
  const previousPage = errors.page;
  const previousMessage = errors.page || errors.head;
  if (append) errors.page = error?.message || (state.paginationStalled ? '服务端返回了重复的分页位置，请重新加载。' : undefined);
  else {
    errors.head = error?.message;
    if (replace && !error) errors.page = undefined;
  }
  const message = errors.page || errors.head;
  if (message === previousMessage && errors.page === previousPage) return;
  const notice = $(kind + '-list-error');
  notice.hidden = !message;
  if (!message) {
    notice.replaceChildren();
    return;
  }
  const retryPage = Boolean(errors.page);
  notice.replaceChildren(
    h('span', { text: (retryPage ? '无法加载更多任务：' : '无法加载任务列表：') + message + ' ' }),
    h('button', { class: 'text-btn', type: 'button', text: '重试',
      onclick: () => retryPage && state.paginationStalled ? refresh(false, { replace: true }) : refresh(retryPage) })
  );
}

function updateRecentCustomJobs(data) {
  S.gen.recentJobs = data.jobs || [];
  setBadge('badge-generate', S.gen.recentJobs.filter(job => ACTIVE_CUSTOM.includes(job.status)).length);
}

async function refreshCustom(append = false, { replace = false, includeRecent = false } = {}) {
  const gen = S.gen;
  if (includeRecent && gen.history && !append) refreshRecentJobs(gen, '/api/custom', data => {
    updateRecentCustomJobs(data);
    window.Workspace?.onCustomJobsUpdated?.(data.jobs, data);
  });
  const request = beginJobListRequest(gen, append, replace);
  if (!request) return;
  prepareJobList(gen, request.history, 'gen', 'gen-feed');
  if (append) gen.historyExpanded = true;
  try {
    const query = request.history ? { history: 1, after: append ? gen.after : '' } : undefined;
    const data = await cloud('/api/custom', { query });
    if (!isCurrentJobListRequest(gen, request)) return;
    const quota = data.quota || {};
    $('gen-quota').textContent = quota.limited
      ? `5h ${quota.window_5h.used.toLocaleString()} / ${quota.window_5h.limit.toLocaleString()} · 本周 ${quota.week.used.toLocaleString()} / ${quota.week.limit.toLocaleString()} token`
      : '';
    gen.jobs = mergeJobListPage(gen, gen.jobs, data.jobs || [], data.next_after, { append, replace });
    updateJobListError(gen, 'gen', refreshCustom, { append, replace });
    $('gen-more').hidden = !gen.history || gen.after === null;
    const feed = $('gen-feed');
    feed.replaceChildren(...gen.jobs.map(jobCard));
    if (!gen.jobs.length) feed.replaceChildren(h('div', { class: 'empty', text: gen.history ? '暂无记录' : '暂无任务' }));
    if (!request.history) updateRecentCustomJobs(data);
    for (const id of gen.open) {
      if (!gen.pendingAttach.has(id) && data.jobs.some(j => j.id === id)) loadResults(id);
    }
    for (const [id, attachment] of gen.pendingAttach) {
      if (attachment.started || attachment.retryAt > Date.now()) continue;
      if (attachment.sourceMotionId) applyPendingAttachment(id, attachment, attachment.sourceMotionId);
      else loadResults(id);
    }
    window.Workspace?.onCustomJobsUpdated?.(data.jobs, data);
  } catch (error) {
    if (isCurrentJobListRequest(gen, request) && error.name !== 'AbortError') {
      updateJobListError(gen, 'gen', refreshCustom, { append, replace }, error);
    }
  } finally { if (isCurrentJobListRequest(gen, request)) gen.busy = false; }
}
window.refreshCustom = refreshCustom;

function jobCard(job) {
  const active = ACTIVE_CUSTOM.includes(job.status), waiting = ['queued', 'ready'].includes(job.status);
  const progress = h('div', { class: 'progress' + (waiting && !job.done ? ' indet' : '') }, h('i', { style: `width:${Math.round(100 * job.done / Math.max(1, job.n))}%` }));
  const results = h('div', { class: 'results', id: 'res-' + job.id, hidden: !S.gen.open.has(job.id) });
  const cached = S.gen.results.get(job.id); if (cached) results.replaceChildren(...cached.map(n => n.cloneNode ? n : n));
  const actions = h('div', { class: 'job-actions' },
    job.done > 0 ? h('button', { class: 'btn sm subtle', type: 'button', 'aria-expanded': String(S.gen.open.has(job.id)), onclick: () => toggleResults(job.id, job) }, `查看候选 (${job.done})`) : null,
    active && !job.cancel ? h('button', {
      class: 'btn sm danger', type: 'button', ...taskCancellationProps('custom:' + job.id),
      onclick: () => cancelCustom(job.id)
    }, '停止') : null,
    customRetryCount(job) > 0 ? h('button', { class: 'btn sm subtle', type: 'button', onclick: () => retryCustom(job) }, '重试') : null);
  return h('article', { class: 'job', dataset: { job: job.id } },
    h('div', { class: 'job-top' }, pill(CUSTOM, job.status), job.cancel && active ? h('span', { class: 'pill warn', text: '停止中' }) : null,
      h('span', { class: 'mono muted', text: `${job.done} / ${job.n}` }), h('span', { class: 'when', text: fmtDate(job.created) })),
    h('p', { class: 'job-desc', copy: '文本提示词', text: job.description }),
    job.prompt && job.prompt !== job.description ? h('p', { class: 'job-prompt', copy: '文本提示词', text: job.prompt }) : null,
    active ? progress : null,
    h('div', { class: 'job-meta' },
      h('span', { text: job.duration + ' s' }),
      job.queue_position ? h('span', { text: `${job.queue_phase === 'gpu' ? 'GPU' : '文本'} #${job.queue_position}` }) : null,
      S.user?.role === 'admin' && job.owner ? h('span', { text: job.owner }) : null),
    job.message ? h('p', { class: 'job-msg', text: job.message }) : null,
    actions, results);
}

function toggleResults(id, job) {
  const gen = S.gen;
  if (gen.open.has(id)) gen.open.delete(id);
  else {
    gen.open.add(id);
    loadResults(id, { showInStage: true, job });
  }
  const card = document.querySelector(`.job[data-job="${id}"]`);
  if (card) { card.querySelector('.results').hidden = !gen.open.has(id); }
}

let textPreviewVersion = 0;
function invalidateTextPreview() {
  textPreviewVersion++;
  window.SceneController?.cancelTextPreview?.();
  const loading = $('gen-preview-loading');
  if (loading) loading.hidden = true;
  return textPreviewVersion;
}
window.invalidateTextPreview = invalidateTextPreview;

async function previewGeneratedCandidate(motionId, job, allDoneItems = [], playback = null) {
  const scene = window.SceneController;
  const sceneView = scene?.view;
  if (!sceneView) { selectMotion(motionId, { reveal: true }); return; }
  const session = S.session;
  const tabId = window.Workspace?.state?.activeTabId;
  const version = ++textPreviewVersion;
  const current = () => version === textPreviewVersion && session === S.session
    && tabId === window.Workspace?.state?.activeTabId && document.body.dataset.stage === 'generate'
    && document.body.dataset.genModality !== 'video';
  if (!current()) return;
  scene.onTextPreviewStarted?.();
  const loading = $('gen-preview-loading');
  try {
    if (loading) loading.hidden = false;
    const decoded = await fullPreview(motionId);
    if (!current()) return;
    scene.setPreviewClip(decoded, { autoplay: true, playback });
    scene.onTextPreviewShown?.();
    if ($('scene-empty')) $('scene-empty').hidden = true;
    if ($('scene-result')) {
      $('scene-result').hidden = false;
      const qualityNote = decoded.scene?.quality
        ? (decoded.scene.quality.passed ? ' · 物体接触与几何筛查通过' : ' · 有接触或几何超差')
        : '';
      $('scene-quality').textContent = `${motionId} · ${decoded.meta.duration_s.toFixed(2)}s · ${decoded.meta.frames} 帧${qualityNote}`;
    }
    if ($('scene-markers') && typeof $('scene-markers').replaceChildren === 'function') {
      const jump = frame => { sceneView.setPlaying(false); sceneView.seek(frame); };
      $('scene-markers').replaceChildren(
        ...(decoded.scene?.targets || []).map(t => h('button', { type: 'button', class: 'btn sm subtle', text: '关键姿态 · 帧 ' + t.frame, onclick: () => jump(t.frame) })),
        ...(decoded.scene?.events || []).map(ev => h('button', { type: 'button', class: 'btn sm danger', text: ev.reason + ' · 帧 ' + ev.frame, onclick: () => jump(ev.frame) }))
      );
    }
    if ($('gen-viewport-mode-label')) $('gen-viewport-mode-label').textContent = '候选预览 · ' + motionId;
    window.Workspace?.setGenerateSelectedCandidate?.(job?.id || null, motionId);
    renderTextCandidateStrip(job, allDoneItems, motionId);
  } catch (error) {
    if (current() && error.name !== 'AbortError') toast(error.message, true);
  } finally {
    if (current()) {
      if (loading) loading.hidden = true;
      await scene.onTextPreviewSettled?.();
    }
  }
}
window.previewGeneratedCandidate = previewGeneratedCandidate;

function renderTextCandidateStrip(job, items, activeMotionId) {
  const strip = $('gen-candidates-strip'), box = $('gen-strip-items');
  if (!strip || !box) return;
  const doneItems = (items || []).filter(x => x.state === 'done');
  if (!doneItems.length) { strip.hidden = true; box.replaceChildren(); return; }
  strip.hidden = false;
  if ($('gen-strip-title')) $('gen-strip-title').textContent = `已完成候选 (${doneItems.length})`;
  if ($('gen-strip-meta')) $('gen-strip-meta').textContent = job?.description ? job.description.slice(0, 36) : '';
  box.replaceChildren(...doneItems.map((it, idx) => {
    const active = it.motion_id === activeMotionId;
    return h('div', { class: 'gen-cand-chip' + (active ? ' active' : '') },
      h('button', {
        type: 'button', class: 'text-btn', 'aria-pressed': String(active),
        text: `候选 ${idx + 1} · ${it.motion_id}`,
        onclick: () => previewGeneratedCandidate(it.motion_id, job, doneItems)
      }),
      h('button', {
        type: 'button', class: 'text-btn', text: '编辑与拼接',
        onclick: e => window.Workspace?.openMotionInTab('editor', it.motion_id, {
          label: job?.description || '', fromTask: job?.id, forceNew: e.altKey || e.shiftKey
        })
      }),
      h('button', {
        type: 'button', class: 'text-btn', text: '重定向',
        onclick: e => window.Workspace?.openMotionInTab('retarget', it.motion_id, {
          label: job?.description || '', fromTask: job?.id, forceNew: e.altKey || e.shiftKey
        })
      }),
      h('button', {
        type: 'button', class: 'text-btn', text: '下载',
        onclick: e => window.downloadMotion(it.motion_id, e.currentTarget)
      }));
  }));
}

async function loadResults(id, { showInStage = false, job = null, preferredMotionId = null, playback = null } = {}) {
  const items = []; let after = 0, pages = 0;
  const tabId = window.Workspace?.state?.activeTabId;
  const attachment = S.gen.pendingAttach.get(id);
  if (attachment?.loading) return;
  const previewVersion = showInStage ? invalidateTextPreview() : null;
  const currentStage = () => showInStage && previewVersion === textPreviewVersion &&
    tabId === window.Workspace?.state?.activeTabId && document.body.dataset.stage === 'generate' &&
    document.body.dataset.genModality !== 'video';
  if (attachment) attachment.loading = true;
  try {
    do { const r = await cloud('/api/custom/results', { query: { id, after } }); items.push(...r.items); after = r.next_after; pages++; } while (after !== null && pages < 5);
  } catch (error) {
    if (attachment && definitiveRejection(error)) finishPendingAttachment(id, attachment);
    if (currentStage() && error.name !== 'AbortError') toast('无法加载候选结果：' + error.message, true);
    else quiet(error);
    return;
  } finally {
    if (attachment) attachment.loading = false;
  }
  const jobObj = job || S.gen.jobs.find(j => j.id === id) || { id };
  S.gen.resultData.set(id, items);
  for (const it of items) {
    if (it.state === 'done') MotionLoader?.rememberMeta?.(it.motion_id, { label: jobObj.description || jobObj.prompt || '' });
  }
  const doneItems = items.filter(x => x.state === 'done');
  const chips = items.map(it => h('span', { class: 'result-item' },
    h('button', {
      class: 'result-chip', type: 'button', disabled: it.state !== 'done', title: '在当前生成标签页预览候选',
      onclick: () => previewGeneratedCandidate(it.motion_id, jobObj, doneItems)
    }, it.motion_id),
    it.state === 'done' ? h('button', {
      class: 'text-btn', type: 'button',
      onclick: e => window.Workspace?.openMotionInTab('editor', it.motion_id, { label: jobObj.description || '', forceNew: e.altKey })
    }, '编辑') : null,
    it.state === 'done' ? h('button', {
      class: 'text-btn', type: 'button',
      onclick: e => window.Workspace?.openMotionInTab('retarget', it.motion_id, { label: jobObj.description || '', forceNew: e.altKey })
    }, '重定向') : null,
    it.state === 'done' ? h('button', { class: 'text-btn', type: 'button', 'aria-label': '下载 ' + it.motion_id, onclick: e => window.downloadMotion(it.motion_id, e.currentTarget) }, '下载') : null));
  if (after !== null) chips.push(h('span', { class: 'muted', text: '…' }));
  S.gen.results.set(id, chips);
  const box = $('res-' + id); if (box) box.replaceChildren(...chips);
  if (attachment && S.gen.pendingAttach.get(id) === attachment) {
    const sourceMotionId = attachment.sourceMotionId || doneItems[0]?.motion_id;
    if (sourceMotionId) {
      await applyPendingAttachment(id, attachment, sourceMotionId);
      return;
    }
    if (['cancelled', 'failed'].includes(jobObj.status)) finishPendingAttachment(id, attachment);
  }
  if (currentStage() && doneItems.length && !window.SceneController?.isViewingSceneResult?.()) {
    const candidate = doneItems.find(item => item.motion_id === preferredMotionId) || doneItems[0];
    previewGeneratedCandidate(candidate.motion_id, jobObj, doneItems,
      candidate.motion_id === preferredMotionId ? playback : null);
  }
}

function finishPendingAttachment(jobId, attachment) {
  if (S.gen.pendingAttach.get(jobId) !== attachment) return;
  S.gen.pendingAttach.delete(jobId);
  window.Workspace?.flushSave?.();
}

async function applyPendingAttachment(jobId, attachment, sourceMotionId) {
  if (attachment.started || attachment.retryAt > Date.now()) return;
  attachment.started = true;
  if (!attachment.sourceMotionId) {
    attachment.sourceMotionId = sourceMotionId;
    window.Workspace?.flushSave?.();
  }
  const session = S.session;
  try {
    await window.SceneController.runObjectResponse(sourceMotionId, attachment.spec, {
      tabId: attachment.tabId, jobId, requestId: attachment.requestId
    });
    finishPendingAttachment(jobId, attachment);
  } catch (error) {
    if (session !== S.session) return;
    if (error.status === 0 || error.status === 408 || error.status >= 500) {
      if (!attachment.retryAt) toast('物体响应暂时未完成，稍后将自动重试', true);
      attachment.retryAt = Date.now() + 15000;
    } else {
      finishPendingAttachment(jobId, attachment);
      toast('物体响应绑定失败：' + error.message, true);
    }
  } finally {
    attachment.started = false;
  }
}
window.loadCustomResults = loadResults;
window.cancelCustomJob = cancelCustom;
window.retryCustomJob = retryCustom;

function refreshAfterCancellation(state, refresh) {
  state.recentRequest = null;
  return refresh(false, { replace: !state.history, includeRecent: true });
}

function taskCancellationProps(key, unavailable = false) {
  return {
    disabled: unavailable || S.taskCancellations.has(key),
    dataset: { taskCancel: key, taskCancelUnavailable: String(unavailable) }
  };
}
window.taskCancellationProps = taskCancellationProps;

function syncTaskCancellationControls(key) {
  for (const button of document.querySelectorAll('[data-task-cancel]')) {
    if (button.dataset.taskCancel === key) {
      button.disabled = S.taskCancellations.has(key) || button.dataset.taskCancelUnavailable === 'true';
    }
  }
}

async function runTaskCancellation(key, submit, onAccepted) {
  if (!S.user || S.taskCancellations.has(key)) return;
  const request = {};
  S.taskCancellations.set(key, request);
  syncTaskCancellationControls(key);
  let accepted = false;
  try {
    const result = await submit();
    if (S.taskCancellations.get(key) !== request) return;
    accepted = true;
    await onAccepted(result);
  } catch (error) {
    if (accepted) quiet(error);
    else if (S.taskCancellations.get(key) === request && error.name !== 'AbortError') {
      toast(error.message || '取消失败', true);
    }
  } finally {
    // Keep accepted cancellations disabled for this session even if the list is stale.
    if (!accepted && S.taskCancellations.get(key) === request) {
      S.taskCancellations.delete(key);
      syncTaskCancellationControls(key);
    }
  }
}
window.runTaskCancellation = runTaskCancellation;

function cancelCustom(id) {
  return runTaskCancellation('custom:' + id,
    () => cloud('/api/custom/cancel', { method: 'POST', body: { request_id: id } }),
    () => {
      toast('正在停止');
      return refreshAfterCancellation(S.gen, refreshCustom);
    });
}
function customRetryCount(job) {
  if (!['failed', 'cancelled'].includes(job.status)) return 0;
  const planned = Number(job.n);
  if (!Number.isInteger(planned) || planned < 1) return 0;
  const reported = Number(job.done);
  const finished = new Set();
  for (const item of S.gen.resultData.get(job.id) || []) {
    if (item.state === 'done' && item.motion_id) finished.add(item.motion_id);
  }
  return Math.max(0, planned - Math.max(finished.size, Number.isInteger(reported) ? reported : 0));
}
window.customRetryCount = customRetryCount;

function retryCustom(job) {
  const count = customRetryCount(job);
  if (!count) return;
  const settings = {
    description: job.description || '',
    count,
    duration: job.duration,
    model: job.prompt_mode === 'exact_english' ? 'exact_english' : (job.rewrite_model || ''),
    retryParent: job.id
  };
  if (window.Workspace) return window.Workspace.openGenerateRetry(settings);
  $('gen-text').value = settings.description;
  $('gen-count').value = settings.count;
  $('gen-duration').value = settings.duration;
  if (settings.model) $('gen-model').value = settings.model;
  S.gen.retryParent = job.id;
  updateChecks(); $('gen-text').focus();
}

// ---------------- Retarget ----------------
const robotView = new RobotView.View($('robot-canvas'), {
  model: robot => cloud('/api/retarget/model', { query: { robot } }),
  meshData: robot => cloud('/api/retarget/mesh-data', { query: { robot } })
});
const rtHumanView = new MotionView.View($('rt-human-canvas'), {});
window.robotView = robotView;
window.rtHumanView = rtHumanView;

let syncingRtScrub = false;
let pendingRetargetPlayback = null;
function retargetClipEnd(clip) {
  return clip ? (clip.frames - 1) / clip.fps : 0;
}

function retargetFrameAtTime(seconds, fps) {
  const frame = seconds * fps, whole = Math.round(frame);
  // Seconds round-trips can land a floating-point step below an exact frame.
  const precision = Number.EPSILON * Math.max(1, Math.abs(frame));
  return Math.abs(frame - whole) <= precision ? whole : frame;
}

function retargetTransportView() {
  const mode = S.rt.viewMode;
  if (mode === 'human') return rtHumanView;
  if (mode === 'robot' || !rtHumanView.clip) return robotView;
  return !robotView.clip || retargetClipEnd(rtHumanView.clip) > retargetClipEnd(robotView.clip) ? rtHumanView : robotView;
}

function retargetPlaybackState() {
  if (pendingRetargetPlayback) return pendingRetargetPlayback;
  const view = retargetTransportView();
  if (!view.clip) return null;
  return { seconds: Math.min(retargetClipEnd(view.clip), view.frame / view.clip.fps), playing: view.playing };
}
window.retargetPlaybackState = retargetPlaybackState;

function restoreRetargetPlayback(playback) {
  const view = retargetTransportView();
  if (!view.clip || !Number.isFinite(playback?.seconds)) return;
  view.seek(retargetFrameAtTime(playback.seconds, view.clip.fps));
  view.setPlaying(playback.playing === true && playback.seconds < retargetClipEnd(view.clip));
}

function restorePendingRetargetPlayback(playback) {
  if (!playback || pendingRetargetPlayback !== playback) return;
  restoreRetargetPlayback(playback);
  if (S.rt.viewMode !== 'compare' || (robotView.clip && rtHumanView.clip)) {
    pendingRetargetPlayback = null;
  }
}

function updateRetargetTransport() {
  if (syncingRtScrub) return;
  syncingRtScrub = true;
  try {
    const primary = retargetTransportView();
    const clip = primary.clip;
    const follower = primary === robotView ? rtHumanView : robotView;
    const comparing = S.rt.viewMode === 'compare';
    if (comparing && follower.playing) {
      follower.setPlaying(false);
      if (!primary.playing) primary.setPlaying(true);
    }
    const play = $('rt-play');
    const icon = play.querySelector('use');
    const finished = clip && !primary.playing && !primary.loop && primary.frame >= clip.frames - 1 &&
      icon.getAttribute('href') === '#i-pause';
    play.disabled = !clip;
    $('rt-scrub').disabled = !clip || clip.frames <= 1;
    icon.setAttribute('href', primary.playing ? '#i-pause' : '#i-play');
    if (!clip) {
      $('rt-time').textContent = '0.00 / 0.00 s';
      $('rt-scrub').max = 0; $('rt-scrub').value = 0;
      $('rt-scrub').style.setProperty('--p', '0%');
      return;
    }
    const end = retargetClipEnd(clip);
    const tSec = Math.min(end, primary.frame / clip.fps);
    if (comparing && follower.clip) follower.seek(retargetFrameAtTime(tSec, follower.clip.fps));
    const lastFrame = clip.frames - 1;
    const frame = Math.min(lastFrame, Math.floor(primary.frame));
    $('rt-scrub').max = lastFrame;
    $('rt-scrub').value = frame;
    $('rt-scrub').style.setProperty('--p', (100 * frame / Math.max(1, lastFrame)) + '%');
    $('rt-time').textContent = `${fmtTime(tSec)} / ${fmtTime(clip.duration_s ?? clip.frames / clip.fps)} s`;
    if (finished && document.body.dataset.stage === 'retarget') window.Workspace?.saveWorkspaceDebounced?.();
  } finally {
    syncingRtScrub = false;
  }
}

robotView.onFrame = updateRetargetTransport;
rtHumanView.onFrame = updateRetargetTransport;

function syncRetargetViewportActive() {
  if (document.body.dataset.stage !== 'retarget') {
    robotView.suspend?.();
    rtHumanView.suspend?.();
    return;
  }
  const mode = S.rt.viewMode;
  if (mode === 'robot') {
    robotView.resume?.();
    rtHumanView.suspend?.();
  } else if (mode === 'human') {
    robotView.suspend?.();
    rtHumanView.resume?.();
  } else {
    robotView.resume?.();
    rtHumanView.resume?.();
  }
}

function setRetargetViewMode(mode, { preservePlayback = false } = {}) {
  const nextMode = ['robot', 'human', 'compare'].includes(mode) ? mode : 'robot';
  if (preservePlayback && nextMode === S.rt.viewMode) return;
  const playback = preservePlayback ? retargetPlaybackState() : null;
  pendingRetargetPlayback = null;
  S.rt.viewMode = nextMode;
  $('stage-retarget').dataset.rtMode = S.rt.viewMode;
  $('rt-mode-robot').setAttribute('aria-pressed', String(S.rt.viewMode === 'robot'));
  $('rt-mode-human').setAttribute('aria-pressed', String(S.rt.viewMode === 'human'));
  $('rt-mode-compare').setAttribute('aria-pressed', String(S.rt.viewMode === 'compare'));
  $('rt-human-pane').hidden = S.rt.viewMode === 'robot';
  $('rt-robot-pane').hidden = S.rt.viewMode === 'human';
  $('rt-robot-head').hidden = S.rt.viewMode !== 'compare';
  if (playback) {
    // Suspend and seek without letting the old follower drive the new timeline.
    syncingRtScrub = true;
    try {
      robotView.setPlaying(false);
      rtHumanView.setPlaying(false);
      syncRetargetViewportActive();
      restoreRetargetPlayback(playback);
    } finally {
      syncingRtScrub = false;
    }
  } else {
    syncRetargetViewportActive();
  }
  if (nextMode !== 'robot') {
    const playbackOnLoad = nextMode === 'human' && !rtHumanView.clip ? playback : null;
    ensureRetargetHumanLoaded(null, playbackOnLoad);
  }
  updateRetargetTransport();
  window.Workspace?.saveWorkspaceDebounced?.();
}
window.setRetargetViewMode = setRetargetViewMode;

for (const mode of ['robot', 'human', 'compare']) {
  $('rt-mode-' + mode)?.addEventListener('click', () => setRetargetViewMode(mode, { preservePlayback: true }));
}

$('rt-play').addEventListener('click', () => {
  pendingRetargetPlayback = null;
  const primary = retargetTransportView();
  primary.setPlaying(!primary.playing);
  window.Workspace?.saveWorkspaceDebounced?.();
});

function seekRetargetFrame(primary, frame) {
  pendingRetargetPlayback = null;
  primary.setPlaying(false);
  primary.seek(frame);
  window.Workspace?.saveWorkspaceDebounced?.();
}

$('rt-scrub').addEventListener('input', e => {
  seekRetargetFrame(retargetTransportView(), Number(e.target.value));
});
$('rt-speed')?.addEventListener('change', e => {
  const sp = Number(e.target.value) || 1;
  robotView.speed = sp;
  rtHumanView.speed = sp;
  window.Workspace?.saveWorkspaceDebounced?.();
});
$('rt-loop')?.addEventListener('click', e => {
  const next = !robotView.loop;
  robotView.loop = next;
  rtHumanView.loop = next;
  e.currentTarget.setAttribute('aria-pressed', String(next));
  window.Workspace?.saveWorkspaceDebounced?.();
});
$('rt-reset').addEventListener('click', () => {
  robotView.resetCamera();
  rtHumanView.resetCamera();
});

let rtHumanLoadedId = '', rtHumanRequest = 0;
function clearRetargetPlayback() {
  pendingRetargetPlayback = null;
  S.rt.previewRequest++;
  S.rt.playing = null;
  S.rt.playingJob = null;
  S.rt.retryPreview = null;
  rtHumanRequest++;
  rtHumanLoadedId = '';
  robotView.clear();
  rtHumanView.clear();
  $('rt-acceptance-bar').hidden = true;
  $('rt-robot-caption').textContent = '';
  $('rt-human-caption').textContent = '';
  viewportMessage('rt-human-msg', '选择源动作后在此预览');
  viewportMessage('rt-msg', '提交重定向任务或点击右侧已完成任务播放');
  updateRetargetTransport();
}
window.clearRetargetPlayback = clearRetargetPlayback;
async function ensureRetargetHumanLoaded(explicitId, playback = null) {
  const id = explicitId || S.rt.playingJob?.motion_id || $('rt-motion').value.trim();
  const request = ++rtHumanRequest;
  if (!id || !MotionLoader.isValidMotionId(id)) {
    rtHumanLoadedId = '';
    rtHumanView.clear();
    $('rt-human-caption').textContent = '';
    viewportMessage('rt-human-msg', '选择有效源动作后预览人体骨架');
    updateRetargetTransport();
    return;
  }
  if (rtHumanLoadedId === id && rtHumanView.clip) {
    if (playback && S.rt.viewMode !== 'robot') restoreRetargetPlayback(playback);
    return;
  }
  if (rtHumanLoadedId) {
    rtHumanLoadedId = '';
    rtHumanView.clear();
    $('rt-human-caption').textContent = '';
    updateRetargetTransport();
  }
  const session = S.session;
  viewportMessage('rt-human-msg', '正在载入源人体动作…', 'loading');
  try {
    const decoded = await fullPreview(id);
    if (session !== S.session || request !== rtHumanRequest) return;
    rtHumanLoadedId = id;
    const robotClip = S.rt.viewMode === 'compare' ? robotView.clip : null;
    const startFrame = robotClip
      ? Math.min(retargetClipEnd(robotClip), robotView.frame / robotClip.fps) * decoded.meta.fps
      : 0;
    rtHumanView.setClip(decoded, false, { startFrame });
    if (playback && S.rt.viewMode !== 'robot') restoreRetargetPlayback(playback);
    else if (S.rt.viewMode === 'compare') restorePendingRetargetPlayback(pendingRetargetPlayback);
    if ($('rt-human-caption')) $('rt-human-caption').textContent = id;
    viewportMessage('rt-human-msg', null);
    updateRetargetTransport();
  } catch (error) {
    if (session === S.session && request === rtHumanRequest && error.name !== 'AbortError') {
      $('rt-human-retry').onclick = () => {
        if (session === S.session && request === rtHumanRequest) ensureRetargetHumanLoaded(id, playback);
      };
      viewportMessage('rt-human-msg', error.message, 'error');
    }
  }
}
window.ensureRetargetHumanLoaded = ensureRetargetHumanLoaded;

function renderRobots(robots) {
  const list = $('robot-list');
  if (JSON.stringify(robots) === JSON.stringify(S.robots) && list.children.length) return;
  S.robots = robots;
  list.replaceChildren(...robots.map(r => h('label', { class: 'robot' },
    h('input', {
      type: 'radio', name: 'robot', value: r.id, required: true, checked: S.rt.robot === r.id,
      onchange: () => { S.rt.robot = r.id; syncRetargetSubmission(); window.Workspace?.onRetargetConfigChanged?.(); }
    }),
    h('div', {}, h('strong', { text: r.label }), r.note ? h('small', { text: r.note }) : null),
    h('span', { class: 'dof', text: r.dof && !String(r.label).includes(r.dof + ' DoF') ? r.dof + ' DoF' : '' }))));
  if (!robots.length) list.replaceChildren(h('p', { class: 'muted', text: '暂无机器人' }));
  syncRetargetSubmission();
  window.SceneController?.onRobotsLoaded?.();
}

function syncRetargetSource() {
  const id = $('rt-motion').value.trim();
  if (S.rt.playingJob && S.rt.playingJob.motion_id !== id) clearRetargetPlayback();
  const snap = S.rt.sourceSnapshot?.id === id ? S.rt.sourceSnapshot : null;
  const cached = MotionLoader?.getCachedMeta?.(id);
  const known = S.lib.items.find(i => i.motion_id === id), detail = S.lib.detail?.motion_id === id ? S.lib.detail : null;
  const text = snap?.label || detail?.prompt?.text || known?.text || cached?.label || '';
  $('rt-source').textContent = text;
  const lineageEl = $('rt-source-lineage');
  if (lineageEl) {
    const parentId = snap?.parentId || detail?.parent_id || cached?.parent_id || null;
    lineageEl.hidden = !parentId && !MotionLoader?.isManagedAsset?.(id);
    lineageEl.textContent = parentId ? `来源版本链：源自 ${parentId}` : (MotionLoader?.isManagedAsset?.(id) ? '不可变编辑/场景资产版本' : '');
  }
  if (S.rt.viewMode !== 'robot') ensureRetargetHumanLoaded(id);
  window.syncRetargetSubmission?.();
  window.Workspace?.onRetargetConfigChanged?.();
}
window.syncRetargetSource = syncRetargetSource;

function syncRetargetSubmission() {
  const { tabKey, robot, submittingTabs, submissionErrors } = S.rt;
  const motion = $('rt-motion').value.trim();
  const quarantined = MotionLoader.getCachedMeta(motion)?.status === 'quarantined';
  const unavailableRobot = Boolean(robot) && !S.robots.some(item => item.id === robot);
  const disabled = submittingTabs.has(tabKey) || quarantined || unavailableRobot;
  $('rt-submit').disabled = disabled;
  const error = submissionErrors.get(tabKey);
  const matchesForm = error && error.motion === motion
    && error.robot === robot && error.raw === $('rt-params').value.trim();
  let message = matchesForm ? error.message : '';
  if (unavailableRobot) message = '所选机器人当前不可用，请重新选择目标机器人。';
  if (quarantined) message = '隔离候选无法提交重定向，请先修正动作。';
  const notice = $('rt-error');
  if (notice.textContent !== message) notice.textContent = message;
  return disabled;
}
window.syncRetargetSubmission = syncRetargetSubmission;

$('rt-motion').addEventListener('input', syncRetargetSource);
$('rt-params')?.addEventListener('input', () => { syncRetargetSubmission(); window.Workspace?.syncTabDirty?.(); });
$('rt-use-current').addEventListener('click', () => {
  if (!S.lib.selected) { toast('请先在左侧动作库中选择动作', true); return; }
  window.Workspace?.applyMotionToRetarget(S.lib.selected) || (($('rt-motion').value = S.lib.selected), syncRetargetSource());
});

$('rt-form').addEventListener('submit', async e => {
  e.preventDefault();
  const rt = S.rt, session = S.session, motion = $('rt-motion').value.trim(), robot = S.rt.robot;
  const tabId = rt.tabKey;
  if (syncRetargetSubmission()) return;
  const raw = $('rt-params').value.trim();
  const reportError = message => {
    if (!window.Workspace || window.Workspace.state.tabs.some(tab => tab.id === tabId)) {
      rt.submissionErrors.set(tabId, { motion, robot, raw, message });
    }
    syncRetargetSubmission();
  };
  rt.submissionErrors.delete(tabId);
  if (!/^(m[0-9a-z]{8,}|ma_[0-9a-f]{32})$/.test(motion)) { reportError('动作 ID 格式不正确（支持 m… 或 ma_…）。'); return; }
  if (!robot) { reportError('请选择目标机器人'); return; }
  const execution = {};
  if (raw) {
    try {
      const parameters = JSON.parse(raw, (_, value) => {
        if (typeof value === 'number' && !Number.isFinite(value)) throw new Error();
        return value;
      });
      if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new Error();
      execution.parameters = parameters;
    } catch {
      reportError('参数需为 JSON 对象，数值必须为有限数。');
      return;
    }
  }
  const submitKey = tabId && tabId !== 'default' ? 'retarget:' + tabId : 'retarget';
  const refreshAfterSubmit = jobListAfterSubmit('rt', refreshRetarget);
  rt.submittingTabs.add(tabId);
  syncRetargetSubmission();
  try {
    const selectedRobot = S.robots.find(item => item.id === robot);
    const rawRobotVersion = robot.startsWith('robot_')
      ? (selectedRobot?.robot_version || selectedRobot?.version || selectedRobot?.latest_profile || rt.robotVersion || rt.robot_version || '')
      : '';
    const robotVersion = typeof rawRobotVersion === 'string' ? rawRobotVersion.trim() : '';
    const body = { motion_id: motion, robot, ...execution };
    if (robotVersion) body.robot_version = robotVersion;
    const created = await submitOnce(submitKey, '/api/retarget', body, 'id');
    if (session !== S.session) return;
    window.Workspace?.recordRetargetSubmission?.(tabId, raw, created?.id, robot, motion);
    toast('已提交重定向任务');
    await refreshAfterSubmit();
  } catch (error) {
    if (session === S.session) {
      const message = error.message || '重定向提交失败，请重试';
      reportError(message);
      if (window.Workspace && tabId !== window.Workspace.state.activeTabId) toast('重定向提交失败：' + message, true);
    }
  }
  finally {
    if (session === S.session) {
      rt.submittingTabs.delete(tabId);
      syncRetargetSubmission();
    }
  }
});
$('rt-live').addEventListener('click', () => chooseJobList('rt', false, refreshRetarget));
$('rt-history').addEventListener('click', () => chooseJobList('rt', true, refreshRetarget));
$('rt-more').addEventListener('click', () => refreshRetarget(true));

function updateRecentRetargetJobs(data) {
  S.rt.recentJobs = data.items || [];
  setBadge('badge-retarget', (data.running || 0) + (data.queued || 0));
}

function retargetSourceKey(job) {
  return JSON.stringify([job.owner || S.user?.username, job.motion_id]);
}

function rememberRetargetQuarantine(items) {
  const sources = S.rt.quarantinedSources;
  const previousCount = sources.size;
  // An isolated source version stays isolated. A correction has its own motion ID.
  for (const job of items) {
    if (job.source_quarantined === true) sources.add(retargetSourceKey(job));
  }
  return sources.size !== previousCount;
}

function isRetargetSourceQuarantined(job) {
  return !!job && (job.source_quarantined === true || S.rt.quarantinedSources.has(retargetSourceKey(job)));
}

function renderRetargetRows(history) {
  const box = $('rt-jobs');
  const focused = document.activeElement;
  const focusedJob = focused && box.contains(focused) ? focused.closest('[data-retarget-job]')?.dataset.retargetJob : null;
  box.replaceChildren(...S.rt.items.map(rtCard));
  if (!S.rt.items.length) box.replaceChildren(h('div', { class: 'empty', text: '暂无作业' }));
  if (focusedJob) {
    const row = [...box.children].find(child => child.dataset.retargetJob === focusedJob);
    const action = focused.dataset.retargetAction;
    const sameControl = action && [...(row?.querySelectorAll('[data-retarget-action]:not(:disabled)') || [])]
      .find(control => control.dataset.retargetAction === action);
    const target = sameControl || row?.querySelector('button[data-retarget-action]:not(:disabled)')
      || $(history ? 'rt-history' : 'rt-live');
    target.focus({ preventScroll: true });
  }
}

function updatePlayingRetargetJob(items) {
  const rt = S.rt;
  if (!rt.playing) return;
  const job = items.find(item => item.id === rt.playing);
  const unavailable = unavailableRetargetResult(job);
  if (unavailable) {
    clearRetargetPlayback();
    viewportMessage('rt-msg', RETARGET[unavailable][0]);
    return;
  }
  if (job) rt.playingJob = job;
  if (job || isRetargetSourceQuarantined(rt.playingJob)) renderRetargetAcceptanceBar(rt.playingJob);
}

async function refreshRetarget(append = false, { replace = false, includeRecent = false } = {}) {
  const rt = S.rt;
  const historyBeforeRecent = rt.items;
  if (includeRecent && rt.history && !append) refreshRecentJobs(rt, '/api/retarget', data => {
    updateRecentRetargetJobs(data);
    const learnedQuarantine = rememberRetargetQuarantine(rt.recentJobs);
    if (rt.listHistory && $('rt-jobs').querySelector('.rt-job')) {
      let updated = false;
      if (rt.items !== historyBeforeRecent) {
        syncRecentRetargetRows(rt.items);
      } else {
        const recent = new Map(rt.recentJobs.map(job => [job.id, job]));
        updated = rt.items.some(job => recent.has(job.id));
        if (updated) rt.items = rt.items.map(job => recent.get(job.id) || job);
      }
      if (learnedQuarantine || updated) renderRetargetRows(true);
    }
    updatePlayingRetargetJob(rt.recentJobs);
    window.Workspace?.onRetargetJobsUpdated?.(data.items, data);
  });
  const request = beginJobListRequest(rt, append, replace);
  if (!request) return;
  prepareJobList(rt, request.history, 'rt', 'rt-jobs');
  if (append) rt.historyExpanded = true;
  const watchedId = rt.playing;
  try {
    const query = request.history ? { history: 1, after: append ? rt.after : '' } : {};
    if (watchedId) query.watch = watchedId;
    const data = await cloud('/api/retarget', { query });
    if (!isCurrentJobListRequest(rt, request)) return;
    renderRobots(data.robots || []);
    $('rt-dispatch-notice').textContent = data.dispatch_notice || '';
    $('rt-dispatch-notice').hidden = !data.dispatch_notice;
    const page = data.items || [];
    const watched = data.watched?.id === watchedId ? data.watched : null;
    const updated = watched ? [...page, watched] : page;
    rememberRetargetQuarantine(updated);
    rt.items = mergeJobListPage(rt, rt.items, page, data.next_after, { append, replace });
    if (watched) rt.items = rt.items.map(job => job.id === watched.id ? watched : job);
    updateJobListError(rt, 'rt', refreshRetarget, { append, replace });
    $('rt-more').hidden = !rt.history || rt.after === null;
    if (!request.history) updateRecentRetargetJobs(data);
    syncRecentRetargetRows(updated);
    renderRetargetRows(request.history);
    updatePlayingRetargetJob(updated);
    window.Workspace?.onRetargetJobsUpdated?.(updated, data);
  } catch (error) {
    if (isCurrentJobListRequest(rt, request) && error.name !== 'AbortError') {
      updateJobListError(rt, 'rt', refreshRetarget, { append, replace }, error);
    }
  } finally { if (isCurrentJobListRequest(rt, request)) rt.busy = false; }
}
window.refreshRetarget = refreshRetarget;

function syncRecentRetargetRows(rows) {
  if (!S.rt.recentJobs?.length) return;
  const history = new Map(rows.map(job => [job.id, job]));
  S.rt.recentJobs = S.rt.recentJobs.map(job => history.get(job.id) || job);
}

function unavailableRetargetResult(job) {
  if (job?.source_removed) return 'source_removed';
  if (job?.result_missing) return 'result_missing';
  if (job?.result_corrupt) return 'result_corrupt';
  return null;
}

function retargetAcceptance(job) {
  const unavailable = unavailableRetargetResult(job);
  if (unavailable) return unavailable;
  if (job.status !== 'done') return null;
  if (isRetargetSourceQuarantined(job)) return 'source_quarantined';
  if (job.export_verified === true) return 'done';
  return job.export_verified === false ? 'unverified' : 'unconfirmed';
}

function renderRetargetAcceptanceBar(job) {
  const bar = $('rt-acceptance-bar');
  if (!bar || typeof bar.replaceChildren !== 'function') return;
  if (!job) { bar.hidden = true; bar.replaceChildren(); return; }
  const focused = document.activeElement;
  const restoreFocus = focused && bar.contains(focused);
  const sameJob = bar.dataset.retargetJob === job.id;
  bar.dataset.retargetJob = job.id;
  bar.hidden = false;
  const acceptance = retargetAcceptance(job);
  const verified = acceptance === 'done';
  const unverifiedDone = acceptance === 'unverified';
  const sourceQuarantined = acceptance === 'source_quarantined';
  bar.replaceChildren(...[
    verified ? pill({ ok: ['验收通过 · 可正式导出', 'ok'] }, 'ok')
      : pill(RETARGET, acceptance || (job.repair_available ? 'repair' : job.status)),
    h('span', { class: 'mono muted small', text: `源版本: ${job.motion_id}` + (job.source_info?.parent_id ? ` (源自 ${job.source_info.parent_id.slice(0, 10)}…)` : '') }),
    sourceQuarantined ? h('span', { class: 'muted small', text: RETARGET_SOURCE_QUARANTINE_REASON }) : null,
    verified ? retargetDownloadButton(job) : null,
    !unavailableRetargetResult(job) && (unverifiedDone || job.repair_available || (job.status === 'done' && job.owner === S.user?.username))
      ? h('button', { class: 'text-btn', type: 'button', dataset: { retargetAction: 'edit' },
        onclick: () => window.openRobotEditor(job.id) }, unverifiedDone ? '进入精修重新验收' : '精修与检查')
      : null
  ].filter(Boolean));
  if (restoreFocus) {
    const buttons = sameJob ? [...bar.querySelectorAll('button:not(:disabled)')] : [];
    const target = buttons.find(button => button.dataset.retargetAction === focused.dataset.retargetAction)
      || buttons[0] || $('rt-view-modes').querySelector('[aria-pressed="true"]');
    target?.focus({ preventScroll: true });
  }
}

function rtCard(job) {
  const label = S.robots.find(r => r.id === job.robot)?.label || job.robot;
  const active = ACTIVE_RT.includes(job.status);
  const cancellation = active ? taskCancellationProps('retarget:' + job.id, job.status === 'stopping') : null;
  const acceptance = retargetAcceptance(job);
  const unverifiedDone = acceptance === 'unverified';
  const sourceAvailable = acceptance !== 'source_removed';
  const done = job.status === 'done' && !unavailableRetargetResult(job);
  const repair = sourceAvailable && job.repair_available;
  const jobMessage = acceptance === 'source_quarantined' ? RETARGET_SOURCE_QUARANTINE_REASON :
    job.repair_warning || (unverifiedDone || acceptance === 'result_corrupt' ? job.export_reason : job.message);
  const statusKey = repair ? 'repair' : (acceptance || job.status);
  const parentNote = job.source_info?.parent_id ? ` · 源自 ${job.source_info.parent_id.slice(0, 10)}…` : '';
  return h('article', { class: 'rt-job' + (S.rt.playing === job.id ? ' playing' : ''), dataset: { retargetJob: job.id } },
    h('div', { class: 'job-top' }, pill(RETARGET, statusKey), h('span', { class: 'mono', text: job.motion_id, copy: '源动作版本', dataset: { retargetAction: 'copy-source' } }), h('span', { class: 'when', text: fmtDate(job.created) })),
    h('div', { class: 'job-meta' }, h('span', { text: '→ ' + label + parentNote }), job.status === 'queued' && job.queue_position ? h('span', { text: '#' + job.queue_position }) : null, S.user?.role === 'admin' ? h('span', { text: job.owner }) : null),
    active ? h('div', { class: 'progress' + (job.status === 'queued' ? ' indet' : '') }, h('i', { style: `width:${Math.round(100 * (job.done || 0) / Math.max(1, job.total || 1))}%` })) : null,
    jobMessage ? h('p', { class: 'job-msg', text: jobMessage }) : null,
    h('div', { class: 'job-actions' },
      done ? h('button', { class: 'btn sm primary', type: 'button', dataset: { retargetAction: 'preview' }, onclick: () => playRetarget(job) }, '预览') : null,
      done ? h('button', { class: 'btn sm subtle', type: 'button', dataset: { retargetAction: 'compare' }, onclick: () => { setRetargetViewMode('compare'); playRetarget(job); } }, '并排对比') : null,
      (repair || (done && job.owner === S.user?.username)) ? h('button', { class: 'btn sm subtle', type: 'button', dataset: { retargetAction: 'edit' }, onclick: () => window.openRobotEditor(job.id) }, repair || unverifiedDone ? '精修验收' : '精修与检查') : null,
      repair ? retargetDownloadButton(job, true) : null,
      job.status === 'failed' && sourceAvailable ? h('button', { class: 'btn sm subtle', type: 'button', dataset: { retargetAction: 'download-source' }, onclick: e => window.downloadMotion(job.motion_id, e.currentTarget) }, '下载源动作') : null,
      acceptance === 'done' ? retargetDownloadButton(job) : null,
      active ? h('button', {
        class: 'btn sm danger', type: 'button', ...cancellation,
        dataset: { ...cancellation.dataset, retargetAction: 'cancel' },
        onclick: () => cancelRetarget(job.id)
      }, '取消') : null,
      sourceAvailable ? h('button', { class: 'btn sm ghost', type: 'button', dataset: { retargetAction: 'open-source' }, onclick: () => selectMotion(job.motion_id, { reveal: true }) }, '查看源动作') : null));
}

async function playRetarget(job, { frame = 0, playback = null } = {}) {
  const unavailable = unavailableRetargetResult(job);
  if (unavailable) {
    clearRetargetPlayback();
    viewportMessage('rt-msg', RETARGET[unavailable][0]);
    return;
  }
  const session = S.session, tabId = S.rt.tabKey, request = ++S.rt.previewRequest;
  const isCurrent = () => S.session === session && S.view === 'retarget' &&
    S.rt.tabKey === tabId && S.rt.previewRequest === request;
  S.rt.retryPreview = null;
  pendingRetargetPlayback = playback;
  robotView.clear();
  robotView.frame = frame;
  S.rt.playing = job.id;
  S.rt.playingJob = job;
  rememberRetargetQuarantine([job]);
  window.Workspace?.saveWorkspaceDebounced?.();
  if ($('rt-robot-caption')) $('rt-robot-caption').textContent = (S.robots.find(r => r.id === job.robot)?.label || job.robot) + ' · ' + job.id.slice(0, 8);
  renderRetargetAcceptanceBar(job);
  viewportMessage('rt-msg', '正在加载机器人预览…', 'loading');
  for (const c of document.querySelectorAll('.rt-job')) c.classList.remove('playing');
  const humanLoading = S.rt.viewMode !== 'robot' ? ensureRetargetHumanLoaded(job.motion_id) : null;
  try {
    if (String(job.robot).startsWith('robot_')) {
      pendingRetargetPlayback = null;
      viewportMessage('rt-msg', '自定义机器人请点击“精修与检查”查看实体关节姿态预览');
      refreshRetarget();
      return;
    }
    const clip = await cloud('/api/retarget/mesh-preview', { query: { id: job.id } });
    if (!isCurrent()) return;
    const loaded = await robotView.load(clip, {
      getPlayback: playback ? () => {
        if (pendingRetargetPlayback === playback || S.rt.viewMode !== 'compare' || !rtHumanView.clip) {
          return { frame: 0, playing: false };
        }
        const seconds = rtHumanView.frame / rtHumanView.clip.fps;
        return { frame: retargetFrameAtTime(seconds, clip.fps), playing: rtHumanView.playing };
      } : null
    });
    if (!loaded || !isCurrent()) return;
    if (playback && pendingRetargetPlayback === playback) {
      retargetTransportView().setPlaying(false);
      await humanLoading;
      if (!isCurrent()) return;
      restorePendingRetargetPlayback(playback);
    } else if (!playback && frame) {
      const primary = retargetTransportView();
      if (S.rt.viewMode === 'compare') primary.seek(retargetFrameAtTime(frame / clip.fps, primary.clip.fps));
      else robotView.seek(frame);
    }
    if (!clip.scene?.objects && String(job.motion_id).startsWith('ma_')) {
      try {
        const preview = await cloud(`/api/harness/motions/${encodeURIComponent(job.motion_id)}/preview`);
        if (isCurrent()) robotView.setSceneObjects?.(preview?.scene?.objects || []);
      } catch (_) {}
    }
    if (!isCurrent()) return;
    viewportMessage('rt-msg', null);
    refreshRetarget();
  } catch (error) {
    if (!isCurrent()) return;
    S.rt.retryPreview = () => {
      if (isCurrent()) return playRetarget(job, { frame, playback: retargetPlaybackState() });
    };
    viewportMessage('rt-msg', error.message, 'error');
  }
}
window.playRetarget = playRetarget;
$('rt-robot-retry').onclick = () => S.rt.retryPreview?.();
window.cancelRetargetJob = cancelRetarget;

function retargetDownloadButton(job, repair = false) {
  const key = (repair ? 'repair:' : 'result:') + job.id;
  return h('button', {
    class: 'btn sm subtle', type: 'button', disabled: S.rt.downloads.has(key),
    dataset: { retargetDownload: key, retargetAction: repair ? 'download-repair' : 'download-result' },
    onclick: () => downloadRetarget(job, key, repair)
  }, repair ? '下载待修复 NPZ' : '导出 NPZ');
}

async function downloadRetarget(job, key, repair) {
  const downloads = S.rt.downloads, session = S.session;
  if (!S.user || downloads.has(key)) return;
  if (!repair && isRetargetSourceQuarantined(job)) {
    toast(RETARGET_SOURCE_QUARANTINE_REASON, true);
    return;
  }
  const syncControls = disabled => {
    for (const button of document.querySelectorAll('[data-retarget-download]')) {
      if (button.dataset.retargetDownload === key) button.disabled = disabled;
    }
  };
  downloads.add(key);
  syncControls(true);
  try {
    const result = await downloadFile({
      path: repair ? '/api/retarget/repair-download' : '/api/retarget/download',
      query: { id: job.id },
      name: `${job.motion_id}_${job.robot}_${repair ? 'needs-repair' : 'reference'}.npz`
    });
    if (session !== S.session) return;
    if (result?.saved) toast('已保存 ' + result.saved); else if (result?.error) toast(result.error, true);
  } catch (error) {
    if (session === S.session) toast(error.message || '下载失败，请重试', true);
  } finally {
    if (session === S.session) {
      downloads.delete(key);
      syncControls(false);
    }
  }
}
function cancelRetarget(id) {
  return runTaskCancellation('retarget:' + id,
    () => cloud('/api/retarget/cancel', { method: 'POST', body: { id } }),
    job => {
      if (job.status === 'done') toast('重定向已完成，无法取消');
      else if (job.status === 'failed') toast('重定向已结束，无法取消');
      else if (job.status === 'cancelled') toast('重定向已取消');
      else if (job.status === 'stopping') toast('正在取消重定向');
      return refreshAfterCancellation(S.rt, refreshRetarget);
    });
}

// ---------------- Polling ----------------
let pollTimer = null, pollTick = 0;
function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (!S.user || document.hidden) return;
    pollTick++;
    if (!S.online) { cloud('/api/auth/me').catch(quiet); return; }
    if (S.view === 'generate' || pollTick % 2 === 0) refreshCustom(false, { includeRecent: true });
    if (S.view === 'retarget' || pollTick % 2 === 1) refreshRetarget(false, { includeRecent: true });
    window.Workspace?.pollHarnessTasks?.(pollTick);
  }, 4500);
}
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }
function setBadge(id, n) { const b = $(id); if (!b) return; b.hidden = !n; b.textContent = n > 99 ? '99+' : String(n); }
$('banner-retry').addEventListener('click', () => cloud('/api/auth/me').catch(quiet));

// ---------------- Login / account ----------------
let credentialRevision = 0;
for (const id of ['login-user', 'login-pass', 'login-remember']) {
  $(id).addEventListener('input', () => { credentialRevision++; });
}
async function restoreCredentials() {
  const session = S.session;
  const revision = ++credentialRevision;
  if (!window.desktop.loadCredentials || $('login-forget').disabled) return;
  $('credential-status').textContent = '';
  try {
    const saved = await window.desktop.loadCredentials();
    if (session !== S.session || revision !== credentialRevision || $('gate-login').hidden || $('login-pass').value || ($('login-user').value && $('login-user').value !== saved?.username)) return;
    if (saved) {
      $('login-user').value = saved.username; $('login-pass').value = saved.password;
      $('login-remember').checked = true;
      $('credential-status').textContent = '已从系统凭据库填入';
    }
  } catch (_) {
    if (session === S.session && revision === credentialRevision && !$('gate-login').hidden) {
      $('credential-status').textContent = '系统凭据库暂不可用，请手动输入';
    }
  }
}
$('login-forget').addEventListener('click', async () => {
  const button = $('login-forget');
  const submit = $('login-submit');
  if (button.disabled || submit.disabled) return;
  const session = S.session;
  button.disabled = submit.disabled = true;
  const revision = ++credentialRevision;
  $('login-remember').checked = false;
  $('credential-status').textContent = '正在从系统凭据库删除…';
  try {
    await window.desktop.forgetCredentials();
    if (session !== S.session) return;
    if (revision === credentialRevision && !$('gate-login').hidden) {
      $('login-user').value = ''; $('login-pass').value = '';
    }
    if (!$('gate-login').hidden) $('credential-status').textContent = '已删除本机保存的账号密码';
  } catch (_) {
    if (session === S.session && !$('gate-login').hidden) $('credential-status').textContent = '删除失败，请解锁系统凭据库后重试';
  } finally {
    button.disabled = submit.disabled = false;
  }
});

$('gate-login').addEventListener('submit', async e => {
  e.preventDefault();
  const submit = $('login-submit'), forget = $('login-forget');
  if (submit.disabled || forget.disabled) return;
  const session = S.session;
  credentialRevision++;
  const credentials = { username: $('login-user').value.trim(), password: $('login-pass').value };
  const remember = $('login-remember').checked;
  submit.disabled = forget.disabled = true;
  $('login-error').textContent = '';
  try {
    const { user } = await cloud('/api/auth/login', { method: 'POST', body: credentials });
    if (session !== S.session) return;
    try {
      if (remember) await window.desktop.saveCredentials(credentials);
      else await window.desktop.forgetCredentials();
    } catch (_) { if (session === S.session) toast('登录成功，但系统凭据库更新失败'); }
    if (session !== S.session) return;
    $('login-pass').value = ''; await enter(user);
  } catch (error) {
    if (session !== S.session) return;
    $('login-pass').value = '';
    $('login-error').textContent = error.message;
  } finally { submit.disabled = forget.disabled = false; }
});
$('offline-retry').addEventListener('click', () => {
  if (!S.status || S.status.serverPreferenceUnavailable) window.location.reload();
  else boot();
});

function openPassword(forced) {
  window.resetGadget?.();
  const d = $('password'); if (d.open) return;
  $('password-reason').hidden = !forced;
  d.dataset.forced = forced ? '1' : '';
  d.querySelector('[data-close]').hidden = !!forced;
  $('pw-error').textContent = ''; $('password-form').reset(); d.showModal();
}
function setPasswordSaving(saving) {
  const dialog = $('password');
  if (saving) dialog.dataset.saving = 'true';
  else delete dialog.dataset.saving;
  dialog.querySelector('[data-close]').disabled = saving;
  $('pw-submit').disabled = saving;
  for (const id of ['pw-old', 'pw-new', 'pw-confirm']) $(id).readOnly = saving;
}
$('password').addEventListener('cancel', e => {
  const {forced, saving} = e.currentTarget.dataset;
  if (forced || saving) e.preventDefault();
});
$('password-form').addEventListener('submit', async e => {
  e.preventDefault();
  const dialog = $('password');
  if (!dialog.open || dialog.dataset.saving) return;
  if ($('pw-new').value !== $('pw-confirm').value) { $('pw-error').textContent = '两次输入的新密码不一致'; return; }
  const session = S.session;
  const forced = dialog.dataset.forced;
  $('pw-error').textContent = '';
  setPasswordSaving(true);
  try {
    const { user } = await cloud('/api/auth/password', { method: 'POST', body: { old_password: $('pw-old').value, new_password: $('pw-new').value } });
    if (session !== S.session) return;
    try {
      const saved = await window.desktop.loadCredentials();
      if (session !== S.session) return;
      if (saved) await window.desktop.forgetCredentials();
    } catch (_) { if (session === S.session) toast('请手动删除系统中保存的旧密码'); }
    if (session !== S.session) return;
    $('password-form').reset();
    dialog.close();
    toast('密码已修改');
    if (forced) await enter(user); else S.user = user;
  } catch (error) {
    if (session === S.session && dialog.open) $('pw-error').textContent = error.message;
  } finally {
    if (session === S.session) setPasswordSaving(false);
  }
});
$('set-password').addEventListener('click', () => { $('settings').close(); openPassword(false); });
$('set-logout').addEventListener('click', async () => {
  $('settings').close();
  await window.Workspace?.flushSave?.();
  cloud('/api/auth/logout', { method: 'POST', body: {} }).catch(quiet);
  toLogin();
});

// ---------------- Settings & updates ----------------
const openSettings = () => {
  $('set-user').textContent = S.user?.username || '未登录';
  $('set-password').hidden = !S.user; $('set-logout').hidden = !S.user;
  $('set-workbench-error').textContent = '';
  $('set-clear-error').textContent = '';
  $('settings').showModal();
};
$('gate-settings').addEventListener('click', openSettings);
$('open-settings').addEventListener('click', openSettings);
$('account-chip').addEventListener('click', openSettings);

let installationCheck;
let installationConflict = '';
let shownInstallationConflict = '';
function maybeShowInstallations() {
  if (!installationConflict || installationConflict === shownInstallationConflict ||
      document.querySelector('dialog[open]:not(#server-settings[data-forced])')) return;
  shownInstallationConflict = installationConflict;
  $('installations').showModal();
}

function checkInstallations() {
  if (installationCheck) return installationCheck;
  if (!$('installations-list').children.length) $('installations-status').textContent = '正在检查本机安装…';
  $('installations-refresh').disabled = true;
  $('installations-error').textContent = '';
  installationCheck = window.desktop.listInstallations().then(copies => {
    installationConflict = copies.length > 1 ? JSON.stringify(copies.map(copy => copy.path)) : '';
    if (!installationConflict) shownInstallationConflict = '';
    $('set-installations').textContent = installationConflict ? '处理重复副本' : '检查 Kinora 副本';
    $('installations-status').textContent = installationConflict
      ? '本机发现多份 Kinora。请对照版本和位置，只保留一份在「应用程序」中。使用「在 Finder 中显示」定位多余的副本，再移到废纸篓。如果多余副本正在运行，请先退出；如果它在安装磁盘映像里，请推出磁盘映像。'
      : copies.length === 1 ? '只找到一份 Kinora。' : '暂未找到可检查的 Kinora 应用。';
    $('installations-list').replaceChildren(...copies.map(copy => h('li', { class: 'set-row' },
      h('div', {},
        h('strong', { text: [copy.version && `v${copy.version}`, copy.runningCopy && '当前运行'].filter(Boolean).join(' · ') || 'Kinora' }),
        h('span', { class: 'mono muted', text: copy.path })
      ),
      h('button', { class: 'btn subtle', type: 'button', text: '在 Finder 中显示', onclick: async () => {
        try { await window.desktop.revealInstallation(copy.path); }
        catch (error) { $('installations-error').textContent = typeof error === 'string' ? error : '无法在 Finder 中显示，请重新检查'; }
      } })
    )));
    if ($('installations').open) shownInstallationConflict = installationConflict;
    else maybeShowInstallations();
  }).catch(error => {
    installationConflict = '';
    $('installations-status').textContent = '暂时无法检查本机安装，请重试。';
    $('installations-list').replaceChildren();
    quiet(error);
  }).finally(() => {
    installationCheck = null;
    $('installations-refresh').disabled = false;
  });
  return installationCheck;
}

$('set-installations').addEventListener('click', () => {
  $('settings').close();
  $('installations').showModal();
  checkInstallations();
});
$('installations-refresh').addEventListener('click', checkInstallations);
window.addEventListener('focus', () => {
  if (S.status?.platform === 'darwin') checkInstallations();
});
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('close', () => setTimeout(maybeShowInstallations, 0));
}

function openServerSettings() {
  const configured = Boolean(S.status?.serverConfigured);
  $('server-address').value = S.status?.server || '';
  $('server-error').textContent = '';
  $('server-cancel').hidden = !configured;
  if (configured) delete $('server-settings').dataset.forced;
  else $('server-settings').dataset.forced = 'true';
  if (!$('server-settings').open) $('server-settings').showModal();
  $('server-address').focus();
}
$('set-server').addEventListener('click', openServerSettings);
$('offline-server').addEventListener('click', openServerSettings);
$('server-settings').addEventListener('cancel', event => {
  if ($('server-settings').dataset.forced || $('server-save').disabled) event.preventDefault();
});
$('server-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('server-save').disabled) return;
  $('server-save').disabled = true; $('server-error').textContent = '';
  $('server-cancel').disabled = true; $('server-settings').dataset.saving = 'true';
  try {
    if (S.user) window.Workspace?.flushSave?.();
    await window.desktop.configureServer($('server-address').value);
    window.resetGadget?.(); S.session++; credentialRevision++; stopPolling();
    window.invalidateTextPreview?.();
    window.location.reload();
  } catch (error) {
    $('server-error').textContent = typeof error === 'string' ? error : error.message || '无法保存服务器地址，请重试';
  } finally {
    $('server-save').disabled = false; $('server-cancel').disabled = false;
    delete $('server-settings').dataset.saving;
  }
});
for (const b of document.querySelectorAll('dialog [data-close]')) b.addEventListener('click', () => b.closest('dialog').close());
for (const d of document.querySelectorAll('dialog:not(#release-notes):not(#gadget-dialog):not(#draft-guard-dialog)')) d.addEventListener('click', e => { if (e.target === d && !d.dataset.forced && !d.dataset.saving) d.close(); });
$('set-reconnect').addEventListener('click', () => {
  $('settings').close();
  if (S.user) {
    window.Workspace?.flushSave?.();
    S.reconnectLibrary = { username: S.user.username,
      selected: S.lib.selected,
      fields: ['search-q', 'f-topic', 'f-status', 'f-sort', 'f-results-sort'].map(id => [id, $(id).value]) };
  }
  resetSession();
  boot();
});
$('set-workbench').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (button.disabled) return;
  const feedback = $('set-workbench-error');
  button.disabled = true;
  feedback.textContent = '';
  try {
    await window.desktop.openWorkbench();
  } catch (error) {
    if ($('settings').open) {
      feedback.textContent = typeof error === 'string' && error ? error : '无法打开网页工作台，请重试';
    }
  } finally {
    button.disabled = false;
  }
});
$('set-clear').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (button.disabled) return;
  const session = S.session;
  const feedback = $('set-clear-error');
  button.disabled = true;
  feedback.textContent = '';
  try {
    let result;
    try { result = await window.desktop.clearSession(); }
    catch (_) {
      if (session === S.session) feedback.textContent = '无法清除本机登录状态，请重试。';
      return;
    }
    if (!result?.cleared || session !== S.session) return;
    let incomplete = false;
    for (const clear of [window.clearSavedWorkspace, window.clearGadgetSavedPaths, clearSavedEditDrafts]) {
      try { clear?.(); }
      catch (_) { incomplete = true; }
    }
    $('settings').close();
    toLogin();
    $('login-error').textContent = incomplete ? '已退出登录，但部分本机草稿或设置未能清除。请重新登录后重试。' : '';
  } finally {
    button.disabled = false;
  }
});
for (const button of document.querySelectorAll('[data-account]')) {
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      await window.desktop.openAccount(button.dataset.account);
      toast('已请求在浏览器打开账号页面，完成后请回到客户端登录');
    } catch (error) {
      toast(typeof error === 'string' ? error : error?.message || '无法启动浏览器，请检查系统默认浏览器设置');
    } finally { button.disabled = false; }
  });
}
for (const [id, action] of [['upd-check', 'checkUpdate'], ['upd-download', 'downloadUpdate'], ['upd-install', 'installUpdate']]) {
  $(id).addEventListener('click', async () => {
    try {
      if (action === 'installUpdate' && S.user) await window.Workspace?.flushSave?.();
      await window.desktop[action]();
    }
    catch { toast('更新操作失败，请重试'); }
  });
}

function renderStatus(status) {
  if (!status) return;
  S.status = status;
  document.body.dataset.platform = status.platform;
  $('set-installations-row').hidden = status.platform !== 'darwin';
  for (const el of document.querySelectorAll('[data-bind="server"]')) el.textContent = status.server || '尚未配置服务器';
  $('set-version').textContent = status.version;
  $('set-update').textContent = status.update;
  $('upd-download').hidden = status.updatePhase !== 'available';
  $('upd-install').hidden = status.updatePhase !== 'downloaded';
  const updating = ['checking', 'downloading', 'downloaded', 'installing'].includes(status.updatePhase);
  $('upd-check').disabled = updating;
  $('set-server').disabled = status.updatePhase === 'installing';
  $('offline-server').disabled = status.updatePhase === 'installing';
  $('badge-update').hidden = !['available', 'downloaded'].includes(status.updatePhase);
  maybeShowReleaseNotes();
}

function showReleaseNotes() {
  const notes = S.status?.releaseNotes;
  if (!notes?.items?.length) return;
  $('release-title').textContent = `v${notes.version} · 本次更新`;
  $('release-items').replaceChildren(...notes.items.map(text => h('li', { text })));
  $('release-error').textContent = '';
  if (!$('release-notes').open) $('release-notes').showModal();
}
function maybeShowReleaseNotes() {
  if (S.status?.serverConfigured && S.status?.releaseNotesUnread && !document.querySelector('dialog[open]')) showReleaseNotes();
}
$('set-release-notes').addEventListener('click', showReleaseNotes);
async function dismissReleaseNotes() {
  const button = $('release-dismiss');
  if (button.disabled) return;
  button.disabled = true;
  $('release-error').textContent = '';
  try {
    await window.desktop.acknowledgeRelease(S.status.releaseNotes.version);
    S.status.releaseNotesUnread = false;
    $('release-notes').close();
  } catch { $('release-error').textContent = '无法保存阅读状态，请重试'; }
  finally { button.disabled = false; }
}
$('release-dismiss').addEventListener('click', dismissReleaseNotes);
$('release-notes').addEventListener('cancel', event => { event.preventDefault(); dismissReleaseNotes(); });
for (const dialog of document.querySelectorAll('dialog:not(#release-notes)')) dialog.addEventListener('close', () => setTimeout(maybeShowReleaseNotes, 0));

// ---------------- Keyboard ----------------
function ignorePlaybackShortcut(event) {
  if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return true;
  if (event.target.closest?.('[contenteditable]:not([contenteditable="false"])')) return true;
  return event.key === ' ' && (event.shiftKey || Boolean(event.target.closest?.('button, a[href], summary, [role="button"], [role="tab"]')));
}

const transportSliders = { preview: 'scrub', generate: 'scene-scrub', editor: 'edit-scrub', retarget: 'rt-scrub' };

document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.isComposing || e.altKey || document.querySelector('dialog[open]')) return;
  if (e.ctrlKey || e.metaKey) {
    if (S.user && ['1', '2', '3', '4'].includes(e.key)) {
      e.preventDefault();
      document.querySelector(`[data-nav="${['library', 'generate', 'retarget', 'editor'][Number(e.key) - 1]}"]`)?.click();
    }
    return;
  }
  const stage = document.body.dataset.stage;
  const focused = document.activeElement;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(focused?.tagName);
  const focusedSlider = typing && focused.id === transportSliders[stage];
  if ((typing && !focusedSlider) || document.body.dataset.screen !== 'ready') return;
  if (!focusedSlider) {
    if (e.key === '/') { e.preventDefault(); window.Workspace?.ensureLeftExpanded?.(); $('search-q').focus(); return; }
    if (e.key === '[' && !e.shiftKey && stage !== 'editor') { e.preventDefault(); window.Workspace?.toggleLeftPane?.(); return; }
    if (e.key === ']' && !e.shiftKey && stage !== 'editor') { e.preventDefault(); window.Workspace?.toggleRightPane?.(); return; }
  }
  if (ignorePlaybackShortcut(e)) return;
  if (stage === 'retarget') {
    const primary = retargetTransportView();
    const hasClip = primary.clip;
    if (e.key === ' ' && hasClip) {
      e.preventDefault();
      if (!e.repeat) $('rt-play').click();
    } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && hasClip) {
      e.preventDefault();
      const delta = (e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? 10 : 1);
      seekRetargetFrame(primary, Math.floor(primary.frame) + delta);
    }
    return;
  }
  const repairSlider = stage === 'editor' && focusedSlider && window.SequenceEditor?.getMode?.() === 'repair';
  const view = stage === 'generate' ? window.SceneController?.view : stage === 'preview' ? motionView
    : repairSlider ? window.RepairEditor?.originalView : null;
  if (e.key === ' ' && view?.clip) {
    e.preventDefault();
    if (!e.repeat) view.setPlaying(!view.playing);
  } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && view?.clip) {
    e.preventDefault(); view.setPlaying(false);
    view.seek(Math.floor(view.frame) + (e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? 10 : 1));
  }
});

window.desktop.onStatus(renderStatus);
window.desktop.status().then(status => {
  renderStatus(status);
  boot();
  if (status.platform === 'darwin') checkInstallations();
}).catch(error => { quiet(error); showServerPreferenceError(); });
applyStageView('library'); setTransport(); updateChecks(); renderInspector(null, null);

makeCopyable($('rt-source'), '文本提示词');

function syncRangeProgress(input) {
  if (!input?.style?.setProperty) return;
  const min = Number(input.min) || 0;
  const max = Number(input.max);
  const val = Number(input.value);
  const span = (Number.isFinite(max) ? max : 100) - min;
  const pct = span > 0 && Number.isFinite(val) ? Math.max(0, Math.min(100, ((val - min) / span) * 100)) : 0;
  input.style.setProperty('--p', `${pct.toFixed(2)}%`);
}
document.querySelectorAll?.('input[type="range"]')?.forEach?.(el => {
  syncRangeProgress(el);
  el.addEventListener?.('input', () => syncRangeProgress(el));
  el.addEventListener?.('change', () => syncRangeProgress(el));
});
