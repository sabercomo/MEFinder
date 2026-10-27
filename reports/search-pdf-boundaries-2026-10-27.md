# 搜索查询与 PDF 提取边界核验（0.6.1，2026-10-27）

## 范围与基线

本轮从 `df4156b` 开始，落实架构评估的第 2、3 项。只调整职责归属，不修改检索评分、召回阈值、出版方页码规则、schema 或 HTTP/MCP 接口，不重解析用户文献库。

| 位置 | 调整前 | 调整后 |
|---|---:|---:|
| persistence 外直接 SQL | 10 个文件 / 72 处 | 8 个文件 / 68 处 |
| search_recall.py | 475 行 | 398 行 |
| search_assembly.py | 394 行 | 361 行 |
| persistence/passage_reads.py | 96 行 | 239 行 |
| pdf_extractors.py | 1,826 行 | 883 行 |
| pdf_native.py | 无 | 571 行 |
| pdf_paragraphs.py | 无 | 429 行 |
| pdf_extraction_types.py | 无 | 22 行 |

## 第 2 项：搜索查询归属

**事实**：精确/标准化、忽略空格/标点的候选读取，以及搜索结果上下文读取，统一进入现有 `persistence/passage_reads.py`。业务层仍负责候选预算、命中区间映射、排序和“跳过跨页窗口、选择相邻真实 PDF 页”的规则。持久化函数接收原连接并返回 Cursor；不新开连接、不改事务，也不把邻居扫描改成 `fetchall()`。

原 SQL 的行序、预算加一哨兵、短词扫描的 unary `+`、正文位置索引、源文献/作品组过滤和 EPUB 格式区分均保留。内部列名仍由召回层现有白名单选择，未增加通用查询容器。

初次搜索回归 7 项因短词预筛 spy 仍指向旧模块而报 `AttributeError`；将测试替换目标改为新的查询所属模块后通过，原断言及响应期望未降低。

## 第 3 项：PDF 提取职责

- `pdf_extractors.py` 保留导入编排、解析缓存接入、书目和页码映射协调。
- `pdf_native.py` 负责 PyMuPDF 与内置简易解析器的探测、原生页面读取。
- `pdf_paragraphs.py` 只消费页面记录，构造段落、跨页窗口、块位置及 Unicode 码点锚点；不打开 PDF 或读取解析缓存。
- `pdf_extraction_types.py` 承载两侧共用的页面记录与异常类型，避免互相引用主模块。旧 `pdf_extractors` 导入入口通过显式重导出继续可用，不复制实现。

**事实**：迁移的 28 个函数/类定义通过 AST 逐项比对，与 `df4156b` 完全一致。进一步合并四个模块的定义清单核对，原有 43 个函数/类均保留且各只有一份实现；只有主模块内 MinerU profile 的底层解析器调用改为指向实际归属模块；相应测试替换目标也随之更新。新增边界守卫钉住模块单向依赖、兼容入口身份和段落层不读取源文件，既有行数预算同步收紧。

打包路径核查：新模块通过静态 import 可被 PyInstaller 跟踪，`tools/slim_main_package.py:worker_source_datas` 递归收集包内 Python 源码。本轮未实际重打安装包，不能据此声称冻结程序验收完成。

## 行为证据与验证

1. 移动代码前先新增 `test_pdf_extraction_contract.py` 并生成 `fixtures/pdf_extraction_contract.json`，与原搜索金标准、候选预算和短词查询用例合跑 20 项通过。随后不再重新生成 PDF 快照；原搜索金标准也未改动。
2. PDF 快照比较完整输出，覆盖 PyMuPDF 原生页、内置解析器完整导入、人工页码、MinerU 跨页拆分、非 BMP 字符与 CRLF、双开与混合映射、空白页；仅归一临时目录、时间和已安装解析器版本。页码、原文、哈希、块坐标、字符区间均保留比较。
3. 把跨页后页窗口临时改成 1 个字符，完整 PDF 快照测试产生断言失败、无执行错误，确认能检出定位行为变化。变异只在独立进程内生效，不写回源码。
4. 搜索专项：`unittest discover -t . -s tests -p 'test_search*.py'`，139 项通过（1 项条件跳过，6.120 秒）。包含旧完整响应金标准、FTS/非 FTS 候选预算、短词预筛对照、上下文和查询计划验证；本轮不主张性能提升。
5. PDF/相关集成/架构专项 99 项通过（7 跳过），新增 PDF 边界加架构/搜索依赖专项 22 项通过。
6. Chrome 16 项通过（55.842 秒）；macOS 全量 `PYTHONUTF8=1 .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests` 共 2,837 项通过（39 项条件跳过，184.218 秒），含前端装配与相关守卫。Ruff 排除既有未入库 `.codex-tmp` 后通过，`git diff --check` 通过。

本地测试日志位于 `.codex-tmp/architecture-followup-0.6.1/`，不入库。0.6.1 尚未打包、安装或发布；Windows/Intel 实机及原生双窗口人工验收不在本轮完成范围内。
