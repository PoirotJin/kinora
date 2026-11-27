'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get('verify') || fragment.get('reset');
  let mode = fragment.has('verify') ? 'verify' : fragment.has('reset') ? 'reset' : new URLSearchParams(location.search).get('view');
  if (!['register', 'progress', 'forgot', 'verify', 'reset'].includes(mode)) mode = 'register';
  // Keep the credential only in memory. Never send it in paths, referrers or logs.
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const copy = {
    register: ['申请账号', '提交申请，管理员审批通过后即可登录，无需邮箱验证。', '提交申请'],
    progress: ['申请进度', '使用申请时的用户名和密码查询审批结果。', '查询进度'],
    forgot: ['找回密码', '输入注册时验证过的电子邮箱，我们会发送重置链接。', '发送重置邮件'],
    verify: ['验证邮箱', '确认邮箱归属，即可开通普通账号。', '验证并开通账号'],
    reset: ['设置新密码', '设置新密码后，旧登录会话将失效。', '保存新密码'],
  }[mode];
  $('heading').textContent = copy[0]; $('description').textContent = copy[1]; $('submit').textContent = copy[2];
  document.title = copy[0] + ' · kinora';
  document.querySelector(`[data-view="${mode === 'progress' ? 'progress' : mode === 'register' || mode === 'verify' ? 'register' : 'forgot'}"]`).setAttribute('aria-current', 'page');
  const fields = { name: ['register'], reason: ['register'], email: ['forgot'], username: ['register', 'progress'], password: ['register', 'progress', 'reset'], confirm: ['register', 'reset'] };
  for (const [name, modes] of Object.entries(fields)) {
    const visible = modes.includes(mode);
    $(name + '-field').hidden = !visible;
    $('account-form').elements[name === 'name' ? 'display_name' : name].disabled = !visible;
  }
  $('footnote').hidden = mode !== 'register';
  const api = async (action, body) => {
    const response = await fetch('/api/account/' + action, { method: 'POST', credentials: 'omit',
      headers: { 'Content-Type': 'application/json', 'X-MotionFarm-Request': '1' }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
    return data;
  };
  const status = async () => {
    try {
      const response = await fetch('/api/account/status', { credentials: 'omit' });
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!data.email_enabled && ['forgot', 'verify', 'reset'].includes(mode)) {
        $('service-note').textContent = '找回密码请联系管理员，由管理员核实身份后重置密码。账号申请无需邮箱，请使用“申请账号”。';
        $('service-note').hidden = false; $('submit').disabled = true;
      }
    } catch {
      $('service-note').textContent = '暂时无法确认服务状态，提交失败时请稍后重试。'; $('service-note').hidden = false;
    }
  };
  $('account-form').addEventListener('submit', async event => {
    event.preventDefault(); $('error').textContent = '';
    const form = event.currentTarget;
    if (['register', 'reset'].includes(mode) && form.elements.password.value !== form.elements.confirm.value) {
      $('error').textContent = '两次输入的密码不一致'; form.elements.confirm.focus(); return;
    }
    $('submit').disabled = true;
    try {
      const data = await api(mode === 'progress' ? 'application-status' : mode, { display_name: form.elements.display_name.value.trim(), reason: form.elements.reason.value.trim(), email: form.elements.email.value.trim(), username: form.elements.username.value.trim(), password: form.elements.password.value, token });
      form.reset(); form.hidden = true; $('footnote').hidden = true;
      $('success-title').textContent = mode === 'register' ? '申请已提交' : mode === 'progress' ? ({pending:'等待审批', approved:'账号已开通', rejected:'申请未通过'}[data.status] || '申请状态') : mode === 'forgot' ? '请检查邮箱' : mode === 'verify' ? '账号已开通' : '密码已重置';
      $('success-detail').textContent = (data.message || '请返回 kinora App，使用用户名与密码登录。') + (data.decision_note ? ' 管理员说明：' + data.decision_note : '');
      $('again').href = '?view=' + (['register', 'progress'].includes(mode) ? 'progress' : 'forgot');
      $('again').textContent = ['register', 'progress'].includes(mode) ? '查询申请进度' : '返回';
      $('again').hidden = ['verify', 'reset'].includes(mode);
      $('success').hidden = false;
    } catch (error) { $('error').textContent = error.message || '暂时无法连接，请稍后重试'; }
    finally { $('submit').disabled = false; }
  });
  status();
})();
