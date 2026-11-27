"""Minute-cached rolling 24h monitoring, independent of frozen acceptance reports.

Only the shared monitor writes samples. HTTP readers return an immutable cached
snapshot. Host samples persist for 48h; event aggregates use indexed [start,end)
queries against existing telemetry. Missing host history is never synthesized.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from review_acceptance import Acceptance, INTERVAL, numeric, readonly

WINDOW = 86400
RETENTION = 2 * WINDOW


class RollingMonitor(Acceptance):
    # Reuse read-only window aggregations, not Acceptance's campaign lifecycle.
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / 'review'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'monitor_rolling.sqlite3'
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.lock = threading.RLock()
        self.prices = {}
        self.price_error = None
        with closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS host_samples(
                    ts REAL PRIMARY KEY, covered REAL NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshot(
                    id INTEGER PRIMARY KEY CHECK(id=1), ts REAL NOT NULL, body TEXT NOT NULL);
            ''')
            row = db.execute('SELECT ts,body FROM snapshot WHERE id=1').fetchone()
        self.updated = row['ts'] if row else None
        self.snapshot = json.loads(row['body']) if row else {
            'available': False, 'status': 'initializing',
            'error': '最近 24 小时统计正在初始化；历史资源采样不会补造。'}

    def get(self):
        with self.lock:
            return self.snapshot

    def observe(self, monitor_snapshot):
        now = time.time()
        if self.updated is not None and 0 <= now - self.updated < INTERVAL:
            return
        point_ts = numeric(monitor_snapshot.get('sampled_at'))
        valid = (monitor_snapshot.get('ready') is True and point_ts is not None
                 and 0 <= now - point_ts <= 2 * INTERVAL)
        point = self._point(monitor_snapshot) if valid else None
        # The cutoff must include the capacity observation performed above.
        now = time.time()
        start = now - WINDOW
        with closing(self._connect()) as db, db:
            previous = db.execute('SELECT ts FROM host_samples WHERE ts<=? ORDER BY ts DESC LIMIT 1', (now,)).fetchone()
            if point is not None and (not previous or point_ts - previous['ts'] >= INTERVAL):
                # An initial sample is an instant observation, not a preceding minute.
                covered = min(INTERVAL, point_ts - previous['ts']) if previous else 0
                db.execute('INSERT OR IGNORE INTO host_samples VALUES(?,?,?)',
                           (point_ts, covered, json.dumps(point, allow_nan=False)))
            db.execute('DELETE FROM host_samples WHERE ts<?', (now - RETENTION,))
            rows = [dict(r) for r in db.execute(
                'SELECT ts,covered,body FROM host_samples WHERE ts>=? AND ts<=? ORDER BY ts LIMIT 1442', (start, now))]
        # Only the portion of a coverage interval inside the rolling window counts.
        for row in rows:
            row['covered'] = min(row['covered'], max(0, row['ts'] - start))
        first = json.loads(rows[0]['body']) if rows else {}
        baseline = first.get('capacity_bytes') if first and first['ts'] - start <= 2 * INTERVAL else None
        result = self._build(dict(id='rolling-24h', started=start, ends=now, baseline_bytes=baseline), rows, now)
        result.update(available=True, mode='rolling_24h', status='rolling', active=True, finalized_at=None)
        result['campaign'].update(finished_at=None)
        result['reports'] = dict(markdown='/api/monitor/24h/report.md', written=False)
        result['notes'] = [
            '统计窗口为 [开始时间, 汇总时间)，每分钟向前滚动；每小时桶从当前窗口起点对齐。',
            '产出、API 与阶段统计读取已有事件；资源曲线仅包含实际采样，停机和首次启用前的历史不补造。',
            '吞吐按完整 24 小时计算；CPU/GPU 等为有效样本的算术均值，GPU 多卡取均值，缺失为未知。',
            '阶段时长按窗口内完成的调用统计，可跨任务重叠；窗口内开始但未完成的调用单列。',
            '费用采用请求记录中的计价结果并分币种显示，仅为配置单价估算，非供应商账单。',
            '每次汇总重新读取窗口内事件，迟到的遥测可在后续刷新中补入；不会修改旧验收报告。']
        result['issues'] = [s.replace('验收窗口', '滚动窗口').replace('报告生成时', '本次汇总时') for s in result['issues']]
        result['resources']['disk']['baseline_sampled_at'] = first.get('capacity_sampled_at') if baseline is not None else None
        if baseline is None:
            result['issues'].append('窗口起点附近无有效容量采样，24 小时占用变化未知。')
        # Publish only after all work succeeds; exceptions leave the last good snapshot.
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?)', (now, encoded))
        with self.lock:
            self.snapshot = result
            self.updated = now

    def _api(self, start, end, issues):
        # Token aggregation is shared; the historical campaign's environment-only
        # pricing must not override newer per-request frozen cache-aware pricing.
        result = super()._api(start, end, issues)
        result.update(cost_cny=None, known_cost_cny=None, unpriced_models=[],
                      currencies={}, unknown_cost_calls=None, costs_available=False)
        for item in result['models']:
            item.pop('known_cost_cny', None)
        if not result['available']:
            return result
        try:
            with closing(readonly(self.folder / 'api_usage.sqlite3')) as db:
                has = db.execute("SELECT 1 FROM sqlite_master WHERE name='cost_events'").fetchone()
                if has:
                    rows = db.execute('''SELECT c.currency,count(*),sum(c.amount),
                        sum(CASE WHEN c.amount IS NULL OR c.currency IS NULL THEN 1 ELSE 0 END)
                        FROM events e LEFT JOIN cost_events c ON c.event_id=e.id
                        WHERE e.ts>=? AND e.ts<? GROUP BY c.currency LIMIT 101''', (start, end)).fetchall()
                    if len(rows) > 100:
                        raise ValueError('currency detail limit exceeded')
                    currencies = {currency: amount for currency, calls, amount, unknown in rows
                                  if currency is not None and amount is not None}
                    unknown = sum(row[3] for row in rows)
                    if sum(row[1] for row in rows) != result['calls']:
                        raise ValueError('usage and cost snapshots differ')
                else:
                    currencies, unknown = {}, result['calls']
            result.update(costs_available=True, currencies=currencies, unknown_cost_calls=unknown)
            result['known_cost_cny'] = currencies.get('CNY')
            if not result['calls']:
                result['known_cost_cny'] = 0
            if not unknown and result['recording_complete'] is True and not (set(currencies) - {'CNY'}):
                result['cost_cny'] = currencies.get('CNY', 0)
            if unknown:
                issues.append(f'{unknown} 次请求缺少可用的历史计价结果；仅展示已知金额，不推算总费用。')
        except (OSError, sqlite3.Error, ValueError, TypeError):
            issues.append('窗口费用记录不可读或快照不一致；费用未知，等待下次汇总。')
        return result

    def export_markdown(self):
        data = self.get()
        if data.get('available') is not True:
            return '# kinora 最近 24 小时监控\n\n统计暂不可用。\n'
        content = self.markdown(data).replace('持续生产验收', '最近 24 小时监控').replace('验收 ID', '窗口 ID')
        costs = data['api']
        amounts = '；'.join(f'{key} {value:.6f}' for key, value in costs['currencies'].items()) or '暂无可计价金额'
        return content + f"\n按请求记录的已知估算：{amounts}；费用未知请求：{costs['unknown_cost_calls']}。\n"
