'use strict';
(() => {
  let epoch = 0, ticket = 0, creating = false, activeTab = 'config', userPickedTab = false;
  const states = {
    active: '有效',
    revoked: '已撤销',
    expired: '已过期',
    password_changed: '密码已变更，已失效',
    password_change_required: '需先修改密码',
    account_unavailable: '账号不可用'
  };
  const scopes = { read: '查询与下载', generate: '生成', retarget: '导入与重定向' };
  const dialog = $('gadget-dialog');
  const isModal = () => typeof dialog?.showModal === 'function';
  const current = (e, s) => e === epoch && s === S.session && S.user && (!isModal() || dialog.open || S.view === 'gadget');
  const say = message => { $('gadget-notice').textContent = message; };

  function hideGuard() {
    if ($('gadget-close-guard')) $('gadget-close-guard').hidden = true;
  }

  const clearSecret = () => {
    $('gadget-secret').value = '';
    $('gadget-secret-box').hidden = true;
    hideGuard();
  };

  function setTab(tab, manual = false) {
    activeTab = tab === 'keys' ? 'keys' : 'config';
    if (manual) userPickedTab = true;
    const keysOn = activeTab === 'keys';
    $('gadget-tab-config').setAttribute?.('aria-selected', String(!keysOn));
    $('gadget-tab-keys').setAttribute?.('aria-selected', String(keysOn));
    $('gadget-panel-config').hidden = keysOn;
    $('gadget-panel-keys').hidden = !keysOn;
  }

  function pathsKey() {
    if (typeof localStorage === 'undefined' || !S.status?.server || !S.user?.username) return null;
    return 'kinora-gadget-paths:' + JSON.stringify([S.status.server, S.user.username]);
  }

  function loadSavedPaths() {
    const key = pathsKey();
    if (!key) return;
    try {
      const saved = JSON.parse(localStorage.getItem(key) || 'null');
      if (!saved) return;
      if (saved.dir && !$('gadget-dir').value) $('gadget-dir').value = saved.dir;
      if (saved.keyfile && !$('gadget-keyfile').value) $('gadget-keyfile').value = saved.keyfile;
      if (saved.output && !$('gadget-output').value) $('gadget-output').value = saved.output;
      if (saved.python) $('gadget-python').value = saved.python;
      if (saved.format) $('gadget-format').value = saved.format;
    } catch (_) { /* ignore malformed local preference */ }
  }

  function savePaths() {
    const key = pathsKey();
    if (!key) return;
    try {
      localStorage.setItem(key, JSON.stringify({
        dir: $('gadget-dir').value.trim(),
        keyfile: $('gadget-keyfile').value.trim(),
        output: $('gadget-output').value.trim(),
        python: $('gadget-python').value.trim(),
        format: $('gadget-format').value
      }));
    } catch (_) { /* quota or storage unavailable */ }
  }

  window.clearGadgetSavedPaths = () => {
    const key = pathsKey();
    if (key) localStorage.removeItem(key);
  };

  window.resetGadget = ({ clearPathFields = false } = {}) => {
    epoch++;
    ticket++;
    creating = false;
    userPickedTab = false;
    clearSecret();
    $('gadget-keys').replaceChildren();
    $('gadget-create').disabled = false;
    $('gadget-config-text').value = '';
    if (clearPathFields) {
      for (const id of ['gadget-dir', 'gadget-keyfile', 'gadget-output', 'gadget-python']) {
        const input = $(id);
        input.value = input.defaultValue;
      }
      $('gadget-format').selectedIndex = 0;
    }
    say('');
    if (isModal() && dialog.open) dialog.close();
  };

  function requestClose() {
    if (creating) {
      say('正在创建密钥，请等待服务器返回后再关闭，以免丢失一次性密钥。');
      return false;
    }
    if ($('gadget-secret').value) {
      setTab('keys', true);
      if ($('gadget-close-guard')) $('gadget-close-guard').hidden = false;
      return false;
    }
    clearSecret();
    epoch++;
    ticket++;
    if (isModal() && dialog.open) dialog.close();
    return true;
  }

  async function refresh(autoSelectTab = false) {
    const e = epoch, s = S.session, t = ++ticket;
    try {
      const data = await cloud('/api/gadget/keys');
      if (!current(e, s) || t !== ticket) return;
      let hasActive = false;
      const rows = (data.keys || []).map(k => {
        const status = k.status || (k.revoked ? 'revoked' : k.expires * 1000 < Date.now() ? 'expired' : 'active');
        if (status === 'active') hasActive = true;
        let permissions = [];
        try { permissions = Array.isArray(k.scopes) ? k.scopes : JSON.parse(k.scopes); } catch (_) {}
        const b = h('button', { class: 'btn subtle sm', type: 'button', disabled: status !== 'active', text: '撤销' });
        let armed = false;
        b.onclick = async () => {
          if (!armed) { armed = true; b.textContent = '确认撤销'; return; }
          b.disabled = true;
          try {
            await cloud('/api/gadget/keys/revoke', { method: 'POST', body: { id: k.id } });
            if (current(e, s)) { say('密钥已撤销。'); clearSecret(); await refresh(false); }
          } catch (error) {
            if (current(e, s)) { say(error.message); b.disabled = false; }
          }
        };
        return h('div', { class: 'gadget-key' },
          h('span', { text: `${k.name} · ${permissions.map(x => scopes[x] || x).join('、')} · ${states[status] || status} · 到期 ${new Date(k.expires * 1000).toLocaleString()}` }),
          b);
      });
      $('gadget-keys').replaceChildren(...(rows.length ? rows : [h('p', { class: 'muted', text: '还没有个人密钥。' })]));
      if (autoSelectTab && !userPickedTab && !$('gadget-secret').value) {
        setTab(hasActive ? 'keys' : 'config', false);
      }
    } catch (error) {
      if (current(e, s)) {
        say('读取密钥失败：' + error.message);
        $('gadget-keys').replaceChildren();
      }
    }
  }

  function openGadgetModal() {
    if (!S.user) return;
    // Cancel any stale in-flight list reads from a prior open without wiping active workspace state.
    epoch++;
    ticket++;
    creating = false;
    userPickedTab = false;
    clearSecret();
    $('gadget-create').disabled = false;
    say('');
    $('gadget-server').textContent = S.status?.server || '';
    loadSavedPaths();
    setTab('config', false);
    if (isModal() && !dialog.open) dialog.showModal();
    refresh(true);
  }

  window.openGadgetModal = openGadgetModal;

  document.querySelector('[data-nav=gadget]').addEventListener('click', openGadgetModal);
  $('gadget-tab-config').onclick = () => setTab('config', true);
  $('gadget-tab-keys').onclick = () => setTab('keys', true);
  $('gadget-goto-keys').onclick = () => setTab('keys', true);
  $('gadget-close').onclick = () => requestClose();
  $('gadget-guard-stay').onclick = () => { hideGuard(); $('gadget-copy').focus?.(); };
  $('gadget-guard-confirm').onclick = () => {
    clearSecret();
    epoch++;
    ticket++;
    if (isModal() && dialog.open) dialog.close();
  };
  dialog.addEventListener('cancel', event => {
    if (!requestClose()) event.preventDefault();
  });
  dialog.addEventListener('click', event => {
    if (event.target === dialog) requestClose();
  });

  $('gadget-refresh').onclick = () => refresh(false);
  $('gadget-form').onsubmit = async event => {
    event.preventDefault();
    if (creating || !S.user) return;
    if ($('gadget-secret').value) { say('请先保存并清除上一个密钥，再创建新密钥。'); return; }
    const selected = [...document.querySelectorAll('[name=gadget-scope]:checked')].map(n => n.value);
    if (!selected.length) { say('请至少选择一种权限。'); return; }
    const e = epoch, s = S.session;
    creating = true;
    $('gadget-create').disabled = true;
    say('');
    try {
      const result = await cloud('/api/gadget/keys', {
        method: 'POST',
        body: { name: $('gadget-name').value.trim(), days: Number($('gadget-days').value), scopes: selected }
      });
      if (!current(e, s)) return;
      $('gadget-secret').value = result.key;
      $('gadget-secret-box').hidden = false;
      setTab('keys', true);
      say('已创建。请保存密钥；完整内容不会再次显示。');
      await refresh(false);
    } catch (error) {
      if (current(e, s)) say('创建未确认：' + error.message + '。请先刷新密钥列表，避免重复创建。');
    } finally {
      if (current(e, s)) {
        creating = false;
        $('gadget-create').disabled = false;
      }
    }
  };
  $('gadget-dismiss').onclick = clearSecret;

  async function copy(id) {
    const text = $(id).value;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      say('已复制。');
    } catch (_) {
      $(id).focus?.();
      $(id).select?.();
      say('复制不可用，请使用系统复制快捷键。');
    }
  }
  $('gadget-copy').onclick = () => copy('gadget-secret');
  $('gadget-config-copy').onclick = () => copy('gadget-config-text');

  $('gadget-download').onclick = async () => {
    const e = epoch, s = S.session, b = $('gadget-download');
    b.disabled = true;
    try {
      const result = await downloadFile({ path: '/gadget/agent-kit.zip', name: 'Kinora-Gadget.zip' });
      if (!current(e, s)) return;
      if (result?.cancelled) say('已取消下载。');
      else if (result?.saved) say('下载完成，请解压到固定目录。');
      else say(result?.error || '下载失败，请重试。');
    } catch (_) {
      if (current(e, s)) say('下载失败，请重试。');
    } finally {
      b.disabled = false;
    }
  };

  $('gadget-config').onclick = () => {
    const dir = $('gadget-dir').value.trim(), keyfile = $('gadget-keyfile').value.trim(), output = $('gadget-output').value.trim(), python = $('gadget-python').value.trim();
    const absolute = v => /^\//.test(v) || /^[A-Za-z]:[\\/]/.test(v) || /^\\\\[^\\]+\\[^\\]+/.test(v);
    if (![dir, keyfile, output].every(absolute) || !python) {
      say('请填写三个本机绝对路径和 Python 命令。');
      return;
    }
    const server = S.status?.server;
    if (!server) { say('请先连接服务器。'); return; }
    savePaths();
    const sep = dir.includes('\\') ? '\\' : '/', script = dir.replace(/[\\/]$/, '') + sep + 'motionfarm_mcp.py';
    const env = { MOTIONFARM_URL: server, MOTIONFARM_API_KEY_FILE: keyfile, MOTIONFARM_OUTPUT_DIR: output };
    const q = JSON.stringify;
    $('gadget-config-text').value = $('gadget-format').value === 'json'
      ? JSON.stringify({ mcpServers: { kinora: { command: python, args: [script], env } } }, null, 2)
      : '[mcp_servers.kinora]\ncommand = ' + q(python) + '\nargs = [' + q(script) + ']\n\n[mcp_servers.kinora.env]\n' + Object.entries(env).map(([k, v]) => k + ' = ' + q(v)).join('\n');
    say('配置已生成，不包含密钥。请合并配置后重新连接。');
  };
})();
