'use strict';
// Unified motion metadata and preview loader across legacy farm motions (m*)
// and immutable harness motion assets (ma_*), backed by a single byte-bounded LRU cache.
const MotionLoader = (() => {
  const MAX_BYTES = 192 * 1024 * 1024;
  const cache = new Map();
  const metaCache = new Map();
  const previewRequests = new Map();
  const listeners = new Set();

  const isManagedAsset = id => typeof id === 'string' && /^ma_[0-9a-f]{32}$/.test(id);
  const isLegacyMotion = id => typeof id === 'string' && /^m[0-9a-z]{8,}$/.test(id);
  const isValidMotionId = id => isLegacyMotion(id) || isManagedAsset(id);

  function notify() {
    for (const fn of listeners) {
      try { fn(previewRequests.size); } catch (_) { /* listener error ignored */ }
    }
  }

  function evictIfNeeded() {
    let bytes = 0;
    for (const v of cache.values()) bytes += v.data?.byteLength || 0;
    for (const [key, v] of cache) {
      if (bytes <= MAX_BYTES || cache.size <= 1) break;
      cache.delete(key);
      bytes -= v.data?.byteLength || 0;
    }
  }

  function has(id) {
    return cache.has(id);
  }

  function clear() {
    cache.clear();
    metaCache.clear();
    if (previewRequests.size) {
      previewRequests.clear();
      notify();
    }
  }

  function rememberMeta(id, info) {
    if (!id || !info) return;
    metaCache.set(id, { ...(metaCache.get(id) || {}), ...info, motion_id: id });
  }

  function getCachedMeta(id) {
    return metaCache.get(id) || null;
  }

  async function fetchPreview(id, { background = false } = {}) {
    id = String(id || '').trim();
    if (!id) throw new Error('请选择动作');
    const hit = cache.get(id);
    if (hit) {
      cache.delete(id);
      cache.set(id, hit);
      return hit;
    }
    const existing = previewRequests.get(id);
    if (existing) {
      if (!background) existing.background = false;
      return existing.promise;
    }
    const request = { background };
    previewRequests.set(id, request);
    request.promise = loadPreview(id, request);
    notify();
    return request.promise;
  }

  async function loadPreview(id, request) {
    try {
      const legacy = isLegacyMotion(id);
      const encoded = encodeURIComponent(id);
      const raw = await cloud(legacy ? `/api/motions/${encoded}/preview.bin` : `/api/harness/motions/${encoded}/preview`);
      if (previewRequests.get(id) !== request) {
        throw new DOMException('预览缓存已清除', 'AbortError');
      }
      const decoded = legacy ? MotionView.decodePreview(raw) : MotionView.decodeJsonPreview(raw);
      const entry = {
        ...decoded,
        frames: decoded.meta.frames,
        count: decoded.meta.joint_count,
        fps: decoded.meta.fps
      };
      cache.set(id, entry);
      evictIfNeeded();
      return entry;
    } finally {
      if (previewRequests.get(id) === request) {
        previewRequests.delete(id);
        notify();
      }
    }
  }

  function getManagedMetadata(id) {
    const known = metaCache.get(id);
    const quarantined = known?.status === 'quarantined';
    const accepted = !quarantined && Boolean(known?.accepted);
    return {
      motion_id: id,
      managed: true,
      owner: S.user?.username || '—',
      topic: known?.kind_label || '编辑与场景资产',
      prompt: { text: known?.label || known?.description || '（场景 / 编辑修正产物）' },
      review: { status: quarantined ? 'rejected' : accepted ? 'accepted' : 'pending' },
      task_id: known?.task_id || null,
      parent_id: known?.parent_id || null,
      accepted,
      status: known?.status || 'ready',
      created: known?.created || null
    };
  }

  async function fetchDetail(id) {
    id = String(id || '').trim();
    if (!id) throw new Error('请选择动作');
    if (isManagedAsset(id)) {
      const preview = await fetchPreview(id);
      const events = MotionView.previewQualityEvents(preview.quality, preview.frames);
      const detail = {
        ...getManagedMetadata(id),
        screening: { flags: events.map(e => `${e.reason} (帧 ${e.frame})`) }
      };
      rememberMeta(id, detail);
      return { detail, preview };
    }
    const detail = await cloud('/api/motions/' + encodeURIComponent(id));
    rememberMeta(id, { label: detail?.prompt?.text || '', owner: detail?.owner, detail });
    return { detail, preview: null };
  }

  return {
    isManagedAsset,
    isLegacyMotion,
    isValidMotionId,
    has,
    clear,
    rememberMeta,
    getCachedMeta,
    getManagedMetadata,
    fetchPreview,
    fetchDetail,
    get loadingCount() { return previewRequests.size; },
    get hasForegroundLoad() {
      for (const request of previewRequests.values()) {
        if (!request.background) return true;
      }
      return false;
    },
    onLoadChange(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    rawCache: cache
  };
})();
if (typeof module !== 'undefined') module.exports = MotionLoader;
