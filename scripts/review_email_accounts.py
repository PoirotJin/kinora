"""Email ownership and password recovery; no credentials or tokens in logs.

Uses the existing account database. SMTP settings belong in the service environment.
Tokens are delivered as URL fragments so they do not enter HTTP access logs.
"""
from contextlib import closing
from email.message import EmailMessage
import hashlib
import os
import re
import secrets
import smtplib
import ssl
import time
from urllib.parse import urlsplit

from review_auth import password_hash, validate_password, validate_username, PROTECTED


def normalize_email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise ValueError('请输入有效的电子邮箱')
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise ValueError('请输入有效的电子邮箱')
    return value


class MailDeliveryError(OSError):
    pass


class Mailer:
    def __init__(self):
        self.host = os.environ.get('MOTIONFARM_SMTP_HOST', '')
        self.sender = os.environ.get('MOTIONFARM_MAIL_FROM', '')
        self.origin = os.environ.get('MOTIONFARM_ACCOUNT_ORIGIN', '').rstrip('/')
        parsed = urlsplit(self.origin)
        if (not self.host or not self.sender or parsed.scheme != 'https' or
                not parsed.hostname or parsed.username or parsed.password or
                parsed.path or parsed.query or parsed.fragment):
            raise ValueError('邮箱服务尚未配置，请联系管理员')

    def __call__(self, recipient, purpose, token):
        reset = purpose == 'reset'
        link = self.origin + '/account#' + ('reset=' if reset else 'verify=') + token
        msg = EmailMessage()
        msg['From'] = self.sender
        msg['To'] = recipient
        msg['Subject'] = 'kinora · ' + ('重置密码' if reset else '验证电子邮箱')
        msg.set_content(('点击链接设置新密码：' if reset else '点击链接验证邮箱并完成账号申请：') +
                        '\n\n' + link + '\n\n链接在 30 分钟内有效，且只能使用一次。\n如果不是你发起的请求，请忽略此邮件。')
        implicit = os.environ.get('MOTIONFARM_SMTP_TLS', 'starttls') == 'implicit'
        port = int(os.environ.get('MOTIONFARM_SMTP_PORT', '465' if implicit else '587'))
        client = smtplib.SMTP_SSL if implicit else smtplib.SMTP
        options = {'context': ssl.create_default_context()} if implicit else {}
        with client(self.host, port, timeout=15, **options) as smtp:
            if not implicit:
                smtp.starttls(context=ssl.create_default_context())
            user = os.environ.get('MOTIONFARM_SMTP_USER', '')
            if user:
                smtp.login(user, os.environ.get('MOTIONFARM_SMTP_PASSWORD', ''))
            smtp.send_message(msg)


class EmailAccounts:
    def __init__(self, auth, send_mail, require_approval=False):
        self.auth = auth
        self.send_mail = send_mail
        self.require_approval = require_approval
        with closing(auth.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS account_emails(
                    username TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS email_tokens(
                    digest TEXT PRIMARY KEY, purpose TEXT NOT NULL,
                    email TEXT NOT NULL, username TEXT NOT NULL,
                    salt TEXT, password_digest TEXT, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS email_token_expiry ON email_tokens(expires);
            ''')

    def _issue(self, purpose, email, username, salt=None, digest=None):
        token = secrets.token_urlsafe(32)
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.auth.connect()) as db, db:
            db.execute('DELETE FROM email_tokens WHERE expires<?', (time.time(),))
            db.execute('DELETE FROM email_tokens WHERE purpose=? AND email=?', (purpose, email))
            db.execute('INSERT INTO email_tokens VALUES(?,?,?,?,?,?,?)',
                       (token_digest, purpose, email, username, salt, digest, time.time() + 1800))
        try:
            self.send_mail(email, purpose, token)
        except Exception:
            with closing(self.auth.connect()) as db, db:
                db.execute('DELETE FROM email_tokens WHERE digest=?', (token_digest,))
            raise MailDeliveryError('验证邮件暂时无法发送，请稍后重试') from None

    def register(self, email, username, password, peer):
        email = normalize_email(email)
        username = validate_username(username)
        validate_password(password)
        self.auth.throttle('email:' + peer, email)
        # Never permit registration of the protected administrator identity.
        if username == PROTECTED:
            raise ValueError('此用户名不可用')
        salt = secrets.token_hex(16)
        digest = password_hash(password, salt)
        with closing(self.auth.connect()) as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise ValueError('此用户名不可用')
            exists = db.execute('SELECT 1 FROM account_emails WHERE email=?', (email,)).fetchone()
        if not exists:
            self._issue('register', email, username, salt, digest)
        return {'ok': True, 'message': '如该邮箱可用于注册，验证邮件已发送。请检查收件箱。'}

    def request_reset(self, email, peer):
        email = normalize_email(email)
        self.auth.throttle('email:' + peer, email)
        with closing(self.auth.connect()) as db:
            row = db.execute('''SELECT e.username FROM account_emails e
                JOIN users u ON u.username=e.username WHERE e.email=? AND u.disabled=0''', (email,)).fetchone()
        if row:
            self._issue('reset', email, row[0])
        return {'ok': True, 'message': '如果该邮箱对应可用账号，重置邮件已发送。请检查收件箱。'}

    def consume(self, token, purpose, peer, new_password=None):
        if purpose not in ('register', 'reset') or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise ValueError('链接无效或已过期，请重新申请')
        self.auth.throttle('token:' + peer, hashlib.sha256(token.encode()).hexdigest())
        if purpose == 'reset':
            validate_password(new_password)
            salt = secrets.token_hex(16)
            digest = password_hash(new_password, salt)
        with closing(self.auth.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT email,username,salt,password_digest FROM email_tokens
                WHERE digest=? AND purpose=? AND expires>?''',
                (hashlib.sha256(token.encode()).hexdigest(), purpose, time.time())).fetchone()
            if not row:
                raise ValueError('链接无效或已过期，请重新申请')
            email, username, saved_salt, saved_digest = row
            if purpose == 'register':
                if (db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone() or
                        db.execute('SELECT 1 FROM account_emails WHERE email=?', (email,)).fetchone()):
                    raise ValueError('账号或邮箱已被使用，请重新申请')
                db.execute("INSERT INTO users(username,salt,digest,must_change,role,disabled) VALUES(?,?,?,0,'user',?)",
                           (username, saved_salt, saved_digest, int(self.require_approval)))
                db.execute('INSERT INTO account_emails VALUES(?,?)', (username, email))
            else:
                user = db.execute('''SELECT u.disabled FROM users u JOIN account_emails e
                    ON e.username=u.username WHERE u.username=? AND e.email=?''', (username, email)).fetchone()
                if not user or user[0]:
                    raise ValueError('链接无效或账号不可用')
                db.execute('UPDATE users SET salt=?,digest=?,must_change=0 WHERE username=?', (salt, digest, username))
                db.execute('DELETE FROM sessions WHERE username=?', (username,))
            db.execute('DELETE FROM email_tokens WHERE email=? OR username=?', (email, username))
            self.auth.audit('email.' + purpose, username, username, peer, db=db)
        return {'ok': True, 'pending_approval': purpose == 'register' and self.require_approval}
