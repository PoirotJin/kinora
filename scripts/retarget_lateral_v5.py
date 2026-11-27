"""Candidate: prevent common-mode leg drift without fixing individual hip roll."""
import numpy as np
from retarget_pose_upright_v3 import body_quaternions
from scipy.spatial.transform import Rotation


def install(cls, world, names, scale):
    import cvxpy as cp
    old_constraints=cls.pose_constraints_callback
    old_objective=cls.pose_objective_callback
    rotation=Rotation.from_quat(body_quaternions(world),scalar_first=True).as_matrix()
    delta=(world[:,names.index('L_Ankle')]+world[:,names.index('R_Ankle')]
           -world[:,names.index('L_Hip')]-world[:,names.index('R_Hip')])*.5*scale
    reference=np.clip(np.einsum('ti,ti->t',delta,rotation[:,:,1]),-.04,.04)

    def expressions(self,q,dqa,jac,points):
        lateral=Rotation.from_quat(q[3:7],scalar_first=True).as_matrix()[:,1]
        # Common mode: both legs shifting sideways together. Opposite outward
        # stance widths are not penalized, nor is each hip roll forced to zero.
        delta=(points['L_Ankle']+points['R_Ankle']-points['L_Hip']-points['R_Hip'])*.5
        derivative=(jac['L_Ankle']+jac['R_Ankle']-jac['L_Hip']-jac['R_Hip'])*.5
        return lateral@(delta+derivative@dqa)

    def constraints(self,q,dqa,jac,points,frame):
        constraints=old_constraints(self,q,dqa,jac,points,frame)
        common=expressions(self,q,dqa,jac,points)
        constraints.extend([common<=.08,common>=-.08])
        return constraints

    def objective(self,q,dqa,jac,points,frame):
        terms=old_objective(self,q,dqa,jac,points,frame)
        terms.append(1000.*cp.square(expressions(self,q,dqa,jac,points)-reference[frame]))
        return terms

    cls.pose_constraints_callback=constraints
    cls.pose_objective_callback=objective


def validate(model,qpos):
    import mujoco
    data=mujoco.MjData(model)
    hips=[model.body(s+'_leg_pelvic_roll_link').id for s in ('left','right')]
    ankles=[model.body(s+'_leg_ankle_roll_link').id for s in ('left','right')]
    base=model.body('base_link').id
    common=[];com_offset=[]
    for q in qpos:
        data.qpos[:]=q;mujoco.mj_kinematics(model,data);mujoco.mj_comPos(model,data)
        lateral=data.xmat[base].reshape(3,3)[:,1]
        common.append(float((data.xpos[ankles]-data.xpos[hips]).mean(axis=0)@lateral))
        com_offset.append(float((data.subtree_com[base]-data.xpos[ankles].mean(axis=0))@lateral))
    return dict(profile='v5-common-mode-lateral-candidate',
                max_abs_common_leg_offset_m=float(np.max(np.abs(common))),
                max_abs_com_from_ankle_midpoint_m=float(np.max(np.abs(com_offset))),
                passed=bool(np.max(np.abs(common))<=.081),
                balance_verified=False,
                note='Lateral geometry only; no contact-force or dynamic balance certification')
