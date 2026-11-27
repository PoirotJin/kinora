'use strict';
// Unified single-page workspace coordinator:
// - Pinned 'preview' tab + multi-instance 'generate' / 'editor' / 'retarget' tabs
// - Shared stage hosts hydrated/dehydrated per tab (zero WebGL context bloat)
// - Unsaved draft guard + saved drafts bin + restart persistence per (server, username)
// - Explicit source binding & left-library bridge + cross-pane pointer drag
// - Unified 5-type background task bar & automatic Harness task polling
(() => {
  const root = typeof window !== 'undefined' ? window : globalThis;

  const TAB_LABELS = { generate: '动作生成', editor: '编辑与拼接', retarget: '重定向', assets: '我的资产' };
  const HARNESS_ACTIVE = ['submitting', 'waiting', 'unknown', 'active', 'running', 'queued'];
  let cancelLibraryDrag = null;

  const W = {
    tabs: [],
    activeTabId: 'preview',
    savedDrafts: [],
    draftRestore: null,
    leftCollapsed: false,
    rightCollapsed: false,
    autoCollapsedLeft: false,
    hydrating: false,
    switching: Promise.resolve(),
    harnessTasks: new Map(), // taskId -> { task, kind: 'scene'|'repair'|'splice' }
    harnessPolls: new Set(),
    sceneResumes: new Set(),
    harnessSeedState: 'idle',
    prevTaskStates: new Map(),
    drawerOpen: false,
    drawerFilter: 'all',
    latestCompletion: null,
    saveTimer: 0
  };

  const uid = () => 'tab_' + randomId().slice(0, 12);

  function storageKey() {
    if (typeof localStorage === 'undefined' || !S.status?.server || !S.user?.username) return null;
    return 'kinora-workspace:v1:' + JSON.stringify([S.status.server, S.user.username]);
  }

  function defaultTabState(kind, opts = {}) {
    if (kind === 'generate') {
      return {
        modality: opts.modality === 'video' ? 'video' : 'text',
        subMode: opts.subMode === 'scene' ? 'scene' : 'text',
        description: opts.prompt || '',
        model: '',
        duration: 4,
        count: 2,
        retryParent: null,
        boundJobId: opts.jobId || null,
        selectedCandidateId: opts.candidateId || null,
        previewMode: null,
        baselineText: opts.prompt || '',
        scene: {
          objects: [], targetId: null, nextId: 1, duration: 4, manual: false,
          text: '', spec: '', taskId: opts.sceneTaskId || null, submittedSnapshot: null, camera: null
        },
        camera: null
      };
    }
    if (kind === 'editor') {
      const subMode = opts.subMode || (opts.sourceId ? 'repair' : 'sequence');
      return {
        subMode,
        sourceLabel: opts.label || '',
        parentId: opts.parentId || null,
        repair: {
          sourceId: subMode === 'repair' ? (opts.sourceId || '') : '',
          start: 0,
          end: 0,
          prompt: '',
          model: 'exact_english',
          baselineRange: [0, 0],
          baselinePrompt: '',
          baselineModel: 'exact_english',
          taskId: opts.repairTaskId || null,
          activeCandId: '',
          zoom: 1,
          origCam: null,
          candCam: null
        },
        sequence: {
          mode: subMode,
          binIds: subMode === 'sequence' && opts.sourceId && !opts.previewAsResult ? [opts.sourceId] : [],
          clips: [],
          transition: 30,
          auto: true,
          selected: -1,
          planMode: false,
          jobId: opts.spliceTaskId || null,
          jobSpec: null,
          savedSpec: '',
          showing: Boolean(opts.previewAsResult),
          resultId: opts.previewAsResult ? (opts.sourceId || null) : null,
          resultQuarantined: opts.resultStatus === 'quarantined',
          camera: null
        }
      };
    }
    if (kind === 'retarget') {
      const robotId = opts.robotId || S.rt.robot || '';
      return {
        sourceId: opts.sourceId || '',
        baselineSourceId: opts.sourceId || '',
        sourceSnapshot: opts.sourceId ? {
          id: opts.sourceId,
          label: opts.label || MotionLoader?.getCachedMeta?.(opts.sourceId)?.label || '',
          parentId: opts.parentId || MotionLoader?.getCachedMeta?.(opts.sourceId)?.parent_id || null
        } : null,
        robotId,
        baselineRobotId: robotId,
        paramsText: opts.paramsText || '',
        baselineParams: opts.paramsText || '',
        viewMode: opts.viewMode || 'robot',
        jobId: opts.jobId || null,
        playingJobId: opts.jobId || null,
        speed: 1,
        loop: true,
        robotCam: null,
        humanCam: null
      };
    }
    return {};
  }

  function shortId(id) {
    if (typeof id !== 'string') return '';
    return id.length > 11 ? id.slice(0, 10) + '…' : id;
  }

  function sequenceClips(state) {
    const clips = state.sequence?.clips;
    return Array.isArray(clips) ? clips.filter(clip => typeof clip?.id === 'string') : [];
  }

  function computeTabTitle(tab) {
    const sameKind = W.tabs.filter(t => t.kind === tab.kind);
    const idx = sameKind.length > 1 ? ' #' + (sameKind.indexOf(tab) + 1) : '';
    const st = tab.state || {};
    if (tab.kind === 'generate') {
      const actors = Array.isArray(st.scene?.actors) ? st.scene.actors : [];
      const actorCount = actors.length || 1;
      const suffix = st.modality === 'video' ? ' · 视频'
        : actorCount > 1 ? ` · ${actorCount}角色`
        : (actors.some(actor => actor?.steps?.length) || st.subMode === 'scene' && st.scene?.objects?.length) ? ' · 场景'
        : (typeof st.description === 'string' && st.description ? ' · ' + st.description.slice(0, 8) : '');
      return TAB_LABELS.generate + idx + suffix;
    }
    if (tab.kind === 'editor') {
      if (st.subMode === 'repair') {
        const sid = st.repair?.sourceId;
        return '编辑修正' + idx + (sid ? ' · ' + shortId(sid) : '');
      }
      const clipIds = sequenceClips(st).map(clip => shortId(clip.id));
      return '动作拼接' + idx + (clipIds.length ? ' · ' + clipIds.join('+') : '');
    }
    if (tab.kind === 'retarget') {
      const rLabel = S.robots.find(r => r.id === st.robotId)?.label?.split(' ')[0] || st.robotId || '';
      const sid = st.sourceId ? shortId(st.sourceId) : '';
      const detail = [sid, rLabel].filter(Boolean).join('→');
      return TAB_LABELS.retarget + idx + (detail ? ' · ' + detail : '');
    }
    return TAB_LABELS[tab.kind] || tab.kind;
  }

  function readGenerateSettings() {
    const duration = $('gen-duration')?.value ?? '4';
    const count = $('gen-count')?.value ?? '2';
    return {
      model: $('gen-model')?.value || '',
      duration: duration === '' ? '' : Number(duration),
      count: count === '' ? '' : Number(count)
    };
  }

  function generateSettingsChanged(current, baseline) {
    return Boolean(baseline && (current.model !== baseline.model
      || current.duration !== baseline.duration || current.count !== baseline.count));
  }

  function retargetConfigChanged(state, params, robotId, sourceId) {
    return params.trim() !== (state.baselineParams || '')
      || robotId !== (state.baselineRobotId ?? state.robotId ?? '')
      || sourceId !== (state.baselineSourceId ?? state.sourceId ?? '');
  }

  function dehydrateActiveTab() {
    if (W.hydrating || W.activeTabId === 'preview') return;
    const tab = W.tabs.find(t => t.id === W.activeTabId);
    if (!tab) return;
    if (tab.kind === 'generate') {
      const modality = document.body?.dataset?.genModality === 'video' ? 'video' : 'text';
      const subMode = document.body?.dataset?.genMode === 'scene' ? 'scene' : 'text';
      const view = root.SceneController?.view;
      const camera = view?.cameraState?.();
      tab.state = {
        ...tab.state,
        modality,
        subMode,
        description: $('gen-text')?.value || '',
        ...readGenerateSettings(),
        retryParent: S.gen.retryParent || null,
        scene: root.SceneController?.getState?.() || tab.state.scene,
        camera: camera ? { ...camera, playing: view.playing } : null
      };
      tab.dirty = computeLiveDirty(tab);
    } else if (tab.kind === 'editor') {
      const subMode = root.SequenceEditor?.getMode?.() || tab.state.subMode || 'sequence';
      tab.state = {
        ...tab.state,
        subMode,
        repair: root.RepairEditor?.getState?.() || tab.state.repair,
        repairDirty: Boolean(root.RepairEditor?.isDirty?.()),
        sequence: root.SequenceEditor?.getState?.() || tab.state.sequence
      };
      tab.dirty = computeLiveDirty(tab);
    } else if (tab.kind === 'retarget') {
      const sid = $('rt-motion').value.trim();
      const playingJobId = S.rt.playing || null;
      const job = S.rt.playingJob;
      const previousPlayback = tab.state.sourceId === sid && tab.state.playingJobId === playingJobId ? tab.state.playback : null;
      tab.state = {
        ...tab.state,
        sourceId: sid,
        sourceSnapshot: S.rt.sourceSnapshot?.id === sid ? S.rt.sourceSnapshot : (sid ? {
          id: sid,
          label: $('rt-source').textContent || '',
          parentId: MotionLoader?.getCachedMeta?.(sid)?.parent_id || null
        } : null),
        robotId: S.rt.robot || '',
        paramsText: $('rt-params').value || '',
        viewMode: S.rt.viewMode || 'robot',
        playingJobId,
        playingJob: job ? {
          id: job.id,
          motion_id: job.motion_id,
          robot: job.robot,
          status: job.status,
          owner: job.owner,
          export_verified: job.export_verified,
          source_quarantined: root.isRetargetSourceQuarantined?.(job) ?? job.source_quarantined,
          export_reason: job.export_reason,
          repair_available: job.repair_available,
          source_info: job.source_info?.parent_id ? { parent_id: job.source_info.parent_id } : null
        } : null,
        playback: root.retargetPlaybackState?.() || previousPlayback || null,
        speed: Number($('rt-speed')?.value) || 1,
        loop: $('rt-loop')?.getAttribute('aria-pressed') !== 'false',
        robotCam: root.robotView?.cameraState?.() || null,
        humanCam: root.rtHumanView?.cameraState?.() || null
      };
      tab.dirty = computeLiveDirty(tab);
    }
    tab.title = computeTabTitle(tab);
  }

  function computeLiveDirty(tab) {
    if (!tab) return false;
    if (tab.id !== W.activeTabId) return Boolean(tab.dirty);
    if (tab.kind === 'generate') {
      const sceneChanged = tab.state.scene?.submittedSnapshot
        ? root.SceneController?.hasUnsubmittedChanges?.()
        : root.SceneController?.isDirty?.();
      if (sceneChanged) return true;
      const txt = ($('gen-text')?.value || '').trim();
      return txt !== (tab.state.baselineText || '')
        || (document.body?.dataset?.genModality !== 'video'
          && !root.SceneController?.shouldHandleComposerSubmit?.()
          && generateSettingsChanged(readGenerateSettings(), tab.state.baselineSettings));
    }
    if (tab.kind === 'editor') {
      return Boolean(root.RepairEditor?.isDirty?.() || root.SequenceEditor?.isDirty?.());
    }
    if (tab.kind === 'retarget') {
      return retargetConfigChanged(tab.state, $('rt-params').value || '', S.rt.robot || '', $('rt-motion').value.trim());
    }
    return false;
  }

  function setGenerateModality(modality) {
    const isVideo = modality === 'video';
    if ((document.body?.dataset?.genModality === 'video') !== isVideo) root.invalidateTextPreview?.();
    if (document.body?.dataset) document.body.dataset.genModality = isVideo ? 'video' : 'text';
    $('gen-modality-text')?.setAttribute('aria-selected', String(!isVideo));
    $('gen-modality-video')?.setAttribute('aria-selected', String(isVideo));
    if ($('gen-text-pane')) $('gen-text-pane').hidden = isVideo;
    if ($('gen-video-pane')) $('gen-video-pane').hidden = !isVideo;
    if ($('gen-model')) $('gen-model').hidden = isVideo;
    if ($('gen-duration-wrap')) $('gen-duration-wrap').hidden = isVideo;
    if ($('gen-duration-hint')) $('gen-duration-hint').hidden = isVideo;
    if ($('gen-count-wrap')) {
      $('gen-count-wrap').hidden = isVideo || Boolean(root.SceneController?.isMultiOrStepMode?.());
    }
    if (!W.hydrating) root.SceneController?.syncModalityFromComposer?.(isVideo ? 'video' : 'text');
    if (isVideo) root.VideoMotion?.refresh?.();
    root.updateChecks?.();
  }

  function setGenerateSubMode(mode) {
    const isScene = mode === 'scene';
    if ((document.body?.dataset?.genMode === 'scene') !== isScene) root.invalidateTextPreview?.();
    if (document.body?.dataset) document.body.dataset.genMode = isScene ? 'scene' : 'text';
    $('gen-mode-text')?.setAttribute('aria-selected', String(!isScene));
    $('gen-mode-scene')?.setAttribute('aria-selected', String(isScene));
    if ($('scene-editor')) $('scene-editor').hidden = false;
    if ($('gen-submit')) $('gen-submit').hidden = false;
    if ($('gen-scene-submit-proxy')) $('gen-scene-submit-proxy').hidden = true;
    if ($('gen-text')) $('gen-text').readOnly = false;
    if ($('gen-count-wrap')) {
      const isVideo = document.body?.dataset?.genModality === 'video';
      $('gen-count-wrap').hidden = isVideo || Boolean(root.SceneController?.isMultiOrStepMode?.());
    }
    const sceneReady = root.SceneController?.open?.();
    root.updateChecks?.();
    return sceneReady;
  }

  async function hydrateTab(tab, { preferAcceptedRepair = false } = {}) {
    const session = S.session;
    W.hydrating = true;
    try {
      if (!tab || tab === 'preview') {
        root.applyStageView('library');
        renderSourceBridge();
        root.ensureLibraryPreview?.();
        return;
      }
      const st = tab.state;
      if (tab.kind === 'assets') {
        root.applyStageView('assets');
      } else if (tab.kind === 'generate') {
        S.gen.tabKey = tab.id;
        S.gen.retryParent = st.retryParent || null;
        root.VideoMotion?.setTab?.(tab.id);
        root.applyStageView('generate');
        const modelSelect = $('gen-model');
        modelSelect?.querySelector?.('[data-missing-retry-model]')?.remove();
        const models = [...(modelSelect?.options || [])];
        const preserveRetryModel = Boolean(st.retryParent && st.model);
        let model = models.find(option => option.value === st.model && (preserveRetryModel || !option.disabled));
        if (!model && preserveRetryModel && modelSelect) {
          model = h('option', {
            value: st.model, disabled: true, text: '原改写模型不可用 · ' + st.model,
            'data-missing-retry-model': true
          });
          modelSelect.append(model);
        }
        model ||= models.find(option => !option.disabled);
        if (model) modelSelect.value = model.value;
        const durationInput = $('gen-duration');
        const duration = String(st.duration ?? st.scene?.duration ?? 4);
        if (durationInput) durationInput.value = duration;
        if ($('gen-count')) $('gen-count').value = String(st.count ?? 2);
        if ($('gen-text')) $('gen-text').value = st.description || '';
        await root.SceneController?.setState?.(st.scene || {}, tab.id, true);
        if (session !== S.session) return;
        setGenerateModality(st.modality || 'text');
        await setGenerateSubMode(st.subMode || 'text');
        if (session !== S.session) return;
        if (durationInput && st.modality !== 'video' && !root.SceneController?.shouldHandleComposerSubmit?.()) {
          durationInput.value = duration;
        }
        st.baselineSettings ??= readGenerateSettings();
        const restoreText = st.modality !== 'video' && st.previewMode === 'text' &&
          Boolean(st.boundJobId && st.selectedCandidateId);
        if (st.boundJobId && (restoreText || !st.scene?.taskId)) {
          S.gen.open.add(st.boundJobId);
          const job = S.gen.jobs.find(j => j.id === st.boundJobId) ||
            S.gen.recentJobs?.find(j => j.id === st.boundJobId);
          const items = (S.gen.resultData.get(st.boundJobId) || []).filter(x => x.state === 'done');
          const cand = st.selectedCandidateId || items[0]?.motion_id;
          const playback = restoreText ? st.camera : null;
          if (cand && (items.length || !restoreText)) root.previewGeneratedCandidate?.(cand, job || { id: st.boundJobId }, items, playback);
          else root.loadCustomResults?.(st.boundJobId, { showInStage: true, job, preferredMotionId: cand, playback });
        } else if (!st.scene?.taskId) {
          root.SceneController?.drawDraft?.();
        }
        root.SceneController?.view?.applyCameraState?.(st.camera);
        root.updateChecks?.();
        if (!restoreText) root.SceneController?.restoreTask?.(st.camera)?.catch(quiet);
      } else if (tab.kind === 'editor') {
        root.applyStageView('editor');
        const subMode = st.subMode || 'repair';
        root.SequenceEditor?.setState?.({ ...(st.sequence || {}), mode: subMode }, tab.id, true)?.catch(quiet);
        await root.RepairEditor?.setState?.(st.repair || {}, tab.id, true, { preferAccepted: preferAcceptedRepair });
        if (session !== S.session) return;
        root.SequenceEditor?.setMode?.(subMode);
      } else if (tab.kind === 'retarget') {
        root.clearRetargetPlayback?.();
        S.rt.tabKey = tab.id;
        S.rt.sourceSnapshot = st.sourceSnapshot || null;
        S.rt.robot = st.robotId || '';
        st.baselineRobotId ??= S.rt.robot;
        st.baselineSourceId ??= st.sourceId || '';
        for (const r of document.querySelectorAll('#robot-list input[name="robot"]')) {
          r.checked = r.value === S.rt.robot;
        }
        $('rt-motion').value = st.sourceId || '';
        $('rt-params').value = st.paramsText || '';
        if ($('rt-speed')) $('rt-speed').value = String(st.speed || 1);
        if ($('rt-loop')) $('rt-loop').setAttribute('aria-pressed', String(st.loop !== false));
        if (root.robotView) { root.robotView.speed = st.speed || 1; root.robotView.loop = st.loop !== false; }
        if (root.rtHumanView) { root.rtHumanView.speed = st.speed || 1; root.rtHumanView.loop = st.loop !== false; }
        root.applyStageView('retarget');
        root.setRetargetViewMode?.(st.viewMode || 'robot');
        root.syncRetargetSource?.();
        root.robotView?.applyCameraState?.(st.robotCam);
        root.rtHumanView?.applyCameraState?.(st.humanCam);
        const jobId = st.playingJobId;
        const savedJob = st.playingJob?.id === jobId ? st.playingJob : null;
        // Only current job responses can confirm eligibility for formal export.
        const cachedJob = savedJob?.export_verified === true ? { ...savedJob, export_verified: undefined } : savedJob;
        const job = S.rt.items.find(j => j.id === jobId) || S.rt.recentJobs?.find(j => j.id === jobId) || cachedJob;
        if (job?.status === 'done' && (savedJob || job.motion_id === st.sourceId)) {
          root.playRetarget?.(job, { frame: st.robotCam?.frame, playback: st.playback });
        } else if (st.playback && st.viewMode !== 'robot') {
          root.ensureRetargetHumanLoaded?.(st.sourceId, st.playback);
        }
      }
      renderSourceBridge();
    } finally {
      if (session === S.session) {
        W.hydrating = false;
        renderTabs();
        root.refreshLibraryCardBadges?.();
      }
    }
  }

  function renderTabs() {
    const previewBtn = $('tab-preview');
    if (previewBtn) {
      previewBtn.setAttribute('aria-selected', String(W.activeTabId === 'preview'));
    }
    const host = $('ws-tabs');
    if (!host) return;
    host.replaceChildren(...W.tabs.map(tab => {
      tab.title = computeTabTitle(tab);
      const active = W.activeTabId === tab.id;
      const dirty = active ? computeLiveDirty(tab) : Boolean(tab.dirty);
      tab.dirty = dirty;
      const dotTone = tab.badgeState === 'done' ? 'ok' : tab.badgeState === 'failed' ? 'bad' : tab.badgeState === 'running' ? 'run' : '';
      return h('div', {
        class: 'ws-tab-item' + (active ? ' active' : ''),
        role: 'presentation'
      },
        h('button', {
          type: 'button',
          role: 'tab',
          class: 'ws-tab',
          'aria-selected': String(active),
          dataset: { tabId: tab.id },
          onclick: () => activateTab(tab.id)
        },
          dotTone ? h('i', { class: 'ws-tab-state ' + dotTone, 'aria-hidden': 'true' }) : null,
          h('span', { class: 'ws-tab-label', text: tab.title }),
          dirty ? h('i', { class: 'ws-dirty-dot', title: '有未保存的编辑草稿', 'aria-label': '未保存草稿' }) : null),
        h('button', {
          type: 'button',
          class: 'ws-tab-close',
          'aria-label': '关闭 ' + tab.title,
          title: '关闭标签页（已提交后台任务继续运行）',
          onclick: e => { e.stopPropagation(); closeTab(tab.id); }
        }, '×'));
    }));
  }

  async function switchTab(tabId, options, session) {
    if (session !== S.session) return;
    if (tabId !== 'preview' && !W.tabs.some(t => t.id === tabId)) tabId = 'preview';
    if (W.activeTabId === tabId && !W.hydrating) {
      renderTabs();
      renderSourceBridge();
      return;
    }
    cancelLibraryDrag?.();
    root.invalidateTextPreview?.();
    dehydrateActiveTab();
    W.activeTabId = tabId;
    const target = tabId === 'preview' ? 'preview' : W.tabs.find(t => t.id === tabId);
    if (target && target !== 'preview' && target.badgeState === 'done') target.badgeState = '';
    if (tabId !== 'preview' && window.innerWidth < 1220 && !W.leftCollapsed) {
      W.leftCollapsed = true;
      W.autoCollapsedLeft = true;
      syncPaneCollapseDOM();
    } else if (tabId === 'preview' && W.autoCollapsedLeft && window.innerWidth >= 1100) {
      W.leftCollapsed = false;
      W.autoCollapsedLeft = false;
      syncPaneCollapseDOM();
    }
    await hydrateTab(target, options);
  }

  function activateTab(tabId, options) {
    const session = S.session;
    W.switching = W.switching.then(async () => {
      await switchTab(tabId, options, session);
      if (session === S.session) saveWorkspaceDebounced();
    }).catch(quiet);
    return W.switching;
  }

  function createTab(kind, opts = {}) {
    dehydrateActiveTab();
    const tab = {
      id: uid(),
      kind,
      title: TAB_LABELS[kind] || kind,
      dirty: false,
      badgeState: '',
      createdAt: Date.now(),
      state: defaultTabState(kind, opts)
    };
    W.tabs.push(tab);
    return tab;
  }

  async function activateOrCreateTab(kind, opts = {}) {
    if (!opts.forceNew) {
      const existing = W.tabs.find(t => t.kind === kind);
      if (existing) {
        const session = S.session;
        await activateTab(existing.id);
        if (session !== S.session) return;
        if (kind === 'generate' && opts.subMode) await setGenerateSubMode(opts.subMode);
        if (kind === 'editor' && opts.subMode) root.SequenceEditor?.setMode?.(opts.subMode);
        return existing;
      }
    }
    const tab = createTab(kind, opts);
    await activateTab(tab.id);
    return tab;
  }

  // ---------- Draft Protection Guard ----------
  let dismissDraftGuard = null;

  function askDraftDecision(descriptionText) {
    const dialog = $('draft-guard-dialog');
    if (!dialog || typeof dialog.showModal !== 'function') return Promise.resolve('discard');
    if (dialog.open) return Promise.resolve('cancel');
    $('draft-guard-desc').textContent = descriptionText || '当前标签页包含未保存的编辑草稿。请选择如何处理：';
    dialog.showModal();
    return new Promise(resolve => {
      const finish = choice => {
        dismissDraftGuard = null;
        cleanup();
        if (dialog.open) dialog.close();
        resolve(choice);
      };
      const onSave = () => finish('save');
      const onDiscard = () => finish('discard');
      const onCancel = e => { if (e) e.preventDefault(); finish('cancel'); };
      dismissDraftGuard = () => finish('cancel');
      const cleanup = () => {
        $('draft-guard-save').removeEventListener('click', onSave);
        $('draft-guard-discard').removeEventListener('click', onDiscard);
        $('draft-guard-cancel').removeEventListener('click', onCancel);
        dialog.removeEventListener('cancel', onCancel);
      };
      $('draft-guard-save').addEventListener('click', onSave);
      $('draft-guard-discard').addEventListener('click', onDiscard);
      $('draft-guard-cancel').addEventListener('click', onCancel);
      dialog.addEventListener('cancel', onCancel);
    });
  }

  function stateWithPendingSequenceImport(tab) {
    const state = tab.state;
    const pendingImport = tab.kind === 'editor' && root.SequenceEditor?.getPendingImport?.(tab.id);
    if (!pendingImport || !state.sequence) return state;
    const sequence = {
      ...state.sequence,
      binIds: [...(state.sequence.binIds || [])],
      pendingAdds: [...(state.sequence.pendingAdds || [])]
    };
    root.SequenceEditor.deferImportedMotion(sequence, pendingImport.id, pendingImport.addAt);
    return { ...state, sequence };
  }

  function saveTabAsDraft(tab) {
    if (tab.id === W.activeTabId) dehydrateActiveTab();
    const copy = JSON.parse(JSON.stringify({
      id: 'draft_' + randomId().slice(0, 10),
      kind: tab.kind,
      title: tab.title,
      savedAt: Math.floor(Date.now() / 1000),
      state: stateWithPendingSequenceImport(tab)
    }));
    W.savedDrafts.unshift(copy);
    renderTaskBar();
    saveWorkspaceDebounced();
    toast('草稿已保存至底部任务栏“已存草稿”');
  }

  function reopenSavedDraft(draftId) {
    if (W.draftRestore) return W.switching;
    const draft = W.savedDrafts.find(item => item.id === draftId);
    if (!draft) return;
    const session = S.session;
    const restore = { draftId, tabId: null };
    W.draftRestore = restore;
    renderTaskBar();
    W.switching = W.switching.then(async () => {
      if (session !== S.session) return;
      const previous = W.activeTabId;
      const tab = {
        id: uid(),
        kind: draft.kind,
        title: draft.title || TAB_LABELS[draft.kind],
        dirty: true,
        badgeState: '',
        createdAt: Date.now(),
        state: JSON.parse(JSON.stringify(draft.state))
      };
      flushSave();
      restore.tabId = tab.id;
      W.tabs.push(tab);
      try {
        await switchTab(tab.id, undefined, session);
        if (session === S.session) W.savedDrafts = W.savedDrafts.filter(item => item !== draft);
      } catch (error) {
        if (session !== S.session) return;
        quiet(error);
        W.tabs = W.tabs.filter(item => item !== tab);
        try {
          await switchTab(previous, undefined, session);
        } catch (fallbackError) {
          if (session !== S.session) return;
          quiet(fallbackError);
          await switchTab('preview', undefined, session);
        }
        if (session === S.session) toast('暂时无法打开草稿，已保留原草稿，可重试', true);
      }
    }).finally(() => {
      if (W.draftRestore !== restore) return;
      W.draftRestore = null;
      if (session === S.session) {
        renderTaskBar();
        flushSave();
      }
    }).catch(quiet);
    return W.switching;
  }

  async function closeTab(tabId) {
    const idx = W.tabs.findIndex(t => t.id === tabId);
    if (idx < 0) return false;
    const tab = W.tabs[idx];
    if (tab.id === W.activeTabId) dehydrateActiveTab();
    const pendingImport = tab.kind === 'editor' && root.SequenceEditor?.getPendingImport?.(tabId);
    if (tab.dirty || pendingImport) {
      const choice = await askDraftDecision(`“${tab.title}”有尚未提交或保存的修改。关闭标签页不会取消已提交的后台任务。`);
      if (choice === 'cancel') return false;
      if (choice === 'save') saveTabAsDraft(tab);
      else if (tab.id === W.activeTabId) {
        if (tab.kind === 'editor') { root.RepairEditor?.clearDraft?.(); root.SequenceEditor?.clearDraft?.(); }
        if (tab.kind === 'generate') root.SceneController?.clearDraft?.();
      }
    }
    const wasActive = W.activeTabId === tabId;
    W.tabs.splice(idx, 1);
    if (tab.kind === 'generate') root.VideoMotion?.clearTab?.(tabId);
    if (tab.kind === 'retarget') S.rt.submissionErrors?.delete(tabId);
    if (wasActive) {
      const fallback = W.tabs[idx] || W.tabs[idx - 1] || null;
      await activateTab(fallback ? fallback.id : 'preview');
    } else {
      renderTabs();
      root.refreshLibraryCardBadges?.();
      saveWorkspaceDebounced();
    }
    return true;
  }

  // ---------- Cross-Tab Motion Handoff & Source Bridge ----------
  function boundSourceOfTab(tab) {
    if (!tab) return [];
    const st = tab.state || {};
    if (tab.kind === 'editor') {
      if (tab.id === W.activeTabId) {
        const mode = root.SequenceEditor?.getMode?.() || st.subMode;
        if (mode === 'repair') {
          const s = root.RepairEditor?.getLoadedSource?.();
          return s ? [s] : [];
        }
        return root.SequenceEditor?.getClipIds?.() || [];
      }
      if (st.subMode === 'repair') return st.repair?.sourceId ? [st.repair.sourceId] : [];
      return sequenceClips(st).map(clip => clip.id);
    }
    if (tab.kind === 'retarget') {
      const s = tab.id === W.activeTabId ? $('rt-motion').value.trim() : st.sourceId;
      return s ? [s] : [];
    }
    return [];
  }

  function getMotionUsageLabels(motionId) {
    if (!motionId) return [];
    const out = new Set();
    for (const tab of W.tabs) {
      const ids = boundSourceOfTab(tab);
      if (ids.includes(motionId)) {
        out.add(tab.kind === 'editor' ? '编辑中' : '重定向源');
      }
    }
    return [...out];
  }

  async function openRepairSource(tab, motionId, opts) {
    const repair = root.RepairEditor, session = S.session;
    const alreadyLoaded = repair?.getLoadedSource?.() === motionId;
    const loaded = await repair?.loadMotion?.(motionId, { beforeLoad: () => root.SequenceEditor?.setMode?.('repair') });
    if (!loaded || alreadyLoaded || session !== S.session || W.activeTabId !== tab.id) return;
    tab.state.sourceLabel = opts.label || '';
    tab.state.parentId = opts.parentId || null;
    renderSourceBridge();
    renderTabs();
    saveWorkspaceDebounced();
  }

  async function openMotionInTab(kind, motionId, opts = {}) {
    if (!motionId) return;
    const editorMode = opts.previewAsResult || opts.subMode === 'sequence' ? 'sequence' : 'repair';
    const result = opts.previewAsResult ? { motion_id: motionId, status: opts.resultStatus } : null;
    const session = S.session;
    dehydrateActiveTab();
    if (!opts.forceNew) {
      const exact = W.tabs.find(t => {
        if (t.kind !== kind) return false;
        if (kind !== 'editor') return boundSourceOfTab(t).includes(motionId);
        if (editorMode === 'repair') return t.state.repair?.sourceId === motionId;
        if (opts.previewAsResult) return t.state.sequence?.resultId === motionId;
        return sequenceClips(t.state).some(clip => clip.id === motionId);
      });
      if (exact) {
        await activateTab(exact.id);
        if (session !== S.session || W.activeTabId !== exact.id) return exact;
        if (kind === 'editor') {
          if (editorMode === 'repair') await openRepairSource(exact, motionId, opts);
          else {
            root.SequenceEditor?.setMode?.('sequence');
            if (result) await root.SequenceEditor?.showTaskResult?.(result);
          }
        } else if (kind === 'retarget' && opts.robotId && opts.robotId !== S.rt.robot) {
          await applyMotionToRetarget(motionId, opts);
        }
        return exact;
      }
      const activeEditor = kind === 'editor' && W.tabs.find(t => t.id === W.activeTabId && t.kind === 'editor');
      const sameKind = activeEditor || W.tabs.find(t => t.kind === kind);
      if (sameKind) {
        await activateTab(sameKind.id);
        if (session !== S.session || W.activeTabId !== sameKind.id) return sameKind;
        if (kind === 'editor') {
          if (editorMode === 'repair') await openRepairSource(sameKind, motionId, opts);
          else {
            root.SequenceEditor?.setMode?.('sequence');
            if (result) await root.SequenceEditor?.showTaskResult?.(result);
            else {
              const count = root.SequenceEditor.getState().clips.length;
              await root.SequenceEditor.importMotion(motionId, { addAt: count < 2 ? count : null });
              renderSourceBridge();
              saveWorkspaceDebounced();
            }
          }
          return sameKind;
        }
        if (kind === 'retarget') {
          await applyMotionToRetarget(motionId, opts);
          return sameKind;
        }
      }
    }
    // Create new tab bound to motionId
    const tab = createTab(kind, {
      sourceId: motionId,
      label: opts.label || MotionLoader?.getCachedMeta?.(motionId)?.label || '',
      parentId: opts.parentId || MotionLoader?.getCachedMeta?.(motionId)?.parent_id || null,
      subMode: kind === 'editor' ? editorMode : opts.subMode,
      robotId: opts.robotId,
      previewAsResult: opts.previewAsResult,
      resultStatus: opts.resultStatus
    });
    await activateTab(tab.id);
    if (kind === 'editor' && editorMode === 'sequence' && !opts.previewAsResult && session === S.session && W.activeTabId === tab.id) {
      await root.SequenceEditor?.importMotion?.(motionId, { addAt: 0 });
    }
    return tab;
  }

  async function applyMotionToRetarget(motionId, opts = {}) {
    const tab = W.tabs.find(t => t.id === W.activeTabId && t.kind === 'retarget');
    const sourceChanged = $('rt-motion').value.trim() !== motionId;
    const robotChanged = opts.robotId !== S.rt.robot && S.robots?.some(r => r.id === opts.robotId);
    if (tab && (sourceChanged || robotChanged) && computeLiveDirty(tab)) {
      const next = sourceChanged ? `切换源动作为 ${motionId}` : '更换目标机器人';
      const decision = await askDraftDecision(`当前重定向页有未提交的设置。${next}前，请选择：`);
      if (decision === 'cancel') return;
      if (decision === 'save') saveTabAsDraft(tab);
    }
    const meta = MotionLoader?.getCachedMeta?.(motionId);
    S.rt.sourceSnapshot = {
      id: motionId,
      label: opts.label || meta?.label || '',
      parentId: opts.parentId || meta?.parent_id || null
    };
    if (robotChanged) {
      S.rt.robot = opts.robotId;
      for (const r of document.querySelectorAll('#robot-list input[name="robot"]')) {
        r.checked = r.value === S.rt.robot;
      }
    }
    $('rt-motion').value = motionId;
    root.syncRetargetSource?.();
    if (S.rt.viewMode !== 'robot') root.ensureRetargetHumanLoaded?.(motionId);
    renderSourceBridge();
    renderTabs();
    root.refreshLibraryCardBadges?.();
    saveWorkspaceDebounced();
  }

  async function openGenerateWithPrompt(text, { forceNew = false } = {}) {
    const session = S.session;
    const tab = await activateOrCreateTab('generate', { subMode: 'text', modality: 'text', prompt: text, forceNew });
    if (session !== S.session || tab?.id !== W.activeTabId) return;
    if (computeLiveDirty(tab) && $('gen-text').value.trim() !== (text || '').trim()) {
      const decision = await askDraftDecision('当前生成页已有未提交的动作描述草稿。是否覆盖？');
      if (decision === 'cancel' || session !== S.session || W.activeTabId !== tab.id) return;
      if (decision === 'save') saveTabAsDraft(tab);
    }
    setGenerateModality('text');
    setGenerateSubMode('text');
    $('gen-text').value = text || '';
    if (/^A person /.test(text || '') && [...$('gen-model').options].some(o => o.value === 'exact_english')) {
      $('gen-model').value = 'exact_english';
    }
    tab.state.baselineText = '';
    root.updateChecks?.();
    $('gen-text').focus?.();
  }

  async function openGenerateRetry(settings) {
    const session = S.session;
    dehydrateActiveTab();
    let tab = W.tabs.find(t => t.kind === 'generate' && t.state.modality === 'text'
      && t.state.subMode === 'text' && t.state.retryParent === settings.retryParent);
    if (!tab) {
      tab = createTab('generate');
      Object.assign(tab.state, settings);
      tab.state.scene.duration = settings.duration;
      tab.dirty = true;
    }
    await activateTab(tab.id);
    if (session === S.session && W.activeTabId === tab.id) $('gen-text').focus?.();
  }

  function renderSourceBridge() {
    const append = (target, ...nodes) => target.append(...nodes.filter(node => node != null));
    const bar = $('ws-source-bar'), curBox = $('ws-source-current'), actBox = $('ws-bridge-actions');
    if (!bar || !curBox || !actBox) return;
    if (W.activeTabId === 'preview' || W.activeTabId === null) {
      bar.hidden = true; return;
    }
    const tab = W.tabs.find(t => t.id === W.activeTabId);
    if (!tab || tab.kind === 'generate') {
      bar.hidden = true; return;
    }
    bar.hidden = false;
    const sources = boundSourceOfTab(tab);
    const browsing = S.lib.selected;
    curBox.replaceChildren();
    actBox.replaceChildren();

    if (tab.kind === 'editor') {
      const mode = root.SequenceEditor?.getMode?.() || tab.state.subMode;
      if (mode === 'repair') {
        const sid = sources[0];
        const meta = sid ? MotionLoader?.getCachedMeta?.(sid) : null;
        const parentNote = (tab.state.parentId || meta?.parent_id) ? ` · 源自 ${shortId(tab.state.parentId || meta.parent_id)}` : '';
        append(curBox,
          h('span', { class: 'caps', text: '正在处理源动作' }),
          sid ? h('strong', { class: 'mono', copy: '源动作编号', text: sid }) : h('span', { class: 'muted', text: '未选择源动作' }),
          parentNote ? h('span', { class: 'muted small mono', text: parentNote }) : null,
          sid ? h('button', { class: 'text-btn', type: 'button', text: '在预览查看', onclick: () => root.selectMotion?.(sid, { reveal: true }) }) : null
        );
        if (browsing && browsing !== sid) {
          append(actBox,
            h('span', { class: 'muted small', text: `动作库选中 ${shortId(browsing)}：` }),
            h('button', { class: 'btn sm subtle', type: 'button', text: sid ? '替换源动作' : '载入此动作', onclick: () => openMotionInTab('editor', browsing, { subMode: 'repair' }) }),
            h('button', { class: 'text-btn', type: 'button', text: '新建编辑标签', onclick: () => openMotionInTab('editor', browsing, { subMode: 'repair', forceNew: true }) })
          );
        }
      } else {
        append(curBox,
          h('span', { class: 'caps', text: '当前拼接序列' }),
          sources.length
            ? h('span', { class: 'mono', text: sources.map((s, i) => `${i === 0 ? 'A' : 'B'}: ${shortId(s)}`).join('  →  ') })
            : h('span', { class: 'muted', text: '尚未向时间线添加片段' })
        );
        if (browsing) {
          append(actBox,
            h('span', { class: 'muted small', text: `动作库选中 ${shortId(browsing)}：` }),
            h('button', { class: 'btn sm subtle', type: 'button', text: '＋ 加入素材箱', onclick: () => root.SequenceEditor?.importMotion?.(browsing) }),
            sources.length < 2 ? h('button', { class: 'btn sm subtle', type: 'button', text: sources.length === 0 ? '放入 A 段' : '放入 B 段', onclick: () => root.SequenceEditor?.importMotion?.(browsing, { addAt: sources.length }) }) : null
          );
        }
      }
    } else if (tab.kind === 'retarget') {
      const sid = sources[0];
      const snap = S.rt.sourceSnapshot?.id === sid ? S.rt.sourceSnapshot : null;
      const meta = sid ? MotionLoader?.getCachedMeta?.(sid) : null;
      const parentId = snap?.parentId || meta?.parent_id;
      append(curBox,
        h('span', { class: 'caps', text: '当前重定向源' }),
        sid ? h('strong', { class: 'mono', copy: '源动作编号', text: sid }) : h('span', { class: 'muted', text: '未绑定源动作' }),
        parentId ? h('span', { class: 'muted small mono', text: `源自 ${shortId(parentId)}` }) : null,
        sid ? h('button', { class: 'text-btn', type: 'button', text: '在预览查看', onclick: () => root.selectMotion?.(sid, { reveal: true }) }) : null
      );
      if (browsing && browsing !== sid) {
        append(actBox,
          h('span', { class: 'muted small', text: `动作库选中 ${shortId(browsing)}：` }),
          h('button', { class: 'btn sm subtle', type: 'button', text: sid ? '替换源动作' : '设为源动作', onclick: () => applyMotionToRetarget(browsing) }),
          h('button', { class: 'text-btn', type: 'button', text: '新建重定向标签', onclick: () => openMotionInTab('retarget', browsing, { forceNew: true }) })
        );
      }
    }
  }

  // ---------- Cross-Pane Drag from Left Library ----------
  function beginLibraryCardDrag(e, item) {
    const stage = document.body.dataset.stage;
    if (e.button !== 0 || !item?.motion_id || cancelLibraryDrag || W.hydrating || (stage !== 'editor' && stage !== 'retarget')) return;
    const tabId = W.activeTabId, session = S.session, mode = root.SequenceEditor?.getMode?.();
    const sequence = stage === 'editor' && mode === 'sequence';
    const current = () => session === S.session && !W.hydrating && tabId === W.activeTabId && stage === document.body.dataset.stage &&
      (stage !== 'editor' || mode === root.SequenceEditor?.getMode?.());
    const inDropTarget = ev => {
      const hit = document.elementFromPoint(ev.clientX, ev.clientY);
      return hit && ($('stage-' + stage)?.contains(hit) || $('param-' + stage)?.contains(hit));
    };
    const start = [e.clientX, e.clientY];
    let ghost = null;
    const move = ev => {
      if (ev.pointerId !== e.pointerId) return;
      if (!ev.buttons || !current()) { cleanup(); return; }
      if (!ghost && Math.hypot(ev.clientX - start[0], ev.clientY - start[1]) < 8) return;
      if (!ghost) {
        ghost = h('div', { class: 'bin-ghost', text: '拖入工作区 · ' + shortId(item.motion_id) });
        document.body.append(ghost);
      }
      ghost.style.transform = `translate(${ev.clientX + 12}px,${ev.clientY + 12}px)`;
      if (sequence) {
        $('seq-track')?.classList.toggle('drop', Boolean(inDropTarget(ev) && root.SequenceEditor.overTrack(ev)));
      }
    };
    const cleanup = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      window.removeEventListener('blur', cleanup);
      $('seq-track')?.classList.remove('drop');
      ghost?.remove();
      cancelLibraryDrag = null;
    };
    const up = ev => {
      if (ev.pointerId !== e.pointerId) return;
      const dropped = ghost && ev.type === 'pointerup' && current() && inDropTarget(ev);
      cleanup();
      if (!dropped) return;
      if (sequence) {
        if (root.SequenceEditor.overTrack(ev)) {
          root.SequenceEditor.importMotion(item.motion_id, { addAt: root.SequenceEditor.dropIndex(ev.clientX) });
        } else {
          root.SequenceEditor.importMotion(item.motion_id);
        }
      } else if (stage === 'editor') {
        openMotionInTab('editor', item.motion_id, { label: item.text, subMode: 'repair' });
      } else {
        applyMotionToRetarget(item.motion_id, { label: item.text });
      }
    };
    cancelLibraryDrag = cleanup;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    window.addEventListener('blur', cleanup);
  }

  // ---------- Unified Background Task Bar & Polling ----------
  function classifyHarnessTask(task, hintKind) {
    const goal = String(task.goal || '');
    if (task.scene_group || task.actors_group || task.pair || task.scene || goal.includes('场景') || goal.includes('物体响应') || (task.operations || []).some(o => o.tool === 'generate_motion')) return 'scene';
    if (task.splice) return 'splice';
    if ((task.operations || []).some(o => o.tool === 'edit_motion' && o.arguments?.clips)) return 'splice';
    // The task list omits splice details and operations for direct Cubic Hermite results.
    if (goal.startsWith('拼接 ') && goal.includes('（Cubic Hermite 过渡 ')) return 'splice';
    if (!task.source_motion_id && goal.includes('过渡')) return 'splice';
    return hintKind || 'repair';
  }

  function needsSceneGroupDetails(task) {
    return task?.scene_group && !task.actors_group && !task.pair;
  }

  function harnessTaskState(task) {
    if (task?.status === 'cancelled') return 'cancelled';
    const group = task?.actors_group;
    if (Array.isArray(group) && group.length) {
      if (group.some(a => root.SceneController?.sceneActorFailure?.(a))) return 'failed';
      if (group.some(a => !(a.candidates || []).some(c => c.status !== 'quarantined'))) return 'running';
      return 'done';
    }
    const partner = task?.pair;
    if (partner) {
      const members = [task, partner];
      if (members.some(actor => root.SceneController?.sceneActorFailure?.(actor))) return 'failed';
      return members.every(actor => actor.candidates?.some(candidate => candidate.status !== 'quarantined')) ? 'done' : 'running';
    }
    if (task?.scene_group) return 'running';
    const lastOperationStatus = task?.operations?.at(-1)?.status ?? task?.last_operation_status;
    if (HARNESS_ACTIVE.includes(lastOperationStatus) || HARNESS_ACTIVE.includes(task?.status)) return 'running';
    if (task?.status === 'failed' || lastOperationStatus === 'failed') return 'failed';
    if ((task?.candidates || []).length > 0 || ['succeeded', 'ready', 'awaiting_review', 'accepted'].includes(task?.status)) return 'done';
    return task?.status || 'unknown';
  }

  function sceneHasActiveActors(task) {
    if (task.status === 'cancelled') return false;
    const actors = task.actors_group || (task.pair ? [task, task.pair] : []);
    return actors.some(actor => actor.status !== 'cancelled' &&
      (HARNESS_ACTIVE.includes(actor.status) || HARNESS_ACTIVE.includes(actor.operations?.at(-1)?.status) ||
        root.SceneController?.sceneActorAwaitingSubmission?.(actor)));
  }

  function registerHarnessTask(task, kind) {
    if (!task) return;
    const id = task.id || task.task_id;
    if (!id) return;
    const existing = W.harnessTasks.get(id);
    // A response started before cancellation can arrive after it was acknowledged.
    if (existing?.task.status === 'cancelled' && task.status !== 'cancelled') return existing.task;
    W.harnessTasks.set(id, { task, kind: classifyHarnessTask(task, existing?.kind || kind) });
    checkTaskTransitions();
    renderTaskBar();
    return task;
  }

  async function seedHarnessTasks() {
    if (!S.user || W.harnessSeedState !== 'pending') return;
    const session = S.session;
    W.harnessSeedState = 'loading';
    try {
      const query = { limit: 30 };
      if (S.capabilities?.grouped_tasks === true) query.grouped = 1;
      const res = await cloud('/api/harness/tasks', { query });
      if (session !== S.session) return;
      for (let t of (res.items || [])) {
        const id = t.id || t.task_id;
        if (!id || W.harnessTasks.has(id)) continue;
        if (!t.scene_group && HARNESS_ACTIVE.includes(t.status) && !t.operations) {
          try {
            const detail = await cloud('/api/harness/tasks/' + encodeURIComponent(id));
            t = detail.task || detail;
          } catch (_) { /* retain the visible summary if detail is unavailable */ }
          if (session !== S.session) return;
          if (W.harnessTasks.has(id)) continue;
        }
        W.harnessTasks.set(id, { task: t, kind: classifyHarnessTask(t) });
      }
      checkTaskTransitions();
      renderTaskBar();
      W.harnessSeedState = 'ready';
    } catch (_) {
      if (session === S.session) W.harnessSeedState = 'pending';
    }
  }

  async function pollHarnessTasks() {
    if (!S.user) return;
    const session = S.session, polls = W.harnessPolls;
    const current = () => session === S.session && polls === W.harnessPolls;
    seedHarnessTasks();
    for (const [id, entry] of W.harnessTasks) {
      if (!current()) return;
      if (polls.has(id) || (!needsSceneGroupDetails(entry.task) &&
        harnessTaskState(entry.task) !== 'running' && !sceneHasActiveActors(entry.task))) continue;
      polls.add(id);
      try {
        let updated;
        if (entry.kind === 'scene') updated = await root.SceneController?.pollTask?.(id);
        else if (entry.kind === 'splice') updated = await root.SequenceEditor?.pollTask?.(id);
        else updated = await root.RepairEditor?.pollTask?.(id);
        if (updated && current() && W.harnessTasks.get(id) === entry) {
          W.harnessTasks.set(id, { task: updated, kind: entry.kind });
        }
      } catch (_) { /* transient network error handled by next tick */ }
      finally { polls.delete(id); }
    }
    if (!current()) return;
    checkTaskTransitions();
    renderTaskBar();
  }

  function jobsForTaskBar(visibleJobs, recentJobs) {
    if (!recentJobs) return visibleJobs || [];
    const byId = new Map((visibleJobs || []).map(job => [job.id, job]));
    for (const job of recentJobs) byId.set(job.id, job);
    return byId.values();
  }

  function collectUnifiedTasks() {
    const list = [];
    const sceneQueueIds = new Set();
    for (const {task, kind} of W.harnessTasks.values()) {
      if (kind !== 'scene') continue;
      const actors = task.actors_group || (task.pair ? [task, task.pair] : [task]);
      for (const actor of actors) for (const op of actor.operations || []) {
        if (op.tool === 'generate_motion' && op.arguments?.scene) sceneQueueIds.add('hgen_' + op.id);
      }
    }
    for (const j of jobsForTaskBar(S.gen.jobs, S.gen.history ? S.gen.recentJobs : null)) {
      if (sceneQueueIds.has(j.id)) continue;
      const active = ACTIVE_CUSTOM.includes(j.status);
      const candidateCount = j.done ? `${j.done} 个候选` : '';
      let progressText = candidateCount || STATUS_TEXT(j.status);
      if (active) progressText = j.cancel ? '停止中' : `${j.done}/${j.n}`;
      else if (j.status === 'cancelled' && candidateCount) progressText = `已取消 · ${candidateCount}`;
      list.push({
        uid: 'custom:' + j.id,
        id: j.id,
        type: 'custom',
        typeLabel: '动作生成',
        title: j.description || j.id,
        sourceText: `${j.duration}s × ${j.n}`,
        state: active ? 'running' : j.status === 'completed' ? 'done' : j.status === 'cancelled' ? 'cancelled' : 'failed',
        progressText,
        created: j.created || 0,
        raw: j
      });
    }
    for (const [id, { task, kind }] of W.harnessTasks) {
      const st = harnessTaskState(task);
      const failedWithActiveActors = st === 'failed' && sceneHasActiveActors(task);
      const awaitingSubmission = task.actors_group?.some(a => root.SceneController?.sceneActorAwaitingSubmission?.(a));
      const labelMap = { scene: '场景生成', repair: '片段修正', splice: '动作拼接' };
      list.push({
        uid: 'harness:' + id,
        id,
        type: kind,
        typeLabel: labelMap[kind] || '编辑任务',
        title: task.goal || id,
        sourceText: task.source_motion_id ? `源: ${shortId(task.source_motion_id)}` : '',
        state: st,
        failedWithActiveActors,
        progressText: failedWithActiveActors ? '失败，仍有角色在处理'
          : st === 'running' ? (awaitingSubmission ? '有角色待续交' : '处理中')
          : (st === 'done' && task.candidates?.length ? `${task.candidates.length} 个结果` : STATUS_TEXT(st)),
        created: task.updated || task.created || 0,
        raw: task
      });
    }
    for (const j of jobsForTaskBar(S.rt.items, S.rt.history ? S.rt.recentJobs : null)) {
      const active = ACTIVE_RT.includes(j.status);
      const resultUnavailable = j.source_removed === true || j.result_missing === true || j.result_corrupt === true;
      const rName = S.robots.find(r => r.id === j.robot)?.label || j.robot;
      let progressText = '未通过';
      if (j.status === 'stopping') progressText = '正在取消';
      else if (active) progressText = j.total ? `${Math.round(100 * (j.done || 0) / j.total)}%` : '排队中';
      else if (j.status === 'cancelled') progressText = '已取消';
      else if (resultUnavailable) progressText = j.source_removed ? '源动作已移入回收站'
        : j.result_missing ? '重定向结果文件缺失' : '重定向结果完整性校验失败';
      else if (j.status === 'done') {
        progressText = (root.isRetargetSourceQuarantined?.(j) ?? j.source_quarantined) ? '源动作已隔离' : j.export_verified === true ? '已通过验收'
          : j.export_verified === false ? '待重新验收' : '验收状态未知';
      }
      list.push({
        uid: 'retarget:' + j.id,
        id: j.id,
        type: 'retarget',
        typeLabel: '重定向',
        title: `${shortId(j.motion_id)} → ${rName}`,
        sourceText: j.source_info?.parent_id ? `源自 ${shortId(j.source_info.parent_id)}` : `源: ${shortId(j.motion_id)}`,
        state: active ? 'running' : j.status === 'done' ? 'done' : j.status === 'cancelled' ? 'cancelled' : 'failed',
        progressText,
        resultUnavailable,
        created: j.created || 0,
        raw: j
      });
    }
    return list.sort((a, b) => (b.created || 0) - (a.created || 0));
  }

  function STATUS_TEXT(s) {
    return { done: '已完成', completed: '已完成', failed: '失败', cancelled: '已取消', running: '运行中' }[s] || s || '';
  }

  function setTaskCompletion(item) {
    if (item === W.latestCompletion) return;
    W.latestCompletion = item;
    const notification = $('taskbar-completion');
    if (!notification) return;
    notification.hidden = !item;
    if (item) $('taskbar-completion-text').textContent = `${item.typeLabel}已完成：${item.title.slice(0, 24)}`;
  }

  function checkTaskTransitions() {
    const all = collectUnifiedTasks();
    let completion = W.latestCompletion;
    let newCompletion = null;
    let badgesChanged = false;
    for (const item of all) {
      // A task-list summary gives the first actor's state, which cannot seed group transitions.
      if (needsSceneGroupDetails(item.raw)) continue;
      if (completion?.uid === item.uid) completion = item.state === 'done' && !item.resultUnavailable ? item : null;
      const prev = W.prevTaskStates.get(item.uid);
      if (prev !== item.state) {
        const boundTab = findTabForTask(item);
        if (boundTab && (prev !== undefined || boundTab.badgeState)) {
          const terminal = item.state === 'done' || item.state === 'failed';
          const badge = terminal && boundTab.id !== W.activeTabId ? item.state : '';
          if (boundTab.badgeState !== badge) {
            boundTab.badgeState = badge;
            badgesChanged = true;
          }
        }
        // The task list is already sorted newest first.
        if (prev === 'running' && item.state === 'done' && !item.resultUnavailable) newCompletion ??= item;
      }
      W.prevTaskStates.set(item.uid, item.state);
    }
    if (badgesChanged) {
      renderTabs();
      saveWorkspaceDebounced();
    }
    setTaskCompletion(newCompletion || completion);
  }

  function findTabForTask(item, { protectDraft = false } = {}) {
    return W.tabs.find(t => {
      if (protectDraft && computeLiveDirty(t)) return false;
      const st = t.state || {};
      if (item.type === 'custom' && t.kind === 'generate') {
        return st.boundJobId === item.id && (!protectDraft || !st.scene?.taskId);
      }
      if (item.type === 'scene' && t.kind === 'generate') return st.scene?.taskId === item.id;
      if (item.type === 'repair' && t.kind === 'editor') return st.repair?.taskId === item.id;
      if (item.type === 'splice' && t.kind === 'editor') return st.sequence?.jobId === item.id;
      if (item.type === 'retarget' && t.kind === 'retarget') {
        return (st.jobId === item.id || (!protectDraft && st.playingJobId === item.id)) &&
          st.sourceId === item.raw?.motion_id && st.robotId === item.raw?.robot;
      }
      return false;
    });
  }

  async function openRepairTaskInTab(tab, taskId, item) {
    const session = S.session;
    const state = root.RepairEditor?.getState?.();
    const bookmark = state?.taskId === taskId ? { candidateId: state.activeCandId, candidateCamera: state.candCam } : {};
    const result = await root.RepairEditor?.openTask?.(taskId, { ...bookmark, protectDraft: true, preferAccepted: true });
    if (item && result === 'draft-edited' && session === S.session && W.activeTabId === tab.id) {
      return openTaskResult(item);
    }
  }

  function retargetJobParams(job) {
    try {
      const execution = typeof job.execution === 'string' ? JSON.parse(job.execution) : job.execution;
      const parameters = execution?.parameters;
      if (parameters && typeof parameters === 'object' && !Array.isArray(parameters) &&
          Object.values(parameters).every(Number.isFinite)) {
        return JSON.stringify(parameters, null, 2);
      }
    } catch (_) { /* Some older job records have no usable execution parameters. */ }
    return '';
  }

  async function openTaskResult(item) {
    if (!item) return;
    W.drawerOpen = false;
    syncDrawerDOM();
    if (W.latestCompletion?.uid === item.uid) setTaskCompletion(null);
    dehydrateActiveTab();
    const bound = findTabForTask(item, { protectDraft: true });
    const session = S.session;
    const isActive = tab => session === S.session && W.activeTabId === tab.id;
    if (item.type === 'custom') {
      const tab = bound || createTab('generate', { subMode: 'text', jobId: item.id });
      const alreadyActive = tab.id === W.activeTabId;
      tab.state.subMode = 'text';
      await activateTab(tab.id);
      if (!isActive(tab)) return;
      if (alreadyActive) {
        if (document.body.dataset.genMode !== 'text') setGenerateSubMode('text');
        S.gen.open.add(item.id);
        const { selectedCandidateId, previewMode, camera } = tab.state;
        await root.loadCustomResults?.(item.id, {
          showInStage: true, job: item.raw, preferredMotionId: selectedCandidateId,
          playback: previewMode === 'text' ? camera : null
        });
      }
    } else if (item.type === 'scene') {
      const tab = bound || createTab('generate', { subMode: 'scene', sceneTaskId: item.id });
      const alreadyActive = tab.id === W.activeTabId;
      tab.state.subMode = 'scene';
      if (!alreadyActive) tab.state.previewMode = 'scene';
      await activateTab(tab.id);
      if (!isActive(tab)) return;
      if (alreadyActive) await root.SceneController?.pollTask?.(item.id, { manual: true });
    } else if (item.type === 'repair') {
      const tab = bound || createTab('editor', { subMode: 'repair', repairTaskId: item.id });
      const alreadyActive = tab.id === W.activeTabId;
      tab.state.subMode = 'repair';
      await activateTab(tab.id, { preferAcceptedRepair: true });
      if (!isActive(tab)) return;
      root.SequenceEditor?.setMode?.('repair');
      if (alreadyActive) await openRepairTaskInTab(tab, item.id, item);
    } else if (item.type === 'splice') {
      const tab = bound || createTab('editor', { subMode: 'sequence', spliceTaskId: item.id });
      tab.state.subMode = 'sequence';
      await activateTab(tab.id);
      if (!isActive(tab)) return;
      root.SequenceEditor?.setMode?.('sequence');
      try {
        const latest = await root.SequenceEditor?.pollTask?.(item.id);
        if (latest && session === S.session) registerHarnessTask(latest, item.type);
      } catch (_) { /* keep the last known result while the service is unavailable */ }
      if (!isActive(tab)) return;
      const task = W.harnessTasks.get(item.id)?.task || item.raw;
      const candidate = root.SequenceEditor?.preferredTaskCandidate?.(task);
      if (candidate?.motion_id) await root.SequenceEditor?.showTaskResult?.(candidate);
    } else if (item.type === 'retarget') {
      const paramsText = retargetJobParams(item.raw);
      const tab = bound || createTab('retarget', {
        sourceId: item.raw.motion_id, robotId: item.raw.robot, jobId: item.id, paramsText
      });
      const alreadyActive = tab.id === W.activeTabId;
      if (bound && paramsText && !tab.state.paramsText && !tab.state.baselineParams) {
        tab.state.paramsText = tab.state.baselineParams = paramsText;
        if (alreadyActive) {
          $('rt-params').value = paramsText;
          root.syncRetargetSubmission?.();
        }
      }
      await activateTab(tab.id);
      if (isActive(tab) && item.raw.status === 'done' && !item.resultUnavailable) {
        if (S.rt.playing !== item.id) await root.playRetarget?.(item.raw);
        else if (alreadyActive) await S.rt.retryPreview?.();
      }
    }
    if (session === S.session) saveWorkspaceDebounced();
  }

  async function resumeSceneTask(item) {
    const pending = W.sceneResumes, session = S.session;
    if (!S.user || pending.has(item.id)) return;
    pending.add(item.id);
    renderTaskBar();
    try {
      const task = await cloud('/api/harness/tasks/' + encodeURIComponent(item.id) + '/scene-resume', {
        method: 'POST', body: {}
      });
      if (session !== S.session) return;
      if (task?.id !== item.id) throw Error('服务器未返回对应的场景任务');
      const current = registerHarnessTask(task, 'scene');
      await root.SceneController?.applyTaskUpdates?.([current]);
    } catch (error) {
      if (session === S.session && error.name !== 'AbortError') toast(error.message || '场景续交失败', true);
    } finally {
      pending.delete(item.id);
      if (session === S.session) renderTaskBar();
    }
  }

  function cancelUnifiedTask(item) {
    if (item.type === 'custom') return root.cancelCustomJob?.(item.id);
    if (item.type === 'retarget') return root.cancelRetargetJob?.(item.id);
    return root.runTaskCancellation?.(item.uid,
      () => cloud('/api/harness/tasks/' + encodeURIComponent(item.id) + '/cancel', { method: 'POST', body: {} }),
      async updated => {
        const task = updated.task || updated;
        registerHarnessTask(task, item.type);
        if (item.type !== 'scene') {
          toast('任务已停止');
          return;
        }
        const cancelled = [task];
        for (const member of task.actors_group || (task.pair ? [task.pair] : [])) {
          if (member.task_id === task.id || member.status !== 'cancelled') continue;
          const previous = W.harnessTasks.get(member.task_id)?.task;
          if (!previous) continue;
          const detail = {...previous, ...member, id: member.task_id};
          registerHarnessTask(detail, 'scene');
          cancelled.push(detail);
        }
        toast('已提交场景停止请求');
        await root.SceneController?.applyTaskUpdates?.(cancelled);
      });
  }

  function replaceTaskbarRows(list, rows) {
    const focused = document.activeElement;
    const rowKey = focused && list.contains(focused) ? focused.closest('[data-taskbar-row]')?.dataset.taskbarRow : null;
    list.replaceChildren(...rows);
    if (!rowKey) return;
    const row = [...list.children].find(item => item.dataset.taskbarRow === rowKey);
    const actionKey = button => button.dataset.taskbarAction || button.dataset.taskCancel;
    const action = actionKey(focused);
    const buttons = [...(row?.querySelectorAll('button:not(:disabled)') || [])];
    const sameAction = action && buttons.find(button => actionKey(button) === action);
    const target = sameAction || buttons[0] || document.querySelector('[data-tb-filter][aria-pressed="true"]');
    target?.focus({ preventScroll: true });
  }

  function renderTaskBar() {
    const all = collectUnifiedTasks();
    const running = all.filter(x => x.state === 'running' || x.failedWithActiveActors);
    const pillEl = $('taskbar-active-pill');
    const sumEl = $('taskbar-live-summary');
    if (pillEl) {
      pillEl.className = 'pill ' + (running.length ? 'accent' : '');
      pillEl.textContent = running.length ? `后台运行中 ${running.length}` : `后台任务 (${all.length})`;
    }
    if (sumEl) {
      sumEl.textContent = running.length
        ? running.slice(0, 2).map(r => `${r.typeLabel}: ${r.progressText}`).join('  ·  ')
        : '';
    }
    const draftsBtn = $('taskbar-drafts-btn');
    if (draftsBtn) {
      draftsBtn.hidden = W.savedDrafts.length === 0;
      draftsBtn.textContent = `已存草稿 (${W.savedDrafts.length})`;
    }
    if (!W.drawerOpen) return;
    const listBox = $('taskbar-list');
    if (!listBox) return;
    if (W.drawerFilter === 'drafts') {
      if (!W.savedDrafts.length) {
        replaceTaskbarRows(listBox, [h('div', { class: 'empty', text: '暂无保存的草稿' })]);
        return;
      }
      replaceTaskbarRows(listBox, W.savedDrafts.map(d => h('div', { class: 'tb-row', role: 'listitem', dataset: { taskbarRow: 'draft:' + d.id } },
        h('span', { class: 'pill', text: '草稿 · ' + (TAB_LABELS[d.kind] || d.kind) }),
        h('span', { class: 'tb-title', text: d.title }),
        h('span', { class: 'mono muted small', text: fmtDate(d.savedAt) }),
        h('div', { class: 'row' },
          h('button', { class: 'btn sm subtle', type: 'button', text: '恢复编辑', disabled: Boolean(W.draftRestore), dataset: { taskbarAction: 'reopen' }, onclick: () => reopenSavedDraft(d.id) }),
          h('button', {
            class: 'text-btn', type: 'button', text: '删除', disabled: W.draftRestore?.draftId === d.id,
            dataset: { taskbarAction: 'delete' }, onclick: () => {
              if (W.draftRestore?.draftId === d.id) return;
              W.savedDrafts = W.savedDrafts.filter(x => x.id !== d.id);
              renderTaskBar(); saveWorkspaceDebounced();
            }
          })))));
      return;
    }

    const filtered = W.drawerFilter === 'active' ? running
      : W.drawerFilter === 'done' ? all.filter(x => x.state === 'done')
      : all;
    if (!filtered.length) {
      replaceTaskbarRows(listBox, [h('div', { class: 'empty', text: '当前筛选下暂无任务记录' })]);
      return;
    }
    const toneMap = { running: 'accent', done: 'ok', failed: 'bad', cancelled: '' };
    replaceTaskbarRows(listBox, filtered.slice(0, 40).map(item => h('div', { class: 'tb-row', role: 'listitem', dataset: { taskbarRow: item.uid } },
      h('span', { class: 'pill ' + (toneMap[item.state] || ''), text: `${item.typeLabel} · ${item.progressText}` }),
      h('span', { class: 'tb-title', text: item.title }),
      h('span', { class: 'mono muted small', text: item.sourceText }),
      h('div', { class: 'row tb-actions' },
        h('button', {
          class: 'btn sm subtle', type: 'button',
          dataset: { taskbarAction: 'open' },
          text: item.resultUnavailable ? '查看任务'
            : item.state === 'done' || (item.type === 'custom' && item.raw.done > 0) ? '查看结果' : '打开标签页',
          onclick: () => openTaskResult(item)
        }),
        item.state !== 'cancelled' && item.raw.scene_resume_available === true ? h('button', {
          class: 'btn sm subtle', type: 'button',
          text: W.sceneResumes.has(item.id) ? '正在续交…' : '继续提交',
          disabled: W.sceneResumes.has(item.id),
          dataset: { taskbarAction: 'resume-scene' },
          onclick: () => resumeSceneTask(item)
        }) : null,
        item.state === 'running' || item.failedWithActiveActors ? h('button', {
          class: 'btn sm danger', type: 'button', text: '停止',
          ...root.taskCancellationProps?.(item.uid, Boolean(item.raw.cancel) || item.raw.status === 'stopping'),
          onclick: () => cancelUnifiedTask(item)
        }) : null,
        item.type === 'custom' && root.customRetryCount?.(item.raw) > 0 ? h('button', {
          class: 'text-btn', type: 'button', text: '重试', dataset: { taskbarAction: 'retry' },
          onclick: () => { W.drawerOpen = false; syncDrawerDOM(); root.retryCustomJob?.(item.raw); }
        }) : null
      ))));
  }

  function syncDrawerDOM() {
    if ($('taskbar-drawer')) $('taskbar-drawer').hidden = !W.drawerOpen;
    $('taskbar-toggle')?.setAttribute('aria-expanded', String(W.drawerOpen));
    if ($('taskbar-open-btn')) $('taskbar-open-btn').textContent = W.drawerOpen ? '收起汇总 ▼' : '任务汇总 ▲';
    if (W.drawerOpen) renderTaskBar();
  }

  function setDrawerFilter(filter) {
    W.drawerFilter = filter;
    for (const button of document.querySelectorAll('[data-tb-filter]')) {
      button.setAttribute('aria-pressed', String(button.dataset.tbFilter === filter));
    }
  }

  // ---------- Pane Collapse ----------
  function syncPaneCollapseDOM() {
    const ws = $('workspace');
    if (!ws) return;
    ws.classList.toggle('left-collapsed', W.leftCollapsed);
    ws.classList.toggle('right-collapsed', W.rightCollapsed);
    if ($('ws-left-body')) $('ws-left-body').hidden = W.leftCollapsed;
    if ($('ws-left-rail')) $('ws-left-rail').hidden = !W.leftCollapsed;
    if ($('ws-right-body')) $('ws-right-body').hidden = W.rightCollapsed;
    if ($('ws-right-rail')) $('ws-right-rail').hidden = !W.rightCollapsed;
    $('toggle-left')?.setAttribute('aria-expanded', String(!W.leftCollapsed));
    $('toggle-right')?.setAttribute('aria-expanded', String(!W.rightCollapsed));
  }

  function toggleLeftPane(force) {
    W.leftCollapsed = typeof force === 'boolean' ? force : !W.leftCollapsed;
    W.autoCollapsedLeft = false;
    syncPaneCollapseDOM();
    saveWorkspaceDebounced();
  }

  function toggleRightPane(force) {
    W.rightCollapsed = typeof force === 'boolean' ? force : !W.rightCollapsed;
    syncPaneCollapseDOM();
    saveWorkspaceDebounced();
  }

  // ---------- Persistence ----------
  function flushSave() {
    clearTimeout(W.saveTimer);
    if (W.draftRestore?.tabId) return;
    const key = storageKey();
    if (!key) return;
    try {
      dehydrateActiveTab();
      const payload = {
        version: 1,
        leftCollapsed: W.leftCollapsed,
        rightCollapsed: W.rightCollapsed,
        libraryScope: S.lib.scope || 'all',
        activeTabId: W.activeTabId,
        tabs: W.tabs.map(tab => ({ ...tab, state: stateWithPendingSequenceImport(tab) })),
        savedDrafts: W.savedDrafts,
        pendingAttachments: Array.from(S.gen.pendingAttach || [], ([id, { tabId, spec, requestId, sourceMotionId }]) =>
          [id, { tabId, spec, requestId, sourceMotionId }])
      };
      localStorage.setItem(key, JSON.stringify(payload));
    } catch (_) { /* ignore storage quota errors */ }
  }

  function saveWorkspaceDebounced() {
    if (W.hydrating || !S.user) return;
    clearTimeout(W.saveTimer);
    W.saveTimer = setTimeout(flushSave, 260);
  }

  function restorableWorkspaceEntries(entries) {
    if (!Array.isArray(entries)) return [];
    const ids = new Set();
    return entries.filter(entry => {
      if (typeof entry?.id !== 'string' || !entry.id || entry.id === 'preview' || ids.has(entry.id)
          || !Object.hasOwn(TAB_LABELS, entry.kind) || !entry.state
          || typeof entry.state !== 'object' || Array.isArray(entry.state)) return false;
      ids.add(entry.id);
      return true;
    });
  }

  async function restoreSession() {
    const session = S.session;
    W.harnessSeedState = 'idle';
    W.harnessPolls = new Set();
    W.sceneResumes = new Set();
    W.tabs = [];
    W.activeTabId = 'preview';
    W.savedDrafts = [];
    S.gen.pendingAttach?.clear();
    W.harnessTasks.clear();
    W.prevTaskStates.clear();
    setTaskCompletion(null);

    const key = storageKey();
    let saved = null;
    if (key) {
      try { saved = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { saved = null; }
    }
    const savedScope = saved?.version === 1 && saved.libraryScope;
    root.applyLibraryScope(['all', 'mine', 'results'].includes(savedScope) ? savedScope : 'all');
    if (saved && saved.version === 1) {
      W.leftCollapsed = Boolean(saved.leftCollapsed);
      W.rightCollapsed = Boolean(saved.rightCollapsed);
      W.tabs = restorableWorkspaceEntries(saved.tabs);
      W.savedDrafts = restorableWorkspaceEntries(saved.savedDrafts);
      for (const entry of Array.isArray(saved.pendingAttachments) ? saved.pendingAttachments : []) {
        if (!Array.isArray(entry)) continue;
        const [id, attachment] = entry;
        if (typeof id === 'string' && typeof attachment?.requestId === 'string'
            && attachment.spec?.attachment?.object_id && Array.isArray(attachment.spec.objects)) {
          S.gen.pendingAttach?.set(id, attachment);
        }
      }
      W.activeTabId = saved.activeTabId && W.tabs.some(t => t.id === saved.activeTabId) ? saved.activeTabId : 'preview';
    } else {
      W.leftCollapsed = false;
      W.rightCollapsed = false;
    }
    syncPaneCollapseDOM();
    renderTabs();
    const target = W.activeTabId === 'preview' ? 'preview' : W.tabs.find(t => t.id === W.activeTabId);
    try {
      await hydrateTab(target || 'preview');
    } catch (error) {
      if (session !== S.session || target === 'preview') throw error;
      quiet(error);
      W.activeTabId = 'preview';
      await hydrateTab('preview');
      if (session === S.session) toast('无法恢复上次打开的标签页，已回到动作库', true);
    }
    if (session !== S.session) return;
    checkTaskTransitions();
    renderTaskBar();
    W.harnessSeedState = 'pending';
    seedHarnessTasks();
  }

  function onSessionReset() {
    dismissDraftGuard?.();
    cancelLibraryDrag?.();
    clearTimeout(W.saveTimer);
    W.switching = Promise.resolve();
    W.hydrating = false;
    W.draftRestore = null;
    W.harnessSeedState = 'idle';
    W.harnessPolls = new Set();
    W.tabs = [];
    W.activeTabId = 'preview';
    W.savedDrafts = [];
    W.harnessTasks.clear();
    W.prevTaskStates.clear();
    setTaskCompletion(null);
    W.drawerOpen = false;
    setDrawerFilter('all');
    syncDrawerDOM();
    renderTabs();
    renderSourceBridge();
    root.applyStageView?.('library');
  }

  root.clearSavedWorkspace = () => {
    const key = storageKey();
    W.savedDrafts = [];
    if (key) localStorage.removeItem(key);
  };

  // ---------- Wire UI Controls ----------
  $('toggle-left')?.addEventListener('click', () => toggleLeftPane());
  $('ws-left-rail')?.addEventListener('click', () => toggleLeftPane(false));
  $('toggle-right')?.addEventListener('click', () => toggleRightPane());
  $('ws-right-rail')?.addEventListener('click', () => toggleRightPane(false));

  $('tab-preview')?.addEventListener('click', () => activateTab('preview'));
  $('new-generate-btn')?.addEventListener('click', e => activateOrCreateTab('generate', { subMode: 'text', forceNew: e.altKey || e.shiftKey }));
  $('new-scene-btn')?.addEventListener('click', e => activateOrCreateTab('generate', { subMode: 'scene', forceNew: e.altKey || e.shiftKey }));
  $('preview-empty-gen')?.addEventListener('click', () => activateOrCreateTab('generate', { subMode: 'text' }));
  $('preview-empty-scene')?.addEventListener('click', () => activateOrCreateTab('generate', { subMode: 'scene' }));

  $('tab-new-generate')?.addEventListener('click', () => activateOrCreateTab('generate', { forceNew: true }));
  $('tab-new-editor')?.addEventListener('click', () => activateOrCreateTab('editor', { sourceId: S.lib.selected || '', forceNew: true }));
  $('tab-new-retarget')?.addEventListener('click', () => activateOrCreateTab('retarget', { sourceId: S.lib.selected || '', forceNew: true }));

  $('gen-modality-text')?.addEventListener('click', () => { setGenerateModality('text'); renderTabs(); saveWorkspaceDebounced(); });
  $('gen-modality-video')?.addEventListener('click', () => { setGenerateModality('video'); renderTabs(); saveWorkspaceDebounced(); });
  $('gen-mode-text')?.addEventListener('click', () => { setGenerateSubMode('text'); renderTabs(); saveWorkspaceDebounced(); });
  $('gen-mode-scene')?.addEventListener('click', () => { setGenerateSubMode('scene'); renderTabs(); saveWorkspaceDebounced(); });

  const toggleDrawer = () => { W.drawerOpen = !W.drawerOpen; syncDrawerDOM(); };
  $('taskbar-toggle')?.addEventListener('click', toggleDrawer);
  $('taskbar-open-btn')?.addEventListener('click', toggleDrawer);
  $('taskbar-close-btn')?.addEventListener('click', () => { W.drawerOpen = false; syncDrawerDOM(); });
  $('taskbar-drafts-btn')?.addEventListener('click', () => {
    setDrawerFilter('drafts');
    W.drawerOpen = true; syncDrawerDOM();
  });
  $('taskbar-refresh-all')?.addEventListener('click', () => {
    root.refreshCustom?.(false, { includeRecent: true });
    root.refreshRetarget?.(false, { includeRecent: true });
    pollHarnessTasks();
  });
  $('taskbar-completion-open')?.addEventListener('click', () => {
    if (W.latestCompletion) openTaskResult(W.latestCompletion);
  });
  $('taskbar-completion-dismiss')?.addEventListener('click', () => {
    setTaskCompletion(null);
  });
  for (const b of document.querySelectorAll('[data-tb-filter]')) {
    b.addEventListener('click', () => {
      setDrawerFilter(b.dataset.tbFilter);
      renderTaskBar();
    });
  }

  window.addEventListener?.('beforeunload', () => { if (S.user) flushSave(); });

  root.Workspace = {
    state: W,
    isPreviewActive: () => W.activeTabId === 'preview',
    activateTab,
    createTab,
    activateOrCreateTab,
    closeTab,
    openMotionInTab,
    applyMotionToRetarget,
    openGenerateWithPrompt,
    openGenerateRetry,
    getMotionUsageLabels,
    beginLibraryCardDrag,
    setGenerateModality,
    registerHarnessTask,
    pollHarnessTasks,
    restoreSession,
    onSessionReset,
    flushSave,
    saveWorkspaceDebounced,
    toggleLeftPane,
    toggleRightPane,
    ensureLeftExpanded: () => { if (W.leftCollapsed) toggleLeftPane(false); },
    routeLegacyNav(view) {
      if (view === 'library') return activateTab('preview');
      if (view === 'generate') return activateOrCreateTab('generate');
      if (view === 'editor') return activateOrCreateTab('editor', { sourceId: S.lib.selected || '' });
      if (view === 'retarget') return activateOrCreateTab('retarget', { sourceId: S.lib.selected || '' });
    },
    markTabDirty() {
      if (W.hydrating) return;
      const tab = W.tabs.find(t => t.id === W.activeTabId);
      if (tab) { tab.dirty = true; renderTabs(); saveWorkspaceDebounced(); }
    },
    recordSceneSubmission(tabId, taskId, snapshot, description) {
      const tab = W.tabs.find(t => t.id === tabId && t.kind === 'generate');
      if (!tab) return;
      tab.state.scene = { ...tab.state.scene, taskId, submittedSnapshot: snapshot };
      tab.state.baselineText = description;
      if (tabId === W.activeTabId) tab.dirty = computeLiveDirty(tab);
      else {
        const current = (tab.state.description || '').trim();
        const draftFingerprint = tab.state.scene.draftFingerprint;
        tab.dirty = current !== description
          || (typeof draftFingerprint === 'string' && draftFingerprint !== snapshot.fingerprint);
      }
      renderTabs(); saveWorkspaceDebounced();
    },
    recordObjectResponse(tabId, jobId, taskId) {
      const tab = W.tabs.find(t => t.id === tabId && t.kind === 'generate' && t.state.boundJobId === jobId);
      if (!tab) return false;
      tab.state.scene = { ...tab.state.scene, taskId };
      saveWorkspaceDebounced();
      return true;
    },
    recordRetargetSubmission(tabId, parameters, jobId, robotId, sourceId) {
      const tab = W.tabs.find(t => t.id === tabId && t.kind === 'retarget');
      if (!tab) return;
      tab.state.baselineParams = parameters;
      if (robotId) tab.state.baselineRobotId = robotId;
      if (sourceId) tab.state.baselineSourceId = sourceId;
      if (jobId) tab.state.jobId = jobId;
      if (tabId === W.activeTabId) tab.dirty = computeLiveDirty(tab);
      else {
        const {paramsText = '', robotId = '', sourceId = ''} = tab.state;
        tab.dirty = retargetConfigChanged(tab.state, paramsText, robotId, sourceId);
      }
      renderTabs(); saveWorkspaceDebounced();
    },
    completeSequenceImport(tabId, session, id, src, addAt) {
      const completion = W.switching.then(() => {
        if (session !== S.session) return -1;
        const tab = W.tabs.find(t => t.id === tabId && t.kind === 'editor');
        if (!tab) return -1;
        if (tabId === W.activeTabId) return root.SequenceEditor.addImportedMotion(id, src, addAt);
        const index = root.SequenceEditor.deferImportedMotion(tab.state.sequence, id, addAt);
        tab.dirty ||= root.SequenceEditor.isDirty(tab.state.sequence);
        renderTabs(); saveWorkspaceDebounced();
        return index;
      });
      W.switching = completion.catch(quiet);
      return completion;
    },
    completeSequenceSubmission(tabId, session, pending, task) {
      const completion = W.switching.then(() => {
        if (session !== S.session) return;
        const tab = W.tabs.find(t => t.id === tabId && t.kind === 'editor');
        if (!tab) return;
        const sequenceEditor = root.SequenceEditor;
        if (tabId === W.activeTabId) sequenceEditor.applySubmission(pending, task);
        else if (sequenceEditor.recordSubmission(tab.state.sequence, pending, task)) {
          tab.dirty = tab.state.repairDirty || sequenceEditor.isDirty(tab.state.sequence);
          renderTabs();
        }
        saveWorkspaceDebounced();
      });
      W.switching = completion.catch(quiet);
      return completion;
    },
    completeRepairSubmission(tabId, session, task, submitted, confirmed) {
      const completion = W.switching.then(() => {
        if (session !== S.session) return;
        const tab = W.tabs.find(t => t.id === tabId && t.kind === 'editor');
        if (!tab) return;
        if (tabId === W.activeTabId) {
          root.RepairEditor.applySubmission(task, submitted, confirmed);
        } else {
          const repair = tab.state.repair;
          if (repair.sourceId !== submitted.create.source_motion_id) return;
          repair.taskId = task.id || task.task_id;
          if (confirmed) {
            Object.assign(repair, root.RepairEditor.submissionBaseline(submitted));
            tab.state.repairDirty = root.RepairEditor.isDirty(repair);
            tab.dirty = tab.state.repairDirty || Boolean(root.SequenceEditor?.isDirty?.(tab.state.sequence));
          }
          renderTabs();
        }
        flushSave();
      });
      W.switching = completion.catch(quiet);
      return completion;
    },
    retrySequenceRestore(tabId) {
      const session = S.session;
      W.switching = W.switching.then(async () => {
        if (session !== S.session || tabId !== W.activeTabId) return;
        const tab = W.tabs.find(t => t.id === tabId && t.kind === 'editor');
        if (!tab) return;
        dehydrateActiveTab();
        await hydrateTab(tab);
        if (session === S.session) saveWorkspaceDebounced();
      }).catch(quiet);
      return W.switching;
    },
    syncTabDirty() {
      if (W.hydrating) return;
      const tab = W.tabs.find(t => t.id === W.activeTabId);
      if (!tab) return;
      const next = computeLiveDirty(tab);
      if (tab.dirty !== next) { tab.dirty = next; renderTabs(); }
      saveWorkspaceDebounced();
    },
    recordGenerateSubmission(tabId, jobId, description, retryParent, settings) {
      const tab = W.tabs.find(t => t.id === tabId && t.kind === 'generate');
      if (!tab) return;
      if (jobId && jobId !== tab.state.boundJobId) {
        tab.state.boundJobId = jobId;
        tab.state.selectedCandidateId = null;
        tab.state.previewMode = null;
      }
      tab.state.baselineText = description;
      if (settings) tab.state.baselineSettings = settings;
      if (tab.state.retryParent === retryParent) tab.state.retryParent = null;
      if (tabId === W.activeTabId) tab.dirty = computeLiveDirty(tab);
      else {
        const text = tab.state.description.trim(), scene = tab.state.scene;
        tab.dirty = Boolean(text !== description
          || generateSettingsChanged(tab.state, tab.state.baselineSettings)
          || (scene && !scene.submittedSnapshot && root.SceneController?.isDirty?.(scene)));
      }
      renderTabs(); saveWorkspaceDebounced();
    },
    setGenerateSelectedCandidate(jobId, motionId) {
      const tab = W.tabs.find(t => t.id === W.activeTabId && t.kind === 'generate');
      if (tab) {
        if (jobId) tab.state.boundJobId = jobId;
        tab.state.selectedCandidateId = motionId;
        tab.state.previewMode = 'text';
        saveWorkspaceDebounced();
      }
    },
    setGenerateScenePreview(tabId) {
      const tab = W.tabs.find(t => t.id === tabId && t.kind === 'generate');
      if (tab) {
        tab.state.previewMode = 'scene';
        saveWorkspaceDebounced();
      }
    },
    onLibrarySelectionChanged() {
      renderSourceBridge();
    },
    confirmRepairSourceChange(motionId) {
      return askDraftDecision(`当前编辑页包含未保存修改。替换源动作为 ${motionId} 前，请处理当前草稿：`);
    },
    openRepairTaskFromList(taskId) {
      const active = W.tabs.find(tab => tab.id === W.activeTabId && tab.kind === 'editor');
      const item = collectUnifiedTasks().find(task => task.type === 'repair' && task.id === taskId);
      if (active && !computeLiveDirty(active)) return openRepairTaskInTab(active, taskId, item);
      if (item) return openTaskResult(item);
    },
    saveActiveRepairDraft() {
      const tab = W.tabs.find(item => item.id === W.activeTabId && item.kind === 'editor');
      if (tab) saveTabAsDraft(tab);
    },
    onEditorSourceChanged() {
      renderSourceBridge();
      renderTabs();
      root.refreshLibraryCardBadges?.();
      saveWorkspaceDebounced();
    },
    onEditorModeChanged(mode) {
      const tab = W.tabs.find(t => t.id === W.activeTabId && t.kind === 'editor');
      if (tab && tab.state.subMode !== mode) {
        cancelLibraryDrag?.();
        root.PersonalAssets?.cancelPoseLookup?.();
        tab.state.subMode = mode;
      }
      renderSourceBridge();
      renderTabs();
      saveWorkspaceDebounced();
    },
    onRetargetConfigChanged() {
      if (W.hydrating) return;
      const tab = W.tabs.find(t => t.id === W.activeTabId && t.kind === 'retarget');
      if (tab) tab.dirty = computeLiveDirty(tab);
      renderSourceBridge();
      renderTabs();
      root.refreshLibraryCardBadges?.();
      saveWorkspaceDebounced();
    },
    onCustomJobsUpdated() {
      checkTaskTransitions();
      renderTaskBar();
    },
    onRetargetJobsUpdated() {
      checkTaskTransitions();
      renderTaskBar();
    }
  };
})();
