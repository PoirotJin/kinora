"""Morphology-independent pose targets for MotionFarm's Humanoid adapter."""
import numpy as np
from scipy.spatial.transform import Rotation

SEGMENTS = [('L_Hip','L_Knee'),('R_Hip','R_Knee'),
            ('L_Knee','L_Ankle'),('R_Knee','R_Ankle'),
            ('L_Shoulder','L_Elbow'),('R_Shoulder','R_Elbow'),
            ('L_Elbow','L_Wrist'),('R_Elbow','R_Wrist')]

def head_rotations(poses):
    # Local robot axes (forward,left,up) in legacy coordinates (+Z,+X,+Y).
    basis=np.array([[0,1,0],[0,0,1],[1,0,0]],dtype=float)
    head=Rotation.from_rotvec(poses[:,12]).as_matrix()@Rotation.from_rotvec(poses[:,15]).as_matrix()
    return Rotation.from_matrix(basis.T@head@basis)


def head_angles(poses):
    return head_rotations(poses).as_euler('ZYX')[:,:2]


def _slerp_midpoint(left, right):
    aligned = np.where(np.sum(left * right, axis=1, keepdims=True) < 0, -right, right)
    midpoint = left + aligned
    return midpoint / np.linalg.norm(midpoint, axis=1, keepdims=True)


def fit_head_joints(target, limits):
    """Fit the Humanoid yaw (Z) and pitch (Y) hinges to a rotation sequence."""
    from scipy.optimize import least_squares
    from scipy.sparse import lil_matrix

    quaternions = target.as_quat()
    filtered = quaternions.copy()
    # Each half-way SLERP aligns signs locally and preserves the endpoints.
    neighbours = _slerp_midpoint(quaternions[:-2], quaternions[2:])
    filtered[1:-1] = _slerp_midpoint(quaternions[1:-1], neighbours)
    inverse_target = Rotation.from_quat(filtered).inv()

    frames = len(target)
    velocity_rows = 2 * max(frames - 1, 0)
    acceleration_rows = 2 * max(frames - 2, 0)
    sparsity = lil_matrix((3 * frames + velocity_rows + acceleration_rows, 2 * frames), dtype=bool)
    for frame in range(frames):
        sparsity[3 * frame:3 * frame + 3, 2 * frame:2 * frame + 2] = True
    for frame in range(frames - 1):
        row = 3 * frames + 2 * frame
        sparsity[row:row + 2, 2 * frame:2 * frame + 4] = True
    for frame in range(frames - 2):
        row = 3 * frames + velocity_rows + 2 * frame
        sparsity[row:row + 2, 2 * frame:2 * frame + 6] = True

    def residual(angles):
        angles = angles.reshape(frames, 2)
        error = (inverse_target * Rotation.from_euler('ZY', angles)).as_rotvec()
        velocity = np.diff(angles, axis=0)
        acceleration = np.diff(velocity, axis=0)
        return np.concatenate((error.ravel(), 0.2 * velocity.ravel(), 0.5 * acceleration.ravel()))

    lower, upper = np.tile(limits[:, 0], frames), np.tile(limits[:, 1], frames)
    initial = np.clip(np.zeros(2 * frames), lower, upper)
    solution = least_squares(
        residual, initial, bounds=(lower, upper), jac_sparsity=sparsity.tocsr(),
        ftol=1e-6, max_nfev=160,
    )
    if not solution.success:
        raise RuntimeError('Humanoid 头部关节轨迹求解未收敛')
    return solution.x.reshape(frames, 2)

def body_quaternions(world):
    """Robot basis X=forward,Y=left,Z=up; preserve source lean, no yaw flips."""
    left=world[:,1]-world[:,2]
    up=world[:,12]-world[:,0]
    forward=np.cross(left,up)
    norms=np.linalg.norm(forward,axis=1)
    if np.any(norms<1e-6):raise ValueError('退化人体躯干，无法确定朝向')
    forward/=norms[:,None]
    left/=np.linalg.norm(left,axis=1)[:,None]
    up=np.cross(forward,left)
    q=Rotation.from_matrix(np.stack([forward,left,up],axis=-1)).as_quat(scalar_first=True)
    for i in range(1,len(q)):
        if np.dot(q[i],q[i-1])<0:q[i]*=-1
    return q

def morphology_targets(world,model,mapping,names):
    """Preserve directions while using Humanoid link lengths and joint offsets."""
    import mujoco
    data=mujoco.MjData(model);mujoco.mj_forward(model,data)
    rest={key:data.xpos[model.body(link).id].copy() for key,link in mapping.items()}
    root=rest['Pelvis']
    # Scale horizontal travel by leg length; ground alignment below sets height.
    human_leg=sum(np.linalg.norm(world[0,names.index(b)]-world[0,names.index(a)]) for a,b in SEGMENTS[:1]+SEGMENTS[2:3])
    robot_leg=sum(np.linalg.norm(rest[b]-rest[a]) for a,b in SEGMENTS[:1]+SEGMENTS[2:3])
    if human_leg<1e-6:raise ValueError('源动作腿长退化')
    scale=robot_leg/human_leg
    result=world.copy()*scale
    rotations=Rotation.from_quat(body_quaternions(world),scalar_first=True).as_matrix()
    for name in ['L_Hip','R_Hip','L_Shoulder','R_Shoulder']:
        result[:,names.index(name)]=result[:,0]+np.einsum('tij,j->ti',rotations,rest[name]-root)
    for a,b in SEGMENTS+[('L_Ankle','L_Foot'),('R_Ankle','R_Foot')]:
        delta=world[:,names.index(b)]-world[:,names.index(a)]
        norm=np.linalg.norm(delta,axis=1)
        if np.any(norm<1e-6):raise ValueError('源动作肢体长度退化')
        delta/=norm[:,None]
        result[:,names.index(b)]=result[:,names.index(a)]+delta*np.linalg.norm(rest[b]-rest[a])
    result[:,:,2]-=result[:,[names.index('L_Foot'),names.index('R_Foot')],2].min()
    return result,scale

def install_pose_targets(cls,world,warm_start=None,root_orientation_weight=40.,limb_direction_weight=2.):
    """Keep Holosoma contacts/limits, add root and limb direction objectives."""
    import cvxpy as cp
    orientations=body_quaternions(world)
    def initialize(self,q):
        q=q.copy();q[3:7]=orientations[0]
        if warm_start is not None:
            for adr,value in warm_start.items():q[adr]=value
        # Robot rest lengths, not human segment lengths scaled by total height.
        _,points,_=self._calc_manipulator_jacobians(self.robot_model.qpos0.copy(),
            links=self.laplacian_match_links,obj_frame=False)
        targets=[]
        for a,b in SEGMENTS:
            length=np.linalg.norm(points[b]-points[a])
            direction=world[:,self.demo_joints.index(b)]-world[:,self.demo_joints.index(a)]
            norm=np.linalg.norm(direction,axis=1)
            if length<1e-6 or np.any(norm<1e-6):raise ValueError('退化肢体长度')
            targets.append((a,b,length,direction/norm[:,None]))
        self._motionfarm_segments=targets
        return q
    def objective(self,q,dqa,jac,points,frame):
        target=orientations[frame].copy()
        if np.dot(target,q[3:7])<0:target*=-1
        indices=[int(np.flatnonzero(self.q_a_indices==i)[0]) for i in range(3,7)]
        terms=[root_orientation_weight*cp.sum_squares(q[3:7]+dqa[indices]-target)]
        for a,b,length,direction in self._motionfarm_segments:
            residual=(points[b]-points[a]+(jac[b]-jac[a])@dqa)/length-direction[frame]
            terms.append(limb_direction_weight*cp.sum_squares(residual))
        return terms
    cls.initialize_pose_callback=initialize
    cls.pose_objective_callback=objective
