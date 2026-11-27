"""SOMA to the farm's fixed SMPL-H rig through global T-pose rotation transfer.

This is an explicit morphology conversion, not a rename of SOMA joint arrays.
Source arrays remain in the asset. The adapter is not a dynamics certificate.
"""
import numpy as np
from scipy.spatial.transform import Rotation

VERSION = 'soma77-smplh52-tpose-fk-v1'
BODY_MAP = ('Hips', 'LeftLeg', 'RightLeg', 'Spine1', 'LeftShin', 'RightShin',
            'Spine2', 'LeftFoot', 'RightFoot', 'Chest', 'LeftToeBase', 'RightToeBase',
            'Neck2', 'LeftShoulder', 'RightShoulder', 'Head', 'LeftArm', 'RightArm',
            'LeftForeArm', 'RightForeArm', 'LeftHand', 'RightHand')


def target_tpose_offsets(joints):
    """The farm rig uses an SMPL T rest pose; fail if its axes ever change."""
    joints = np.asarray(joints, dtype=np.float64)
    if joints.shape != (52, 3) or not np.isfinite(joints).all():
        raise ValueError('Invalid farm rest joints')
    validate_body_rest(joints[:22])
    return np.repeat(np.eye(3)[None], 22, axis=0)


def validate_body_rest(joints):
    for first, last, axis in ((16,18,(1,0,0)), (17,19,(-1,0,0)),
                              (1,4,(0,-1,0)), (2,5,(0,-1,0)), (3,9,(0,1,0))):
        direction = joints[last] - joints[first]
        if np.dot(direction, axis) / max(np.linalg.norm(direction), 1e-12) < .9:
            raise ValueError('Farm rig is not in the expected Y-up T-pose convention')
    for ankle, toe in ((7,10),(8,11)):
        direction = joints[toe] - joints[ankle]
        if direction[2] / max(np.linalg.norm(direction), 1e-12) < .7:
            raise ValueError('The source and farm rigs must share the +Z-forward rest frame')


def validate_rotations(value):
    value = np.asarray(value, dtype=np.float64)
    if value.shape[-2:] != (3, 3) or not np.isfinite(value).all():
        raise ValueError('Invalid source rotation matrices')
    if (np.max(np.abs(value.swapaxes(-1, -2) @ value - np.eye(3))) > 2e-3
            or np.max(np.abs(np.linalg.det(value) - 1)) > 2e-3):
        raise ValueError('Source rotations are not SO(3)')
    # Official float32 exports accumulate small orthogonality errors along the
    # chain. Project only already-validated rotations before changing hierarchy.
    return Rotation.from_matrix(value.reshape(-1, 3, 3)).as_matrix().reshape(value.shape)


def fk(local, joints, parents):
    local = validate_rotations(local)
    joints = np.asarray(joints, dtype=np.float64)
    parents = np.asarray(parents, dtype=int)
    frames, count = local.shape[:2]
    if joints.shape != (count, 3) or parents.shape != (count,) or parents[0] != -1:
        raise ValueError('Invalid target rig')
    world = np.empty((frames, count, 3), dtype=np.float64)
    rotations = np.empty_like(local)
    rotations[:, 0] = local[:, 0]
    world[:, 0] = joints[0]
    for i in range(1, count):
        p = parents[i]
        if not 0 <= p < i:
            raise ValueError('Target hierarchy must be topologically ordered')
        rotations[:, i] = rotations[:, p] @ local[:, i]
        world[:, i] = world[:, p] + np.einsum('tij,j->ti', rotations[:, p], joints[i] - joints[p])
    return world, rotations


def convert(output, source_names, source_joints, joints, parents, fps=30, preserve_root=False, *, exact_root=False):
    """Transfer body rotations; synthesize unarticulated fingers; recompute target FK."""
    local_source = np.asarray(output['local_rot_mats'])
    source_global = validate_rotations(output['global_rot_mats'])
    root = np.asarray(output['root_positions'], dtype=np.float64)
    if (source_global.ndim != 4 or local_source.shape != source_global.shape
            or root.shape != (len(source_global), 3) or not np.isfinite(root).all()
            or not 2 <= len(root) <= 3600 or float(fps) != 30):
        raise ValueError('Invalid SOMA motion shape, duration or frame rate')
    if len(source_names) != source_global.shape[1] or len(set(source_names)) != len(source_names):
        raise ValueError('Invalid SOMA joint names')
    indices = [source_names.index(name) for name in BODY_MAP]
    source_joints = np.asarray(source_joints, dtype=np.float64)
    if source_joints.shape != (len(source_names), 3) or not np.isfinite(source_joints).all():
        raise ValueError('Invalid source rest joints')
    validate_body_rest(source_joints[indices])
    target_tpose_offsets(joints)
    # Both native FK rigs use Y-up/+Z-forward neutral T-pose axes. SOMA's
    # standard_t_pose_global_offsets_rots is a BVH bone-axis export convention,
    # NOT the transform between these two native global rotation frames.
    target_global = source_global[:, indices]
    parents = np.asarray(parents, dtype=int)
    if parents.shape != (52,):
        raise ValueError('Expected the farm SMPL-H 52-joint rig')
    local = np.broadcast_to(np.eye(3), (len(root), 52, 3, 3)).copy()
    local[:, 0] = target_global[:, 0]
    for i in range(1, 22):
        if not 0 <= parents[i] < i:
            raise ValueError('Invalid body hierarchy')
        local[:, i] = target_global[:, parents[i]].swapaxes(-1, -2) @ target_global[:, i]
    # Fingers are deliberately fixed, not hallucinated motion channels.
    points, rebuilt_global = fk(local, joints, parents)
    if np.max(np.abs(rebuilt_global[:, :22] - target_global)) > 1e-5:
        raise ValueError('Body rotation transfer failed')
    translation = root - np.asarray(joints)[0]
    # Uniform vertical morphology correction only; preserve the original XY-plane
    # trajectory (X/Z in Y-up) and never apply per-frame floor snapping.
    source_points = np.asarray(output['posed_joints'], dtype=np.float64)
    if source_points.shape != (len(root), len(source_names), 3) or not np.isfinite(source_points).all():
        raise ValueError('Invalid source joint positions')
    foot_ids = [source_names.index(n) for n in ('LeftFoot', 'LeftToeBase', 'RightFoot', 'RightToeBase')]
    src_floor = source_points[:, foot_ids, 1].min()
    target_floor = (points[:, (7, 10, 8, 11)] + translation[:, None])[:, :, 1].min()
    if exact_root:
        vertical_shift = 0.
    elif preserve_root:
        hips_idx = source_names.index('Hips')
        soma_sole = float(source_joints[foot_ids, 1].min() - source_joints[hips_idx, 1])
        human_sole = float(np.asarray(joints, dtype=np.float64)[[7, 8, 10, 11], 1].min()
                           - np.asarray(joints, dtype=np.float64)[0, 1])
        vertical_shift = soma_sole - human_sole
    else:
        vertical_shift = float(src_floor - target_floor)
    if abs(vertical_shift) > .35:
        raise ValueError('Unexpected morphology height correction; inspect source convention')
    translation[:, 1] += vertical_shift
    poses = Rotation.from_matrix(local.reshape(-1, 3, 3)).as_rotvec().reshape(len(root), 156)
    arrays = {'fps': np.asarray(30), 'native_keypoints3d': points.astype(np.float32),
              'native_transl': translation.astype(np.float32),
              'native_root_rotations_mat': local[:, 0].astype(np.float32),
              'native_rot6d': local[..., :2].reshape(len(root), 52, 6).astype(np.float32),
              'smpl_poses': poses.astype(np.float32), 'smpl_trans': translation.astype(np.float32),
              'smpl_betas': np.zeros((1, 16), dtype=np.float32), 'smpl_mocap_framerate': np.asarray(30),
              'smpl_num_frames': np.asarray(len(root)), 'smpl_Rh': poses[:, :3].astype(np.float32)}
    for key, value in output.items():
        value = np.asarray(value)
        if value.dtype.kind not in 'biuf' or (value.dtype.kind in 'f' and not np.isfinite(value).all()):
            raise ValueError('Unexpected source output: ' + key)
        arrays['kimodo_' + key] = value
    return arrays, {'version': VERSION, 'source_skeleton': 'somaskel77',
                    'target_skeleton': 'farm_smplh52', 'method': 'shared_neutral_frame_rotation_transfer_and_target_fk',
                    'vertical_shift_m': vertical_shift, 'root_xz_scaled': False,
                    'finger_motion': 'fixed_identity_unverified', 'dynamics_verified': False}
