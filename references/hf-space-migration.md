# Hugging Face Space 迁移评估

迁移目标是保留核心体验，同时让依赖、运行语义和资源需求适配 ModelScope 创空间；不是逐行复制上游实现。

## 先固定上游事实

记录 Space URL、revision、SDK、入口、许可证、模型与依赖。检查 README frontmatter、依赖锁文件和源码中的：

- `@spaces.GPU`、ZeroGPU `size` 与动态 GPU duration
- `float8`、FP8、NVFP4/FP4、Blackwell 或 compute capability 判断
- AoTI、`torch.compile`、`torch.export`、SM 特定产物
- FlashAttention、xFormers、自定义 CUDA extension
- `trust_remote_code`、动态 sibling loader、模型索引中的远程组件
- `huggingface_hub`、`hf_hub_download`、GitHub 安装和运行时网络请求
- examples 是否自动触发推理、并发队列、可变模型/VAE cache

先在固定 revision 的本地副本运行 `scripts/inspect_hf_space.py` 生成结构化证据候选，再阅读命中位置确认上下文。脚本不访问网络，也不根据单个信号给出迁移结论。

HF ZeroGPU 的上游档位只是资源事实，不是迁移结论。当前官方硬件与配额定义应在评估时重新查阅 [ZeroGPU 官方文档](https://huggingface.co/docs/hub/main/spaces-zerogpu)，不要永久写死在判断代码中。

## 判断 GPU 特性是否可迁移

不要使用“FP8 即不可迁移”的规则。区分：

- 通用 FP8 权重或算子是否被目标 PyTorch/CUDA/GPU 支持；
- kernel 是否锁定特定 SM；
- 是否依赖 Blackwell 专属 FP4/NVFP4 能力；
- 是否使用针对特定 GPU 编译的 AoTI 产物；
- 是否存在 BF16/FP16 eager 等便携路径，以及它的显存代价。

上游使用完整 96GB GPU 也不自动等于不可迁移。需要按推荐工作负载估算或实测峰值，并评估 tiling、offload、量化、缩短输入、降低输出、单并发等方案是否仍能保留核心体验。

## 迁移动作

- 移除 ZeroGPU 调度装饰器和 HF 专属队列假设，重新定义 ModelScope 上的并发与超时。
- 优先将模型及组件解析到 ModelScope 本地 snapshot；检查配置文件内部是否仍引用 HF repo。
- 启动时不可靠的 GitHub clone 或 git dependency，应改为固定版本包、随仓库携带的必要代码或可复现依赖。
- 使用 `/mnt/workspace` 缓存大型模型；区分首次下载、启动加载和单次推理时间。
- 对带可变 cache 的 pipeline 限制并发，避免示例或页面加载隐式触发后台生成。
- 如果上游 HF Space 使用黄色主色主题，迁移到 ModelScope 时默认改用 Gradio `gr.themes.Soft()` 的蓝紫色主题，并检查文字、按钮和状态提示的对比度。黄色若属于模型或项目的明确品牌识别，不要擅自改色，先保留或向用户确认。
- 保留上游许可证、署名和必要的非官方迁移说明。

完成后使用 `studio-verification.yaml` 记录实际 GPU、可见显存、峰值显存、启动时间、推理时间、缓存命中以及仍残留的外部网络访问。

在部署前对完成适配的项目运行 `scripts/audit_migration.py`，复核残留 ZeroGPU、HF/GitHub 依赖、缓存、端口、并发、署名和疑似秘密。不要把“没有静态命中”解释为运行兼容已经证明。
