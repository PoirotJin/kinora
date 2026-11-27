"""Persistent, bounded production acceptance windows; no workload control.

Only committed motion telemetry contributes to production. Host measurements
are spot samples, not claims of uninterrupted GPU utilization. A completed
report freezes the configured time boundary even if the sampler restarts late.
"""
from contextlib import closing
import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import re
import threading
import time

INTERVAL = 60
MAX_DURATION = 7 * 86400
SCHEMA = '''
CREATE TABLE IF NOT EXISTS campaigns(
 id TEXT PRIMARY KEY,started REAL NOT NULL,ends REAL NOT NULL,status TEXT NOT NULL,
 finished REAL,baseline_bytes INTEGER,summary TEXT NOT NULL DEFAULT '{}');
CREATE UNIQUE INDEX IF NOT EXISTS acceptance_active ON campaigns((1)) WHERE status='running';
CREATE INDEX IF NOT EXISTS acceptance_recent ON campaigns(started DESC);
CREATE TABLE IF NOT EXISTS samples(
 campaign TEXT NOT NULL,ts REAL NOT NULL,covered REAL NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(campaign,ts));
CREATE INDEX IF NOT EXISTS acceptance_sample_time ON samples(ts);
'''


def numeric(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.3)
    deadline = time.monotonic() + 1.5
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return db


def iso(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


class Acceptance:
    def __init__(self, root, prices=None):
        self.root = Path(root).resolve()
        self.folder = self.root / 'review'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'acceptance.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.lock = threading.RLock()
        self.prices = prices
        self.price_error = None
        if prices is None:
            try:
                self.prices = json.loads(os.environ.get('MOTIONFARM_ACCEPTANCE_PRICES', '{}'))
            except (ValueError, TypeError):
                self.prices = {}
                self.price_error = 'API 单价配置不可解析，费用保持未知。'
        if not isinstance(self.prices, dict):
            self.prices = {}
            self.price_error = 'API 单价配置必须为模型到单价的映射，费用保持未知。'
        with closing(self._connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=.5)
        db.row_factory = sqlite3.Row
        return db

    def _campaign(self, db):
        return db.execute('SELECT * FROM campaigns ORDER BY started DESC LIMIT 1').fetchone()

    def _capacity(self):
        path = self.folder / 'storage/capacity.sqlite3'
        try:
            with closing(readonly(path)) as db:
                row = db.execute('SELECT total,sampled FROM capacity WHERE id=1').fetchone()
            if row and time.time() - row[1] <= 300:
                return int(row[0]), row[1]
        except (OSError, sqlite3.Error):
            pass
        return None, None

    def start(self, duration_s=86400):
        duration_s = numeric(duration_s)
        if duration_s is None or not 1 <= duration_s <= MAX_DURATION:
            raise ValueError('验收时长必须为 1 秒至 7 天。')
        with self.lock:
            self.observe({})  # Close an expired window before starting another.
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute("SELECT id FROM campaigns WHERE status='running'").fetchone()
                if existing:
                    return self.get()
                now = time.time()
                baseline, _ = self._capacity()
                key = secrets.token_hex(8)
                db.execute('INSERT INTO campaigns(id,started,ends,status,baseline_bytes) VALUES(?,?,?,?,?)',
                           (key, now, now + duration_s, 'running', baseline))
                # Raw samples retain 90 days; summaries/reports are retained.
                # Each explicit start removes at most 2,000 stale sample rows.
                db.execute('DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples WHERE ts<? ORDER BY ts LIMIT 2000)',
                           (now - 90 * 86400,))
            try:
                from g2h.observability import initialize
                initialize(self.root)
            except (ImportError, OSError, sqlite3.Error):
                pass
            self._refresh(now)
            return self.get()

    def get(self):
        """Read one persisted, minute-cached summary; never scan farm files."""
        with self.lock, closing(self._connect()) as db:
            row = self._campaign(db)
            if row:
                result = json.loads(row['summary'])
                if result:
                    return result
            pending = dict(id=row['id'], started_at=row['started'], ends_at=row['ends'],
                finished_at=row['finished'], duration_s=row['ends']-row['started'],
                elapsed_s=max(0, min(time.time(), row['ends'])-row['started']), progress=0) if row else None
            return {'version': 1, 'active': bool(row and row['status'] == 'running'),
                    'status': row['status'] if row else 'not_started', 'campaign': pending,
                    'sampled_at': None, 'coverage': {}, 'production': {}, 'resources': {},
                    'api': {}, 'reviews': {}, 'text_review': {}, 'stages': [], 'issues': [], 'notes': []}

    def observe(self, monitor_snapshot):
        """Called by the shared monitor; persist at most one sample per minute."""
        with self.lock:
            now = time.time()
            with closing(self._connect()) as db:
                row = self._campaign(db)
                if not row or row['status'] != 'running':
                    return
                last = db.execute('SELECT ts FROM samples WHERE campaign=? ORDER BY ts DESC LIMIT 1',
                                  (row['id'],)).fetchone()
                previous = last[0] if last else row['started']
                if now < row['ends'] and now - previous < INTERVAL:
                    return
                point_ts = numeric(monitor_snapshot.get('sampled_at'))
                valid = (monitor_snapshot.get('ready') is True and point_ts is not None and
                         row['started'] <= point_ts < row['ends'] and
                         0 <= now - point_ts <= 2 * INTERVAL and point_ts >= previous)
                if valid:
                    point = self._point(monitor_snapshot)
                    covered = min(INTERVAL, max(0, point_ts - previous))
                    with db:
                        db.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?)',
                                   (row['id'], point_ts, covered, json.dumps(point, allow_nan=False)))
            # Missing host data still refreshes production/API figures once per
            # minute. The cached timestamp prevents repeated failed samplings.
            prior = json.loads(row['summary'])
            if now >= row['ends'] or now - (prior.get('sampled_at') or row['started']) >= INTERVAL:
                self._refresh(now)

    def _point(self, snapshot):
        gpus = (snapshot.get('gpu') or {}).get('items') or []
        interfaces = snapshot.get('network') or []
        def network(key):
            values = [numeric(item.get(key)) for item in interfaces]
            return sum(values) if values and all(v is not None for v in values) else None
        capacity, sampled = self._capacity()
        loads = [numeric(item.get('load')) for item in gpus]
        memories = [numeric(item.get('used_mb')) for item in gpus]
        return dict(ts=snapshot['sampled_at'], cpu=numeric(snapshot.get('cpu')),
                    gpu=mean(loads), memory=numeric((snapshot.get('memory') or {}).get('percent')),
                    gpu_memory_mb=sum(memories) if memories and all(v is not None for v in memories) else None,
                    rx_bps=network('rx_bps'), tx_bps=network('tx_bps'),
                    capacity_bytes=capacity, capacity_sampled_at=sampled, capacity_observed_at=time.time(),
                    topic_ids=[str(item.get('id', '')) for item in (snapshot.get('topics') or [])[:100]],
                    alerts=[{'key': str(item.get('key', ''))[:100],
                             'severity': str(item.get('severity', ''))[:20]}
                            for item in (snapshot.get('alerts') or [])[:30]])

    def _refresh(self, now):
        # CLI and the web sampler can overlap. Serialize the complete
        # read/build/publish/freeze sequence across processes without waiting.
        descriptor = os.open(self.folder/'acceptance.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._refresh_locked(now)
        finally:
            os.close(descriptor)

    def _refresh_locked(self, now):
        with closing(self._connect()) as db:
            row = self._campaign(db)
            if not row or row['status'] != 'running':
                return
            samples = db.execute('SELECT ts,covered,body FROM samples WHERE campaign=? ORDER BY ts LIMIT 10082',
                                 (row['id'],)).fetchall()
        result = self._build(row, samples, now)
        finished = now if now >= row['ends'] else None
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if finished is not None:
            # Publish files before marking complete, so a transient filesystem
            # error leaves the campaign retryable on the next monitor tick.
            self._write_reports(row['id'], result)
        with closing(self._connect()) as db, db:
            db.execute('UPDATE campaigns SET summary=?,status=?,finished=? WHERE id=? AND status=\'running\'',
                       (encoded, result['status'], finished, row['id']))

    def _write_reports(self, key, result):
        folder = self.folder / 'acceptance_reports'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for suffix, content in (('.json', json.dumps(result, ensure_ascii=False, indent=2)),
                                ('.md', self.markdown(result))):
            target = folder / (key + suffix)
            temporary = target.with_suffix(target.suffix + '.%s.tmp' % os.getpid())
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _build(self, campaign, rows, now):
        start, end = campaign['started'], min(now, campaign['ends'])
        elapsed = max(0, end - start)
        points = [json.loads(row['body']) for row in rows]
        covered = min(elapsed, sum(row['covered'] for row in rows))
        times = [start] + [row['ts'] for row in rows] + [end]
        gaps = [max(0, b - a) for a, b in zip(times, times[1:])]
        complete = now >= campaign['ends']
        for point in points:
            observed = point.get('capacity_observed_at', point['ts'])
            sampled = point.get('capacity_sampled_at')
            if (observed > end or complete and observed >= end or
                    sampled is not None and sampled > point['ts']):
                point['capacity_bytes'] = None
                point['capacity_sampled_at'] = None
        issues = []
        resources = dict(method='sampled_mean',
            cpu_percent=mean([p['cpu'] for p in points]),
            gpu_percent=mean([p['gpu'] for p in points]),
            memory_percent=mean([p['memory'] for p in points]),
            rx_bps=mean([p['rx_bps'] for p in points]),
            tx_bps=mean([p['tx_bps'] for p in points]),
            gpu_memory_mb=mean([p['gpu_memory_mb'] for p in points]),
            gpu_peak_memory_mb=max((p['gpu_memory_mb'] for p in points if p['gpu_memory_mb'] is not None), default=None),
            points=points[::max(1, math.ceil(len(points) / 1441))])
        disk_points = [p for p in points if p['capacity_bytes'] is not None and
                       (p.get('capacity_observed_at', p['ts']) < end or
                        not complete and p.get('capacity_observed_at', p['ts']) <= end) and
                       p.get('capacity_sampled_at', end) <= p['ts']]
        latest = disk_points[-1] if disk_points else {}
        baseline = campaign['baseline_bytes']
        latest_bytes = latest.get('capacity_bytes')
        resources['disk'] = dict(baseline_bytes=baseline, latest_bytes=latest_bytes,
            growth_bytes=latest_bytes-baseline if latest_bytes is not None and baseline is not None else None,
            latest_sampled_at=latest.get('capacity_sampled_at'))
        resources['alerts'] = {severity: sum(any(a['severity'] == severity for a in p.get('alerts', [])) for p in points)
                               for severity in ('critical', 'warning', 'info')}
        production, stages = self._telemetry(start, end, issues)
        for key in ('motions', 'motion_seconds'):
            production[key + '_per_hour'] = production[key] * 3600 / elapsed if production[key] is not None and elapsed else None
        api = self._api(start, end, issues)
        reviews = self._reviews(start, end, production['motions'], issues)
        text_review = self._text_review(start, end, issues)
        if complete:
            topic_ids = set(points[-1].get('topic_ids', []) if points else [])
            topic_ids.update(item['topic'] for item in production['topics'])
            topic_ids.add('custom')
            self._pending_outbox(topic_ids, start, end, issues)
        if self.price_error:
            issues.append(self.price_error)
        if max(gaps, default=0) > 2 * INTERVAL:
            issues.append('主机采样存在超过 2 分钟的缺口；缺口不计入监控覆盖时间，不推算资源用量。')
        report_base = 'review/acceptance_reports/' + campaign['id']
        return dict(version=1, active=not complete, status='completed' if complete else 'running',
            campaign=dict(id=campaign['id'], started_at=start, ends_at=campaign['ends'],
                finished_at=now if complete else None, duration_s=campaign['ends']-start,
                elapsed_s=elapsed, progress=min(1, elapsed/(campaign['ends']-start))),
            sampled_at=now, finalized_at=now if complete else None,
            reports=dict(json=report_base+'.json', markdown=report_base+'.md', written=complete),
            coverage=dict(samples=len(points), expected_samples=int(elapsed // INTERVAL),
                covered_seconds=covered, gap_seconds=max(0, elapsed-covered),
                coverage_ratio=covered/elapsed if elapsed else None, max_gap_s=max(gaps, default=0),
                notes=['采样间隔 60 秒；每对采样最多计 60 秒覆盖，重启和长缺口不外推。',
                       '覆盖率表示采样链连续程度，不代表整段时间内 GPU/CPU 被连续测量。']),
            production=production, resources=resources, api=api, reviews=reviews,
            text_review=text_review, stages=stages,
            issues=issues,
            notes=['统计窗口为 [started_at, ends_at)，所有时间戳为 UTC Unix 秒；每小时桶从验收开始时对齐。',
                   '吞吐按完整已过窗口时间计算，包含停机、排队和失败等待；不外推 24 小时总产量。',
                   'CPU/GPU/内存和网卡吞吐为有效采样的算术均值；缺失数据为 null。GPU 多卡先取卡间均值，显存求和。',
                   '阶段时长为在窗口内完成的调用耗时，重试分别计数，可跨任务重叠，不能相加当作墙钟时间。',
                   '动作产出仅来自实际提交后的遥测；审核浏览、预览加载、旧数据补索引不计为产出。',
                   '账本占用可能保守高估，校准会使占用下降；增长量不是动作文件的压缩率。',
                   'API 统计仅覆盖本项目记录；未配置模型单价或用量未知时，总费用为 null。',
                   '报告在窗口结束后的首个采样周期生成，截止时间不延长；生成后才抵达的延迟事件不回写已冻结报告。'])

    def _telemetry(self, start, end, issues):
        production = dict(available=False, motions=None, motion_seconds=None, bytes=None,
                          topics=[], hourly=[], sources=[], recording_complete=None)
        stages = []
        try:
            with closing(readonly(self.folder/'telemetry.sqlite3')) as db:
                db.execute('BEGIN')
                meta = dict(db.execute('SELECT key,value FROM meta'))
                started = numeric(float(meta.get('started_at', 'nan')))
                if started is not None and started > start + 1:
                    issues.append('动作遥测在验收窗口开始后才初始化；此前的产出覆盖未知。')
                scope = (start, end)
                row = db.execute('SELECT count(*),coalesce(sum(duration),0),coalesce(sum(bytes),0) FROM motions WHERE saved>=? AND saved<?', scope).fetchone()
                production.update(available=True, motions=row[0], motion_seconds=row[1], bytes=row[2])
                failures = db.execute('SELECT count(*) FROM errors WHERE ts>=? AND ts<?', scope).fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='errors'").fetchone() else 0
                production['recording_complete'] = not bool(failures)
                if failures:
                    issues.append(f'本窗口记录到 {failures} 次遥测写入故障；产出、阶段或 API 记录可能不完整。')
                production['topics'] = [dict(zip(('topic', 'motions', 'motion_seconds', 'bytes'), r)) for r in db.execute(
                    'SELECT topic,count(*),sum(duration),sum(bytes) FROM motions WHERE saved>=? AND saved<? GROUP BY topic ORDER BY topic LIMIT 1001', scope)]
                production['sources'] = [dict(zip(('source', 'motions', 'motion_seconds'), r)) for r in db.execute(
                    'SELECT source,count(*),sum(duration) FROM motions WHERE saved>=? AND saved<? GROUP BY source LIMIT 20', scope)]
                buckets = {int(r[0]): r[1:] for r in db.execute(
                    'SELECT CAST((saved-?)/3600 AS INTEGER),count(*),sum(duration) FROM motions WHERE saved>=? AND saved<? GROUP BY 1',
                    (start, start, end))}
                for hour in range(max(1, math.ceil((end-start)/3600))):
                    count, seconds = buckets.get(hour, (0, 0))
                    production['hourly'].append(dict(started_at=start+hour*3600,
                        elapsed_s=max(0, min(3600, end-start-hour*3600)), motions=count, motion_seconds=seconds))
                stage_rows = db.execute('''SELECT stage,count(*),sum(CASE WHEN status!='ok' THEN 1 ELSE 0 END),
                    coalesce(sum(seconds),0),avg(seconds),max(seconds) FROM spans
                    WHERE finished>=? AND finished<? GROUP BY stage ORDER BY stage LIMIT 101''', scope).fetchall()
                stages = [dict(zip(('stage', 'calls', 'failures', 'total_seconds', 'mean_seconds', 'max_seconds'), r)) for r in stage_rows]
                unfinished = dict(db.execute('''SELECT stage,count(*) FROM spans
                    WHERE started>=? AND started<? AND (finished IS NULL OR finished>=?) GROUP BY stage LIMIT 101''',
                    (start, end, end)))
                for item in stages:
                    item['unfinished'] = unfinished.pop(item['stage'], 0)
                stages.extend(dict(stage=stage, calls=0, failures=0, total_seconds=0,
                                   mean_seconds=None, max_seconds=None, unfinished=count)
                              for stage, count in unfinished.items())
                if sum(s['unfinished'] for s in stages):
                    issues.append('存在窗口结束前未完成的阶段调用；耗时与失败结果尚未知，未计作成功或 0 秒。')
                if len(production['topics']) > 1000:
                    issues.append('主题明细超过 1,000 项，明细已截断；总量仍包括所有主题。')
                    production['topics'] = production['topics'][:1000]
        except (OSError, sqlite3.Error, ValueError, TypeError):
            issues.append('动作/阶段遥测不可读或超过查询时限；相关指标未知。')
            production.update(available=False, motions=None, motion_seconds=None, bytes=None, topics=[], hourly=[], sources=[])
            production['recording_complete'] = None
            stages = []
        marker = self.folder/'telemetry-error.json'
        try:
            if marker.exists() and marker.stat().st_size <= 4096:
                error = json.loads(marker.read_text())
                timestamp = numeric(error.get('time', error.get('ts')))
                if timestamp is not None and start <= timestamp < end:
                    production['recording_complete'] = False
                    issues.append('本窗口发生过遥测写入失败；产出或阶段记录可能缺失，不能认定完全覆盖。')
        except (OSError, ValueError, TypeError):
            issues.append('遥测故障标记无法读取，记录完整性未知。')
        return production, stages

    def _api(self, start, end, issues):
        result = dict(available=False, calls=None, errors=None, unknown_calls=None,
            input_tokens=None, output_tokens=None, total_tokens=None, models=[],
            cost_cny=None, known_cost_cny=None, unpriced_models=[], recording_complete=None)
        try:
            with closing(readonly(self.folder/'api_usage.sqlite3')) as db:
                rows = db.execute('''SELECT model,kind,count(*),sum(status!='ok'),sum(known=0),
                    sum(CASE WHEN known=1 THEN input ELSE 0 END),
                    sum(CASE WHEN known=1 THEN output ELSE 0 END),
                    sum(CASE WHEN known=1 THEN total ELSE 0 END),
                    sum(known=1),sum(known=1 AND input+output!=total)
                    FROM events WHERE ts>=? AND ts<? GROUP BY model,kind LIMIT 501''',
                    (start, end)).fetchall()
            if len(rows) > 500:
                raise ValueError('model detail limit exceeded')
            columns = ('model', 'kind', 'calls', 'errors', 'unknown_calls',
                       'input_tokens', 'output_tokens', 'total_tokens', 'known_calls', 'unknown_split_calls')
            result['models'] = [dict(zip(columns, row)) for row in rows]
            result.update(available=True)
            for key in columns[2:]:
                result[key] = sum(item[key] or 0 for item in result['models'])
            priced = 0
            partial_cost = 0.
            uncertain = bool(result['unknown_calls'] or result['unknown_split_calls'])
            for item in result['models']:
                rate = self.prices.get(item['model'], {})
                inp = numeric(rate.get('input_cny_per_million')) if isinstance(rate, dict) else None
                out = numeric(rate.get('output_cny_per_million')) if isinstance(rate, dict) else None
                if inp is None or out is None or min(inp, out) < 0:
                    result['unpriced_models'].append(item['model'])
                    uncertain = True
                    item['known_cost_cny'] = None
                elif item['unknown_split_calls']:
                    item['known_cost_cny'] = None
                    uncertain = True
                else:
                    priced += 1
                    item['known_cost_cny'] = (item['input_tokens']*inp+item['output_tokens']*out)/1_000_000
                    partial_cost += item['known_cost_cny']
                    item['price_cny_per_million'] = dict(input=inp, output=out)
                if item['calls'] and not item['known_calls']:
                    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                        item[key] = None
            result['unpriced_models'] = sorted(set(result['unpriced_models']))
            result['known_cost_cny'] = partial_cost if priced or not rows else None
            result['cost_cny'] = None if uncertain else partial_cost
            faults = self._api_recording_faults(start, end)
            result['recording_complete'] = not faults if faults is not None else None
            if faults or faults is None:
                result['cost_cny'] = None
                issues.append('API 写入记录曾失败或完整性状态不可读；调用与 Token 仅为已记录部分，总费用未知。')
            if result['calls'] and not result['known_calls']:
                for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                    result[key] = None
            if result['unknown_calls']:
                issues.append('存在 API 请求未返回用量；Token 汇总只包含已知用量，总费用未知。')
            if result['unknown_split_calls']:
                issues.append('部分 API 用量缺少可靠的输入/输出拆分，不能按分项单价计算费用。')
        except (OSError, sqlite3.Error, ValueError, TypeError):
            issues.append('API 用量记录不可读或超过查询时限；调用、Token 和费用未知。')
        return result

    def _api_recording_faults(self, start, end):
        count = 0
        try:
            with closing(readonly(self.folder/'telemetry.sqlite3')) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='errors'").fetchone():
                    count = db.execute("SELECT count(*) FROM errors WHERE ts>=? AND ts<? AND operation='api_usage_write'", (start, end)).fetchone()[0]
            marker = self.folder/'telemetry-error.json'
            if marker.exists():
                if marker.stat().st_size > 4096:
                    return None
                error = json.loads(marker.read_text())
                timestamp = numeric(error.get('time', error.get('ts')))
                if timestamp is not None and start <= timestamp < end and error.get('operation') == 'api_usage_write':
                    count += 1
            return count
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None

    def _text_review(self, start, end, issues):
        result = dict(available=False, decisions=None, distinct=None, duplicate=None,
                      invalid=None, uncertain=None, distinct_rate=None,
                      scope='文本 distinct 判定率；包含审核尝试，不是动作质量通过率或提示词成功入库率。')
        try:
            with closing(readonly(self.folder/'agent.sqlite3')) as db:
                counts = dict(db.execute('SELECT verdict,count(*) FROM decisions WHERE ts>=? AND ts<? GROUP BY verdict LIMIT 20', (start, end)))
            total = sum(counts.values())
            result.update(available=True, decisions=total,
                          distinct_rate=counts.get('distinct', 0)/total if total else None)
            for key in ('distinct', 'duplicate', 'invalid', 'uncertain'):
                result[key] = counts.get(key, 0)
        except (OSError, sqlite3.Error, TypeError):
            issues.append('文本审核判定记录不可读或超过查询时限；文本 distinct 判定率未知。')
        return result

    def _pending_outbox(self, topic_ids, start, end, issues):
        deadline = time.monotonic() + 1.5
        pending = 0
        capped = len(topic_ids) > 100
        for topic in sorted(topic_ids)[:100]:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', topic):
                continue
            if time.monotonic() > deadline:
                capped = True
                break
            path = self.root/'topics'/topic/'index.sqlite3'
            if not path.is_file():
                continue
            try:
                with closing(readonly(path)) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='telemetry_outbox'").fetchone():
                        pending += db.execute('''SELECT count(*) FROM telemetry_outbox o
                            JOIN records r ON r.id=o.task_id WHERE o.saved>=? AND o.saved<?''', (start, end)).fetchone()[0]
            except (OSError, sqlite3.Error):
                capped = True
        if pending:
            issues.append(f'报告生成时有 {pending} 条已提交动作的遥测仍待转发；这些动作可能尚未进入产出统计。')
        if capped:
            issues.append('部分主题的待转发遥测未能在限定范围内核对；延迟事件完整性未知。')

    def _reviews(self, start, end, cohort, issues):
        result = dict(available=False, cohort=cohort, reviewed=None, accepted=None,
            rejected=None, uncertain=None, unreviewed=None, pass_rate=None, coverage=None,
            denominator='通过率 = 本窗口生成动作中 accepted 数 / 已审核数（含 uncertain）；未审核不视为失败。')
        if cohort is None:
            return result
        try:
            review_path = self.folder/'reviews.sqlite3'
            if not review_path.is_file():
                raise FileNotFoundError(review_path)
            with closing(readonly(self.folder/'telemetry.sqlite3')) as db:
                db.execute('ATTACH DATABASE ? AS reviewdb', (review_path.resolve().as_uri()+'?mode=ro',))
                db.execute('BEGIN')
                # Select the latest human decision BEFORE the cutoff. The
                # current `latest` table could already contain a later review.
                rows = db.execute('''SELECT json_extract(r.body,'$.status'),count(*)
                    FROM motions m LEFT JOIN reviewdb.events r ON r.event_id=(
                      SELECT e.event_id FROM reviewdb.events e WHERE e.motion_id=m.motion_id
                      AND julianday(json_extract(e.body,'$.updated_at')) < julianday(?)
                      ORDER BY e.revision DESC LIMIT 1)
                    WHERE m.saved>=? AND m.saved<? GROUP BY 1''', (iso(end), start, end)).fetchall()
            counts = dict(rows)
            if sum(counts.values()) != cohort:
                issues.append('本轮汇总期间收到延迟动作遥测，产出与审核队列快照暂不一致；通过率等待下一次刷新。')
                return result
            reviewed = sum(counts.get(status, 0) for status in ('accepted', 'rejected', 'uncertain'))
            result.update(available=True, reviewed=reviewed, accepted=counts.get('accepted', 0),
                rejected=counts.get('rejected', 0), uncertain=counts.get('uncertain', 0),
                unreviewed=cohort-reviewed, pass_rate=counts.get('accepted', 0)/reviewed if reviewed else None,
                coverage=reviewed/cohort if cohort else None)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            issues.append('本窗口生成动作的人工审核结果不可读或超过查询时限；通过率未知。')
        return result

    def export_markdown(self):
        return self.markdown(self.get())

    @staticmethod
    def markdown(result):
        if result['status'] == 'not_started':
            return '# kinora 持续生产验收\n\n尚未开始验收。\n'
        campaign = result['campaign']
        def value(v, suffix=''):
            if v is None:
                return '未知'
            return (f'{v:.2f}' if isinstance(v, float) else str(v)) + suffix
        production, api = result['production'], result['api']
        resources, coverage, reviews = result['resources'], result['coverage'], result['reviews']
        text_review = result['text_review']
        lines = ['# kinora 持续生产验收', '',
            f"验收 ID：{campaign['id']}；状态：{result['status']}。", '',
            f"窗口：{iso(campaign['started_at'])} 至 {iso(campaign['ends_at'])}（结束边界不包含）。",
            f"数据汇总时间：{iso(result['sampled_at'])}；已过窗口 {value(campaign['elapsed_s'], ' 秒')}。", '',
            '## 产出与人工审核', '',
            f"- 实际提交动作：{value(production['motions'])} 个；动作总时长：{value(production['motion_seconds'], ' 秒')}。",
            f"- 已过窗口平均产能：{value(production['motions_per_hour'], ' 个/小时')}；{value(production['motion_seconds_per_hour'], ' 动作秒/小时')}。",
            f"- 已审核：{value(reviews['reviewed'])} / {value(reviews['cohort'])}；通过：{value(reviews['accepted'])}；拒绝：{value(reviews['rejected'])}；待定：{value(reviews['uncertain'])}。",
            f"- 审核通过率：{value(reviews['pass_rate']*100 if reviews['pass_rate'] is not None else None, '%')}。{reviews['denominator']}", '',
            f"- 文本 distinct 判定率：{value(text_review['distinct_rate']*100 if text_review['distinct_rate'] is not None else None, '%')}（{value(text_review['distinct'])} / {value(text_review['decisions'])} 次判定）；不代表实际动作质量或提示词入库成功率。", '',
            '## API 与资源', '',
            f"- API 请求：{value(api['calls'])}；失败：{value(api['errors'])}；用量未知：{value(api['unknown_calls'])}。",
            f"- 已知输入/输出/总 Token：{value(api['input_tokens'])} / {value(api['output_tokens'])} / {value(api['total_tokens'])}。",
            f"- 总估算费用：{value(api['cost_cny'], ' 元')}；可计价已知部分：{value(api['known_cost_cny'], ' 元')}（仅配置单价估算，不是账单）。",
            f"- CPU/GPU 采样均值：{value(resources['cpu_percent'], '%')} / {value(resources['gpu_percent'], '%')}。",
            f"- 接收/发送吞吐采样均值：{value(resources['rx_bps'], ' bytes/s')} / {value(resources['tx_bps'], ' bytes/s')}。",
            f"- 数据账本占用增长：{value(resources['disk']['growth_bytes'], ' bytes')}。",
            f"- 主机采样：{coverage['samples']} 次；覆盖 {value(coverage['covered_seconds'], ' 秒')}；缺口 {value(coverage['gap_seconds'], ' 秒')}。", '',
            '## 每小时产出', '']
        for item in production['hourly']:
            lines.append(f"- {iso(item['started_at'])}，窗口 {value(item['elapsed_s'], ' 秒')}：{item['motions']} 个动作 / {value(item['motion_seconds'], ' 动作秒')}。")
        lines += ['', '## 主题产出', '']
        for item in production['topics']:
            lines.append(f"- {item['topic']}：{item['motions']} 个 / {value(item['motion_seconds'], ' 秒')}。")
        lines += ['', '## 阶段耗时', '']
        for item in result['stages']:
            lines.append(f"- {item['stage']}：完成 {item['calls']} 次，失败 {item['failures']} 次，未完成 {item['unfinished']} 次；总耗时 {value(item['total_seconds'], ' 秒')}，均值 {value(item['mean_seconds'], ' 秒')}，最大 {value(item['max_seconds'], ' 秒')}。")
        lines += ['', '## 数据限制与异常', '']
        lines += ['- '+message for message in result['issues']+result['notes']+coverage['notes']]
        return '\n'.join(lines)+'\n'


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('--duration', type=float, default=86400)
    commands.add_parser('status')
    commands.add_parser('report')
    args = parser.parse_args()
    acceptance = Acceptance(args.root)
    if args.command == 'start':
        output = acceptance.start(args.duration)
    elif args.command == 'report':
        print(acceptance.export_markdown())
        return
    else:
        output = acceptance.get()
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
