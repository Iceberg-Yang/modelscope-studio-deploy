# 创空间评估工作流

本参考用于正式迁移 Hugging Face Space、基于 ModelScope 模型搭建创空间，或升级已有创空间。用户只咨询概念且不需要留档时，可以口头完成简化评估；进入正式实施时，必须在项目根目录生成 `studio-assessment.yaml`。

## 两阶段记录

- 部署前使用 `studio-assessment.yaml`，记录当时可获得的事实、推断、未知项、决定和实施条件。
- 部署后使用 `studio-verification.yaml`，记录目标环境实测结果，并通过 `assessment_id` 关联原评估。
- 不用部署后的结果覆写原评估。源代码、模型 revision、目标资源配置或工作负载目标发生实质变化时，将原评估标记为 `stale`，只重评受影响项目。
- 从相应 template 复制文件，并用同目录 JSON Schema 与 `scripts/validate_studio_records.py` 校验。
- 字段含义和四个检查脚本的统一输出协议见 [assessment-schema.md](assessment-schema.md)。脚本只产生证据候选，AI 复核后再写入评估记录。

## 评估顺序

1. 明确请求模式：`migrate_space`、`build_from_model` 或 `upgrade_existing_studio`。
2. 固定源 revision，收集代码、配置、Model Card、官方文档、官方仓库和运行记录。HF Space 使用 `scripts/inspect_hf_space.py`，本地 ModelScope 模型 snapshot 使用 `scripts/inspect_model_repo.py`。
3. 定义核心体验与工作负载包络：`minimum`、`recommended`、`maximum`。数值来自该模型的官方资料或官方示例，不设置跨模型的统一分辨率、时长、上下文或输出长度。
4. 按组件盘点模型、精度、远程代码、依赖、许可证和外部下载。
5. 分别评估显存、GPU 架构、精度、CUDA kernel、框架、运行语义、网络、存储、模型来源和许可证。
6. 记录风险、未知项、复用方案、适配动作和可验证的成功条件。
7. 根据本页决策逻辑给出结论。不要用权重大小、FP8 或上游 GPU 档位单独替代完整评估。
8. 实施适配后运行 `scripts/audit_migration.py`；部署后运行 `scripts/runtime_probe.py`，再填写验证记录。

## 证据与事实类型

每条关键结论必须区分：

- `observed`：直接见于代码、配置、Model Card、官方文档或日志。
- `inferred`：由已有事实推导的工程判断。
- `measured`：在明确环境中实际测得。

发生冲突时保留所有证据，不静默覆盖。参与决策的默认优先级为：

1. 目标环境实测结果
2. 锁定 revision 的源码、配置和依赖文件
3. 官方文档、Model Card 和官方仓库
4. 上游 Space 声明
5. 第三方案例
6. 技术推断

证据必须记录稳定 locator、revision（若存在）、采集时间和简短摘要。推断填写 `confidence` 与 `evidence_refs`；无法证明的内容写入 `unknowns`，不要编造精确值。

## 兼容性状态

每个维度只使用以下状态：

- `compatible`：已有足够证据表明在推荐包络内可直接使用。
- `adaptation_required`：存在明确、可实施且可验证的适配路径。
- `blocked`：在当前目标与约束内没有合规可行路径。
- `unknown`：证据不足。
- `not_applicable`：该维度不适用。

建议至少覆盖：`memory`、`gpu_architecture`、`precision`、`cuda_kernels`、`model_source`、`framework`、`runtime_semantics`、`network`、`storage`、`license`。

## 决策逻辑

- 任一核心维度为 `blocked` 且无替代方案：`stop`。
- 存在 `critical: true` 的未解决未知项：`needs_information`。
- 没有阻断或关键未知项，但需要适配：`conditional_proceed`。
- 核心维度均兼容，非核心未知项不影响核心体验：`proceed`。

`conditional_proceed` 的每个条件必须包含动作、原因、验证方法、成功标准和失败后的处理。诸如“优化显存”或“检查兼容性”不是可验收条件。

决定是基于证据得出的建议，不代表获得了修改代码、创建创空间、申请特殊资源或公开发布的授权。仅当用户明确要求搭建、迁移或部署时，才进入实施。

## ModelScope 模型来源约束

`build_from_model` 的前提是用户已有 ModelScope 模型。优先从用户提供的信息或搜索结果确认准确模型链接和 revision。AI 没有找到时，向用户索取 ModelScope 模型链接；不要把 Hugging Face 模型源当成默认兜底，也不要因为搜索失败推断模型不存在。

## 资源估算

将官方声明、工程估算和目标环境实测分开记录。显存至少考虑：权重、激活、attention、VAE/decoder、临时峰值和安全余量；磁盘至少考虑下载体积、安装后体积和缓存；启动至少考虑首次下载和编译。

常规评估以 L20 48GB xGPU 为基线，但必须把平台声明的 SKU 与运行时实际观测的 GPU 名称、compute capability、可见显存分别记录。特殊申请资源不进入常规可行性结论，也不能由 Skill 自动申请或切换。

## 外部依赖与安全

盘点模型下载、GitHub 安装、CDN、字体、媒体、API、数据库、动态 remote code 和模型索引中的远程引用，并标记它们发生在 build、startup 还是 runtime。正式迁移默认优先 ModelScope 模型源和持久化缓存。

`secrets` 只记录变量名、用途和使用阶段，永远不记录值。评估与验证文件都不得包含 token、密码、cookie 或私钥。
