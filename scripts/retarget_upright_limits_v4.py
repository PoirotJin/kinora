"""Explicit batch-specific torso and inter-frame joint bounds; no post-hoc clipping."""
import numpy as np
import os

HIP_SPEED=np.deg2rad(120.)
HIP_ACCEL=np.deg2rad(1440.)
ROLL_SPEED=np.deg2rad(120.)
ROLL_ACCEL=np.deg2rad(1800.)
ANKLE_SPEED=np.deg2rad(180.)
ANKLE_ACCEL=np.deg2rad(3600.)
BANK_LIMIT=np.deg2rad(12.)

def install(cls,fps=30.):
    def geometry_constraints(self,q,dqa,jac,points,frame):
        import mujoco
        import cvxpy as cp
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
            from retarget_pose_upright_v4 import foot_axes
            (lat,dlat),(normal,dnormal)=foot_axes(self,side)
            bank=lat[2]+dlat[2]@dqa
            up=normal[2]+dnormal[2]@dqa
            constraints.extend([up>=.65, bank<=np.tan(BANK_LIMIT)*up,
                                bank>=-np.tan(BANK_LIMIT)*up])
        # Leave enough distance to brake before a joint limit. A per-frame
        # acceleration bound alone can make the NEXT frame infeasible when
        # the previous pose reaches a limit with nonzero outward velocity.
        if frame>0:
            last=self._v4_last_q
            for jid in range(1,self.robot_model.njnt):
                name=self.robot_model.joint(jid).name
                acceleration=(HIP_ACCEL if 'pelvic_yaw' in name else
                              ROLL_ACCEL if 'pelvic_roll' in name else
                              ANKLE_ACCEL if 'ankle_roll' in name else None)
                if acceleration is None:continue
                adr=int(self.robot_model.jnt_qposadr[jid])
                idx=int(np.flatnonzero(self.q_a_indices==adr)[0])
                value=q[adr]+dqa[idx]
                delta=value-last[adr]
                lo,hi=self.robot_model.jnt_range[jid]
                braking=2.*acceleration/fps**2
                constraints.extend([cp.square(cp.pos(delta))<=braking*(hi-value),
                                    cp.square(cp.pos(-delta))<=braking*(value-lo)])
        return constraints
    cls.pose_constraints_callback=geometry_constraints
    original=cls.solve_single_iteration
    def solve(self,**kwargs):
        lower,upper=self.q_a_lb.copy(),self.q_a_ub.copy()
        lo,hi=lower.copy(),upper.copy()
        frame=kwargs['frame_idx'];last=kwargs['q_t_last']
        self._v4_last_q=last.copy()
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
                accel_limit=None
                if 'pelvic_yaw' in name:accel_limit=HIP_ACCEL
                elif 'pelvic_roll' in name:speed,accel_limit=ROLL_SPEED,ROLL_ACCEL
                elif 'ankle_roll' in name:speed,accel_limit=ANKLE_SPEED,ANKLE_ACCEL
                lo[idx]=max(lo[idx],last[adr]-speed/fps)
                hi[idx]=min(hi[idx],last[adr]+speed/fps)
                prior=getattr(self,'_upright_previous_delta',None)
                # Acceleration is optimized with a strong temporal objective,
                # then checked against the unchanged export limits. Greedy
                # hard acceleration boxes can deadlock against stance contacts.
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
    data=mujoco.MjData(model);tilts=[];angles=[];banks=[]
    for q in qpos:
        data.qpos[:]=q;mujoco.mj_kinematics(model,data)
        root=data.xmat[model.body('base_link').id].reshape(3,3)
        tilts.append(np.degrees(np.arccos(np.clip(root[2,2],-1,1))))
        forward=root[:,0];side=root[:,1]
        feet=[]
        bank=[]
        for name,sign in [('left',-1),('right',1)]:
            toe=data.xmat[model.body(name+'_leg_ankle_roll_link').id].reshape(3,3)[:,0]
            feet.append(sign*np.degrees(np.arctan2(toe@side,toe@forward)))
            rot=data.xmat[model.body(name+'_leg_ankle_roll_link').id].reshape(3,3)
            bank.append(np.degrees(np.arctan2(rot[2,1],rot[2,2])))
        angles.append(feet)
        banks.append(bank)
    yaw=qpos[:,[model.jnt_qposadr[model.joint(s+'_leg_pelvic_yaw_joint').id] for s in ('left','right')]]
    speed=np.max(np.abs(np.diff(yaw,axis=0)))*fps
    accel=np.max(np.abs(np.diff(yaw,n=2,axis=0)))*fps**2 if len(yaw)>2 else 0.
    result=dict(profile='upright-v4-foot-bank-roll-continuity',max_torso_tilt_deg=float(max(tilts)),max_hip_yaw_speed_deg_s=float(np.degrees(speed)),
        max_hip_yaw_accel_deg_s2=float(np.degrees(accel)),max_toe_inward_deg=float(np.max(angles)),
        p95_toe_inward_deg=np.percentile(angles,95,axis=0).tolist(),
        wrist_ranges_deg={model.joint(j).name:float(np.degrees(np.ptp(qpos[:,model.jnt_qposadr[j]]))) for j in range(1,model.njnt) if 'wrist' in model.joint(j).name})
    result['passed']=bool(max(tilts)<1. and speed<=HIP_SPEED+.005 and accel<=HIP_ACCEL+.05 and np.max(angles)<=6.)
    result['max_abs_foot_bank_deg']=float(np.max(np.abs(banks)))
    result['p95_abs_foot_bank_deg']=np.percentile(np.abs(banks),95,axis=0).tolist()
    result['passed'] &= result['max_abs_foot_bank_deg']<=np.degrees(BANK_LIMIT)+.5
    for label,part,speed_limit,accel_limit in [('hip_roll','pelvic_roll',ROLL_SPEED,ROLL_ACCEL),('ankle_roll','ankle_roll',ANKLE_SPEED,ANKLE_ACCEL)]:
        values=qpos[:,[model.jnt_qposadr[model.joint(s+'_leg_'+part+'_joint').id] for s in ('left','right')]]
        velocity=float(np.max(np.abs(np.diff(values,axis=0)))*fps)
        acceleration=float(np.max(np.abs(np.diff(values,n=2,axis=0)))*fps**2) if len(values)>2 else 0.
        result['max_'+label+'_speed_deg_s']=float(np.degrees(velocity))
        result['max_'+label+'_accel_deg_s2']=float(np.degrees(acceleration))
        result[label+'_second_difference_rms_deg']=float(np.degrees(np.sqrt(np.mean(np.diff(values,n=2,axis=0)**2)))) if len(values)>2 else 0.
        result['passed'] &= velocity<=speed_limit+.005 and acceleration<=accel_limit+.05
    result['passed']=bool(result['passed'])
    return result
