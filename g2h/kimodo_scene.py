"""Shared world-space route and pose constraints for Kimodo."""
import math


def _number(value, limit):
    return type(value) in (int, float) and abs(value) <= limit and math.isfinite(value)


def _vector(value, size, limit):
    return isinstance(value, list) and len(value) == size and all(_number(v, limit) for v in value)


def make_constraints(pipeline, route_frames, pose_frames, path, heading, origin, positions, rotations):
    import numpy as np
    import torch
    from kimodo.constraints import FullBodyConstraintSet, Root2DConstraintSet

    def tensor(value, dtype=torch.float32):
        return torch.as_tensor(value, dtype=dtype, device=pipeline.device)

    class TerminalPose(FullBodyConstraintSet):
        # Earlier multiprompt segments have no seated keyframes. Kimodo's
        # condition writer cannot reshape an empty set of indexed values.
        def crop_move(self, start, end):
            keep = (self.frame_indices >= start) & (self.frame_indices < end)
            return type(self)(self.skeleton, self.frame_indices[keep] - start,
                              self.global_joints_positions[keep], self.global_joints_rots[keep],
                              self.smooth_root_2d[keep])

        def update_constraints(self, data_dict, index_dict):
            if self.frame_indices.numel():
                super().update_constraints(data_dict, index_dict)

    class SceneRoute(Root2DConstraintSet):
        def crop_move(self, start, end):
            keep = (self.frame_indices >= start) & (self.frame_indices < end)
            return type(self)(self.skeleton, self.frame_indices[keep] - start,
                              self.smooth_root_2d[keep],
                              global_root_heading=self.global_root_heading[keep])

        def update_constraints(self, data_dict, index_dict):
            if self.frame_indices.numel():
                super().update_constraints(data_dict, index_dict)

    route = SceneRoute(
        pipeline.skeleton, tensor(route_frames, torch.long),
        tensor(path[route_frames] - origin[[0, 2]]),
        global_root_heading=tensor(np.column_stack((np.cos(heading[route_frames]), np.sin(heading[route_frames])))))
    if not len(pose_frames):
        return [route], origin, float(heading[0])
    terminal = TerminalPose(
        pipeline.skeleton, tensor(pose_frames, torch.long),
        tensor(positions), tensor(rotations))
    return [route, terminal], origin, float(heading[0])
