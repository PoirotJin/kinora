"""Read-only, fixed-snapshot original human motion export; one payload in memory."""
import csv
import hashlib
import io
import json
import os
import pickle
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from g2h.cold_storage import read_payload
from g2h.farm_index import short_id

ROOT = Path('/home/jfc/MotionFarm/outputs/text_motion_farm')
OUT = ROOT / 'exports/human-smplh-delta-20261011'
PREVIOUS = ROOT / 'exports/human-smplh-20261010'
import numpy as np
FIELDS={'poses':'smpl_poses','trans':'smpl_trans','betas':'smpl_betas','gender':'smpl_gender','mocap_framerate':'smpl_mocap_framerate','num_frames':'smpl_num_frames','Rh':'smpl_Rh','root_translation':'native_transl','root_rotations_mat':'native_root_rotations_mat','fps':'fps'}
PART_BYTES = 78 * 1024 * 1024


def ro(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)


def json_bytes(value):
    def convert(x):
        if hasattr(x, 'tolist'):
            return x.tolist()
        raise TypeError(type(x).__name__)
    return json.dumps(value, ensure_ascii=False, default=convert).encode('utf-8')


def sha_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run():
    OUT.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    catalog = ro(ROOT / 'prompts.sqlite3')
    reviews = ro(ROOT / 'review/reviews.sqlite3')
    catalog.execute('BEGIN'); reviews.execute('BEGIN')
    with (PREVIOUS/'manifest.csv').open(encoding='utf-8-sig',newline='') as f:
        already = {row['motion_id'] for row in csv.DictReader(f)}
    removed = set(x[0] for x in reviews.execute('SELECT motion_id FROM removed'))
    review_rows = {mid: dict(revision=rev, status=status, body=json.loads(body))
                   for mid, rev, status, body in reviews.execute('SELECT motion_id,revision,status,body FROM latest')}
    prompts = {tid: json.loads(raw) for tid, raw in catalog.execute('SELECT id,item FROM prompts')}
    links = catalog.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links ORDER BY number').fetchall()
    records = {}
    for topic in sorted(set(x[3] for x in links)):
        with ro(ROOT / 'topics' / topic / 'index.sqlite3') as db:
            records[topic] = {key: (shard, offset, size, digest)
                             for key, shard, offset, size, digest in db.execute('SELECT id,shard,offset,bytes,sha256 FROM records')}
    catalog.close(); reviews.close()
    selected, pending, deleted = [], [], []
    for number, key, tid, topic, seed in links:
        mid = short_id(number)
        if mid in already:
            continue
        if mid in removed:
            deleted.append(mid)
        elif key not in records[topic]:
            pending.append(dict(motion_id=mid, task_id=key, text_id=tid, topic=topic, reason='not_committed_at_snapshot'))
        else:
            selected.append((mid, key, tid, topic, seed, records[topic][key]))
    snapshot = dict(snapshot_utc=started, max_motion_number=max(x[0] for x in links),
                    selected_committed_motions=len(selected), all_catalog_descriptions=len(prompts),
                    removed_excluded=len(deleted), not_committed_excluded=len(pending),
                    scope='Original public gallery human motion records committed at snapshot; removed records excluded. All catalog text descriptions are included separately. Private custom jobs and robot retarget outputs are not included.')
    snapshot.update(previous_export=str(PREVIOUS), previous_snapshot=json.loads((PREVIOUS/'snapshot.json').read_text()), selection='Current committed gallery minus exact prior exported IDs')
    (OUT/'selection.json').write_bytes(json_bytes(selected))
    prompts={tid:prompts[tid] for tid in {x[2] for x in selected}}
    (OUT / 'snapshot.json').write_bytes(json_bytes(snapshot))
    (OUT / 'not-committed.json').write_bytes(json_bytes(pending))
    with (OUT / 'texts.jsonl').open('wb') as f:
        for tid, prompt in sorted(prompts.items()):
            f.write(json_bytes(dict(text_id=tid, prompt=prompt)) + b'\n')
    linked = {}
    for mid, key, tid, topic, seed, loc in selected:
        linked[tid] = linked.get(tid, 0) + 1
    with (OUT / 'texts.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f); w.writerow(['text_id','topic','text','duration_s','snapshot_motion_count'])
        for tid, p in sorted(prompts.items()):
            w.writerow([tid,p.get('topic_id',p.get('topic','')),p.get('text',''),p.get('duration_s',''),linked.get(tid,0)])
    readme = (PREVIOUS/'README.txt').read_text().replace('Fixed snapshot: 2026-10-10 18:00:12 Asia/Shanghai (10:00:12 UTC).', 'Incremental snapshot: '+started+'; excludes every ID in the previous export manifest.').replace('texts.csv/jsonl includes every catalog description, including descriptions without a generated motion.', 'texts.csv/jsonl contains descriptions linked to this incremental motion snapshot.')
    (OUT / 'README.txt').write_text(readme, encoding='utf-8')
    loader = (PREVIOUS/'read_motion.py').read_text()
    (OUT / 'read_motion.py').write_text(loader, encoding='utf-8')
    fields = ['motion_id','task_id','text_id','topic','seed','file','bytes','sha256','source_payload_sha256','frames','fps','duration_s','representation','review_status','part']
    manifest = (OUT / 'manifest.csv').open('w', newline='', encoding='utf-8-sig')
    writer = csv.DictWriter(manifest, fieldnames=fields); writer.writeheader()
    failures = []; parts = []; part_rows = []; z = None; index = 0
    total_frames = 0; total_seconds = 0.; exported = 0; exported_texts = set(); topic_counts = {}; representations = {}

    def finish():
        nonlocal z, part_rows
        if z is None: return
        buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=fields); w.writeheader(); w.writerows(part_rows)
        z.writestr('manifest-part.csv', buf.getvalue().encode('utf-8-sig'))
        z.writestr('README.txt', readme); z.writestr('snapshot.json', json_bytes(snapshot)); z.writestr('read_motion.py', loader)
        name = Path(z.filename); z.close()
        with zipfile.ZipFile(name) as check:
            bad = check.testzip()
            if bad: raise RuntimeError('ZIP CRC mismatch: ' + bad)
        item = dict(file=name.name, size=name.stat().st_size, sha256=sha_file(name), motions=len(part_rows))
        parts.append(item)
        print(json.dumps(dict(part_finished=item, exported=exported, selected=len(selected))), flush=True)
        (OUT / 'progress.json').write_bytes(json_bytes(dict(exported=exported, selected=len(selected), parts=parts)))
        z = None; part_rows = []

    for mid, key, tid, topic, seed, loc in selected:
        try:
            shard, offset, size, digest = loc
            payload = read_payload(ROOT / 'topics' / topic, shard, offset, size, digest, key)
            record = pickle.loads(payload)
            if record['id'] != key: raise ValueError('payload identity mismatch')
            prompt = prompts[tid]
            if record.get('text') != prompt.get('text'): raise ValueError('payload/catalog text mismatch')
            motion = record['motion']; fps = float(motion['fps'])
            frame_array = motion.get('native_transl', motion.get('smpl_trans', motion.get('native_keypoints3d')))
            if frame_array is None or fps <= 0: raise ValueError('missing frames/fps')
            frames = len(frame_array); duration = frames / fps
            meta = {k:v for k,v in record.items() if k != 'motion'}
            meta.update(motion_id=mid,text_id=tid,prompt=prompt,review_snapshot=review_rows.get(mid),
                        source_payload_sha256=digest,
                        motion_arrays={k:dict(dtype=str(getattr(v,'dtype',type(v).__name__)),shape=list(getattr(v,'shape',()))) for k,v in motion.items()})
            arrays={dst:np.asarray(motion[src]) for dst,src in FIELDS.items()}
            if any(a.dtype.hasobject for a in arrays.values()):raise ValueError('Object array')
            if arrays['poses'].shape!=(frames,156) or arrays['trans'].shape!=(frames,3):raise ValueError('SMPL-H shape mismatch')
            if not np.array_equal(arrays['Rh'],arrays['poses'][:,:3]):raise ValueError('Root rotation convention mismatch')
            buf=io.BytesIO();np.savez_compressed(buf,**arrays);payload=buf.getvalue()
            with np.load(io.BytesIO(payload),allow_pickle=False) as decoded:
                for name,a in arrays.items():
                    v=decoded[name]
                    if v.dtype!=a.dtype or v.shape!=a.shape or v.tobytes()!=a.tobytes():raise ValueError('Lossless check failed '+name)
            meta.update(export_format='SMPL-H NPZ',npz_field_mapping=FIELDS,omitted_motion_fields=sorted(set(motion)-set(FIELDS.values())))
            metadata = json_bytes(meta)
        except Exception as exc:
            failures.append(dict(motion_id=mid,task_id=key,text_id=tid,topic=topic,error=str(exc)))
            continue
        if z and z.fp.tell() > PART_BYTES: finish()
        if z is None:
            index += 1
            z = zipfile.ZipFile(OUT / ('human-smplh-delta-part-%03d.zip' % index), 'w', compression=zipfile.ZIP_STORED, allowZip64=True)
        member = 'motions/%s/%s.npz' % (topic,mid)
        z.writestr(member,payload); z.writestr('metadata/%s/%s.json' % (topic,mid),metadata)
        status = review_rows.get(mid,{}).get('status','unreviewed')
        row = dict(motion_id=mid,task_id=key,text_id=tid,topic=topic,seed=seed,file=member,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),source_payload_sha256=digest,
                   frames=frames,fps=fps,duration_s=duration,representation=record.get('representation','unknown'),review_status=status,part=Path(z.filename).name)
        writer.writerow(row); part_rows.append(row)
        exported += 1; total_frames += frames; total_seconds += duration; exported_texts.add(tid)
        topic_counts[topic] = topic_counts.get(topic,0)+1
        rep = record.get('representation','unknown'); representations[rep]=representations.get(rep,0)+1
    finish(); manifest.close()
    (OUT / 'failures.json').write_bytes(json_bytes(failures))
    summary = dict(**snapshot, exported_motions=exported, linked_descriptions=len(exported_texts),
                   total_frames=total_frames,total_duration_seconds=total_seconds,total_duration_hours=total_seconds/3600,
                   failed_motions=len(failures),topic_counts=topic_counts,representations=representations,parts=parts,
                   finished_utc=datetime.now(timezone.utc).isoformat())
    assert exported + len(failures) == len(selected)
    (OUT / 'summary.json').write_bytes(json_bytes(summary))
    with (OUT / 'SHA256SUMS.txt').open('w') as f:
        for path in sorted(OUT.iterdir()):
            if path.is_file() and path.name not in ('SHA256SUMS.txt','progress.json'):
                digest = next((x['sha256'] for x in parts if x['file']==path.name), None) or sha_file(path)
                f.write(digest+'  '+path.name+'\n')
    (OUT / 'COMPLETE').write_text(str(exported)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='parts'}),flush=True)


if __name__ == '__main__':
    run()
