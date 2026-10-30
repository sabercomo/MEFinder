# 组件安装器共用机制核验（0.6.1，2026-10-31）

## 范围与基线

本轮从 `6430297` 开始，落实 2026-10-31 架构评估的第 3 项。只合并三个组件安装器里重复实现的机制，不改变安装清单、状态文件、安装步骤、重试策略、HTTP/MCP 接口或前端。

| 文件 | 调整前 | 调整后 |
|---|---:|---:|
| `managed_mineru.py` | 1,586 行 | 1,517 行 |
| `managed_alignment_runtime.py` | 1,553 行 | 1,517 行 |
| `local_ocr_installer.py` | 1,377 行 | 1,303 行 |
| `component_install.py`（新增） | 无 | 279 行 |

三个安装器合计减少 179 行，新增的共用模块含文档与类型协议，总行数基本持平；收益在于每种机制只剩一份实现，而不是总行数下降。

## 调整前的重复（事实）

按同名方法逐对比较文本相似度：`_ensure_uv`（MinerU 与对齐运行时 0.96）、`_update_uv_download_progress`（OCR 与 MinerU 0.96）、`_begin_download_progress` / `_set_download_progress`（0.85—0.86）、`_stop_process`（0.90—0.98）基本是复制粘贴；uv 下载日志的正则与单位表在 OCR 和 MinerU 中各有一份。

**更正**：评估时称“下载速度/剩余时间只有对齐组件有”，不成立。MinerU 与本地 OCR 早已显示速度与剩余时间，只是各自内联了一份计算；对齐组件用的是 `transfer_rate.py`。三种实现的窗口与取整规则不同（MinerU 30 秒窗口、OCR 8 秒或不限窗口、`TransferRate` 20 秒窗口且至少 2 秒跨度），本轮保持各自现有数值。

## 共用模块 `component_install.py`

- `uv_download_totals` / `read_log_tail`：解析 uv 安装日志里已宣布和已完成的下载字节。
- `begin_download_progress` / `record_download_progress`：MinerU 与 OCR 共用的测速与剩余时间更新；窗口由调用方传入，时钟读数也由调用方在本模块外读取，原有 `time.monotonic` 替换点不变。
- `copy_response_to_file`：分块写盘，每块前检查取消并回报累计字节；三个下载器共用。
- `verify_download`：流式计算 SHA-256 并核对大小，错误类型与文案由调用方给出。
- `extract_archive_member` / `install_uv_executable`：在临时目录下载、解包、发布固定版本的 uv 可执行文件，失败时清理临时目录且不动已有工具目录；OCR 的 receipt 由参数写入。
- `stop_process`：终止子进程，5 秒后升级为强制结束。

各安装器保留：清单与状态模型、重试与镜像策略、`_run_command`（含停滞检测与 uv 进度跟踪）、操作线程生命周期。这几处差异由现有测试钉住，合并会改变网络失败或停滞时的行为，本轮不动。MinerU 的 uv 下载仍是单次尝试、不续传（MinerU 的大体积依赖经 uv/pip 子进程下载，有其自身重试）。

## 行为差异（仅边缘情况）

1. uv 归档解包统一为最严格的检查与文案：缺少指定成员、成员不是普通文件、成员不可读、格式不受支持都报安装器自己的错误类型。这些分支只在已通过清单 SHA-256 校验的归档上才可能触发，实际不可达；内置清单只使用 `zip` 与 `tar.gz`。
2. `stop_process`：对齐运行时强制结束后的等待由不限时改为 5 秒；MinerU 与 OCR 在进程恰好已退出、`terminate` 抛 `OSError` 时不再报错。
3. MinerU 与对齐运行时的 SHA-256 改为流式计算、大小写不敏感比较；清单哈希为小写十六进制，结果相同，只是不再一次读入整个文件。

## 验证

1. 迁移每个安装器后立即运行其专项：OCR 安装器、组件目录与运行位置 28 项通过；MinerU 与 MinerU 本地设置 41 项通过；对齐运行时、复审与精简打包 62 项通过。已有测试直接调用的 `_begin_download_progress`、`_set_download_progress`、`_update_uv_download_progress` 与 `urlopen` 替换点保持原样，断言未修改。
2. 新增 `tests/test_component_install.py` 15 项：uv 日志统计、测速窗口与停滞、短跨度保持、未知总量、分块写盘与取消检查、大小与哈希校验、zip/tar 解包与三类坏归档、uv 发布与失败清理、进程终止升级与已退出进程。新增守卫 `test_component_installers_share_install_mechanics`，禁止三个安装器重新定义上述机制。
3. macOS 全量 `PYTHONUTF8=1 .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests`：2,855 项通过（39 项条件跳过，186.767 秒）。
4. `ruff check src tests scripts` 与 `git diff --check` 通过。

本轮未在 Windows 实机或真实网络下完整安装三个组件；下载、解包与子进程行为由带桩的单元与集成测试覆盖。
