# MCP v1 并发与发布验证记录

更新日期：2026-08-14

## 结论

MCP v1 的短连接边界已通过自动化并发验证：MCP 进程可以长期存在，但每次工具调用结束后不保留 `SearchEngine` 或 SQLite 句柄；索引替换后的下一次调用读取新快照；数据目录迁移只在完整副本校验成功后切换下一次调用的数据根。

当前主机完成了 macOS 15.7.3 arm64 与 Rosetta x86_64 的完整 ad-hoc 发布验证。Windows 构建链、生命周期测试和手动触发的 Windows Server 2022 托管工作流已实现并推送到 0.4.4 开发分支，但尚未进入默认分支或执行；当前主机也没有 Windows、PowerShell、Wine、Docker 或 Windows 交叉工具链，不能生成或运行 Windows PyInstaller 产物。Windows 10/11 x64 实机复验仍必须在消费者系统执行。Developer ID/hardened runtime 与 notarization 也必须在持有发布证书的 macOS 构建机复验。

## 并发矩阵

| 场景 | 自动化断言 | 当前结果 |
| --- | --- | --- |
| MCP Server 保持运行时替换索引 | 一次调用读取旧快照；替换后下一次调用只读取新快照 | 通过 |
| Windows 风格短暂文件占用 | 替换先收到 `PermissionError`，MCP 调用结束后在既有退避预算内成功 | 通过（跨平台模拟）；Windows 实机门禁已加入发布脚本 |
| 数据目录迁移 | 指针切换前调用读取旧根；完整迁移并原子写入指针后调用读取新根 | 通过 |
| 进程与句柄清理 | 协议会话退出，无残留 MCP 进程；同一运行中 Server 可原子替换索引 | 通过 |

对应测试：`tests/test_mcp_concurrency.py`。

测试没有证明需要新增共享锁，因此 0.4.4 不引入跨进程锁或桌面应用桥接。若 Windows 实机门禁出现当前退避预算无法覆盖的真实锁竞争，再以失败记录为输入设计协调机制。

## macOS 实物发布结果

构建命令：

```bash
MEFINDER_PYTHON=.venv-macos312-arm64/bin/python ./build_macos.sh
arch -x86_64 /bin/bash -c \
  'MEFINDER_PYTHON=.venv-macos12-x86_64/bin/python MEFINDER_TARGET_ARCH=x86_64 ./build_macos.sh'
```

结果：

- 两个架构各自执行发布前 1074 项测试，均全部通过；
- arm64 `MEFinderMCP`：43,076,896 bytes，Mach-O arm64 onefile；
- x86_64 `MEFinderMCP`：45,022,400 bytes，Mach-O x86_64 onefile；
- arm64 ZIP：`598363ca99d3f29303cfbe13c9a3c36d85cd752739e4fbefede5d733282ad46a`；
- arm64 DMG：`23aeda7002e8c394183a3819cf10250cd8cf9b03b8e4593ed2538ace0079fb3b`；
- x86_64 ZIP：`55cec9c290eebde9144d179547a788f2aa0065ffc39124b1280df816a7713cc8`；
- x86_64 DMG：`22882a6a315cdb5bc05e663d8edc1a5c2baa143125b4f76cc65a969bae45aa4f`；
- ZIP/DMG SHA-256 自校验通过，DMG 挂载、`/Applications` 快捷方式和复制后签名验证通过；
- sidecar 在应用、ZIP 解包、DMG 挂载和复制位置均完成 `initialize`、`tools/list` 与 `list_documents`；
- 包内包含项目许可证、第三方通知、MCP 依赖归属和所选 Python 运行时许可证；
- 构建结束后无残留 `MEFinderMCP`/源码 MCP 进程。

Intel 依赖固定 `cryptography 46.0.3`，因为 50.0.0 不再提供适用于该目标的预编译轮子；46.0.3 仍满足 MCP 依赖并提供 universal2 轮子。两次构建使用 ad-hoc 签名，只证明本机构建与包结构正确，不替代 Developer ID 与公证验证。

## 回归结果

- 发布脚本选择的 1074 项测试在 arm64 与 x86_64 各通过一次；
- 排除依赖个人语料或当前环境缺少可选解析依赖的 7 个既有模块后，可复现产品套件 1366 项通过、1 项跳过；
- 未排除的全量发现运行了 1434 项，7 项环境错误全部来自缺少 `corpus/raw_docx`、3 个私有 PDF 夹具或可选 `python-docx`，没有 MCP 或本轮产品代码失败；
- MCP 契约、协议、质量、文档、打包和并发测试全部纳入发布脚本。

## Windows 托管发布门禁

`.github/workflows/windows-release-smoke.yml` 固定使用 `windows-2022` x64，并覆盖：

- 构建 0.4.4 安装版与绿色版，执行各自发布脚本内的 Windows/MCP 测试、隐私扫描和真实 sidecar 冒烟；
- 从 v0.4.3 草稿 Release 下载既有 Windows 安装包，验证覆盖升级后稳定安装路径出现 `MEFinderMCP.exe` 且原数据保留；
- 在桌面关闭、开启两种状态下用不带测试路径覆盖的 sidecar 完成真实 STDIO 会话；
- 静默卸载后 sidecar 被删除、用户数据保留；
- 解压绿色版、建立会话、移动目录后再次建立会话，并检查无残留 MCP 进程；
- 上传安装包、绿色版及其 SHA-256 文件作为未签名 CI 产物。

该工作流已推送到 `codex/v0.4.4-mcp` 开发分支，但尚未进入默认分支或执行，因此没有可引用的远端运行记录。即使托管运行通过，也只补足 Windows Server 2022 自动门禁，不替代计划要求的 Windows 10/11 x64 消费者实机与代码签名验收。

## 发布与回滚

- MCP 是可选只读集成；不配置 Codex 时，桌面、HTTP、导入、备份和页码校准行为不变；
- 临时禁用：在 Codex MCP 列表关闭 `mefinder`，或设置 `enabled = false`；
- 完全移除：执行 `codex mcp remove mefinder` 后重启当前 Codex 客户端；该操作不删除 MEFinder 数据；
- 回滚应用：安装/复制上一个版本并保持原数据目录；若旧版本不含 sidecar，同时移除 Codex 中的 `mefinder` 配置；
- 绿色版移动后必须用新绝对路径重新添加；卸载或删除应用不会静默编辑 Codex 配置，旧配置会明确报告命令不存在。
