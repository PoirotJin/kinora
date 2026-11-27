"""Email-free applications; approval and user creation share one transaction."""
from contextlib import closing
import hmac
import secrets
import time

from review_auth import PROTECTED, password_hash, validate_password, validate_username


class Applications:
    def __init__(self, auth):
        self.auth = auth
        with closing(auth.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS account_applications(
                username TEXT PRIMARY KEY, display_name TEXT NOT NULL, reason TEXT NOT NULL,
                salt TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, decided REAL, actor TEXT, decision_note TEXT NOT NULL DEFAULT '')''')

    def submit(self, value, peer):
        username = validate_username(value.get('username'))
        password = value.get('password')
        validate_password(password)
        name, reason = value.get('display_name'), value.get('reason')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('请填写姓名或同事能识别的称呼（最多 80 字）')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('请填写申请说明（最多 500 字）')
        self.auth.throttle('application:' + peer, 'application:' + username)
        salt = secrets.token_hex(16)
        digest = password_hash(password, salt)
        with closing(self.auth.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if username == PROTECTED or db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise ValueError('该用户名不可申请，请更换用户名或联系管理员')
            row = db.execute('SELECT salt,digest,status FROM account_applications WHERE username=?', (username,)).fetchone()
            if row:
                if not hmac.compare_digest(password_hash(password, row[0]), row[1]):
                    raise ValueError('该用户名已有申请，请更换用户名或使用原密码查询进度')
                return {'status': row[2], 'message': '已有申请，请在“申请进度”查看审批结果。'}
            if db.execute("SELECT count(*) FROM account_applications WHERE status='pending'").fetchone()[0] >= 1000:
                raise ValueError('待审批申请较多，请稍后重试或联系管理员')
            db.execute('INSERT INTO account_applications(username,display_name,reason,salt,digest,status,created) VALUES(?,?,?,?,?,?,?)',
                       (username, name.strip(), reason.strip(), salt, digest, 'pending', time.time()))
            self.auth.audit('application.submit', username, username, peer, db=db)
        return {'status': 'pending', 'message': '申请已提交，等待管理员审批。请保留用户名和密码，在“申请进度”查询；通过后即可返回客户端登录。'}

    def status(self, value, peer):
        username = validate_username(value.get('username'))
        password = value.get('password')
        validate_password(password)
        self.auth.throttle('application-status:' + peer, 'application-status:' + username)
        with closing(self.auth.connect()) as db:
            row = db.execute('SELECT salt,digest,status,decision_note FROM account_applications WHERE username=?', (username,)).fetchone()
        digest = password_hash(password, row[0] if row else '00' * 16)
        if not row or not hmac.compare_digest(digest, row[1]):
            raise ValueError('申请用户名或密码不正确')
        messages = {'pending': '申请正在等待管理员审批。', 'approved': '申请已通过，请返回客户端使用申请时设置的用户名和密码登录。',
                    'rejected': '申请未通过。请联系管理员确认；需要重新申请时请使用新的用户名。'}
        return {'status': row[2], 'message': messages[row[2]], 'decision_note': row[3]}

    def listing(self):
        with closing(self.auth.connect()) as db:
            rows = db.execute("SELECT username,display_name,reason,status,created,decision_note FROM account_applications ORDER BY (status='pending') DESC,created DESC LIMIT 1001").fetchall()
        keys = ('username', 'display_name', 'reason', 'status', 'created', 'decision_note')
        return {'applications': [dict(zip(keys, row)) for row in rows[:1000]], 'truncated': len(rows) > 1000}

    def decide(self, username, decision, note, actor, peer):
        validate_username(username)
        if decision not in ('approve', 'reject'): raise ValueError('无效审批操作')
        if not isinstance(note, str) or len(note) > 500: raise ValueError('审批说明最多 500 字')
        status = 'approved' if decision == 'approve' else 'rejected'
        with closing(self.auth.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            admin = db.execute('SELECT role,disabled,must_change FROM users WHERE username=?', (actor,)).fetchone()
            if not admin or admin != ('admin', 0, 0): raise PermissionError('仅管理员可审批申请')
            row = db.execute('SELECT salt,digest,status FROM account_applications WHERE username=?', (username,)).fetchone()
            if not row: raise ValueError('申请不存在')
            if row[2] == status: return {'status': status, 'username': username}
            if row[2] != 'pending': raise ValueError('该申请已处理，请刷新列表')
            if decision == 'approve':
                if username == PROTECTED or db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                    raise ValueError('该用户名已存在，不能覆盖；请拒绝申请并说明原因')
                db.execute("INSERT INTO users(username,salt,digest,must_change,role,disabled) VALUES(?,?,?,0,'user',0)", (username, row[0], row[1]))
            db.execute('UPDATE account_applications SET status=?,decided=?,actor=?,decision_note=? WHERE username=?',
                       (status, time.time(), actor, note.strip(), username))
            self.auth.audit('application.' + decision, actor, username, peer, db=db)
        return {'status': status, 'username': username}
