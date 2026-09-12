# 对齐计算组件隔离与工作进程决策

2026-09-12:把对齐"已有成果展示 / 任务管理 / 模型与计算依赖"三条职责的边界钉死;是否把生成移入独立工作进程,按 0.5.4 真实库基线测量结果决策。

## 事实(基线引用)

- 真实库基线(`reports/performance-real-v0.5.4-2026-09-11.json`):对齐期间 OS 峰值 RSS 1,304 MiB,约为普通搜索(98 MiB)的 13 倍;对齐期间 503 占重叠请求 30.5%(73/239),源于同进程内 IndexRuntime suspend/reopen 写窗口;首轮真实 MiniLM 计算每轮 115–122 秒,复用向量后 4–5 秒。
- 计算依赖 `fastembed`/`onnxruntime` 只在 `FastEmbedEmbeddingProvider.__call__` 内惰性导入;模型文件是托管组件(`components/text-alignment/models`,设置页下载),数据库中的成果(`alignment_links`、segment set、人工 override)与组件目录天然分离。

## 本轮钉死的边界

1. **不安装组件也能搜索与阅读**:`tests/test_alignment_component_isolation.py` 在屏蔽 `fastembed`/`onnxruntime` 的前提下验证搜索、`list_alignment_targets`、已存链接读取全部可用。
2. **任务管理先行校验**:`TextAlignmentCoordinator.generate` 在启动任务前检查 `model_component_installed`;缺组件时以本地明确错误失败(提示到设置 → 译本对齐 下载),**绝不**在任务内部触发隐藏联网下载——与"在线动作仅用户主动触发"的项目原则一致。
3. **安装后离线生成**:下载探针 `download_embedding_model` 即计算栈本地校验;HF_HUB_OFFLINE 下的生成已有性能基线与对齐测试覆盖。
4. **卸载不删成果**:删除组件目录后,已存链接、对照目标列表与搜索全部不变(测试钉死)。
5. **管理层与算法解耦**:`managed_embedding_models` 不再在模块顶层导入 `semantic_alignment`(探针改为函数内惰性导入);组件管理(summary/状态)在无计算栈时可用。

## 工作进程决策(按测量)

**0.5.4 保持生成在宿主进程内,不引入独立工作进程。**

- 收益侧:独立进程能把 1.3 GiB 峰值与 suspend/reopen 503 窗口与交互搜索隔离,直指 30.5% 可用性损失。
- 成本侧:需要进度/取消/结果的进程间通道与打包变更(PyInstaller 已有 MCP sidecar 先例,可复用);而 503 的更便宜修法(收窄 suspend/reopen 写窗口,见 `docs/issues/search-unavailable-during-alignment.md`)尚未尝试,不能跳过便宜方案直接付进程化的固定成本。
- 内存侧:1.3 GiB 为瞬态峰值且任务后回落,16 GiB 目标机型可承受;不是当前最痛指标。
- **触发条件**:若写窗口收窄后真实库 503 率仍显著(>5%),再迁移工作进程——本轮已把计算收敛到 coordinator + `embed_text_sequences` + provider 三层接缝,迁移不触碰展示与任务管理代码。

## 验证

`tests/test_alignment_component_isolation.py` 5 项;既有对齐套件(test_text_alignment / overrides / runtime / anchor_gates)77 项全绿。

## 2026-09-12 复核与修正：冷启动、完整缓存和离线边界

**事实**：原测试预先导入了业务模块，只屏蔽 FastEmbed/ONNX Runtime，未覆盖 NumPy 缺失的冷启动。实际 `web → backup → alignment_snapshots → semantic_alignment` 以及页码锚点模块会在启动阶段加载 NumPy。原来的目录非空判断也会把仅含 `blobs/*.incomplete` 的残缺下载视为已安装；生成时没有强制 `local_files_only`，可能隐式联网修复。

本次将 NumPy 导入移到实际数值计算处，保持已验证算法和阈值不变。已有链接照常定位；缺少 NumPy 时跳过可选的缓存向量细化，使用已有链接和锚点。模块传递依赖损坏等其他导入错误仍抛出。

模型安装状态改为核对活动 HF snapshot／受支持 archive 缓存中的必要非空文件，包括 E5 外部权重 `model.onnx_data`。回执不能代替文件完整性；损坏缓存允许在设置中重新下载。文件预检不等于模型内容校验，实际 ONNX 加载仍决定模型是否有效。生成默认 `local_files_only=True`，只有设置中的明确下载动作使用 False；模型在但数值运行时不在时，生成在进入写入协调前给出明确提示。

验证分两层：`test_core_without_alignment` 在新进程禁止三种计算依赖，覆盖 HTTP 搜索、阅读页面、已有对照目标和定位、管理状态、可选向量缓存缺失降级、生成拒绝与关闭；同一测试另在**无第三方包的空白虚拟环境**实跑。CI 新增独立空白环境步骤。完整环境的原有数值算法套件和真实离线模型验收继续保留。

`requirements-core.txt` 提供不含计算栈的源码后端依赖，`requirements-alignment.txt` 提供可选数值运行时；既有 macOS/Windows 完整构建仍包含计算运行时。**尚未实现设置内下载 Python 二进制运行时的独立安装器，也未发布精简桌面包**，不能把依赖隔离等同于完整插件产品。

**对工作进程的判断（修正推断）**：迁移计算进程可以改善 CPU/内存隔离，但若仍通过同样的 `suspend/reopen` 发布结果，503 不会自动消失。必须先测量和缩短搜索不可用窗口，再按数据决定进程化；”>5%”只是上文提出的工程目标，不是质量实验得出的阈值。

## 2026-09-12 后续：写窗口收窄已实施，进程化触发条件未触发

同日 `search-unavailable-during-alignment.md` 记录的修复取消了对齐写窗口内的 suspend/reopen（对齐不写搜索可见数据，活引擎跨事务继续服务）。修复前探针测得不可用窗口约 2.2 s/次（准备 663 ms + 发布 1,549 ms）；修复后同口径 0 次不可用，正式同快照复测 503 归零（见 [复测报告](../../reports/performance-real-alignment503-fix-2026-09-12.md)）。上文”>5% 触发工作进程”的条件没有发生：0.5.4 维持对齐生成在宿主进程内，`coordinator + embed_text_sequences + provider` 三层接缝保持不变。对齐计算期间成功搜索的延迟受同进程 GIL 竞争影响仍偏高（另一议题，不在该修复范围内）。
