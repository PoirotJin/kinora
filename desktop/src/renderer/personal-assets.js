'use strict';
(() => {
  const host = $('personal-assets');
  if (!host) return;
  const base = '/api/harness/personal-assets/';
  const collections = '/api/library/collections';
  const tabs = [['motions','动作集合'], ['rigs','骨架与姿态'], ['objects','交互物体'], ['snapshots','资产快照']];
  const request = (path, query) => cloud(base + path, {query});
  const post = (path, body) => cloud(base + path, {method:'POST', body});
  const collection = (query, body) => cloud(collections, body ? {method:'POST', body} : {query});
  const button = (text, onclick, primary = false) => h('button', {type:'button', class:primary ? 'btn primary' : 'btn subtle', onclick}, text);
  const para = (text, className = '') => h('p', {class:className}, text);
  const assetId = item => item.motion_id || item.id;
  const bodyNames = ['pelvis','left_hip','right_hip','spine1','left_knee','right_knee','spine2','left_ankle','right_ankle','spine3','left_foot','right_foot','neck','left_collar','right_collar','head','left_shoulder','right_shoulder','left_elbow','right_elbow','left_wrist','right_wrist'];
  let group = 'motions', collectionId = '', detailId = '', previousSceneId = '', scope = '', after = null;
  let items = [], collectionItems = [], busy = false, active = false, session = 0, nav = 0;
  let collectionRead = null;
  let pageLoad = null;
  let hasRequestedMore = false;
  let retryFirstPage = false;
  const pageCursors = new Set();
  const snapshotDownloads = new Map();
  const snapshotDownloadHistory = new Map();
  let detailSerial = 0, poseRequest = 0, addContext = 0, view, dialog, jobPoll;
  const toolbar = h('div', {class:'pa-toolbar'});
  const controls = h('div', {class:'pa-controls'});
  const folders = h('div', {class:'pa-folders', 'aria-label':'集合'});
  const search = h('input',{type:'search',placeholder:'搜索当前列表','aria-label':'搜索当前列表'});
  const list = h('div',{class:'pa-list'});
  const details = h('div',{class:'pa-details'});
  const status = h('p',{class:'pa-status',role:'status','aria-live':'polite'});
  host.append(h('div',{class:'pa-header'},h('h1',{},'我的资产'),toolbar),controls,status,
              h('div',{class:'pa-layout'},h('div',{class:'pa-catalog'},folders,search,list),details));

  function note(message = '') { status.textContent = message; }

  async function work(fn) {
    if (busy || controls.contains(collectionRead)) return;
    const epoch = session;
    busy = true;
    try { await fn(epoch); }
    catch (err) { if (epoch === session) note(err.message || String(err)); }
    finally { if (epoch === session) busy = false; }
  }

  async function readCollection(trigger, run) {
    if (busy || !active || !collectionId || !controls.contains(trigger) || controls.contains(collectionRead)) return;
    const cid = collectionId, epoch = session;
    const current = () => active && epoch === session && collectionRead === trigger
      && cid === collectionId && controls.contains(trigger);
    collectionRead = trigger;
    note();
    try { await run(cid, current); }
    catch (error) { if (current() && error.name !== 'AbortError') note(error.message || String(error)); }
    finally { if (collectionRead === trigger) collectionRead = null; }
  }

  function field(text, input) { return h('label',{class:'field'},h('span',{},text),input); }
  function input(type = 'text', value = '', attrs = {}) { return h('input',{type,value,...attrs}); }
  function choices(values, selected) {
    const el = h('select',{},...values.map(([value,text]) => h('option',{value},text)));
    if (selected != null) el.value = selected;
    return el;
  }
  async function readAssetChoices(route, filters, current) {
    const assets = new Map(), cursors = new Set();
    let after = null;
    do {
      const query = after === null ? filters : {...filters, after};
      const page = await request(route, query);
      if (!current()) return [];
      for (const asset of page.items) assets.set(asset.id, asset);
      after = page.next_after ?? null;
      if (after !== null) {
        const cursor = String(after);
        if (cursors.has(cursor)) throw Error('服务端返回了重复的分页位置，无法读取完整资产列表。请重试。');
        cursors.add(cursor);
      }
    } while (after !== null);
    return [...assets.values()];
  }

  function createForm(title, content, save) {
    ++addContext;
    ++poseRequest;
    dialog?.close();
    dialog?.remove();
    const element = h('dialog',{class:'sheet pa-dialog'});
    const error = h('p',{class:'pa-status',role:'alert'});
    const submit = h('button',{type:'submit',class:'btn primary'},'保存');
    const close = h('button',{type:'button',class:'icon-btn','aria-label':'关闭',onclick:() => element.close()},'×');
    const form = h('form',{},
      h('header',{class:'sheet-head'},h('h2',{},title),close),
      h('div',{class:'sheet-body'},content,error,
        h('div',{class:'pa-controls pa-dialog-actions'},button('取消',() => element.close()),submit)));
    form.onsubmit = async event => {
      event.preventDefault();
      if (submit.disabled) return;
      const epoch = session;
      const isCurrentForm = () => epoch === session && dialog === element && element.open;
      submit.disabled = true;
      error.textContent = '';
      try {
        await save(isCurrentForm);
        if (isCurrentForm()) element.close();
      } catch (err) {
        if (isCurrentForm()) error.textContent = err.message || String(err);
      } finally { submit.disabled = false; }
    };
    element.append(form);
    document.body.append(element);
    element.addEventListener('close',() => {
      element.remove();
      if (dialog === element) dialog = null;
    });
    dialog = element;
    element.showModal();
    return element;
  }

  function readFile(file) {
    if (!file || !file.size || file.size > 8 * 1024 * 1024) throw Error('请选择 8 MiB 以内的文件。');
    return new Promise((resolve,reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(Error('文件读取失败。'));
      reader.readAsDataURL(file);
    });
  }

  function pageKey() {
    if (group !== 'motions') return scope || group;
    return collectionId ? 'collection' : scope || 'motions';
  }

  async function reloadCollectionPages(id, lastCursor, includePendingPage, current) {
    const members = new Map(), cursors = new Set();
    let cursor = null;
    while (current()) {
      const page = await collection({id, ...(cursor === null ? {} : {after: cursor})});
      if (!current()) return;
      for (const item of page.items) members.set(assetId(item), item);
      const next = page.next_after ?? null;
      const reachedPreviousRange = next !== null && lastCursor !== null && String(next) >= String(lastCursor);
      if (next === null || cursors.has(String(next)) || (reachedPreviousRange && !includePendingPage)) {
        return {items: [...members.values()], next_after: next, cursors};
      }
      if (reachedPreviousRange) includePendingPage = false;
      cursors.add(String(next));
      cursor = next;
    }
  }

  async function load(append = false, {retainCollectionPages = false} = {}) {
    if (append && (after == null || pageLoad?.ticket === nav)) return;
    const key = pageKey();
    const preservePages = !append && retainCollectionPages && key === 'collection' && hasRequestedMore;
    const includePendingPage = pageLoad?.append === true;
    if (append) hasRequestedMore = true;
    const ticket = ++nav;
    const snapshotUpdates = key === 'snapshots' && !append ? new Map() : null;
    pageLoad = { ticket, append, snapshotUpdates };
    const epoch = session;
    const cursor = append ? after : null;
    const cid = collectionId;
    const query = cursor != null ? {after:cursor} : undefined;
    const current = () => ticket === nav && epoch === session;
    const retrying = retryFirstPage;
    retryFirstPage = false;
    if (append || retrying) note();
    if (after != null || retrying) renderList();
    try {
      const [result, sets] = await Promise.all([
        preservePages ? reloadCollectionPages(cid, after, includePendingPage, current)
          : key === 'collection' ? collection({id:cid,...query})
          : key === 'generated' ? cloud('/api/personal/motions',{query}) : request(key,query),
        group === 'motions' ? collection() : null
      ]);
      if (!current()) return;
      if (sets) collectionItems = sets.collections;
      if (append) {
        const seen = new Set(items.map(assetId));
        for (const item of result.items) {
          const id = assetId(item);
          if (seen.has(id)) continue;
          items.push(item);
          seen.add(id);
        }
      } else {
        items = (result.jobs || []).concat(result.items);
        if (snapshotUpdates?.size) {
          const received = new Set(items.map(assetId));
          const missing = [...snapshotUpdates.values()].filter(row => !received.has(row.id));
          items = missing.concat(items.map(row => row.status === 'queued'
            ? snapshotUpdates.get(assetId(row)) ?? row : row));
        }
        hasRequestedMore = preservePages;
        pageCursors.clear();
        if (preservePages) for (const cursor of result.cursors) pageCursors.add(cursor);
      }
      const next = result.next_after ?? null;
      const stalled = next !== null && pageCursors.has(String(next));
      after = stalled ? null : next;
      if (stalled) {
        retryFirstPage = true;
        note('服务端返回了重复的分页位置，请重新加载。');
      }
      if (after !== null) pageCursors.add(String(after));
      if (!append && key === 'collection' && detailId) {
        if (!items.some(item => assetId(item) === detailId)) clearMotionDetail();
        else if (active && view && details.contains(view.canvas)) view.resume();
      }
    } catch (err) {
      if (current() && err.name !== 'AbortError') {
        retryFirstPage = !append;
        note(err.message || String(err));
      }
    } finally {
      if (current()) {
        pageLoad = null;
        renderList();
      }
    }
  }

  function navigate(next, sub = '', id = '') {
    ++addContext;
    group = next; scope = sub; collectionId = id; detailId = ''; items = []; after = null;
    hasRequestedMore = false;
    retryFirstPage = false;
    pageCursors.clear();
    ++detailSerial;
    clearTimeout(jobPoll);
    view?.suspend();
    search.value = '';
    details.replaceChildren(para('选择一项查看详情。','muted'));
    note();
    renderChrome();
    renderList();
    load();
  }

  function renderChrome() {
    toolbar.replaceChildren(...tabs.map(([key,text]) => h('button',{type:'button',class:'nav-btn',
      'aria-pressed':String(group === key),onclick:() => navigate(key)},text)));
    const actions = [];
    if (group === 'motions') {
      actions.push(button('导入 BVH',() => rigForm(null,true),true),button('新建集合',createCollection));
      if (collectionId) actions.push(button('加入当前动作',event => addMotion(S.lib.selected,collectionId,event.currentTarget)),
        button('固定快照',freeze,true),button('批量导出',bulkExport));
    } else if (group === 'rigs') {
      actions.push(button('骨架预设',() => navigate('rigs')),
                   button('单帧姿态',() => navigate('rigs','poses')),
                   button(scope === 'poses' ? '保存当前动作帧' : '新建骨架预设',
                     () => scope === 'poses' ? saveLibraryPose() : rigForm(),true));
    } else if (group === 'objects') actions.push(button('上传物体模型',() => objectForm(),true));
    controls.replaceChildren(...actions);
    controls.hidden = !actions.length;
  }

  function renderList() {
    const focused = document.activeElement;
    const restoreListFocus = active && list.contains(focused);
    const restoreFolderFocus = active && folders.contains(focused);
    const scrollTop = host.scrollTop;
    const folderRows = [];
    if (group === 'motions') {
      const folder = (id,name,sub = '') => h('button',{type:'button',class:'pa-row',dataset:{collectionId:id,scope:sub},'aria-pressed':String(collectionId === id && scope === sub),
        onclick:() => navigate('motions',sub,id)},name);
      folderRows.push(folder('','我的动作版本'),folder('','我生成的','generated'),...collectionItems.map(x => folder(x.id,x.name)));
    }
    folders.replaceChildren(...folderRows);
    const text = search.value.trim().toLowerCase();
    const filtered = items.filter(item => {
      const words = [item.name,item.text,item.motion_id,item.id,item.collection_name].filter(Boolean).join(' ');
      return !text || words.toLowerCase().includes(text);
    });
    const rows = filtered.map(item => {
      const id = assetId(item);
      let hint = item.duration ? item.duration.toFixed(2) + ' s' :
        item.count != null ? item.count + ' 个动作 · ' + new Date(item.created * 1000).toLocaleDateString() : '';
      if (group === 'snapshots' && item.status === 'queued') hint = '排队中';
      if (group === 'snapshots' && item.status === 'failed') hint = '创建失败';
      return h('button',{type:'button',class:'pa-row',dataset:{assetId:id},'aria-pressed':String(id === detailId),onclick:() =>
        showDetail(item)},h('strong',{},item.name || item.text || id), hint ? h('small',{},hint) : null);
    });
    list.replaceChildren(...rows);
    if (!filtered.length && !retryFirstPage) list.append(para(text ? '当前列表没有匹配项。' : '这里还没有资产。','muted'));
    if (retryFirstPage) {
      list.append(button('重新加载', () => load(false, {retainCollectionPages: true})));
    } else if (after != null) {
      const more = button('加载更多',() => load(true));
      more.disabled = pageLoad?.ticket === nav;
      list.append(more);
    }
    if (restoreFolderFocus) {
      const next = folderRows.find(row => row.dataset.collectionId === focused.dataset.collectionId && row.dataset.scope === focused.dataset.scope)
        || folderRows.find(row => row.getAttribute('aria-pressed') === 'true') || search;
      next.focus({preventScroll:true});
    } else if (restoreListFocus) {
      const next = rows.find(row => row.dataset.assetId === focused.dataset.assetId) || rows.at(-1) || search;
      next.focus({preventScroll:true});
    }
    if (active && host.scrollTop !== scrollTop) host.scrollTop = scrollTop;
  }

  function createCollection() {
    const name = input('text','',{required:true,maxlength:80});
    const requestIds = new Map();
    createForm('新建动作集合',field('集合名称',name),async isCurrentForm => {
      const collectionName = name.value.trim();
      if (!collectionName) throw Error('请输入集合名称。');
      if (!requestIds.has(collectionName)) requestIds.set(collectionName, randomId());
      const id = requestIds.get(collectionName);
      name.readOnly = true;
      try {
        await collection(null,{action:'create',id,name:collectionName});
        if (isCurrentForm()) navigate('motions','',id);
      } finally {
        name.readOnly = false;
      }
    });
  }

  async function addMotion(id, target = '', trigger = null) {
    if (!active) return;
    if (!id) { note('请先从动作库或编辑结果中选择动作。'); return; }
    const epoch = session;
    const context = ++addContext;
    const current = () => active && epoch === session && context === addContext && (!trigger || trigger.isConnected);
    note();
    let sets;
    try {
      sets = (await collection()).collections;
    } catch (error) {
      if (current() && error.name !== 'AbortError') note(error.message || String(error));
      return;
    }
    if (!current()) return;
    if (!sets.length) { note('请先新建一个动作集合。'); return; }
    const select = choices(sets.map(x => [x.id,x.name]),target || sets[0].id);
    createForm('加入动作集合',field('集合',select),async isCurrentForm => {
      const selectedCollection = select.value;
      if (!selectedCollection) throw Error('请先新建一个动作集合。');
      try {
        await collection(null,{action:'add',id:selectedCollection,motion_id:id,require_existing:true});
      } catch (error) {
        if (error.status === 409 && isCurrentForm()) {
          const latest = await collection();
          if (!isCurrentForm()) return;
          const preferred = select.value;
          const values = latest.collections.map(row => [row.id,row.name]);
          select.replaceChildren(...(values.length ? values : [['','暂无可用集合']])
            .map(([value,name]) => h('option',{value},name)));
          if (values.some(([id]) => id === preferred)) select.value = preferred;
          collectionItems = latest.collections;
          if (collectionId === selectedCollection) navigate('motions');
          else renderList();
        }
        throw error;
      }
      if (!isCurrentForm()) return;
      if (active) note('已加入集合。');
      if (collectionId === selectedCollection) await load(false, {retainCollectionPages: true});
    });
  }

  function freeze(event) {
    return readCollection(event.currentTarget, async (cid, current) => {
      let source = await request('snapshot-source',{collection:cid});
      if (!current()) return;
      const name = input('text',source.name,{required:true,maxlength:120});
      const summary = para('将保留 ' + source.count + ' 条动作的确切版本。快照用于取回结果。','muted');
      createForm('固定快照',h('div',{},field('快照名称',name),summary),async isCurrentForm => {
        const snapshotName = name.value.trim();
        if (!snapshotName) throw Error('请输入快照名称。');
        name.readOnly = true;
        let job;
        try {
          job = await submitOnce('asset-snapshot', base + 'snapshots', {
            collection_id:cid, name:snapshotName, expected_basis:source.basis
          }, 'id', { acknowledge: isCurrentForm });
        } catch (error) {
          if (error.status !== 409 || !isCurrentForm()) throw error;
          summary.textContent = '集合成员已变化，正在重新读取…';
          try {
            const latest = await request('snapshot-source',{collection:cid});
            if (!isCurrentForm()) return;
            source = latest;
            summary.textContent = '集合已更新。将保留 ' + source.count + ' 条动作的确切版本。';
          } catch (refreshError) {
            if (isCurrentForm()) summary.textContent = '暂时无法确认当前集合成员。';
            throw refreshError;
          }
          throw Error('请确认更新后的成员数量，再次点击保存。');
        } finally {
          name.readOnly = false;
        }
        if (!isCurrentForm()) return;
        navigate('snapshots');
        await showSnapshotJob(job);
      });
    });
  }

  function bulkExport(event) {
    return readCollection(event.currentTarget, async (cid, current) => {
      const members = new Map();
      const cursors = new Set();
      let cursor = null, basis = null;
      do {
        const query = {id:cid};
        if (cursor !== null) query.after = cursor;
        if (basis !== null) query.expected_basis = basis;
        const page = await collection(query);
        if (!current()) return;
        if (basis !== null && page.basis !== basis) throw Error('集合成员已变化，请重新读取集合。');
        basis = page.basis ?? null;
        for (const item of page.items) members.set(item.motion_id, item);
        cursor = page.next_after ?? null;
        if (cursor !== null) {
          const key = String(cursor);
          if (cursors.has(key)) throw Error('服务端返回了重复的分页位置，无法读取完整集合。请重试。');
          cursors.add(key);
        }
        // The collection API allows 1,000 members and reads 50 at a time before filtering.
        if (members.size > 1000 || cursors.size >= 20) throw Error('读取集合时超出了分页上限。请重试。');
      } while (cursor !== null);
      if (!members.size) throw Error('集合里还没有可导出的动作。');
      window.openAssetCollectionDownloads?.([...members.values()]);
    });
  }

  function rigForm(existing, importing = false) {
    const name = input('text',existing?.name || '',{required:true,maxlength:120});
    const file = input('file','',{accept:'.bvh',required:true});
    const frame = input('number',existing?.data.calibration_frame ?? '',{min:0,step:1,placeholder:'留空使用零旋转骨架'});
    const units = choices([['0.01','厘米'],['1','米'],['0.001','毫米']],String(existing?.data.units || .01));
    const directions = ['+X','-X','+Y','-Y','+Z','-Z'].map(axis => [axis,axis]);
    const up = choices(directions,existing?.data.up || '+Y'), forward = choices(directions,existing?.data.forward || '+Z');
    const preview = h('div');
    const config = h('div',{class:'pa-fields'},field('单位',units),field('上轴',up),field('前向',forward),
                     field('校准帧（从 0 开始）',frame));
    const mapping = h('div',{class:'pa-fields'});
    const saved = choices([['','创建新的骨架预设']],'');
    const rigLabel = input('text','',{maxlength:120,placeholder:'例如 录制室 A · 单人骨架'});
    const newRig = h('div',{},importing ? field('新预设名称',rigLabel) : null,config,mapping);
    let parsed, raw, mapFields, candidates = [], suggestedName = '';
    let inspectionRequest = 0;
    const createdRigs = new Map();
    const rigRequests = new Map();
    const importRequests = new Map();
    function syncRigFields() {
      newRig.hidden = Boolean(saved.value) || !parsed;
      for (const control of newRig.querySelectorAll('input, select')) control.disabled = newRig.hidden;
    }
    saved.onchange = syncRigFields;
    syncRigFields();
    const setFile = async () => {
      if (file.disabled) return;
      const selected = file.files[0];
      const requestId = ++inspectionRequest;
      const isCurrent = () => requestId === inspectionRequest && dialog === rigDialog &&
        rigDialog.open && file.files[0] === selected;
      parsed = null; raw = null; saved.disabled = true;
      createdRigs.clear();
      rigRequests.clear();
      importRequests.clear();
      candidates = [];
      mapping.replaceChildren();
      saved.replaceChildren(h('option',{value:''},'创建新的骨架预设'));
      syncRigFields();
      frame.removeAttribute('max');
      preview.textContent = '';
      const nextName = selected?.name.replace(/\.bvh$/i,'') || '';
      if (!name.value || name.value === suggestedName) name.value = nextName;
      suggestedName = nextName;
      if (!selected) return;
      preview.textContent = '正在读取骨架…';
      try {
        const payload = await readFile(selected);
        if (!isCurrent()) return;
        const info = await post('inspect-bvh',{file:payload});
        if (!isCurrent()) return;
        const matching = importing ? await readAssetChoices('rigs',{signature:info.signature},isCurrent) : [];
        if (!isCurrent()) return;
        raw = payload; parsed = info; candidates = matching;
        preview.textContent = info.frames + ' 帧 · ' + info.fps.toFixed(2) + ' FPS';
        frame.max = String(info.frames - 1);
        mapFields = bodyNames.map(key => [key,choices([['','未映射'],...info.joints.map(j => [j,j])],
          (existing?.data.mapping || info.mapping)[key] || '')]);
        mapping.replaceChildren(...mapFields.map(([key,el]) => field(key,el)));
        saved.replaceChildren(...matching.map(x => h('option',{value:x.id},x.name)),h('option',{value:''},'创建新的骨架预设'));
        saved.disabled = false;
        saved.value = matching[0]?.id || '';
        syncRigFields();
      } catch (err) {
        if (isCurrent()) preview.textContent = err.message;
      }
    };
    file.onchange = setFile;
    const fields = h('div',{},field(importing ? '动作名称' : '预设名称',name),
      field(importing ? '动作 BVH' : '校准 BVH',file),preview,
      importing ? field('骨架预设',saved) : null,newRig,
      importing ? para('用动作文件创建预设时，可留空使用骨架零旋转，或指定文件中实际的校准帧。','muted') : null);
    const rigDialog = createForm(importing ? '导入单人 BVH' : existing ? '编辑骨架预设' : '新建骨架预设',fields,
      async isCurrentForm => {
        if (!parsed || !raw) throw Error('请先读取 BVH 骨架。');
        const payload = raw;
        const assetName = name.value.trim();
        let rig = saved.value;
        if (rig && !candidates.some(x => x.id === rig)) throw Error('请选择这个骨架的预设。');
        const controls = fields.querySelectorAll('input:enabled, select:enabled');
        for (const control of controls) control.disabled = true;
        try {
          if (!rig) {
            const rigName = importing ? rigLabel.value.trim() : assetName;
            if (importing && !rigName) throw Error('请输入新骨架预设名称。');
            const settings = {name:rigName,
              frame:frame.value === '' ? null : Number(frame.value),mapping:Object.fromEntries(mapFields.map(([key,el]) => [key,el.value])),
              units:Number(units.value),up:up.value,forward:forward.value,...(existing ? {previous:existing.id} : {})};
            const key = JSON.stringify(settings);
            rig = createdRigs.get(key);
            if (!rig) {
              const request = {...settings,file:payload};
              if (importing && parsed.rig_import_preflight === true) request.validate_import = true;
              if (parsed.rig_save_request_id === true) {
                if (!rigRequests.has(key)) rigRequests.set(key,randomId());
                request.id = rigRequests.get(key);
              }
              rig = (await post('rigs',request)).id;
              createdRigs.set(key,rig);
            }
          }
          if (!isCurrentForm()) return;
          if (importing) {
            const key = JSON.stringify([assetName,rig]);
            if (!importRequests.has(key)) importRequests.set(key,randomId());
            const motion = await post('import-bvh',{name:assetName,file:payload,rig,id:importRequests.get(key)});
            if (!isCurrentForm()) return;
            navigate('motions');
            await showDetail({motion_id:motion.id,text:motion.description});
          } else {
            navigate('rigs');
            await showDetail({id:rig});
          }
        } finally {
          for (const control of controls) control.disabled = false;
        }
      });
  }

  function objectForm(existing) {
    const data = existing?.data;
    const name = input('text',existing?.name || '',{required:true,maxlength:120});
    const file = input('file','',{accept:'.obj',required:!existing});
    const size = ['宽 X','高 Y','深 Z'].map((axis,i) => [axis,input('number',data?.size_m[i] ?? '',{required:true,min:0.01,max:20,step:'any'})]);
    const angle = input('number',data?.heading_deg ?? 0,{min:-360,max:360,step:'any',required:true});
    const anchors = h('div',{class:'pa-anchors'});
    const rows = [];
    const add = (value = {}) => {
      if (rows.length >= 16) return;
      const a = input('text',value.name || '',{required:true,maxlength:120});
      const xyz = ['X','Y','Z'].map((key,i) => input('number',value.position_m?.[i] ?? '',{step:'any',required:true,'aria-label':key}));
      const row = h('div',{class:'pa-fields'},field('接触点名称',a),...xyz.map((el,i) => field('XYZ'[i]+'（米）',el)));
      const values = {a,xyz};
      row.append(button('移除',() => { rows.splice(rows.indexOf(values),1); row.remove(); }));
      rows.push(values); anchors.append(row);
    };
    data?.anchors.forEach(add);
    const fields = h('div',{},field('名称',name),field('扫描模型（OBJ）',file),
      h('div',{class:'pa-fields'},...size.map(([axis,el]) => field(axis+'（米）',el)),field('朝向（度）',angle)),
      para('接触点坐标以包围盒中心为原点，需落在包围盒表面。','muted'),anchors,button('添加接触点',() => add()));
    const requestIds = new Map();
    let requestFile;
    createForm(existing ? '编辑交互物体' : '上传交互物体',fields,async isCurrentForm => {
      const value = {name:name.value.trim(),size_m:size.map(([,el]) => +el.value),heading_deg:+angle.value,
        anchors:rows.map(row => ({name:row.a.value.trim(),position_m:row.xyz.map(el => +el.value)}))};
      if (!value.name) throw Error('请输入物体名称。');
      if (existing) value.previous = existing.id;
      const selected = file.files[0];
      if (S.capabilities?.object_save_request_id === true) {
        if (selected !== requestFile) {
          requestIds.clear();
          requestFile = selected;
        }
        const key = JSON.stringify(value);
        if (!requestIds.has(key)) requestIds.set(key, randomId());
        value.id = requestIds.get(key);
      }
      const controls = fields.querySelectorAll('input:enabled, button:enabled');
      for (const control of controls) control.disabled = true;
      try {
        if (selected) value.file = await readFile(selected);
        if (!isCurrentForm()) return;
        const asset = await post('objects',value);
        if (!isCurrentForm()) return;
        navigate('objects'); await showDetail(asset);
      } finally {
        for (const control of controls) control.disabled = false;
      }
    });
  }

  function saveLibraryPose() {
    const id = S.lib.selected;
    const viewer = S.lib.previewId === id ? window.motionView : null;
    const clip = viewer?.clip;
    saveSelectedPose(id, clip ? Math.floor(viewer.frame) : 0, clip?.frames);
  }

  function saveSelectedPose(id, frame, frames) {
    if (!id) { note('请先选择动作。'); return; }
    const name = input('text','',{required:true,maxlength:120});
    const index = input('number',frame,{required:true,min:0,step:1});
    if (frames > 0) index.max = String(frames - 1);
    const requestIds = new Map();
    createForm('保存单帧姿态',h('div',{},field('姿态名称',name),field('帧（从 0 开始）',index)),async isCurrentForm => {
      const value = {name:name.value.trim(),motion_id:id,frame:Number(index.value)};
      if (!value.name) throw Error('请输入姿态名称。');
      if (S.capabilities?.pose_save_request_id === true) {
        const key = JSON.stringify([value.name,value.frame]);
        if (!requestIds.has(key)) requestIds.set(key, randomId());
        value.id = requestIds.get(key);
      }
      name.disabled = index.disabled = true;
      try {
        const asset = await post('poses',value);
        if (!isCurrentForm()) return;
        if (active) { navigate('rigs','poses'); await showDetail(asset); }
        else if (scope === 'poses') await load();
      } finally {
        name.disabled = index.disabled = false;
      }
    });
  }

  async function applySelectedPose() {
    const editor = window.RepairEditor;
    const workspace = window.Workspace;
    const tab = workspace.state.activeTabId;
    const motionId = editor?.getLoadedSource();
    const clip = editor?.originalView?.clip;
    if (!motionId || !clip) return;
    const targetFrame = Math.floor(editor.originalView.frame);
    const epoch = session;
    const ticket = ++poseRequest;
    const current = () => epoch === session && ticket === poseRequest && workspace.state.activeTabId === tab &&
      window.SequenceEditor.getMode() === 'repair' && editor.originalView.clip === clip && editor.getLoadedSource() === motionId;
    try {
      const poses = await readAssetChoices('poses',undefined,current);
      if (!current()) return;
      const select = choices(poses.length ? poses.map(pose => [pose.id,pose.name]) : [['','请先保存单帧姿态']]);
      const frame = input('number',targetFrame,{min:0,max:clip.frames-1,step:1,required:true});
      const blend = input('number',8,{min:1,max:30,step:1,required:true});
      const requestIds = new Map();
      createForm('插入单帧姿态',h('div',{},field('姿态',select),field('目标帧',frame),field('前后过渡帧数',blend),
        para('保留骨盆轨迹，保存为新动作版本。','muted')), async isCurrentForm => {
        if (!select.value) throw Error('请先保存单帧姿态。');
        const body = {pose:select.value,motion_id:motionId,frame:+frame.value,blend:+blend.value};
        const fingerprint = JSON.stringify(body);
        if (!requestIds.has(fingerprint)) requestIds.set(fingerprint,randomId());
        select.disabled = frame.disabled = blend.disabled = true;
        try {
          const asset = await post('apply-pose',{id:requestIds.get(fingerprint),...body});
          if (isCurrentForm()) await workspace.openMotionInTab('editor',asset.id,{label:asset.description,forceNew:true});
        } finally {
          select.disabled = frame.disabled = blend.disabled = false;
        }
      });
    } catch (err) {
      if (current() && err.name !== 'AbortError') toast(err.message || String(err), true);
    }
  }

  function useCanvas() {
    if (!view) {
      const canvas = h('canvas',{class:'pa-canvas','aria-label':'动作三维预览'});
      view = new MotionView.View(canvas);
      view.canvas = canvas;
    }
    view.clear();
    return view;
  }

  async function showMotion(motion, panel, ticket, frame = null, snapshotId = null) {
    const id = motion.version_id || motion.motion_id;
    const viewer = useCanvas();
    if (!active) viewer.suspend();
    const clip = snapshotId
      ? MotionView.decodeJsonPreview(await cloud('/api/harness/motions/' + encodeURIComponent(id) + '/preview', {
          query: {snapshot: snapshotId, source: motion.motion_id}
        }))
      : await MotionLoader.fetchPreview(id);
    if (ticket !== detailSerial) return;
    viewer.setClip(clip);
    panel.append(viewer.canvas);
    if (frame != null) viewer.seek(frame);
    if (active) viewer.resume();
    if (frame == null) panel.append(button('播放 / 暂停',() => viewer.setPlaying(!viewer.playing)),
      button('保存当前帧为姿态',() => saveSelectedPose(id,Math.floor(viewer.frame),clip.meta.frames)));
  }

  async function showDetail(item) {
    const key = pageKey(), id = assetId(item), ticket = ++detailSerial;
    clearTimeout(jobPoll);
    detailId = id; renderList();
    view?.suspend();
    const loading = para('正在加载…','muted');
    details.replaceChildren(loading);
    try {
      await renderDetail(item, key, id, ticket);
    } catch (err) {
      if (ticket !== detailSerial || err.name === 'AbortError') return;
      loading.remove();
      detailError(err, item);
    }
  }

  function detailError(err, item) {
    details.append(h('p',{class:'pa-status',role:'alert'},err.message || String(err)),
      button('重试',() => showDetail(item)));
  }

  function clearMotionDetail() {
    detailId = '';
    ++detailSerial;
    view?.clear();
    view?.suspend();
    details.replaceChildren(para('选择一项查看详情。','muted'));
  }

  function removeFromCollection(cid, id) {
    return work(async epoch => {
      const current = () => epoch === session && group === 'motions' && collectionId === cid;
      try {
        await collection(null,{action:'remove',id:cid,motion_id:id});
      } catch (error) {
        if (current()) throw error;
        return;
      }
      if (!current()) return;
      if (detailId === id) clearMotionDetail();
      if (!hasRequestedMore || (pageLoad && !pageLoad.append)) {
        await load(false, {retainCollectionPages: true});
        return;
      }
      // Collection cursors are ordered by motion ID and remain valid after removal.
      items = items.filter(item => assetId(item) !== id);
      renderList();
    });
  }

  async function renderDetail(item, key, id, ticket) {
    if (key === 'snapshots') {
      if (item.status) { await showSnapshotJob(item); return; }
      const value = await request('snapshots/' + id);
      if (ticket === detailSerial) showSnapshot(value);
      return;
    }
    if (key === 'motions' || key === 'collection' || key === 'generated') {
      const {task_id, status, accepted} = item;
      if (task_id && status) MotionLoader.rememberMeta(id, {task_id, status, accepted});
      const quarantined = (status ?? MotionLoader.getCachedMeta(id)?.status) === 'quarantined';
      const panel = h('div',{},h('h2',{},item.text || id),
        quarantined ? para('未通过 · 隔离候选', 'pill bad') : null);
      details.replaceChildren(panel);
      const cid = collectionId;
      panel.append(h('div',{class:'pa-controls'},
        button('在编辑器打开',() => window.Workspace.openMotionInTab('editor',id,{label:item.text})),
        button('加入集合',event => addMotion(id,'',event.currentTarget)),
        cid ? button('移出集合',() => removeFromCollection(cid,id)) : null,
        button('版本历史',event => showHistory(id,panel,event.currentTarget))));
      await showMotion(item,panel,ticket);
      return;
    }
    const asset = await request(key + '/' + id);
    if (ticket !== detailSerial) return;
    const panel = h('div',{},h('h2',{},asset.name),para('版本 ' + id.slice(0,8),'muted'));
    details.replaceChildren(panel);
    const data = asset.data;
    if (key === 'rigs') {
      panel.append(para(data.up + ' 向上 · ' + data.forward + ' 向前'),
      para(data.calibration_frame == null ? '零旋转骨架' : '校准帧 ' + data.calibration_frame),
      h('dl',{class:'pa-mapping'},...Object.entries(data.mapping).flatMap(([k,v]) => [h('dt',{},k),h('dd',{},v)])),
        button('编辑并保存为新版本',() => rigForm(asset)));
      const calibration = await request('rigs/' + id + '/calibration');
      if (ticket === detailSerial) {
        const canvas = h('canvas',{class:'pa-canvas',width:620,height:300,'aria-label':'左侧源校准骨架，右侧 Kinora 校准骨架'});
        panel.insertBefore(canvas,panel.children[2]);
        panel.insertBefore(para('源校准骨架（左）  ·  Kinora（右）','muted'),canvas);
        drawCalibration(canvas,calibration);
      }
    }
    if (key === 'poses') await showMotion(data,panel,ticket,data.frame);
    if (key === 'objects') {
      panel.append(para(data.size_m.join(' × ') + ' m · ' + (data.anchors.length ? '可交互' : '可预览')),
        button('编辑并保存为新版本',() => objectForm(asset)));
      const anchors = choices([['','仅摆放'],...data.anchors.map((a,i) => [String(i),a.name])],'');
      const hand = choices([['20','左手'],['21','右手']],'20');
      panel.append(h('div',{class:'pa-controls'},field('接触点',anchors),field('接触侧',hand),button('添加到场景',() =>
        work(() => placeObject(asset,anchors.value === '' ? null : data.anchors[+anchors.value],+hand.value)),true)));
      const canvas = h('canvas',{class:'pa-canvas',width:620,height:340,hidden:true,'aria-label':'OBJ 网格预览，拖动旋转'});
      const feedback = h('div');
      panel.append(canvas,feedback);
      async function loadPreview() {
        feedback.replaceChildren(h('p',{class:'muted',role:'status'},'正在加载 OBJ 预览…'));
        try {
          const mesh = await request('objects/' + id + '/mesh');
          if (ticket !== detailSerial) return;
          drawMesh(canvas,mesh);
          canvas.hidden = false;
          feedback.replaceChildren();
        } catch (err) {
          if (ticket !== detailSerial || err.name === 'AbortError') return;
          feedback.replaceChildren(h('p',{class:'pa-status',role:'alert'},err.message || String(err)),
            button('重试预览',loadPreview));
        }
      }
      await loadPreview();
    }
  }

  async function placeObject(asset, anchor, hand) {
    const ws = window.Workspace;
    const epoch = session;
    let target = ws.state.tabs.find(t => t.kind === 'generate' &&
      (previousSceneId ? t.id === previousSceneId : t.state.subMode === 'scene'));
    if (previousSceneId && !target) throw Error('原场景标签已关闭。请从目标场景重新选择物体。');
    if (target && target.state.subMode !== 'scene') {
      throw Error('原场景标签已切换模式。请从目标场景重新选择物体。');
    }
    if (target) await ws.activateTab(target.id);
    else target = await ws.activateOrCreateTab('generate',{subMode:'scene',forceNew:true});
    if (epoch !== session || ws.state.activeTabId !== target?.id || document.body.dataset.genMode !== 'scene') return;
    try {
      window.SceneController.addPersonalObject(asset,anchor,hand);
      previousSceneId = '';
    } catch (err) {
      toast(err.message || String(err), true);
    }
  }

  function drawCalibration(canvas,calibration) {
    const ctx = canvas.getContext('2d');
    const rig = model => {
      const xs = model.points.map(p => p[0]), ys = model.points.map(p => p[1]);
      return {x:(Math.min(...xs)+Math.max(...xs))/2,y:Math.min(...ys),height:Math.max(...ys)-Math.min(...ys)};
    };
    const bounds = [rig(calibration.source),rig(calibration.target)];
    const scale = 235 / Math.max(.01,...bounds.map(b => b.height));
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.strokeStyle = 'rgba(11,11,11,.14)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(16,canvas.height-25);
    ctx.lineTo(canvas.width-16,canvas.height-25);
    ctx.stroke();
    [calibration.source,calibration.target].forEach((model,side) => {
      const x = canvas.width*(side ? .75 : .25), {x:cx,y:cy} = bounds[side];
      const coords = model.points.map(p => [x+scale*(p[0]-cx),canvas.height-25-scale*(p[1]-cy)]);
      ctx.strokeStyle = side ? '#0b0b0b' : '#8e8a82';
      ctx.lineWidth = 2;
      ctx.beginPath();
      model.parents.forEach((parent,i) => {
        if (parent >= 0) { ctx.moveTo(...coords[parent]); ctx.lineTo(...coords[i]); }
      });
      ctx.stroke();
    });
  }

  function drawMesh(canvas, mesh) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const extents = [[Infinity,-Infinity],[Infinity,-Infinity],[Infinity,-Infinity]];
    for (const v of mesh.vertices) v.forEach((n,i) => { extents[i][0]=Math.min(n,extents[i][0]); extents[i][1]=Math.max(n,extents[i][1]); });
    const center = extents.map(([a,b]) => (a+b)/2);
    const scale = 235 / Math.max(1e-8,...extents.map(([a,b]) => b-a));
    const faces = mesh.polygons || mesh.faces;
    const step = Math.max(1,Math.ceil(faces.length/16000));
    let yaw = .6, pitch = .35, previous = null;
    function draw() {
      const c = Math.cos(yaw), s = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
      const projected = mesh.vertices.map(v => {
        const [a,b,d] = v.map((n,i) => (n-center[i])*scale);
        return [canvas.width/2+a*c+d*s,canvas.height/2-b*cp-(-a*s+d*c)*sp];
      });
      ctx.clearRect(0,0,canvas.width,canvas.height);
      ctx.strokeStyle = 'rgba(11,11,11,.72)'; ctx.lineWidth = .65; ctx.beginPath();
      for (let i=0;i<faces.length;i+=step) {
        const face = faces[i];
        ctx.moveTo(...projected[face[0]]);
        for (let j=1;j<face.length;j++) ctx.lineTo(...projected[face[j]]);
        ctx.closePath();
      }
      ctx.stroke();
    }
    canvas.onpointerdown = event => { previous=[event.clientX,event.clientY]; canvas.setPointerCapture(event.pointerId); };
    canvas.onpointermove = event => {
      if (!previous) return;
      yaw += (event.clientX-previous[0])*.012; pitch += (event.clientY-previous[1])*.012;
      previous=[event.clientX,event.clientY]; draw();
    };
    canvas.onpointerup = canvas.onpointercancel = () => { previous=null; };
    draw();
  }

  async function showHistory(id,panel,trigger) {
    if (trigger.getAttribute('aria-disabled') === 'true') return;
    let history = panel.querySelector('.pa-history');
    if (!history) {
      history = h('div',{class:'pa-history'},h('h3',{},'版本历史'));
      panel.append(history);
    }
    history.querySelector('.pa-history-feedback')?.remove();
    const feedback = h('p',{class:'pa-history-feedback muted',role:'status'},'正在加载版本历史…');
    history.append(feedback);
    trigger.setAttribute('aria-disabled','true');
    let graph;
    try {
      graph = await request('history',{motion:id});
    } catch (error) {
      if (panel.isConnected && error.name !== 'AbortError') {
        feedback.setAttribute('role','alert');
        feedback.textContent = error.message || String(error);
      } else {
        feedback.remove();
      }
      return;
    } finally {
      trigger.removeAttribute('aria-disabled');
    }
    if (!panel.isConnected) return;
    history.replaceChildren(h('h3',{},'版本历史'),
      ...graph.items.map(m => h('div',{},h('strong',{},m.text || m.motion_id),
        para(m.motion_id + (m.unavailable ? ' · 不可访问' : ''),'muted'),
        m.provenance?.rig_version ? button('标定预设版本 ' + m.provenance.rig_version.slice(0,8),() => {
          navigate('rigs'); showDetail({id:m.provenance.rig_version});
        }) : null,
        m.provenance?.original_sha256 ? h('p',{class:'muted',copy:'原始 BVH 校验值'},'BVH · ' + m.provenance.original_sha256) : null,
        ...((m.sources || []).map(source => para('来源 ' + source.motion_id +
          (source.selection ? ' · 帧 ' + source.selection.start_frame + '–' + source.selection.end_frame :
           source.start_frame != null ? ' · 帧 ' + source.start_frame + '–' + source.end_frame : '')))))));
    if (graph.truncated) history.append(para('此处最多显示 50 个来源版本。','muted'));
  }

  async function showSnapshotJob(job) {
    clearTimeout(jobPoll);
    const serial = detailSerial;
    detailId = job.id;
    if (!items.some(item => item.id === job.id)) updateSnapshotRow(job);
    renderList();
    const state = h('p',{role:'status'});
    let refreshing = false;
    let retryDelay = 2000;
    const retry = button('重试',() => {
      if (refreshing) return;
      clearTimeout(jobPoll);
      retry.hidden = true;
      progress();
      return refresh();
    });
    retry.hidden = true;
    details.replaceChildren(h('h2',{},job.name),state,retry);

    function progress() {
      state.setAttribute('role','status');
      state.textContent = job.status === 'done' ? '正在读取快照…' : '正在固定动作版本…';
    }

    function schedule(canRetry = true, delay = 2000) {
      clearTimeout(jobPoll);
      jobPoll = setTimeout(() => refresh(canRetry),delay);
    }

    async function refresh(canRetry = true) {
      if (serial !== detailSerial || refreshing) return;
      refreshing = true;
      try {
        if (job.status === 'queued') {
          job = await request('snapshot-jobs/' + job.id);
          canRetry = true;
        }
        if (serial !== detailSerial) return;
        if (job.status === 'done') {
          progress();
          const snapshot = await request('snapshots/' + job.id);
          if (serial !== detailSerial) return;
          const {id, name, created, collection_name} = snapshot;
          updateSnapshotRow({id, name, created, collection_name, count:snapshot.items.length});
          showSnapshot(snapshot);
          if (!hasRequestedMore) await load();
        } else if (job.status === 'queued') {
          retryDelay = 2000;
          retry.hidden = true;
          progress();
          schedule();
        } else {
          updateSnapshotRow(job);
          renderList();
          state.setAttribute('role','alert');
          state.textContent = job.error;
        }
      } catch (error) {
        if (serial !== detailSerial || error.name === 'AbortError') return;
        const status = error.status;
        const temporary = status === 0 || status === 408 || status === 429 || status >= 500;
        if (temporary && job.status === 'queued') {
          state.textContent = '暂时无法读取快照状态，正在重试…';
          retry.hidden = retryDelay === 2000;
          schedule(true, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
          return;
        }
        if (temporary && canRetry) {
          state.textContent = '正在重试读取快照…';
          schedule(false);
          return;
        }
        state.setAttribute('role','alert');
        state.textContent = error.message || String(error);
        retry.hidden = false;
      } finally {
        refreshing = false;
      }
    }

    progress();
    if (job.status === 'queued') schedule();
    else await refresh();
  }

  function updateSnapshotRow(row) {
    pageLoad?.snapshotUpdates?.set(row.id, row);
    const index = items.findIndex(item => item.id === row.id);
    if (index === -1) items.unshift(row);
    else items[index] = row;
  }

  function showSnapshot(snapshot) {
    detailId = snapshot.id;
    renderList();
    const preview = h('div');
    const exportControls = h('div',{class:'pa-controls'});
    const batches = snapshot.batches.length;
    const history = snapshotDownloadHistory.get(snapshot.id) ?? {selectedBatch:1, outcomes:new Map()};
    const choose = choices(Array.from({length:batches},(_,i) => [String(i+1),`第 ${i+1} 批 / 共 ${batches} 批`]),
      String(history.selectedBatch));
    if (batches > 1) exportControls.append(choose);
    const downloadStatus = h('p',{class:'pa-status',role:'status'});
    function rememberBatch() {
      history.selectedBatch = Number(choose.value);
      snapshotDownloadHistory.set(snapshot.id, history);
    }
    choose.addEventListener('change', () => { rememberBatch(); updateDownload(); });
    const downloadButton = button('下载 NPZ 快照',async () => {
      if (snapshotDownloads.has(snapshot.id)) return;
      rememberBatch();
      const batch = history.selectedBatch;
      const operation = {update: updateDownload};
      snapshotDownloads.set(snapshot.id, operation);
      updateDownload();
      let message, failed = false;
      try {
        const result = await downloadFile({path:base+'snapshots/'+snapshot.id+'/download',query:{batch},
          name:'kinora-snapshot-'+snapshot.id.slice(0,8)+'-'+batch+'.zip'});
        if (result?.cancelled) message = '已取消下载。';
        else if (result?.saved) message = '已保存 ' + result.saved;
        else throw Error(result?.error || '下载失败，请重试。');
      } catch (error) {
        failed = true;
        message = error?.message || '下载失败，请重试。';
      } finally {
        if (snapshotDownloads.get(snapshot.id) === operation) {
          snapshotDownloads.delete(snapshot.id);
          history.outcomes.set(batch, {message, failed});
          operation.update();
        }
      }
    }, true);
    function updateDownload() {
      if (!exportControls.isConnected) return;
      const pending = snapshotDownloads.has(snapshot.id);
      const outcome = pending ? null : history.outcomes.get(Number(choose.value));
      choose.disabled = pending;
      if (pending) downloadButton.setAttribute('aria-disabled','true');
      else downloadButton.removeAttribute('aria-disabled');
      downloadStatus.setAttribute('role',outcome?.failed ? 'alert' : 'status');
      downloadStatus.textContent = pending ? '请选择保存位置，随后开始下载…' : outcome?.message || '';
    }
    exportControls.append(downloadButton);
    const versions = h('div',{class:'pa-snapshot-items'},...snapshot.items.map(item =>
      h('button',{type:'button',class:'btn subtle','aria-pressed':'false',
        onclick:event => previewVersion(item,event.currentTarget)},
        (item.label || item.motion_id) + ' · ' + item.version_id.slice(-8))));

    async function previewVersion(item, trigger) {
      versions.querySelector('[aria-pressed="true"]')?.setAttribute('aria-pressed','false');
      trigger.setAttribute('aria-pressed','true');
      const serial = ++detailSerial;
      const loading = h('p',{role:'status',class:'muted'},'正在加载动作预览…');
      preview.replaceChildren(loading);
      try {
        await showMotion(item,preview,serial,null,snapshot.id);
      } catch (error) {
        if (serial !== detailSerial || error.name === 'AbortError') return;
        preview.replaceChildren(h('p',{class:'pa-status',role:'alert'},error.message || String(error)),
          button('重试',() => previewVersion(item,trigger)));
      } finally {
        loading.remove();
      }
    }
    details.replaceChildren(h('h2',{},snapshot.name),para(snapshot.collection_name + ' · ' + snapshot.items.length + ' 个动作'),
      para('结果快照 · 固定动作文件与版本清单','muted'),exportControls,downloadStatus,preview,versions);
    const pending = snapshotDownloads.get(snapshot.id);
    if (pending) pending.update = updateDownload;
    updateDownload();
  }

  search.oninput = renderList;
  $('motion-collect')?.addEventListener('click',async () => {
    const id = S.lib.selected;
    const epoch = session;
    const workspace = window.Workspace;
    const tab = await workspace.activateOrCreateTab('assets');
    if (tab && epoch === session && tab.id === workspace.state.activeTabId) addMotion(id);
  });
  $('edit-save-personal-pose')?.addEventListener('click',() => {
    const editor = window.RepairEditor;
    const viewer = editor?.originalView;
    saveSelectedPose(editor?.getLoadedSource(),Math.floor(viewer?.frame || 0),viewer?.clip?.frames);
  });
  $('edit-apply-personal-pose')?.addEventListener('click',applySelectedPose);
  $('scene-pick-personal')?.addEventListener('click',() => window.PersonalAssets.pickObject());
  $('assets-open')?.addEventListener('click',() => {
    previousSceneId = '';
    window.Workspace.activateOrCreateTab('assets');
  });
  window.PersonalAssets = {
    cancelPoseLookup() { ++poseRequest; },
    setActive(value) {
      const refreshCollection = value && !active && pageKey() === 'collection';
      if (active && !value) ++addContext;
      active = value;
      if (value) {
        if (!refreshCollection && view && details.contains(view.canvas)) view.resume();
        renderChrome();
        if (refreshCollection) load(false, {retainCollectionPages: true});
        else if (!hasRequestedMore && pageLoad === null) load();
      } else view?.suspend();
    },
    async pickObject() {
      const epoch = session;
      previousSceneId = window.Workspace.state.activeTabId;
      await window.Workspace.activateOrCreateTab('assets');
      if (epoch === session && active) navigate('objects');
    },
    addMotion, savePose:saveSelectedPose,
    reset() {
      ++session; ++nav; ++detailSerial;
      active = false; busy = false; group = 'motions'; collectionId = ''; scope = ''; detailId = '';
      collectionRead = null;
      snapshotDownloads.clear();
      snapshotDownloadHistory.clear();
      previousSceneId = ''; items = []; collectionItems = []; after = null;
      pageLoad = null; hasRequestedMore = false; retryFirstPage = false; pageCursors.clear();
      clearTimeout(jobPoll); dialog?.close(); view?.clear(); view?.suspend(); details.replaceChildren(); search.value = ''; note(); renderList();
    }
  };
  renderChrome();
  renderList();
})();
