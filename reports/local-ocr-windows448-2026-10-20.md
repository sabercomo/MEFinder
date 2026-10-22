# 本地 OCR Windows 448 安装修复核验

日期：2026-10-20。

## 成功标准

1. Windows 上 `Failed to create Python minor version link directory` 与 `(os error 448)` 同时出现时，复用已下载的托管 Python，禁止再次下载，最多恢复一次。
2. 恢复成功后完成后续安装与验证；恢复失败继续报错并回滚；不掩盖其他错误、不扩展到非 Windows 平台。
3. 真实 uv 0.12.1 能在版本目录链接不可用时，离线从物理 Python 目录创建可运行的独立环境。

## 复现测试

使用项目 Python 3.12.14 与 unittest。测试 uv 子进程先模拟已下载的 Python 和链接错误；恢复命令必须携带 `--no-python-downloads`，并继续使用同一 `UV_PYTHON_INSTALL_DIR` 和 `--managed-python`。

- `test_windows_minor_version_link_error_reuses_downloaded_python`：修复前失败，修复后完成安装；安装日志保留原错误，配置与安装凭据有效。
- `test_windows_minor_version_link_recovery_failure_is_not_hidden`：修复前没有执行恢复而失败；修复后执行两次并保留第二次失败原因，配置和临时目录完整回滚。
- `test_unrelated_windows_mount_error_is_not_retried`：其他位置的 448 不重试。
- `test_other_minor_version_link_errors_are_not_retried`：同一链接错误但错误码为 5 时不重试。
- `test_non_windows_minor_version_link_error_is_not_retried`：非 Windows 平台不进入恢复。

`tests.test_local_ocr_installer`、`tests.test_local_ocr_settings` 与 `tests.test_component_catalog` 共 32 项通过（14.937 秒）。修改文件与全部 Git 跟踪 Python 文件 Ruff(F) 通过。

## 全量门禁

设置 `PYTHONUTF8=1`，使用 `.venv-windows/Scripts/python.exe -m unittest discover -t . -s tests`：2,778 项通过，30 项环境跳过，耗时 219.075 秒。前端指纹、偏好及相关守卫随全量测试通过；本轮无前端资源、偏好、schema 或接口变更，无需更新对应基线、README 或契约。未额外运行可选真实浏览器回归。

全部 Git 跟踪 Python 文件 Ruff(F) 零告警，变更文件 `git diff --check` 通过。全量原始日志在 `.codex-tmp/ocr-win448-20261020/unittest-full.log`。日志中的故障注入异常以最终 unittest 汇总判定，不作为实际测试失败。

## 真实 uv 离线验证

在 `.codex-tmp/ocr-win448-20261020/中文 テスト offline-runtime/` 创建隔离目录，只复制已安装的 CPython 3.11.15 物理目录；同级 `cpython-3.11-windows-x86_64-none` 写为普通文件，确保不能作为目录链接使用。原运行时仅只读，不作修改。

使用实际 uv `0.12.1 (329541a50 2026-07-31 x86_64-pc-windows-msvc)`：

```text
uv venv --python 3.11 --managed-python --relocatable --no-python-downloads --offline <隔离目录>/venv
```

退出 0；新环境解释器运行成功，版本为 3.11.15，`sys.base_prefix` 指向隔离目录中的 `cpython-3.11.15-windows-x86_64-none`，普通文件占位仍存在。说明恢复路径无需可用的版本目录链接或网络下载。

脚本与原始日志：`.codex-tmp/ocr-win448-20261020/smoke_uv.py`、`smoke-uv.log`。

## 验证范围

尚未在反馈用户的机器上验证原生系统错误 448；本轮不声称重现了其 OneDrive 或 Windows 安全策略。现有安装包、便携包和 GitHub 草稿资产尚不包含本轮修复。

## 2026-10-20 — 重新打包与草稿替换

用户随后授权重打并替换草稿，已按 `9fe9b03` 完成 Windows 双包、两个校验文件及远端核验；上节未重打包状态已更新。见[交付核验](windows-ocr-release-2026-10-20.md)。反馈用户实机仍待验收。
