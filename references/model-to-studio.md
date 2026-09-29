# 从 ModelScope 模型搭建创空间

本流程只用于已有明确 ModelScope 模型的请求。AI 搜索不到准确模型时向用户索取链接，不自动改用 Hugging Face 模型。

## 搜索参考实现

1. 固定 ModelScope 模型 URL、revision、Model Card、许可证、权重与组件结构。
2. 对固定 revision 的本地 snapshot 运行 `scripts/inspect_model_repo.py`，将结构化结果作为证据候选；脚本无法确认官方身份或推荐工作负载。
3. 搜索是否存在基于该模型的 Hugging Face Space，并按迁移评估检查其代码是否适合参考。
4. 查看 Model Card 中的官方示例、官方 GitHub 仓库、推理脚本、Notebook、在线 Demo 和推荐框架。
5. 如果是已有模型的迭代版本，寻找上一版本创空间或实现并评估复用范围。

参考资料优先级为官方模型资料、官方仓库和官方示例。第三方实现只能补充工程思路，不能覆盖官方接口或许可证事实。

## 选择体验与运行形态

先定义用户实际要体验的核心流程，再选择资源：

- 纯展示或静态交互：`static`。
- 调用外部 API、不在本地加载模型：CPU Gradio、Streamlit 或 Docker。
- 常规本地模型推理：通常使用 Gradio + xGPU。
- 需要自定义系统依赖、服务协议或前后端构建：Docker；同时确认平台认证和端口要求。
- 非交互批处理或不适合公开体验的长任务：先向用户说明限制，不为了“有页面”强行包装。

根据官方文档或官方仓库，为当前模型定义 `minimum`、`recommended`、`maximum` 工作负载。L20 48GB 上不能复现官方最大规格时，可以缩小上下文、分辨率、帧数、时长、batch 或输出长度，但必须确认推荐包络仍体现模型核心能力。

## 模型组件与实现

按 transformer、VAE/decoder、text encoder、tokenizer/processor、辅助模型等组件记录来源、revision、dtype、权重大小、remote code 和许可证。不要假设主模型仓库包含全部组件。

优先复用已经跑通的官方调用路径。开发前检查：

- 框架与模型 revision 是否匹配；
- `trust_remote_code` 是否还会动态加载其他文件；
- 模型配置或 index 是否引用外部 repo；
- 需要哪些精度、attention backend 和自定义 kernel；
- 下载、解压、编译后的磁盘与启动成本；
- 是否需要单并发、流式输出、取消任务或进度显示；
- 非商业或限制性许可证是否已在公开 Demo 中清楚展示。

## 迭代模型复用

不要因为名称相近就直接替换 model id。比较架构、tokenizer/processor、prompt contract、依赖、输入输出、资源需求、缓存和许可证后，选择：

- `full`：实现与 UI 基本不变，只替换模型及少量示例。
- `inference_only`：复用加载和推理封装，重做 UI 或工作流。
- `ui_only`：保留交互形式，重写推理实现。
- `reference_only`：仅借鉴结构和经验。
- `none`：没有可靠可复用部分。

记录复用候选的 URL、revision、关系、可复用与需替换部分、缓存兼容性和署名要求。升级已有创空间时，只有缓存兼容且回滚路径明确，才考虑原地更新。
