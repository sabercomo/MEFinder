2026-11-17：**0.6.2 Windows 本地测试包已重打（未发布）。** 安装版与便携版均通过 2,876 项 unittest（52 跳过），Ruff 全仓通过；产物仅放在本机 `release/`，未上传 GitHub、未做安装与冻结 GUI 人工验收。

本版在 0.6.1 基础上修复 Windows「MinerU 在线 API → 测试连接」在远端提前断开时只显示 `Failed to fetch` 的问题：连接重置、对端断开与响应截断现在都转换为带具体原因的 MinerU 错误，测试账号接口保持原有 JSON 错误响应。回归用例为 `test_connection_test_returns_json_when_upstream_connection_breaks`，详见 [问题记录](issues/windows-mineru-connection-fetch.md)。

同时修复冻结版 HTTPS 的打包根因：本机构建解释器的 `ssl.py` 被 Codex 运行时打了补丁，创建 `SSLContext` 时强制读取同目录的 `codex-rejected-cas.pem`；PyInstaller 未收集该文件，冻结版所有 HTTPS 在 `urllib.request.build_opener` 阶段抛 `FileNotFoundError`，本地后端静默断开。三个 PyInstaller spec 现在仅在构建解释器确有此文件时把它打入包根目录，干净 Python 构建不受影响，并补测试钉住。重建后的 dist 冻结版用真实账号调用测试连接已返回 200（425 ms）。

构建源码 `4bcda9c`（版本号推进到 0.6.2，包含远端修复提交 `1609b9d` 与 SSL 打包修复）。安装版门禁：2,876 项通过（52 跳过，222.626 秒）；便携版门禁：2,876 项通过（52 跳过，225.224 秒）。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-windows-setup.exe` | 82,259,437 | `dcbcd3ec8595956391d9e3897c4d98fa372007c46c9f5fac8fa4b80bc6efe5ac` |
| `MEFinder-v0.6.2-windows-portable.zip` | 98,066,081 | `a1ccd34f2ee9f07a579662ce476bfea50044806c93db76fc83bf342a64cd825a` |

各文件均附同名 `.sha256.txt`。两个 EXE 的版本信息与冻结应用源码均为 0.6.2；便携版 ZIP CRC 通过，`config/pdf_imports.json` 为空、空白索引与 `portable.flag` 校验通过，安装版 payload 与便携包内均含 `codex-rejected-cas.pem`。安装 / 升级 / 卸载与冻结 GUI 人工验收本轮未执行。
