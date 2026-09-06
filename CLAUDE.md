# CLAUDE.md

本仓库的完整代理工作规范见 **[AGENTS.md](AGENTS.md)**(文档/Git/代码/DB/EPUB 标准、迭代收尾与会话启动流程)。下面是 Claude Code 的速查,细节以 AGENTS.md 为准。

@AGENTS.md

## 红线(违反即返工)

1. 本地优先:不引入必须联网的路径。
2. 不重解析:下游只读已入库数据,不重新解析源文件、不 OCR。
3. 引用可定位:检索/对齐结果必须带页码锚点 + 字符区间。
4. 页码不虚构:EPUB 只用出版方页码,无则标 `uncalibrated`。
5. 向量不混用:嵌入数据必带 `model_id`,禁跨模型混向量空间。
6. `persistence/` 内禁止 `from ..` 上行 import(有边界测试钉死)。
7. 禁止 force push `main`。

## 会话启动

先按 AGENTS.md §5 只读现有 `.project-memory/` 并校对工作区,不初始化或修订归档。自动读档后继续当前用户任务,不据此执行旧 TODO;仅用户单独要求“读档”时报告状态后停止。用户要求继续项目工作时,才从归档下一步选任务。

## 常用命令(权威:AGENTS.md §3.6)

全量测试(发布门禁,与 CI 一致,仓库根执行,无需 PYTHONPATH):

```bash
.venv-windows/Scripts/python.exe -m unittest discover -t . -s tests
```

单模块:

```bash
.venv-windows/Scripts/python.exe -m unittest tests.test_alignment_anchor_gates
```

启动无头 Web 服务(端口 8765):

```bash
$env:PYTHONPATH="D:\ME_Finder\src"; & D:\ME_Finder\.venv-windows\Scripts\python.exe -m me_finder serve --host 127.0.0.1 --port 8765
```

## 迭代收尾(AGENTS.md §4)

仅已授权的仓库实施迭代执行:通过现有测试与提交门禁 → 更新相关文档 → 提交并推送 → 存档与校验。归档若纳入版本控制,须移到最终提交前完成;若保持本地忽略,可在提交后存档。只读审阅、答疑及待审批方案不写入或推送。自检仅要求本次应提交改动无遗漏,不得为清空状态处理既有用户文件。外部阻塞时完成其余可执行工作并如实报告未完成项,不绕过门禁。**不再手写 `docs/agent/SESSION.md`**,交接由 `project-save-load` 归档承载。
