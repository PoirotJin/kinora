"""Explicit batch-specific torso and inter-frame joint bounds; no post-hoc clipping."""
import numpy as np
import os

HIP_SPEED=np.deg2rad(120.)
HIP_ACCEL=np.deg2rad(1440.)

def install(cls,fps=30.):
    def geometry_constraints(self,q,dqa,jac,points,frame):
        import mujoco
        from scipy.spatial.transform import Rotation
        rotation=Rotation.from_quat(q[3:7],scalar_first=True).as_matrix()
        forward,lateral=rotation[:,0],rotation[:,1]
        constraints=[]
        for side,wrist,sign in [('left','L_Wrist',1),('right','R_Wrist',-1)]:
            delta=points[wrist]-points['Pelvis']+(jac[wrist]-jac['Pelvis'])@dqa
            constraints.append(sign*(lateral@delta)>=.40)
            body=self.robot_model.body(side+'_leg_ankle_roll_link').id
            axis=self.robot_data.xmat[body].reshape(3,3)[:,0].copy()
            jp=np.zeros((3,self.robot_model.nv));jr=np.zeros_like(jp)
            mujoco.mj_jacBody(self.robot_model,self.robot_data,jp,jr,body)
            x,y,z=axis;skew=np.array([[0,-z,y],[z,0,-x],[-y,x,0]])
            derivative=-skew@jr@self._build_transform_qdot_to_qvel_fast()
            direction=axis+derivative[:,self.q_a_indices]@dqa
            x=forward@direction;y=sign*(lateral@direction)
            constraints.extend([x>=.65,y>=-np.tan(np.deg2rad(5))*x,y<=np.tan(np.deg2rad(40))*x])
        return constraints
    cls.pose_constraints_callback=geometry_constraints
    original=cls.solve_single_iteration
    def solve(self,**kwargs):
        lower,upper=self.q_a_lb.copy(),self.q_a_ub.copy()
        lo,hi=lower.copy(),upper.copy()
        frame=kwargs['frame_idx'];last=kwargs['q_t_last']
        for adr,value in enumerate(self._upright_orientations[frame],3):
            idx=int(np.flatnonzero(self.q_a_indices==adr)[0])
            if frame==0 or adr in (4,5) or os.environ.get('AMP70_FIXED_YAW')=='1':lo[idx]=hi[idx]=value
            else:
                # Keep roll/pitch upright but let yaw accommodate planted feet.
                lo[idx]=max(lo[idx],last[adr]-.025)
                hi[idx]=min(hi[idx],last[adr]+.025)
        for jid in range(1,self.robot_model.njnt):
            name=self.robot_model.joint(jid).name
            adr=int(self.robot_model.jnt_qposadr[jid])
            idx=int(np.flatnonzero(self.q_a_indices==adr)[0])
            # Keep the arm chain outside the hip envelope while wrists move.
            if name=='left_shoulder_roll_joint':lo[idx]=max(lo[idx],.30)
            if name=='right_shoulder_roll_joint':hi[idx]=min(hi[idx],-.30)
            if frame>0:
                speed=HIP_SPEED if 'pelvic_yaw' in name else np.deg2rad(240.)
                lo[idx]=max(lo[idx],last[adr]-speed/fps)
                hi[idx]=min(hi[idx],last[adr]+speed/fps)
                prior=getattr(self,'_upright_previous_delta',None)
                if frame>1 and prior is not None and 'pelvic_yaw' in name:
                    lo[idx]=max(lo[idx],last[adr]+prior[adr]-HIP_ACCEL/fps**2)
                    hi[idx]=min(hi[idx],last[adr]+prior[adr]+HIP_ACCEL/fps**2)
        self.q_a_lb,self.q_a_ub=lo,hi
        try:
            try:return original(self,**kwargs)
            except Exception as exc:
                import cvxpy as cp
                if frame!=0 or not isinstance(exc,(cp.error.SolverError,RuntimeError)):raise
                # Initial pose may lie outside the wrist/foot feasible region.
                # Widen only the first-frame trust region; retain all physical constraints.
                step=self.step_size
                try:
                    self.step_size=max(step,2.)
                    return original(self,**kwargs)
                finally:self.step_size=step
        finally:self.q_a_lb,self.q_a_ub=lower,upper
    cls.solve_single_iteration=solve
    iterate=cls.iterate
    def frame_solve(self,**kwargs):
        q,cost=iterate(self,**kwargs)
        self._upright_previous_delta=q-kwargs['q_t_last']
        return q,cost
    cls.iterate=frame_solve

def validate(model,qpos,fps):
    import mujoco
    data=mujoco.MjData(model);tilts=[];angles=[]
    for q in qpos:
        data.qpos[:]=q;mujoco.mj_kinematics(model,data)
        root=data.xmat[model.body('base_link').id].reshape(3,3)
        tilts.append(np.degrees(np.arccos(np.clip(root[2,2],-1,1))))
        forward=root[:,0];side=root[:,1]
        feet=[]
        for name,sign in [('left',-1),('right',1)]:
            toe=data.xmat[model.body(name+'_leg_ankle_roll_link').id].reshape(3,3)[:,0]
            feet.append(sign*np.degrees(np.arctan2(toe@side,toe@forward)))
        angles.append(feet)
    yaw=qpos[:,[model.jnt_qposadr[model.joint(s+'_leg_pelvic_yaw_joint').id] for s in ('left','right')]]
    speed=np.max(np.abs(np.diff(yaw,axis=0)))*fps
    accel=np.max(np.abs(np.diff(yaw,n=2,axis=0)))*fps**2 if len(yaw)>2 else 0.
    result=dict(profile='upright-v3-hard-geometry',max_torso_tilt_deg=float(max(tilts)),max_hip_yaw_speed_deg_s=float(np.degrees(speed)),
        max_hip_yaw_accel_deg_s2=float(np.degrees(accel)),max_toe_inward_deg=float(np.max(angles)),
        p95_toe_inward_deg=np.percentile(angles,95,axis=0).tolist(),
        wrist_ranges_deg={model.joint(j).name:float(np.degrees(np.ptp(qpos[:,model.jnt_qposadr[j]]))) for j in range(1,model.njnt) if 'wrist' in model.joint(j).name})
    result['passed']=bool(max(tilts)<1. and speed<=HIP_SPEED+.005 and accel<=HIP_ACCEL+.05 and np.max(angles)<=6.)
    return result
