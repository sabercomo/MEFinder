# 本地 OCR 组件 — v0.4.7 实施方案

**状态：方案已定，按本文件实施。** 质量校准仍需真实语料，但不再阻断页图 runner、结果契约、恢复和索引接入。

## 1. 目标与边界

本地 OCR 补足非 `native_text` PDF 在没有可用云解析服务时的离线路径。首版支持：

- NDLOCR-Lite 1.2.3：现代日文横排、竖排扫描件。
- NDL古典籍OCR-Lite 1.4.3：日文古籍与繁体竖排古籍。
- 原生文本 PDF 仍走 PyMuPDF；用户显式选择 MinerU 或视觉 API 时仍尊重显式选择。
- 不宣传现代简体或现代繁体中文 OCR 能力。

首版只承诺组件契约、结构化结果、完整页覆盖、可恢复任务和失败回退。没有校准/留出数据前，不宣称自动路由达到某个准确率。

## 2. 核心决定：OCR 永远只接页图

MEFinder 负责把 PDF 页渲染成确定性 PNG；两个 OCR runner 只接收 PNG，永远不接收 PDF。

这一决定同时解决：

1. NDLOCR-Lite 的 PDF 分支即使使用 `--json-only` 仍生成 `_text.pdf`；页图入口完全绕开该分支。
2. 古籍版 CLI 只支持图片；统一页图后两个 runner 具有相同输入契约。
3. 原 PDF 始终是唯一文献副本；OCR 输出只保存 JSON 和 MEFinder 的规范化结果。

渲染参数是任务指纹的一部分。OCR 组件不读取原 PDF 路径，只读取 MEFinder 工作目录中的页图；组件路径与页图工作路径的 Unicode 兼容性仍按 spike 单独验证，不在实现前臆测为已解决。

## 3. 不建立 provider registry

本功能只有两个固定候选，不建立全局 registry。复用现有注入点：

- `ParserProvider`：runner 适配和规范化。
- `LargeDocumentJobEngine`：物理分片、ledger、重试、页覆盖验证和断点恢复。
- 现有 manifest publisher 与 `parser_results` attachment：让索引器消费结构化结果。

稳定标识：

| 用途 | 现代版 | 古籍版 |
|---|---|---|
| parser/provider ID | `ndlocr-lite` | `ndlkotenocr-lite` |
| 显示名 | `NDL 日文 OCR` | `NDL 古籍 OCR` |
| 上游版本 | `1.2.3` | `1.4.3` |

导入路由标识统一为 `local_ocr`；具体 provider ID 写入任务进度、manifest 和解析统计。

## 4. Runner 契约

每个已配置组件包含：

- 独立 Python 可执行文件。
- 上游 `ocr.py`。
- 独立依赖和模型目录。
- 固定版本、启用状态及可选权重摘要。

调用方式：

```text
<python> <ocr.py> --sourceimg <page.png> --output <page-output> [--json-only]
```

现代版追加 `--json-only`；古籍版不支持该参数。每页单独调用，输出目录只允许出现该页的 JSON。进程超时、非零退出、缺 JSON 或 JSON 结构错误均快速失败；取消任务时终止当前子进程，不再启动后续页面。

## 5. 规范化契约

上游每行的四点 polygon 映射为 PDF 坐标系中的轴对齐框：

```text
[min_x, min_y, max_x, max_y]
```

映射按渲染图像宽高与 PDF 页宽高分别缩放。`isVertical` 不作为事实来源：现代版由框高宽推断，古籍版字段恒为字符串 `"true"`，因此两边统一使用几何推断，并把原始值保存在 provenance。

每个物理页必须产生一个 `NormalizedPage`：

- 有文本：按上游数组顺序生成 `reading_order`，同时保留原始行 ID、检测 confidence 和原始 polygon。
- 空白页：生成空文本、零 block 的页，并带 `blank_page` warning。
- 非空插图页允许零文字，但整本文档零文字时视为 runner/模型失败，不发布 attachment。

`confidence` 仍是检测分数，只写入 provenance，不能作为识别准确率。

## 6. 自动选择与回退

路由优先级：

```text
native_text                     -> native
显式 MinerU / 视觉 API          -> 用户选择
其他 PDF + 已启用本地 OCR       -> local_ocr
local_ocr 结构失败               -> 现有 MinerU / vision 回退
未安装本地 OCR                  -> 旧路由不变
```

候选规则保持保守且可解释：

- 无论启用一个还是两个组件，都先对分层抽取的非空白页做探针。
- 横排页面只有检测到明确的日文假名信号时才使用现代版。
- 探针明显以竖排为主：古籍版覆盖没有明显劣势时使用古籍版；否则只有检测到日文信号才使用现代版。
- 抽样页既没有日文信号也不是竖排版式：不启动本地 OCR，沿现有路径改用 MinerU。
- 不会根据两个不同检测器的 confidence 直接比大小。

这只是稳定的产品路由规则，不是 OCR 准确率证明。阈值必须在修订后的 spike 中用校准集选择、留出集验收。

## 7. 发布与索引契约

每个完成的物理分片写入规范化 NDJSON。合并器先验证物理页从 1 到总页数完整且不重复，然后 publisher 生成：

- `content_list.json`：索引器现有结构化入口。
- parser manifest：parser/provider ID、版本、源 SHA-256、总页数、分片范围和结果目录。
- `config/pdf_imports.json` 的 `parser_results` attachment。

attachment 在配置锁内一次写入；只有全部页验证通过才替换旧 attachment。中断、失败或半成品不会进入索引。

## 8. 任务与恢复

- 导入 journal 保存 `parse_route=local_ocr`；具体 OCR 文档任务由现有 parser ledger 按源 SHA、provider、渲染参数和版本恢复。
- 每个分片独立完成并持久化，重启只重跑缺失或损坏的分片。
- 用户取消时终止当前子进程，将 OCR 文档任务标为 cancelled，并由现有导入生命周期清理 journal。
- 导入队列仍使用既有 worker 数；同一 provider ID 的 runner 使用进程级互斥锁，避免两个导入任务同时争抢同一模型运行时。

## 9. 设置与组件边界

设置页提供“本地 OCR 组件”，分别配置并启用现代版和古籍版的独立 Python/`ocr.py` 路径。测试操作执行上游 CLI 的 `--help`，验证运行时、依赖和入口可启动。

首版不把未经核实的 GUI release ZIP 当成 CLI 包，也不伪造下载器。后续只有在构建出带摘要、许可证和 SBOM 的 MEFinder sidecar 资产后，才把路径配置替换为“一键下载”。

组件路径是机器状态，不进入备份；parser manifest 和 OCR 大结果同样不进轻量备份。`pdf_imports.json` 只保留可移植的相对 attachment。

## 10. 验收标准

代码验收：

- 两种上游 JSON 均能映射 polygon、reading order、几何竖排和空白页。
- runner 永远使用 `--sourceimg`，测试中禁止出现 `--sourcepdf`。
- 全书页覆盖缺失、重复或越界时不得 attachment。
- parser ID、journal route、manifest 和索引 profile 一致。
- 未配置本地 OCR 时现有导入路由与结果保持兼容。
- 取消、超时、非零退出和损坏 JSON 均有明确测试。

质量验收见 [local-ocr-spike.md](local-ocr-spike.md)。
