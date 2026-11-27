'use strict';
(() => {
  const dialog = $('custom-robots-dialog'), status = $('custom-robot-status');
  const listStatus = $('custom-robot-list-status'), previewStatus = $('custom-robot-preview-status');
  const listRetry = $('custom-robot-list-retry');
  let session = S.session, busy = false, request = 0, payload = null, shown = null;
  let dialogVersion = 0, listRequest = 0;
  let importMessage = '';
  const importRequestIds = new Map();
  const dialogCurrent = (token, version) => token === S.session && version === dialogVersion && dialog.open;
  const mark = id => { shown = id; $('custom-robot-empty').hidden = !!id; for (const b of $('custom-robot-list').querySelectorAll('.cr-item')) b.setAttribute('aria-current', String(b.dataset.robot === id)); };
  const view = new RobotView.View($('custom-robot-canvas'), {
    model: async () => payload.manifest,
    meshData: async () => Uint8Array.from(atob(payload.binary), c => c.charCodeAt(0)).buffer
  });
  view.suspend();
  function controls() {
    for (const id of ['custom-robot-import', 'custom-robot-file', 'custom-robot-name', 'custom-robot-entry']) $(id).disabled = busy;
  }
  function setImportStatus(message) {
    importMessage = message;
    if (dialog.open) status.textContent = message;
  }
  function setPreviewStatus(message) {
    previewStatus.hidden = !message;
    previewStatus.textContent = message;
  }
  function clearPreview() {
    ++request;
    payload = null;
    view.clear();
    mark(null);
    $('custom-robot-detail').replaceChildren();
    setPreviewStatus('');
  }
  function clearEntry() {
    $('custom-robot-entry').replaceChildren();
    $('custom-robot-entry-field').hidden = true;
  }
  function clearImportForm() {
    $('custom-robot-file').value = '';
    $('custom-robot-name').value = '';
    clearEntry();
    importRequestIds.clear();
  }
  function clearListStatus() {
    listStatus.textContent = '';
    listRetry.hidden = true;
    listRetry.removeAttribute('aria-disabled');
  }
  function clear() {
    busy = false;
    controls();
    clearPreview();
    $('custom-robot-list').replaceChildren();
    clearImportForm();
    setImportStatus('');
    clearListStatus();
  }
  function robotButton(robot) {
    return h('button', {class:'cr-item', type:'button', role:'listitem', dataset:{robot:robot.id}, 'aria-current':String(shown === robot.id), onclick:() => preview(robot)},
      h('span', {class:'cr-name', text:robot.label}), h('span', {class:'mono muted', text:robot.dof + ' DoF'}));
  }
  async function refresh() {
    const token = S.session, version = dialogVersion, seq = ++listRequest;
    const current = () => dialogCurrent(token, version) && seq === listRequest;
    listRetry.setAttribute('aria-disabled', 'true');
    listStatus.textContent = '正在读取模型列表…';
    try {
      const data = await cloud('/api/harness/robots');
      if (!current()) return;
      const list = $('custom-robot-list'), focused = document.activeElement;
      const focusedRobot = list.contains(focused) ? focused.dataset.robot : null;
      const rows = data.items.map(robotButton);
      list.replaceChildren(...rows);
      if (!rows.length) list.append(h('p', {class:'empty', text:'尚未导入自定义模型'}));
      clearListStatus();
      if (focused === listRetry || focusedRobot) {
        const next = rows.find(row => row.dataset.robot === (focusedRobot || shown)) || rows[0] || $('custom-robot-file');
        next.focus();
      }
    } catch (e) {
      if (current()) {
        listStatus.textContent = '无法刷新模型列表：' + e.message;
        listRetry.hidden = false;
      }
    } finally {
      if (current()) listRetry.removeAttribute('aria-disabled');
    }
  }
  listRetry.onclick = () => {
    if (dialog.open && !listRetry.hidden && listRetry.getAttribute('aria-disabled') !== 'true') void refresh();
  };
  async function preview(robot) {
    clearPreview();
    const token = S.session, version = dialogVersion, seq = request;
    const current = () => dialogCurrent(token, version) && seq === request;
    setPreviewStatus('正在读取模型…');
    mark(robot.id);
    try {
      const data = await cloud('/api/harness/robots/' + robot.id + '/preview');
      if (!current()) return;
      payload = data;
      // Per-record identity avoids reusing another imported model with the same pose hash.
      data.clip.metadata.robot = robot.id;
      await view.load(data.clip);
      if (!current()) return;
      view.setPlaying(false); view.render(0);
      const bounds = new THREE.Box3();
      for (const mesh of view.meshes) { mesh.updateMatrixWorld(true); bounds.expandByObject(mesh); }
      const center = bounds.getCenter(new THREE.Vector3()), size = bounds.getSize(new THREE.Vector3());
      data.clip.roots = [[center.x, center.y, 0]];
      data.clip.centerZ = center.z;
      data.clip.cameraBoundsRadius = size.length() / 2;
      view.resetCamera();
      view.render(0);
      $('custom-robot-detail').replaceChildren(h('div', {class:'insp-head'}, h('span', {class:'insp-id', text:robot.label}), h('span', {class:'pill plain', text:robot.format})),
        h('div', {class:'stats mono'}, h('span', {text:robot.bodies + ' 连杆'}), h('span', {text:robot.dof + ' DoF'}), h('span', {text:robot.joints.length + ' 关节'})),
        robot.note ? h('p', {class:'muted', text:robot.note}) : null,
        h('details', {class:'advanced'}, h('summary', {text:'关节与限位（转动：弧度，平移：米）'}), h('ul', {class:'joint-list mono'}, robot.joints.map(j => h('li', {}, h('span', {text:j.name}), h('span', {class:'muted', text:j.type + (j.limited ? ' · [' + j.range.map(x => x.toFixed(3)).join(', ') + ']' : ' · 未设置限位')}))))));
      setPreviewStatus('可旋转和缩放预览。尚未配置重定向映射。');
    } catch (e) {
      if (current()) {
        clearPreview();
        setPreviewStatus('预览失败：' + e.message);
      }
    }
  }
  $('custom-robots-open').onclick = async () => {
    if (dialog.open) return;
    if (session !== S.session) { session = S.session; clear(); }
    clearPreview();
    clearListStatus();
    ++dialogVersion;
    dialog.showModal();
    status.textContent = importMessage;
    view.resume();
    await refresh();
  };
  $('custom-robots-close').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {
    if (dialog.open) return;
    ++dialogVersion;
    view.suspend();
    clearPreview();
    status.textContent = '';
    if (!busy && !$('custom-robot-file').files.length) importMessage = '';
    clearListStatus();
  });
  $('custom-robot-file').onchange = () => {
    clearEntry();
    importRequestIds.clear();
    const file = $('custom-robot-file').files[0];
    if (file) $('custom-robot-name').value = file.name.replace(/\.(zip|urdf|xml)$/i, '').slice(0, 100);
    setImportStatus('');
  };
  $('custom-robot-import').onclick = async () => {
    if (busy) return;
    const file = $('custom-robot-file').files[0], label = $('custom-robot-name').value.trim();
    if (!file) { setImportStatus('请先选择模型文件'); return; }
    if (!label) { setImportStatus('请填写机器人名称'); return; }
    if (file.size > 32*1024*1024) { setImportStatus('模型文件不能超过 32 MB'); return; }
    busy = true; controls();
    const token = S.session, version = dialogVersion, priorPreview = request;
    const entry = $('custom-robot-entry-field').hidden ? '' : $('custom-robot-entry').value;
    setImportStatus('正在上传并检查模型与资源文件…');
    try {
      const bytes = new Uint8Array(await file.arrayBuffer()); let binary = '';
      for (let i=0; i<bytes.length; i+=32768) binary += String.fromCharCode(...bytes.subarray(i, i+32768));
      if (token !== S.session) return;
      const body = {name:file.name, label, data:btoa(binary)};
      if (entry) body.entry = entry;
      if (S.capabilities?.custom_robot_import_request_id === true) {
        const config = JSON.stringify([label, entry]);
        if (!importRequestIds.has(config)) importRequestIds.set(config, randomId());
        body.request_id = importRequestIds.get(config);
      }
      const result = await cloud('/api/harness/robots', {method:'POST', body});
      if (token !== S.session) return;
      if (result.choose_entry) {
        $('custom-robot-entry').replaceChildren(...result.choose_entry.map(name => h('option', {value:name, text:name})));
        $('custom-robot-entry-field').hidden = false;
        setImportStatus('发现多个模型，请选择入口后再次点击“导入模型”。');
        return;
      }
      clearImportForm();
      setImportStatus('模型已导入。');
      window.refreshRetarget?.();
      if (!dialog.open) return;
      const list = $('custom-robot-list'), robot = result.robot;
      const existing = [...list.querySelectorAll('.cr-item')].find(button => button.dataset.robot === robot.id);
      if (existing) existing.replaceWith(robotButton(robot));
      else {
        list.querySelector('.empty')?.remove();
        list.append(robotButton(robot));
      }
      void refresh();
      if (dialogCurrent(token, version) && request === priorPreview) void preview(robot);
    } catch (e) {
      if (token === S.session) setImportStatus(e.message || '导入失败，请重试');
    } finally {
      if (token === S.session) { busy = false; controls(); }
    }
  };
  new MutationObserver(() => {
    if (session !== S.session) { session = S.session; dialog.close(); clear(); }
  }).observe(document.body, {attributes:true, attributeFilter:['data-screen']});
})();
