# 2026-10-20 macOS 双架构构建与草稿上传核验

## 范围与源码

用户要求拉取远端、更新 macOS 包并上传 Release，明确要求 arm64 与 x86_64 均上传，保持草稿。版本沿用 0.5.9。合并本地 `4558842` 与远端 `e51af5b`，保留本地三项架构整理与远端导入断连、首页时序、Windows OCR 修复。

导入状态冲突按现有任务所有权解决：把通信边界的断连恢复移入 `78-import-jobs.js`，保留旧响应隔离和单轮询链；测试执行完整模块。未改检索算法、页码锚点、数据库或版本号。

## 合并验证

- 先移植断连用例：18 项中 4 个断连场景失败；迁入恢复逻辑后，导入状态、静态装配及译本对照专项共 74 项通过。
- 完整 Chrome 回归 13 项通过，43.353 秒，包含默认首页顺序、断连恢复和任务跨页面/重载恢复。
- 跟踪 Python 文件 Ruff(F) 零告警；装配指纹为 1,525,202 字节 / `b20eeca80339d70230c8328de3b06a63a5c5413c0d35172f6326e6ceaa1f6a36`。

- Apple Silicon 官方构建内置全量 unittest 2,790 项通过（41 跳过，173.076 秒）。

## 构建与上传

- 包内源码提交为 `9fe9b0305a87d519f92954e856fadeb3ebf72dce`，本地与远端 main 已核对该提交。
- Intel 官方 Rosetta 构建全量 2,790 项通过（46 跳过，200.694 秒）。ARM 原生与 Intel Rosetta 均通过官方脚本的 JS、五阶段 MCP、严格签名、PDFKit / CA / 隐私检查、ZIP / DMG / SHA 校验。
- 解包独立核对：两包各 307 份文件与构建前清单相同；两个可执行文件均为纯目标架构。ARM 最低 macOS 14，Intel 最低 macOS 12。

| 制品 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.5.9-macos-arm64.dmg` | 104,634,836 | `6273e47f96917177acc013aec6417c67958f7e5482f45d6b6ec74d6db2111d02` |
| `MEFinder-v0.5.9-macos-arm64.zip` | 96,224,144 | `31ca0c89e4c11055e7c62d32e3b2534813b94d816b2d95d45df2a55d9552f478` |
| `MEFinder-v0.5.9-macos-x86_64.dmg` | 108,623,817 | `ec35a731f97f84e1c6e0023f41e21e2458d6c8583e3b243f41ac093ebc836a53` |
| `MEFinder-v0.5.9-macos-x86_64.zip` | 102,759,767 | `4db29659539c3e1d219c12da9824024212bc4b94bdeb644d4ee375623d8c5e17` |

- 八个 macOS 上传文件大小、远端 digest 与本地实算逐项一致；草稿 id `418255184`，draft=true，目标 `9fe9b03`，当前 [URL](https://github.com/sabercomo/MEFinder/releases/tag/untagged-c88f9533375e8073107e)。更改草稿目标提交后 untagged URL 改变，应使用最新查询结果。
- Windows 资产在上传期间由并行流程更新：原 `75fbeba` 制品已被 `e51af5b` 制品替换；已根据最新 Release 内容合入 macOS 记录，避免覆写 Windows 更新。本轮未改 Windows 资产。
- 未正式发布、未新建或移动 tag，未覆盖 `/Applications/MEFinder.app`。包 ad-hoc 签名、未公证；Intel 为 Rosetta 验证。
- 构建日志、构建前清单、两个 verification JSON、上传前后 Release JSON 留在本地 `.codex-tmp/macos-v059-20261020/`；此前 ARM 同名包备份于其 `previous-artifacts/`。

## Windows CI 测试驱动修复

合并提交的 CI `39420846583` 中 Linux、Chrome、lint 通过；Windows 跑完 2,790 项（53 跳过）后，唯一错误为 `test_import_and_zotero_refresh_version_metadata_on_visible_and_hidden_pages` 启动 Node 时的 WinError 206。该用例把完整任务模块和场景塞进 `node -e`，超过 Windows CreateProcess 命令行长度限制。将这一个场景改为 `node -` + 标准输入，继续执行相同断言；不改产品代码或安装包内容。


复核场景得到旧命令行 35,316 个 UTF-16 单元，超过 32,767 上限；标准输入执行后的命令行仅 32 个单元，相同 JS 场景与断言全部执行。修正后专项 26 项通过；最终全量 2,790 项通过（41 跳过，178.840 秒），Ruff(F) 零告警。该修正仅改测试驱动，不改变已上传包中的 307 份产品文件；最终提交会触发 CI 复验，远端终态需另行核对。
