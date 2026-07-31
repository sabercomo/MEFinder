# 文献原句定位器

文献原句定位器（ME_Finder）是一款本地优先的 Word / PDF 文献检索与引文辅助工具。输入完整原句、片段或带少量错漏的文字，即可定位文献、上下文和页码，并生成便于复制的引文信息。

搜索、索引和页码校准默认均在本机完成，不使用向量数据库、Embedding 或 LLM API。只有在用户主动导入扫描版、乱码文本层或复杂布局 PDF，并确认使用 MinerU 时，相关 PDF 页面才会提交到在线解析服务。

## Windows 安装版（推荐）

从 Releases 页面下载 `MEFinder-v<版本>-windows-setup.exe` 并运行。安装版把程序文件放在
`%LOCALAPPDATA%\Programs\MEFinder`，把文献、索引、设置和更新缓存放在
`%LOCALAPPDATA%\MEFinder`；覆盖安装或应用内更新不会删除用户数据。

- Windows 主窗口采用与页面融为一体的自绘标题栏，切换皮肤时与正文同一帧更新；
- PDF 默认在应用内的 Edge WebView2 阅读窗口打开，并直接跳到搜索命中的 PDF 物理页；
- 在“设置 → PDF 阅读”中可以改用 Windows 默认 PDF 阅读器，例如 Adobe Acrobat DC；
- 在“设置 → 软件更新”中可以手动检查、下载并安装新版，也可以开启启动时自动检查和下载；
- 安装更新前仍会请求确认，下载的安装包必须通过发布页配套的 SHA-256 校验。

当前可执行文件若未做代码签名，Windows SmartScreen 可能显示安全提示。

## Windows 绿色免安装版

从仓库的 **Releases** 页面下载 `MEFinder-v<版本>-windows-portable.zip`，完整解压后双击 `文献原句定位器.exe` 即可运行，无需安装 Python。

- 支持 Windows 10 21H2 及以上系统；
- 程序、索引、导入文献、设置和日志均保存在解压目录；
- 发布包不包含作者的语料、索引、API 密钥或个人设置；
- 首次启动为空文献库，可在“文献导入”中添加自己的 PDF；
- 绿色版可以检查发布信息，但不会自行覆盖当前目录；需要完整解压新版或改用安装版；
- 当前可执行文件未做代码签名，Windows SmartScreen 可能显示安全提示。

绿色版的完整使用说明见发布包内的 `README.md`。

## macOS 桌面版

macOS 版本使用 Cocoa/WebKit 原生窗口，当前发布包支持 macOS 14 或更高版本，
并支持 Apple Silicon 和 Intel 构建。开发构建命令：

```bash
python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos.txt
MEFINDER_PYTHON=.venv-macos/bin/python ./build_macos.sh
```

普通用户推荐下载 `MEFinder-v<版本>-macos-<架构>.dmg`：打开镜像，把
`MEFinder.app` 拖到旁边的 `Applications`，以后即可从“应用程序”或 Launchpad 启动，
无需每次回到下载目录。构建流程同时保留 ZIP，并为 DMG 和 ZIP 分别生成 SHA-256。

搜索结果中的 PDF 默认使用应用内的 Apple PDFKit 轻量阅读窗口打开，并精确定位到
命中的 PDF 物理页。阅读窗口提供上一页、下一页、页码跳转、缩放和适合窗口；如果更喜欢
系统“预览”，可在“设置 → PDF 阅读”中切换，使用预览时页码需要手动翻到。

应用数据保存在 `~/Library/Application Support/MEFinder/`，替换或升级 `.app`
不会删除文献、索引和设置。完整构建、签名与发布说明见 `MACOS_BUILD.md`。

## 安装方式

源码模式需要 Python 3；Windows 命令示例使用 `py -3`，macOS 使用 `python3`。

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

在桌面应用的“设置 → MinerU API”中填写 API Token。可以粘贴原始 Token，
也可以粘贴完整的 `Authorization: Bearer <token>`。配置会保存到本机私有文件：

```text
config/mineru_api.local.json
```

这个文件已被 `.gitignore` 排除，不要发给别人。MinerU 当前精准解析 API 使用
`Authorization: Bearer <token>`，必须在 MinerU API 管理页创建或复制 Token；
Access Key ID / Secret Access Key 不能替代该 Token。

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

桌面应用左侧进入“设置”，在“MinerU API”区域填写 API Token。可以直接粘贴原始 Token，也可以粘贴完整的 `Authorization: Bearer <token>`；Access Key ID / Secret Access Key 不能用于当前精准解析 API。填写 Token 到期日期后，设置页会显示到期提醒。

点击“保存 API 配置”后，桌面版凭据只写入当前 Windows 用户的本地数据目录：

```text
%LOCALAPPDATA%\MEFinder\mineru_api.local.json
```

已经保存的 Token 不会回显在页面中。更新 Token 时只需填写新的 Token；程序升级或重新打包不会清除这个文件。源码/命令行模式仍使用项目中的 `config/mineru_api.local.json`。

## 配置其他视觉解析 API

MinerU 继续作为默认、免费的在线 PDF 解析服务。设置页在 MinerU 下方提供“其他解析 API”区域，可保存多套兼容 OpenAI Chat Completions 的视觉模型或中转接口。每套配置包含服务名称、API 地址、API Key 和模型名。

其他接口的密钥只保存在本机，且不会回显：

```text
安装版：%LOCALAPPDATA%\MEFinder\vision_api.local.json
绿色版 / 源码模式：config/vision_api.local.json
```

填写 API 地址和 API Key 后，设置页会尝试调用兼容接口的 `/models` 自动读取模型列表；输入框支持搜索选择，同时始终保留手动填写。部分厂商或中转接口不开放模型列表，此时获取失败不会影响保存，只需按服务商文档填写模型 ID。列表中的“可能支持图片”仅根据模型名称或接口元数据提示；保存后点击“测试”，程序会发送一张极小的测试图片，确认地址、密钥、模型和视觉输入能力确实可用。

导入 PDF 时可以主动选择已配置的其他视觉接口。程序会把 PDF 逐页渲染为图片，交给视觉模型转写，并把结果接入现有本地索引。此路径可能产生模型调用费用，且通用视觉接口通常不包含 MinerU 的完整版面框信息。

MinerU 解析失败时默认停止任务并提示用户自行切换。设置中只有一个“MinerU 失败后自动切换”开关；开关会立即保存，并自动使用列表中的首个已启用且配置完整的备用接口。界面会明确提示该路径可能产生模型调用费用。失败任务也可直接在导入队列中手动改用备用接口重试，无需重新上传 PDF。

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

## Windows 桌面版（原生窗口 exe）

桌面版用 pywebview 把同一个 Web UI 包进原生窗口：双击 exe 即用，不占浏览器，窗口关闭后进程自动退出。服务只绑定 `127.0.0.1`，端口由系统自动分配。

开发模式直接运行：

```powershell
py -3 desktop.py
```

生成带空白索引、隐私检查和 SHA256 的绿色发布 ZIP，可直接双击：

```powershell
rebuild_portable_release.cmd
```

生成带空白索引、隐私检查和 SHA-256 的单文件安装程序（需安装 Inno Setup 6）：

```powershell
.\build_windows_installer.ps1
```

安装版与发布流程详见 `WINDOWS_BUILD.md`。

绿色版 ZIP 和安装程序都会生成到 `release\`。构建脚本只携带空白索引和示例配置，
不会复制本机语料或私有 API 密钥；每位用户在应用中导入自己的文献，并在“设置”中填写 Token。

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

## 开发文档

- `CORPUS_AUDIT.md`：初始 Word 语料结构与风险审计；
- `PROPOSED_DATA_SCHEMA.md`：索引核心实体与可信度字段；
- `PAGE_NUMBER_STRATEGY.md`：Word 原书页码的来源、限制和校验规则；
- `PDF_PAGE_MODEL.md`：PDF 物理页、页码标签与引用页码的边界。

## 已知限制

- DOCX 页码依赖文档分节和页面结构；导入后仍建议人工抽样核验。
- 旧版二进制 DOC 使用 OLE/UTF-16 文本流进行本地抽取时，页码可能只能显示目录页码范围，不能视为段落级精确原书页码。
- 标题识别优先使用目录页码范围和正文标题候选，复杂多行标题仍可能需要后续校验。
- 搜索结果会标明页码来源；未验证页码不会伪装成精确页码。
- PDF 前置页未校准时会显示“PDF 第 X 页，引用页码尚未校准”，不会把 PDF 物理页序冒充引用页码。
- 跨页搜索通过相邻页窗口实现，结果会返回起始页和结束页。
- 当前环境未安装 PyMuPDF 时，复杂对象流 PDF 只能分类为需要 PyMuPDF/MinerU，不能保证原生抽取。
- MinerU 自动解析依赖有效 Token 和网络；如果任务失败，导入队列会保留错误提示，不会把不可靠文本写入正文索引。
