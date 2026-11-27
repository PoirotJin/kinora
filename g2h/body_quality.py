"""Bounded human-volume and stance/root consistency screening, Y-up metres.

Capsules approximate body volume; they are not a posed SMPL surface certificate.
No source arrays are changed. Thresholds are explicit and require human calibration.
"""
import re
import numpy as np

VERSION = 'body-capsules-stance-v1'


def segment_distance(a, b, c, d):
    """Exact minimum distance of finite line segments, vectorized over frames."""
    u=b-a;v=d-c;w=a-c
    aa=np.sum(u*u,axis=-1);bb=np.sum(u*v,axis=-1);cc=np.sum(v*v,axis=-1)
    dd=np.sum(u*w,axis=-1);ee=np.sum(v*w,axis=-1)
    den=aa*cc-bb*bb
    s=np.divide(bb*ee-cc*dd,den,out=np.zeros_like(den),where=np.abs(den)>1e-12)
    t=np.divide(aa*ee-bb*dd,den,out=np.zeros_like(den),where=np.abs(den)>1e-12)
    interior=np.linalg.norm(w+s[...,None]*u-t[...,None]*v,axis=-1)
    interior=np.where((np.abs(den)>1e-12)&(s>=0)&(s<=1)&(t>=0)&(t<=1),interior,np.inf)
    def point(p,x,y):
        delta=y-x;length=np.sum(delta*delta,axis=-1)
        f=np.divide(np.sum((p-x)*delta,axis=-1),length,out=np.zeros_like(length),where=length>1e-12)
        return np.linalg.norm(p-x-np.clip(f,0,1)[...,None]*delta,axis=-1)
    return np.minimum.reduce([interior,point(a,c,d),point(b,c,d),point(c,a,b),point(d,a,b)])


def intervals(mask,fps):
    ids=np.flatnonzero(mask);out=[]
    if not len(ids):return out
    for group in np.split(ids,np.flatnonzero(np.diff(ids)>1)+1):
        out.append(dict(start_frame=int(group[0]),end_frame=int(group[-1]),
                        start_s=round(float(group[0]/fps),3),end_s=round(float((group[-1]+1)/fps),3)))
    return out[:40]


def intent(text,topic):
    text=(text or '').lower()
    # Explicitly negated exemptions must not disable a detector.
    clean=re.sub(r'\b(?:not|without|no)\b(?:\W+\w+){0,3}\W+(?:treadmill|in place|on the spot)\b','',text)
    if re.search(r'\btreadmill\b',clean):return 'moving_surface'
    from g2h.motion_vocab import GAIT,CHINESE_GAIT
    GAIT=GAIT+'|'+CHINESE_GAIT
    # Shared with gait screening: "from a stationary crouch" is a start state, not in place.
    from g2h.gait_checks import in_place_requested
    if in_place_requested(clean):return 'in_place'
    if topic in ('ground_get_up','standing_to_ground','squatting','crawling','floor_movements'):return 'non_locomotion'
    if topic in ('walking','running') or re.search(GAIT,text):return 'travelling_gait'
    return 'unspecified'


def analyze(record):
    m=record['motion'];p=np.asarray(m['native_keypoints3d'],float);tr=np.asarray(m['native_transl'],float);fps=float(m['fps'])
    if p.ndim!=3 or p.shape[1]<22 or p.shape[2]!=3 or not 2<=len(p)<=3600 or tr.shape!=(len(p),3) or not np.isfinite(fps) or not 1<=fps<=240 or not np.isfinite(p).all() or not np.isfinite(tr).all():
        raise ValueError('Invalid body geometry')
    world=p[:,:22]-p[:,:1]+tr[:,None]
    # Scale proxy thickness by lower-leg length; report it, never infer true shape.
    leg=float(np.median(np.linalg.norm(world[:,4]-world[:,7],axis=1)))
    if not .15<=leg<=.8:
        return dict(version=VERSION,flags=['body_geometry_unavailable'],checks={'self_collision_proxy':None,'stance_slide':None,'root_gait_consistency':None},reason='unsupported_body_scale',events={},issue_frames={})
    scale=leg/.42
    segments={}
    for side,hip,knee,ankle,toe,shoulder,elbow,wrist in [('left',1,4,7,10,16,18,20),('right',2,5,8,11,17,19,21)]:
        for label,i,j,radius in [('thigh',hip,knee,.075),('shin',knee,ankle,.05),('foot',ankle,toe,.04),('upper_arm',shoulder,elbow,.04),('forearm',elbow,wrist,.035)]:
            a=world[:,i].copy();b=world[:,j].copy()
            if label=='thigh':a += .18*(b-a)  # exclude the shared pelvis attachment
            segments[side+'_'+label]=(a,b,radius*scale)
    segments['torso']=(world[:,0]+.15*(world[:,12]-world[:,0]),world[:,9],.12*scale)
    pairs=[('left_'+a,'right_'+b) for a in ('thigh','shin','foot') for b in ('thigh','shin','foot')]
    pairs += [('left_'+a,'right_'+b) for a in ('upper_arm','forearm') for b in ('upper_arm','forearm')]
    for side in ('left','right'):
        pairs.append((side+'_forearm','torso'))
        pairs += [(side+'_forearm',s+'_'+legpart) for s in ('left','right') for legpart in ('thigh','shin')]
    collisions=[];collision_frames=np.zeros(len(p),bool)
    for left,right in pairs:
        a,b,ra=segments[left];c,d,rb=segments[right]
        depth=ra+rb-segment_distance(a,b,c,d)
        # Midpoint check catches brief crossings, explicitly a linear sweep proxy.
        middle=ra+rb-segment_distance((a[:-1]+a[1:])/2,(b[:-1]+b[1:])/2,(c[:-1]+c[1:])/2,(d[:-1]+d[1:])/2)
        depth[:-1]=np.maximum(depth[:-1],middle)
        mask=depth>.02*scale
        spans=intervals(mask,fps)
        sustained=any((x['end_frame']-x['start_frame']+1)/fps>=.08 for x in spans)
        severe=float(depth.max())>.06*scale
        if mask.any() and (sustained or severe):
            collision_frames|=mask
            collisions.append(dict(parts=[left,right],max_overlap_proxy_m=round(float(depth.max()),4),intervals=spans))
    mode=intent(record.get('text',''),record.get('topic',''))
    feet=world[:,[7,10,8,11]];local=feet-tr[:,None]
    floor=float(np.percentile(feet[:,:,1].min(axis=1),5))
    contact=(feet[:-1,:,1]-floor<.045*scale)&(feet[1:,:,1]-floor<.045*scale)&(np.abs(np.diff(feet[:,:,1],axis=0))*fps<.3*scale)
    v=np.diff(feet[:,:,[0,2]],axis=0)*fps;lv=np.diff(local[:,:,[0,2]],axis=0)*fps
    # Per foot use the slower valid heel/toe anchor, allowing normal foot roll.
    speed=np.linalg.norm(v,axis=-1);anchors=[];anchor_local=[];support=[]
    for k in (0,2):
        choice=np.argmin(np.where(contact[:,k:k+2],speed[:,k:k+2],np.inf),axis=1)+k
        rows=np.arange(len(choice));anchors.append(speed[rows,choice]);anchor_local.append(lv[rows,choice]);support.append(contact[:,k:k+2].any(axis=1))
    anchors=np.stack(anchors,axis=1);anchor_local=np.stack(anchor_local,axis=1);support=np.stack(support,axis=1)
    slip=(anchors>.25*scale)&support
    slip_frames=slip.any(axis=1)
    slip_time=float(slip_frames.sum()/fps);support_time=float(support.any(axis=1).sum()/fps)
    slip_distance=float(np.sum(np.max(np.where(slip,anchors,0),axis=1))/fps)
    floor_mode=mode=='non_locomotion'
    slide_applicable=mode!='moving_surface' and not floor_mode
    slide_bad=slide_applicable and slip_time>=.2 and slip_distance>.12*scale and slip_time/max(support_time,1e-9)>.2
    count=support.sum(axis=1)
    expected=-np.sum(anchor_local*support[:,:,None],axis=1)/np.maximum(count,1)[:,None]
    root_v=np.diff(tr[:,[0,2]],axis=0)*fps
    expected_path=float(np.sum(np.linalg.norm(expected,axis=1))/fps)
    actual_path=float(np.sum(np.linalg.norm(root_v,axis=1)*(count>0))/fps)
    root_bad=mode=='travelling_gait' and support_time>=.5 and expected_path>.6*scale and actual_path<.35*expected_path
    root_frames=(count>0)&(np.linalg.norm(expected,axis=1)>.2*scale)&(np.linalg.norm(root_v,axis=1)<.35*np.linalg.norm(expected,axis=1))
    flags=[]
    if collisions:flags.append('possible_self_penetration')
    if slide_bad:flags.append('possible_stance_sliding')
    if root_bad:flags.append('possible_root_gait_mismatch')
    return dict(version=VERSION,flags=flags,
        checks=dict(self_collision_proxy=not bool(collisions),stance_slide=not slide_bad if slide_applicable and support_time>.2 else None,root_gait_consistency=not root_bad if mode=='travelling_gait' and support_time>.5 else None),
        metrics=dict(body_scale=scale,intent=mode,support_time_s=support_time,slide_time_s=slip_time,slide_distance_proxy_m=slip_distance,expected_root_path_proxy_m=expected_path,actual_root_path_on_support_m=actual_path,floor_proxy_m=floor),
        thresholds=dict(overlap_m=.02*scale,severe_overlap_m=.06*scale,overlap_duration_s=.08,contact_height_m=.045*scale,slide_speed_mps=.25*scale,slide_duration_s=.2,slide_distance_m=.12*scale,root_path_ratio=.35,expected_path_m=.6*scale),
        events=dict(self_collision=collisions,stance_slide=intervals(slip_frames,fps) if slide_bad else [],root_gait_mismatch=intervals(root_frames,fps) if root_bad else []),
        issue_frames=dict(self_collision_proxy=np.flatnonzero(collision_frames).tolist(),stance_slide=np.flatnonzero(slip_frames).tolist() if slide_bad else [],root_gait_consistency=np.flatnonzero(root_frames).tolist() if root_bad else []),
        scope='Estimated human capsule volumes with midpoint sweep; inferred stance on static ground. Not mesh-exact, complete self-collision coverage, robot geometry or dynamics verification. Treadmill surface speed is unknown; ground actions skip upright gait checks.')
