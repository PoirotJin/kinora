"""Frame-addressable checks for robot trajectories; no dynamics claim."""
import numpy as np
from scipy.spatial.transform import Rotation
VERSION='robot-diagnostics-v1'
LIMITS=dict(root_step_m=.25,root_acceleration_m_s2=150.,rotation_step_rad=float(np.pi/4),joint_step_rad=float(np.pi/4))

def temporal(model,qpos,fps):
    q=np.asarray(qpos,float);events=[]
    def add(kind,values,limit,offset,part):
        bad=np.flatnonzero(values>limit)
        for i in bad[:100]:events.append(dict(kind=kind,frame=int(i+offset),end_frame=int(i+offset+1),part=part,value=float(values[i]),limit=float(limit),severity='error'))
        return len(bad)==0
    step=np.linalg.norm(np.diff(q[:,:3],axis=0),axis=1)
    acc=np.linalg.norm(np.diff(q[:,:3],n=2,axis=0),axis=1)*fps*fps
    rot=Rotation.from_quat(q[:,3:7],scalar_first=True)
    angle=(rot[:-1].inv()*rot[1:]).magnitude()
    checks=[add('root_step',step,LIMITS['root_step_m'],1,'root'),add('root_acceleration',acc,LIMITS['root_acceleration_m_s2'],2,'root'),add('root_rotation',angle,LIMITS['rotation_step_rad'],1,'root')]
    for j in range(1,model.njnt):
        if int(model.jnt_type[j])==3:
            v=np.abs(np.diff(q[:,int(model.jnt_qposadr[j])]))
            checks.append(add('joint_step',v,LIMITS['joint_step_rad'],1,model.joint(j).name))
    return dict(version=VERSION,passed=all(checks),events=events[:1000],limits=LIMITS,
        root_step_max_m=float(step.max(initial=0)),root_acceleration_max_m_s2=float(acc.max(initial=0)),scope='Discrete kinematic continuity, not motor or balance validation')

def tracking_events(tracking):
    rows=[]
    for i,v in enumerate(tracking.get('per_frame_rms_m',[])):
        if v>.15:rows.append(dict(kind='tracking_rms',frame=i,end_frame=i+1,part='mapped bodies',value=v,limit=.15,severity='error'))
    for i,v in enumerate(tracking.get('per_frame_max_m',[])):
        if v>.35:rows.append(dict(kind='tracking_max',frame=i,end_frame=i+1,part='mapped bodies',value=v,limit=.35,severity='error'))
    return rows[:1000]
