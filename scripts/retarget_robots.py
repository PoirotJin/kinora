"""Explicit robot allowlist shared by queue, solver, and mesh endpoints."""
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_ROBOT = 'unitree_g1_23dof'
ROBOTS = {
    'unitree_g1_23dof': dict(
        label='Unitree G1 · 23 DoF',
        dof=23,
        family='g1',
        height=1.39,
        model='motionfarm/unitree_g1_23dof.xml',
        asset='unitree_g1_23dof',
        note='G1 23-DoF humanoid configuration with independent joint mapping and limits.',
    ),
}


def robot_spec(name=DEFAULT_ROBOT):
    if not isinstance(name, str) or name not in ROBOTS:
        raise ValueError('不支持的机器人模型')
    return dict(ROBOTS[name], id=name)


def model_path(name=DEFAULT_ROBOT):
    return PROJECT / 'third_party/holosoma/holosoma_retargeting/models' / robot_spec(name)['model']


def catalog():
    return [dict(id=k, label=v['label'], dof=v['dof'], note=v['note']) for k, v in ROBOTS.items()]


def joint_mapping(spec, defaults):
    return dict(defaults[('smplx', spec['family'])])
