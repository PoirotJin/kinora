"""Run the existing checked retargeter on an owner-verified numeric import."""
import argparse
from pathlib import Path
import sys
P=Path(__file__).resolve().parents[1];sys.path[:0]=[str(P/'scripts'),str(P)]

def main():
    parser=argparse.ArgumentParser()
    for name in ('root','job','motion','robot'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    from review_retarget import Retarget
    from gadget_imports import Imports
    from contextlib import closing
    queue=Retarget(args.root)
    with closing(queue.db()) as db:row=db.execute('SELECT owner,motion_id,robot,status FROM jobs WHERE id=?',(args.job,)).fetchone()
    if not row or row['motion_id']!=args.motion or row['robot']!=args.robot or row['status']!='running':raise ValueError('Import job identity mismatch')
    if args.motion.startswith('ma_'):
        from g2h.motion_assets import MotionAssets
        arrays,meta=MotionAssets(args.root).load(row['owner'],args.motion)
        record=dict(motion_id=args.motion,text=meta.get('description',''),duration_s=float(len(arrays['native_keypoints3d'])/30.0),motion=arrays)
    else:
        record=Imports(args.root).read(args.motion,row['owner'])
    import g2h.farm_index as index
    original=index.read_motion
    index.read_motion=lambda db,root,mid:record if mid==args.motion else original(db,root,mid)
    from retarget_engine import run
    run(args.root,args.job,args.motion,robot=args.robot)

if __name__=='__main__':main()
