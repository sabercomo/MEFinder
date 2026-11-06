# 0.6.1 macOS 更新与发布准备

2026-11-06：macOS 双架构包已按最新主线构建并核验，本机应用已更新；Mac 八个 GitHub 草稿附件已核齐，Windows 按用户要求改由 Win 电脑打包，云端流程已停止，尚未正式发布。

## 源码与范围

- 构建源码：`55f6def7e02746b86e328a4e6d1db6eabaf60962`，从 `d761d8c` 快进拉取。新增 MinerU 分片错误提示、永久失败取消续传承诺，以及终态历史任务不再阻止删除账号。
- 两种架构使用同一份源码，官方 `build_macos.sh` 顺序构建。未改产品源码、静态资源、接口或数据库结构。
- Apple Silicon 包最低 macOS 14.0，Intel 包最低 macOS 12.0；Intel 经 Rosetta 构建，未在 Intel 真机验收。两种包均为 ad-hoc 签名，未公证。

## Mac 门禁

- arm64：全量 unittest 2,874 项通过（39 项条件跳过，198.948 秒）。
- x86_64：全量 unittest 2,874 项通过（44 项条件跳过，216.833 秒）。
- 本机真实 Chrome 回归 16 项通过（55.505 秒）；Ruff(F) 排除用户既有未入库 `.codex-tmp/` 后通过；前端守卫、装配指纹与 JavaScript 语法检查通过。
- 两架构官方流程均完成桌面与 MCP 打包、五阶段 MCP STDIO 冒烟、严格签名、隐私与元数据反查、ZIP 解包、DMG 挂载和拖出复制、SHA-256 自校。
- 独立核验：每架构 323 个交付源码、模板、静态资源和清单与构建清单逐文件相同；主程序 PYZ 中本次四个 MinerU 修复模块与源码编译结果完整相同，两个反序页码核心函数亦一致；主程序与侧车均为对应的单一架构。
- 源码 CI [42197049248](https://github.com/sabercomo/MEFinder/actions/runs/42197049248) 第二次运行全绿。首次仅阅读器切换文献的浏览器回归失败，本机 16 项与远端失败 job 复跑均通过；未据此修改产品或放宽测试。
- 工作流修复提交 `167c73a` 的 [CI 42221186926](https://github.com/sabercomo/MEFinder/actions/runs/42221186926) 四项（Linux、Windows、Chrome 与 lint）均已通过。

## 本机安装

- 已将新 arm64 应用安装到 `/Applications/MEFinder.app`。退出旧 MCP 侧车后替换应用，560 个包内文件与新 ZIP 中应用逐一相同，严格签名通过。
- 安装前实际核对发现，原已装应用只差此次四个 MinerU 模块，已包含此前页码与架构更新；此前存档中的“仍为旧代码、未安装”已经过时。
- 隔离数据根下启动新应用，桌面进程存活，首页包含 0.6.1，文献库摘要 API 返回正常；安装路径中的 MCP STDIO 与对齐 worker `--probe` 握手通过。未执行需要真实云端账号的解析或组件下载安装。
- 仅替换 `.app`，没有对真实文献库执行写入。旧应用备份在本机 `.codex-tmp/release-0.6.1-20261106/installed-before.app`。

## 制品

| 本机文件（`release/`） | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MEFinder-v0.6.1-macos-arm64.dmg` | 103,456,572 | `c2a31f55ae59dd0896f4a18a6d9883426817caa8b10c3e440eccd0976538c6d9` |
| `MEFinder-v0.6.1-macos-arm64.zip` | 96,310,860 | `271af67cd9c60f749fec468fe8829df2986f2616a855a95db24c4422eb413dff` |
| `MEFinder-v0.6.1-macos-x86_64.dmg` | 108,843,061 | `9ff2033d02cf6996d034d667e50fb8e1458dd2a3e48f440062c7a152f6b01087` |
| `MEFinder-v0.6.1-macos-x86_64.zip` | 102,845,652 | `902a7777bcc7f8557705d5a1bcbec11a5ce017121796f30f347e8f2f39d9b4a4` |

四个制品均附同名 `.sha256.txt`。本轮同名旧 ARM 测试包已备份至本机任务目录，再由官方构建替换。

## 发布准备状态

- GitHub 已创建 `v0.6.1` 草稿（release id `436482072`），产品源码为 `55f6def`，发布目标已更新为仅改工作流与测试的 `167c73a`；未创建或移动正式 tag，未发布。
- 首次 Windows 发布冒烟：[42218215279](https://github.com/sabercomo/MEFinder/actions/runs/42218215279)，原计划构建安装版、便携版，再验证安装、从 0.4.9 升级、MCP 生命周期、卸载与便携版移动。
- 首次 Windows 冒烟在构建门禁失败：2,869 项、53 项跳过，8 项失败、36 项错误，992.725 秒；日志显示中文夹具在 cp1252 编码下发生 `UnicodeEncodeError` / `UnicodeDecodeError`，随后依赖这些夹具的安装断言亦失败。安装版未构建，安装、升级与卸载步骤未执行。
- 发布工作流此前缺少普通 Windows CI 已有的 `PYTHONUTF8=1`。本轮先新增 `test_windows_release_job_enables_utf8_for_chinese_fixtures` 复现失败，再在发布 job 设置 UTF-8；MCP 打包守卫 7 项通过。全量 unittest 2,875 项通过（39 项跳过，206.645 秒），Ruff 通过；修复提交 `167c73a` 已推送，重新运行 [42221216166](https://github.com/sabercomo/MEFinder/actions/runs/42221216166)，后按用户要求取消，不再继续云端打包；Windows 包由用户的 Win 电脑构建后补入。该提交只改工作流和测试，产品源码仍为 `55f6def`，Mac 包无需重打。
- 本机 GitHub 连接曾返回 TLS EOF / 代理 503；用户切换网络后，按草稿 id 逐文件上传，Mac 八个附件均为 `uploaded`，字节数与 GitHub digest 和本地 SHA-256 全部一致。

构建、源码清单、冻结代码、安装和启动证据保存在本机 `.codex-tmp/release-0.6.1-20261106/`；该目录不提交。本轮交付范围按用户最后指令收敛为保留草稿：Mac 八个附件完整，Windows 本机打包、Windows 发布验收与正式发布留待后续指令，不宣称 Windows 已完成。
