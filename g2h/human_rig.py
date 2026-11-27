"""Kinora's independently specified, procedural 52-joint authoring rig.

Dimensions below are design parameters in metres, not extracted model data.
The channel order is a compatibility interface, not a licensed body mesh/model.
"""
from pathlib import Path
import hashlib
import json
import numpy as np

VERSION = 'kinora-human52-v1'
ASSETS = Path(__file__).resolve().parents[1] / 'data/human_rig'
BODY_NAMES = ('pelvis','left_hip','right_hip','spine1','left_knee','right_knee',
              'spine2','left_ankle','right_ankle','spine3','left_foot','right_foot',
              'neck','left_collar','right_collar','head','left_shoulder','right_shoulder',
              'left_elbow','right_elbow','left_wrist','right_wrist')

def definition():
    parents = [-1,0,0,0,1,2,3,4,5,6,7,8,9,9,9,12,13,14,16,17,18,19]
    offsets = [(0,0,0),(.095,-.055,0),(-.095,-.055,0),(0,.13,0),
               (0,-.415,0),(0,-.415,0),(0,.13,0),(0,-.415,0),(0,-.415,0),
               (0,.16,0),(0,-.025,.16),(0,-.025,.16),(0,.145,0),
               (.085,.07,0),(-.085,.07,0),(0,.13,0),(.105,0,0),(-.105,0,0),
               (.285,0,0),(-.285,0,0),(.255,0,0),(-.255,0,0)]
    names = list(BODY_NAMES)
    # Fingers use straight, articulated design chains in neutral pose.
    for side, wrist, sign in [('left',20,1),('right',21,-1)]:
        for finger,z,length in [('index',.027,.04),('middle',.009,.045),
                                ('pinky',-.027,.03),('ring',-.009,.04),('thumb',.055,.03)]:
            start=len(parents)
            parents += [wrist,start,start+1]
            offsets += [(sign*.055,-.008,z),(sign*length,0,0),(sign*length*.7,0,0)]
            names += [f'{side}_{finger}{i}' for i in (1,2,3)]
    parents=np.asarray(parents,dtype='<i4');rest=np.zeros((52,3),dtype='<f4')
    for i in range(1,52):rest[i]=rest[parents[i]]+offsets[i]
    return rest,parents,names

def load():
    # Runtime reads the same independent definition used to build the bundled
    # preview descriptors; no external asset lookup or network fallback.
    rest,parents,_=definition()
    return rest.copy(),parents.copy()

def write_assets(destination=ASSETS):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    rest,parents,names=definition()
    rest.tofile(destination/'j_template.bin');parents.tofile(destination/'kintree.bin')
    (destination/'joint_names.json').write_text(json.dumps(names,indent=2)+'\n')
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(destination.iterdir()) if p.suffix in ('.bin','.json') and p.name!='provenance.json'}
    (destination/'provenance.json').write_text(json.dumps({'version':VERSION,'origin':'Independent numerical design in g2h/human_rig.py; no downloaded or copied skeleton, mesh, skinning or texture assets.','units':'metres','axes':'Y up, +Z forward, T pose','preview':'Procedural bones and joints drawn from these coordinates; no textured body mesh.','files':files},indent=2)+'\n')

def motion_rig(arrays):
    """Use embedded geometry, or recover it from the user's own motion.

    Legacy files need no external template. Inverse FK recovers constant bone
    offsets from stored poses/points and checks all frames before editing.
    It never changes the source file or silently substitutes new body lengths.
    """
    from .kimodo_adapter import fk
    from scipy.spatial.transform import Rotation
    default,parents=load()
    if 'parents' in arrays:
        raw=np.asarray(arrays['parents'])
        if raw.shape!=(52,) or raw.dtype.kind not in 'iu':raise ValueError('Invalid embedded hierarchy')
        parents=raw.astype(np.int32)
    points=np.asarray(arrays['native_keypoints3d'],dtype=np.float64)
    if points.ndim!=3 or points.shape[1:]!=(52,3) or not len(points) or not np.isfinite(points).all():raise ValueError('Invalid motion skeleton')
    if 'smpl_poses' not in arrays:raise ValueError('Motion rotations are required to recover its rest rig')
    local=Rotation.from_rotvec(np.asarray(arrays['smpl_poses']).reshape(-1,3)).as_matrix().reshape(-1,52,3,3)
    if len(local)!=len(points):raise ValueError('Motion frame counts disagree')
    _,glob=fk(local,default,parents)
    if 'rest_joints' in arrays:
        rest=np.asarray(arrays['rest_joints'],dtype=np.float64)
        if rest.shape!=(52,3) or not np.isfinite(rest).all():raise ValueError('Invalid embedded rest joints')
    else:
        rest=np.zeros((52,3));rest[0]=preview_root(arrays, default)
        for i in range(1,52):
            p=int(parents[i]);delta=points[:,i]-points[:,p]
            offset=np.einsum('tji,tj->ti',glob[:,p],delta)
            rest[i]=rest[p]+np.median(offset,axis=0)
    rebuilt,_=fk(local,rest,parents)
    if not np.allclose(points-points[:,:1],rebuilt-rebuilt[:,:1],atol=5e-3,rtol=0):
        raise ValueError('Stored points and rotations cannot establish a consistent rest rig')
    return rest,parents

def preview_root(arrays, fallback):
    # Existing preview-only imports may not contain rotations. Their root point
    # is preserved; new artifacts carry the exact rest pivot explicitly.
    if 'root_origin' in arrays:
        origin=np.asarray(arrays['root_origin'])
        if origin.shape!=(3,) or not np.isfinite(origin).all():raise ValueError('Invalid root origin')
        return origin
    if 'rest_joints' in arrays:return np.asarray(arrays['rest_joints'])[0]
    if 'smpl_poses' not in arrays:return np.asarray(fallback)[0]
    points=np.asarray(arrays['native_keypoints3d'])
    return points[0,0] if points.ndim==3 and len(points) else np.asarray(fallback)[0]
