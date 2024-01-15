#include <pybind11/pybind11.h>
#include <torch/extension.h>
#include <cub/cub.cuh>

template<typename T>
__global__ void complete_cumsum(T *input, T *output, T *total) {
  T const v = *output + *input;
  *total = *(output + 1) = v;
}


template <typename T>
T full_cumsum_helper(T *input, int64_t input_size, T *output, torch::ScalarType input_scalar_type) {
  void *d_temp_storage = nullptr;
  size_t temp_storage_bytes = 0;
  cub::DeviceScan::ExclusiveSum(d_temp_storage, temp_storage_bytes, input, output, input_size);

  // Make temp storage on in the torch mempool.
  auto temp_storage_tensor_options = torch::TensorOptions().dtype(torch::kUInt8).device(torch::kCUDA);
  auto temp_storage_tensor = torch::empty({int64_t(temp_storage_bytes)}, temp_storage_tensor_options);
  assert(temp_storage_tensor.nbytes() >= temp_storage_bytes);
  d_temp_storage = temp_storage_tensor.data_ptr<uint8_t>();

  cub::DeviceScan::ExclusiveSum(d_temp_storage, temp_storage_bytes, input, output, input_size);

  // cumsum is now in the output but the final value is missing.

  // Make storage in pinned_memory.
  auto total_tensor_options = torch::TensorOptions().dtype(input_scalar_type).pinned_memory(true);
  auto total_tensor = torch::empty({int64_t(1)}, total_tensor_options);

  // Now we run a kernel that will complete the full_cumsum.  The
  // final element of input gets added to the penultimate element of
  // output and written into the final element of output.  It also
  // gets written into total_tensor.
  complete_cumsum<<<1,1>>>(&input[input_size-1],
                           &output[input_size-1],
                           total_tensor.data_ptr<T>());
  // complete_cumsum must finish before we can read the total_tensor.
  cudaDeviceSynchronize();
  return *total_tensor.data_ptr<T>();
}

// full cumsum and also returns the total to the CPU.
int64_t full_cumsum(const torch::Tensor input, const torch::Tensor output) {
  assert(input.size(0) + 1 == output.size(0));
  switch (input.scalar_type()) {
    case torch::kInt32:
      return full_cumsum_helper(input.data_ptr<int32_t>(), input.size(0),
                                output.data_ptr<int32_t>(), input.scalar_type());
      break;
    case torch::kInt64:
      return full_cumsum_helper(input.data_ptr<int64_t>(), input.size(0),
                                output.data_ptr<int64_t>(), input.scalar_type());
      break;
    default:
      // TODO, add all the other cases.
      throw std::runtime_error("Not yet implemented for data type.");
      break;
  }
}

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("full_cumsum", &full_cumsum, "Full cumulative sum");
}