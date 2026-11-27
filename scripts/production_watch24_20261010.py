"""Bounded, read-only production queries; append monitoring evidence only."""
import json
import sqlite3
import time
from pathlib import Path
from production_health import read

ROOT = Path('/home/jfc/MotionFarm/outputs/text_motion_farm')
OUT = ROOT / 'review/production/monitor_24h_20261010'
OUT.mkdir(exist_ok=True)
baseline_path = OUT / 'baseline.json'
now = time.time()
if not baseline_path.exists():
    baseline_path.write_text(json.dumps({
        'start': now, 'end': now + 86400, 'target': 48000,
        'initial_job': 'cfc585d1443f9241fe29ed0f',
        'definition': 'Committed production motions by saved timestamp; includes random variants, not quality acceptance.'
    }, indent=2))
baseline = json.loads(baseline_path.read_text())
start, end = baseline['start'], min(now, baseline['end'])
db = sqlite3.connect(f'file:{ROOT}/review/telemetry.sqlite3?mode=ro', uri=True, timeout=10)
def totals(a, b):
    row = db.execute('SELECT count(*),coalesce(sum(duration),0),max(saved) FROM motions WHERE saved>=? AND saved<? AND source=?', (a,b,'production')).fetchone()
    return {'count': row[0], 'duration_hours': row[1]/3600, 'last_saved': row[2]}
total = totals(start, end)
hour = totals(max(start,end-3600),end)
elapsed = max(0,end-start)
result = {'sampled': now, 'start': start, 'end': baseline['end'],
          'elapsed_hours': elapsed/3600, 'total': total, 'last_hour': hour,
          'expected_so_far': elapsed/86400*48000,
          'projected_daily': total['count']/elapsed*86400 if elapsed else None,
          'finished': now>=baseline['end'], 'target_met': total['count']>=48000,
          'topics': db.execute('SELECT topic,count(*),sum(duration)/3600.0 FROM motions WHERE saved>=? AND saved<? AND source=? GROUP BY topic',(start,end,'production')).fetchall(),
          'jobs': db.execute('SELECT job,count(*) FROM motions WHERE saved>=? AND saved<? AND source=? GROUP BY job',(start,end,'production')).fetchall(),
          'health': read(ROOT)}
result['low_hour'] = elapsed>=3600 and hour['count']<1600
result['stalled'] = result['health'].get('no_output_seconds',0)>=1800
with (OUT/'samples.jsonl').open('a') as f:
    f.write(json.dumps(result,ensure_ascii=False)+'\n')
(OUT/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
if result['finished']:
    result['hourly_counts'] = [totals(start+i*3600,start+(i+1)*3600)['count'] for i in range(24)]
    (OUT/'final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
