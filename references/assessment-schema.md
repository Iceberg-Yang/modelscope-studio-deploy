# 评估记录与检查脚本协议

本文件是 `studio-assessment.yaml`、`studio-verification.yaml` 与四个只读检查脚本之间的人类可读契约。机器结构分别以同目录的 JSON Schema 为准；跨字段决策由 `scripts/validate_studio_records.py` 校验。

## 记录边界

- `studio-assessment.yaml` 保存部署前已知事实、推断、风险、未知项和建议，不保存部署后结果。
- `studio-verification.yaml` 保存目标创空间的实测结果，通过 `assessment_id` 关联原评估。
- 检查脚本只生成证据候选，不直接修改上述文件，也不替代 AI 对官方资料、工作负载和兼容性的判断。
- 正式实施时把评估与验证文件放在项目根目录。脚本报告默认输出到 stdout；需要留档时由调用方显式使用 `--output`。

## 评估字段

### `assessment`

记录唯一 ID、状态、完整度、评估时间、已检查 revision 和目标资源档案版本。

- `status`: `draft`、`ready`、`stale`
- `completeness`: `incomplete`、`complete`
- 当源 revision、模型 revision、目标资源、基础镜像或工作负载发生实质变化时设为 `stale`。

### `request` 与 `source`

`request.mode` 只允许：

- `migrate_space`，对应 `source.kind: huggingface_space`
- `build_from_model`，对应 `source.kind: modelscope_model`
- `upgrade_existing_studio`，对应 `source.kind: existing_modelscope_studio`

从模型搭建时必须有准确 ModelScope 模型链接。搜索不到时向用户索取，不自动改为 Hugging Face 模型源。

### `evidence`

每条证据需要：`id`、`kind`、`source_type`、`source`、`locator`、`revision`、`observed_at`、`summary`。

- `observed`：来自锁定 revision 的源码、配置、官方资料或日志。
- `inferred`：由多个事实推导出的工程结论。
- `measured`：在明确运行环境中实际测得。

脚本 finding 是 `observed` 证据候选。AI 应检查上下文后再写入 `evidence`，不要机械复制误报。

### `workload`

以 `recommended` 包络作为主要评估与验收对象。`minimum`、`recommended`、`maximum` 均需写明参数与官方证据引用，不使用跨模型统一数值。

### `models`

按组件记录主模型、transformer、VAE/decoder、text encoder、tokenizer/processor 和辅助模型。每个组件分别记录来源、revision、dtype、体积、许可证、remote code 和是否必需。

### `target_runtime` 与 `resource_requirements`

`declared`、`estimated`、`measured` 不可混写。常规目标是 L20 48GB，但平台 SKU、预期 GPU、运行时准确 GPU 名称、compute capability 与框架可见显存必须分开记录。

显存估算至少覆盖权重、激活、attention、VAE/decoder、临时峰值和安全余量；权重能加载不代表完整推理可运行。

### `compatibility`

每个维度使用：`compatible`、`adaptation_required`、`blocked`、`unknown`、`not_applicable`。脚本只能指出信号，不能仅凭 FP8、上游 GPU 档位或权重体积直接填写最终状态。

### `risks`、`unknowns` 与 `decision`

- 风险必须有证据、缓解方式、验证动作和成功标准。
- 无法证明的关键问题写入 `unknowns`，不要伪造精确值。
- 存在核心阻断项时为 `stop`；存在关键未知项时为 `needs_information`；需要明确适配时为 `conditional_proceed`；核心维度均有充分兼容证据时才为 `proceed`。
- `conditional_proceed` 的每个条件必须具有动作、原因、验证、成功标准和失败处理。

### `secrets`

只允许 `name`、`purpose`、`required_at`。严禁 token、密码、cookie、私钥或其他值。

## 检查脚本统一输出

四个脚本输出 UTF-8 JSON：

```json
{
  "schema_version": "1.0",
  "report_type": "hf_space_inspection",
  "generated_at": "2026-01-01T00:00:00Z",
  "tool": {"name": "inspect_hf_space.py", "version": "1.0"},
  "target": {"path": "/absolute/path", "revision": "git-sha-or-null"},
  "facts": {},
  "findings": [],
  "unknowns": [],
  "summary": {"files_scanned": 0, "finding_counts": {}}
}
```

finding 统一字段：

```json
{
  "id": "stable-finding-id",
  "rule_id": "zero_gpu_decorator",
  "category": "runtime_semantics",
  "severity": "high",
  "kind": "observed",
  "message": "发现 Hugging Face ZeroGPU 调度装饰器",
  "locator": {"path": "app.py", "line": 42},
  "excerpt": "@spaces.GPU(...) ",
  "assessment_paths": ["source_runtime", "compatibility.runtime_semantics"]
}
```

约束：

- `severity` 只表示复核优先级：`info`、`low`、`medium`、`high`；不是最终兼容性结论。
- `excerpt` 必须压缩并脱敏。疑似凭据只能报告变量名、文件和行号，不得输出值。
- `id` 由规则、相对路径和行号稳定生成，便于重复扫描对比。
- `unknowns` 记录脚本无法确定、需要官方资料或实测补充的问题。
- 扫描成功返回 0，即使发现高优先级信号；输入无效、解析失败或运行探测失败才返回非零。

## 各脚本职责

### `inspect_hf_space.py`

输入本地 HF Space 代码目录。提取 README frontmatter、SDK、入口、许可证、模型候选与依赖，并检查 ZeroGPU、FP8/FP4、Blackwell/SM 特定 kernel、AoTI、compile/export、FlashAttention、remote code、HF/GitHub 运行时来源、examples 和队列信号。

### `audit_migration.py`

输入已经适配的项目目录。检查残留 ZeroGPU、HF/GitHub 模型或运行时依赖、端口与监听地址、非持久化缓存、动态 remote code、自动示例、并发、署名/许可证和疑似硬编码秘密。它是迁移后静态审计，不证明模型一定能运行。

### `inspect_model_repo.py`

输入本地 ModelScope 模型 snapshot。提取 config、model index、权重文件、架构、dtype、组件、remote code、量化配置、许可证、README 官方链接和依赖信号。它不联网确认仓库身份；ModelScope URL 与 revision 仍需由 AI 或用户提供。

### `runtime_probe.py`

在目标环境中记录 Python、PyTorch、CUDA、GPU、compute capability、可见显存、驱动、磁盘和 `/mnt/workspace` 状态。默认不写磁盘；只有显式传入 `--test-write` 时，才创建并立即删除临时文件来实测持久化目录可写性。它不运行模型推理，也不读取环境变量值。

## 调用顺序

```text
inspect source
→ AI核查官方资料并填写 studio-assessment.yaml
→ validate_studio_records.py
→ 用户授权后实施
→ audit_migration.py
→ 部署
→ runtime_probe.py
→ 填写并校验 studio-verification.yaml
```
