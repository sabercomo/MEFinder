# 文献原句定位器

文献原句定位器（ME_Finder）是一款本地优先的 Word / PDF 文献检索与引文辅助工具。输入完整原句、片段或带少量错漏的文字，即可定位文献、上下文和页码，并生成便于复制的引文信息。

搜索、索引和页码校准默认均在本机完成，不使用向量数据库、Embedding 或 LLM API。只有在用户主动导入扫描版、乱码文本层或复杂布局 PDF，并确认使用 MinerU 时，相关 PDF 页面才会提交到在线解析服务。

## Windows 绿色免安装版

从仓库的 **Releases** 页面下载 `MEFinder-v0.1.2-windows-portable.zip`，完整解压后双击 `文献原句定位器.exe` 即可运行，无需安装 Python。

- 支持 Windows 10 21H2 及以上系统；
- 程序、索引、导入文献、设置和日志均保存在解压目录；
- 发布包不包含作者的语料、索引、API 密钥或个人设置；
- 首次启动为空文献库，可在“文献导入”中添加自己的 PDF；
- 当前可执行文件未做代码签名，Windows SmartScreen 可能显示安全提示。

绿色版的完整使用说明见发布包内的 `README.md`。

## 安装方式

需要 Windows 和 Python 3。

Word 检索仍只使用 Python 标准库。PDF 原生文本解析优先使用 PyMuPDF；如果当前环境没有 PyMuPDF，系统会退回到一个只覆盖简单原生文本 PDF 的内置解析器。通过桌面应用的“文献导入”加入扫描、乱码文本层或复杂布局 PDF 时，系统会自动分段提交 MinerU、下载结构化结果并重建 SQLite 索引。

检查 Python：

```powershell
py -3 --version
```

推荐安装 PyMuPDF：

```powershell
py -3 -m pip install PyMuPDF
```

## 建立索引

在项目根目录运行：

```powershell
py -3 -m src.me_finder build-index
```

默认索引写入：

```text
data/index.sqlite3   # 应用和默认搜索使用
```

SQLite 数据库保存来源、卷次、文献、段落、页码映射和检索字段。如需额外生成便于迁移的 JSON 副本，可添加 `--export-json`；桌面应用不会加载该 JSON 文件。

```powershell
py -3 -m src.me_finder build-index --export-json
```

导入配置中的全部 PDF：

```powershell
py -3 -m src.me_finder build-index --include-pdf
```

只处理前 N 本 PDF 时才使用 `--pdf-limit N`。原生文本 PDF 会直接入库；扫描版、乱码文本层或复杂布局 PDF 会被标记为需要进一步解析。

PDF 导入配置位于：

```text
config/pdf_imports.json
```

PDF 页面级解析快照位于：

```text
corpus/parsed/pdf/
```

索引内容包括：来源类型、卷次或 PDF 文献名、文献标题、作者、目录页码范围或 PDF 页码映射、段落、句子、原始文件名、原始文本和 `normalized_text`。

## MinerU API 处理扫描/乱码 PDF

如果 PDF 被识别为扫描版、乱码文本层或复杂布局，可以先用 MinerU API 处理少量页码，下载结构化结果后再导入项目。

首次配置密钥：

```powershell
setup_mineru_key.cmd
```

密钥会保存到本机私有文件：

```text
config/mineru_api.local.json
```

这个文件已被 `.gitignore` 排除，不要发给别人。MinerU 官方接口使用 `Authorization: Bearer <token>`；如果 API 页面给了单独的 Bearer Token，请在设置工具里填写。如果页面只给 Access Key ID / Secret Access Key，可以先留空 Token 试运行；若返回 Token 错误，需要回到 MinerU API 管理页创建或查看 Token。

如果已经填过 Access Key / Secret，后来才找到 Bearer Token，可以只运行：

```powershell
setup_mineru_token.cmd
```

它只更新 `token` 字段，不需要重新打开 JSON。

建议先提交小范围页码，不要直接跑整本书：

```powershell
py -3 -m src.me_finder mineru-submit "corpus/raw_pdf/自由的权利 (【德】阿克塞尔·霍耐特（Axel Honneth）) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --data-id freedom-rights-p001-020 --page-ranges 1-20
```

命令返回 `batch_id` 后查询进度：

```powershell
py -3 -m src.me_finder mineru-status <batch_id>
```

完成后下载并解压结果：

```powershell
py -3 -m src.me_finder mineru-download <batch_id>
```

下载目录默认是：

```text
corpus/processed/mineru/results/
```

对于超过 200 页的扫描版或乱码文本层 PDF，使用自动分段命令。MinerU 精准解析单个任务最多 200 页，本命令会按页码段生成 `data_id`，提交时保留每段的页码范围：

```powershell
py -3 -m src.me_finder mineru-submit-segments "corpus/raw_pdf/自由的权利 (【德】阿克塞尔·霍耐特（Axel Honneth）) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --data-id-prefix freedom-rights
```

如果希望提交后一直等待并自动下载完成结果：

```powershell
py -3 -m src.me_finder mineru-submit-segments "corpus/raw_pdf/自由的权利 (【德】阿克塞尔·霍耐特（Axel Honneth）) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --data-id-prefix freedom-rights --wait --download
```

分段清单会写入：

```text
corpus/processed/mineru/manifests/
```

已经下载过的分段结果会被识别并跳过，避免重复提交。注意：分段解析后的 `page_idx` 是每个分段内部的 0 起始页号，导入索引时必须加上分段起始页 offset，不能直接当成 PDF 物理页或引用页码。

如果暂时没有精准解析 API Token，也可以用官方 Agent 轻量解析 API。它不需要 Token，但限制更严格：适合先处理不超过 20 页、10MB 以内的小样本，并且当前只返回 Markdown，不能替代带页面信息的结构化索引来源。

本项目会先把指定页码切成一个小 PDF，再提交 Agent：

```powershell
py -3 -m src.me_finder mineru-agent-submit "corpus/raw_pdf/自由的权利 (【德】阿克塞尔·霍耐特（Axel Honneth）) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --data-id freedom-rights-p001-020 --page-range 1-20
```

查询 Agent 任务：

```powershell
py -3 -m src.me_finder mineru-agent-status <task_id>
```

下载 Agent Markdown：

```powershell
py -3 -m src.me_finder mineru-agent-download <task_id>
```

下载目录默认是：

```text
corpus/processed/mineru/agent_results/
```

## 启动本地 Web 界面

先建立索引，然后运行：

```powershell
py -3 -m src.me_finder serve --host 127.0.0.1 --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

页面提供主搜索框、匹配模式、返回数量设置和结果卡片。结果卡片会突出显示命中文字，并提供“卷次 + 文献名 + 页码 + 原文”的复制文本。

## 在应用内填写 MinerU API

桌面应用左侧进入“设置”，在“MinerU API”区域填写 Bearer Token；如果 MinerU 页面同时提供 Access Key ID 和 Secret Access Key，也可以一并填写。填写 Token 到期日期后，设置页会显示到期提醒。

点击“保存 API 配置”后，桌面版凭据只写入当前 Windows 用户的本地数据目录：

```text
%LOCALAPPDATA%\MEFinder\mineru_api.local.json
```

已经保存的密钥不会回显在页面中。更新三个月后的 Token 时，只需填写新的 Token，其他密钥字段留空即可，旧字段会保留。程序升级或重新打包不会清除这个文件。源码/命令行模式仍使用项目中的 `config/mineru_api.local.json`。

## 配置其他视觉解析 API

MinerU 继续作为默认、免费的在线 PDF 解析服务。设置页在 MinerU 下方提供“其他解析 API”区域，可保存多套兼容 OpenAI Chat Completions 的视觉模型或中转接口。每套配置包含服务名称、API 地址、API Key 和模型名。

其他接口的密钥只保存在本机，且不会回显：

```text
安装版：%LOCALAPPDATA%\MEFinder\vision_api.local.json
绿色版 / 源码模式：config/vision_api.local.json
```

填写 API 地址和 API Key 后，设置页会尝试调用兼容接口的 `/models` 自动读取模型列表；输入框支持搜索选择，同时始终保留手动填写。部分厂商或中转接口不开放模型列表，此时获取失败不会影响保存，只需按服务商文档填写模型 ID。列表中的“可能支持图片”仅根据模型名称或接口元数据提示；保存后点击“测试”，程序会发送一张极小的测试图片，确认地址、密钥、模型和视觉输入能力确实可用。

导入 PDF 时可以主动选择已配置的其他视觉接口。程序会把 PDF 逐页渲染为图片，交给视觉模型转写，并把结果接入现有本地索引。此路径可能产生模型调用费用，且通用视觉接口通常不包含 MinerU 的完整版面框信息。

MinerU 解析失败时默认停止任务并提示用户自行切换。也可以在设置中选择默认备用接口，并显式开启“MinerU 失败时自动切换”；开启前界面会提示可能产生费用。失败任务可直接在导入队列中改用备用接口重试，无需重新上传 PDF。

## 应用内导入 PDF

在左侧“文献导入”中选择或拖入 PDF。系统会先在本地检测文本层：

- `native_text`：跳过 MinerU，直接重建本地索引；
- `scanned` / `broken_text` / `complex_layout`：自动按每段不超过 200 页提交 MinerU，下载带页面信息的结构化结果，然后重建本地索引。

导入队列会显示 MinerU 分段进度和索引重建状态。自动解析要求先在“设置”中填写有效的 Bearer Token，并使用包含完整 `corpus/raw_docx/` 的桌面包。

页面可以按来源筛选：

- 全部；
- Word；
- PDF。

PDF 结果会显示 PDF Page Label、引用页码或“引用页码尚未校准”，并提供“打开原始 PDF”链接。

## PDF 页码校准

“页码校准”页面中的 PDF 起始页和结束页都按阅读器显示的页数填写，从 `1` 开始。例如正文第一页位于 PDF 文件第 48 页时，填写：

```text
PDF 起始页：48
PDF 结束页：324
引用起始页：1
```

这表示 PDF 第 48 页对应书内引用第 1 页，PDF 第 49 页对应引用第 2 页。扫描 PDF 中如果存在重复页、插图页或不计页码页，必须在偏移发生处再添加分段，不能用一个固定 offset 覆盖整本书。点击“保存校准配置”后，应用会自动重建 SQLite 索引；完成提示出现后，新页码立即用于搜索结果。

## 桌面版（原生窗口 exe）

桌面版用 pywebview 把同一个 Web UI 包进原生窗口：双击 exe 即用，不占浏览器，窗口关闭后进程自动退出。服务只绑定 `127.0.0.1`，端口由系统自动分配。

开发模式直接运行：

```powershell
py -3 desktop.py
```

打包成可分发的文件夹（需要先 `py -3 -m pip install pywebview pyinstaller`）：

```powershell
build_desktop.cmd
```

生成带空白索引、隐私检查和 SHA256 的绿色发布 ZIP，可直接双击：

```text
rebuild_portable_release.cmd
```

如需让"打开原文"按钮可用，把语料一并复制（约 400MB）：

```powershell
build_desktop.cmd full
```

产物在 `dist\MEFinder\`，双击其中的 `文献原句定位器.exe` 启动。桌面包使用 SQLite 索引，首次启动期间窗口会显示加载页。出错时窗口内会显示错误信息，详细日志在 exe 同目录的 `desktop.log`。

整个 `dist\MEFinder\` 文件夹可以拷给别人（对方需要 Windows 10 21H2+ 自带的 WebView2）。构建脚本不会复制本机私有 API 密钥；每个 Windows 用户在应用“设置”中填写自己的 Token。

## 命令行搜索

```powershell
py -3 -m src.me_finder search "宗教是人民的鸦片" --limit 5
```

只搜索 PDF：

```powershell
py -3 -m src.me_finder search "We make and cannot escape making value judgments" --source-type pdf
```

匹配模式：

- `auto`：按优先级自动搜索；
- `exact`：完全一致；
- `compact`：忽略多余空格；
- `punctuation`：忽略常见标点差异；
- `fuzzy`：中文字符 n-gram 召回并模糊排序。

## 更新语料后重新建立索引

把新文件放入 `corpus/raw_docx/` 后重新运行：

```powershell
py -3 -m src.me_finder build-index
```

旧的 `data/index.json` 和 `data/index.sqlite3` 会在 `data/backups/` 中自动备份后更新。

更新 PDF 配置或 PDF 语料后重新运行：

```powershell
py -3 -m src.me_finder build-index --include-pdf
```

备份位置：

```text
data/backups/
```

## 运行测试

```powershell
py -3 -m unittest discover
```

测试样例位于：

```text
tests/known_quotes.json
```

样例覆盖完整原句、缺少标点、标点错误、多余空格、少量错字、少量漏字、截取片段和重复表述。

PDF 测试样例位于：

```text
tests/known_pdf_quotes.json
```

## 已知限制

- DOCX 页码依赖文档分节和页面结构；导入后仍建议人工抽样核验。
- 旧版二进制 DOC 使用 OLE/UTF-16 文本流进行本地抽取时，页码可能只能显示目录页码范围，不能视为段落级精确原书页码。
- 标题识别优先使用目录页码范围和正文标题候选，复杂多行标题仍可能需要后续校验。
- 搜索结果会标明页码来源；未验证页码不会伪装成精确页码。
- PDF 前置页未校准时会显示“PDF 第 X 页，引用页码尚未校准”，不会把 PDF 物理页序冒充引用页码。
- 跨页搜索通过相邻页窗口实现，结果会返回起始页和结束页。
- 当前环境未安装 PyMuPDF 时，复杂对象流 PDF 只能分类为需要 PyMuPDF/MinerU，不能保证原生抽取。
- MinerU 自动解析依赖有效 Token 和网络；如果任务失败，导入队列会保留错误提示，不会把不可靠文本写入正文索引。
