---
name: modelscope-studio-deploy
description: 评估、搭建、迁移和部署 ModelScope 创空间 (Studio)。当用户希望基于 ModelScope 模型制作在线 Demo、把 Hugging Face Space 迁移到魔搭、升级已有模型创空间，或部署 Gradio、Streamlit、Docker、FastAPI、静态网站时使用。也适用于创空间构建失败、运行错误、资源适配、环境变量和更新部署。普通的本地 Web 开发且没有 ModelScope 上线意图时不要触发。
---

# ModelScope 创空间部署 Skill

先评估来源、体验目标和常规资源可行性，再完成 ModelScope 创空间的实现、迁移、部署与验证。评估只生成证据和建议，不自动授予修改代码、创建创空间、申请特殊资源或公开发布的权限。

## 工作模式与参考资料

根据请求选择模式：

- `migrate_space`：给出 Hugging Face Space，希望迁移到 ModelScope。读取 [references/assessment-workflow.md](references/assessment-workflow.md)、[references/hf-space-migration.md](references/hf-space-migration.md) 和涉及 xGPU 时的 [references/xgpu-l20-profile.md](references/xgpu-l20-profile.md)；将 Space 固定到本地 revision 后运行 `scripts/inspect_hf_space.py`。
- `build_from_model`：已有明确 ModelScope 模型，希望从零搭建创空间。读取 [references/assessment-workflow.md](references/assessment-workflow.md)、[references/model-to-studio.md](references/model-to-studio.md) 和涉及 xGPU 时的 [references/xgpu-l20-profile.md](references/xgpu-l20-profile.md)；取得本地模型 snapshot 后运行 `scripts/inspect_model_repo.py`。
- `upgrade_existing_studio`：模型迭代或已有创空间需要较大升级。读取评估工作流及与来源相符的上述参考；先判断全量、推理层、UI、参考或不复用。
- `deploy_existing_project`：用户已有可部署项目，只需发布或排障。直接使用本文件的部署流程；如果模型、资源或工作负载发生实质变化，再进入评估。

正式搭建、迁移或模型升级时，在项目根目录创建 `studio-assessment.yaml`：

1. 读取 [references/assessment-schema.md](references/assessment-schema.md)，运行来源对应的只读检查脚本。脚本输出是证据候选，必须结合上下文和官方资料复核，不能直接当作兼容性结论。
2. 复制 [references/studio-assessment.template.yaml](references/studio-assessment.template.yaml) 并填写，保留事实、推断与实测的区别。
3. 依据 [references/studio-assessment.schema.json](references/studio-assessment.schema.json) 校验。
4. 运行本 Skill 目录下的 `scripts/validate_studio_records.py /absolute/path/to/studio-assessment.yaml`。脚本优先使用 PyYAML；未安装时可使用系统 Ruby/Psych。
5. 只有 `proceed` 或用户接受全部条件后的 `conditional_proceed` 才进入实现。`needs_information` 先补证据；`stop` 不部署当前方案。

部署后复制 [references/studio-verification.template.yaml](references/studio-verification.template.yaml) 生成 `studio-verification.yaml`，用 [references/studio-verification.schema.json](references/studio-verification.schema.json) 和同一脚本校验。不要用部署后结果覆写原始评估。

用户只咨询可行性时可以先口头给出简化评估；用户要求留档或问题复杂时也生成 YAML。AI 无法确认用户所指 ModelScope 模型时，索取准确的 ModelScope 模型链接，不默认改用 Hugging Face 模型源。

## 评估不变量

- 常规 xGPU 评估基线是 L20 48GB；平台 SKU、运行时 GPU 名称、compute capability 和框架可见显存分别记录。特殊申请资源不进入常规结论。
- 主要以 `recommended` 工作负载决策；每个模型的 `minimum`、`recommended`、`maximum` 来自官方文档、官方仓库或官方示例，不设跨模型固定数值。
- 不使用“FP8 一定不可迁移”“上游 xlarge 一定不可迁移”或“权重小于 48GB 一定可运行”等单点规则。
- 模型、组件、代码、许可证、运行时网络、缓存、动态 remote code 和 GPU 专用 kernel 都要有来源或明确标为未知。
- `secrets` 只记录环境变量名称、用途和使用阶段，禁止写入值。

## 前置条件

### 1. MODELSCOPE_API_TOKEN

整个流程依赖此令牌（MCP 服务获取、Git 推送、环境变量配置），必须最先确认。

```bash
# 1. 先检查环境变量
[ -n "${MODELSCOPE_API_TOKEN:-}" ] && echo "MODELSCOPE_API_TOKEN is configured" || echo "MODELSCOPE_API_TOKEN is missing"

# 2. 如果环境变量为空，只检查是否存在 ModelScope remote，不输出可能包含令牌的 URL
git remote 2>/dev/null | grep -q '^modelscope$' && echo "ModelScope remote exists"
```

如果环境变量为空但已有 ModelScope remote，可以在进程内读取凭据供当前操作使用，但不要把带凭据 URL、令牌或提取结果打印到日志或写入评估文件。

两者都没有时，引导用户：
1. 访问 https://modelscope.cn/my/myaccesstoken 获取令牌
2. `export MODELSCOPE_API_TOKEN=your_token`

### 2. 获取并配置 ModelScope MCP 服务

MCP 尚未配置前不能使用 MCP 工具，必须通过 curl 调用 HTTP API 获取部署链接。

#### 2.1 查询已有部署链接

```bash
curl -s -X GET \
  "https://modelscope.cn/openapi/v1/mcp/servers/modelscope/modelscope-mcp-server?get_operational_url=true" \
  -H "Authorization: Bearer ${MODELSCOPE_API_TOKEN}" \
  -H "Content-Type: application/json"
```

检查返回 JSON 中 `data.operational_urls`：
- 数组非空 → 取第一个条目，检查 `expiration` 字段：
  - 如果 `expiration` 早于当前时间，说明链接已过期 → 进入 2.2 重新部署
  - 未过期 → 提取 `url`、`auth_required`、`transport_type`，跳到 2.3
- 数组为空或不存在 → 进入 2.2

如果请求失败（HTTP 非 200 或 `success: false`），检查 Token 是否正确，提示用户重新获取。

#### 2.2 部署 MCP 服务（仅在 2.1 无可用链接时）

```bash
curl -s -X POST \
  "https://modelscope.cn/openapi/v1/mcp/servers/modelscope/modelscope-mcp-server/deploy" \
  -H "Authorization: Bearer ${MODELSCOPE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"transport_type": "sse"}'
```

从返回 JSON 的 `data.url` 提取部署链接。如果部署失败，向用户展示错误信息并建议稍后重试。

#### 2.3 写入本地 MCP 配置

根据当前使用的 IDE 把获取到的 URL 写入配置文件。如果 `auth_required` 为 `true`，需要添加 Authorization header，因为服务端会校验令牌身份：

配置模板：
```json
{
  "mcpServers": {
    "studio-mcp": {
      "type": "sse",
      "url": "<从 API 返回的 url>",
      "headers": {
        "Authorization": "Bearer <MODELSCOPE_API_TOKEN 的值>"
      }
    }
  }
}
```

> `auth_required` 为 `false` 时可省略 `headers` 部分。

各 IDE 配置位置：
- **Kiro**：写入 `.kiro/settings/mcp.json`
- **Cursor**：写入项目根目录 `.cursor/mcp.json` 或全局配置
- **Qoder**：打印配置信息，提示用户手动添加到 MCP 设置
- 其他客户端：按文档添加对应 `transport_type` 的 MCP 服务，不清楚怎么配置时请用户手动操作

配置完成后调用 MCP 工具 `getCurrentUser` 验证连接。如果 MCP 配置后工具调用不生效（连接超时、工具不可用等），改用 HTTP API 直接调用创空间相关接口完成全部操作，见底部「HTTP API 速查」。

### 3. Git 环境

创空间代码通过 Git 同步，确保已安装 Git 并配置用户信息。

## 部署流程

### Step 1: 确认评估结论并检查本地 Git 仓库

正式搭建、迁移或模型升级必须先确认 `studio-assessment.yaml` 已通过校验，且结论允许进入实施。普通已有项目部署或排障不强制补做模型评估。

然后检查项目是否已关联创空间，这决定后续是创建还是更新：

```bash
[ -d .git ] && git remote -v 2>/dev/null | grep modelscope.cn/studios
```

- 已有创空间远程地址 → 从 URL 提取 `owner` 和 `repo_name`，跳到 Step 3
- 没有 → 继续 Step 2

### Step 2: 分析项目并获取用户信息

#### 2.1 确定 SDK 类型

分析项目结构：

| 类型 | 检测条件 | 入口文件 | 必需配置 |
|-----|---------|---------|---------|
| `gradio` | `app.py` 导入 gradio | `app.py` | `sdk_version`, `base_image` |
| `streamlit` | `app.py` 使用 streamlit | `app.py` | `base_image` |
| `docker` | 存在 `Dockerfile` | `Dockerfile` | 无（端口必须 7860） |
| `static` | 存在 `index.html`（已构建） | `index.html` | 无 |

选择建议：
- `static` 不支持构建步骤，文件必须已构建并提交
- 需要构建的前端项目（如 `npm run build`）使用 `docker`
- 不确定时推荐 `docker`

> ⚠️ 选择 `docker` 类型时，必须提醒用户：需先在魔搭平台完成阿里云账号绑定并通过实名认证，否则无法构建 Docker 镜像。详见：https://modelscope.cn/docs/studios/docker

#### 2.2 获取用户信息

```
调用 getCurrentUser → 获取 username 作为 owner
```

`repo_name` 从项目目录名或用户指定获取。

### Step 3: 创建或更新创空间

检查创空间是否已存在：
```
调用 getStudio: owner, repo_name
```

**不存在 → 创建：**

> 创建前主动询问用户：「创空间设为公开还是私有？」用户不回答或无所谓则默认 `private: true`。

```
调用 createStudio:
- owner, repo_name, sdk_type, display_name, description
- private: 用户选择（默认 true）
- base_image: 仅 gradio/streamlit 需要
- sdk_version: 仅 gradio 需要
```

**已存在 → 更新：**
```
调用 updateStudioSettings:
- owner, repo_name
- sdk_type, base_image, sdk_version 等（按需）
```

配置参考：
- `base_image`: 推荐各项版本最新的镜像
- `sdk_version`: 推荐最新版
- `hardware`: 免费 `platform/2v-cpu-16g-mem`，xGPU 需申请（https://modelscope.cn/docs/studios/xGPU）

### Step 4: 处理敏感信息

在推送代码前，扫描所有文件中的敏感信息（API Key、Token、密码、Secret 等）。

1. 逐文件检查，找出硬编码的敏感信息
2. 将硬编码改为环境变量读取：

```python
# 错误 ❌
api_key = "sk-xxxxxxxxxxxx"

# 正确 ✅
import os
api_key = os.environ.get("API_KEY")
```

3. 汇总完整的环境变量清单（包括新增的和原有的），供 Step 6 使用

### Step 5: 同步代码到创空间

默认分支 `master`，禁止 force push。

```bash
# 1. 初始化并配置远程仓库（已有 .git 则跳过 init，已有 modelscope remote 则跳过 add）
[ -d .git ] || git init
git remote remove modelscope 2>/dev/null || true
git remote add modelscope https://oauth2:${MODELSCOPE_API_TOKEN}@www.modelscope.cn/studios/${owner}/${repo_name}.git

# 2. 大文件处理（超过 100MB 必须用 LFS）
git lfs install

# 3. 拉取远程（必须先拉后推）
git fetch modelscope master
git merge modelscope/master --allow-unrelated-histories -m "Merge remote"
```

合并冲突时保留本地版本：
```bash
git checkout --ours .
git add .
git commit -m "Resolve conflicts, keep local version"
```

提交并推送：
```bash
git add .
git commit -m "Deploy to ModelScope Studio"
git push -u modelscope master
```

### Step 6: 配置环境变量

根据 Step 4 的清单配置：

1. `调用 listStudioSecrets` 获取已配置的变量
2. 对比清单，缺失的向用户询问值
3. `调用 addStudioSecret` 配置缺失变量

### Step 7: 部署创空间

对于迁移或模型升级，在触发部署前运行本 Skill 目录下的 `scripts/audit_migration.py /absolute/path/to/project`。逐项复核高优先级 finding：修复、写入明确适配条件，或以证据说明为何不适用。静态审计通过不等于模型运行验证通过。

推送代码后，调用 `deployStudio` 触发创空间重新拉取代码并构建部署（无论当前状态是 Stopped、Running 还是 Failed 均可调用）：

```
调用 deployStudio: owner, repo_name
```

### Step 8: 监控状态和日志

```
调用 getStudio → 状态
调用 getStudioLogs → 日志
```

按 SDK 类型查看日志：
- `docker`：先查 `log_type="build"`，构建完成后查 `log_type="run"`
- 其他类型：直接查 `log_type="run"`

持续交替查看，直到 `Running` 或发现错误。

### Step 9: 自动诊断和修复

**通用错误：**

| 错误特征 | 修复方案 |
|---------|---------|
| `ModuleNotFoundError` | 添加到 requirements.txt |
| `SyntaxError` | 修复代码语法 |
| `MemoryError` / CUDA OOM | 回到评估中的工作负载、峰值显存和适配条件；不要直接假设升级或特殊申请硬件 |
| `Permission denied` | 检查文件权限 |
| 环境变量为空 | 检查 Step 6 是否配置完整 |

**Docker 特有错误：**

| 错误特征 | 修复方案 |
|---------|---------|
| 端口问题 / `Address already in use` | 确保监听 `0.0.0.0:7860`，不用 8080 |
| `COPY failed` | 检查 Dockerfile 文件路径 |
| `RUN` 步骤失败 | 检查依赖安装命令 |
| 镜像拉取失败 | 检查 FROM 基础镜像地址 |

修复流程：分析日志 → 修改代码 → 推送 → `deployStudio` → 再次检查日志

### Step 10: 完成部署

提供创空间 URL：`https://modelscope.cn/studios/${owner}/${repo_name}`

对于经过评估的搭建、迁移或升级任务，在目标运行环境中先运行本 Skill 目录下的 `scripts/runtime_probe.py`，再创建并填写 `studio-verification.yaml`。需要实测持久化目录可写性时显式使用 `--test-write`。至少验证推荐工作负载的核心流程、实际 GPU 与可见显存、峰值显存、首次及缓存启动、推理耗时、持久化缓存，以及残留的外部运行时访问。所有条件通过后才能把验证结果标记为 `passed`。

> 如果创空间为私有，提示用户：「部署已成功，当前为私有。是否需要改为公开？」确认后调用 `updateStudioSettings` 设 `private: false`。

## Docker 创空间参考

适用于 FastAPI、Golang、Node.js 等超出 Gradio/Streamlit 范畴的应用。

前置要求：需在魔搭平台完成阿里云账号绑定并通过实名认证，详见：https://modelscope.cn/docs/studios/docker

**Dockerfile 模板（Python）：**
```dockerfile
FROM python:3.10
WORKDIR /home/user/app
COPY ./ /home/user/app
RUN pip install -r requirements.txt
ENTRYPOINT ["python", "-u", "app.py"]
```

**Dockerfile 模板（Node.js）：**
```dockerfile
FROM node:18
WORKDIR /home/user/app
COPY ./ /home/user/app
RUN npm install
RUN npm run build
EXPOSE 7860
CMD ["npm", "start"]
```

端口要求：必须暴露 `0.0.0.0:7860`，禁止使用 `8080`（平台占用）

HTTP Header 限制：禁止使用 `Authorization`、`X-modelscope-*`、`X-studio-*`

## 数据持久化

- 默认每次重启数据丢失
- 持久化目录：`/mnt/workspace`
- 转移/重命名创空间时数据仍会丢失
- 高可靠性需求使用外部存储（OSS、数据库）

## 注意事项

1. 默认分支 `master`，禁止 force push
2. 超过 100MB 文件必须用 Git LFS
3. Docker 类型首次构建约 3-5 分钟，其他类型启动较快
4. 免费配额有时长限制
5. 代码中禁止硬编码敏感信息，必须通过环境变量注入
6. Docker 类型需在魔搭完成阿里云账号绑定并通过实名认证（https://modelscope.cn/docs/studios/docker）

## 工具速查

### MCP 工具（配置完成后使用）

| 操作 | 工具 |
|-----|-----|
| 获取用户信息 | `getCurrentUser` |
| 创建创空间 | `createStudio` |
| 获取创空间详情 | `getStudio` |
| 部署（启动/重启） | `deployStudio` |
| 停止 | `stopStudio` |
| 获取日志 | `getStudioLogs` |
| 更新设置 | `updateStudioSettings` |
| 环境变量 | `listStudioSecrets` / `addStudioSecret` / `updateStudioSecret` / `deleteStudioSecret` |

### HTTP API（MCP 不可用时的备选方案）

基础地址：`https://modelscope.cn/openapi/v1`，所有请求需携带 `Authorization: Bearer ${MODELSCOPE_API_TOKEN}`。

**MCP 服务管理（配置 MCP 前使用）：**

| 操作 | 方法 | 端点 |
|-----|-----|-----|
| 查询 MCP 服务详情/部署链接 | GET | `/mcp/servers/{id}?get_operational_url=true` |
| 部署 MCP 服务 | POST | `/mcp/servers/{id}/deploy` |

**创空间操作（MCP 配置后仍不生效时使用）：**

| 操作 | 方法 | 端点 | Body |
|-----|-----|-----|------|
| 获取当前用户 | GET | `/users/me` | — |
| 创建创空间 | POST | `/studios` | `{owner, repo_name, sdk_type, display_name, description, private, ...}` |
| 获取创空间详情 | GET | `/studios/{owner}/{repo_name}` | — |
| 更新创空间设置 | PATCH | `/studios/{owner}/{repo_name}/settings` | `{sdk_type, base_image, private, ...}` |
| 部署创空间 | POST | `/studios/{owner}/{repo_name}/deploy` | — |
| 停止创空间 | POST | `/studios/{owner}/{repo_name}/stop` | — |
| 获取日志 | GET | `/studios/{owner}/{repo_name}/logs/{log_type}` | — |
| 获取环境变量列表 | GET | `/studios/{owner}/{repo_name}/secrets` | — |
| 添加环境变量 | POST | `/studios/{owner}/{repo_name}/secrets` | `{key, value}` |
| 更新环境变量 | PUT | `/studios/{owner}/{repo_name}/secrets` | `{key, value}` |
| 删除环境变量 | DELETE | `/studios/{owner}/{repo_name}/secrets` | `{key}` |

完整 OpenAPI 文档：https://modelscope.cn/.well-known/openapi.json
