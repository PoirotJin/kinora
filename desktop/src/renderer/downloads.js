'use strict';
(() => {
  let rows=[], session=S.session, savingSource=null;
  let sourceVersion = 0, resultsLoadFailed = false;
  let downloadFeedback = null, triggerButton = null;
  const dialog=$('downloads-dialog'), status=$('downloads-status');
  const selected = new Set();
  const pendingDownloads = new Map();
  const pendingMessage = '请选择保存位置，随后开始下载…';
  const selectionLimit = 20;
  const selectionLimitMessage = '每次最多下载 ' + selectionLimit + ' 个动作';
  const downloadKey = (ids, format) => JSON.stringify([S.session, format, [...ids].sort()]);
  const selectedDownloadKey = () => selected.size ? downloadKey(selected, $('downloads-format').value) : null;
  const visible=()=>rows.filter(r=>(r.label+' '+r.motion_id).toLowerCase().includes($('downloads-search').value.toLowerCase()));

  function downloadBadge(row) {
    const known = MotionLoader.getCachedMeta(row.motion_id);
    const status = row.status ?? known?.status;
    if (status === 'quarantined') return ['未通过 · 隔离候选', 'bad'];
    if (row.task_id || known?.task_id) {
      return (row.accepted ?? known?.accepted) === true ? ['已接受', 'ok'] : ['候选 · 待确认', 'warn'];
    }
    return [MotionLoader.isManagedAsset(row.motion_id) ? '个人动作' : '人体源动作', ''];
  }

  function render(){
    const locked = Boolean(savingSource?.());
    const list = $('downloads-list');
    const matching = visible();
    $('downloads-all').disabled = locked || !matching.length;
    list.replaceChildren(...matching.map(r=>{
      const check=h('input',{type:'checkbox','aria-label':'选择 '+r.motion_id});check.checked=selected.has(r.motion_id);check.disabled=locked;
      check.onchange = () => {
        if (check.checked && selected.size >= selectionLimit) {
          check.checked = false;
          status.textContent = selectionLimitMessage;
          return;
        }
        check.checked ? selected.add(r.motion_id) : selected.delete(r.motion_id);
        check.closest('.download-row')?.classList.toggle('checked', check.checked);
        update();
      };
      const preview = h('button', {class:'text-btn', type:'button', text:'预览', onclick: () => {
        dialog.close();
        const known = MotionLoader.getCachedMeta(r.motion_id);
        const resultStatus = r.status ?? known?.status;
        if (r.task_id || known?.task_id || resultStatus === 'quarantined') {
          window.previewExportedMotion(r.motion_id, resultStatus);
        } else {
          selectMotion(r.motion_id, {reveal:true});
        }
      }});
      const state = downloadBadge(r);
      return h('div',{class:'download-row'+(check.checked?' checked':'')},h('label',{class:'dl-check'},check),h('div',{class:'dl-body'},h('span',{class:'dl-label',copy:'文本提示词',text:r.label||r.motion_id}),h('small',{},h('span',{class:'mono',copy:'动作序号',text:r.motion_id}),h('span',{class:'pill '+state[1],text:state[0]}))),preview);
    }));
    if (resultsLoadFailed) {
      list.append(h('button',{type:'button',class:'btn subtle',text:'重新加载',onclick:() => {
        $('downloads-results').focus();
        results();
      }}));
    } else if (!matching.length && (rows.length || !status.textContent)) {
      list.append(h('div',{class:'empty',text:rows.length?'没有匹配的动作':'暂无动作'}));
    }
    update();
  }
  function setScope(source) {
    $('downloads-library').setAttribute('aria-pressed', String(source === 'library'));
    $('downloads-results').setAttribute('aria-pressed', String(source === 'results'));
  }
  function update() {
    const locked = Boolean(savingSource?.());
    const key = selectedDownloadKey();
    const pending = pendingDownloads.has(key);
    if (downloadFeedback && downloadFeedback.key !== key) {
      if (status.textContent === downloadFeedback.message) status.textContent = '';
      downloadFeedback = null;
    }
    $('downloads-count').textContent = '已选 ' + selected.size + ' / ' + selectionLimit;
    for (const id of ['downloads-clear', 'downloads-format']) $(id).disabled = locked;
    $('downloads-save').disabled = locked || pending || !selected.size;
    if (pending) status.textContent = pendingMessage;
    else if (status.textContent === pendingMessage ||
      (status.textContent === selectionLimitMessage && selected.size < selectionLimit)) status.textContent = '';
    const failures = pending ? [] : downloadFeedback?.failures || [];
    const failedDetails = $('downloads-failures');
    failedDetails.hidden = !failures.length;
    if (!failures.length) failedDetails.open = false;
    $('downloads-failed-list').replaceChildren(...failures.map(({motion_id,error}) => {
      const label = rows.find(row => row.motion_id === motion_id)?.label;
      return h('li',{},h('strong',{text:label || motion_id}),
        label && label !== motion_id ? h('span',{class:'mono',text:' '+motion_id}) : null,
        h('span',{text:' · '+(error || '无法下载，请稍后重试。')}));
    }));
  }
  function begin() {
    ++sourceVersion;
    resultsLoadFailed = false;
    downloadFeedback = null;
    session = S.session;
    setScope(null);
    selected.clear();
    $('downloads-search').value = '';
    status.textContent = '';
    if (!dialog.open) dialog.showModal();
  }
  function library() {
    ++sourceVersion;
    resultsLoadFailed = false;
    setScope('library');
    status.textContent = '';
    rows = S.lib.items.map(x => ({motion_id:x.motion_id,label:x.text}));
    $('downloads-scope').textContent = '动作库已加载的 '+rows.length+' 个动作；可先在动作库搜索并加载更多。';
    render();
  }
  function currentSource() {
    const version = sourceVersion;
    const token = session;
    return () => version === sourceVersion && token === S.session && dialog.open;
  }
  async function results() {
    ++sourceVersion;
    resultsLoadFailed = false;
    const current = currentSource();
    setScope('results');
    $('downloads-scope').textContent = '拼接与修正结果';
    status.textContent = '正在读取拼接与修正结果…';
    rows = [];
    render();
    try {
      const data = await cloud('/api/harness/results');
      if (!current()) return;
      rows = data.items || [];
      for (const row of rows) MotionLoader.rememberMeta(row.motion_id, row);
      $('downloads-scope').textContent = data.scope || '拼接与修正结果';
      status.textContent = rows.length ? '' : '暂无可下载结果；未生成候选的失败任务仍保留在编辑任务中。';
      render();
    } catch (error) {
      if (current() && error.name !== 'AbortError') {
        resultsLoadFailed = true;
        status.textContent = error.message || '读取导出结果失败，请重试。';
        render();
      }
    }
  }
  window.downloadMotion = (id, optionsOrButton) => {
    const isButton = Boolean(optionsOrButton && (typeof optionsOrButton.tagName === 'string' || typeof optionsOrButton.disabled === 'boolean'));
    const options = isButton ? { button: optionsOrButton } : (optionsOrButton && typeof optionsOrButton === 'object' ? optionsOrButton : {});
    const button = options.button && typeof options.button === 'object' ? options.button : null;
    if (!id) {
      if (typeof toast === 'function') toast('请先选择动作', true);
      return;
    }
    const cachedMeta = typeof MotionLoader !== 'undefined' ? MotionLoader?.getCachedMeta?.(id) : null;
    const resultStatus = options.resultStatus ?? cachedMeta?.status;
    triggerButton = button;
    if (button && button.disabled) return;
    begin();
    rows = [{motion_id: id, label: cachedMeta?.label || '所选动作', status: resultStatus}];
    selected.add(id);
    $('downloads-scope').textContent = '选择格式后保存完整动作';
    render();
  };
  dialog?.addEventListener?.('close', () => {
    const button = triggerButton;
    triggerButton = null;
    if (button && button.disabled !== true && button.isConnected !== false) button.focus?.();
  });
  window.openAssetCollectionDownloads = list => {
    begin();
    rows = list.map(({motion_id, text, task_id, status, accepted}) => ({
      motion_id, label: text || motion_id, task_id, status, accepted
    }));
    for (const {motion_id, task_id, status, accepted} of rows) {
      if (task_id) MotionLoader.rememberMeta(motion_id, {task_id, status, accepted});
    }
    $('downloads-scope').textContent = '当前集合 · ' + rows.length + ' 个动作';
    render();
  };
  $('downloads-open').onclick=()=>{begin();library();};
  $('splice-results-open').onclick=()=>{begin();results();};
  $('downloads-close').onclick=()=>dialog.close();
  $('downloads-library').onclick=()=>{selected.clear();library();};$('downloads-results').onclick=()=>{selected.clear();results();};
  $('downloads-search').oninput=render;
  $('downloads-format').onchange=update;
  $('downloads-all').onclick = () => {
    for (const {motion_id} of visible()) {
      if (selected.has(motion_id)) continue;
      if (selected.size >= selectionLimit) {
        status.textContent = selectionLimitMessage;
        break;
      }
      selected.add(motion_id);
    }
    render();
  };
  $('downloads-clear').onclick=()=>{selected.clear();render();};
  $('downloads-save').onclick = async () => {
    if (savingSource?.() || !selected.size) return;
    const ids = [...selected];
    const format = $('downloads-format').value;
    const key = downloadKey(ids, format);
    if (pendingDownloads.has(key)) return;
    const current = currentSource();
    const batch = ids.length > 1;
    pendingDownloads.set(key, current);
    savingSource = current;
    render();
    let message, failures = [];
    try {
      const result = await downloadFile({
        path: batch ? '/api/harness/export' : '/api/harness/motions/'+encodeURIComponent(ids[0])+'/download',
        query: batch ? {ids:ids.join(','),format} : {format},
        name: batch ? 'kinora-'+format+'-'+ids.length+'-motions.zip' : ids[0]+'.'+format
      });
      failures = result?.batch?.failures || [];
      if (result?.error) throw Error(result.error);
      if (!result?.cancelled && !result?.saved) throw Error('下载失败，请重试');
      const summary = result.batch
        ? `。成功 ${result.batch.saved} 个，失败 ${result.batch.failed} 个` : '';
      message = result.cancelled ? '已取消，未保存文件。' :
        '已保存 '+result.saved+summary+(batch ? '；每项结果见 ZIP 内 export-report.json。' : '');
    } catch (error) {
      message = error?.message || '下载失败，请重试';
    } finally {
      if (pendingDownloads.get(key) === current) {
        pendingDownloads.delete(key);
        if (savingSource === current) savingSource = null;
        if (current() || (dialog.open && selectedDownloadKey() === key)) {
          downloadFeedback = {key, message, failures};
          status.textContent = message;
          render();
        }
      }
    }
  };
  new MutationObserver(() => {
    if (session === S.session) return;
    session = S.session;
    rows = [];
    selected.clear();
    pendingDownloads.clear();
    savingSource = null;
    resultsLoadFailed = false;
    triggerButton = null;
    dialog.close();
    render();
  }).observe(document.body,{attributes:true,attributeFilter:['data-screen']});
})();
