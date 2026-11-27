"""Bounded, owner-scoped job history; current queue ordering stays unchanged."""
import json
import math


def job_page(db, table, user, active, query=None):
    if table not in ('requests', 'jobs'):
        raise ValueError('无效作业类型')
    query = query or {}
    history = query.get('history', ['0'])[0] == '1'
    where, params = [], []
    if user and user.get('role') != 'admin':
        where.append('owner=?')
        params.append(user['username'])
    cursor = query.get('after', [''])[0]
    if cursor:
        try:
            created, key = json.loads(cursor)
            if not history or type(created) not in (int, float) or not math.isfinite(created) or not isinstance(key, str) or not 1 <= len(key) <= 200:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError('无效历史分页游标') from None
        where.append('(created < ? OR (created = ? AND id < ?))')
        params.extend((created, created, key))
    order = 'created DESC,id DESC'
    if not history:
        order = "CASE WHEN status IN (" + ','.join('?' for _ in active) + ") THEN 0 ELSE 1 END," + order
        params.extend(active)
    sql = 'SELECT * FROM ' + table + (' WHERE ' + ' AND '.join(where) if where else '')
    rows = [dict(row) for row in db.execute(sql + ' ORDER BY ' + order + ' LIMIT ?', [*params, 101 if history else 100])]
    after = json.dumps([rows[99]['created'], rows[99]['id']], separators=(',', ':')) if history and len(rows) > 100 else None
    return rows[:100], after
