'use strict';
(() => {
  const root = typeof window !== 'undefined' ? window : globalThis;
  const DEFAULT_MODEL = 'exact_english';
  let task = null, loaded = '', activeCandId = '', busy = null, pending = null, session = S.session;
  let tabKey = 'default', baselineRange = [0, 0], baselinePrompt = '';
  let baselineModel = DEFAULT_MODEL, modelToRestore = null;
  let candidateRequest = null;
  let loadingSource = null;
  let sourceConfirmation = null;
  let viewRevision = 0, restoringTaskId = null;
  let restoringPreview = null;
  const acceptances = new Map();
  const submissions = new Map();
  const isActive = key => key === tabKey && key === (root.Workspace?.state?.activeTabId ?? tabKey);
  const captureView = () => {
    const key = tabKey, token = S.session, revision = viewRevision;
    return () => S.session === token && viewRevision === revision && isActive(key);
  };
  const isSubmitting = () => submissions.has(tabKey);
  let timeline;
  const empty = (id, on) => { const n = $(id); if (n) n.hidden = !on; };
  const original = new MotionView.View($('edit-original'), {
    onFrame: () => {
      $('edit-play').querySelector?.('use')?.setAttribute('href', original.playing ? '#i-pause' : '#i-play');
      empty('edit-original-empty', !original.clip);
      $('edit-scrub').value = Math.floor(original.frame);
      $('edit-scrub').style?.setProperty('--p', (100 * original.frame / Math.max(1, (original.clip?.frames || 1) - 1)) + '%');
      syncCandidateFrame();
      timeline?.setFrame(original.frame);
    }
  });
  const candidate = new MotionView.View($('edit-candidate'), { onFrame: () => empty('edit-candidate-empty', !candidate.clip) });
  function syncCandidateFrame() {
    if (original.clip && candidate.clip) {
      candidate.seek(original.frame * (candidate.clip.fps / original.clip.fps));
    }
  }

  timeline = new EditTimeline.Timeline({
    seek: frame => { original.setPlaying(false); original.seek(frame); },
    changed: (start, end) => {
      $('edit-start').value = start;
      $('edit-end').value = end;
      updateStateBanner();
      root.Workspace?.syncTabDirty?.();
    },
    locked: () => busy || isSubmitting() || Boolean(pending)
  });
  for (const id of ['edit-start', 'edit-end']) {
    $(id).addEventListener('input', () => {
      timeline.setRange(Number($('edit-start').value), Number($('edit-end').value));
      updateStateBanner();
      root.Workspace?.syncTabDirty?.();
    });
  }
  if ($('edit-prompt').addEventListener) {
    $('edit-prompt').addEventListener('input', () => {
      updateStateBanner();
      root.Workspace?.syncTabDirty?.();
    });
  }
  $('edit-model').addEventListener('change', () => {
    modelToRestore = null;
    updateStateBanner();
    root.Workspace?.syncTabDirty?.();
  });

  const note = text => ($('edit-notice').textContent = text);
  function setPreviewRetry(retry = null) {
    const button = $('edit-preview-retry');
    if (!button) return;
    button.hidden = !retry;
    button.onclick = retry;
  }
  const taskOf = x => x.task || x;
  const idOf = x => x.id || x.task_id;
  const isAccepted = (job, candidate) => job.accepted_candidate_id
    ? job.accepted_candidate_id === (candidate.id || candidate.candidate_id)
    : candidate.status === 'accepted' && job.current_motion_id === candidate.motion_id;
  const items = x => Array.isArray(x) ? x : x?.items || [];
  const request = (path, body) => cloud('/api/harness/' + path, body === undefined ? {} : { method: 'POST', body });
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const pendingKey = (key = tabKey) => key === 'default' ? 'human-edit' : 'human-edit:' + key;

  function savePending(value) {
    pendingRecord(pendingKey()).set(value);
    pending = value;
  }

  const selectedModel = () => $('edit-model').value || modelToRestore || DEFAULT_MODEL;
  function restoreModel(id) {
    $('edit-model').value = id;
    modelToRestore = $('edit-model').value === id ? null : id;
  }

  function isDirty(snapshot) {
    if (!snapshot && pending) return true;
    const draft = snapshot || {
      sourceId: loaded || restoringPreview?.sourceId || $('edit-source').value.trim(),
      start: Number($('edit-start').value), end: Number($('edit-end').value),
      prompt: $('edit-prompt').value, model: selectedModel(),
      baselinePrompt, baselineRange, baselineModel
    };
    const model = draft.model || DEFAULT_MODEL;
    const [start, end] = draft.baselineRange || [0, 0];
    return (draft.prompt || '').trim() !== (draft.baselinePrompt || '') ||
      model !== (draft.baselineModel || model) ||
      Boolean(draft.sourceId && (draft.start !== start || draft.end !== end));
  }

  const modelForRepair = args => args?.prompt_mode === 'translate' ? args.rewrite_model || DEFAULT_MODEL : DEFAULT_MODEL;

  function submissionBaseline(submitted) {
    const { start_frame, end_frame } = submitted.create.selection;
    return {
      baselinePrompt: submitted.create.goal,
      baselineRange: [start_frame, end_frame],
      baselineModel: modelForRepair(submitted.repair)
    };
  }

  const STATE = {
    succeeded: ['生成候选', 'ok'],
    accepted: ['已选用结果', 'ok'],
    failed: ['未通过', 'bad'],
    waiting: ['已提交任务 · 生成中', 'accent'],
    submitting: ['已提交任务 · 正在提交', 'accent'],
    unknown: ['状态待确认', 'warn'],
    quarantined: ['隔离候选', 'bad'],
    draft: ['编辑草稿', '']
  };
  const badge = key => {
    const [label, tone] = STATE[key] || [key || '未知', ''];
    return h('span', { class: 'pill ' + tone, text: label });
  };
  const taskBadge = job => badge(job.accepted_candidate_id ? 'accepted' : job.status);

  function currentLifecycleState() {
    if (pending || busy || isSubmitting()) return 'submitting';
    if (task) {
      const ops = items(task.operations).filter(x => x.tool === 'edit_motion');
      const lastOp = ops.at(-1);
      if (lastOp && ['submitting', 'waiting', 'unknown'].includes(lastOp.status)) return 'waiting';
      if (isDirty()) return 'draft';
      if (task.status === 'accepted' || task.accepted_candidate_id) return 'accepted';
      if (items(task.candidates).length) return 'succeeded';
      if (task.status === 'failed') return 'failed';
    }
    return loaded || ($('edit-prompt').value || '').trim() ? 'draft' : '';
  }

  function updateStateBanner() {
    const box = $('repair-state-banner');
    if (!box || typeof box.replaceChildren !== 'function') return;
    if ($('repair-orig-label')) $('repair-orig-label').textContent = loaded ? '· ' + loaded : '';
    if ($('repair-cand-label')) $('repair-cand-label').textContent = activeCandId ? '· ' + activeCandId : '';
    const st = currentLifecycleState();
    if (!st) { box.replaceChildren(); return; }
    const srcText = loaded ? `源动作 ${loaded}` : '尚未载入源动作';
    box.replaceChildren(badge(st), h('span', { class: 'mono muted small', text: srcText }));
  }

  async function run(fn) {
    if (busy) return;
    let finish;
    const operation = { promise: new Promise(resolve => { finish = resolve; }) };
    const isCurrent = captureView();
    busy = operation;
    timeline.sync();
    $('edit-submit').disabled = true;
    try { return await fn(); }
    catch (e) { if (isCurrent()) note(e.message); }
    finally {
      if (busy === operation) busy = null;
      finish();
      if (isCurrent()) {
        timeline.sync();
        $('edit-submit').disabled = !enabled || isSubmitting() || Boolean(restoringPreview && !original.clip);
        pendingActions();
        updateStateBanner();
      }
    }
  }

  let enabled = false;
  function pendingActions() {
    const box = $('edit-pending'); box.replaceChildren(); box.hidden = !pending;
    if (!pending) return;
    box.append(
      h('p', { class: 'muted', text: isSubmitting() ? '正在处理修正请求…' : '上次提交待确认。你可以对账或用同一请求编号重试。' }),
      h('button', { type: 'button', class: 'btn sm subtle', text: '刷新对账', disabled: !pending.task || isSubmitting(), onclick: () => runPending(reconcile) }),
      h('button', { type: 'button', class: 'btn sm subtle', text: '重试上次提交', disabled: !enabled || isSubmitting(), onclick: () => runPending(submitPending) }),
      h('button', {
        type: 'button', class: 'text-btn', text: '放弃跟踪此请求', disabled: isSubmitting(), onclick: () => {
          if (busy || isSubmitting()) return;
          savePending(null); pendingActions(); timeline.sync(); updateStateBanner();
          root.Workspace?.syncTabDirty?.();
          note('已清除本机待确认记录，原任务仍可在任务列表查看。');
        }
      }));
  }

  async function runPending(action) {
    const key = tabKey, token = S.session;
    if (busy || isSubmitting() || !isActive(key)) return;
    const record = pendingRecord(pendingKey(key));
    const current = () => token === S.session;
    const visible = () => current() && isActive(key);
    const say = text => { if (visible()) note(text); };
    const save = value => {
      if (!current()) return;
      record.set(value);
      if (visible()) pending = value;
    };
    const operation = { save, current, visible, say, read: () => record.get(), async publish(result, submitted, confirmed) {
      if (!current()) return;
      const updated = taskOf(result);
      root.Workspace?.registerHarnessTask?.(updated, 'repair');
      if (root.Workspace?.completeRepairSubmission) {
        await root.Workspace.completeRepairSubmission(key, token, updated, submitted, confirmed);
      } else if (visible()) applySubmission(updated, submitted, confirmed);
    } };
    submissions.set(key, operation);
    $('edit-submit').disabled = true;
    pendingActions(); timeline.sync(); updateStateBanner();
    try { await action(operation); }
    catch (e) {
      if (current() && e.name !== 'AbortError') {
        if (visible()) note(e.message);
        else toast('修正请求未完成：' + e.message, true);
      }
    } finally {
      if (submissions.get(key) === operation) submissions.delete(key);
      if (visible()) {
        $('edit-submit').disabled = !enabled || busy;
        pendingActions(); timeline.sync(); updateStateBanner();
        root.Workspace?.syncTabDirty?.();
      }
    }
  }

  function applySubmission(updated, submitted, confirmed) {
    if ((loaded || $('edit-source').value.trim()) !== submitted.create.source_motion_id) return;
    if (confirmed) {
      ({ baselinePrompt, baselineRange, baselineModel } = submissionBaseline(submitted));
    }
    if (!loaded && busy) {
      const snapshot = root.RepairEditor.getState();
      root.RepairEditor.setState({ ...snapshot, taskId: idOf(updated), activeCandId: '' }, tabKey, true);
      return;
    }
    render(updated); markTask();
    root.Workspace?.syncTabDirty?.();
  }

  async function reconcile(operation) {
    const submitted = operation.read();
    if (!submitted?.task) return;
    const result = await request('tasks/' + encodeURIComponent(submitted.task) + '/repair-status', {});
    if (!operation.current()) return;
    const { prompt, prompt_mode, rewrite_model } = submitted.repair;
    const matched = items(taskOf(result).operations).some(op => op.tool === 'edit_motion' && op.arguments?.prompt === prompt
      && op.arguments.prompt_mode === prompt_mode && (op.arguments.rewrite_model || '') === rewrite_model);
    await operation.publish(result, submitted, matched);
    if (matched) operation.save(null);
    operation.say(matched ? '已找到服务端作业，后续进度可在任务列表查看。' : '服务端尚未返回匹配的作业。可以刷新对账或使用原请求编号重试。');
  }

  async function submitPending(operation) {
    let submitted = operation.read();
    if (!submitted || !enabled) return;
    operation.say('正在提交修正…');
    let result;
    try {
      if (!submitted.task) {
        const created = taskOf(await request('tasks', submitted.create));
        if (!operation.current()) return;
        const taskId = idOf(created);
        if (!taskId) throw Error('服务器未返回修正任务编号');
        submitted = { ...submitted, task: taskId };
        operation.save(submitted);
      }
      result = await request('tasks/' + encodeURIComponent(submitted.task) + '/repair', submitted.repair);
    } catch (e) {
      if (definitiveRejection(e)) operation.save(null);
      throw e;
    }
    if (!operation.current()) return;
    const confirmed = Boolean(result.decision_receipt?.action_applied);
    await operation.publish(result, submitted, confirmed);
    if (confirmed) operation.save(null);
    if (!confirmed && result.decision_receipt?.state === 'uncertain') await reconcile(operation);
    else operation.say(confirmed ? '修正请求已确认。' : '修正请求待确认。');
    if (operation.visible()) await refresh();
  }

  async function fetchPreview(id) {
    const clip = await request('motions/' + encodeURIComponent(id) + '/preview');
    const frames = clip?.joints?.length, count = clip?.parents?.length;
    if (!Number.isInteger(frames) || frames < 1 || !Number.isInteger(count) || count < 1 ||
      !Number.isFinite(clip.fps) || clip.fps <= 0) throw Error('预览格式无效');
    MotionView.validatePreviewParents(clip.parents, count);
    const data = MotionView.decodePreviewCoordinates(clip.joints, count);
    const names = Array.isArray(clip.names) && clip.names.length === count ? clip.names : clip.parents.map((_, i) => String(i));
    return {
      decoded: { meta: { frames, joint_count: count, fps: clip.fps, parents: clip.parents, names }, data },
      events: clip.quality ? MotionView.previewQualityEvents(clip.quality, frames) : []
    };
  }

  function clearRepairPreviews() {
    timeline.setClip(null);
    original.clear();
    candidate.clear();
    for (const id of ['edit-source-markers', 'edit-candidate-markers']) {
      $(id).replaceChildren();
      $(id).hidden = true;
    }
    $('edit-seam-markers').hidden = true;
  }

  function applyPreview({ decoded, events }, view) {
    const isCandidate = view === candidate;
    const markers = $(isCandidate ? 'edit-candidate-markers' : 'edit-source-markers');
    const label = isCandidate ? '候选' : '源动作';
    markers.replaceChildren(...events.map(e => h('button', {
      type: 'button', class: 'text-btn', text: label + ' · ' + e.reason + ' · ' + e.frame + ' 帧',
      onclick: () => {
        if (!view.clip) return;
        original.setPlaying(false);
        if (original.clip) original.seek(e.frame * (original.clip.fps / decoded.meta.fps));
        if (isCandidate) candidate.seek(e.frame);
        const part = typeof e.part === 'string' ? e.part.trim() : '';
        note(part ? e.reason + ' · ' + part : e.reason);
      }
    })));
    markers.hidden = !events.length;
    $('edit-seam-markers').hidden = $('edit-source-markers').hidden && $('edit-candidate-markers').hidden;
    view.setClip(decoded);
  }

  async function showCandidate(id, { camera } = {}) {
    const key = tabKey, token = S.session;
    if (!id || !isActive(key)) return;
    if (camera === undefined && candidateRequest?.id === id) camera = candidateRequest.camera;
    if (candidateRequest?.failed) note('');
    setPreviewRetry();
    const loading = { id, camera };
    candidateRequest = loading;
    const isCurrent = () => token === S.session && isActive(key) && candidateRequest === loading;
    try {
      const preview = await fetchPreview(id);
      if (!isCurrent()) return;
      applyPreview(preview, candidate);
      activeCandId = id;
      candidate.applyCameraState?.(camera);
      syncCandidateFrame();
      updateStateBanner();
      root.Workspace?.saveWorkspaceDebounced?.();
    } catch (e) {
      if (isCurrent() && e.name !== 'AbortError') {
        loading.failed = true;
        note('候选预览加载失败：' + e.message);
        setPreviewRetry(() => showCandidate(id, { camera }));
        root.Workspace?.saveWorkspaceDebounced?.();
      }
    } finally {
      if (candidateRequest === loading && !loading.failed) candidateRequest = null;
    }
  }

  async function load({ taskId = null, sourceId = $('edit-source').value.trim(), trackRequest = true, draftDecision = null, canApply } = {}) {
    const id = sourceId;
    if (!id) {
      if (loaded) $('edit-source').value = loaded;
      throw Error('请输入源动作编号');
    }
    const isCurrent = captureView();
    const operation = { id, draftDecision };
    const promptAtRequest = $('edit-prompt').value;
    const modelAtRequest = selectedModel();
    if (trackRequest && !taskId && loaded !== id) loadingSource = operation;
    let preview;
    try {
      preview = await fetchPreview(id);
    } catch (error) {
      if (isCurrent() && loaded) $('edit-source').value = loaded;
      throw error;
    } finally {
      if (loadingSource === operation) loadingSource = null;
    }
    if (!isCurrent() || canApply?.() === false) return false;
    if (draftDecision) {
      if (draftDecision === 'save') root.Workspace?.saveActiveRepairDraft?.();
      savePending(null);
      if ($('edit-prompt').value === promptAtRequest) $('edit-prompt').value = '';
      if (selectedModel() === modelAtRequest) restoreModel(baselineModel);
    }
    candidateRequest = null;
    setPreviewRetry();
    if (trackRequest && !taskId) restoringPreview = null;
    restoringTaskId = taskId;
    loaded = ''; activeCandId = '';
    clearRepairPreviews();
    $('edit-result').replaceChildren(); task = null;
    applyPreview(preview, original);
    $('edit-source').value = id;
    loaded = id;
    timeline.setClip(original.clip);
    const endVal = Math.min(300, original.clip.frames);
    $('edit-start').value = 0;
    $('edit-end').value = endVal;
    baselineRange = [0, endVal];
    baselinePrompt = ($('edit-prompt').value || '').trim();
    baselineModel = selectedModel();
    $('edit-scrub').max = original.clip.frames - 1;
    updateStateBanner();
    root.Workspace?.onEditorSourceChanged?.(loaded);
    note('已载入 ' + id);
    return true;
  }

  function retryRepairRestore(sourceId) {
    root.RepairEditor.setState({ ...root.RepairEditor.getState(), sourceId }, tabKey, true);
    const current = captureView();
    return busy?.promise.then(() => current() && loaded === sourceId);
  }

  async function requestSource(id, { beforeLoad } = {}) {
    const isCurrent = captureView();
    if (!isCurrent() || sourceConfirmation) return false;
    while (busy) {
      await busy.promise;
      if (!isCurrent() || sourceConfirmation) return false;
    }
    if (id && loaded === id) {
      beforeLoad?.();
      $('edit-source').value = id;
      return true;
    }
    if (id && id === restoringPreview?.sourceId) {
      beforeLoad?.();
      return retryRepairRestore(id);
    }
    let draftDecision = null;
    const previous = loaded || restoringPreview?.sourceId || pending?.create?.source_motion_id;
    if (id && previous && previous !== id && isDirty() && root.Workspace?.confirmRepairSourceChange) {
      const confirmation = {};
      sourceConfirmation = confirmation;
      try { draftDecision = await root.Workspace.confirmRepairSourceChange(id); }
      finally { if (sourceConfirmation === confirmation) sourceConfirmation = null; }
      if (!isCurrent()) return false;
      if (draftDecision === 'cancel') {
        if (loaded) $('edit-source').value = loaded;
        return false;
      }
    }
    beforeLoad?.();
    return run(async () => {
      if (!enabled) { try { await initCapabilities(); } catch (_) {} }
      if (!isCurrent()) return false;
      return load({ sourceId: id, draftDecision });
    });
  }

  async function acceptCandidate(taskId, candidateId) {
    const key = tabKey, token = S.session;
    if (!isActive(key) || !task || idOf(task) !== taskId || acceptances.has(taskId)) return;
    const receipt = {};
    acceptances.set(taskId, receipt);
    const isViewingTask = () => token === S.session && isActive(tabKey) && task && idOf(task) === taskId;
    render(task);
    try {
      const updated = taskOf(await request('tasks/' + encodeURIComponent(taskId) + '/accept', { candidate_id: candidateId }));
      if (token !== S.session) return;
      if (idOf(updated) !== taskId) throw Error('服务器未返回此修正任务的选用结果');
      if (isViewingTask()) {
        task = updated;
        if (isActive(key)) note('已选用修正版本，原动作保留。');
      } else root.Workspace?.registerHarnessTask?.(updated, 'repair');
    } catch (e) {
      if (token !== S.session || e.name === 'AbortError') return;
      if (isViewingTask() && isActive(key)) note(e.message);
      else toast('未能选用修正版本：' + e.message, true);
    } finally {
      if (acceptances.get(taskId) === receipt) acceptances.delete(taskId);
      if (isViewingTask()) render(task);
    }
  }

  function render(value) {
    task = taskOf(value);
    restoringTaskId = null;
    root.Workspace?.registerHarnessTask?.(task, 'repair');
    const historyRow = [...$('edit-tasks').children].find(row => row.dataset?.task === idOf(task));
    historyRow?.querySelector?.('.pill')?.replaceWith(taskBadge(task));
    timeline.reviewRange = task.selection;
    timeline.sync();
    const box = $('edit-result');
    box.replaceChildren(
      h('div', { class: 'ed-head section' }, h('span', { class: 'caps', text: '当前任务版本' }), taskBadge(task)),
      h('p', { class: 'ed-goal', text: task.goal || '' }),
      task.source_motion_id ? h('p', { class: 'muted small mono', text: '来源动作：' + task.source_motion_id }) : null
    );
    for (const c of items(task.candidates)) {
      const taskId = idOf(task), cid = c.id || c.candidate_id, accepted = isAccepted(task, c);
      if (typeof MotionLoader !== 'undefined' && c.motion_id) {
        MotionLoader.rememberMeta(c.motion_id, {
          label: task.goal || '局部修正版本',
          task_id: idOf(task),
          parent_id: task.source_motion_id || loaded || null,
          accepted,
          status: c.status,
          kind_label: '片段修正'
        });
      }
      const compare = h('button', { class: 'text-btn', type: 'button', text: '对比预览' });
      compare.onclick = () => showCandidate(c.motion_id);
      const useNext = h('button', { class: 'text-btn', type: 'button', text: '以此继续编辑' });
      useNext.disabled = c.status === 'quarantined';
      useNext.onclick = () => requestSource(c.motion_id);
      const toRetarget = h('button', { class: 'text-btn', type: 'button', text: '重定向' });
      toRetarget.disabled = c.status === 'quarantined';
      toRetarget.onclick = () => root.Workspace?.openMotionInTab('retarget', c.motion_id, {
        label: task.goal || '修正结果',
        parentId: task.source_motion_id || loaded || null,
        fromTask: idOf(task)
      });
      const download = h('button', { class: 'text-btn', type: 'button', text: '下载' });
      download.onclick = () => root.downloadMotion(c.motion_id, download);
      const accept = h('button', { class: 'btn sm subtle', type: 'button', text: accepted ? '已选用此版' : '选用此版本' });
      accept.disabled = c.status === 'quarantined' || accepted || acceptances.has(taskId);
      accept.onclick = () => acceptCandidate(taskId, cid);
      box.append(h('article', { class: 'cand' },
        h('div', { class: 'cand-top' },
          h('span', { class: 'mono', copy: '动作序号', text: c.motion_id || c.id }),
          c.status === 'quarantined' ? badge('quarantined') : accepted ? badge('accepted') : badge('succeeded')),
        h('div', { class: 'cand-actions' }, compare, useNext, toRetarget, download, h('span', { class: 'fill' }), accept)));
    }
    const op = items(task.operations).filter(x => x.tool === 'edit_motion').at(-1);
    if (op) {
      for (const [side, row] of Object.entries(op.result?.validation?.boundaries || {})) {
        if (row.neighbor && row.passed === false && task.selection) {
          const f = task.selection[side === 'start' ? 'start_frame' : 'end_frame'];
          box.append(h('button', {
            type: 'button', class: 'text-btn', text: '接缝异常 · ' + f + ' 帧',
            onclick: () => { original.setPlaying(false); original.seek(f); note((op.result.validation.failures || []).join('；'), true); }
          }));
        }
      }
      box.append(h('p', { class: 'job-msg', text: op.result?.message || op.status }));
      const gait = op.result?.validation?.bridge?.gait_review;
      if (gait) {
        const list = h('ul', { class: 'tr-issues' });
        list.append(h('li', { class: gait.crossing_intervals?.length ? 'warn' : 'ok', text: '脚步检查（骨架估计）：' + (gait.crossing_intervals?.length ? '发现疑似交叉步' : '未检测到持续交叉步') + '；最大单帧转向 ' + gait.pelvis_yaw_step_max_deg.toFixed(2) + '°' }));
        for (const warning of gait.warnings || []) list.append(h('li', { class: 'warn', text: warning }));
        box.append(list, h('p', { class: 'muted small', text: '脚底接触与机器人碰撞尚未验证。' }));
      }
    }
    updateStateBanner();
  }

  async function openTaskById(tid, {
    candidateId, candidateCamera, preferAccepted = false, preserveForm = false, protectDraft = false, restoring = false
  } = {}) {
    const isCurrent = captureView();
    const canApply = () => !protectDraft || !isDirty();
    let result = await request('tasks/' + tid);
    if (!isCurrent()) return;
    const detail = taskOf(result);
    if (items(detail.operations).some(x => x.tool === 'edit_motion' && ['submitting', 'waiting', 'unknown'].includes(x.status))) {
      result = await request('tasks/' + tid + '/repair-status', {});
      if (!isCurrent()) return;
    }
    if (!canApply()) return 'draft-edited';
    const current = taskOf(result), mid = current.source_motion_id;
    if (mid) {
      const applied = await load({ taskId: tid, sourceId: mid, canApply });
      if (!isCurrent()) return;
      if (!applied) return canApply() ? undefined : 'draft-edited';
      if (current.selection) {
        $('edit-start').value = current.selection.start_frame;
        $('edit-end').value = current.selection.end_frame;
        baselineRange = [current.selection.start_frame, current.selection.end_frame];
        timeline.setRange(current.selection.start_frame, current.selection.end_frame);
      }
    } else {
      candidateRequest = null;
      loaded = ''; activeCandId = ''; clearRepairPreviews();
    }
    if (!preserveForm) {
      const args = items(current.operations).filter(op => op.tool === 'edit_motion').at(-1)?.arguments;
      baselinePrompt = (args?.prompt ?? current.goal ?? '').trim();
      baselineModel = modelForRepair(args);
      $('edit-prompt').value = baselinePrompt;
      restoreModel(baselineModel);
    }
    if (!restoring) restoringPreview = null;
    setPreviewRetry();
    render(result); markTask();
    const candidates = items(current.candidates);
    const bookmarked = candidates.find(c => c.motion_id === candidateId);
    const accepted = candidates.find(c => c.status !== 'quarantined' && isAccepted(current, c));
    const preferred = (preferAccepted ? accepted || bookmarked : bookmarked || accepted)
      || candidates.find(c => c.status !== 'quarantined');
    showCandidate(preferred?.motion_id, { camera: preferred === bookmarked ? candidateCamera : undefined });
    root.Workspace?.syncTabDirty?.();
    return true;
  }

  async function refresh() {
    const isCurrent = captureView();
    const tasks = root.Workspace?.state?.harnessTasks;
    const previous = new Map(tasks);
    const data = await request('tasks');
    if (!isCurrent()) return;
    $('edit-tasks').replaceChildren();
    for (const item of items(data)) {
      const id = idOf(item);
      const latest = tasks?.get(id);
      const t = latest !== previous.get(id) && latest ? latest.task : item;
      if (t === item) root.Workspace?.registerHarnessTask?.(item, 'repair');
      const b = h('button', {
        class: 'task-row', type: 'button', role: 'listitem', dataset: { task: id },
        'aria-current': String(Boolean(task) && idOf(task) === id)
      },
        h('span', { class: 'task-goal', text: t.goal || id }),
        taskBadge(t),
        t.source_motion_id ? h('span', { class: 'mono muted task-src', text: t.source_motion_id }) : null);
      b.onclick = () => root.Workspace?.openRepairTaskFromList
        ? root.Workspace.openRepairTaskFromList(id)
        : run(() => openTaskById(id));
      $('edit-tasks').append(b);
    }
    if (!items(data).length) $('edit-tasks').append(h('p', { class: 'empty', text: '暂无修正任务' }));
  }

  function markTask() {
    for (const b of $('edit-tasks').children || []) {
      if (b.dataset?.task) b.setAttribute('aria-current', String(Boolean(task) && b.dataset.task === idOf(task)));
    }
  }

  $('edit-load').onclick = () => requestSource($('edit-source').value.trim());
  $('edit-current').onclick = () => {
    if (S.lib.selected) return requestSource(S.lib.selected);
    note('请先在动作库中选择动作');
  };
  $('edit-refresh').onclick = () => run(refresh);
  $('edit-play').onclick = () => original.setPlaying(!original.playing);
  $('edit-scrub').oninput = e => {
    const frame = Number(e.target.value);
    original.setPlaying(false);
    original.seek(frame);
  };
  $('edit-form').onsubmit = e => {
    e.preventDefault();
    runPending(async operation => {
      const start = Number($('edit-start').value), end = Number($('edit-end').value), goal = $('edit-prompt').value.trim(), model = $('edit-model').value;
      if (!enabled || !loaded || loaded !== $('edit-source').value.trim()) throw Error('请先载入原动作');
      if (!model) throw Error('请选择可用的提示词处理方式');
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > original.clip.frames || end - start < 12 || end - start > 300 || original.clip.frames < 30) throw Error('请选择有效的 12–300 帧片段');
      if (goal.length < 3 || goal.length > 2000) throw Error('请填写 3–2000 字的修改描述');
      const body = { source_motion_id: loaded, selection: { start_frame: start, end_frame: end }, goal, budget: { max_attempts: 1, max_cost_usd: 0 } };
      const fingerprint = JSON.stringify([body, model]);
      if (pending && pending.fingerprint !== fingerprint) throw Error('上次提交尚未确认，请保留原参数重试');
      if (!pending) operation.save({ fingerprint, create: { ...body, request_id: uid() }, repair: { request_id: uid(), prompt: goal, prompt_mode: model === 'exact_english' ? 'exact_english' : 'translate', rewrite_model: model === 'exact_english' ? '' : model } });
      await submitPending(operation);
    });
  };

  async function initCapabilities() {
    const isCurrent = captureView();
    pending = pendingRecord(pendingKey()).get();
    const caps = await request('capabilities');
    if (!isCurrent()) return;
    enabled = caps.editing_enabled === true;
    note(enabled ? '修正服务可用' : '修正服务暂不可用');
    const models = await cloud('/api/rewrite/models');
    if (!isCurrent()) return;
    const prev = selectedModel();
    $('edit-model').replaceChildren(
      h('option', { value: 'exact_english', text: '英文原文直传' }),
      ...items(models.models).filter(m => m.configured).map(m => h('option', { value: m.id, text: m.label }))
    );
    const configured = [...($('edit-model').children || [])].some(o => o.value === prev);
    restoreModel(configured ? prev : DEFAULT_MODEL);
    await refresh();
  }

  document.querySelector('[data-nav="editor"]').addEventListener('click', () => run(initCapabilities));

  root.RepairEditor = {
    originalView: original,
    candidateView: candidate,
    timeline,
    applySubmission,
    submissionBaseline,
    isDirty,
    getLoadedSource: () => loaded,
    clearDraft() {
      savePending(null);
      viewRevision++;
      busy = null;
      restoringTaskId = null;
      restoringPreview = null;
      setPreviewRetry();
      candidateRequest = null;
      loadingSource = null;
      sourceConfirmation = null;
      loaded = ''; activeCandId = ''; task = null;
      baselineRange = [0, 0]; baselinePrompt = '';
      $('edit-form').reset();
      baselineModel = DEFAULT_MODEL; restoreModel(DEFAULT_MODEL);
      $('edit-source').value = '';
      $('edit-prompt').value = '';
      clearRepairPreviews();
      $('edit-result').replaceChildren();
      pendingActions();
      updateStateBanner();
      note('');
    },
    loadMotion: requestSource,
    openTask(tid, options) {
      return run(async () => {
        if (!enabled) { try { await initCapabilities(); } catch (_) {} }
        return openTaskById(tid, options);
      });
    },
    getState() {
      return {
        sourceId: loaded || restoringPreview?.sourceId || $('edit-source').value.trim() || '',
        requestedSourceId: loadingSource?.id || null,
        requestedSourceDecision: loadingSource?.draftDecision || null,
        start: Number($('edit-start').value) || 0,
        end: Number($('edit-end').value) || 0,
        prompt: $('edit-prompt').value || '',
        model: selectedModel(),
        baselineRange: [...baselineRange],
        baselinePrompt,
        baselineModel,
        taskId: restoringTaskId || (task ? idOf(task) : null),
        activeCandId: candidateRequest?.id || activeCandId || restoringPreview?.activeCandId || '',
        zoom: restoringPreview?.zoom ?? (timeline.zoom || 1),
        origCam: restoringPreview ? restoringPreview.origCam : original.cameraState?.() || null,
        candCam: candidateRequest?.camera || (restoringPreview ? restoringPreview.candCam : candidate.cameraState?.() || null)
      };
    },
    setState(snap = {}, key = 'default', active = false, { preferAccepted = false } = {}) {
      tabKey = key || 'default';
      viewRevision++;
      busy = null;
      const isCurrent = captureView();
      pending = pendingRecord(pendingKey()).get();
      candidateRequest = null;
      loadingSource = snap.requestedSourceId ? { id: snap.requestedSourceId, draftDecision: snap.requestedSourceDecision } : null;
      setPreviewRetry();
      note('');
      sourceConfirmation = null;
      restoringTaskId = snap.taskId || null;
      // Keep this tab's snapshot until the shared preview views have loaded it.
      const savedPreview = active && (snap.sourceId || snap.taskId) ? {
        sourceId: snap.sourceId || '',
        origCam: snap.origCam ?? null,
        candCam: snap.candCam ?? null,
        activeCandId: snap.activeCandId,
        zoom: snap.zoom ?? 1
      } : null;
      restoringPreview = savedPreview;
      loaded = ''; activeCandId = ''; task = null;
      clearRepairPreviews();
      $('edit-result').replaceChildren();
      $('edit-source').value = snap.sourceId || '';
      $('edit-prompt').value = snap.prompt || '';
      $('edit-start').value = snap.start || 0;
      $('edit-end').value = snap.end || 0;
      const savedModel = snap.model || DEFAULT_MODEL;
      const savedBaselineModel = snap.baselineModel || savedModel;
      restoreModel(savedModel);
      baselineModel = savedBaselineModel;
      baselinePrompt = snap.baselinePrompt ?? '';
      const savedRange = Array.isArray(snap.baselineRange) && snap.baselineRange[1] > snap.baselineRange[0]
        ? snap.baselineRange : null;
      baselineRange = savedRange ? [...savedRange] : [0, 0];
      pendingActions();
      updateStateBanner();
      if (!active) return;
      run(async () => {
        if (!enabled || modelToRestore) { try { await initCapabilities(); } catch (_) {} }
        if (!isCurrent()) return;
        let openedHistory = false;
        try {
          if (snap.taskId) {
            const preserveForm = Boolean(snap.sourceId || snap.prompt || snap.end);
            const opened = await openTaskById(snap.taskId, {
              candidateId: snap.activeCandId, candidateCamera: snap.candCam, preferAccepted, preserveForm, restoring: true
            });
            if (!opened) return;
            openedHistory = !preserveForm;
          } else if (snap.sourceId) {
            await load({ sourceId: snap.sourceId, trackRequest: false });
          }
        } catch (error) {
          if (isCurrent() && error.name !== 'AbortError') {
            note('恢复预览失败：' + error.message);
            setPreviewRetry(() => retryRepairRestore(savedPreview.sourceId));
            root.Workspace?.saveWorkspaceDebounced?.();
          }
          return;
        }
        if (!isCurrent()) return;
        if (original.clip && Number.isFinite(snap.start) && Number.isFinite(snap.end) && snap.end > snap.start) {
          $('edit-start').value = snap.start;
          $('edit-end').value = snap.end;
          timeline.setRange(snap.start, snap.end);
        }
        if (savedRange) baselineRange = [...savedRange];
        if (!openedHistory) {
          if (snap.baselinePrompt !== undefined) baselinePrompt = snap.baselinePrompt;
          baselineModel = savedBaselineModel;
        }
        if (snap.zoom && timeline.setZoom) timeline.setZoom(snap.zoom);
        original.applyCameraState?.(snap.origCam);
        if (original.clip) original.seek(original.frame);
        restoringPreview = null;
        root.Workspace?.syncTabDirty?.();
        if (snap.requestedSourceId && snap.requestedSourceId !== loaded) {
          await load({ sourceId: snap.requestedSourceId, draftDecision: snap.requestedSourceDecision });
        }
      });
      updateStateBanner();
    },
    async pollTask(tid) {
      if (!tid) return null;
      const isCurrent = captureView();
      const displayedTask = task;
      const res = await request('tasks/' + encodeURIComponent(tid) + '/repair-status', {});
      const updated = taskOf(res);
      if (isCurrent() && displayedTask === task && task && idOf(task) === tid && !busy && !acceptances.has(tid)) {
        render(res);
      }
      return updated;
    },
    suspend() { original.suspend?.(); candidate.suspend?.(); },
    resume() { original.resume?.(); candidate.resume?.(); timeline.draw(); }
  };

  new MutationObserver(() => {
    if (S.session !== session) {
      acceptances.clear();
      submissions.clear();
      viewRevision++;
      busy = null;
      restoringTaskId = null;
      restoringPreview = null;
      setPreviewRetry();
      candidateRequest = null;
      loadingSource = null;
      sourceConfirmation = null;
      session = S.session; task = null; pending = null; loaded = ''; activeCandId = ''; enabled = false;
      baselineRange = [0, 0]; baselinePrompt = '';
      baselineModel = DEFAULT_MODEL; modelToRestore = null;
      clearRepairPreviews();
      $('edit-form').reset(); $('edit-result').replaceChildren(); $('edit-tasks').replaceChildren();
      pendingActions(); updateStateBanner(); note('');
    }
    if (S.view !== 'editor' || !S.user) { timeline.cancel(); original.setPlaying(false); }
    else timeline.draw();
  }).observe(document.body, { attributes: true, attributeFilter: ['data-screen', 'data-view'] });
})();
