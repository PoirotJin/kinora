const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/../src/renderer/app.js', 'utf8');
const buttons = ['register', 'forgot', 'register', 'forgot'].map(mode => ({
  dataset: { account: mode }, disabled: false,
  addEventListener(event, handler) { this.click = handler; },
}));
const messages = [];
let open;
vm.runInNewContext(source.slice(source.indexOf('for (const button of document.querySelectorAll(\'[data-account]\'))'), source.indexOf("for (const [id, action]")), {
  document: { querySelectorAll: () => buttons },
  window: { desktop: { openAccount: mode => open(mode) } },
  toast: message => messages.push(message),
});
(async () => {
  for (const button of buttons) {
    let finish, calls = 0;
    open = mode => {
      assert.equal(mode, button.dataset.account);
      calls++;
      return new Promise(resolve => { finish = resolve; });
    };
    const pending = button.click();
    assert.equal(button.disabled, true);
    await button.click();
    assert.equal(calls, 1, 'Repeated clicks must not open extra browser tabs');
    finish(); await pending;
    assert.equal(button.disabled, false);
    assert.match(messages.at(-1), /浏览器/);
    for (const error of ['请先配置服务器地址', new Error('无法启动浏览器'), null]) {
      open = async () => { throw error; };
      await button.click();
      assert.equal(button.disabled, false, 'Failure must allow retry');
      assert.equal(messages.at(-1), typeof error === 'string' ? error : error?.message || '无法启动浏览器，请检查系统默认浏览器设置');
    }
  }
  console.log('Account entry: register/reset, duplicate clicks, errors and retry passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
