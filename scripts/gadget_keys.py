"""Owner-bound, expiring API keys. Only token digests are stored."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from review_access import private_file,Forbidden

SCOPES={'generate','retarget','read'}

class Keys:
    def __init__(self,root):
        self.root=Path(root);self.path=private_file(self.root/'review/gadget/keys.sqlite3')
        with closing(self.db()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS keys(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,
              scopes TEXT NOT NULL,created REAL,expires REAL,revoked INTEGER DEFAULT 0,last_used REAL DEFAULT 0,account_salt TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS keys_owner ON keys(owner);
              CREATE TABLE IF NOT EXISTS rates(id TEXT PRIMARY KEY,minute INTEGER,count INTEGER);
              CREATE TABLE IF NOT EXISTS journal_server(singleton INTEGER PRIMARY KEY CHECK(singleton=1),subject TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS journal_accounts(owner TEXT PRIMARY KEY,created_event INTEGER NOT NULL,subject TEXT NOT NULL);''')
    def db(self):
        db=sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row;return db
    def list(self,owner):
        with closing(sqlite3.connect((self.root/'review/accounts.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as accounts:
            account=accounts.execute('SELECT salt,disabled,must_change FROM users WHERE username=?',(owner,)).fetchone()
        now=time.time()
        with closing(self.db()) as db:
            rows=[dict(r) for r in db.execute('SELECT id,name,scopes,created,expires,revoked,last_used,account_salt FROM keys WHERE owner=? ORDER BY created DESC LIMIT 100',(owner,))]
        for row in rows:
            salt=row.pop('account_salt')
            status=('revoked' if row['revoked'] else 'expired' if row['expires']<=now else
                    'account_unavailable' if not account or account[1] else 'password_changed' if salt!=account[0] else
                    'password_change_required' if account[2] else 'active')
            row.update(status=status,active=status=='active')
        return rows
    def create(self,owner,value):
        name=value.get('name','Local agent');scopes=value.get('scopes',['read','generate','retarget']);days=value.get('days',30)
        if not isinstance(name,str) or not 1<=len(name)<=80:raise ValueError('名称需要1–80个字符')
        if not isinstance(scopes,list) or not scopes or any(not isinstance(s,str) or s not in SCOPES for s in scopes):raise ValueError('无效权限范围')
        if type(days)!=int or not 1<=days<=90:raise ValueError('有效期需要1–90天')
        raw='mfg_'+secrets.token_urlsafe(32);key=secrets.token_hex(12);now=time.time()
        with closing(sqlite3.connect((self.root/'review/accounts.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as accounts:
            account=accounts.execute('SELECT salt,disabled,must_change FROM users WHERE username=?',(owner,)).fetchone()
        if not account or account[1] or account[2]:raise Forbidden('账户不可用')
        with closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM keys WHERE owner=? AND revoked=0 AND expires>? AND account_salt=?',(owner,now,account[0])).fetchone()[0]>=10:raise ValueError('最多10个有效密钥')
            db.execute('DELETE FROM keys WHERE owner=? AND (revoked=1 OR expires<=?)',(owner,now))
            db.execute('INSERT INTO keys(id,owner,name,digest,scopes,created,expires,account_salt) VALUES(?,?,?,?,?,?,?,?)',
                (key,owner,name,hashlib.sha256(raw.encode()).hexdigest(),json.dumps(sorted(set(scopes))),now,now+days*86400,account[0]))
        return dict(id=key,key=raw,expires=now+days*86400,scopes=scopes)
    def revoke(self,owner,key):
        with closing(self.db()) as db,db:
            db.execute('UPDATE keys SET revoked=1 WHERE id=? AND owner=?',(key,owner))
        return {'ok':True}

    def identity(self,owner):
        """Stable non-secret subjects, only after the caller authenticates its key.

        Password/key changes do not change an account subject. A deleted and
        recreated username has a new account.create event and a new subject.
        """
        with closing(sqlite3.connect((self.root/'review/accounts.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as accounts:
            row=accounts.execute('SELECT disabled,must_change FROM users WHERE username=?',(owner,)).fetchone()
            if not row or row[0] or row[1]:raise Forbidden('账户不可用')
            event=accounts.execute("SELECT COALESCE(MAX(id),0) FROM security_events WHERE action='account.create' AND target=?",(owner,)).fetchone()[0]
        with closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO journal_server VALUES(1,?)',(secrets.token_hex(16),))
            row=db.execute('SELECT created_event,subject FROM journal_accounts WHERE owner=?',(owner,)).fetchone()
            if not row or row['created_event']!=event:
                db.execute('INSERT OR REPLACE INTO journal_accounts VALUES(?,?,?)',(owner,event,secrets.token_hex(16)))
            subject=db.execute('SELECT subject FROM journal_accounts WHERE owner=?',(owner,)).fetchone()[0]
            server=db.execute('SELECT subject FROM journal_server WHERE singleton=1').fetchone()[0]
        return dict(schema_version='gadget-account-v1',server_id=server,account_id=subject,username=owner)

    def authenticate(self,raw,scope):
        if not isinstance(raw,str) or not raw.startswith('mfg_') or len(raw)>100:raise Forbidden('无效Gadget API Key')
        now=time.time()
        with closing(self.db()) as db:
            row=db.execute('SELECT * FROM keys WHERE digest=? AND revoked=0 AND expires>?',(hashlib.sha256(raw.encode()).hexdigest(),now)).fetchone()
        if row is None or (scope is not None and scope not in json.loads(row['scopes'])):raise Forbidden('密钥无效、已过期或权限不足')
        accounts=self.root/'review/accounts.sqlite3'
        with closing(sqlite3.connect(accounts.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            user=db.execute('SELECT role,disabled,must_change,salt FROM users WHERE username=?',(row['owner'],)).fetchone()
        if not user or user[1] or user[2] or user[3]!=row['account_salt']:raise Forbidden('账户不可用、密码已变更或需要先修改临时密码')
        with closing(self.db()) as db,db:
            db.execute('BEGIN IMMEDIATE');minute=int(now//60)
            prior=db.execute('SELECT minute,count FROM rates WHERE id=?',(row['id'],)).fetchone()
            count=prior['count']+1 if prior and prior['minute']==minute else 1
            if count>120:raise PermissionError('Gadget每分钟最多120次请求，请稍后重试')
            db.execute('INSERT OR REPLACE INTO rates VALUES(?,?,?)',(row['id'],minute,count))
            db.execute('UPDATE keys SET last_used=? WHERE id=?',(now,row['id']))
        # Even an administrator's API key is restricted to owner-scoped workflows.
        return dict(username=row['owner'],role='user',must_change=False)
