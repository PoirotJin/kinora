"""Candidate v4: constrain the foot lateral axis, not just its heading."""
import numpy as np
from retarget_pose_upright_v3 import morphology_targets, head_angles
from retarget_pose_upright_v3 import install_pose_targets as install_v3


def foot_axes(self, side):
    import mujoco
    body = self.robot_model.body(side + '_leg_ankle_roll_link').id
    rotation = self.robot_data.xmat[body].reshape(3, 3)
    jp = np.zeros((3, self.robot_model.nv))
    jr = np.zeros_like(jp)
    mujoco.mj_jacBody(self.robot_model, self.robot_data, jp, jr, body)
    transform = jr @ self._build_transform_qdot_to_qvel_fast()
    result = []
    for axis in (rotation[:, 1], rotation[:, 2]):
        x, y, z = axis
        skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
        result.append((axis.copy(), (-skew @ transform)[:, self.q_a_indices]))
    return result


def install_pose_targets(cls, world, warm_start=None, poses=None):
    import cvxpy as cp
    install_v3(cls, world, warm_start, poses)
    original = cls.pose_objective_callback

    def objective(self, q, dqa, jac, points, frame):
        terms = original(self, q, dqa, jac, points, frame)
        for side in ('left', 'right'):
            (lateral, dlateral), _ = foot_axes(self, side)
            # Level lateral axis suppresses inversion/eversion without directly
            # penalizing toe-off pitch. Full world-up tracking would flatten it.
            terms.append(160. * cp.square(lateral[2] + dlateral[2] @ dqa))
        prior = getattr(self, '_upright_previous_delta', None)
        last = getattr(self, '_v4_last_q', None)
        if frame > 1 and prior is not None and last is not None:
            for jid in range(1, self.robot_model.njnt):
                name = self.robot_model.joint(jid).name
                if not any(part in name for part in ('pelvic_roll','pelvic_yaw','ankle_roll')):
                    continue
                adr = int(self.robot_model.jnt_qposadr[jid])
                idx = int(np.flatnonzero(self.q_a_indices == adr)[0])
                terms.append(3000. * cp.square(q[adr] + dqa[idx] - last[adr] - prior[adr]))
        return terms

    cls.pose_objective_callback = objective
