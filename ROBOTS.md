# Built-in Robots

Kinora includes built-in retargeting support for **Unitree G1 (23 DoF)** (`unitree_g1_23dof`), and also supports importing custom robots via URDF/MJCF in the Robot Editor.

- `unitree_g1_23dof`: Unitree G1 · 23 DoF (12 leg, 1 waist, 10 arm/wrist; wrists fixed at neutral).
- Optional accelerated retargeting runtime (`kinora_retarget_accel` wheel): when installed in `.retarget-venv`, `unitree_g1_23dof` automatically uses the accelerated solver with automatic fallback to the standard offline kinematic solver.
