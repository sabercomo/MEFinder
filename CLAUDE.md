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

先执行 `project-save-load` 技能的 **`读档`**(从 `.project-memory/` 恢复状态),再 `git log --oneline -15` 校对,然后开工。详见 AGENTS.md §5。

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

测试全绿 → 更新文档 → 提交(`type(scope): 中文标题`)→ 执行 `存档`(写 `.project-memory/`)→ 自检工作区干净。**不再手写 `docs/agent/SESSION.md`**,交接由 `project-save-load` 归档承载。
