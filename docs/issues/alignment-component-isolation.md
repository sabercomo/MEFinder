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
