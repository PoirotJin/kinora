"""Read-only, bounded administrator security-event listing."""
from contextlib import closing
from review_access import Forbidden


def audit_events(auth, user, query):
    if user.get('role') != 'admin': raise Forbidden('仅管理员可查看审计记录')
    value = query.get('before', [''])[0]
    if value and (not value.isascii() or not value.isdigit() or len(value) > 18):
        raise ValueError('无效审计分页参数')
    before = int(value) if value else None
    actor = query.get('actor', [''])[0].strip()
    if len(actor) > 80: raise ValueError('账号筛选最多 80 字符')
    clauses, args = [], []
    if before is not None: clauses.append('id < ?'); args.append(before)
    if actor: clauses.append('actor = ?'); args.append(actor)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with closing(auth.connect()) as db:
        rows = db.execute('SELECT id,ts,action,actor,target,peer,outcome FROM security_events' + where + ' ORDER BY id DESC LIMIT 51', args).fetchall()
    keys = ('id', 'ts', 'action', 'actor', 'target', 'peer', 'outcome')
    return {'items': [dict(zip(keys, row)) for row in rows[:50]], 'next_before': rows[49][0] if len(rows) > 50 else None}
