# MEFinder MCP v1 Codex 端到端验收记录

验收日期：2026-08-14

适用版本：MEFinder 0.4.4 源码模式

## 验收结论

源码模式已经被真实 Codex 本地客户端发现并调用。Codex 正确处理已校准页、只有物理页、重复候选、少量错字和无结果；MEFinder Web 服务关闭和开启两种状态均能读取同一测试索引。

本验收使用 `codex-cli 0.147.0-alpha.6.5`。CLI 输出没有报告明确模型标识，因此本记录不猜测具体模型名称。

## 隔离方式

- 使用公共合成质量夹具，不读取用户个人文献；
- 使用 `codex exec --ignore-user-config --ephemeral` 和命令行配置覆盖；
- 没有读取、创建或修改用户的 `~/.codex/config.toml`；
- Codex 沙箱为只读，验收提示明确禁止 shell、网络和文件修改；
- 验收结束后没有残留 MCP/Web 进程或 SQLite 文件句柄。

## MEFinder Web 关闭时

Codex 仅使用 `mefinder_e2e.locate_quote` 完成五项核对：

| 场景 | 工具证据 | Codex 最终表述 | 结果 |
| --- | --- | --- | --- |
| 已校准 PDF | 物理页标签 1；引用页 38；`calibrated` | 明确区分物理第 1 页和正式第 38 页 | 通过 |
| 未校准 PDF | 物理索引 1；引用页为空；`uncalibrated` | 说明是第 2 个物理页，不能当作引用页码 | 通过 |
| 重复原句 | PDF 和 Word 两个候选 | 分别列出两个来源，没有替用户选择 | 通过 |
| 少量错字 | `fuzzy`，分数 0.9，返回正确原文 | 明确说明不是逐字精确命中 | 通过 |
| 无结果 | `total=0`，空候选 | 明确无结果，不推断或编造 | 通过 |

五个场景产生五次 `locate_quote` 调用，没有使用其他工具。

## MEFinder Web 开启时

使用同一 SQLite 索引启动本地 `serve` 进程，使桌面业务使用的 Web 服务和 MCP 同时访问索引。Codex 随后完成：

- `locate_quote` 精确定位目标句；
- `read_document_window` 读取两个 PDF 页窗口；
- 根据结构化证据报告物理第 1 页、正式引用第 38 页；
- 明确相邻物理第 2 页的引用页尚未校准，不能生成可靠脚注。

本轮模型额外重复了一次相同参数的 `locate_quote`。它没有影响正确性或数据库状态，但作为实际调用开销记录保留；当前没有足够证据为单次冗余调用修改工具契约。

## 全新源码环境

另建空白临时虚拟环境，只安装 `mcp==2.0.0`，随后完成：

1. 导入 `mcp` 和 `src.me_finder.mcp_server`；
2. MCP 客户端 `initialize`；
3. `tools/list`；
4. 三个工具调用及输出 schema 校验。

这台测试机的 Python 最初没有正确使用系统 CA 证书，直接访问 PyPI 出现 TLS 证书链错误；指定受信任的系统 CA bundle 后，依赖安装和协议测试通过。验收没有关闭 TLS 校验。

## 本记录未覆盖的后续范围

- Windows 安装版 sidecar；
- Windows 绿色版 sidecar；
- macOS 应用包内 sidecar、签名和覆盖升级；
- 普通用户发布包的固定命令路径。

这些不属于本次源码验收本身。后续 sidecar 实现、macOS arm64 实物构建与仍待完成的跨平台实机矩阵见 [`mcp-v1-concurrency-release-report.md`](mcp-v1-concurrency-release-report.md)；本记录继续只证明源码模式及其 Codex 接入。
