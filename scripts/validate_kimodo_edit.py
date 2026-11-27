"""Real CUDA acceptance for Kimodo generation, local repair and two-clip splice.

Writes evidence only after measured gates pass. Takes the production GPU flock;
never edits source motions or weakens validation. Run with the Kimodo interpreter.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'scripts'), str(PROJECT)]
from g2h.generation_backend import current_recipe, EDIT_VALIDATION_FILES
from g2h.motion_assets import load_numeric_npz, STATIC
from g2h.kimodo_edit import window
from g2h import motion_splice
from inference_metadata import read_metadata


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path, data):
    path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2));os.replace(tmp,path)


def run(output, gpu_root):
    import numpy as np
    from collection_kimodo import run_batch
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    state={};cases=[];start=time.time()
    code={name:sha(PROJECT/name) for name in EDIT_VALIDATION_FILES}
    def job(name,text,seconds,seed,edit=None):
        folder=output/name;folder.mkdir(exist_ok=True)
        value=dict(spec=dict(current_recipe(),text=text,duration_s=seconds,seed=seed),
                   features=str(folder/'features.pt'),motion=str(folder/'motion.npz'))
        if edit:value['edit']=edit
        batch=folder/'batch.json';write(batch,[value])
        run_batch(SimpleNamespace(batch=batch,stage='run',persistent=True),state)
        path=Path(value['motion']);meta=read_metadata(path)
        assert meta['generation_device']=='cuda:0' and meta['generator_name']==current_recipe()['model']
        assert meta['sequence']['passed']
        record=dict(name=name,artifact=str(path),artifact_sha256=sha(path),seed=seed,sequence=meta['sequence'])
        if edit:
            assert meta['editing']['passed'] and meta['editing']['outside_selection_bitwise_equal']
            before=load_numeric_npz(edit['source']);after=load_numeric_npz(path)
            a,b=edit['start_frame'],edit['end_frame'];frames=len(before['native_transl'])
            for key,v in before.items():
                if key not in STATIC and v.ndim and len(v)==frames:
                    assert np.array_equal(v[:a],after[key][:a]) and np.array_equal(v[b:],after[key][b:]),key
            record['editing']=meta['editing']
        cases.append(record);write(output/'progress.json',dict(cases=cases));print('ACCEPTED '+name,flush=True)
        return path
    with (Path(gpu_root)/'gpu_worker.lock').open('a') as lock:
        print('Waiting for production GPU lock',flush=True);fcntl.flock(lock,fcntl.LOCK_EX)
        source=job('source','A person stands upright with feet comfortably apart and relaxed arms clear of the torso, breathing gently while remaining in place.',5,210)
        plan=window(150,25,125)
        edit=dict(source=str(source),source_sha256=sha(source),start_frame=25,end_frame=125)
        repaired=job('repair','A person stands with feet planted and gently raises the left hand to chest height, then relaxes it.',plan['frames']/30,211,edit)
        from g2h.human_rig import motion_rig
        arrays=load_numeric_npz(source);rest,parents=motion_rig(arrays)
        for name,turn,dx,dz,seed in [('splice-straight',0,0,.4,212),('splice-turn',30,.2,.4,213)]:
            a=motion_splice.transform(arrays,0,60)
            b=motion_splice.transform(arrays,90,150,turn,dx,dz)
            joined,report=motion_splice.join(a,b,240,rest,parents)
            src=output/(name+'-source.npz')
            with src.open('wb') as stream:np.savez_compressed(stream,**joined)
            selection=report['selection'];plan=window(len(joined['native_transl']),selection['start_frame'],selection['end_frame'],True)
            prompt,_=motion_splice.walk_prompt(a,b,240,'A person stands quietly.')
            job(name,prompt,plan['frames']/30,seed,dict(source=str(src),source_sha256=sha(src),**selection,follow_root=True,staged_bridge=True))
    assert code=={name:sha(PROJECT/name) for name in EDIT_VALIDATION_FILES},'Code changed during acceptance'
    receipt=dict(schema_version=2,passed=True,code=code,cases=cases,started_at=start,completed_at=time.time(),
                 scope='Actual Kimodo CUDA inference; exact exterior and numerical seam gates. Semantics and robot dynamics are not certified.')
    write(output/'acceptance.json',receipt);return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--gpu-root',type=Path,required=True)
    args=p.parse_args();run(args.output,args.gpu_root)
