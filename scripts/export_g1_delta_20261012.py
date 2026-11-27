"""Export only validated final G1 results from one immutable batch."""
import csv
import hashlib
import io
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path('/home/jfc/MotionFarm/outputs/text_motion_farm')
batch = sys.argv[1]
if batch not in ('incremental-delta-20261011', 'incremental-delta-20261012'):
    raise ValueError('Unexpected batch')
folder = ROOT / 'review/retarget/batches' / batch
status = json.loads((folder/'status.json').read_text())
if not status['state'].startswith('complete'):
    raise RuntimeError('Batch not complete; preserve running jobs')
OUT = ROOT / 'exports' / ('g1-' + batch)
OUT.mkdir(exist_ok=False)
db = sqlite3.connect(f'file:{folder}/batch.sqlite3?mode=ro', uri=True)
rows = db.execute('SELECT motion_id,topic,job_id,status,message FROM items ORDER BY position').fetchall()
parts = []
failed = []
all_rows = []
part_rows = []
z = None
columns = ['motion_id','topic','job_id','file','bytes','sha256','algorithm','parameter_schema','parameters','part']

def finish():
    global z, part_rows
    if z is None:
        return
    data = io.StringIO()
    w = csv.DictWriter(data,fieldnames=columns)
    w.writeheader(); w.writerows(part_rows)
    z.writestr('manifest-part.csv', data.getvalue())
    path = Path(z.filename); z.close()
    with zipfile.ZipFile(path) as check:
        if check.testzip(): raise ValueError('CRC failure')
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    digest = h.hexdigest()
    parts.append(dict(file=path.name,size=path.stat().st_size,sha256=digest,motions=len(part_rows)))
    (OUT/'progress.json').write_text(json.dumps(dict(exported=len(all_rows),parts=parts)))
    z = None; part_rows = []

for mid, topic, jid, state, message in rows:
    if state != 'done':
        failed.append(dict(motion_id=mid,status=state,reason=message)); continue
    p = ROOT/'review/retarget'/jid
    meta = json.loads((p/'preview.json').read_text())['metadata']
    if meta.get('motion_id') != mid or meta.get('robot') != 'unitree_g1_23dof' or meta.get('quality',{}).get('export_allowed') is not True:
        raise ValueError('Result identity or quality mismatch: '+mid)
    payload = (p/'result.npz').read_bytes()
    if z and z.fp.tell() > 78*1024**2: finish()
    if z is None:
        z = zipfile.ZipFile(OUT/('g1-'+batch+'-part-%03d.zip' % (len(parts)+1)), 'w', zipfile.ZIP_STORED)
    member='motions/'+topic+'/'+mid+'.npz'
    z.writestr(member,payload)
    z.writestr('metadata/'+topic+'/'+mid+'.json',json.dumps(meta,ensure_ascii=False))
    execution=meta.get('execution',{})
    row=dict(motion_id=mid,topic=topic,job_id=jid,file=member,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),algorithm=json.dumps(meta.get('engine',{}),ensure_ascii=False),parameter_schema=execution.get('schema_version','unknown'),parameters=json.dumps(execution.get('parameters',{})),part=Path(z.filename).name)
    part_rows.append(row); all_rows.append(row)
finish()
with (OUT/'manifest.csv').open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(all_rows)
summary=dict(batch=batch,robot='unitree_g1_23dof',selected=len(rows),exported=len(all_rows),excluded=len(failed),parts=parts,finished=time.time(),note='Existing results retain their original algorithm and parameters. Quality failures are excluded.')
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
(OUT/'failures.json').write_text(json.dumps(failed,ensure_ascii=False,indent=2))
(OUT/'COMPLETE').write_text(str(len(all_rows)))
print(json.dumps(summary,ensure_ascii=False))
