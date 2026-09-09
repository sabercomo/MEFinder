# 《谁在害怕性别》生成与对照恢复验证

2026-09-09：六组真实文献对齐全部生成成功，24 次指定选句双向定位通过；复现并修复桌面长请求 `Load failed`。

## 输入与隔离

- 输入为本机已入库的中文 EPUB、英文 EPUB，以及用户用本地 MinerU JSON 重新解析的中文 PDF。
  PDF 在对齐层读取已入库文本，不重新解析 PDF 或 JSON，不 OCR。
- SQLite 只读连接配合 backup 建立 `.codex-tmp/gender-alignment/index.sqlite3` 副本。
  试验对副本经 `generate_alignment` 正式入口写入，使用本机已安装的两种模型和缓存，关闭 Hugging Face 网络访问。
- 每种模型依次测试三种配对，不覆盖文献原文件、书目或人工修正。不修改模型阈值。
- 算法 v22、语义 v20、分段器 v13、正文区域 v2。复核的是任务交付和指定句子的定位恢复，不是全书金标准准确率。

## 长请求根因

- 生产日志：17:57:31 收到 PDF→英文 EPUB 请求，18:01:37 才完成，保存 1,732 组可定位链接。
  中间又收到两次相同请求。用户界面的错误文字为 `Load failed`。
- 同机 pywebview / WKWebView 向本地服务发起请求，服务延迟 70 秒返回；旧同步方式在
  **61.006 秒**返回 `Load failed`。该复现不加载模型或文献。
- 使用修改后的真实 `30-library.js` 请求函数与真实 `TextAlignmentController`，仅将模型计算
  替换为 70 秒延迟：每秒短请求查询任务，**70.284 秒**返回成功结果。
- 证据日志保留于本地 `.codex-tmp/gender-alignment/webkit-{timeout,polling}.log`。
  测试服务必须显式声明 UTF-8，避免汉字正则被错误解码；这不是产品错误。

## EPUB 存量范围

旧 v21 记录的中文正文范围 `[539,3918)`、英文 `[500,4231)` 排除了导论。
截图选句位于中文 order 41；当前检测范围为中文 `[3,3918)`、英文 `[22,4231)`。
仅升级检测源码不改已存链接，因此需通过正常生成重新计算。区域版本与旧结果提示均已修复，
人工复核范围不受自动检测覆盖。

## 真实模型结果

| 模型 | 配对 | 可定位链接数 | 指定选句双向检查 |
|---|---|---:|---:|
| minilm-l12-v2 | 中文 EPUB → 英文 EPUB | 1562 | 4/4 |
| minilm-l12-v2 | MinerU PDF → 英文 EPUB | 1510 | 4/4 |
| minilm-l12-v2 | MinerU PDF → 中文 EPUB | 2820 | 4/4 |
| multilingual-e5-large | 中文 EPUB → 英文 EPUB | 1625 | 4/4 |
| multilingual-e5-large | MinerU PDF → 英文 EPUB | 1732 | 4/4 |
| multilingual-e5-large | MinerU PDF → 中文 EPUB | 3156 | 4/4 |

每组检查开篇“为什么会有人害怕……”及截图“可以继续列举……”两处，中→英、英→中各一次；
中文 PDF↔中文 EPUB 同样验证双向。共 24 次均返回目标文献、页／段锚点与字符区间，
并断言返回文本包含已核对的对应句。英文对应截图句以 `The list of what there is to fear` 开头。
PDF 开篇标题与第一句同属一个分段，部分返回包含标题；不将此描述为句界完全精确。

中文 EPUB 无出版方页码，仍如实显示未校准；不制造引用页码。
此处“可定位链接数”为程序接受数，不等同于人工确认数，不推断全书每句话皆正确。

## 工程回归

- 先增加失败测试，再修实现：
  `test_background_generation_returns_before_computation_and_deduplicates`、
  `test_background_terminal_errors_and_cancellation_are_returned_to_polling`、
  `test_old_detected_body_range_requests_regeneration_instead_of_blaming_text`。
- Node 执行前端请求函数，验证运行中只查询、成功／取消／错误终态，未重复 POST。
- 第一轮完整测试仅 HTTP 新路由契约断言失败，补齐 v0.5.3 路由快照后：
  `unittest discover -t . -s tests` **2082 项，95.073 秒，OK（21 skip）**。
- Ruff F、前端指纹／符号预算／结构守卫、Node 语法与 diff 空白检查通过。
- 21 skip 是平台或可选语料条件跳过，不算已执行验证。

## 交付边界

- 未改默认模型、置信度门槛和人工修正机制；E5 继续为实验档。
- 修复只证明本次三个已入库版本可生成和指定句子可定位；用户此前被替换的旧 PDF 索引没有纳入当前矩阵。
- 正式库恢复、应用安装与最终构建记录在交付后追加，不把副本试验冒充生产验收。


## 2026-09-09 20:40 — 正式库与安装版验收

- 通过 SQLite backup 保留正式库副本后，按用户当前 `minilm-l12-v2` 设置重新生成三组对照。
  12 次双向查询与已核对的副本字符区间逐项一致；三版本各提供另两个对照目标。
- 对备份逐行比对 `source_files`（30）、`paragraphs`（8262）、`pdf_pages`（3156）、
  `document_groups`（1）、`document_group_members`（3）、`alignment_manual_overrides`（0），
  全部保持原样；SQLite `quick_check=ok`。正式库只通过正常生成路径更新对齐相关数据。
- 官方 `build_macos.sh` 2082 项测试通过（21 skip），93.898 秒；完整构建／签名／sidecar／
  ZIP／DMG 门禁通过。已保留旧应用并安装本地修复包，安装后的严格签名验证通过。
- 原生 pywebview 界面搜索截图选句，打开中文 EPUB 的“译本对照”：左右栏显示中文及对应英文
  段落，无“副文本”错误。源选句和译文仍采用既有粗定位／自动跟随显示方式。
- 原生“管理作品组”选择 MinerU PDF→英文 EPUB，点击“重新生成”，按钮进入“取消对齐”状态，
  20:40:00 日志确认接受 1510、拒绝 1666、未配对 34，界面恢复“重新生成”。
- 该原生 UI 生成使用用户当前 MiniLM 设置；E5 的六组矩阵中三组均在数据库副本验证，未改用户设置。
- 产物摘要见 `docs/release-notes-0.5.3.md` 晚间修复包记录。未发布 Release。
