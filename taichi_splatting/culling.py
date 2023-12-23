import taichi as ti
from taichi.math import vec3, mat4
import torch

from taichi_splatting.data_types import CameraParams
from taichi_splatting.ti.projection import point_to_image



@ti.kernel
def frustum_culling_kernel(
    pointcloud: ti.types.ndarray(vec3, ndim=1),  # (N, 3)

    T_image_world: ti.types.ndarray(mat4, ndim=1),  # (1, 4, 4)
    output_mask: ti.types.ndarray(ti.u1, ndim=1),  # (N), output
    
    near_plane: ti.f32,
    far_plane: ti.f32,

    image_size: ti.math.ivec2,
    margin_pixels: ti.i32,
):    
    # filter points in camera
    for point_id in range(pointcloud.shape[0]):
        pixel, depth = point_to_image(
            position=pointcloud[point_id],
            T_image_world=T_image_world[0],
        )

        output_mask[point_id] = (depth > near_plane and 
            depth < far_plane and 
            pixel.x >= -margin_pixels and pixel.x < image_size.x + margin_pixels and 
                pixel.y >= -margin_pixels and pixel.y < image_size.y + margin_pixels)


def frustum_culling(pointcloud: torch.Tensor, camera_params: CameraParams, margin_pixels: int):
  mask = torch.zeros(pointcloud.shape[0], dtype=torch.bool, device=pointcloud.device)

  frustum_culling_kernel(
    pointcloud=pointcloud.contiguous(),
    T_image_world=camera_params.T_image_world,
    output_mask=mask,

    near_plane=camera_params.near_plane,
    far_plane=camera_params.far_plane,
    image_size=ti.math.ivec2(camera_params.image_size),
    
    margin_pixels=margin_pixels
  )

  return mask
    
    