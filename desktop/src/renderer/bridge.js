'use strict';
// Tauri bridge: provides the window.desktop API the renderer is written against.
// All cloud requests go to the Rust side, which enforces the endpoint allow-list.
(() => {
  const T = window.__TAURI__;
  if (!T || window.desktop) return;
  const invoke = T.core.invoke;
  let server = '';
  const rememberServer = status => { server = status.server || ''; return status; };
  const BINARY = /^\/api\/motions\/[^/]+\/preview\.bin$|^\/api\/retarget\/mesh-data$/;
  const failed = error => (error && typeof error === 'object' && 'ok' in error) ? error : { ok: false, status: 0, error: '操作失败，请重试' };
  window.desktop = {
    loadCredentials: () => invoke('load_credentials', { server }),
    saveCredentials: credentials => invoke('save_credentials', { server, credentials }),
    forgetCredentials: () => invoke('forget_credentials', { server }),
    status: () => invoke('status').then(rememberServer),
    listInstallations: () => invoke('list_installations'),
    revealInstallation: path => invoke('reveal_installation', { path }),
    configureServer: address => invoke('configure_server', { address }),
    cloud: async request => {
      request = { ...request, server };
      try {
        if (!BINARY.test(request.path)) return await invoke('cloud', { request });
        const data = await invoke('cloud_binary', { request });
        return { ok: true, status: 200, buffer: data instanceof ArrayBuffer ? data : new Uint8Array(data).buffer };
      } catch (error) { return failed(error); }
    },
    download: request => invoke('download', { request: { ...request, server } }).catch(() => ({ error: '下载失败，请重试' })),
    openWorkbench: () => invoke('open_workbench'),
    openAccount: mode => invoke('open_account', { mode }),
    clearSession: () => invoke('clear_session'),
    acknowledgeRelease: version => invoke('acknowledge_release', { version }),
    checkUpdate: () => invoke('check_update'),
    downloadUpdate: () => invoke('download_update'),
    installUpdate: () => invoke('install_update'),
    onStatus: callback => {
      let off = null;
      T.event.listen('status', event => callback(event.payload)).then(unlisten => { off = unlisten; });
      return () => off?.();
    }
  };
})();
