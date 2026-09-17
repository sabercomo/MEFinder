# 第二阶段复审与修复

2026-09-17：原实现未通过本轮审核；已修复发现的问题，本机代码门禁及 macOS ARM 冻结计算闭环通过。跨平台正式交付仍未验收完毕。

## 审核范围与结论

基于 `f785ab7` 审核 2A–2E：计算进程边界、结果发布、安装管理、主包与 MCP sidecar 精简。保持算法 v22、默认 batch64、模型及缓存语义不变；不推进第三阶段，不访问或修改正式文献库。该基线的 CI run `35121810954` 中 Linux / Windows 测试及 lint 均成功。

## 已复现并修复

| 问题 | 影响与修复 | 回归证据 |
|---|---|---|
| 安装完成只刷新卡片，计算总状态仍不可用；卸载后模型仍显示已下载 | 运行时响应统一刷新计算状态，维护结束重新读取模型 | `test_install_completion_refreshes_compute_availability`、`test_uninstall_completion_refreshes_model_files` |
| 旧 GET 晚于安装响应抵达 | 旧状态覆盖安装中并停止轮询；用请求序号淘汰过期响应，操作开始清除旧轮询 | `test_old_status_read_cannot_stop_install_polling` |
| 首次状态读取失败，卡片保持隐藏 | 无安装/恢复入口；展示失败原因及可工作的重新读取按钮 | `test_initial_read_failure_has_a_working_retry` |
| 不支持平台被描述成随应用提供；不兼容安装显示就绪 | 说明与就绪态依据真实计算能力 | `test_unsupported_platform_does_not_claim_bundled_runtime`、`test_incompatible_install_does_not_claim_ready` |
| 取消已经退出的安装子进程被报告为失败；验证捕获取消后改写成异常 | 退出后先识别取消，验证保留取消类型；验证完成至发布前再次检查取消 | `test_cancelled_command_exit_is_not_reported_as_install_failure`、`test_verify_preserves_cancellation_outcome`、`test_cancellation_after_verify_does_not_publish_staging` |
| uv 压缩包下载 100% 被沿用到 Python/依赖安装 | 切换安装阶段清除下载字节及百分比，避免长时间假报 100% | `test_python_install_does_not_reuse_uv_download_progress` |

以上 10 项测试均先在原实现复现失败，再验证修复。6 项 UI 测试通过 Node VM 执行实际 `60-settings.js` 和按钮回调，覆盖异步状态变化；不以源码字符串断言替代行为验证。同步 HTML 装配指纹，未新增全局符号。

## macOS ARM 冻结验证

使用当前源码、项目 Python 3.12 和 PyInstaller 定向构建，产物与临时库均隔离于系统临时目录。桌面构建复用现有 macOS stage 资源；在临时 `.app/Contents/Resources` 写入 `portable.flag`，确保组件也落在临时目录。仅设置 `ME_FINDER_APP_DATA_ROOT` 不足以隔离 macOS 的稳定组件目录。

- **真实运行时安装**：启动冻结桌面可执行，经其 `/api/text-alignment/runtime` POST install，真实下载 uv、创建 Python 环境、安装依赖、验证并发布回执。设置 `SSL_CERT_FILE=/etc/ssl/cert.pem` 使用本机系统信任根，安装成功。
- **离线计算**：只复制既有 MiniLM 模型文件到临时模型目录，不复制向量缓存或安装回执；`HF_HUB_OFFLINE=1`。通过冻结应用 `/api/text-alignments/generate` 完成公开合成 fixture 的计算和发布，与隔离库内原进程内路径比较。
- **结果相等**：4 条链接、20 个成员；排序、cost、confidence、anchor_key、review_status、side、segment_id、member_order 逐值一致；20 条 text_segments、20 条 text_segment_paragraph_spans 全字段一致。segment_sets 和 alignment_runs 除运行 ID、创建/完成时间外一致。该样本没有 PDF text_segment_spans，不能据此声称新增 PDF 页码跨度验收。
- **真实卸载**：经冻结应用 POST uninstall，组件及其模型目录删除，成果成员 20→20 保留；卸载后同一冻结后端搜索命中、已有对齐目标读取正常。
- **真实 sidecar**：重新构建 `MEFinderMCP`，48,148,288 B。使用 PyInstaller CArchiveReader 检查内嵌 PYZ 目录与归档条目，没有 NumPy / ONNX Runtime / fastembed 模块；通过 MCP SDK STDIO 执行 `find_parallel_passages`、`locate_quote`、`list_alignment_corrections`、`list_documents`，全部成功且对应英文原句一致。MCP serverInfo 版本遵循既有 v0.5.1 工具契约，与应用版本 0.5.5 分开。

机器摘要见 [JSON](alignment-phase2-review-2026-09-17.json)。这些是定向冻结包的生产 HTTP/STDIO 验证；未声称人工点击全部桌面 UI，也未完成发布级签名、ZIP/DMG 或首次联网下载模型。验证结束后终止本轮临时桌面进程，不把该清理当作正常 GUI 退出验收。

## 自动门禁与复现入口

本机全量 **2369 项，23 项跳过，其余通过**（132.142 秒）；Ruff(F) 通过；JS 语法与前端装配/预算/主题等守卫通过。Ruff 仅排除原有未跟踪 `.codex-tmp/` 外部实验源码，CI 干净 checkout 不含它。

```bash
PYTHONUTF8=1 NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests
.venv-macos312-arm64/bin/python -m ruff check . --extend-exclude .codex-tmp
node --check src/me_finder/static/js/60-settings.js
.venv-macos312-arm64/bin/python -m unittest tests.test_alignment_runtime_settings_ui tests.test_alignment_runtime_review
# 在临时目录构建，不覆盖现有 dist：
# .venv-macos312-arm64/bin/python -m PyInstaller packaging/mcp_sidecar.spec --distpath <scratch>/mcp-dist --workpath <scratch>/mcp-work --noconfirm --clean
# MEFINDER_TARGET_ARCH=arm64 .venv-macos312-arm64/bin/python -m PyInstaller packaging/desktop_macos.spec --distpath <scratch>/desktop-dist --workpath <scratch>/desktop-work --noconfirm --clean
```

## 仍需验收

- Windows / macOS Intel 的冻结组件安装、外部 worker 离线计算与结果对照；Windows 发布冒烟 workflow 可验证打包/安装升级/MCP/卸载，但不包含完整对齐组件安装计算链。
- 首次从冻结包联网下载模型，以及正式用户数据路径、发布级 ZIP/DMG/签名验收。本轮复用了既有模型文件，并用 portable.flag 隔离验证。
- 本轮不创建 tag 或 Release，不把本机或源码 CI 成功外推为三平台交付完成。

## 2026-09-17 补充：x86_64 冻结产物的 Rosetta 验证

使用既有 `.venv-macos12-x86_64`，以 `arch -x86_64 env MEFINDER_TARGET_ARCH=x86_64` 运行上述 PyInstaller 命令；桌面和 sidecar 的 Mach-O 均确认为纯 x86_64。该验证运行在 Apple Silicon 的 Rosetta 上，**不是 Intel 真机验收**。

- 冻结桌面正常启动，选择 `darwin-x86_64` 清单；经生产 HTTP 真实安装独立 Python 3.12 运行时，NumPy 2.5.2 / fastembed 0.8.0 / ONNX Runtime 1.23.2，实际独立解释器报告 x86_64。
- 复制同一 MiniLM 模型文件，`HF_HUB_OFFLINE=1`；用同版本 x86_64 依赖的原进程内路径作比较，4 条链接和20个成员全部相等，段落区间与运行参数的比较口径同 ARM。卸载后模型/运行时删除、20个成员保留，搜索与已有目标读取通过。
- sidecar 50,703,312 B，归档/PYZ 无三栈模块；真实 STDIO 四工具均通过。
- 临时桌面进程已终止；用户正式库和正式组件未修改。ARM 与 x86_64 分别同架构比较，不以跨 ONNX 版本数值比较替代各自等价验证。

修复提交 `8d571fe` 已推送；代码 CI：[35177699594](https://github.com/sabercomo/MEFinder/actions/runs/35177699594)。另已启动现有 [Windows 发布冒烟 35177710345](https://github.com/sabercomo/MEFinder/actions/runs/35177710345)，写本段时尚在构建步骤。用户明确要求：该发布冒烟若失败只记录位置，不继续排修，由用户后续在 Windows 本机运行。
