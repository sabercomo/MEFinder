2026-11-17：**0.6.2 Windows 本地测试包已构建（未发布）。** 安装版与便携版均通过 2,876 项 unittest（52 跳过），Ruff 全仓通过；产物仅放在本机 `release/`，未上传 GitHub、未做安装与冻结 GUI 人工验收。

本版在 0.6.1 基础上修复 Windows「MinerU 在线 API → 测试连接」在远端提前断开时只显示 `Failed to fetch` 的问题：连接重置、对端断开与响应截断现在都转换为带具体原因的 MinerU 错误，测试账号接口保持原有 JSON 错误响应。回归用例为 `test_connection_test_returns_json_when_upstream_connection_breaks`，详见 [问题记录](issues/windows-mineru-connection-fetch.md)。

构建源码 `3c9d809`（版本号推进到 0.6.2），包含远端修复提交 `1609b9d`。安装版门禁：2,876 项通过（52 跳过，223.975 秒）；便携版门禁：2,876 项通过（52 跳过，222.681 秒）。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.2-windows-setup.exe` | 82,257,169 | `61cf162331044802075ae658d47db74b70a6f14fdbe4f34fb0fb4d92e3a90f6e` |
| `MEFinder-v0.6.2-windows-portable.zip` | 98,064,723 | `bbd7d82c45fd7a4d6de6cf7898e743f744222684b6c678e15ad1b55dade6ab14` |

各文件均附同名 `.sha256.txt`。两个 EXE 的版本信息与冻结应用源码均为 0.6.2；便携版 ZIP CRC 通过，`config/pdf_imports.json` 为空、空白索引与 `portable.flag` 校验通过。安装 / 升级 / 卸载与冻结 GUI 人工验收本轮未执行。
