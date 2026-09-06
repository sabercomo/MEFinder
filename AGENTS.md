# AGENTS.md — MEFinder 代理工作规范

本文件约束 AI 代理(以及人类协作者)在本仓库中执行迭代的方式。
目标:任何一次会话开始时,代理读完本文件即可无损接手工作,不依赖对话历史;历史工作状态由 `project-save-load` 技能维护的 `.project-memory/` 归档承载(见 §4)。

> Claude Code 用户:本文件通过仓库根目录的 `CLAUDE.md` 以 `@AGENTS.md` 引入,两处内容一致。

---

## 0. 项目速览与不可违反的原则

MEFinder 是本地优先(local-first)的 PDF / Word / EPUB 文献原句检索、页码定位与引文辅助工具。

- **技术栈**:Python 3.12 + SQLite(FTS5 trigram)/ pywebview 桌面壳 / PyInstaller 打包 / 原生 JS + CSS(无框架)。
- **架构**:src 布局(`src/me_finder/`),`persistence/` 分层,DB schema 以 `PRAGMA user_version` 版本化(以代码中实际值为准,勿在文档里写死数字)。
- **三格式入口统一**:导入、扫描、删除、查询四处白名单必须同步为 `{".pdf", ".docx", ".epub"}`(见 `web_http.py` / `document_file_store.py` / `document_query_service.py` / `document_deletion.py`)。
- **核心原则(违反即返工)**:
  1. 本地优先:数据不离开用户机器;任何新功能不得引入必须联网的路径。
  2. 不重解析:下游模块(如 `markdown_export.py`)只读已入库数据,不重新解析源文件、不 OCR。
  3. 引用可定位:一切检索/对齐结果必须携带页码锚点与字符区间,丢失定位信息的"优化"一律不接受。
  4. 存量兼容:DB 迁移走 `persistence/migrations.py` + `schema_installers.py`,`persistence/` 内禁止 `from ..` 上行 import(有边界测试钉死)。
  5. 证据优先:涉及质量结论(阈值、对齐准确率)的改动必须引用 `reports/` 实证报告,不得凭感觉调参。
  6. 页码不虚构:EPUB 只用出版方页码,见 §3.5。

---

## 1. 文档管理标准

### 1.1 目录职责(单一归属,不放错地方)

| 位置 | 职责 | 更新时机 |
|---|---|---|
| `README.md` | 功能总览、MCP 工具数量/分组说明 | 功能集合变化时 |
| `docs/RELEASE_NOTES.md` | 正式发布版说明(测试数、构建产物、SHA-256) | 每次正式发版 |
| `docs/release-notes-X.Y.Z.md` | 单版本说明,顶部必须带日期行与当前结论 | 该版本迭代期间 |
| `docs/contracts/` | 版本化契约(`vX.Y.Z-http-api.json` 等),文件名含版本号 | API/接口变化时 |
| `docs/issues/` | 议题与实验记录,一个问题一个文件(如 `note-layout-alignment-conflicts.md`) | 问题发现/有新证据时追加,不删除旧结论 |
| `reports/` | 实证报告(阈值扫描、全库重跑等),`docs/issues/` 引用它们 | 实验完成时 |
| `.project-memory/` | 会话交接归档(状态、TODO、决策、技术笔记、注意事项、handover),由 `project-save-load` 技能维护 | 每次迭代收尾"存档",见 §4 |
| `MCP_CLIENT_SETUP.md` | MCP 客户端接入步骤 | 工具清单变化时 |

### 1.2 写作规则

- 文档正文用中文;代码标识符、命令、文件名保留英文原文。
- `docs/issues/` 记录必须区分**事实**(带报告引用)与**推断**(显式标注"推断"),后续补充证据时以日期行追加,不覆写旧结论。
- 数量类断言(如"13 个 MCP 工具")改动后必须全仓库 grep 同步,历史教训:MCP 工具从 9 个变 13 个时 README 三处没更新。
- release notes 顶部格式:`YYYY-MM-DD:一行结论`,先说"能不能用",再列变更。

---

## 2. Git 管理标准

### 2.1 提交信息

格式:`type(scope): 中文标题`,正文用中文 bullet 列出要点,代理参与生成时附 `Co-Authored-By: <模型名> <noreply@...>`。

type 取值:`feat` / `fix` / `docs` / `test` / `refactor` / `polish`(视觉与交互微调,本仓库特有)/ `chore`。

示例(取自本仓库真实提交):

```
feat(alignment): 模型选择持久化、正文区域参数化、DB v6

- 拆出 embedding_models.py(模型注册表与阈值)和
  managed_embedding_models.py(下载管理,managed-component 合约)
- DB schema v5→v6:alignment_links 加 confidence 与 anchor_key 列
- 补 docs/issues/d-bertalign-body-corridor-experiment.md
```

### 2.2 提交粒度与分支

- 一个迭代主题一次提交;测试基线同步、指纹更新可并入同一次提交,但必须在正文说明。
- 日常开发直接在 `main` 上按主题推进(当前仓库惯例);涉及大面积重构或实验性方案时开 `feat/*` 分支,验证后合入。
- 禁止 force push `main`;禁止提交构建产物以外的临时文件(`release/` 下按版本命名并附 SHA-256 的除外)。
- 提交前必须:全量测试(unittest)全绿 + ruff(pyflakes F)无新告警 + 前端相关守卫通过。**具体命令见 §3.6**。

### 2.3 版本与发布

- 版本号格式 `vX.Y.Z`;`docs/contracts/` 与 `docs/release-notes-X.Y.Z.md` 随版本走。
- 发布门禁:全量测试通过、Windows/macOS 构建冒烟(workflow)、安装包与便携包 SHA-256 落入 RELEASE_NOTES.md。
- 质量未达标的实验特性(如 E5 对齐)不得设为默认,必须在 release notes 显式标注"实验档"并写明门禁状态。

---

## 3. 代码管理标准

### 3.1 Python

- src 布局,模块职责单一:注册表类配置(如 `embedding_models.py`)与运行时管理(如 `managed_embedding_models.py`)分文件。
- 带类型注解;公共函数写 docstring(英文即可)。
- 禁止为绕过架构边界引入函数内 import 环(参照 `docs/refactor-v0.5.0.md` 的分层原则)。
- ruff 配置仅承载 lint(`["F"]` pyflakes),当前为零告警并阻塞回归。

### 3.2 前端(原生 JS/CSS)

硬性守卫(有测试钉死,改动后必须同步基线):

- `reader.js` 禁止 `innerHTML`;DOM 构造走已有辅助函数。
- CSS 禁止裸 hex 颜色,全部落在 `00-themes.css` 主题 token(圆角已收敛为 `--radius-xs/sm/md/lg/xl/pill`)。
- 每个组件 CSS 头部保留 Hallmark 注释(`component / genre / pre-emit critique / 检查编号`),新组件必须补齐并通过 contrast/slop/chrome/tokens/responsive 各项检查。
- 全局命令预算:每个 JS 文件的全局符号数有基线(如 `30-library.js` 当前 47),新增全局函数必须更新对应测试断言。
- 装配指纹基线:静态资源改动后同步 `test_frontend_assets.py` 指纹,更新命令见 §3.6。

### 3.3 测试

- 新功能必须带测试;涉及偏好的功能要同步 `test_theme_system.py` 的全量偏好快照断言(历史教训:`reader_line_mode` 加了偏好但快照没更新)。
- 测试命名 `test_<模块>_<场景>.py`;实验验证类测试(如 `test_bertalign_corridor_experiment.py`)允许存在,但须注明是实验路径。
- 修 bug 先写复现测试,再修,提交正文引用测试名。
- **测试运行器是 `unittest`(与 CI 一致),不是 pytest**;命令见 §3.6。

### 3.4 数据库

- schema 变更走 `user_version` 递增 + `migrations.py` + `schema_installers.py`,禁止在业务代码里裸 `ALTER TABLE`。
- 向量/嵌入类数据必须携带 `model_id`,禁止跨模型混用向量空间(向量漂移)。

### 3.5 EPUB 处理与检索标准

EPUB 与 DOCX 共用文本语料通道(`corpus/raw_docx/`),不经 MinerU / PDF 视觉解析管线;`import_orchestrator.index_text_document` 单本解析发布,不重建全库。

- **提取**:`extractors.py` 按 `EPUB_TEXT_BLOCKS` 白名单(p / h1-h6 / blockquote / li / pre / dd / dt / figcaption / address)决定段落边界;命名空间 `EPUB_NS`(idpf 2007/ops);`source_id` 形如 `epub-<hash>`。新增可提取块类型必须同时考虑检索与对齐两侧的影响,不得只改一边。
- **页码原则(EPUB 最高守则)**:**只采用出版方提供的 page-list / pagebreak,绝不按屏幕重排生成页码**。映射方法枚举固定为 `epub_page_list`(EPUB页码表)与 `epub_pagebreak`(EPUB分页标记),前端查表 `06-pure.js` 的 `mappingMethodLabel`。无出版方页码时如实标注 `uncalibrated`,不猜、不补、不推算——这是引用定位可信度的根基。
- **检索**:全文检索走与 PDF/Word 相同的 `searchable_paragraphs` + FTS5 trigram;`source_type` 取值固定 `{all, word, epub, pdf}`,后端在 `search.py` 归一,前端 `20-search.js` / `30-library.js` 的 facet 判定与之保持一致(EPUB 在 DB 里可能挂在 `source_type == "word"` 下,靠 `source_format == "epub"` 区分,两侧判定逻辑必须同步改)。
- **元数据**:导入时从 OPF 填好(来源显示"EPUB 元数据");「自动识别 / 重新识别」读 PDF 页面,仅 `source_type === 'pdf'` 显示;EPUB 走「查图书信息」+ 手动。书目信息面板对 PDF 与 EPUB 共用,UI 门不得再写成 `source_type==='pdf'` 独占。
- **对齐**:EPUB 是自动对齐的一等公民(`text_alignment.py`:"自动对齐只支持 PDF 和 EPUB 文献");对照阅读、作品组成员均含 EPUB。
- **导出**:EPUB 导出(`epub_export.py`)与 Markdown 导出共享 `markdown_export_normalize.py` 规范化层与同一 `PageMarker`;渲染分叉只在各自 renderer 里(Markdown 渲染成 `<!-- printed_page: N -->` 注释,EPUB 渲染成 EPUB 3 `pagebreak` 锚 + page-list nav)。页码模式默认 `printed`(`preferences.py` 的 `DEFAULT_PAGE_MARKER_MODE`)。改页码策略时两格式必须同步验证。

### 3.6 运行 / 测试 / lint 命令(权威)

> 本机 PATH 上的 `python` 是 Windows Store 桩(不可用),`py -3` 太旧(3.8)。**一律用项目 venv 的解释器**:`D:\ME_Finder\.venv-windows\Scripts\python.exe`(Python 3.12)。以下命令在**仓库根目录**执行;测试用 `-t .` 从根发现,源码经 `from src.me_finder ...` 导入,**无需设置 `PYTHONPATH`**。

**全量测试(发布门禁,与 CI 一致):**

```bash
.venv-windows/Scripts/python.exe -m unittest discover -t . -s tests
```

**跑单个模块 / 单个用例(快速迭代):**

```bash
.venv-windows/Scripts/python.exe -m unittest tests.test_alignment_anchor_gates
.venv-windows/Scripts/python.exe -m unittest tests.test_alignment_anchor_gates.ClassName.test_method
```

**Lint(pyflakes,门禁):** CI 用 `pipx run ruff check .`。venv 内默认未装 ruff/pipx;本机跑需先 `.venv-windows/Scripts/python.exe -m pip install ruff` 后 `.venv-windows/Scripts/python.exe -m ruff check .`,或直接依赖 CI 的 lint job。规则集仅 `["F"]`,须零新增告警。

**启动无头 Web 服务(浏览器里看真实数据):**

```bash
$env:PYTHONPATH="D:\ME_Finder\src"; & D:\ME_Finder\.venv-windows\Scripts\python.exe -m me_finder serve --host 127.0.0.1 --port 8765
```

默认库是 `DEFAULT_DATABASE_PATH`(较小的开发库,非用户真实库)。SQLite 会被占用,验证完停掉进程释放端口/DB。web.py 在 import 时一次性拼装 HTML,改模板后浏览器要**重启 serve 进程**才生效(reload 不够)。

**更新前端装配指纹基线**(改 index.html / static 下 CSS·JS 后 `test_frontend_assets.py` 会失败,取新值填回 `BASELINE_BYTES` + `BASELINE_SHA256`):

```bash
PYTHONPATH=src .venv-windows/Scripts/python.exe -c "import tests.test_frontend_assets as t,hashlib;p=t.HTML.encode('utf-8');print(len(p),hashlib.sha256(p).hexdigest())"
```

---

## 4. 迭代收尾流程(每次迭代完成后强制执行)

按顺序完成以下五步,缺一不可。任何一步未完成,迭代不算结束:

1. **同步测试基线**:全量测试(§3.6 的 unittest 命令);若改了前端/偏好/schema,更新对应指纹、快照、预算断言。
2. **更新文档**:按 §1.1 的"更新时机"列逐项检查——release notes、contracts、README、issues 记录、reports(有实验的话)。检查数量类断言是否全仓一致。
3. **提交仓库**:按 §2.1 格式写提交信息,推送到远端;一次迭代一个主题提交。
4. **压缩上下文(存档)**:执行 `project-save-load` 技能的 **`存档`** 流程。它把本次迭代结论固化进 `.project-memory/`(state / session-summary / todo / decisions / technical-notes / cautions / handover / logs),带脚本校验、去重与脱敏。完成后在本会话内声明"上下文已交接,新会话可从 `.project-memory/` 恢复(读档)"。**不再手写 `docs/agent/SESSION.md`**——该单文件交接机制已被 `project-save-load` 归档取代。
5. **自检**:确认工作区干净(`git status` 无未跟踪的应提交文件)、`.project-memory/` 校验通过(`validate` 无致命告警)、无 `待核实` 之外的模板占位符残留。

> 交接载体的唯一真相是 `.project-memory/`(由 `project-save-load` 维护)。存/读档的具体文件结构、读取顺序、冲突处理规则以该技能的 `SKILL.md` 与 `references/schema.md` 为准,本文件不重复描述。

---

## 5. 会话启动清单

新会话的第一组动作:

1. 读本文件(或经 `CLAUDE.md` 的 `@AGENTS.md` 自动加载)。
2. 执行 `project-save-load` 技能的 **`读档`** 流程,从 `.project-memory/` 恢复工作状态(handover → state → todo → cautions → decisions → technical-notes → 最新 log)。
3. `git log --oneline -15` 确认归档描述与远端一致;不一致时以 git 与当前工作区为准,并先修订归档。
4. 从"未完成 / 下一步"取任务,开工。
