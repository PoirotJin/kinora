"""Public account pages/API and signed desktop update feed.

Called before the session gate; each account write enforces the same origin and
JSON-header rules as the authenticated API. No SMTP config means fail closed.
"""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from review_email_accounts import EmailAccounts, Mailer, MailDeliveryError

ASSETS = {
    '/account': ('index.html', 'text/html; charset=utf-8'),
    '/account/': ('index.html', 'text/html; charset=utf-8'),
    '/account/account.css': ('account.css', 'text/css; charset=utf-8'),
    '/account/account.js': ('account.js', 'text/javascript; charset=utf-8'),
    '/account/fonts/eb-garamond-latin-500-normal.woff2': ('fonts/eb-garamond-latin-500-normal.woff2', 'font/woff2'),
}


def handle_public(handler, path, write):
    if path == '/desktop/download.css':
        from desktop_download_page import CSS
        handler.send(405, {'error': '仅支持读取'}) if write else handler.send(200, CSS.encode('utf-8'), 'text/css; charset=utf-8')
        return True
    if path in ASSETS:
        if write:
            handler.send(405, {'error': '仅支持读取'})
        else:
            name, mime = ASSETS[path]
            handler.send(200, (handler.server.web.parent / 'account' / name).read_bytes(), mime)
        return True
    if path in ('/desktop', '/desktop/') or path.startswith('/api/desktop/update/') or path.startswith('/desktop/releases/'):
        if write:
            handler.send(405, {'error': '仅支持读取'})
            return True
        root = Path(os.environ.get('MOTIONFARM_DESKTOP_RELEASES', str(handler.server.web.parent.parent / 'var/desktop-releases')))
        manifest_path = root / 'latest.json'
        if not manifest_path.is_file():
            # Not a claim of being up to date: a release has not been published.
            handler.send(503, {'error': '更新包尚未发布'})
            return True
        manifest = json.loads(manifest_path.read_text())
        if path in ('/desktop', '/desktop/'):
            from desktop_download_page import render
            handler.send(200, render(manifest).encode('utf-8'), 'text/html; charset=utf-8')
            return True
        match = re.fullmatch(r'/api/desktop/update/(darwin|windows|linux)/(aarch64|x86_64|i686|armv7)/[0-9A-Za-z.+-]{1,64}', path)
        if match:
            package = manifest.get('platforms', {}).get(match[1] + '-' + match[2])
            if not package:
                handler.send(503, {'error': '此平台的更新包尚未发布'})
            else:
                base = match[1] + '-' + match[2]
                variants = {k: v for k, v in manifest.get('platforms', {}).items()
                            if k == base or k in (base + '-deb', base + '-appimage')}
                # Tauri selects {os}-{arch}-{installer} before {os}-{arch}.
                # A dynamic single-URL response cannot distinguish DEB from AppImage.
                payload = {'platforms': variants} if match[1] == 'linux' and base + '-deb' in variants else {k: package[k] for k in ('url', 'signature')}
                handler.send(200, dict(version=manifest['version'], notes=manifest.get('notes', ''), **payload))
            return True
        # Only serve artifacts explicitly referenced by the published manifest.
        filenames = {urlsplit(p['url']).path for p in [*manifest.get('platforms', {}).values(), *manifest.get('downloads', {}).values()]}
        name = path.removeprefix('/desktop/releases/')
        if path in filenames and re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', name):
            artifact = root / name
            if artifact.is_file() and not artifact.is_symlink():
                handler.download(artifact)
                return True
        handler.send(404, {'error': 'Not found'})
        return True
    prefix = '/api/account/'
    if not path.startswith(prefix):
        return False
    action = path[len(prefix):]
    if not handler.origin_allowed(required=write) or (write and (
            handler.headers.get('X-MotionFarm-Request') != '1' or
            handler.headers.get('Content-Type', '').split(';')[0] != 'application/json')):
        handler.send(403, {'error': '请求验证失败，请刷新页面'})
        return True
    try:
        mailer = Mailer()
        if mailer.origin not in handler.server.origins:
            raise ValueError('邮件链接地址必须与服务地址一致')
        ready = True
    except ValueError:
        ready = False
    if action == 'status' and not write:
        handler.send(200, {'email_enabled': ready, 'registration_mode': 'approval'})
        return True
    if action in ('register', 'application-status') and write:
        from review_account_applications import Applications
        applications = Applications(handler.server.auth)
        result = applications.submit(handler.body(), handler.peer()) if action == 'register' else applications.status(handler.body(), handler.peer())
        handler.send(200, result)
        return True
    if action not in ('register', 'forgot', 'verify', 'reset') or not write:
        handler.send(404, {'error': 'Not found'})
        return True
    if not ready:
        handler.send(503, {'error': '邮箱服务尚未启用，请联系管理员'})
        return True
    accounts = EmailAccounts(handler.server.auth, mailer)
    value = handler.body()
    peer = handler.peer()
    try:
        if action == 'register':
            result = accounts.register(value.get('email'), value.get('username'), value.get('password'), peer)
        elif action == 'forgot':
            result = accounts.request_reset(value.get('email'), peer)
        elif action == 'verify':
            result = accounts.consume(value.get('token'), 'register', peer)
        else:
            result = accounts.consume(value.get('token'), 'reset', peer, value.get('password'))
    except MailDeliveryError:
        handler.send(503, {'error': '邮件发送暂时不可用，请稍后重试或联系管理员'})
        return True
    handler.send(200, result)
    return True
