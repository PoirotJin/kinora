"""Additional edit-quality envelope, not manufacturer motor ratings.

Check the correction relative to its parent across the ENTIRE clip. Zero edits
are exact; protected frames and source trajectories are never rewritten.
"""
import numpy as np
VERSION='robot-edit-smoothness-v1'
LIMITS={'joint_velocity':4.,'joint_acceleration':40.,'joint_jerk':800.,'root_velocity':.5,'root_acceleration':5.,'root_jerk':100.}
def analyze(model,parent,candidate,fps):
    delta=np.asarray(candidate,float)-np.asarray(parent,float);events=[];metrics={};failures=0
    channels=[('root',np.linalg.norm(np.diff(delta[:,:3],n=n,axis=0),axis=1)*fps**n,n) for n in (1,2,3)]
    for j in range(1,model.njnt):
        adr=int(model.jnt_qposadr[j]);name=model.joint(j).name
        for n in (1,2,3):channels.append((name,np.abs(np.diff(delta[:,adr],n=n))*fps**n,n))
    for part,values,order in channels:
        prefix='root' if part=='root' else 'joint';key=prefix+'_'+{1:'velocity',2:'acceleration',3:'jerk'}[order];limit=LIMITS[key]
        metrics[key]=max(metrics.get(key,0.),float(values.max(initial=0)))
        bad=np.flatnonzero(~np.isfinite(values)|(values>limit));failures+=len(bad)
        for i in bad[:30]:events.append(dict(kind='edit_'+key,frame=int(i+order),end_frame=int(i+order+1),part=part,value=float(values[i]),limit=limit,severity='error'))
    return dict(version=VERSION,passed=not failures,failures=failures,metrics=metrics,limits=LIMITS.copy(),events=sorted(events,key=lambda x:x['frame'])[:1000],scope='Incremental edit velocity/acceleration/jerk across all frames and boundaries; physical and absolute-motion gates remain separate.')
def verified(quality):
    r=quality.get('edit_smoothness',{})
    return r.get('version')==VERSION and r.get('passed') is True
