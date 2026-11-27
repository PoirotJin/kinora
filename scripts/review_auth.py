"""Local account management and revocable sessions for kinora Review."""
import argparse
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

TTL = 8 * 60 * 60
HASH_LOCK = threading.BoundedSemaphore(1)
PROTECTED = 'jfc'


class AccountBlocked(Exception):
    pass


def password_hash(password, salt):
    if not HASH_LOCK.acquire(timeout=0.05):
        raise PermissionError('登录繁忙，请稍后重试')
    try:
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    finally:
        HASH_LOCK.release()


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('密码需要 12–256 个字符')


def validate_username(username):
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', username):
        raise ValueError('用户名应为 1–40 位字母、数字、点、下划线或短横线')
    return username


def revoke_password_reset_tokens(db, username):
    # Older installations may never have enabled email recovery.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_tokens'").fetchone():
        db.execute("DELETE FROM email_tokens WHERE username=? AND purpose='reset'", (username,))


class Auth:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'accounts.sqlite3'
        try:fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:pass
        else:os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT NOT NULL,
                    digest TEXT NOT NULL, must_change INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS account_emails(username TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, username TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS session_user ON sessions(username);
                CREATE TABLE IF NOT EXISTS attempts(bucket TEXT PRIMARY KEY, started REAL, count INTEGER);
                CREATE TABLE IF NOT EXISTS security_events(id INTEGER PRIMARY KEY,ts REAL NOT NULL,
                    action TEXT NOT NULL,actor TEXT,target TEXT,peer TEXT,outcome TEXT);
                CREATE INDEX IF NOT EXISTS security_time ON security_events(ts);
            ''')
            # Hold the same write transaction from inspecting the schema through migration.
            db.execute('BEGIN IMMEDIATE')
            cols={r[1] for r in db.execute('PRAGMA table_info(users)')}
            if 'role' not in cols:
                db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
                db.execute("UPDATE users SET role='admin' WHERE username=?", (PROTECTED,))
            if 'disabled' not in cols:
                db.execute('ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')
            sess={r[1] for r in db.execute('PRAGMA table_info(sessions)')}
            if 'created' not in sess:
                db.execute('ALTER TABLE sessions ADD COLUMN created REAL NOT NULL DEFAULT 0')
            # jfc remains admin for as long as this service runs; demotion requires editing the server.
            db.execute("UPDATE users SET role='admin', disabled=0 WHERE username=?", (PROTECTED,))
            db.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def audit(self,action,actor='',target='',peer='',outcome='ok',db=None):
        """Only bounded identifiers/outcome; never persist credentials or requests."""
        values=(time.time(),str(action)[:48],str(actor)[:80],str(target)[:80],str(peer)[:80],str(outcome)[:48])
        if db is not None:db.execute('INSERT INTO security_events(ts,action,actor,target,peer,outcome) VALUES(?,?,?,?,?,?)',values)
        else:
            with closing(self.connect()) as con,con:
                con.execute('INSERT INTO security_events(ts,action,actor,target,peer,outcome) VALUES(?,?,?,?,?,?)',values)

    def create(self, username, password, role='user', actor='system', peer=''):
        if role not in ('admin','user'):raise ValueError('无效用户组')
        username=validate_username(username)
        if username==PROTECTED: role='admin'
        validate_password(password)
        salt = secrets.token_hex(16)
        digest = password_hash(password, salt)
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO users(username,salt,digest,must_change,role,disabled) VALUES(?,?,?,1,?,0)',
                       (username, salt, digest, role))
            self.audit('account.create',actor,username,peer,db=db)

    def users(self):
        now=time.time()
        with closing(self.connect()) as db:
            rows=db.execute('''SELECT username,role,must_change,disabled,
                (SELECT count(*) FROM sessions s WHERE s.username=u.username AND s.expires>?)
                FROM users u ORDER BY username LIMIT 200''',(now,)).fetchall()
        return [dict(username=r[0],role=r[1],must_change=bool(r[2]),disabled=bool(r[3]),sessions=r[4]) for r in rows]

    def throttle(self, peer, username):
        now = time.time()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE started<?', (now - 600,))
            # All attempts are counted, including unknown accounts and successful logins.
            for bucket, limit in [('ip:' + peer, 20), ('user:' + username, 12)]:
                row = db.execute('SELECT count FROM attempts WHERE bucket=?', (bucket,)).fetchone()
                if row and row[0] >= limit:
                    raise PermissionError('登录尝试过于频繁，请 10 分钟后重试')
            for bucket in ['ip:' + peer, 'user:' + username]:
                db.execute('INSERT INTO attempts VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (bucket, now))

    def issue(self, db, username):
        token = secrets.token_urlsafe(32)
        now=time.time()
        db.execute('DELETE FROM sessions WHERE expires<?', (now,))
        db.execute('INSERT INTO sessions(digest,username,expires,created) VALUES(?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), username, now + TTL, now))
        return token

    def login(self, username, password, peer):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 254 or len(password) > 256:
            raise ValueError('用户名或密码格式不正确')
        if '@' in username:
            with closing(self.connect()) as db:
                email_user = db.execute('SELECT username FROM account_emails WHERE email=?', (username.strip().lower(),)).fetchone()
            if email_user: username = email_user[0]
        try:self.throttle(peer, username)
        except PermissionError:
            self.audit('login',username,username,peer,'throttled')
            raise
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT salt,digest,must_change,role,disabled FROM users WHERE username=?', (username,)).fetchone()
            digest = password_hash(password, row[0] if row else '00' * 16)
            if not row or not hmac.compare_digest(digest, row[1]):
                self.audit('login',username,username,peer,'invalid_credentials',db=db)
                return None
            if row[4]:
                self.audit('login',username,username,peer,'disabled',db=db)
                db.commit()
                raise AccountBlocked('该账户已停用，请联系管理员')
            self.audit('login',username,username,peer,db=db)
            return self.issue(db, username), dict(username=username, must_change=bool(row[2]),role=row[3],disabled=False)

    def session(self, token):
        if not isinstance(token, str) or not 30 <= len(token) <= 100:
            return None
        with closing(self.connect()) as db:
            row = db.execute('''SELECT u.username,u.must_change,u.role,u.disabled FROM sessions s JOIN users u ON u.username=s.username
                WHERE s.digest=? AND s.expires>?''', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row or row[3]:
            return None
        return dict(username=row[0], must_change=bool(row[1]),role=row[2],disabled=False)

    def logout(self, token, peer=''):
        with closing(self.connect()) as db, db:
            row=db.execute('SELECT username FROM sessions WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))
            if row:self.audit('logout',row[0],row[0],peer,db=db)

    def kick(self, username, actor='system', peer=''):
        username=validate_username(username)
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
            self.audit('session.revoke',actor,username,peer,db=db)
        return {'ok':True,'username':username}

    def set_disabled(self, username, disabled, actor='system', peer=''):
        username=validate_username(username)
        if username==PROTECTED:
            raise ValueError('不能停用受保护的管理员账户')
        with closing(self.connect()) as db, db:
            row=db.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone()
            if not row: raise ValueError('用户不存在')
            db.execute('UPDATE users SET disabled=? WHERE username=?',(1 if disabled else 0, username))
            if disabled:
                db.execute('DELETE FROM sessions WHERE username=?',(username,))
            self.audit('account.disable' if disabled else 'account.enable',actor,username,peer,db=db)
        if disabled:
            from review_access import cancel_owner_jobs
            cancel_owner_jobs(self.path.parent.parent,username)
        return {'ok':True,'username':username,'disabled':bool(disabled)}

    def reset_password(self, username, actor='system', peer=''):
        username=validate_username(username)
        password=secrets.token_urlsafe(20)
        validate_password(password)
        salt=secrets.token_hex(16)
        digest=password_hash(password, salt)
        with closing(self.connect()) as db, db:
            row=db.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone()
            if not row: raise ValueError('用户不存在')
            db.execute('UPDATE users SET salt=?,digest=?,must_change=1,disabled=0 WHERE username=?',
                       (salt, digest, username))
            db.execute('DELETE FROM sessions WHERE username=?',(username,))
            revoke_password_reset_tokens(db, username)
            self.audit('password.reset',actor,username,peer,db=db)
        return password

    def change_password(self, username, old_password, new_password, peer=''):
        validate_password(new_password)
        if not isinstance(old_password, str) or len(old_password) > 256:
            raise ValueError('原密码格式不正确')
        if old_password == new_password:
            raise ValueError('新密码不能与原密码相同')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT salt,digest FROM users WHERE username=?', (username,)).fetchone()
            if not row or not hmac.compare_digest(password_hash(old_password, row[0]), row[1]):
                raise ValueError('原密码不正确')
            salt = secrets.token_hex(16)
            db.execute('UPDATE users SET salt=?,digest=?,must_change=0 WHERE username=?', (salt, password_hash(new_password, salt), username))
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
            revoke_password_reset_tokens(db, username)
            self.audit('password.change',username,username,peer,db=db)
            return self.issue(db, username)


def main():
    parser = argparse.ArgumentParser(description='创建 kinora 本地账户；不会覆盖已有账户')
    parser.add_argument('username')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1] / 'outputs/text_motion_farm/review')
    parser.add_argument('--credentials-file', type=Path, required=True)
    parser.add_argument('--role',choices=['admin','user'],default='user')
    args = parser.parse_args()
    password = secrets.token_urlsafe(20)
    # Reserve the delivery file first; never overwrite an existing secret file.
    fd = os.open(args.credentials_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        Auth(args.root).create(args.username, password,args.role)
        with os.fdopen(fd, 'w') as stream:
            stream.write('kinora 登录账户\n用户名：' + args.username + '\n临时密码：' + password + '\n首次登录必须修改密码，修改后临时密码失效。\n')
    except BaseException:
        try: os.close(fd)
        except OSError: pass
        args.credentials_file.unlink(missing_ok=True)
        raise
    print(json.dumps({'created': args.username, 'credentials_file': str(args.credentials_file)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
