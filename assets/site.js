// Kinora homepage: theme toggle, language memory, platform highlight and latest-release links.
(() => {
  const root = document.documentElement, en = root.lang === 'en';
  const T = en ? {
    download: os => 'Download for ' + os, meta: v => 'Desktop ' + v + ' · prebuilt, ready to install',
    source: tag => 'Desktop release: ' + tag + '. Server source and Gadget are versioned independently.'
  } : {
    download: os => '下载 ' + os + ' 版', meta: v => '桌面端 ' + v + ' · 已编译，下载即可安装',
    source: tag => '桌面端发布：' + tag + '；服务端源码与 Gadget 各自独立版本。'
  };

  // Theme: follow the system; the button pins the opposite choice for this visitor.
  const themeKey = 'kinora-theme';
  try { const saved = localStorage.getItem(themeKey); if (saved) root.dataset.theme = saved; } catch (_) {}
  document.getElementById('theme')?.addEventListener('click', () => {
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem(themeKey, root.dataset.theme); } catch (_) {}
  });

  // Remember an explicit language choice so the Chinese page stops suggesting English.
  for (const a of document.querySelectorAll('.lang a[hreflang]')) a.addEventListener('click', () => {
    try { localStorage.setItem('kinora-lang', a.hreflang); } catch (_) {}
  });

  // Highlight the visitor's platform and point the hero button at its installer.
  const ua = navigator.userAgent;
  const os = /Mac/.test(ua) && !/iPhone|iPad/.test(ua) ? 'mac' : /Windows/.test(ua) ? 'win' : /Linux|X11/.test(ua) && !/Android/.test(ua) ? 'linux' : '';
  const label = { mac: 'macOS', win: 'Windows', linux: 'Linux' };
  const pick = () => {
    if (!os) return;
    const row = document.querySelector(`tr[data-os="${os}"]`); if (!row) return;
    row.classList.add('you');
    const link = row.querySelector('a[data-asset]'), hero = document.getElementById('hero-download');
    if (hero && link) { hero.href = link.href; hero.textContent = T.download(label[os]); }
  };
  pick();

  // Use the newest GitHub release when reachable; the static links in the page stay as fallback.
  const patterns = { dmg: /_universal\.dmg$/, exe: /_x64-setup\.exe$/, deb: /_amd64\.deb$/, server: /^kinora-server-\d+\.\d+\.\d+\.zip$/, gadget: /^kinora-gadget-.*\.zip$/, sums: /^SHA256SUMS\.txt$/ };
  const versionOf = {
    desktop: n => (n.match(/^kinora_(\d+\.\d+\.\d+)_/) || [])[1],
    server: n => (n.match(/^kinora-server-(\d+\.\d+\.\d+)\.zip$/) || [])[1],
    gadget: n => (n.match(/gadget-(\d+\.\d+\.\d+)\.zip$/) || [])[1]
  };
  fetch('https://api.github.com/repos/PoirotJin/kinora/releases/latest', { headers: { Accept: 'application/vnd.github+json' } })
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(release => {
      const assets = release.assets || [];
      for (const [kind, re] of Object.entries(patterns)) {
        const asset = assets.find(a => re.test(a.name)); if (!asset) continue;
        const group = kind === 'server' || kind === 'gadget' ? kind : ['dmg', 'exe', 'deb'].includes(kind) ? 'desktop' : null;
        const v = group && versionOf[group](asset.name);
        const current = group && document.querySelector(`[data-ver="${group}"]`)?.textContent;
        // A server-only correction can be newer than the latest desktop release.
        // Never replace its curated download with an older carried-over asset.
        if (v && current) {
          const old = current.trim().split('.').map(Number);
          const comparison = v.split('.').map(Number).reduce((value, n, i) => value || n - old[i], 0);
          if (comparison < 0) continue;
        }
        for (const a of document.querySelectorAll(`a[data-asset="${kind}"]`)) a.href = asset.browser_download_url;
        if (v) for (const cell of document.querySelectorAll(`[data-ver="${group}"]`)) cell.textContent = v;
      }
      const desktop = document.querySelector('[data-ver="desktop"]')?.textContent;
      if (desktop) document.getElementById('hero-meta').textContent = T.meta(desktop);
      document.getElementById('release-src').textContent = T.source(release.tag_name);
      pick();
    })
    .catch(() => {});
})();
