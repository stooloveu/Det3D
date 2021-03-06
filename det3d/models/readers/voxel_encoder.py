import time

import numpy as np
import torch
from det3d.models.utils import Empty, change_default_args, get_paddings_indicator
from torch import nn
from torch.nn import functional as F

from .. import builder
from ..registry import READERS


@READERS.register_module
class VFELayer(nn.Module):
    def __init__(self, in_channels, out_channels, use_norm=True, name="vfe"):
        super(VFELayer, self).__init__()
        self.name = name
        self.units = int(out_channels / 2)
        if use_norm:
            BatchNorm1d = change_default_args(eps=1e-3, momentum=0.01)(nn.BatchNorm1d)
            Linear = change_default_args(bias=False)(nn.Linear)
        else:
            BatchNorm1d = Empty
            Linear = change_default_args(bias=True)(nn.Linear)
        self.linear = Linear(in_channels, self.units)
        self.norm = BatchNorm1d(self.units)

    def forward(self, inputs):
        # [K, T, 7] tensordot [7, units] = [K, T, units]
        voxel_count = inputs.shape[1]
        x = self.linear(inputs)
        x = self.norm(x.permute(0, 2, 1).contiguous()).permute(0, 2, 1).contiguous()
        pointwise = F.relu(x)
        # [K, T, units]

        aggregated = torch.max(pointwise, dim=1, keepdim=True)[0]
        # [K, 1, units]
        repeated = aggregated.repeat(1, voxel_count, 1)

        concatenated = torch.cat([pointwise, repeated], dim=2)
        # [K, T, 2 * units]
        return concatenated


@READERS.register_module
class VoxelFeatureExtractor(nn.Module):
    def __init__(
        self,
        num_input_features=4,
        use_norm=True,
        num_filters=[32, 128],
        with_distance=False,
        voxel_size=(0.2, 0.2, 4),
        name="VoxelFeatureExtractor",
    ):
        super(VoxelFeatureExtractor, self).__init__()
        self.name = name
        if use_norm:
            BatchNorm1d = change_default_args(eps=1e-3, momentum=0.01)(nn.BatchNorm1d)
            Linear = change_default_args(bias=False)(nn.Linear)
        else:
            BatchNorm1d = Empty
            Linear = change_default_args(bias=True)(nn.Linear)
        assert len(num_filters) == 2
        num_input_features += 3  # add mean features
        if with_distance:
            num_input_features += 1
        self._with_distance = with_distance
        self.vfe1 = VFELayer(num_input_features, num_filters[0], use_norm)
        self.vfe2 = VFELayer(num_filters[0], num_filters[1], use_norm)
        self.linear = Linear(num_filters[1], num_filters[1])
        # var_torch_init(self.linear.weight)
        # var_torch_init(self.linear.bias)
        self.norm = BatchNorm1d(num_filters[1])

    def forward(self, features, num_voxels, coors):
        # features: [concated_num_points, num_voxel_size, 3(4)]
        # num_voxels: [concated_num_points]
        # t = time.time()
        # torch.cuda.synchronize()

        points_mean = features[:, :, :3].sum(dim=1, keepdim=True) / num_voxels.type_as(
            features
        ).view(-1, 1, 1)
        features_relative = features[:, :, :3] - points_mean
        if self._with_distance:
            points_dist = torch.norm(features[:, :, :3], 2, 2, keepdim=True)
            features = torch.cat([features, features_relative, points_dist], dim=-1)
        else:
            features = torch.cat([features, features_relative], dim=-1)
        voxel_count = features.shape[1]
        mask = get_paddings_indicator(num_voxels, voxel_count, axis=0)
        mask = torch.unsqueeze(mask, -1).type_as(features)
        # mask = features.max(dim=2, keepdim=True)[0] != 0

        # torch.cuda.synchronize()
        # print("vfe prep forward time", time.time() - t)
        x = self.vfe1(features)
        x *= mask
        x = self.vfe2(x)
        x *= mask
        x = self.linear(x)
        x = self.norm(x.permute(0, 2, 1).contiguous()).permute(0, 2, 1).contiguous()
        x = F.relu(x)
        x *= mask
        # x: [concated_num_points, num_voxel_size, 128]
        voxelwise = torch.max(x, dim=1)[0]
        return voxelwise


@READERS.register_module
class VoxelFeatureExtractorV2(nn.Module):
    def __init__(
        self,
        num_input_features=4,
        use_norm=True,
        num_filters=[32, 128],
        with_distance=False,
        voxel_size=(0.2, 0.2, 4),
        name="VoxelFeatureExtractor",
    ):
        super(VoxelFeatureExtractorV2, self).__init__()
        self.name = name
        if use_norm:
            BatchNorm1d = change_default_args(eps=1e-3, momentum=0.01)(nn.BatchNorm1d)
            Linear = change_default_args(bias=False)(nn.Linear)
        else:
            BatchNorm1d = Empty
            Linear = change_default_args(bias=True)(nn.Linear)
        assert len(num_filters) > 0
        num_input_features += 3
        if with_distance:
            num_input_features += 1
        self._with_distance = with_distance

        num_filters = [num_input_features] + num_filters
        filters_pairs = [
            [num_filters[i], num_filters[i + 1]] for i in range(len(num_filters) - 1)
        ]
        self.vfe_layers = nn.ModuleList(
            [VFELayer(i, o, use_norm) for i, o in filters_pairs]
        )
        self.linear = Linear(num_filters[-1], num_filters[-1])
        # var_torch_init(self.linear.weight)
        # var_torch_init(self.linear.bias)
        self.norm = BatchNorm1d(num_filters[-1])

    def forward(self, features, num_voxels, coors):
        # features: [concated_num_points, num_voxel_size, 3(4)]
        # num_voxels: [concated_num_points]
        points_mean = features[:, :, :3].sum(dim=1, keepdim=True) / num_voxels.type_as(
            features
        ).view(-1, 1, 1)
        features_relative = features[:, :, :3] - points_mean
        if self._with_distance:
            points_dist = torch.norm(features[:, :, :3], 2, 2, keepdim=True)
            features = torch.cat([features, features_relative, points_dist], dim=-1)
        else:
            features = torch.cat([features, features_relative], dim=-1)
        voxel_count = features.shape[1]
        mask = get_paddings_indicator(num_voxels, voxel_count, axis=0)
        mask = torch.unsqueeze(mask, -1).type_as(features)
        for vfe in self.vfe_layers:
            features = vfe(features)
            features *= mask
        features = self.linear(features)
        features = (
            self.norm(features.permute(0, 2, 1).contiguous())
            .permute(0, 2, 1)
            .contiguous()
        )
        features = F.relu(features)
        features *= mask
        # x: [concated_num_points, num_voxel_size, 128]
        voxelwise = torch.max(features, dim=1)[0]
        return voxelwise


@READERS.register_module
class VFEV3_ablation(nn.Module):
    def __init__(self, num_input_features=4, norm_cfg=None, name="VFEV3_ablation"):
        super(VFEV3_ablation, self).__init__()
        self.name = name
        self.num_input_features = num_input_features

    def forward(self, features, num_voxels, coors=None):
        points_mean = features[:, :, [0, 1, 3]].sum(
            dim=1, keepdim=False
        ) / num_voxels.type_as(features).view(-1, 1)
        points_mean = torch.cat(
            [points_mean, 1.0 / num_voxels.to(torch.float32).view(-1, 1)], dim=1
        )

        return points_mean.contiguous()


@READERS.register_module
class VoxelFeatureExtractorV3(nn.Module):
    def __init__(
        self, num_input_features=4, num_raw_features=0, norm_cfg=None, name="VoxelFeatureExtractorV3",
    ):
        super(VoxelFeatureExtractorV3, self).__init__()
        self.name = name
        self.num_input_features = num_input_features
        self.num_raw_features = num_raw_features

    def forward(self, features, num_voxels, coors=None, with_unnormalized_xyz = False):
        if with_unnormalized_xyz:
            features = features[:, :, 3:]
        if self.num_raw_features > 0:
            points_mean = features[:, :, : self.num_raw_features].sum(
                dim=1, keepdim=False
            ) / num_voxels.type_as(features).view(-1, 1)
            features_max = features[:, :, self.num_raw_features :].max(
                dim=1, keepdim=False
            )[0]
            features = torch.cat([points_mean, features_max], dim=-1)
        else:
            features = features[:, :, : self.num_input_features].sum(
                dim=1, keepdim=False
            ) / num_voxels.type_as(features).view(-1, 1)

        return features.contiguous()

@READERS.register_module
class VoxelFeatureExtractorV4(nn.Module):
    def __init__(
        self, 
        num_input_features=4, 
        num_raw_features=0, 
        with_distance=False,
        with_elevation=False,
        norm_cfg=None, 
        voxel_size=(0.2, 0.2, 4),
        pc_range=(0, -40, -3, 70.4, 40, 1),
        name="VoxelFeatureExtractorV4",
    ):
        super(VoxelFeatureExtractorV4, self).__init__()
        self.name = name
        self.num_input_features = num_input_features
        self.num_raw_features = num_raw_features

        self.vx = voxel_size[0]
        self.vy = voxel_size[1]
        self.vz = voxel_size[2]
        self.x_offset = self.vx / 2 + pc_range[0]
        self.y_offset = self.vy / 2 + pc_range[1]
        self.z_offset = self.vz / 2 + pc_range[2]
        self._with_distance = with_distance
        self._with_elevation = with_elevation

    def forward(self, features, num_voxels, coors=None, with_unnormalized_xyz = False):
        device = features.device
        dtype = features.dtype
        features_all = []

        assert with_unnormalized_xyz

        if self.num_raw_features > 0:
            points_mean = features[:, :, : self.num_raw_features+3].sum(
                dim=1, keepdim=False
            ) / num_voxels.type_as(features).view(-1, 1)
            features_max = features[:, :, self.num_raw_features+3 :].max(
                dim=1, keepdim=False
            )[0]
            features_mean = torch.cat([points_mean, features_max], dim=-1)

        else:
            features_mean = (features[:, :, : self.num_input_features].sum(
                dim=1, keepdim=False
            ) / num_voxels.type_as(features).view(-1, 1))

        # f_center = torch.zeros([features.shape[0], 3], dtype=dtype, device=device)
        f_center = torch.zeros_like(features_mean[:, :3])
        f_center[:, 0] = coors[:, 3].to(dtype) * self.vx + self.x_offset
        f_center[:, 1] = coors[:, 2].to(dtype) * self.vy + self.y_offset
        f_center[:, 2] = coors[:, 1].to(dtype) * self.vz + self.z_offset
        f_center = features_mean[:, :3] - f_center

        f_center[:, 0] = 2 * f_center[:, 0] / self.vx
        f_center[:, 1] = 2 * f_center[:, 1] / self.vy
        f_center[:, 2] = 2 * f_center[:, 2] / self.vz
        features_all.append(f_center)

        if self._with_elevation:
            r = torch.norm(features_mean[:, :2], dim=-1, keepdim=True)
            phi = torch.atan2(r, features_mean[:, 2].view_as(r))
            features_all.append(phi)

        features_mean = features_mean[:, 3:]

        if self._with_distance:
            points_dist = torch.norm(features_mean[:, :3], dim=-1, keepdim=True)
            features_all.append(points_dist)

        features_all.append(features_mean)
        features = torch.cat(features_all, dim=-1)

        return features.contiguous()


@READERS.register_module
class SimpleVoxel(nn.Module):
    """Simple voxel encoder. only keep r, z and reflection feature.
    """

    def __init__(self, num_input_features=4, norm_cfg=None, name="SimpleVoxel"):

        super(SimpleVoxel, self).__init__()

        self.num_input_features = num_input_features
        self.name = name

    def forward(self, features, num_voxels, coors=None):
        # features: [concated_num_points, num_voxel_size, 3(4)]
        # num_voxels: [concated_num_points]
        points_mean = features[:, :, : self.num_input_features].sum(
            dim=1, keepdim=False
        ) / num_voxels.type_as(features).view(-1, 1)
        feature = torch.norm(points_mean[:, :2], p=2, dim=1, keepdim=True)
        # z is important for z position regression, but x, y is not.
        res = torch.cat([feature, points_mean[:, 2 : self.num_input_features]], dim=1)
        return res
