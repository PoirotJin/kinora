"""Anonymous installer landing page; only published manifest entries are linked."""
from html import escape
import re
from urllib.parse import urlsplit


CSS = '\n:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#272720;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1000px;margin:64px auto;padding:0 28px}header{border-bottom:1px solid #c8c5ba;padding-bottom:28px}.brand{font:44px Georgia,serif;letter-spacing:-2px}h1{font-size:28px;font-weight:500;margin:28px 0 8px}h2{font-size:21px;font-weight:500}p{color:#625f55}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin:32px 0}article{border-top:1px solid #aaa79c;padding:10px 0 20px}.download{display:block;color:inherit;border:1px solid #605d52;padding:10px 12px;text-decoration:none;margin:24px 0 16px}a:hover{background:#e9e6db}a:focus-visible,summary:focus-visible{outline:2px solid #272720;outline-offset:4px}summary{cursor:pointer;font-size:13px}code{display:block;overflow-wrap:anywhere;font-size:12px;margin-top:10px}section{border-top:1px solid #c8c5ba;padding-top:24px;margin-top:24px}li{margin:10px 0}footer{margin-top:36px;color:#625f55;font-size:13px}@media(max-width:700px){main{margin:28px auto}.grid{grid-template-columns:1fr;gap:8px}}\n'

def render(manifest):
    cards = []
    labels = {'macos-universal': ('macOS', 'Apple silicon / Intel · macOS 11+'),
              'windows-x86_64': ('Windows', 'Windows x86_64'),
              'ubuntu-x86_64': ('Ubuntu / Debian', 'Ubuntu 22.04+ / Debian 12+ · x86_64')}
    for platform, (title, detail) in labels.items():
        item = manifest.get('downloads', {}).get(platform)
        if not item:
            continue
        path = urlsplit(item['url']).path
        if not re.fullmatch(r'/desktop/releases/[A-Za-z0-9_.-]{1,180}', path):
            continue
        digest = item.get('sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            continue
        cards.append(f'<article><h2>{title}</h2><p>{detail}</p><a class="download" href="{escape(path)}" download>下载 {title} 安装包 ↗</a><details><summary>SHA-256 校验值</summary><code>{digest}</code></details></article>')
    version = escape(str(manifest['version']))
    notes = ''.join('<li>'+escape(line)+'</li>' for line in manifest.get('notes', '').splitlines() if line.strip())
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kinora · 下载与更新</title><link rel="stylesheet" href="/desktop/download.css"><main><header><div class="brand">kinora</div><h1>下载 kinora '''+version+'''</h1><p>动作生成、机器人重定向与实时精修工作台。</p></header><div class="grid">'''+(''.join(cards) or '<p>此版本安装包尚未公开，请稍后再试。</p>')+'''</div><section><h2>已有用户升级</h2><p>打开 kinora → 设置 → 检查更新 → 下载更新 → 重启安装。也可下载对应系统的安装包覆盖安装，保留现有账号配置。</p></section><section><h2>本次更新</h2><ul>'''+notes+'''</ul></section><footer>macOS 包采用 ad-hoc 签名，尚未 Apple 公证；Windows 安装包尚未代码签名。应用内更新包使用独立签名验证。Linux .deb 可使用系统软件安装器安装。请核对下载版本及校验值。</footer></main></html>'''
