# 常规 xGPU 评估基线

常规模型创空间以 ModelScope L20 48GB xGPU 为团队评估基线。该基线不是平台永久 SKU 保证：每次评估记录申请/声明的硬件标识，部署后再记录运行时实际 GPU。

## 需要分别记录的信息

部署前：

- 平台资源 SKU 或申请项原文
- 预期 GPU：NVIDIA L20
- 名义显存：48GB
- CPU、内存、磁盘和时长等平台同时给出的限制
- 信息来源与采集日期

部署后：

- `nvidia-smi` 返回的准确 GPU 名称
- CUDA compute capability
- 框架实际可见的显存总量（GiB）
- PyTorch、CUDA、Python 和关键 kernel 版本
- `/mnt/workspace` 是否可写、剩余空间
- 推荐工作负载下的峰值显存和推理时间

NVIDIA 将 L20 列为 Ada Lovelace、48GB GPU；涉及架构能力时查阅最新的 [NVIDIA L20 官方规格](https://www.nvidia.com/en-us/data-center/l20/)。平台 SKU、驱动和可见显存仍以当次 ModelScope 页面与运行时实测为准。

## 解释规则

- 名义 48GB 不等于框架恰好可分配 48GiB，应保留运行时和临时峰值余量。
- FP8 不是自动阻断项；检查具体 dtype、算子、kernel、框架版本和 fallback 路径。
- Blackwell 专属 FP4/NVFP4、SM 特定 kernel 或预编译产物需要单独判定。
- 权重能加载不等于完整推理能运行；必须覆盖 attention、VAE/decoder 和输出阶段峰值。
- 特殊申请的更高规格 GPU 不进入常规基线，也不能用于证明 L20 方案可行。
