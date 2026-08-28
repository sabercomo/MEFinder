# MEFinder v0.5.0 架构重构记录

本轮在 0.4.8 分层的基础上，收口"装配、门禁、持久化方向、工具链、前端作用域"五处边界。**不推倒重写**：依赖图验证过 application 层无一条边指向传输层、SQL 已收进 persistence、导出层职责清晰。以下全部改动均在整套 `tests/`（`unittest discover`）全绿下完成。

## 已完成

### 1. 发布门禁：手工名单 → `unittest discover`
四份构建脚本（`build_macos.sh`、`build_windows_dist.cmd`、`build_windows_installer.ps1`、`build_portable_release.ps1`）曾各自维护一份 `unittest` 模块名单，三份互不相同，**静默漏掉了 48 个测试模块**（含架构守卫、HTTP 契约、迁移、TaskEvent 等自称"门禁"的测试）。改为 `unittest discover -t . -s tests` 后，实跑量 **87 模块 → 141 模块 / 1868 测试**。

- 新增 `tests/corpus_fixtures.py`：缺私有语料或可选开发依赖（python-docx）的用例改为 `skipUnless` **可见跳过**，而非靠漏列隐形排除。
- `test_windows_packaging` / `test_mcp_packaging` 中断言脚本含特定模块名的元测试，改为断言"跑 discover"，抗漂移。
- 修真 bug：`test_import_orchestrator` 在 macOS `$TMPDIR` 符号链接下未 `.resolve()`。

### 2. 拆分 `web.py`（1200 → 496 行）
`web.py` 曾精确卡在 1200 行上限、0 余量。抽出 `web_runtime.py` 作为组合根：`build_application_runtime(context, ...) -> ApplicationRuntime`，承载 15 个服务 + 四张路由表 + 生命周期。`web.py` 只剩入口、平台 PDF 打开、`serve`。晚绑定装配 lambda **原序原样搬移**，行为不变。测试的 monkeypatch 与源码扫描 wiring 测试从 `web` 重定向到 `web_runtime`。边界上限"只降不升"：web.py 1200→700，web_runtime.py=950。

### 3. persistence 迁移层不再向上依赖领域模块
`persistence.migrations` 曾用函数内 import 向上调 `document_groups` / `text_alignment` 的 `install_*_schema`，形成两条逻辑环。两个 installer（含私有 `_now`/`_table_exists` 副本）下沉到 `persistence/schema_installers.py`：migrations 平级 import、领域模块反向下依赖。新增边界测试禁止 `persistence/` 内出现任何 `from ..` 上行 import。

> 残留一条 `database ↔ text_alignment ↔ calibration_library ↔ bibliographic_metadata ↔ database` 环，由 database.py 里一条懒 import 兜住、良性；根在 `bibliographic_metadata` 顶层依赖 `database.paragraph_payload_for_storage`，属领域纠缠，本轮未动。

### 4. 工具链与 CI
此前 GitHub workflow 全是 `workflow_dispatch`，push/PR 不跑任何东西。新增 `.github/workflows/ci.yml`：`tests` job 阻塞跑整套 discover，`lint` job 跑 ruff pyflakes（`["F"]`，先 advisory）。`pyproject.toml` 仅作 ruff 配置载体（无 `[project]` 表——应用从 src 运行、PyInstaller 打包）。清掉全库 15 处未用 import；`web_http.py` 用 `Optional` 未导入的潜伏 bug 一并修掉。

### 5. 前端全局作用域收敛（#7，进行中：6/13）
`static/js/*` 13 个文件共享一个全局作用域（拼接加载）。按 `reader.js` 的 IIFE 范式逐个私有化，仅显式 `global.*` 导出被外部/内联 onclick 引用的公共面。已包裹 **6 个**，约 **190 个私有 helper 移出全局**：

| 文件 | 声明 | 公共 | 私有 |
|---|---|---|---|
| 05-theme-engine.js | 31 | 17 | 14 |
| 70-vision.js | 131 | 46 | 85 |
| 10-shell.js | 13 | 10 | 3 |
| 20-search.js | 50 | 19 | 31 |
| 50-calibration.js | 30 | 16 | 14 |
| 60-settings.js | 76 | 33 | 43 |

**模式与约束**：
- IIFE 实参 `typeof window !== 'undefined' ? window : globalThis`，兼容 node（`module.exports` / vm）单测。
- 每个包裹文件首行加唯一 `// module: <name>` 标记，避免 `test_frontend_assets` 的加载顺序锚点撞车。
- `test_inline_handlers_have_definitions` 增强为同时识别 `global.X=` / `window.X=` 导出。
- node 纯逻辑测试需要的内部函数，用 `typeof module !== 'undefined'` 门控的额外块挂到 globalThis，浏览器不暴露。
- **每个文件都做了浏览器实时冒烟**（公共符号在 window、私有的不在、无控制台错误）。其中一次冒烟抓到了静态测试漏掉的运行时 bug：`00-state.js` 顶层初始化调用了 `loadLocalCitationStyles`/`loadLocalSelectedCitationStyle`，它们原在被包裹的文件里、靠跨脚本函数提升才可用——已把这两个 localStorage 读取器移进 00-state（全局）。

## 未做 / 待后续立项

- **#7 剩余 7 个文件**：`30-library` / `40-bibliography` / `80-import` 有 `vm.runInContext` / node 白盒测试，**覆盖它们自己定义的内部函数**做间谍断言（如 `context.loadDocumentGroups = spy`）。IIFE 化后内部裸调用解析到词法私有版本，覆盖失效——需先把前端逻辑改成**依赖注入**（渲染器/收集器参数传入而非全局覆盖）再包，属行为契约变更，应单独立项。`00-state`（共享 store 中枢）、`06-pure`（纯函数、被大量 node 测试 eval）、`25-toast`/`90-init`（各 3–5 声明）收益低，暂缓。
- **装配晚绑定显式化**、**测试改用 `ApplicationRuntime` 而非 Handler 私有属性**：#2 之后已具备条件，但当前均正常工作，属洁癖优化。

## 门禁与约束（新增/强化）
1. 发布脚本一律 `unittest discover -t . -s tests`，不再维护手工名单；环境缺失用 `skipUnless` 可见跳过。
2. `web.py`、`web_runtime.py`、`web_http.py`、`database.py` 行数上限只降不升；碰上限先迁出真实职责。
3. `persistence/` 不得出现 `from ..` 上行 import（`test_architecture_boundaries` 守卫）。
4. 新增/删除 HTTP 路由必须同步 `docs/contracts/*.json`（`test_http_api_contract` 守卫，路由字典现在 web_runtime.py 里）。
5. 新包裹的前端文件：唯一 `// module:` 首行标记 + 显式 `global.*` 导出 + 浏览器冒烟。
