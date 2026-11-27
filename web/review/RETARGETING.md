# MotionFarm · Holosoma retargeting

The **动作重定向** tab is available to administrators and ordinary users. Select
“重定向” on a Gallery card, or enter an existing motion ID. Jobs run asynchronously;
closing the browser does not cancel them. Results are separate from the original
human motion. Ordinary users can only list, cancel, preview and download their
own jobs. Administrators can access all retarget jobs.

## Robot and coordinates

`unitree_g1_23dof` uses the local Humanoid ENCOS model from
`/home/humanoid/humanoid_02`. Its source URDF contains 23 movable joints; the supplied
MJCF fixes the head. The bundled Holosoma 21-DoF model is extended with the URDF's
head yaw/pitch axes, origins, limits and inertias. Holosoma's contact-marker links
are retained. Export order is **left leg (6), right leg (6), waist (1), left arm
(4), right arm (4), head yaw, head pitch**. Always read `joint_names` from the
export rather than assuming this matches another simulator's URDF traversal.

Body optimization uses Holosoma's interaction mesh, foot constraints and joint
limits. Head yaw and pitch come from the original SMPL neck/head local rotations,
projected to two axes and clamped to the robot limits. This projection is an
approximation, not a learned retargeter or dynamic controller. The web preview renders the compiled Humanoid mesh surfaces using exact MuJoCo geometry transforms, with a white ground plane, lighting and shadows. Existing NPZ trajectories are converted on demand and cached; no retargeting rerun is needed. Offline
kinematics do not establish real-robot balance, collision safety or trackability.

Input: 30 FPS MotionFarm SMPL-H records, 2–720 frames. Rest-template human height
is used rather than the height of a crouching pose. HY world positions reconstruct
root translation and grounding once, then apply `[x,y,z] -> [x,-z,y]`. The first
22 SMPL-H body joints match the SMPL-X body ordering used by Holosoma.

## Result format

Download `result.npz` using `numpy.load(path, allow_pickle=False)`:

- `qpos`: float32 `(T,30)`: world XYZ, root quaternion **wxyz**, 23 joint angles.
- `fps`: scalar 30.
- `joint_names`: ordered Unicode names, one per actuated joint.
- `metadata`: JSON string containing source motion/text IDs, text, model hash,
  converted-input hash, adapter version, coordinate convention and timestamp.
- `costs`: per-frame optimizer costs; these are not calibrated quality scores.

Results and bounded job logs live under
`outputs/text_motion_farm/review/retarget/<job_id>/`. The SQLite queue is
`review/retarget/jobs.sqlite3`. It is included in the existing data-root capacity
accounting. The current 340 GB production guard reserves space below the 350 GB
limit and also applies before a retarget job. Retarget NPZ exports are compressed;
preview JSON remains directly readable. This version does not cold-archive these
small derived outputs automatically.

## Scheduling and operations

One CPU job at a time, nice level 10, BLAS/OpenMP/Rayon threads capped at 1, no
CUDA visibility. This avoids competing for the generator's GPU; CPU work can
still consume a small amount of shared host capacity. Jobs are served by user
turn and oldest job within that user. Limits: 5 active jobs per user, 50 globally,
15 minutes per job. Cancellation terminates the isolated solver, with kill after
5 seconds if needed. The worker lock is inherited by the solver, preventing a
web restart or worker crash from starting an overlapping solver. Incomplete
jobs are retried from their source after the previous worker has actually exited.

The separate `.retarget-venv` inherits existing read-only g2h packages and adds
CVXPY 1.7.3 / Tyro 0.9.31 and solver dependencies. It does not alter g2h's packages.
Use SciPy 1.14 or newer in the worker environment for scalar-first quaternion conversion.
Vendored code is under `third_party/holosoma`, with upstream licenses retained.
Local patches: 23-DoF robot assets and an optional per-frame progress callback.

For failures inspect `review/retarget/<job_id>/engine.log` and
`review/retarget/worker.log`. User-facing job messages contain the final error.
Restart the existing review service to restart queue supervision. Do not launch
the engine directly for production jobs: that bypasses the singleton queue lock.

API: GET/POST `/api/retarget`, POST `/api/retarget/cancel`, GET
`/api/retarget/preview?id=...`, GET `/api/retarget/download?id=...`.
Session authentication and normal same-origin write checks apply to every route.

Validation: queue ownership, actual HTTP access control and CSRF, concurrent
admission, idempotency, fair queue order, coordinate conversion, finite values,
robot nq/nv, ordered joints, joint-limit tolerance, and a real MotionFarm clip.
