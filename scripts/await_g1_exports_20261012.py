import json,subprocess,time
from pathlib import Path
root=Path('/home/jfc/MotionFarm/outputs/text_motion_farm')
pending={'incremental-delta-20261011','incremental-delta-20261012'}
while pending:
 for batch in sorted(pending.copy()):
  out=root/'exports'/('g1-'+batch)
  if (out/'COMPLETE').exists(): pending.remove(batch);continue
  status=json.loads((root/'review/retarget/batches'/batch/'status.json').read_text())
  if not status['state'].startswith('complete'):continue
  subprocess.run(['/home/jfc/miniconda3/envs/g2h/bin/python','-u','/home/jfc/MotionFarm/scripts/export_g1_delta_20261012.py',batch],check=True)
  pending.remove(batch)
 if pending:time.sleep(60)
