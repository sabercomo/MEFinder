# #24：0.6.2 连接故障核查

2026-11-19：0.6.2 的 Windows 正式包已包含 SSL 修复，但截图中的完整 MinerU 接口地址仍会导致错误请求。本轮源码已修复地址处理，尚未重打包。

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

本轮未访问用户凭据、未上传文献、未修改真实文献库、未重新打包或发布。Windows GUI 和报告者具体 API/模型的实际请求未复测；其现场成功仍需安装包含本次修复的新包后确认。
