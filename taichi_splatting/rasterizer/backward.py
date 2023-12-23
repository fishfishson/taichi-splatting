
from dataclasses import dataclass
from functools import cache
import taichi as ti
from taichi.math import ivec2


from taichi_splatting.data_types import Gaussian2D
from taichi_splatting.ti.covariance import conic_pdf_with_grad

@dataclass(frozen=True)
class Config:
  tile_size: int = 16
  clamp_max_alpha: float = 0.99
  alpha_threshold: float = 1. / 255.
  saturate_threshold: float = 0.9999


@cache
def backward_kernel(config: Config, feature_size: int):

  feature_vec = ti.types.vector(feature_size, dtype=ti.f32)
  tile_size = config.tile_size

  @ti.kernel
  def _backward_kernel(
      points: ti.types.ndarray(Gaussian2D.vec, ndim=1),  # (M, 6)
      point_features: ti.types.ndarray(feature_vec, ndim=1),  # (M, F)
      
      # (TH, TW, 2) the start/end (0..K] index of ranges in the overlap_to_point array
      tile_overlap_ranges: ti.types.ndarray(ti.math.ivec2, ndim=1),
      # (K) ranges of points mapping to indexes into points list
      overlap_to_point: ti.types.ndarray(ti.i32, ndim=1),
      
      # saved from forward
      image_feature: ti.types.ndarray(feature_vec, ndim=2),  # (H, W, F)
      image_alpha: ti.types.ndarray(ti.f32, ndim=2),       # H, W
      image_last_valid: ti.types.ndarray(ti.i32, ndim=2),  # H, W

      # input gradients
      grad_image_feature: ti.types.ndarray(feature_vec, ndim=2),  # (H, W, F)

      # output gradients
      grad_points: ti.types.ndarray(Gaussian2D.vec, ndim=1),  # (M, 6)
      grad_point_features: ti.types.ndarray(feature_vec, ndim=1),  # (M, F)
  ):

    camera_height, camera_width = image_feature.shape

    tile_area = ti.static(tile_size * tile_size)
    tiles_wide, tiles_high = camera_width // tile_size, camera_height // tile_size

    # put each tile_size * tile_size tile in the same CUDA thread group (block)
    ti.loop_config(block_dim=(tile_area))
    for tile_v, tile_u, v, u in ti.ndrange(tiles_high, tiles_wide, tile_size, tile_size):
      
      pixel = ivec2(tile_u, tile_v) * tile_size + ivec2(u, v) 
      tile_id = tile_u + tile_v * tiles_wide
      thread_id = u + v * tile_size

      # The initial value of accumulated alpha (initial value of accumulated multiplication)
      T_i = 1.0
      accum_feature = feature_vec(0.)

      # open the shared memory
      tile_point = ti.simt.block.SharedArray((tile_area, ), dtype=Gaussian2D.vec)
      tile_feature = ti.simt.block.SharedArray((tile_area, ), dtype=feature_vec)

      start_offset, end_offset = tile_overlap_ranges[tile_id]
      tile_point_count = end_offset - start_offset

      last_point_idx = image_last_valid[pixel.y, pixel.x]
      accumulated_alpha: ti.f32 = image_alpha[pixel.y, pixel.x]
      T_i = 1.0 - accumulated_alpha  

      #  T_i = \prod_{j=1}^{i-1} (1 - a_j) \\
      #  \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} \\
      #  \sum_{j=i+1}^{n} c_j a_j T(j) \\
      #  \text{let } w_i = \sum_{j=i+1}^{n} c_j a_j T(j) \\
      #  w_n = 0 \\
      #  w_{i-1} = w_i + c_i a_i T(i) \\
      #  \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} w_i \\

      w_i = ti.math.vec3(0.0)
      grad_pixel_feature = grad_image_feature[pixel.y, pixel.x]

      num_point_groups = (tile_point_count + ti.static(tile_area - 1)) // tile_area

      # Loop through the range in groups of tile_area
      for point_group_id in range(num_point_groups):
        group_offset_base = point_group_id * tile_area

        block_end_idx = end_offset - group_offset_base
        block_start_idx = ti.max(block_end_idx - tile_area, 0)

        load_index = block_end_idx - thread_id - 1
        if load_index >= block_start_idx:
          point_idx = overlap_to_point[load_index]

          tile_point[thread_id] = points[point_idx]
          tile_feature[thread_id] = point_features[point_idx]

        ti.simt.block.sync()

        point_group_size = ti.min(
          tile_area, tile_point_count - group_offset_base)
            
        for in_group_idx in range(point_group_size):
          point_group_offset = group_offset_base + in_group_idx

          point_index = end_offset - point_group_offset - 1
          if point_index >= last_point_idx :
              continue

          uv, uv_conic, point_alpha = Gaussian2D.unpack(tile_point[in_group_idx])
          feature = tile_feature[in_group_idx]

          gaussian_alpha, dp_dmean, dp_dconic = conic_pdf_with_grad(
            ti.cast(pixel, ti.f32) + 0.5, uv, uv_conic)
          
          alpha = point_alpha * gaussian_alpha
          
          # from paper: we skip any blending updates with 𝛼 < 𝜖 (we choose 𝜖 as 1
          # 255 ) and also clamp 𝛼 with 0.99 from above.
          if alpha < ti.static(config.alpha_threshold):
            continue

          alpha = ti.min(alpha, ti.static(config.clamp_max_alpha))
          T_i = T_i / (1. - alpha)

          # \frac{dC}{da_i} = c_i T(i) - \frac{1}{1 - a_i} w_i
          alpha_grad_from_feature = (feature * T_i - w_i / (1. - alpha)
                                     ) * grad_pixel_feature

          # w_{i-1} = w_i + c_i a_i T(i)
          w_i += feature * alpha * T_i
          alpha_grad: ti.f32 = alpha_grad_from_feature.sum()

          point_offset = overlap_to_point[point_index]
          point_grad_feature = alpha * T_i * grad_pixel_feature

          # alpha_grad * point_alpha is dp
          # (2,) as the paper said, view space gradient is used for detect candidates for densification

          # TODO: accumulate in block shared feature and write to global memory
          grad_point_features[point_offset] += point_grad_feature
          grad_points[point_offset] += alpha_grad * Gaussian2D.pack(
              point_alpha * dp_dmean, 
              point_alpha * dp_dconic,
              gaussian_alpha)

        # end of point group loop
      # end of point group id loop

      image_feature[pixel.y, pixel.x] = accum_feature

      # No need to accumulate a normalisation factor as it is exactly 1 - T_i
      image_alpha[pixel.y, pixel.x] = 1. - T_i    
      image_last_valid[pixel.y, pixel.x] = last_point_idx

    # end of pixel loop

  return _backward_kernel



