# MEFinder MCP 完整开发计划

更新日期：2026-08-14

适用基线：MEFinder 0.4.x

当前实施状态：v0.4.4 里程碑 0 至 5 已完成；里程碑 6、7 的代码、macOS arm64/x86_64 实物验证和 Windows Server 2022 托管门禁均已实现，托管工作流尚未推送执行，Windows 10/11 x64 消费者实机与正式签名门禁仍待执行；里程碑 8 的评估已完成，结论是保持 v1 只读并延期写工具。源码接入见 `docs/mcp-v1-codex-e2e-report.md`，发布与并发验证见 `docs/mcp-v1-concurrency-release-report.md`，写能力决策见 `docs/mcp-v2-decision.md`。

## 1. 最终目标

交付一个随 MEFinder Windows、Windows 绿色版和 macOS 安装包发布的本地 MCP Server，使 Codex 能直接使用用户现有的 MEFinder 文献库完成文献原句核对。

正式版必须满足：

- 使用本地 STDIO MCP，不要求公网服务、OpenAI API Key 或额外 Agent；
- MEFinder 桌面窗口开启或关闭时都能执行只读核对；
- 复用现有搜索、结构化阅读、页码映射和题录数据，不建立第二套业务逻辑；
- 不混淆 PDF 物理页与正式引用页，未校准页码必须明确标注；
- MCP 不长期占用索引数据库，不妨碍桌面应用导入、迁移和原子替换索引；
- 源码模式、Windows 安装版、Windows 绿色版和 macOS 安装版均有可验证的接入方式；
- 第一正式版只读。导入、删除、校准和元数据修改进入后续写能力里程碑。

本计划不承诺日期。阶段推进以退出门槛为准，不以“代码已经写完”代替验证通过。

## 2. 范围与版本边界

### 2.1 MCP v1：本地只读文献核对

MCP v1 覆盖：

- 列出和筛选已导入文献；
- 定位原句、残句和带少量错漏的引文；
- 返回命中原文、前后文、匹配方式和匹配分数；
- 返回文献题名、作者、文献 ID 和题录相关信息；
- 返回 PDF 物理页、正式引用页及页码映射可靠性；
- 根据命中位置继续读取 PDF 页面或 Word 段落窗口；
- 对无结果、多候选、页码未校准和索引不可用给出明确结构化结果。

### 2.2 MCP v2：受控写操作

MCP v2 在 v1 稳定后评估，候选能力包括：

- 导入本地 PDF/DOCX；
- 查询导入任务状态；
- 保存人工确认的题录信息；
- 应用用户明确确认的页码校准结果。

删除文献、恢复备份、迁移数据目录等高影响操作不自动进入 v2。每项必须单独做授权、审批和恢复性设计。

### 2.3 MCP v3：远程或插件形态

只有出现跨设备或托管需求时，才评估 Streamable HTTP、OAuth 和插件发布。它不属于本地文献核对目标的前置条件。

## 3. 已确认的架构决策

### 3.1 使用 STDIO

本地 Codex 原生支持由命令启动的 STDIO MCP Server。v1 不增加 HTTP 监听端口、端口发现、Bearer Token 或 OAuth。

参考：

- <https://learn.chatgpt.com/docs/extend/mcp?surface=cli>
- <https://developers.openai.com/plugins/build/mcp-server>

### 3.2 MCP 是适配器，不是新的业务核心

目标结构：

```text
Codex
  │ STDIO MCP
  ▼
MEFinder MCP Adapter
  ▼
LiteratureVerificationService
  ├─ SearchService / SearchEngine
  ├─ structured_reader
  └─ SQLite index
```

桌面 UI、HTTP 和 MCP 是同一业务能力的不同入口。MCP 不调用前端，也不通过桌面应用的随机 HTTP 端口间接查询。

### 3.3 每次调用获取当前数据根并使用短连接

MCP 进程可以长期存在，但不得长期持有 `SearchEngine` 或 SQLite 连接。每次工具调用：

1. 解析当前活动数据根目录；
2. 确认索引文件存在；
3. 打开只读数据库或搜索引擎；
4. 完成一次有界查询；
5. 在返回结果前关闭连接。

理由：

- 数据目录可能被用户迁移；
- 新导入文献应在下一次调用中可见；
- Windows 下长期打开的 SQLite 文件可能阻碍索引文件替换；
- 当前索引替换机制已经处理短暂文件锁，MCP 不应扩大锁持有时间。

### 3.4 MCP 返回证据，不替 Codex作语义裁决

MCP 可以返回 `exact`、`fuzzy`、分数、页码状态和候选数量，但不能仅凭搜索分数产生 `verified=true`。

最终“原句吻合、近似、存在歧义或未找到”的结论由 Codex 根据结构化证据作出。

### 3.5 v1 不暴露不必要的本地路径

工具结果使用稳定的 `source_file_id`、阅读游标和锚点串联调用。除非未来增加明确的“打开原文件”能力，否则不返回绝对路径。

## 4. MCP v1 工具契约

工具命名和字段在进入实现前冻结。破坏性变更必须更新 schema 版本和契约测试。

### 4.1 `list_documents`

用途：当用户指定书名、作者或文献类型时，先解析出稳定的 `source_file_id`。

输入：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `query` | string/null | null | 对题名、作者和原始文件名做筛选 |
| `source_type` | enum | `all` | `all`、`pdf`、`word` |
| `limit` | integer | 20 | 1..100 |

输出：

```json
{
  "schema_version": "1",
  "total": 1,
  "has_more": false,
  "documents": [
    {
      "source_file_id": "pdf-example",
      "source_type": "pdf",
      "title": "示例文献",
      "author": "作者",
      "original_file_name": "example.pdf"
    }
  ]
}
```

### 4.2 `locate_quote`

用途：检索用户提供的引文并返回可核查证据。

输入：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `quote` | string | 必填 | 非空，最大 10,000 个 Unicode codepoint |
| `mode` | enum | `auto` | `auto`、`exact`、`compact`、`punctuation`、`fuzzy` |
| `source_file_id` | string/null | null | 限定单一文献 |
| `source_type` | enum | `all` | `all`、`pdf`、`word` |
| `limit` | integer | 5 | 1..20 |

输出中的每个候选至少包含：

```json
{
  "schema_version": "1",
  "query": "待核对原句",
  "total": 1,
  "has_more": false,
  "matches": [
    {
      "source_file_id": "pdf-example",
      "source_type": "pdf",
      "document_title": "示例文献",
      "work_title": null,
      "author": "作者",
      "matched_text": "命中的原文",
      "paragraph_text": "命中所在完整段落",
      "context_before": [],
      "context_after": [],
      "match_type": "exact",
      "match_score": 1.0,
      "physical_page": {
        "start_index": 12,
        "end_index": 12,
        "start_label": "13",
        "end_label": "13"
      },
      "citation_page": {
        "start": "1",
        "end": "1",
        "status": "calibrated"
      },
      "page_mapping": {
        "method": "manual_segment",
        "confidence": 1.0,
        "confidence_level": "high",
        "note": null
      },
      "reader": {
        "unit": "pdf_page",
        "start": 12
      }
    }
  ]
}
```

输出不得包含前端专用的 `highlighted_html`。`copy_text`、全部引文格式和内部映射证据只有在完成实际模型上下文测试后，才能决定是否保留。

### 4.3 `read_document_window`

用途：在搜索上下文不足时，从指定位置继续读取文献。

输入：

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `source_file_id` | string | 必填 | 已存在的文献 ID |
| `start` | integer | 0 | PDF 页索引或 Word 段落自然位置 |
| `count` | integer | 10 | 1..50；低于现有内部上限 |

输出沿用现有结构化阅读的稳定概念：

- `source`；
- `start`、`count`、`total`；
- `previous_start`、`next_start`、`has_more`；
- `items[*].anchor_id`；
- 页面或段落文本；
- 物理页、引用页及引用格式状态。

### 4.4 工具安全标注

三个 v1 工具统一声明：

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "openWorldHint": false
}
```

### 4.5 Server instructions

初始化 instructions 保持简短，并明确：

- 先使用 `locate_quote`；
- 用户指定文献但未提供 ID 时使用 `list_documents`；
- 上下文不足时使用 `read_document_window`；
- 不把物理页称为正式引用页；
- 多候选时不得隐藏歧义；
- 搜索命中只证明文本在本地索引中出现，不自动证明题录元数据完全正确。

## 5. 错误契约

MCP 边界只转换已知错误，不吞掉未知异常。

| 错误码 | 场景 | 是否建议重试 |
| --- | --- | --- |
| `invalid_input` | 参数不符合 schema | 否 |
| `index_not_found` | 当前数据根没有索引 | 否；提示启动或导入 MEFinder |
| `index_unavailable` | 索引正在替换、锁定或暂时不可读 | 是 |
| `source_not_found` | 指定文献 ID 不存在 | 否 |
| `unsupported_source_type` | 文献类型不支持结构化阅读 | 否 |
| `internal_error` | 未归类故障 | 否；保留日志，返回简洁错误 |

STDOUT 只允许 MCP 协议帧。日志写入 STDERR；发布版是否同时写文件，在打包阶段按现有日志策略决定。

## 6. 分阶段实施计划

### 里程碑 0：契约冻结与基线夹具

工作：

- 用真实但可公开/测试的最小索引建立 MCP 测试夹具；
- 固定三个工具的名称、输入 schema、精简输出和错误码；
- 记录同一查询在现有 UI/API 下的基线结果；
- 固定页码术语：物理页、引用页、未校准、跨页和双开页；
- 确认 v1 不包含任何写操作。

退出门槛：

- 工具契约评审通过；
- 夹具覆盖 PDF、Word、已校准、未校准和无结果；
- 基线结果可由自动测试读取，不依赖个人文献库。

### 里程碑 1：最小架构接缝

工作：

- 新增 `src/me_finder/runtime_location.py`；
- 从 `desktop.py` 提取源码版、安装版、绿色版和 macOS 数据根解析逻辑；
- 桌面入口改为复用该模块，保持现有行为；
- 新增 `LiteratureVerificationService`，作为三个 MCP 用例的唯一业务入口；
- 明确该服务每次调用获取当前路径并关闭数据库资源；
- 不拆分 `web.py`，不改 HTTP 契约。

退出门槛：

- 现有桌面数据路径测试全部通过；
- 安装版、绿色版、macOS 和 `ME_FINDER_APP_DATA_ROOT` 覆盖行为未改变；
- 服务层测试不导入 pywebview 或启动 HTTP Server；
- 没有新增长期 SQLite 连接。

### 里程碑 2：只读核对服务

工作：

- 实现 `list_documents`；
- 通过 `SearchService`/`SearchEngine` 实现 `locate_quote`；
- 通过 `structured_reader.get_document_window` 实现 `read_document_window`；
- 增加 MCP 专用的精简结果转换；
- 明确引用页为空时的结构化状态，不用展示字符串反推语义；
- 保证多候选和跨页结果不被再次去重。

退出门槛：

- 核心搜索字段与现有 UI/API 基线一致；
- 不返回 HTML；
- 每次调用结束后连接已关闭；
- 无结果返回成功的空集合，故障返回错误，两者不混淆；
- 同一输入在服务层有确定性输出。

### 里程碑 3：STDIO MCP Server

工作：

- 引入并锁定 Python `mcp` SDK 依赖；
- 新增 `src/me_finder/mcp_server.py`；
- 注册三个工具及输入/输出 schema；
- 设置稳定的 server name、应用版本和 instructions；
- 返回 `structuredContent` 和简短 `content`；
- 映射已知服务错误，未知错误快速失败并记录堆栈；
- 增加源码模式入口，例如 `python -m src.me_finder.mcp_server --runtime-root ...`。

退出门槛：

- MCP 客户端能够完成 initialize、tools/list 和三种 tools/call；
- 工具标注正确；
- STDOUT 无日志污染；
- 缺少索引时服务器仍能初始化，调用工具时返回明确错误；
- 启动时间小于 Codex 默认 10 秒启动超时；
- 单次工具调用受 Codex 默认 60 秒工具超时约束，不在服务端静默无限等待。

### 里程碑 4：文献核对质量闭环

实施状态：已完成。质量矩阵、模型可见上下文和代表性工作流基线见 `docs/mcp-v1-quality-report.md`；真实 Codex 客户端接入与自然语言回答复验仍属于里程碑 5。

工作：

- 建立核对场景矩阵；
- 调整 MCP 字段裁剪和 descriptions，减少模型误用；
- 验证 instructions 对页码和歧义表述的约束；
- 对模型上下文体积做基线记录；
- 只在证据表明需要时增加字段或新工具。

必测场景：

- 完全精确命中；
- 空格、标点和 NFKC 归一化命中；
- 少量错字的模糊命中；
- 同一句在多个文献或多个段落重复出现；
- PDF 跨页命中；
- 双开页左右侧定位；
- 页码已校准；
- 只有 PDF 物理页、没有正式引用页；
- Word 文献；
- 指定文献范围；
- 无结果；
- 索引文件不存在或暂时不可用。

退出门槛：

- Codex 不把未校准物理页表述为正式引用页；
- 多候选回答明确说明歧义；
- 无结果时不编造来源；
- 搜索命中、上下文和页码证据可以追溯到工具结构化输出；
- 工具调用次数和结果体积在代表性文献库上记录完成。

### 里程碑 5：Codex 接入与用户指引

实施状态：已完成。源码接入、健康检查、隐私与故障排查见 `docs/CODEX_MCP.md`，真实 Codex 隔离验收记录见 `docs/mcp-v1-codex-e2e-report.md`。安装包 sidecar 路径仍严格留在里程碑 6。

工作：

- 提供源码开发配置；
- 提供 ChatGPT/Codex 桌面设置中的 STDIO 添加步骤；
- 提供 `codex mcp add` 示例；
- 提供 `config.toml` 示例；
- 提供 `codex mcp list` 和 `/mcp` 健康检查步骤；
- 文档明确数据隐私、只读边界和故障排查；
- 第一版只提供“复制配置命令/手工添加”，不由 MEFinder 静默修改用户 Codex 配置。

退出门槛：

- 一台未配置过 MEFinder MCP 的测试机可仅按文档完成接入；
- 重启 Codex 后工具可见；
- 软件开启与关闭两种状态都能核对同一测试引文；
- 移除 MCP 配置后不影响 MEFinder 桌面功能。

### 里程碑 6：Windows 与 macOS sidecar 打包

实施状态：代码完成；macOS arm64 与 x86_64 的 ZIP/DMG、签名、挂载、复制和真实 STDIO 冒烟通过。Windows 构建脚本、安装器/绿色版包含规则，以及安装、覆盖升级、桌面开关、卸载、绿色版移动的 Windows Server 2022 托管门禁已完成，但工作流尚未推送执行，且托管 Server 不能替代 Windows 10/11 x64 消费者实机。详见 `docs/mcp-v1-concurrency-release-report.md`。

#### Windows 安装版

工作：

- 生成可使用 STDIO 的 `MEFinderMCP.exe` sidecar；
- 修改 `desktop.spec` 或增加专用 PyInstaller spec；
- 修改构建脚本当前“发布目录只能有一个 exe”的假设；
- 让 Inno Setup 和自动更新包含 sidecar；
- 保持安装路径稳定，使 Codex 配置在覆盖升级后继续有效；
- 更新第三方依赖清单和许可证材料；
- 验证应用卸载后旧 MCP 配置产生明确的命令不存在错误。

#### Windows 绿色版

工作：

- ZIP 包含 sidecar；
- sidecar 根据 `portable.flag` 使用包内运行时数据；
- 文档说明移动绿色版目录后需要更新 Codex 命令路径；
- 不向绿色版外写隐式安装标记。

#### macOS

工作：

- 在 `MEFinder.app` 中包含可执行 sidecar；
- 固定 Codex 配置所引用的包内路径；
- 更新 `desktop_macos.spec`、签名、DMG/ZIP 校验；
- 验证 Developer ID/hardened runtime 场景下 sidecar 可由 Codex 启动；
- 验证应用覆盖升级后配置路径保持不变。

打包决策门：

- 先验证“独立 sidecar 可正确继承 stdio”；
- 再在“共享 PyInstaller 依赖目录”和“独立打包”之间选择体积更小且稳定的方案；
- 未经真实 Windows/macOS 产物验证，不假设一种 PyInstaller 结构在两端都可靠。

退出门槛：

- 三种发布物均包含可启动的 MCP sidecar；
- 构建脚本、许可证检查、隐私文件扫描和签名检查通过；
- 安装、覆盖升级、绿色版移动、macOS DMG 安装都有记录的冒烟结果；
- sidecar 不弹出桌面窗口；
- MCP 进程退出后无残留后台进程和数据库句柄。

### 里程碑 7：并发、回归与发布

实施状态：并发场景、自动化回归、文档、发布说明和回滚路径已完成；macOS 双架构发布门禁通过。Windows 短连接与发布物冒烟已进入构建脚本，仍待 Windows 10/11 x64 实机执行。详见 `docs/mcp-v1-concurrency-release-report.md`。

工作：

- 在 MCP 搜索过程中执行普通导入、索引替换和数据目录迁移测试；
- Windows 上专门验证短连接是否仍会导致数据库替换失败；
- 如果测试证明确有跨进程锁问题，再设计共享锁或应用桥接，不预先引入；
- 执行现有全量测试和新增 MCP 测试；
- 更新 README、发布说明、Windows/macOS 构建文档；
- 准备禁用与回滚说明。

发布门槛：

- 现有 UI、HTTP、导入、备份、页码校准测试无回归；
- MCP 契约、协议、质量矩阵和打包测试全部通过；
- Windows 与 macOS 实机冒烟通过；
- 本地文献内容未上传，日志不记录完整引文或文献正文；
- 发布说明明确 MCP 是可选只读集成。

### 里程碑 8：MCP v2 写能力评估与实现

实施状态：决策门完成。现有进程内导入队列、mutation gate、题录缓存和页码映射锁不能安全协调独立 MCP 进程，因此 0.4.4 保持 v1 只读，不注册写工具；进入 v2 前置条件见 `docs/mcp-v2-decision.md`。

v2 不是简单给现有数据库方法套 MCP。写操作开始前必须完成：

- 明确每个工具的用户目标和不可逆影响；
- 为每项工具设置准确的写/破坏性标注；
- 让 Codex 在执行写操作前获得审批；
- 复用现有导入队列、持久化任务、数据根门禁和 mutation gate；
- 解决 MCP 独立进程与桌面进程之间的跨进程协调；
- 给长任务设计状态查询，不让单个 MCP 调用长期阻塞；
- 为失败、取消、恢复和重复调用定义幂等边界。

推荐先后顺序：

1. `import_document`；
2. `get_import_status`；
3. `save_bibliographic_metadata`；
4. `apply_page_calibration`。

删除、批量删除、备份恢复和数据迁移保持在 v2 范围外，除非有独立需求和审批设计。

## 7. 预计文件变更范围

### 新增

- `src/me_finder/runtime_location.py`
- `src/me_finder/application/literature_verification_service.py`
- `src/me_finder/mcp_server.py`
- `tests/test_runtime_location.py` 或扩展现有数据目录测试
- `tests/test_literature_verification_service.py`
- `tests/test_mcp_server.py`
- MCP 测试夹具

### 修改

- `desktop.py`：改为复用数据根解析模块；
- `requirements-windows.txt`、`requirements-macos.txt`：增加锁定的 MCP SDK 依赖；
- `desktop.spec`、`desktop_macos.spec`：加入 sidecar；
- `build_windows_installer.ps1`、`build_portable_release.ps1`、`build_macos.sh`：构建和验证 sidecar；
- `installer/MEFinder.iss`：包含 sidecar；
- `THIRD_PARTY_NOTICES.txt` 和第三方许可证材料；
- `README.md`、构建文档和发布说明。

### 原则上不修改

- 前端搜索和文献库 UI；
- 现有 `/api/*` 请求/响应契约；
- SQLite schema；
- 搜索算法；
- 页码映射算法；
- 导入和备份流程。

如果实现被迫修改上述部分，必须先说明 MCP 的具体阻塞点，不能借 MCP 顺带重构。

## 8. 测试分层

### 8.1 单元测试

- 数据根解析；
- MCP 输入边界；
- 结果字段裁剪；
- 页码状态转换；
- 已知错误映射；
- 每次调用关闭连接。

### 8.2 契约测试

- tools/list 的名称、schema、标注；
- 三个工具的 `structuredContent`；
- `schema_version`；
- 稳定 ID 和阅读游标；
- 不出现前端 HTML 或绝对路径。

### 8.3 协议集成测试

- initialize；
- tools/list；
- tools/call；
- 正常退出；
- STDERR 日志与 STDOUT 协议隔离；
- 客户端取消时资源释放。

### 8.4 应用回归测试

- 桌面启动；
- 搜索；
- 结构化阅读；
- 文献导入；
- 索引重建/替换；
- 数据目录迁移；
- Windows 绿色版；
- macOS 数据目录和签名。

### 8.5 Codex 端到端测试

至少使用以下自然语言任务：

- “核对这句话出自哪本书、哪一页。”
- “只在《指定文献》中查这句话。”
- “这句话可能有两个错字，找最接近的原文。”
- “把命中位置前后各读几段，再判断引用有没有断章取义。”
- “只有 PDF 第 48 页，没有书内页码时，请明确告诉我。”
- “有多个来源时不要替我猜，列出差异。”

## 9. 性能与资源约束

- MCP 初始化不加载完整文献正文；
- 单次搜索结果最多 20 条；
- 单次结构化阅读最多 50 个单元；
- 不提供一次返回整本文献的工具；
- 不在服务端做无限自动重试；
- 先记录代表性数据集上的启动、搜索、窗口读取时间，再决定是否需要缓存；
- 任何缓存都不得重新引入长期 SQLite 句柄或数据根陈旧问题。

Codex 当前默认 MCP 启动超时为 10 秒、工具调用超时为 60 秒。实现需要在该边界内快速失败或完成，不依赖用户放宽超时。

## 10. 安全与隐私

- v1 工具只读；
- 不访问网络；
- 不读取 MEFinder 数据根之外的文件；
- 不返回 API Token、配置密钥、绝对路径或无关个人数据；
- 日志不记录完整引文、完整正文或工具原始返回；
- MCP 输入是系统边界，只校验工具 schema 和路径归属；
- 内部服务返回值遵循现有业务保证，不增加推测性的空值回退；
- 未知异常不伪装成“未找到”。

## 11. 主要风险与决策点

| 风险 | 当前处理 | 触发进一步设计的证据 |
| --- | --- | --- |
| Windows 文件锁阻碍索引替换 | 每次调用短连接 | 实机并发测试仍能稳定复现替换失败 |
| 数据根迁移后 MCP 读旧目录 | 每次调用重新解析 | 路径解析性能成为可测瓶颈 |
| MCP 结果过大 | 精简 DTO、限制候选和窗口 | 质量测试证明缺少必要证据 |
| 工具过多导致模型误选 | v1 固定三个目标明确的工具 | 出现不能由现有三工具完成的重复任务 |
| sidecar 显著增大发布包 | 打包阶段比较共享/独立依赖 | 两端实测体积和签名结果 |
| 安装升级后配置失效 | 使用稳定安装路径 | 安装器或 macOS 包结构实测变化 |
| 搜索分数被误当验证结论 | instructions + 输出命名 + E2E 测试 | Codex 仍反复产生错误结论 |

## 12. 回滚与兼容策略

- MCP 是可选 sidecar，桌面应用不得依赖 MCP 才能启动；
- MCP 初始化失败不得影响 UI、HTTP 和索引；
- 发布后如发现问题，可在下一版本移除或禁用 sidecar，不迁移用户数据；
- 工具字段新增保持向后兼容；删除或改变语义需要新的 schema 主版本；
- Codex 配置属于用户外部配置，卸载时不主动删除；文档提供手工移除方式；
- v1 不写数据库，因此不需要数据回滚方案。

## 13. 完整完成定义

只有同时满足以下条件，才能认为“MEFinder MCP 文献核对功能”完成，而不是只完成原型：

- 三个只读工具契约稳定并有自动测试；
- 源码模式可以被 Codex 调用；
- 文献核对质量矩阵通过；
- Windows 安装版、Windows 绿色版和 macOS 发布物均包含可用 sidecar；
- 新用户能按文档完成配置和健康检查；
- 软件开启/关闭、导入、重建和迁移场景通过并发验证；
- 现有桌面功能无回归；
- 许可证、隐私扫描、签名和发布检查通过；
- README、构建文档、故障排查和发布说明完成；
- 有明确的禁用和回滚路径。

达到上述定义后，再进入 MCP v2 写能力；不能用尚未完成的打包、接入或质量工作换取提前扩展工具数量。
