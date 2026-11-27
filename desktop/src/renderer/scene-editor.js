'use strict';
(() => {
  const root = typeof window !== 'undefined' ? window : globalThis;
  const dialog = $('scene-editor'), form = $('scene-form'), status = $('scene-status');
  if (dialog && typeof dialog.showModal !== 'function') {
    dialog.open = !dialog.hidden;
    dialog.showModal = () => { dialog.open = true; dialog.hidden = false; };
    dialog.close = () => { dialog.open = false; dialog.hidden = true; };
  }

  const ACTOR_COLORS = ['#0b0b0b', '#8a6416', '#2e5b3c', '#57544e'];
  const MAX_ACTORS = 4;

  const duration = $('scene-duration'), manual = $('scene-manual');
  const text = $('scene-text'), spec = $('scene-spec');
  const pair = $('scene-pair');
  const attachObjectSelect = $('gen-attach-object');
  const attachModeSelect = $('gen-attach-mode');
  const attachWindowSpan = $('gen-attach-window');
  const attachStartInput = $('gen-attach-start');
  const attachEndInput = $('gen-attach-end');
  const actorTracksList = $('actor-tracks-list');
  const actorTrackAdd = $('actor-track-add');
  const addHandshakeBtn = $('scene-add-handshake');
  const peerInteractionsBox = $('scene-peer-interactions');

  let enabled = false, capChecked = false, taskId = null, tabKey = 'default';
  const runningTabs = new Set();
  let objects = [], target = null, nextId = 1;
  let actors = [], activeActorId = 'actor1', nextActorNum = 2;
  let peerInteractions = [], nextPeerId = 1;
  let submittedSnapshot = null, lastTask = null, viewingResult = false;
  let selectedSceneCandidate = null;
  let viewRevision = 0, lastDraftRevision = 0, previewSerial = 0;
  let viewingTextResult = false, pendingTextPreview = null;
  let pairEnabled = false;
  let syncingAutoPrompt = false;
  let lastSyncedDuration = String(duration?.value || '4');

  const types = Object.entries(ScenePresets.types).filter(([type]) => type !== 'asset');
  const request = (path, body) => cloud('/api/harness/' + path, body === undefined ? {} : { method: 'POST', body });
  const taskRecord = (key = tabKey) => pendingRecord(key === 'default' ? 'scene-task' : 'scene-task:' + key);
  const isBusy = () => runningTabs.has(tabKey);
  const isActive = key => key === tabKey && (!root.Workspace?.state || root.Workspace.state.activeTabId === key);

  function cloneDefaultDims(type) {
    if (typeof ScenePresets.defaultDims === 'function') {
      const d = ScenePresets.defaultDims(type);
      return d ? JSON.parse(JSON.stringify(d)) : null;
    }
    if (type === 'box') return { x: 0.65, y: 0.55, z: 0.50, baseY: 0, mode: 'touch', liftM: 0.32, toX: null, toZ: null };
    if (type === 'stairs') return { width: 1.0, stepHeight: 0.16, stepDepth: 0.42, steps: 3 };
    if (type === 'chair') return { width: 0.54, seatHeight: 0.42, depth: 0.48 };
    return null;
  }

  function partCountFor(item) {
    if (typeof ScenePresets.partsFor === 'function') return ScenePresets.partsFor(item).length;
    if (item.type === 'stairs' && item.dims?.steps) return Math.max(2, Math.min(5, Math.round(item.dims.steps)));
    return ScenePresets.types[item.type].parts.length;
  }

  function makeActor(num, x, z, heading_deg = 0) {
    return {
      id: 'actor' + num,
      number: num,
      label: 'R' + num,
      x,
      z,
      heading_deg,
      robot: '',
      modality: 'text',
      text: '',
      textEdited: false,
      steps: [],
      attach: { objectId: '', mode: 'auto', startS: '', endS: '' }
    };
  }

  function resetActors() {
    actors = [makeActor(1, 0, 0, 0)];
    activeActorId = actors[0].id;
    nextActorNum = 2;
    peerInteractions = [];
    nextPeerId = 1;
  }
  resetActors();

  function activeActor() {
    return actors.find(a => a.id === activeActorId) || actors[0];
  }

  function syncLegacyOrders() {
    const primary = actors[0];
    if (!primary) return;
    for (const item of objects) {
      const idx = primary.steps.indexOf(item.id);
      if (idx >= 0) item.order = idx + 1;
      else delete item.order;
    }
  }

  function actorOrderedItems(actor) {
    if (!actor) return [];
    const mapped = (actor.steps || [])
      .map((id, idx) => {
        const item = objects.find(o => o.id === id);
        return item ? { ...item, order: idx + 1 } : null;
      })
      .filter(Boolean);
    if (mapped.length) return mapped;
    if (actor === actors[0]) {
      const legacyOrdered = objects.filter(o => o.order > 0).sort((a, b) => a.order - b.order);
      if (legacyOrdered.length) return legacyOrdered;
    }
    return [];
  }

  function coCarryActorPair(item) {
    if (actors.length < 2 || item?.type !== 'box' || item.dims?.mode !== 'co_carry') return null;
    const a0 = Math.max(0, actors.findIndex(a => a.id === activeActorId));
    let a1 = actors.findIndex(a => a.id === item.dims.coActorId);
    if (a1 < 0 || a1 === a0) a1 = a0 === 0 ? 1 : 0;
    if (a0 < 0 || a0 >= actors.length || a1 < 0 || a1 >= actors.length || a0 === a1) return null;
    return [a0, a1];
  }

  function isMultiMode() {
    return actors.length > 1
      || peerInteractions.length > 0
      || objects.some(o => o.dims?.mode === 'co_carry');
  }

  function hasAssignedSceneSteps() {
    return actors.some(a => actorOrderedItems(a).length > 0);
  }

  function setUnifiedDuration(val) {
    const str = String(val);
    lastSyncedDuration = str;
    if (duration) duration.value = str;
    if ($('gen-duration') && $('gen-duration').value !== undefined && document.activeElement !== $('gen-duration')) {
      $('gen-duration').value = str;
    }
  }

  function currentDurationSeconds() {
    const sceneVal = Number(duration?.value);
    const compVal = Number($('gen-duration')?.value);
    if (duration && String(duration.value) !== lastSyncedDuration && Number.isFinite(sceneVal) && sceneVal >= 1) {
      return sceneVal;
    }
    if (Number.isFinite(compVal) && compVal >= 1 && $('gen-duration')?.value !== '') return compVal;
    return Number.isFinite(sceneVal) && sceneVal >= 1 ? sceneVal : 4;
  }

  function buildSingleActorScene(actor, seconds) {
    const ordered = actorOrderedItems(actor);
    const stepTarget = ordered[0] || (actor === actors[0] ? target : null);
    if (!stepTarget) return null;
    const itemMap = new Map(ordered.map(o => [o.id, o]));
    const scopedObjects = objects.map(o => itemMap.get(o.id) || { ...o, order: 0 });
    return ScenePresets.request(scopedObjects, stepTarget, seconds);
  }

  function buildMultiActorPayload(seconds) {
    const hasRoutes = hasAssignedSceneSteps();
    const preparedActors = actors.map(a => {
      const hasSteps = Boolean(a.steps?.length);
      const draftText = a.id === activeActorId ? ($('gen-text')?.value ?? a.text) : a.text;
      const rawText = hasSteps && !a.textEdited ? '' : (draftText || '').trim();
      const normalized = rawText.normalize('NFC').replace(/\s+/g, ' ');
      const english = [...normalized].every(c => (c >= ' ' && c <= '~') || '‘’“”–—…'.includes(c) || /\p{Script=Latin}/u.test(c));
      if (!english) throw Error(a.label + ' 的动作描述需要使用英文。');
      return {
        id: a.id,
        label: a.label,
        robot: a.robot || undefined,
        start_xz_m: [Number(a.x) || 0, Number(a.z) || 0],
        x: Number(a.x) || 0,
        z: Number(a.z) || 0,
        heading_deg: Number(a.heading_deg) || 0,
        modality: a.modality,
        steps: [...(a.steps || [])],
        ...(!hasRoutes && !a.textEdited && a === actors[0] && target && !peerInteractions.length && !objects.some(o => Boolean(coCarryActorPair(o)))
          ? { targetId: target.id } : {}),
        ...(rawText ? { text: rawText } : {})
      };
    });
    const interactions = peerInteractions.map(p => ({
      kind: p.kind,
      actors: p.actors.map(id => {
        const idx = actors.findIndex(a => a.id === id);
        return idx >= 0 ? idx : 0;
      }),
      xz_m: [Number(p.x) || 0, Number(p.z) || 0]
    }));
    for (const item of objects) {
      const pairActors = coCarryActorPair(item);
      if (!pairActors) continue;
      const boxId = item.id + '-box';
      if (!interactions.some(it => it.kind === 'co_carry' && it.object_id === boxId)) {
        interactions.push({
          kind: 'co_carry',
          actors: pairActors,
          object_id: boxId,
          size_m: [Number(item.dims.x) || 0.65, Number(item.dims.y) || 0.55, Number(item.dims.z) || 0.5],
          to_xz_m: [
            item.dims.toX !== null && item.dims.toX !== undefined && item.dims.toX !== '' ? Number(item.dims.toX) : item.x + 1.1,
            item.dims.toZ !== null && item.dims.toZ !== undefined && item.dims.toZ !== '' ? Number(item.dims.toZ) : item.z
          ],
          lift_m: Number(item.dims.liftM) || 0.32
        });
      }
    }
    if (typeof ScenePresets.multiRequest === 'function') {
      return ScenePresets.multiRequest(preparedActors, objects, seconds, interactions);
    }
    return {
      duration: seconds,
      objects: ScenePresets.geometry(objects),
      actors: preparedActors,
      interactions
    };
  }

  function currentPayload() {
    const seconds = currentDurationSeconds();
    if (pair?.value) return { kind: pair.value, duration: seconds, x: target?.x || 0, z: target?.z || 0 };
    if (manual?.checked) {
      return { manual: true, text: text?.value.trim() || '', duration: seconds, spec: spec?.value.trim() || '' };
    }
    if (isMultiMode()) return { multi: true, ...buildMultiActorPayload(seconds) };
    const actor = activeActor();
    const ordered = actorOrderedItems(actor);
    const activeTarget = ordered[0] || target;
    if (!activeTarget) return { manual: false, objects: ScenePresets.geometry(objects), target: null, duration: seconds };
    const req = buildSingleActorScene(actor, seconds) || ScenePresets.request(objects, activeTarget, seconds);
    const customText = ($('gen-text')?.value || '').trim();
    if (actor.textEdited && customText) req.text = customText;
    return { manual: false, ...req };
  }

  function currentFingerprint() {
    try { return JSON.stringify(currentPayload()); } catch (_) { return ''; }
  }

  function updateMismatchBanner() {
    const bar = $('gen-scene-mismatch');
    if (!bar || (bar.replaceChildren === undefined && bar.hidden === undefined)) return;
    const mismatch = Boolean(viewingResult && submittedSnapshot && submittedSnapshot.fingerprint !== currentFingerprint());
    bar.hidden = !mismatch;
  }

  function syncComposerHint() {
    const actor = activeActor();
    const ordered = actorOrderedItems(actor);
    const autoBar = $('gen-auto-bar');
    const hasSteps = Boolean(!manual?.checked && (ordered.length > 0 || (target && !attachObjectSelect?.value)));
    if (autoBar) autoBar.hidden = !hasSteps && !isMultiMode();
    const genText = $('gen-text');
    if (!genText || genText.value === undefined) return;
    if (manual?.checked) {
      if (!actor.textEdited) genText.value = text?.value || '';
      return;
    }
    if (pair?.value) {
      if (!actor.textEdited) genText.value = pair.value === 'handshake' ? '双人握手' : '双人合力搬箱';
      return;
    }
    if (!actor.textEdited && (ordered.length > 0 || (target && !attachObjectSelect?.value && actor.modality === 'scene'))) {
      try {
        const req = buildSingleActorScene(actor, currentDurationSeconds());
        syncingAutoPrompt = true;
        genText.value = req?.text || '';
        actor.text = genText.value;
      } catch (error) {
        genText.value = error.message;
      } finally {
        syncingAutoPrompt = false;
      }
    }
  }

  function renderAttachOptions() {
    if (!attachObjectSelect || typeof attachObjectSelect.replaceChildren !== 'function') return;
    const boxes = objects.filter(o => o.type === 'box');
    const validIds = new Set(boxes.map(box => box.id + '-box'));
    for (const actor of actors) {
      if (actor.attach?.objectId && !validIds.has(actor.attach.objectId)) actor.attach.objectId = '';
    }
    const attachment = activeActor()?.attach;
    const opts = [h('option', { value: '', text: '不绑定物体' })];
    for (const b of boxes) {
      const d = b.dims || cloneDefaultDims('box');
      opts.push(h('option', {
        value: b.id + '-box',
        text: `箱子 ${b.number} (${Number(d.x).toFixed(2)}×${Number(d.y).toFixed(2)}×${Number(d.z).toFixed(2)}m)`
      }));
    }
    attachObjectSelect.replaceChildren(...opts);
    attachObjectSelect.value = attachment?.objectId || '';
    if (attachModeSelect) attachModeSelect.value = attachment?.mode || 'auto';
    if (attachStartInput) attachStartInput.value = attachment?.startS ?? '';
    if (attachEndInput) attachEndInput.value = attachment?.endS ?? '';
    syncAttachWindowVisibility();
  }

  function syncAttachWindowVisibility() {
    const hasObj = Boolean(attachObjectSelect?.value);
    if (attachModeSelect) attachModeSelect.hidden = !hasObj;
    const isManual = hasObj && attachModeSelect?.value === 'manual';
    if (attachWindowSpan) attachWindowSpan.hidden = !isManual;
  }

  function renderPeerInteractions() {
    if (addHandshakeBtn) {
      addHandshakeBtn.hidden = actors.length < 2;
      addHandshakeBtn.disabled = isBusy() || actors.length < 2;
    }
    if (!peerInteractionsBox || typeof peerInteractionsBox.replaceChildren !== 'function') return;
    if (!peerInteractions.length) {
      peerInteractionsBox.hidden = true;
      peerInteractionsBox.replaceChildren();
      return;
    }
    peerInteractionsBox.hidden = false;
    peerInteractionsBox.replaceChildren(...peerInteractions.map(peer => {
      const selA = h('select', {
        'aria-label': '握手角色 A',
        onchange: e => { peer.actors[0] = e.currentTarget.value; controls(); drawDraft(); }
      }, ...actors.map(a => h('option', { value: a.id, text: a.label, selected: a.id === peer.actors[0] })));
      selA.value = peer.actors[0];
      const selB = h('select', {
        'aria-label': '握手角色 B',
        onchange: e => { peer.actors[1] = e.currentTarget.value; controls(); drawDraft(); }
      }, ...actors.map(a => h('option', { value: a.id, text: a.label, selected: a.id === peer.actors[1] })));
      selB.value = peer.actors[1];
      return h('div', { class: 'scene-peer-item' },
        h('span', { class: 'pill info', text: '双人握手' }),
        selA,
        h('span', { text: '↔' }),
        selB,
        h('button', {
          type: 'button', class: 'text-btn', text: '移除',
          onclick: () => {
            if (isBusy()) return;
            peerInteractions = peerInteractions.filter(p => p.id !== peer.id);
            renderPeerInteractions();
            controls();
            drawDraft();
          }
        })
      );
    }));
  }

  function renderActorTracks() {
    if (actorTrackAdd) actorTrackAdd.disabled = isBusy() || actors.length >= MAX_ACTORS;
    if (!actorTracksList || typeof actorTracksList.replaceChildren !== 'function') return;
    const robots = root.S?.robots || [];
    actorTracksList.replaceChildren(...actors.map((actor, idx) => {
      const isActive = actor.id === activeActorId;
      const ordered = actorOrderedItems(actor);
      const robotSel = h('select', {
        class: 'actor-track-select',
        'aria-label': `${actor.label} 目标机器人`,
        onchange: e => { actor.robot = e.currentTarget.value; controls(); }
      },
        h('option', { value: '', text: '未指定机器人' }),
        ...robots.map(r => h('option', { value: r.id, text: r.label }))
      );
      robotSel.value = actor.robot || '';

      const modSel = h('select', {
        class: 'actor-track-select',
        'aria-label': `${actor.label} 轨道模态`,
        onchange: e => {
          actor.modality = e.currentTarget.value;
          if (isActive && (actor.modality === 'video' || actor.modality === 'text')) {
            root.Workspace?.setGenerateModality?.(actor.modality);
          } else if (isActive && actor.modality === 'scene') {
            root.Workspace?.setGenerateModality?.('text');
          }
          renderActorTracks();
          syncComposerHint();
          controls();
        }
      },
        h('option', { value: 'text', text: '文本' }),
        h('option', { value: 'video', text: '视频' }),
        h('option', { value: 'scene', text: '场景步骤' })
      );
      modSel.value = actor.modality || 'text';

      const coordInput = (field, title, step = 'any') => h('label', { class: 'actor-track-coord' },
        title,
        h('input', {
          type: 'number', step, value: actor[field],
          'aria-label': `${actor.label} ${title}`,
          oninput: e => {
            const v = e.currentTarget.valueAsNumber;
            if (!Number.isFinite(v)) return;
            actor[field] = v;
            drawDraft();
            controls();
          }
        })
      );

      return h('div', {
        class: 'actor-track-row' + (isActive ? ' active' : ''),
        role: 'listitem'
      },
        h('button', {
          type: 'button',
          class: 'actor-track-pick',
          'aria-pressed': String(isActive),
          text: `${actor.label} · 机器人 ${idx + 1}`,
          onclick: () => selectActor(actor.id)
        }),
        coordInput('x', 'X'),
        coordInput('z', 'Z'),
        coordInput('heading_deg', '朝向°', '15'),
        robotSel,
        modSel,
        ordered.length ? h('div', { class: 'actor-track-steps' }, ...ordered.map((it, sIdx) => h('span', { class: 'actor-step-chip' },
          h('span', { text: `${sIdx + 1}·${ScenePresets.types[it.type].label}${it.number}` }),
          h('button', {
            type: 'button', 'aria-label': '移除步骤', text: '×',
            onclick: () => toggleObjectStepForActor(it, actor)
          })
        ))) : null,
        h('span', { class: 'fill' }),
        h('button', {
          type: 'button',
          class: 'text-btn',
          disabled: isBusy() || actors.length <= 1,
          text: '移除轨道',
          onclick: () => removeActorTrack(actor.id)
        })
      );
    }));
  }

  function selectActor(id) {
    const prev = activeActor();
    if (prev && $('gen-text') && prev.modality !== 'video') {
      prev.text = $('gen-text').value || '';
    }
    activeActorId = id;
    const next = activeActor();
    if ($('gen-text') && next.modality !== 'video') {
      $('gen-text').value = next.text || '';
    }
    if (next.modality === 'video') root.Workspace?.setGenerateModality?.('video');
    else root.Workspace?.setGenerateModality?.('text');
    renderActorTracks();
    renderObjects();
    drawDraft();
    controls();
  }

  function addActorTrack() {
    if (isBusy() || actors.length >= MAX_ACTORS) return;
    const offsets = [[0, 0], [1.2, 0], [-1.2, 0], [0, -1.2]];
    const idx = actors.length;
    const num = nextActorNum++;
    const [dx, dz] = offsets[idx % offsets.length];
    const created = makeActor(num, dx, dz, 0);
    actors.push(created);
    selectActor(created.id);
    root.Workspace?.markTabDirty?.();
  }

  function removeActorTrack(id) {
    if (isBusy() || actors.length <= 1) return;
    actors = actors.filter(a => a.id !== id);
    peerInteractions = peerInteractions.filter(p => p.actors.every(aid => actors.some(a => a.id === aid)));
    for (const item of objects) {
      if (item.dims?.mode === 'co_carry' && actors.length < 2) {
        item.dims.mode = cloneDefaultDims(item.type)?.mode || 'touch';
        item.dims.coActorId = '';
      } else if (item.dims?.coActorId === id) {
        item.dims.coActorId = '';
      }
    }
    if (!actors.some(a => a.id === activeActorId)) activeActorId = actors[0].id;
    syncLegacyOrders();
    renderActorTracks();
    renderPeerInteractions();
    renderObjects();
    drawDraft();
    controls();
    root.Workspace?.markTabDirty?.();
  }

  function removeActorStep(actor, objectId) {
    const idx = actor.steps.indexOf(objectId);
    if (idx < 0) return false;
    actor.steps.splice(idx, 1);
    if (!actor.steps.length) {
      if (actor.modality === 'scene') actor.modality = 'text';
      if (!actor.textEdited) {
        actor.text = '';
        if (actor.id === activeActorId && $('gen-text')) $('gen-text').value = '';
      }
    }
    return true;
  }

  function toggleObjectStepForActor(item, actor = activeActor()) {
    if (isBusy() || manual?.checked || !actor) return;
    if (!removeActorStep(actor, item.id)) {
      actor.steps.push(item.id);
      actor.modality = 'scene';
    }
    syncLegacyOrders();
    const routeDurations = actors.map(track => actorOrderedItems(track).reduce(
      (seconds, step) => seconds + ScenePresets.types[step.type].duration, 0));
    const longestRoute = Math.max(...routeDurations);
    if (longestRoute) setUnifiedDuration(longestRoute);
    renderActorTracks();
    renderObjects();
    drawDraft();
    controls();
  }

  function isMultiOrStepMode() {
    return isMultiMode() || hasAssignedSceneSteps() || Boolean(pair?.value || manual?.checked);
  }

  function shouldHandleComposerSubmit() {
    if (manual?.checked || pair?.value) return true;
    if (isMultiMode()) return true;
    const actor = activeActor();
    if (actor?.modality === 'scene' || actorOrderedItems(actor).length > 0) return true;
    const typed = ($('gen-text')?.value || '').trim();
    if (target && !attachObjectSelect?.value && (!typed || !actor?.textEdited)) return true;
    return false;
  }

  function canSubmitScene() {
    if (isBusy() || !enabled) return false;
    if (pair?.value) return pairEnabled;
    if (manual?.checked) return Boolean(text?.value?.trim() && spec?.value?.trim());
    if (objects.some(o => o.type === 'box' && o.dims?.mode === 'co_carry' && !coCarryActorPair(o))) return false;
    if (isMultiMode()) return actors.every(a => actorOrderedItems(a).length > 0 || (a.text || '').trim().length > 0 || peerInteractions.length > 0 || objects.some(o => Boolean(coCarryActorPair(o))) || Boolean(target));
    return Boolean(target || hasAssignedSceneSteps());
  }

  function submitLabel() {
    if (isMultiMode()) return `生成 ${actors.length} 角色场景`;
    return '生成场景动作';
  }

  const view = new MotionView.View($('scene-view'), {
    onSceneMove(id, dx, dz) {
      if (isBusy() || manual?.checked || viewingResult) return false;
      const item = objects.find(object => object.id === id);
      if (!item) return false;
      item.x = Number((item.x + dx).toFixed(3));
      item.z = Number((item.z + dz).toFixed(3));
      const focus = view.target;
      renderObjects();
      drawDraft();
      view.target = focus;
      return true;
    },
    onActorMove(id, dx, dz) {
      if (isBusy() || manual?.checked || viewingResult) return false;
      const actor = actors.find(a => a.id === id);
      if (!actor) return false;
      actor.x = Number((actor.x + dx).toFixed(3));
      actor.z = Number((actor.z + dz).toFixed(3));
      const focus = view.target;
      renderActorTracks();
      drawDraft();
      view.target = focus;
      return true;
    },
    onFrame(frame) {
      const scrub = $('scene-scrub');
      if (scrub) {
        scrub.value = Math.floor(frame.frame);
        const max = Number(scrub.max) || 0;
        scrub.style?.setProperty?.('--p', max > 0 ? `${((Number(scrub.value) / max) * 100).toFixed(2)}%` : '0%');
      }
      const playBtn = $('scene-play');
      if (playBtn) {
        playBtn.setAttribute?.('aria-label', frame.playing ? '暂停' : '播放');
        const useEl = playBtn.querySelector?.('use');
        if (useEl) useEl.setAttribute('href', frame.playing ? '#i-pause' : '#i-play');
        else playBtn.textContent = frame.playing ? '暂停' : '播放';
      }
      const clock = $('gen-cand-clock');
      if (clock && frame.clip) clock.textContent = `${(frame.frame / frame.clip.fps).toFixed(2)} / ${(frame.clip.frames / frame.clip.fps).toFixed(2)} s`;
    }
  });

  function updateRefreshControl() {
    const refresh = $('scene-refresh');
    if (refresh) refresh.disabled = isBusy() || !enabled || !taskId;
  }

  function setPreviewClip(decoded, { autoplay = false, playback = null, keepPosition = false } = {}) {
    const scrub = $('scene-scrub');
    if (scrub) scrub.max = decoded.meta.frames - 1;
    view.setClip(decoded, keepPosition);
    if (Number.isFinite(playback?.frame)) view.seek(playback.frame);
    if (playback || autoplay) view.setPlaying(playback ? playback.playing === true : true);
  }

  function controls() {
    const manualMode = Boolean(manual?.checked);
    const used = objects.reduce((count, item) => count + partCountFor(item), 0);
    for (const [type] of types) {
      const minNeeded = type === 'chair' ? 6 : type === 'stairs' ? 3 : 1;
      const btn = $('scene-add-' + type);
      if (btn) btn.disabled = isBusy() || manualMode || used + minNeeded > ScenePresets.MAX_PARTS;
    }
    const personalPicker = $('scene-pick-personal');
    if (personalPicker) personalPicker.disabled = isBusy() || manualMode || used >= ScenePresets.MAX_PARTS;
    if ($('scene-limit')) $('scene-limit').hidden = manualMode || used + 6 <= ScenePresets.MAX_PARTS;
    if ($('scene-objects')) $('scene-objects').inert = isBusy() || manualMode;
    if ($('scene-new')) $('scene-new').disabled = isBusy() || (!pair?.value && !objects.length && actors.length <= 1 && !peerInteractions.length && !manualMode && $('scene-result')?.hidden !== false);
    if (pair) pair.disabled = isBusy() || !pairEnabled;
    if (manual) manual.disabled = isBusy() || Boolean(pair?.value);
    if (duration) duration.disabled = isBusy();
    if (text && spec) {
      text.disabled = spec.disabled = isBusy() || !manualMode;
      text.required = spec.required = manualMode;
    }
    const canSubmit = !isBusy() && enabled && canSubmitScene();
    if ($('scene-submit')) $('scene-submit').disabled = !canSubmit;
    if ($('gen-scene-submit-proxy')) $('gen-scene-submit-proxy').disabled = !canSubmit;
    updateRefreshControl();
    if ($('scene-open')) $('scene-open').textContent = objects.length || manualMode ? '继续编辑场景' : '新建场景';
    if ($('gen-count-wrap')) {
      const isVideo = document.body?.dataset?.genModality === 'video';
      $('gen-count-wrap').hidden = isVideo || isMultiOrStepMode();
    }
    renderAttachOptions();
    renderPeerInteractions();
    renderActorTracks();
    syncComposerHint();
    updateMismatchBanner();
    root.updateChecks?.();
  }

  function drawDraft() {
    root.invalidateTextPreview?.();
    lastDraftRevision = ++viewRevision;
    viewingTextResult = false;
    viewingResult = false;
    if ($('scene-result')) $('scene-result').hidden = true;
    if ($('scene-empty')) {
      $('scene-empty').hidden = objects.length > 0 || actors.length > 1;
      if (!objects.length && actors.length <= 1) {
        $('scene-empty').textContent = '在下方输入动作描述、上传动作视频，或在右侧布置交互场景物体';
      }
    }
    if ($('scene-action')) {
      $('scene-action').hidden = !target || Boolean(manual?.checked);
      if (pair?.value) $('scene-action').hidden = false;
      $('scene-action').textContent = pair?.value
        ? '双人交互中心位于所选物体的 X/Z 坐标，未选物体时位于原点。'
        : target ? ScenePresets.types[target.type].action : '';
    }
    const focus = target || { x: activeActor()?.x || 0, z: activeActor()?.z || 0 };
    const actorDrafts = actors.map(a => ({
      id: a.id,
      label: a.label,
      x: Number(a.x) || 0,
      z: Number(a.z) || 0,
      heading_deg: Number(a.heading_deg) || 0,
      active: a.id === activeActorId
    }));
    view.setScene(ScenePresets.geometry(objects), [focus.x, 0.5, focus.z + 0.15], target?.id, actorDrafts);
    if ($('gen-viewport-mode-label')) $('gen-viewport-mode-label').textContent = actors.length > 1 ? `多角色草稿预览 (${actors.length})` : '场景草稿预览';
    updateMismatchBanner();
    root.Workspace?.markTabDirty?.();
  }

  function select(item) {
    if (isBusy() || manual?.checked) return;
    if (item && item !== target && !objects.some(object => object.order) && !hasAssignedSceneSteps()) {
      setUnifiedDuration(ScenePresets.types[item.type].duration);
    }
    target = item;
    for (const object of objects) {
      $('scene-pick-' + object.id)?.setAttribute('aria-pressed', object === target);
    }
    if (status) status.textContent = '';
    drawDraft();
    controls();
  }

  function coordinateField(item, axis, label) {
    return h('label', { class: 'scene-coordinate' }, axis.toUpperCase(),
      h('input', {
        type: 'number', step: 'any', required: true, value: item[axis],
        'aria-label': label + ' ' + axis.toUpperCase() + ' 坐标（米）',
        oninput: event => {
          const input = event.currentTarget;
          if (isBusy() || manual?.checked || !Number.isFinite(input.valueAsNumber)) return;
          item[axis] = input.valueAsNumber;
          drawDraft();
          controls();
        }
      }));
  }

  function dimNumberField(labelText, value, attrs, onUpdate) {
    return h('label', {}, labelText,
      h('input', {
        type: 'number',
        value: value ?? '',
        ...attrs,
        oninput: e => {
          const v = e.currentTarget.valueAsNumber;
          if (isBusy() || manual?.checked) return;
          onUpdate(v, e.currentTarget.value);
          drawDraft();
          controls();
        }
      }));
  }

  function buildObjectDimsNode(item, label) {
    if (item.type === 'point') {
      return h('input', {
        type: 'text', value: item.instruction || '', maxlength: 120,
        placeholder: '留空经过，可填站立、挥手、蹲下或英文动作',
        'aria-label': label + ' 到点动作',
        oninput: event => {
          item.instruction = event.currentTarget.value;
          drawDraft();
          controls();
        }
      });
    }
    if (item.type === 'asset') {
      const {size_m, anchor, hand} = item.asset;
      const contact = anchor ? anchor.name + ' · ' + (hand === 20 ? '左手' : '右手') : '仅摆放';
      return h('small', {class:'muted'}, size_m.join(' × ') + ' m · ' + contact);
    }
    if (!item.dims) item.dims = cloneDefaultDims(item.type);
    const d = item.dims;
    if (item.type === 'box') {
      const modeSelect = h('select', {
        'aria-label': label + ' 交互模式',
        onchange: e => {
          d.mode = e.currentTarget.value;
          if (d.mode === 'co_carry' && actors.length < 2) {
            addActorTrack();
          }
          renderObjects();
          drawDraft();
          controls();
        }
      },
        h('option', { value: 'touch', text: '触碰' }),
        h('option', { value: 'lift', text: '单人抬起' }),
        h('option', { value: 'carry', text: '搬运' }),
        h('option', { value: 'co_carry', text: '双人合抬' })
      );
      modeSelect.value = d.mode || 'touch';

      const children = [
        dimNumberField('离地Y', d.baseY ?? 0, { step: '0.05', min: '0', max: '2', 'aria-label': label + ' 离地 Y' }, v => { if (Number.isFinite(v)) d.baseY = Math.max(0, v); }),
        dimNumberField('宽X', d.x, { step: '0.05', min: '0.05', max: '4', 'aria-label': label + ' 宽 X' }, v => { if (Number.isFinite(v) && v >= 0.05) d.x = v; }),
        dimNumberField('高Y', d.y, { step: '0.05', min: '0.05', max: '3', 'aria-label': label + ' 高 Y' }, v => { if (Number.isFinite(v) && v >= 0.05) d.y = v; }),
        dimNumberField('深Z', d.z, { step: '0.05', min: '0.05', max: '4', 'aria-label': label + ' 深 Z' }, v => { if (Number.isFinite(v) && v >= 0.05) d.z = v; }),
        h('label', {}, '模式', modeSelect)
      ];
      if (['lift', 'carry', 'co_carry'].includes(d.mode)) {
        children.push(dimNumberField('抬高', d.liftM ?? 0.32, { step: '0.05', min: '0.05', max: '1.5', 'aria-label': label + ' 抬起高度' }, v => { if (Number.isFinite(v) && v >= 0) d.liftM = v; }));
      }
      if (['carry', 'co_carry'].includes(d.mode)) {
        children.push(
          dimNumberField('搬运X', d.toX ?? Number((item.x + 1.2).toFixed(2)), { step: '0.1', 'aria-label': label + ' 搬运目标 X' }, (v, raw) => { d.toX = raw === '' ? null : (Number.isFinite(v) ? v : d.toX); }),
          dimNumberField('搬运Z', d.toZ ?? Number((item.z + 1.2).toFixed(2)), { step: '0.1', 'aria-label': label + ' 搬运目标 Z' }, (v, raw) => { d.toZ = raw === '' ? null : (Number.isFinite(v) ? v : d.toZ); })
        );
      }
      if (d.mode === 'co_carry' && actors.length >= 2) {
        const partnerSel = h('select', {
          'aria-label': label + ' 合抬搭档',
          onchange: e => { d.coActorId = e.currentTarget.value; controls(); }
        }, ...actors.filter(a => a.id !== activeActorId).map(a => h('option', { value: a.id, text: a.label })));
        if (d.coActorId) partnerSel.value = d.coActorId;
        children.push(h('label', {}, '搭档', partnerSel));
      }
      return h('div', { class: 'scene-dims' }, ...children);
    }
    if (item.type === 'stairs') {
      return h('div', { class: 'scene-dims' },
        dimNumberField('宽', d.width, { step: '0.05', min: '0.3', max: '4', 'aria-label': label + ' 宽' }, v => { if (Number.isFinite(v) && v >= 0.3) d.width = v; }),
        dimNumberField('单级高', d.stepHeight, { step: '0.02', min: '0.05', max: '0.4', 'aria-label': label + ' 单级高' }, v => { if (Number.isFinite(v) && v >= 0.05) d.stepHeight = v; }),
        dimNumberField('单级深', d.stepDepth, { step: '0.02', min: '0.2', max: '1.0', 'aria-label': label + ' 单级深' }, v => { if (Number.isFinite(v) && v >= 0.2) d.stepDepth = v; }),
        dimNumberField('级数', d.steps, { step: '1', min: '2', max: '5', 'aria-label': label + ' 级数' }, v => {
          if (Number.isFinite(v)) d.steps = Math.max(2, Math.min(5, Math.round(v)));
        })
      );
    }
    if (item.type === 'chair') {
      return h('div', { class: 'scene-dims' },
        dimNumberField('座宽', d.width, { step: '0.05', min: '0.3', max: '2', 'aria-label': label + ' 座宽' }, v => { if (Number.isFinite(v) && v >= 0.3) d.width = v; }),
        dimNumberField('座高', d.seatHeight, { step: '0.02', min: '0.2', max: '0.8', 'aria-label': label + ' 座高' }, v => { if (Number.isFinite(v) && v >= 0.2) d.seatHeight = v; }),
        dimNumberField('座深', d.depth, { step: '0.05', min: '0.25', max: '1.2', 'aria-label': label + ' 座深' }, v => { if (Number.isFinite(v) && v >= 0.25) d.depth = v; })
      );
    }
    return null;
  }

  function renderObjects() {
    const host = $('scene-objects');
    if (!host || typeof host.replaceChildren !== 'function') return;
    const curActor = activeActor();
    host.replaceChildren(...objects.map(item => {
      const label = (item.asset?.name || ScenePresets.types[item.type].label) + ' ' + item.number;
      const stepIdx = curActor ? curActor.steps.indexOf(item.id) : -1;
      const legacyIdx = stepIdx >= 0 ? stepIdx + 1 : (curActor === actors[0] ? item.order : 0);
      const stepText = legacyIdx ? `步骤 ${legacyIdx} ×` : '加入步骤';
      return h('li', {},
        h('button', {
          id: 'scene-pick-' + item.id, type: 'button', class: 'scene-pick',
          'aria-pressed': item === target ? 'true' : 'false', text: label, onclick: () => select(item)
        }),
        h('button', {
          type: 'button', class: 'text-btn', 'aria-label': '移除' + label, text: '移除', onclick: () => {
            if (isBusy() || manual?.checked) return;
            for (const a of actors) removeActorStep(a, item.id);
            if (item.order) for (const other of objects) if (other.order > item.order) other.order--;
            objects = objects.filter(object => object !== item);
            const nextStep = actorOrderedItems(activeActor())[0];
            const replacement = target === item ? objects.find(o => o.id === nextStep?.id) || objects[0] || null : target;
            renderObjects();
            select(replacement);
            if (replacement) $('scene-pick-' + replacement.id)?.focus?.();
            else $(item.type === 'asset' ? 'scene-pick-personal' : 'scene-add-' + item.type)?.focus?.();
          }
        }),
        coordinateField(item, 'x', label),
        coordinateField(item, 'z', label),
        h('button', {
          type: 'button', class: 'text-btn', text: stepText,
          onclick: () => toggleObjectStepForActor(item, activeActor())
        }),
        buildObjectDimsNode(item, label)
      );
    }));
  }

  for (const [type] of types) {
    const addBtn = $('scene-add-' + type);
    if (!addBtn) continue;
    addBtn.onclick = () => {
      if (!addBtn.disabled) addObject(type);
    };
  }

  function addObject(type, asset = null) {
    let slot = 0;
    while (objects.some(item => item.slot === slot)) slot++;
    const column = [0, 1, -1, 2, -2][slot % 5];
    const number = nextId++;
    const item = {
      id: 'object' + number,
      number,
      type,
      slot,
      x: column * 1.8,
      z: Math.floor(slot / 5) * 3.3,
      dims: cloneDefaultDims(type)
    };
    if (asset) item.asset = asset;
    objects.push(item);
    renderObjects();
    select(item);
  }

  function editMode() {
    if ($('scene-manual-fields')) $('scene-manual-fields').hidden = !manual?.checked;
    if (duration) {
      duration.min = manual?.checked ? 1 : 3;
      if (!manual?.checked && Number(duration.value) < 3) setUnifiedDuration(target ? ScenePresets.types[target.type].duration : 4);
    }
    if (manual?.checked && target && !spec?.value && !text?.value) {
      const preset = ScenePresets.request(objects, target, Number(duration?.value || 4));
      if (text) text.value = preset.text;
      if (spec) spec.value = JSON.stringify(preset.scene, null, 2);
    }
    if (status) status.textContent = '';
    drawDraft();
    controls();
  }

  function clearDraft() {
    view.stopDrag?.();
    objects = []; target = null; nextId = 1;
    submittedSnapshot = null; lastTask = null; viewingResult = false;
    resetActors();
    form?.reset?.();
    if (duration) setUnifiedDuration(duration.value || 4);
    if ($('scene-advanced')) $('scene-advanced').open = false;
    if ($('scene-markers')) $('scene-markers').replaceChildren?.();
    if ($('gen-candidates-strip')) $('gen-candidates-strip').hidden = true;
    if ($('gen-auto-bar')) $('gen-auto-bar').hidden = true;
    if (attachObjectSelect) attachObjectSelect.value = '';
    syncAttachWindowVisibility();
    renderObjects();
    editMode();
  }

  async function run(action) {
    const key = tabKey, session = S.session;
    if (runningTabs.has(key)) return;
    runningTabs.add(key);
    controls();
    try { await action(session, key); }
    catch (error) {
      if (session === S.session && isActive(key) && error.name !== 'AbortError' && status) status.textContent = error.message;
    } finally {
      if (session === S.session) {
        runningTabs.delete(key);
        if (isActive(key)) controls();
      }
    }
  }

  function sceneActorFailure(actor) {
    if (['cancelled', 'canceled'].includes(actor.status)) return '已取消';
    const operations = actor.operations || [];
    const failed = operations.find(op => op.status === 'failed');
    if (actor.status === 'failed' || failed) {
      const message = failed?.result?.message;
      return message ? '生成失败：' + message : '生成失败';
    }
    const candidates = actor.candidates || [];
    if (candidates.some(c => c.status !== 'quarantined')) return '';
    const runningStates = ['submitting', 'waiting', 'unknown', 'active', 'running', 'queued'];
    if (runningStates.includes(actor.status) || operations.some(op => runningStates.includes(op.status))) return '';
    if (candidates.length) return '候选未通过检查，已隔离';
    return actor.status === 'budget_exhausted' ? '未生成可用候选' : '';
  }

  const usableSceneCandidate = actor => actor.candidates?.find(candidate => candidate.status !== 'quarantined');
  const sceneActorAwaitingSubmission = actor => actor.status === 'ready' && !actor.operations?.length;

  function pairedPreviewReady(task) {
    return [task, task.pair].every(actor => !sceneActorFailure(actor) && usableSceneCandidate(actor));
  }

  function renderCandidateStrip(task, selectedMotionId) {
    const strip = $('gen-candidates-strip'), box = $('gen-strip-items');
    if (!strip || !box || typeof box.replaceChildren !== 'function') return;
    const groupActors = Array.isArray(task?.actors_group) ? task.actors_group : [];
    const list = task?.candidates || [];
    if (!list.length && !groupActors.length && !task?.pair) { strip.hidden = true; box.replaceChildren(); return; }
    strip.hidden = false;
    const stale = Boolean(submittedSnapshot && submittedSnapshot.fingerprint !== currentFingerprint());

    if (groupActors.length) {
      const allReady = groupActors.every(a => !sceneActorFailure(a) && (a.candidates || []).some(c => c.status !== 'quarantined'));
      if ($('gen-strip-title')) $('gen-strip-title').textContent = `多角色场景 (${groupActors.length})` + (stale ? ' · 旧配置' : '');
      if ($('gen-strip-meta')) $('gen-strip-meta').textContent = task.id || '';
      const chips = [];
      if (allReady) {
        const active = groupActors[0].candidates.some(c => c.motion_id === selectedMotionId && c.status !== 'quarantined');
        chips.push(h('div', { class: 'gen-cand-chip' + (active ? ' active' : '') },
          h('button', {
            type: 'button', class: 'text-btn', 'aria-pressed': String(active),
            text: `多角色合景预览 (${groupActors.length})`,
            onclick: () => run(s => previewSceneCandidate(groupActors[0].candidates.find(c => c.status !== 'quarantined') || groupActors[0].candidates[0], task, s))
          })
        ));
      }
      for (const a of groupActors) {
        const aCand = (a.candidates || []).find(c => c.status !== 'quarantined');
        const failure = sceneActorFailure(a);
        const aLabel = a.label || ('R' + (a.actor_index + 1));
        const actorStatus = failure || (sceneActorAwaitingSubmission(a) ? '待续交' : a.status);
        chips.push(h('div', { class: 'gen-cand-chip' },
          h('span', { class: 'mono small', text: aCand ? `${aLabel} · ${aCand.motion_id.slice(0, 10)}…` : `${aLabel} · ${actorStatus}` }),
          aCand ? h('button', {
            type: 'button', class: 'text-btn', text: `${aLabel} 编辑`,
            onclick: () => root.Workspace?.openMotionInTab('editor', aCand.motion_id, { label: `${aLabel} · ${task.goal || '多角色动作'}`, fromTask: a.task_id })
          }) : null,
          aCand ? h('button', {
            type: 'button', class: 'text-btn', text: '重定向',
            onclick: () => root.Workspace?.openMotionInTab('retarget', aCand.motion_id, { label: `${aLabel} · ${task.goal || '多角色动作'}`, robotId: a.robot || undefined, fromTask: a.task_id })
          }) : null
        ));
      }
      box.replaceChildren(...chips);
      return;
    }

    if ($('gen-strip-title')) $('gen-strip-title').textContent = '场景候选 (' + list.length + ')' + (stale ? ' · 旧配置' : '');
    if ($('gen-strip-meta')) $('gen-strip-meta').textContent = task.id || '';
    const pairReady = !task.pair || pairedPreviewReady(task);
    const chips = list.map((c, idx) => {
      const active = selectedMotionId === c.motion_id;
      const quarantined = c.status === 'quarantined';
      if (typeof MotionLoader !== 'undefined') {
        MotionLoader.rememberMeta(c.motion_id, {
          label: task.goal || text?.value || (target ? ScenePresets.types[target.type].text : '场景交互动作'),
          task_id: task.id,
          status: c.status,
          kind_label: '场景生成'
        });
      }
      return h('div', { class: 'gen-cand-chip' + (active ? ' active' : '') + (quarantined ? ' bad' : '') },
        h('button', {
          type: 'button', class: 'text-btn', 'aria-pressed': String(active),
          disabled: Boolean(task.pair && (!pairReady || quarantined)),
          text: `候选 ${idx + 1} · ${c.motion_id.slice(0, 11)}…` + (quarantined ? '（超差）' : ''),
          onclick: () => run(s => previewSceneCandidate(c, task, s))
        }),
        !quarantined ? h('button', {
          type: 'button', class: 'text-btn', text: '编辑与拼接',
          onclick: () => root.Workspace?.openMotionInTab('editor', c.motion_id, { label: task.goal || '场景候选', fromTask: task.id })
        }) : null,
        !quarantined ? h('button', {
          type: 'button', class: 'text-btn', text: '重定向',
          onclick: () => root.Workspace?.openMotionInTab('retarget', c.motion_id, { label: task.goal || '场景候选', robotId: actors[0]?.robot || undefined, fromTask: task.id })
        }) : null);
    });
    if (task.pair) {
      const pCand = usableSceneCandidate(task.pair);
      const partnerStatus = sceneActorFailure(task.pair) || '等待可用候选';
      chips.push(h('div', { class: 'gen-cand-chip' },
        h('span', { class: 'mono small', text: pCand ? `R2 · ${pCand.motion_id.slice(0, 10)}…` : 'R2 · ' + partnerStatus }),
        pCand ? h('button', {
          type: 'button', class: 'text-btn', text: 'R2 重定向',
          onclick: () => root.Workspace?.openMotionInTab('retarget', pCand.motion_id, { label: 'R2 · ' + (task.goal || '双人场景'), robotId: actors[1]?.robot || undefined, fromTask: task.pair.task_id })
        }) : null
      ));
    }
    box.replaceChildren(...chips);
  }

  async function previewSceneCandidate(candidate, task, session, key = tabKey, playback = null) {
    if (session !== S.session || !isActive(key)) return;
    root.invalidateTextPreview?.();
    const revision = viewRevision;
    const serial = ++previewSerial;
    const current = () => session === S.session && isActive(key) && revision === viewRevision &&
      serial === previewSerial && (!dialog || dialog.open || document.body?.dataset?.stage === 'generate');
    const isMultiGroup = Array.isArray(task.actors_group) && task.actors_group.length > 0;
    let group, clip;
    try {
      group = isMultiGroup
        ? await request('tasks/' + encodeURIComponent(task.id) + '/multi-preview')
        : (task.pair ? await request('tasks/' + encodeURIComponent(task.id) + '/paired-preview') : null);
      clip = group ? group.actors[0] : await request('motions/' + encodeURIComponent(candidate.motion_id) + '/preview');
    } catch (error) {
      if (!current()) return;
      throw error;
    }
    if (!current()) return;
    const frames = clip.joints.length, count = clip.parents.length;
    if (frames < 30 || frames > 3600 || count !== 52 || clip.fps !== 30) throw Error('场景预览尺寸无效');
    const data = new Float32Array(clip.joints.flat(2));
    if (!clip.scene || data.length !== frames * count * 3 || !data.every(Number.isFinite)) throw Error('场景预览格式无效');
    if (group) {
      if (isMultiGroup) {
        const metaActors = group.meta?.actors || [];
        clip.scene.actors = group.actors.map((actorClip, idx) => {
          if (actorClip.fps !== clip.fps || actorClip.frames !== frames || actorClip.parents?.length !== count) {
            throw Error('多角色场景时间轴不匹配');
          }
          return {
            joints: actorClip.joints,
            color: ACTOR_COLORS[idx % ACTOR_COLORS.length],
            label: metaActors[idx]?.label || ('R' + (idx + 1)),
            primary: idx === 0
          };
        });
      } else {
        const partner = group.actors[1];
        if (partner?.fps !== clip.fps || partner?.frames !== frames || partner?.parents?.length !== count) {
          throw Error('双人场景时间轴不匹配');
        }
        clip.scene.partner = partner.joints;
      }
    }
    const keepPosition = viewingResult && Boolean(view.clip) &&
      selectedSceneCandidate?.taskId === task.id && selectedSceneCandidate.motionId === candidate.motion_id;
    viewingResult = true;
    viewingTextResult = false;
    selectedSceneCandidate = { taskId: task.id, motionId: candidate.motion_id };
    setPreviewClip(
      { meta: { frames, joint_count: count, fps: clip.fps, parents: clip.parents, names: clip.names }, data, scene: clip.scene },
      { keepPosition, playback: keepPosition ? null : playback }
    );
    if ($('gen-viewport-mode-label')) $('gen-viewport-mode-label').textContent = (isMultiGroup ? '多角色候选 · ' : '场景候选 · ') + candidate.motion_id;
    if ($('scene-quality')) {
      $('scene-quality').textContent = group
        ? (group.quality?.passed ? (isMultiGroup ? '多角色接触与场景几何检查通过。' : '双人接触与场景几何检查通过。') : (isMultiGroup ? '多角色接触或间距检查未通过。' : '双人接触或场景几何检查未通过。'))
        : ((clip.scene.quality?.passed ? '骨架几何筛查通过。' : '有骨架几何超差。') + ' 绿线为物体，橙线为目标姿态。');
    }
    const jump = frame => { view.setPlaying(false); view.seek(frame); };
    $('scene-markers')?.replaceChildren?.(
      ...(clip.scene.targets || []).map(t => h('button', { type: 'button', class: 'btn sm subtle', text: '关键姿态 · 帧 ' + t.frame, onclick: () => jump(t.frame) })),
      ...(clip.scene.events || []).map(ev => h('button', { type: 'button', class: 'btn sm danger', text: ev.reason + ' · 帧 ' + ev.frame, onclick: () => jump(ev.frame) }))
    );
    if ($('scene-result')) $('scene-result').hidden = false;
    if ($('scene-empty')) $('scene-empty').hidden = true;
    view.dirty = true;
    renderCandidateStrip(task, candidate.motion_id);
    updateMismatchBanner();
    root.Workspace?.setGenerateScenePreview?.(key);
  }

  async function show(result, session, key = tabKey, previewRevision = viewRevision, { background = false, playback = null } = {}) {
    if (session !== S.session) return;
    const received = result.task || result;
    if (!received.id) throw Error('服务器未返回有效的场景任务');
    const task = root.Workspace?.registerHarnessTask?.(received, 'scene') || received;
    if (!isActive(key)) return;
    lastTask = task;
    taskId = task.id;
    taskRecord(key).set(taskId);
    updateRefreshControl();
    if (task.status === 'cancelled') {
      previewSerial++;
      if (status) status.textContent = '场景任务已取消';
      renderCandidateStrip(task, null);
      return;
    }
    const operation = task.operations?.find(op => op.tool === 'generate_motion');
    if (status) status.textContent = operation?.result?.message || sceneActorFailure(task) ||
      (task.candidates?.length ? '场景候选已完成' : '场景作业已保存，后台正在生成。');
    const available = task.candidates?.filter(c => c.status !== 'quarantined') || [];
    const preferred = selectedSceneCandidate?.taskId === task.id ? selectedSceneCandidate.motionId : null;
    const candidate = available.find(c => c.motion_id === preferred) || available[0];
    const sameView = previewRevision === viewRevision;
    const hasTextPreview = viewingTextResult || Boolean(pendingTextPreview);
    const preserveTextPreview = hasTextPreview && (background || !sameView);
    const visibleMotionId = viewingResult ? preferred : null;
    if (!hasTextPreview) renderCandidateStrip(task, visibleMotionId);
    async function previewWhenAvailable(sceneCandidate) {
      if (!sceneCandidate || !sameView) return;
      const resume = playback?.taskId === task.id && playback.motionId === sceneCandidate.motion_id ? playback : null;
      if (!preserveTextPreview) {
        await previewSceneCandidate(sceneCandidate, task, session, key, resume);
      } else if (pendingTextPreview && !viewingTextResult) {
        pendingTextPreview.resumeScene = async () => {
          const current = () => session === S.session && isActive(key) &&
            task === lastTask && previewRevision === viewRevision;
          if (!current()) return;
          renderCandidateStrip(task, visibleMotionId);
          try {
            await previewSceneCandidate(sceneCandidate, task, session, key, resume);
          } catch (error) {
            if (current() && error.name !== 'AbortError' && status) status.textContent = error.message;
          }
        };
      }
    }
    if (Array.isArray(task.actors_group) && task.actors_group.length) {
      const failed = task.actors_group.flatMap((actor, index) => {
        const reason = sceneActorFailure(actor);
        return reason ? [`${actor.label || 'R' + (index + 1)} ${reason}`] : [];
      });
      if (failed.length) {
        if (status) status.textContent = '多角色场景未完成：' + failed.join('；');
        return;
      }
      const pending = task.actors_group.filter(a => !(a.candidates || []).some(c => c.status !== 'quarantined'));
      if (pending.length) {
        const unsubmitted = pending.filter(sceneActorAwaitingSubmission).length;
        if (status) status.textContent = unsubmitted
          ? `多角色场景有 ${unsubmitted} 个角色尚未提交，` +
            (task.scene_resume_available ? '可在任务汇总中继续提交' : '可从原场景重试')
          : `多角色生成中：${task.actors_group.length - pending.length}/${task.actors_group.length} 个角色已就绪`;
        return;
      }
      const firstCand = task.actors_group[0].candidates.find(c => c.status !== 'quarantined');
      await previewWhenAvailable(firstCand);
      return;
    }
    if (task.pair && !pairedPreviewReady(task)) {
      const failed = [task, task.pair].flatMap((actor, index) => {
        const reason = sceneActorFailure(actor);
        return reason ? [`R${index + 1} ${reason}`] : [];
      });
      const readyCount = [task, task.pair].filter(usableSceneCandidate).length;
      if (status) status.textContent = failed.length
        ? '双人场景未完成：' + failed.join('；')
        : `双人场景生成中：${readyCount}/2 个角色已就绪`;
      return;
    }
    await previewWhenAvailable(candidate);
  }

  const statusRequests = new Map();

  async function pollTask(id, { manual = false, playback = null } = {}) {
    if (!id || (!manual && statusRequests.get(id)?.manual)) return null;
    const key = tabKey, session = S.session, revision = viewRevision;
    const requestedDraftRevision = lastDraftRevision;
    const token = { manual };
    statusRequests.set(id, token);
    const latest = () => session === S.session && statusRequests.get(id) === token;
    const current = () => isActive(key) && taskId === id && (manual || !isBusy());
    try {
      const result = await request('tasks/' + encodeURIComponent(id) + '/scene-status', {});
      if (!latest()) return null;
      if (current()) {
        const previewRevision = lastDraftRevision === requestedDraftRevision ? viewRevision : revision;
        await show(result, session, key, previewRevision, { background: !manual || revision !== viewRevision, playback });
      } else if (manual) root.Workspace?.registerHarnessTask?.(result.task || result, 'scene');
      return result.task || result;
    } catch (error) {
      if (!latest()) return null;
      if (current() && revision === viewRevision && error.name !== 'AbortError' && status) status.textContent = error.message;
      throw error;
    } finally {
      if (statusRequests.get(id) === token) statusRequests.delete(id);
    }
  }

  async function ensureCapabilities(session, key) {
    if (capChecked && enabled) return;
    if ($('scene-capability')) $('scene-capability').textContent = '正在检查服务端能力…';
    controls();
    try {
      const caps = await request('capabilities');
      if (session !== S.session) return;
      capChecked = true;
      enabled = caps.scene_interaction === true;
      pairEnabled = caps.paired_scene === true;
      if (isActive(key) && $('scene-capability')) $('scene-capability').textContent = enabled ? '' : '当前服务器未提供桌面场景接口，请升级或启用服务端。';
    } catch (error) {
      if (session === S.session && isActive(key) && $('scene-capability')) $('scene-capability').textContent = '未能检查服务器能否生成场景动作。';
      throw error;
    }
  }

  async function openScenePanel() {
    return run(async (session, key) => {
      enabled = false; capChecked = false;
      taskId = taskRecord().get();
      if (status) status.textContent = '';
      if (!viewingResult) drawDraft();
      dialog?.showModal?.();
      await ensureCapabilities(session, key);
    });
  }

  async function submitSceneOrMulti() {
    if (!canSubmitScene()) return;
    return run(async (session, key) => {
      const seconds = currentDurationSeconds();
      let value, endpoint;
      if (pair?.value) {
        value = currentPayload();
        endpoint = '/api/harness/paired-scene';
      } else if (manual?.checked) {
        try { value = { text: text.value.trim(), duration: seconds, scene: JSON.parse(spec.value) }; }
        catch { throw Error('场景 JSON 格式有误，请检查后重试。'); }
        endpoint = '/api/harness/scene';
      } else if (isMultiMode()) {
        value = buildMultiActorPayload(seconds);
        endpoint = '/api/harness/multi-scene';
      } else {
        const actor = activeActor();
        value = buildSingleActorScene(actor, seconds) || ScenePresets.request(objects, target, seconds);
        const customText = ($('gen-text')?.value || '').trim();
        if (actor.textEdited && customText) value.text = customText;
        endpoint = '/api/harness/scene';
      }
      const description = ($('gen-text')?.value || '').trim();
      const snapshot = {
        fingerprint: currentFingerprint(),
        objects: JSON.parse(JSON.stringify(objects)),
        actors: JSON.parse(JSON.stringify(actors)),
        peerInteractions: JSON.parse(JSON.stringify(peerInteractions)),
        targetId: target?.id || null,
        pair: pair?.value || '',
        duration: seconds,
        manual: Boolean(manual?.checked),
        text: text?.value || '',
        spec: spec?.value || '',
        nextId
      };
      drawDraft();
      const submittedDraftRevision = lastDraftRevision;
      if (status) status.textContent = isMultiMode() ? '正在提交多角色场景作业…' : '正在提交场景作业…';
      const submitKind = key === 'default' ? 'scene' : 'scene:' + key;
      const result = await submitOnce(submitKind, endpoint, value, 'request_id');
      if (session !== S.session) return;
      const id = (result.task || result).id;
      if (!id) throw Error('服务器未返回有效的场景任务');
      if (isActive(key)) submittedSnapshot = snapshot;
      else if (root.Workspace?.state?.tabs.some(tab => tab.id === key)) taskRecord(key).set(id);
      root.Workspace?.recordSceneSubmission?.(key, id, snapshot, description);
      const previewRevision = lastDraftRevision === submittedDraftRevision ? viewRevision : submittedDraftRevision;
      await show(result, session, key, previewRevision, { background: true });
    });
  }

  function getAttachmentRequest() {
    const val = attachObjectSelect?.value;
    if (!val || !objects.length) return null;
    const item = objects.find(o => (o.id + '-box') === val || o.id === val);
    if (!item) return null;
    const geom = ScenePresets.geometry(objects);
    const objectId = item.type === 'box' ? item.id + '-box' : (geom.find(g => g.id.startsWith(item.id + '-'))?.id || val);
    const mode = ['lift', 'carry', 'follow_hands', 'place'].includes(item.dims?.mode) ? item.dims.mode : 'lift';
    const isManual = attachModeSelect?.value === 'manual';
    let start_frame = null, end_frame = null;
    if (isManual) {
      const s = Number(attachStartInput?.value);
      const e = Number(attachEndInput?.value);
      start_frame = Number.isFinite(s) && s >= 0 ? Math.round(s * 30) : 0;
      end_frame = Number.isFinite(e) && e * 30 > start_frame ? Math.round(e * 30) : start_frame + 30;
    }
    return {
      objects: geom,
      attachment: {
        object_id: objectId,
        start_frame,
        end_frame,
        auto_detect: !isManual,
        mode,
        auto_place: true
      }
    };
  }

  async function runObjectResponse(sourceMotionId, attachSpec, { tabId, jobId, requestId } = {}) {
    if (!sourceMotionId || !attachSpec?.attachment?.object_id || !attachSpec?.objects?.length) return null;
    const session = S.session;
    const key = tabId ?? tabKey;
    const requestedViewRevision = viewRevision;
    const requestedDraftRevision = lastDraftRevision;
    if (isActive(key) && status) status.textContent = '正在计算物体响应与抓握耦合…';
    const value = {
      source_motion_id: sourceMotionId,
      objects: attachSpec.objects,
      attachment: attachSpec.attachment
    };
    let result;
    try {
      result = requestId
        ? await request('object-response', { ...value, request_id: requestId })
        : await submitOnce('objresp:' + sourceMotionId, '/api/harness/object-response', value, 'request_id');
    } catch (error) {
      if (session === S.session && isActive(key) && viewRevision === requestedViewRevision &&
          lastDraftRevision === requestedDraftRevision && status && error.name !== 'AbortError') {
        status.textContent = error.message || '物体响应计算失败';
      }
      throw error;
    }
    if (session !== S.session) return null;
    if (jobId) {
      const task = result.task || result;
      if (!task.id) throw Error('服务器未返回有效的场景任务');
      if (!root.Workspace?.recordObjectResponse?.(key, jobId, task.id)) {
        root.Workspace?.registerHarnessTask?.(task, 'scene');
        return result;
      }
      if (!isActive(key)) taskRecord(key).set(task.id);
    }
    const selectedCandidate = jobId
      ? root.Workspace?.state?.tabs.find(tab => tab.id === key)?.state.selectedCandidateId
      : null;
    const background = requestedViewRevision !== viewRevision ||
      Boolean(selectedCandidate && selectedCandidate !== sourceMotionId);
    const previewRevision = lastDraftRevision === requestedDraftRevision ? viewRevision : requestedViewRevision;
    await show(result, session, key, previewRevision, { background });
    return result;
  }

  if ($('scene-open')) $('scene-open').onclick = openScenePanel;
  if ($('scene-new')) $('scene-new').onclick = () => { if (!isBusy()) { clearDraft(); $('scene-add-box')?.focus?.(); } };
  if (pair) pair.onchange = () => { if (pair.value && manual) manual.checked = false; editMode(); };
  if (manual) manual.onchange = editMode;
  if (duration) duration.oninput = () => { updateMismatchBanner(); controls(); root.Workspace?.markTabDirty?.(); };
  if (text?.addEventListener) text.addEventListener('input', () => { syncComposerHint(); updateMismatchBanner(); root.Workspace?.markTabDirty?.(); });
  if (spec?.addEventListener) spec.addEventListener('input', () => { updateMismatchBanner(); root.Workspace?.markTabDirty?.(); });
  if ($('scene-close')) $('scene-close').onclick = () => dialog?.close?.();
  dialog?.addEventListener?.('close', () => view.setPlaying(false));
  if ($('scene-play')) $('scene-play').onclick = () => view.setPlaying(!view.playing);
  if ($('scene-scrub')) $('scene-scrub').oninput = event => {
    const frame = Number(event.target.value);
    view.setPlaying(false);
    view.seek(frame);
  };
  if ($('scene-refresh')) $('scene-refresh').onclick = () => run(() => pollTask(taskId, { manual: true }));
  if ($('gen-view-reset')?.addEventListener) $('gen-view-reset').onclick = () => view.resetCamera();
  if ($('gen-switch-manual')?.addEventListener) $('gen-switch-manual').onclick = () => { if (!isBusy() && manual) { manual.checked = true; editMode(); text?.focus?.(); } };
  if ($('gen-reset-auto-prompt')?.addEventListener) {
    $('gen-reset-auto-prompt').onclick = () => {
      activeActor().textEdited = false;
      syncComposerHint();
      controls();
    };
  }
  if ($('gen-scene-submit-proxy')?.addEventListener) {
    $('gen-scene-submit-proxy').onclick = () => form?.requestSubmit ? form.requestSubmit() : form?.onsubmit?.({ preventDefault() {} });
  }
  if ($('gen-back-draft')?.addEventListener) $('gen-back-draft').onclick = () => { drawDraft(); controls(); };
  if ($('gen-restore-spec')?.addEventListener) {
    $('gen-restore-spec').onclick = () => {
      if (!submittedSnapshot || isBusy()) return;
      objects = JSON.parse(JSON.stringify(submittedSnapshot.objects || []));
      actors = JSON.parse(JSON.stringify(submittedSnapshot.actors || [makeActor(1, 0, 0, 0)]));
      peerInteractions = JSON.parse(JSON.stringify(submittedSnapshot.peerInteractions || []));
      nextId = submittedSnapshot.nextId || (objects.length + 1);
      if (pair) pair.value = submittedSnapshot.pair || '';
      if (manual) manual.checked = Boolean(submittedSnapshot.manual);
      if (duration) duration.value = String(submittedSnapshot.duration || 4);
      if (text) text.value = submittedSnapshot.text || '';
      if (spec) spec.value = submittedSnapshot.spec || '';
      target = objects.find(o => o.id === submittedSnapshot.targetId) || null;
      if ($('scene-manual-fields')) $('scene-manual-fields').hidden = !manual?.checked;
      renderObjects();
      controls();
      updateMismatchBanner();
    };
  }
  if (actorTrackAdd) actorTrackAdd.onclick = addActorTrack;
  if (addHandshakeBtn) {
    addHandshakeBtn.onclick = () => {
      if (isBusy() || actors.length < 2) return;
      const a = actors[0], b = actors[1];
      const mx = Number((((Number(a.x) || 0) + (Number(b.x) || 0)) / 2).toFixed(2));
      const mz = Number((((Number(a.z) || 0) + (Number(b.z) || 0)) / 2).toFixed(2));
      peerInteractions.push({ id: 'peer' + nextPeerId++, kind: 'handshake', actors: [a.id, b.id], x: mx, z: mz });
      renderPeerInteractions();
      controls();
      drawDraft();
      root.Workspace?.markTabDirty?.();
    };
  }
  if (attachObjectSelect) {
    attachObjectSelect.onchange = () => {
      const a = activeActor();
      if (a?.attach) a.attach.objectId = attachObjectSelect.value;
      syncAttachWindowVisibility();
      controls();
    };
  }
  if (attachModeSelect) {
    attachModeSelect.onchange = () => {
      const a = activeActor();
      if (a?.attach) a.attach.mode = attachModeSelect.value;
      syncAttachWindowVisibility();
    };
  }
  if (attachStartInput) attachStartInput.oninput = () => { const a = activeActor(); if (a?.attach) a.attach.startS = attachStartInput.value; };
  if (attachEndInput) attachEndInput.oninput = () => { const a = activeActor(); if (a?.attach) a.attach.endS = attachEndInput.value; };

  if (form) {
    form.onsubmit = event => {
      event.preventDefault();
      return submitSceneOrMulti();
    };
  }

  root.SceneController = {
    view,
    sceneActorFailure,
    sceneActorAwaitingSubmission,
    applyTaskUpdates(tasks) {
      for (const task of tasks) statusRequests.delete(task.id);
      const active = tasks.find(task => task.id === taskId);
      if (active && isActive(tabKey)) return show(active, S.session);
    },
    open: openScenePanel,
    addPersonalObject(asset, anchor, hand) {
      const used = objects.reduce((count, item) => count + partCountFor(item), 0);
      if (isBusy() || manual?.checked || used >= ScenePresets.MAX_PARTS) {
        throw Error('无法添加物体，请退出手动模式并留出物体位置。');
      }
      const {size_m, heading_deg} = asset.data;
      addObject('asset', {id:asset.id, name:asset.name, size_m, heading_deg, anchor, hand});
    },
    close: () => dialog?.close?.(),
    drawDraft,
    onTextPreviewStarted() {
      viewRevision++;
      pendingTextPreview = {};
    },
    cancelTextPreview() {
      pendingTextPreview = null;
    },
    onTextPreviewShown() {
      viewingTextResult = true;
      viewingResult = false;
      updateMismatchBanner();
    },
    async onTextPreviewSettled() {
      const resumeScene = pendingTextPreview?.resumeScene;
      pendingTextPreview = null;
      if (!viewingTextResult) await resumeScene?.();
    },
    hasUnsubmittedChanges() {
      return Boolean(submittedSnapshot && submittedSnapshot.fingerprint !== currentFingerprint());
    },
    isDirty(snapshot = { pair: pair?.value, objects, actors, peerInteractions,
      manual: manual?.checked, text: text?.value, spec: spec?.value }) {
      return Boolean(snapshot.pair
        || snapshot.objects?.length
        || snapshot.actors?.length > 1
        || snapshot.peerInteractions?.length
        || snapshot.actors?.some(a => a.x !== 0 || a.z !== 0 || a.heading_deg !== 0)
        || (snapshot.manual && (snapshot.text?.trim() || snapshot.spec?.trim())));
    },
    clearDraft,
    canSubmit: canSubmitScene,
    submitLabel,
    isMultiOrStepMode,
    shouldHandleComposerSubmit,
    isViewingSceneResult: () => viewingResult,
    submitFromComposer: submitSceneOrMulti,
    getAttachmentRequest,
    runObjectResponse,
    addActorTrack,
    renderActorTracks,
    onRobotsLoaded: () => renderActorTracks(),
    onComposerTextInput: value => {
      if (syncingAutoPrompt) return;
      const a = activeActor();
      if (a) {
        a.text = value;
        a.textEdited = true;
      }
      updateMismatchBanner();
    },
    onComposerDurationChange: value => {
      if (Number.isFinite(value) && value >= 1) setUnifiedDuration(value);
      updateMismatchBanner();
      controls();
    },
    syncModalityFromComposer: mod => {
      const a = activeActor();
      if (!a) return;
      if (mod === 'video') a.modality = 'video';
      else if (a.modality === 'video') a.modality = actorOrderedItems(a).length ? 'scene' : 'text';
      renderActorTracks();
      controls();
    },
    getState() {
      return {
        objects: JSON.parse(JSON.stringify(objects)),
        actors: JSON.parse(JSON.stringify(actors)),
        activeActorId,
        nextActorNum,
        peerInteractions: JSON.parse(JSON.stringify(peerInteractions)),
        nextPeerId,
        targetId: target?.id || null,
        pair: pair?.value || '',
        nextId,
        duration: Number(duration?.value) || 4,
        manual: Boolean(manual?.checked),
        text: text?.value || '',
        spec: spec?.value || '',
        taskId,
        selectedSceneCandidate,
        draftFingerprint: currentFingerprint(),
        submittedSnapshot: submittedSnapshot ? JSON.parse(JSON.stringify(submittedSnapshot)) : null,
        camera: view.cameraState?.() || null
      };
    },
    async setState(snap = {}, key = 'default', active = false) {
      view.stopDrag?.();
      tabKey = key || 'default';
      objects = JSON.parse(JSON.stringify(snap.objects || []));
      nextId = snap.nextId || (objects.reduce((m, o) => Math.max(m, o.number || 0), 0) + 1);
      if (Array.isArray(snap.actors) && snap.actors.length) {
        actors = JSON.parse(JSON.stringify(snap.actors));
        activeActorId = actors.some(a => a.id === snap.activeActorId) ? snap.activeActorId : actors[0].id;
        nextActorNum = snap.nextActorNum || (actors.reduce((m, a) => Math.max(m, a.number || 1), 1) + 1);
      } else {
        resetActors();
        const legacyOrdered = objects.filter(o => o.order > 0).sort((a, b) => a.order - b.order);
        if (legacyOrdered.length) {
          actors[0].steps = legacyOrdered.map(o => o.id);
          actors[0].modality = 'scene';
        }
      }
      peerInteractions = JSON.parse(JSON.stringify(snap.peerInteractions || []));
      nextPeerId = snap.nextPeerId || (peerInteractions.length + 1);
      if (duration) setUnifiedDuration(snap.duration || 4);
      if (pair) pair.value = snap.pair || '';
      if (manual) manual.checked = Boolean(snap.manual);
      if (text) text.value = snap.text || '';
      if (spec) spec.value = snap.spec || '';
      taskId = snap.taskId || taskRecord().get() || null;
      selectedSceneCandidate = snap.selectedSceneCandidate || null;
      if (taskId) taskRecord().set(taskId);
      submittedSnapshot = snap.submittedSnapshot ? JSON.parse(JSON.stringify(snap.submittedSnapshot)) : null;
      target = objects.find(o => o.id === snap.targetId) || null;
      lastTask = null; viewingResult = false;
      for (const actor of actors) {
        if (typeof actor.textEdited === 'boolean') continue;
        const restored = (actor === activeActor() ? $('gen-text')?.value : '') || actor.text || '';
        let automatic = '';
        try {
          if (manual?.checked) automatic = text?.value || '';
          else if (pair?.value) automatic = pair.value === 'handshake' ? '双人握手' : '双人合力搬箱';
          else automatic = buildSingleActorScene(actor, currentDurationSeconds())?.text || '';
        } catch (_) { /* Preserve a legacy draft when its automatic description cannot be computed. */ }
        actor.textEdited = Boolean(restored && restored !== automatic);
      }
      if ($('scene-manual-fields')) $('scene-manual-fields').hidden = !manual?.checked;
      renderObjects();
      if (active) {
        const session = S.session, currentTab = tabKey;
        await openScenePanel();
        if (session !== S.session || currentTab !== tabKey) return;
        view.applyCameraState?.(snap.camera);
      } else {
        dialog?.close?.();
      }
      controls();
    },
    setPreviewClip,
    restoreTask: playback => pollTask(taskId, {
      playback: selectedSceneCandidate && playback ? { ...selectedSceneCandidate, frame: playback.frame, playing: playback.playing } : null
    }),
    pollTask
  };

  let session = S.session;
  new MutationObserver(() => {
    if (S.session === session) return;
    session = S.session; enabled = false; capChecked = false; runningTabs.clear(); statusRequests.clear(); taskId = null;
    selectedSceneCandidate = null;
    if (dialog?.open) dialog.close();
    clearDraft();
  }).observe(document.body, { attributes: true, attributeFilter: ['data-screen'] });
})();
