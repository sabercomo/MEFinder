# Windows 本地 OCR 安装在 Python 版本目录链接处失败

## 2026-10-20 — 用户报告与修复

### 事实

- 用户截图显示，NDLOCR-Lite 1.2.3 安装日志已输出 `Downloaded cpython-3.11.15-windows-x86_64-none`，随后报 `Failed to create Python minor version link directory`、`os error 448`，安装退出 2。截图未提供用户 Windows 版本和实际组件路径。
- 组件清单固定使用 uv 0.12.1；安装器通过 `uv venv --python 3.11 --managed-python --relocatable` 创建独立环境。
- uv 0.12.1 的 [Python 安装流程](https://github.com/astral-sh/uv/blob/0.12.1/crates/uv-python/src/installation.rs)先下载、解压和标记 Python，再调用 `ensure_minor_version_link`。该链接用于透明升级 Python 补丁版本；已有解释器的发现流程不重新执行这一步。
- 在复现测试中，修复前安装直接回滚；修复后只对 Windows 上同时包含该链接错误和 `(os error 448)` 的失败，使用原组件 Python 目录与 `--no-python-downloads` 再创建一次环境。成功后仍执行依赖安装、SBOM 生成、CLI 与样图验证，再发布配置。原始错误保留在安装日志中。验证过程与结果见[核验报告](../../reports/local-ocr-windows448-2026-10-20.md)。

### 推断与限制

- 此次失败发生在本地安装阶段，不能按下载不通处理。uv 上游 [#19616](https://github.com/astral-sh/uv/issues/19616) 有相同错误的用户报告，其中包含 OneDrive 场景；尚无证据确认本用户由 OneDrive 触发。
- 本机验证使用受控的 448 错误注入和真实 uv 的离线目录链接缺失场景，没有在反馈用户的 Windows 环境中重现系统原生 448。

### 行为边界

- 最多恢复一次；恢复失败继续报错并沿用原回滚流程。
- 其他 448 错误、版本链接的其他错误码、非 Windows 平台不重试；取消和超时继续沿用现有处理。
- 不切换系统 Python、不移动用户数据、不修改系统权限或 uv 版本；本轮不改变其他托管组件的安装流程。
