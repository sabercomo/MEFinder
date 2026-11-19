# #24：0.6.2 连接故障核查

2026-11-19：0.6.2 的 Windows 正式包已包含 SSL 修复，但截图中的完整 MinerU 接口地址仍会导致错误请求。本轮补修已打成 0.6.3 macOS arm64 本地测试包，包内连接验证通过，尚未公开发布。

## 核查对象与事实

- 仓库 `sabercomo/MEFinder`；本轮开始时本机与远端 main 均为 `db0f149`，公开 `v0.6.2` 标签为 `78ff068`。该标签到 main 只有发布文档变化。
- 公开 Release 于 2026-11-18 发布；实际有 macOS arm64、Windows 安装版和 Windows 便携版及各自校验文件，共八个附件。Intel 包不在公开附件中。
- 两个未关闭 issue 中，#24 是本次连接故障；#18 是单独的走廊对齐实验，本轮未修改其冻结的正式管线。
- #24 原截图左下角显示 0.6.1，MinerU 服务地址为 `https://mineru.net/api/v4/extract/task`。另图中的在线及本地兼容接口只显示连接失败，没有详细响应。维护者已在 11 月 18 日请报告者测试 0.6.2；核查时未见报告者回复。

来源：[#24](https://github.com/sabercomo/MEFinder/issues/24)、[v0.6.2 Release](https://github.com/sabercomo/MEFinder/releases/tag/v0.6.2)、[MinerU 官方接口文档](https://mineru.net/doc/docs/index.html)。

## 已发布实物

| 制品 | SHA-256 | 核对方式 |
| --- | --- | --- |
| Windows 便携 ZIP | `4d5f1e46ea4302e97786a473c04455fbb2df4b2bafa0d198adf48c7d866cd247` | 本轮从 Release 下载后计算，与远端资产 digest 一致 |
| macOS arm64 ZIP | `f31899024dafe7e297c7c930a2d834278efc8eeaafbae5eafdb5a3b3dbc2aa5f` | 本机 release worktree 实物计算，与远端资产 digest 一致 |

Windows 主程序名为 `文献原句定位器.exe`。使用 PyInstaller CArchive/PYZ reader 读取其冻结模块：

- `ssl` 确实依赖 `codex-rejected-cas.pem`，ZIP 的 `_internal/` 下已携带该文件（906 字节），另有 `certifi/cacert.pem`。
- 在本机同为 Python 3.12 的环境执行提取的 SSL 代码，使用包内证书数据能创建默认 SSLContext，证书校验与主机名校验均开启。这是模块级验证，不是 Windows GUI 实机验收。
- `MinerUClient._json_request` 已包含 `OSError` / `HTTPException` 的转换，修复不是仅存在于源码中。
- macOS arm64 冻结 SSL 模块不依赖该拒绝列表；包内 MinerU 与其他解析 API 模块和当前修复前源码一致。

## 地址问题复现

MinerU 官方文档给出的是完整任务接口；MEFinder 的批量文件流程会自行追加 `/api/v4/file-urls/batch`。0.6.2 原逻辑直接将两者相加。

使用从已发布 Windows 主程序提取的 MinerU 模块，以及本轮源码，分别向同一个临时本地 HTTP 服务发起连接测试。使用虚构 Token；服务仅接受正确的批量上传地址，返回模拟的上传 URL，不接收 PDF、不访问真实账号。

| 对象 | 服务实际收到的路径 | 结果 |
| --- | --- | --- |
| 0.6.2 Windows 冻结模块 | `/api/v4/extract/task/api/v4/file-urls/batch` | HTTP 404，连接失败 |
| 本轮修复源码 | `/api/v4/file-urls/batch` | HTTP 200，`ok: true` |

这是旧版本在截图配置下仍会失败的独立证据。**推断**：它可能是报告者升级后仍无法连接的一项原因；报告者尚未回报升级结果，不能断言其现场故障只有这一项。

## 修复与回归

仅修改 `parsers/mineru_api.py`：在读取配置、保存配置及直接连接测试时，将服务根地址、`/api/v4`、`/api/v4/extract/task`、`/api/v4/file-urls/batch` 统一为服务根地址。保留自定义代理前缀；已有配置读取不写回文件，Token 和其他字段保留。

新增回归：

- `test_service_address_accepts_documented_mineru_endpoints`：根地址与已知接口地址、官方站点与代理前缀均规范化保存。
- `test_existing_task_endpoint_is_normalized_without_rewriting_config`：旧配置可读，文件逐字不变。
- `test_connection_uses_service_root_for_pasted_task_endpoint`：经真实本机 HTTP 设置路由测试已有配置及新保存配置，捕获上游请求地址，确认不重复拼接。

修复前，上述三项测试产生九个失败的子场景；修复后通过。MinerU 配置、账号、Web 路由、桌面打包、其他解析 API、本地设置和导入桥接专项共 102 项通过，Ruff 通过。

全量门禁使用项目 `.venv-macos312-arm64/bin/python -m unittest discover -t . -s tests`，2,881 项通过（40 项按环境跳过，281.305 秒）。随后复核配置与 Web 两个修改的测试模块，共 27 项通过；仓库 Ruff（排除任务开始前已有的 `.codex-tmp`）及 `git diff --check` 通过。前端、偏好、schema 未变，无需更新指纹或快照。

## 验证范围

本轮未访问用户凭据、未上传文献、未修改真实文献库。核查及源码修复阶段没有打包，随后按用户要求构建 0.6.3 macOS arm64 本地测试包，见下节。Windows GUI 和报告者具体 API/模型的实际请求未复测；需 Windows 同源构建后再确认现场结果。

## 同日追加：0.6.3 包内验证

- 官方构建门禁 2,881 项通过（40 项环境跳过，216.770 秒），Ruff、版本契约与前端守卫通过。
- 325 个产品文件逐项匹配构建前 SHA-256；冻结版本为 0.6.3，冻结 MinerU 模块和本轮源码编译结果一致。严格签名、MCP、ZIP/DMG 校验通过。
- 直接执行冻结 MinerU 模块，完整地址直测和旧配置连接均成功。再启动解包后的原生应用，用临时数据目录，经其自身的服务设置、账号保存、账号测试路由实际请求本地模拟上游。三次请求路径均为 `/proxy/api/v4/file-urls/batch`，没有重复拼接，代理前缀保留；旧配置读取不改文件。测试应用进程已退出。
- DMG：104,288,601 字节，SHA-256 `632035d3ef3f1eab7be28597440e4a43d23878f566001ceb632e00ca7febb40e`。
- ZIP：96,391,114 字节，SHA-256 `874025a59d3a0cb9b0a9aa1437e2f033b2124ea6923cde8cae4f50531346042b`。
- 制品与各自校验文件位于 `release/`；本机日志位于 `/tmp/mefinder-0.6.3-build/`。详见 [0.6.3 本地测试包说明](../docs/release-notes-0.6.3.md)。本轮未安装到 `/Applications`、未上传附件、未公开发布。
