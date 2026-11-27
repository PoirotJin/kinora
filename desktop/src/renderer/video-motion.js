'use strict';
(() => {
  const root = typeof window !== 'undefined' ? window : globalThis;
  const MAX_BYTES = 32 * 1024 * 1024;
  const panel = $('video-motion-panel');
  const file = $('video-motion-file');
  const start = $('video-motion-start');
  const dropZone = $('video-drop-zone');
  const dropHint = $('video-drop-hint');
  const status = $('video-motion-status');
  const jobs = $('video-motion-jobs');
  const refreshBtn = $('video-motion-refresh');

  let timer = null;
  let serial = 0;
  let submitting = false;
  let activeTabKey = 'default';
  let fileInputOwnerTab = null;
  let selectedFile = null;
  const filesByTab = new Map();
  const pendingAttachments = new Map();
  const autoPreviewIds = new Set();

  const labels = {
    queued: '已排队',
    tracking: '正在跟踪人体',
    reconstructing: 'Kimodo 正在重建',
    done: '已完成',
    failed: '提取失败'
  };

  function currentFile() {
    return selectedFile || (fileInputOwnerTab === activeTabKey ? file?.files?.[0] : null) || null;
  }

  function setTab(tabId) {
    const nextKey = tabId || 'default';
    if (activeTabKey !== nextKey && file && fileInputOwnerTab !== nextKey) {
      file.value = '';
    }
    activeTabKey = nextKey;
    selectedFile = filesByTab.get(activeTabKey) || null;
    updateFileHint();
    if (status) status.textContent = '';
    root.updateChecks?.();
  }

  function clearTab(tabId) {
    const key = tabId || 'default';
    filesByTab.delete(key);
    if (fileInputOwnerTab === key) {
      fileInputOwnerTab = null;
      if (file) file.value = '';
    }
    if (activeTabKey === key) {
      selectedFile = null;
      updateFileHint();
      root.updateChecks?.();
    }
  }

  function updateFileHint() {
    if (!dropHint) return;
    const f = currentFile();
    if (!f) {
      dropHint.textContent = '拖放或选择单人动作视频（≤ 120s，32 MiB）';
      return;
    }
    const mb = (f.size / (1024 * 1024)).toFixed(1);
    dropHint.textContent = f.size > MAX_BYTES
      ? `${f.name || '已选视频'} (${mb} MiB) 超出 32 MiB 上限`
      : `已选：${f.name || '视频'} (${mb} MiB)`;
  }

  function canSubmit() {
    if (submitting) return false;
    const f = currentFile();
    return Boolean(f && f.size > 0 && f.size <= MAX_BYTES);
  }

  async function processCompletedJob(job, session) {
    if (session !== S?.session || !job || job.status !== 'done' || !job.motion_id) return;
    if (pendingAttachments.has(job.id)) {
      const spec = pendingAttachments.get(job.id);
      pendingAttachments.delete(job.id);
      if (status) status.textContent = '视频提取完成，正在生成物体响应…';
      try {
        await root.SceneController?.runObjectResponse?.(job.motion_id, spec, { sourceLabel: '视频动作响应' });
        if (session === S?.session && status) status.textContent = '视频提取与物体响应已完成。';
      } catch (error) {
        if (session === S?.session && status) status.textContent = '物体响应失败：' + error.message;
      }
      return;
    }
    if (autoPreviewIds.has(job.id)) {
      autoPreviewIds.delete(job.id);
      await root.previewGeneratedCandidate?.(
        job.motion_id,
        { id: job.id, description: '视频提取动作' },
        [{ motion_id: job.motion_id, state: 'done' }]
      );
    }
  }

  async function refresh() {
    const version = ++serial;
    const session = S?.session;
    clearTimeout(timer);
    if (!S?.user) return;
    if (panel && !panel.open && document.body?.dataset?.genModality !== 'video' && !pendingAttachments.size) return;
    try {
      const result = await cloud('/api/harness/video-motion');
      if (version !== serial || session !== S?.session) return;
      const items = result?.items || [];
      if (jobs && typeof jobs.replaceChildren === 'function') {
        jobs.replaceChildren(...items.map(job => h('div', { class: 'row wrap' },
          h('span', {
            text: (labels[job.status] || job.status)
              + (job.frames ? ' · ' + job.frames + ' 帧' : '')
              + (job.shots ? ' · ' + job.shots.length + ' 个镜头' : '')
              + (job.error ? ' · ' + job.error : '')
          }),
          job.motion_id ? h('button', {
            type: 'button', class: 'text-btn', text: '预览',
            onclick: () => root.previewGeneratedCandidate?.(
              job.motion_id,
              { id: job.id, description: '视频提取动作' },
              [{ motion_id: job.motion_id, state: 'done' }]
            )
          }) : null,
          job.motion_id ? h('button', {
            type: 'button', class: 'text-btn', text: '打开动作',
            onclick: () => root.Workspace?.openMotionInTab?.('editor', job.motion_id, { label: '视频提取动作' })
          }) : null,
          job.motion_id ? h('button', {
            type: 'button', class: 'text-btn', text: '重定向',
            onclick: () => root.Workspace?.openMotionInTab?.('retarget', job.motion_id, { label: '视频提取动作' })
          }) : null)));
      }
      for (const job of items) {
        if (job.status === 'done' && job.motion_id) {
          await processCompletedJob(job, session);
        } else if (job.status === 'failed') {
          pendingAttachments.delete(job.id);
          autoPreviewIds.delete(job.id);
        }
      }
      if (items.some(job => !['done', 'failed'].includes(job.status))) {
        timer = setTimeout(refresh, 4000);
      }
    } catch (error) {
      if (session === S?.session && version === serial && status) status.textContent = error.message;
    }
  }

  async function submit() {
    const tabKey = activeTabKey;
    const video = currentFile();
    const session = S?.session;
    if (!video || video.size > MAX_BYTES) {
      if (status) status.textContent = '请选择 32 MiB 以内的单人视频。';
      return null;
    }
    const attachSpec = root.SceneController?.getAttachmentRequest?.() || null;
    submitting = true;
    if (start) start.disabled = true;
    if (status) status.textContent = '正在上传视频…';
    root.updateChecks?.();
    try {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(Error('无法读取视频'));
        reader.onload = () => resolve(String(reader.result).split(',')[1]);
        reader.readAsDataURL(video);
      });
      if (session !== S?.session) return null;
      const created = await submitOnce('video-motion', '/api/harness/video-motion', { video_base64: encoded }, 'request_id');
      if (session !== S?.session) return null;
      if (created?.id) {
        if (attachSpec?.attachment?.object_id) pendingAttachments.set(created.id, attachSpec);
        else autoPreviewIds.add(created.id);
      }
      filesByTab.delete(tabKey);
      if (fileInputOwnerTab === tabKey) fileInputOwnerTab = null;
      if (activeTabKey === tabKey) {
        if (file) file.value = '';
        selectedFile = null;
        updateFileHint();
      }
      if (status) status.textContent = attachSpec?.attachment?.object_id ? '视频已提交，提取完成后将自动绑定物体响应。' : '视频已提交，正在提取动作…';
      await refresh();
      return created;
    } catch (error) {
      if (session === S?.session && status) status.textContent = error.message;
      return null;
    } finally {
      if (session === S?.session) {
        submitting = false;
        if (start) start.disabled = false;
        root.updateChecks?.();
      }
    }
  }

  function reset() {
    clearTimeout(timer);
    serial++;
    submitting = false;
    activeTabKey = 'default';
    fileInputOwnerTab = null;
    selectedFile = null;
    filesByTab.clear();
    pendingAttachments.clear();
    autoPreviewIds.clear();
    if (file) file.value = '';
    updateFileHint();
    if (status) status.textContent = '';
    jobs?.replaceChildren?.();
  }

  if (file?.addEventListener) {
    file.addEventListener('change', () => {
      selectedFile = file.files?.[0] || null;
      fileInputOwnerTab = selectedFile ? activeTabKey : null;
      if (selectedFile) filesByTab.set(activeTabKey, selectedFile);
      else filesByTab.delete(activeTabKey);
      updateFileHint();
      if (status) status.textContent = '';
      root.updateChecks?.();
    });
  }
  if (dropZone?.addEventListener) {
    dropZone.addEventListener('dragover', e => {
      e.preventDefault();
      dropZone.classList?.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => {
      dropZone.classList?.remove('dragover');
    });
    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList?.remove('dragover');
      const dropped = e.dataTransfer?.files?.[0] || null;
      if (dropped) {
        selectedFile = dropped;
        fileInputOwnerTab = null;
        if (file) file.value = '';
        filesByTab.set(activeTabKey, dropped);
        updateFileHint();
        if (status) status.textContent = '';
        root.updateChecks?.();
      }
    });
  }
  if (start) start.onclick = submit;
  if (panel) panel.ontoggle = () => { jobs?.replaceChildren?.(); refresh(); };
  if (refreshBtn) refreshBtn.onclick = refresh;

  root.VideoMotion = {
    canSubmit,
    submit,
    refresh,
    reset,
    setTab,
    clearTab,
    isSubmitting: () => submitting
  };
})();
