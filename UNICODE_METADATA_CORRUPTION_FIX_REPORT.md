# Unicode 元数据损坏修复报告

## 结论

《食人资本主义》的中文并非在 PDF、MinerU、SQLite、API 或前端显示阶段损坏。字面量问号第一次出现在一次运行时数据维护脚本的“数据库保存前参数”层：含中文常量的 Python 源码经 PowerShell 管道送入 `py -3 -` 时，被 Windows 当前控制台代码页转换成了 `?`。后续 UTF-8 JSON、SQLite TEXT、API 和 UI 只是原样保存并显示了已经损坏的值。

标题的坏值由多个 U+003F 组成，而不是 Unicode replacement character，也不是字体缺字。数字 `2023` 未受影响，与上述根因一致。

## 数据保护

修复前已建立：

- 完整数据库备份：`%LOCALAPPDATA%\MEFinder\runtime\data\backups\index-unicode-recovery-20260712123951.sqlite3`
- 问号相关记录导出：`%LOCALAPPDATA%\MEFinder\runtime\data\backups\metadata-question-mark-rows-20260712123951.json`

没有重建全文索引，没有删除或重新导入《食人资本主义》PDF，也没有删除 MinerU 结果、PDF 页码映射或人工映射。恢复只按 `source_file_id=pdf-cannibal-capitalism` 更新书目字段及其目录显示副本。

恢复“消费社会”时，单文献替换还自动生成了数据库备份：

- `%LOCALAPPDATA%\MEFinder\runtime\data\backups\index-20260712050116.sqlite3`

## 链路诊断

逐层检查结果：

| 层 | 结果 |
|---|---|
| 原始 PDF 文件名 | 中文正常 |
| MinerU/PDF 页面文本 | 中文正常；可见书名、作者近似 OCR、译者和年份证据 |
| 工作区 `config/pdf_imports.json` | 中文正常 |
| 元数据解析器与 API UTF-8 JSON | 中文往返正常 |
| 运行时 `pdf_imports.json` | 修复前已是字面量 `?` |
| SQLite 保存值 | 修复前已是字面量 `?` |
| 前端返回和表单 | 忠实显示数据库中的 `?` |

SQLite 字段为 Unicode TEXT，写入使用参数化 SQL。没有发现数据库类型或前端字符集问题。

项目目录没有可用的 Git 仓库元数据，`git status`/`git log` 均报告 `not a git repository`，因此无法用提交历史定位回归。对当前源码的编码边界扫描只发现 CLI 标准输出采用 `errors="replace"`；该代码只影响控制台日志，不参与元数据对象、JSON 或数据库写入。Adobe 打开逻辑的 `subprocess.Popen` 也不捕获或解码文本。

## 代码修复

修改内容：

- `src/me_finder/bibliographic_metadata.py`
  - 新增书目字段有效性校验；拒绝纯问号、高问号比例、replacement character、占位符和无可用文字的值。
  - 完整状态改为先验证字段内容，不再把非空的 `????` 计为完整。
  - 自动识别结果只作为返回预览，不写数据库；来源标记改为 `automatic_recognition`。
  - 自动候选无效时保留既有稳定标题，并标记 `recognition_failed`。
  - 用户明确保存后才设置 `metadata_source=manual`。
- `src/me_finder/web.py`
  - 元数据保存 API 对无效中文/问号字段返回 400，不再写入配置或 SQLite。
- `src/me_finder/database.py`
  - 新增带备份、事务和来源隔离的单文献原子替换，用于恢复 MinerU 文献而不重建全库。
- `tools/repair_unicode_metadata.py`
  - 只接收 ASCII `source_file_id`，从可信 UTF-8 配置读取书目值；不再通过 shell 参数或内联脚本传递中文。
- `tools/restore_mineru_source.py`
  - 可复用已有 MinerU manifest，只替换指定 PDF 的数据库记录。
- `tests/test_citations.py`、`tests/test_database_search.py`
  - 增加真实中文 JSON/API 字节/SQLite/重启搜索/引文往返、问号拒绝、自动来源、稳定标题和单文献替换测试。

## 数据恢复

已原位恢复《食人资本主义》：

- 书名：食人资本主义
- 作者：南希·弗雷泽
- 译者：蓝江
- 出版地：上海
- 出版社：上海人民出版社
- 出版年份：2023

恢复来源是未损坏的工作区 UTF-8 导入配置，并与现有 PDF 文件名及前 20 页 OCR 证据交叉检查。恢复后更新 1 个来源、1 个卷目录项、1 个文献目录项和 576 个段落显示副本；未重新导入正文。全库 21 个来源的书目字段复查后，问号损坏列表为空。

中文脚注与 GB/T 验收结果：

- `南希·弗雷泽：《食人资本主义》，蓝江译，上海人民出版社，2023年，第197页。`
- `南希·弗雷泽. 食人资本主义[M]. 蓝江, 译. 上海: 上海人民出版社, 2023: 197.`

## 消费社会

《消费社会》原运行时记录只有 `broken_text` 的 PyMuPDF 检测结果，段落数为 0；此前留下的 MinerU Markdown 没有 `page_idx`，不能作为可靠页面索引。现使用持久化 MinerU Token 将 246 页 PDF 分为 1–200、201–246 两段重新精准解析，下载结构化结果并原位替换该来源。

当前状态：

- `detected_pdf_type=mineru_structured`
- `text_source=mineru`
- 491 条页面/跨页检索段落
- 实测“消费控制当代人的全部生活”精确命中
- 返回 `序言第4页`，PDF 物理页索引为 9

同时验证 Word 引文仍可搜索，`247晚期资本主义与睡眠的终结`仍在数据库。

## 测试

最终全套测试通过 54 项。测试覆盖：

- 中文识别结果到 UTF-8 JSON/API 字节再到 UI state 的完全一致；
- SQLite 参数化写入和应用重启后中文不变；
- `??????` 不能保存，也不能得到 `complete` 状态；
- 自动识别不设置 `manual`，也不直接覆盖已有标题；
- 恢复后的中文脚注与 GB/T 完整输出；
- 单文献替换保留其他来源；
- Word/PDF 统一搜索不回归。

## 尚未恢复

没有发现其他书目字段被大量问号覆盖的来源，因此没有自动猜测或批量覆盖其他文献。MinerU OCR 本身可能存在标点、姓名间隔和版权页出版社文字识别误差，这些属于 OCR 质量问题，仍应在书目预览中由用户确认，不能自动覆盖已确认元数据。
