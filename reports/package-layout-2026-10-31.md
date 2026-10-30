# 顶层模块按领域分子包核验（0.6.1，2026-10-31）

## 范围与基线

本轮从 `0ea6c61` 开始，落实 2026-10-31 架构评估的第 4 项。只移动模块位置并同步所有引用，不改函数逻辑、检索/对齐结果、页码规则、schema、HTTP/MCP 接口或前端。

**更正**：评估时称顶层有 164 个模块，统计口径有误，把 `application/` 等四个既有子包的 `__init__` 也算了进去。按 Git 跟踪文件计，`83ad26c` 时 `src/me_finder/` 顶层有 161 个 `.py` 文件（160 个模块加 `__init__.py`）。

| 指标 | 调整前（`0ea6c61`） | 调整后 |
|---|---:|---:|
| `src/me_finder/` 顶层 `.py` 文件 | 163 | 76 |
| 领域子包 | 4（application / persistence / tasks / large_document） | 11 |

## 分批移动（每批一次提交）

| 提交 | 子包 | 模块数 | 说明 |
|---|---|---:|---|
| `d7e969c` | `bibliographic/` | 13 | `bibliographic_*` 与书目检索、CNKI 引文、文集元数据 |
| `c64fff6` | `export/` | 8 | Markdown / EPUB / 文档包导出与共享规范化层 |
| `fce624e` | `components/` | 8 | 组件目录、三个安装器、共用安装机制、下载测速 |
| `8b97f00` | `parsers/` | 15 | MinerU 云端与本地、本地 OCR、视觉模型、解析器契约 |
| `fbf40ab` | `pdf/` | 11 | PDF 原生解析、段落锚点、页码映射与页内刷新 |
| `b28e918` | `search/` | 11 | 检索门面 `search.py` 改为 `search/engine.py`，各阶段模块 |
| `21a8e66` | `alignment/` | 21 | 分段、锚点、嵌入、对齐计算进程、人工校正、作品对照 |

包内模块去掉了与包名重复的前缀（如 `alignment_generation` → `alignment/generation`、`search_recall` → `search/recall`）。顶层保留：命令行与 MCP 入口（用户 MCP 配置直接启动 `src.me_finder.mcp_server`）、`web` 服务入口、16 个 controller、`*_assembly` 装配、web/HTTP 传输、桌面壳，以及尚未归类的文献库与导入模块。

## 做法

1. 用脚本按映射表 `git mv`，再改写全部引用：包内相对导入按每个文件的新位置重算；`src/`、`tests/`、`scripts/`、`tools/`、`packaging/*.spec` 与根目录脚本中的 `src.me_finder.<旧名>` / `me_finder.<旧名>`（含 `mock.patch` 字符串、`-m` 启动参数、spec 的 hiddenimports）、`src/me_finder/<旧名>.py` 路径，以及测试里守卫用的 `"<旧名>.py"` 字面量。只改模块路径的导入语句保留原排版。
2. **不保留旧路径转发模块**：仓库内引用已全部改写；转发模块会让测试的 `mock.patch("旧路径.x")` 打到转发对象而非实际模块，静默失效。
3. 每批运行 Ruff、逐模块导入冒烟、全量 unittest 后提交。

## 需要手工处理的点

- `__file__` 相对路径：`components/local_ocr_installer.py` 改从包根读取 `local_ocr_manifest.json`（打包 spec 仍把该文件放在 `src/me_finder/`）；`alignment/compute.py` 与 `components/alignment_runtime.py` 源码模式的仓库根由 `parents[2]` 改为 `parents[3]`。`runtime_location.py`、`mcp_server.py` 仍在顶层，未变。
- 对齐计算进程：开发态 `-m src.me_finder.alignment.compute_worker`；冻结包与已装运行时启动 `me_finder.alignment.compute_worker`；`tools/slim_main_package.py` 的 `FROZEN_WORKER_MODULE` 同步。随包源码树按目录递归收集，新子包自动包含。磁盘上的安装状态与收据不记录模块路径，无需迁移。
- 守卫测试：`test_slim_main_package` 原按文件名核对随包源码，两个 `embedding_models.py` 无法区分，改按包内相对路径；`test_pdf_extraction_boundaries` 与 `test_search_pipeline_boundaries` 的依赖表改按包内模块名，规则与阶段表不变（双向相等断言仍通过，证明依赖结构未变）；安装器守卫改认包内 `install` 模块。
- 改写工具缺陷两处，均在提交前发现并修正：多名称的紧凑导入被重排为每行一个，使 `import_assembly.py` 超出 290 行预算（恢复原排版）；`search` 既是旧模块名又是新包名，`from src.me_finder.search import recall` 被二次改成 `search.engine`，影响一个测试与一个实验脚本（已改回，并调整工具改写顺序）。

## 推送后发现的漏改

`desktop.py`（仓库根的桌面入口）在冻结包以 `alignment-compute-worker` 参数启动时导入对齐计算进程模块。改写工具已经改好该文件，但各批只按 `src/`、`tests/`、`scripts/`、`tools/`、`packaging/` 暂存，它没有进入 `21a8e66`，并随 `a56961c` 一起推送到 `origin/main`。在 `660288c` 修复之前，冻结包的对齐计算进程会找不到旧模块。全量测试不导入根目录入口，所以没有暴露。

补救：`660288c` 提交该改写；复核根目录脚本、构建脚本、CI 工作流、打包 spec 与 `tools/`，没有其他旧模块路径。新增守卫 `test_root_entry_scripts_import_existing_modules`，静态核对 `desktop.py`、`mefinder_mcp.py` 的包内导入目标存在；用修复前的 `desktop.py` 验证，该守卫会报出 `src.me_finder.alignment_compute_worker`。期间没有打包，已安装的应用不受影响。

## 验证

- 每批全量 unittest：书目、组件、PDF、检索四批各有守卫或导入问题如上，修正后专项通过；导出、解析器、对齐三批 2,855 项一次通过（39 项条件跳过）。最终提交 `21a8e66` 全量 2,855 项通过（39 项跳过，187.857 秒）。
- Chrome 回归 16 项通过（52.704 秒）；`python -m src.me_finder --help` 正常；`src.me_finder.mcp_server` 可导入；`test_managed_alignment_runtime` 以 `-m` 实际启动新的对齐计算进程模块并通过 `--verify` 检查。
- 全包逐模块导入 0 失败；`ruff check src tests scripts tools` 通过。

未实际重打 macOS / Windows 安装包；PyInstaller 静态导入跟踪与 `worker_source_datas` 递归收集按代码核对，冻结程序需在下次打包时验收。

## 2026-10-31 补记：macOS arm64 测试包实测

用 `PYTHONUTF8=1 MEFINDER_PYTHON="$PWD/.venv-macos312-arm64/bin/python" ./build_macos.sh` 从 `adde09b` 打测试包，产物 `release/MEFinder-v0.6.1-macos-arm64.{zip,dmg}`（本地，未上传、未安装）。构建内门禁 2,856 项通过（39 跳过）；脚本自带的 MCP 侧车 STDIO 冒烟五次、签名校验、ZIP/DMG 校验均通过。SHA-256：ZIP `a895dd238171001bc25c99968271745504cab114e6c1f1b15f7e655641086967`，DMG `a121106aeaa1d10a27c52e11683dbfb462f1cfbb678560cc40e8b09caf739f76`。

在临时目录解出 ZIP 后的实测：

1. **应用自身分派对齐计算进程**：`MEFinder alignment-compute-worker --probe` 返回协议 1 的 hello；计算依赖显示为无，符合主包精简、计算栈在独立运行时的设计。证明 `660288c` 的修复在冻结包内生效。
2. **已安装的独立运行时调用包内源码**：本机 `text-alignment` 运行时的 Python 从包内 `Contents/Frameworks/me_finder/alignment/compute_worker.py` 导入新模块；`--probe`、`--verify` 均报告 numpy / fastembed / onnxruntime 可用。经应用自身的 `SubprocessAlignmentComputeRunner` 跑一次 4 句英译中的真实对齐（MiniLM 模型，已在本机缓存），结果与分包前 `83ad26c` 源码逐项相同（区间、cost、confidence 精确到 6 位小数）。
3. **MCP 侧车对真实库只读调用**：包内 `MEFinderMCP` 与分包前 `83ad26c` 源码模式的 MCP 服务，对同一真实库（461 个来源、70 次对齐运行）执行 8 个只读工具（list_documents、search_passages、locate_quote、find_parallel_passages、describe_page_mapping、list_sections、read_document_window、list_alignment_corrections），输出 JSON 逐字节相同；其中 find_parallel_passages 读到对齐数据返回 1 条。
4. **桌面应用启动**：以 `ME_FINDER_APP_DATA_ROOT` 指向空的临时数据目录启动，0.2 秒内本地后端就绪；主页 HTML 与 14 个只读接口（文献库、来源、页码校准、作品组、偏好、本地 OCR、MinerU 配置、解析统计、对齐运行时与模型、译本作品、视觉模型等）均返回 200；日志无错误，收到终止信号后正常退出。

未覆盖：窗口界面的人工点击；Windows 与 Intel 包；用户真实库上的写操作与重新导入。
