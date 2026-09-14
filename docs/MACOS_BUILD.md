# macOS 构建说明

> 构建入口 `build_macos.sh` 与依赖清单仍位于仓库根目录。

macOS 版本沿用现有的 Python 后端和 HTML/CSS/JavaScript 界面，用 pywebview 的
Cocoa/WebKit 窗口封装，并由 PyInstaller 生成原生 `.app`。

## 环境

- Apple Silicon 发布包最低支持 macOS 14；
- Intel 发布包最低支持 macOS 12；
- Python 3.10 或更高版本；
- Xcode Command Line Tools；
- 构建机架构决定默认产物架构：Apple Silicon 为 `arm64`，Intel 为 `x86_64`。

pywebview 官方建议使用独立安装的 Python，而不是 macOS 系统 Python，以避免窗口焦点
和 `Cmd+Tab` 行为异常。推荐为构建创建隔离环境：

```bash
python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install --upgrade pip
.venv-macos/bin/python -m pip install -r requirements-macos.txt
```

## 构建

```bash
MEFINDER_PYTHON=.venv-macos/bin/python ./build_macos.sh
```

脚本会：

1. 生成不含私人语料的空白 SQLite 索引；
2. 从 SVG 生成 macOS `.icns` 图标；
3. 运行桌面、PDFKit 与索引回归测试；
4. 在系统临时目录中构建桌面应用和独立 onefile `MEFinderMCP` sidecar；
5. 把 sidecar 固定到 `MEFinder.app/Contents/MacOS/MEFinderMCP`，用真实 STDIO 客户端冒烟并随外层应用签名；
6. 检查包内包含 PDFKit 桥接模块、许可证材料，且没有 API 密钥、偏好设置或日志；
7. 在系统临时目录中生成并验证 ZIP 与 DMG，避免“文稿”目录的 File Provider
   给 `.app` 重新附加 Finder 元数据；
8. 验证 DMG 中包含 `MEFinder.app` 和指向 `/Applications` 的快捷方式，挂载镜像后
   再次严格校验应用签名；
9. 生成以下发布文件：

```text
release/MEFinder-v<版本>-macos-<架构>.dmg
release/MEFinder-v<版本>-macos-<架构>.dmg.sha256.txt
release/MEFinder-v<版本>-macos-<架构>.zip
release/MEFinder-v<版本>-macos-<架构>.zip.sha256.txt
```

## 用户安装

推荐向普通用户提供 DMG。用户打开 DMG 后，把 `MEFinder.app` 拖到
`Applications`，弹出镜像；之后可从“应用程序”、Launchpad 或 Spotlight 启动。
ZIP 主要作为备用分发格式。

本地构建默认使用 PyInstaller 的 ad-hoc 签名，适合开发测试。面向其他用户发布前仍需使用
Apple Developer ID 签名并完成 notarization；未公证的包可能被 Gatekeeper 拦截。
DMG 本身不会绕过 Gatekeeper：ad-hoc 签名版本首次运行时，用户可能仍需在访达中
右键应用并选择“打开”一次。Developer ID 签名和公证完成后，普通用户才可直接双击启动。

如果构建机已经安装 Developer ID Application 证书，可以让脚本保留该签名并启用
hardened runtime 与可信时间戳：

```bash
MEFINDER_CODESIGN_IDENTITY="Developer ID Application: 名称 (TEAMID)" \
MEFINDER_PYTHON=.venv-macos/bin/python \
./build_macos.sh
```

这只完成签名；正式外部分发仍需另外执行 Apple notarization 和 stapling。

使用 Developer ID 时，脚本会对 sidecar 和外层 `.app` 都启用 hardened runtime 与可信时间戳，并在未签名副本、签名应用、ZIP 解包、DMG 挂载和 DMG 复制五个位置实际建立 MCP STDIO 会话。Codex 的稳定命令路径为 `/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP`；覆盖替换应用后无需修改配置。

## 签名与扩展属性

如果项目位于启用了 iCloud Drive“桌面与文稿”的目录，File Provider 可能在构建完成后
给 `dist/MEFinder.app` 或手工复制的 `.app` 重新附加 `com.apple.FinderInfo`。
该属性不一定改变应用内容，但会让 `codesign --verify --deep --strict` 报错。

构建脚本因此始终从系统临时目录中的洁净应用生成 ZIP 和 DMG，并在发布前后多次验证。
它不再把裸 `.app` 持久化到工作区的 `dist/`：在 File Provider 管理目录内，即使刚清除
属性并通过校验，Finder 仍可能立刻把属性写回来。正式交付和本机测试都应使用
`release/` 下校验通过的 DMG；ZIP 是备用格式。

## 数据位置

应用包只带空白索引和公开配置。首次启动后，可变数据写入：

```text
~/Library/Application Support/MEFinder/
├── mineru_api.local.json
├── vision_api.local.json
├── preferences.json
└── runtime/
    ├── data/
    ├── config/
    ├── corpus/
    └── desktop.log
```

升级 `.app` 不会覆盖这个目录中的用户文献、索引、API 配置和偏好设置。

自动化冒烟测试如需隔离真实用户数据，可以在启动应用前设置
`ME_FINDER_APP_DATA_ROOT`，将运行时数据临时指向其他目录。

## Homebrew Cask tap 分发

macOS 产物同时通过自建 Homebrew tap 分发，让会使用命令行的用户用
`brew install --cask mefinder` 安装、`brew upgrade --cask mefinder` 升级。
tap 只指向 GitHub Releases 的公开资产；更新通道在应用之外，应用本身继续保持零联网，
不改变「本地优先」原则。

### 用户侧使用

```bash
brew trust sabercomo/mefinder   # 新版 Homebrew 要求先信任第三方 tap
brew tap sabercomo/mefinder
brew install --cask mefinder
brew update && brew upgrade --cask mefinder
```

Homebrew 渠道要求 macOS 14 及以上（cask 的 `depends_on macos` 取两架构产物中较严的
`LSMinimumSystemVersion`：arm64=Sonoma、x86_64=Monterey）；Intel 机若是 macOS 12–13，
请直接从 Releases 下载 DMG 安装。

未公证包的首次启动仍需在「系统设置 → 隐私与安全性 → 仍要打开」批准一次；之后的
`brew upgrade` 会延续已批准状态（Homebrew 升级时会把旧版本已批准的 Gatekeeper
状态带到新版本）。应用数据在 `~/Library/Application Support/MEFinder/`，
升级与卸载都不受影响（cask 刻意不带 `zap`）。

### 维护流程（发版后）

1. 按既有流程正式发布 vX.Y.Z（含 macOS 双架构 DMG 资产，tag/Release 仍须单独授权）。
2. 在仓库根目录运行：

   ```bash
   .venv-macos312-arm64/bin/python scripts/update_homebrew_tap.py --version X.Y.Z
   ```

   脚本从 `release/` 读取该版本 DMG、与 `.sha256.txt` sidecar 交叉校验摘要，渲染
   `homebrew-tap/Casks/mefinder.rb`。省略 `--version` 时取 `release/` 里最新构建。
3. 同步更新 `tests/test_homebrew_tap_cask.py` 里的 `PUBLISHED_DMG_SHA256` 金样基线
   （该测试要求 cask、脚本渲染输出与已发布 digest 三方一致），并提交主仓库。
4. 首次接入：创建独立 GitHub 仓库 `sabercomo/homebrew-mefinder`（空仓库即可，tap 名
   即 `sabercomo/mefinder`），克隆到本地后运行：

   ```bash
   .venv-macos312-arm64/bin/python scripts/update_homebrew_tap.py \
       --tap-repo <本地克隆路径> --push
   ```

   脚本会把 `Casks/mefinder.rb` 与 `README.md` 复制进 tap 仓库并提交推送。
5. 若某版本只构建了单一架构，脚本会自动切换为单架构 cask 并加 `depends_on arch`
   门禁，防止 Intel 用户装到不可运行的包。

### 发版后自动化（GitHub Actions）

`.github/workflows/homebrew-tap.yml` 在 GitHub Release **published** 时触发（也可
`workflow_dispatch` 手动传 `version` 重跑），自动完成上面第 4 步的**推送独立 tap
仓库**部分：从该 Release 下载 `MEFinder-v*-macos-*.dmg` 资产、运行
`update_homebrew_tap.py --tap-repo … --push`，把新版 `Casks/mefinder.rb` 推到独立
tap 仓库。这样正式发版后 `brew upgrade --cask mefinder` 即可跟到新版，无需本地再动手。

首次接入需在**主仓库** Settings → Secrets and variables → Actions 配置两项：

- 变量 `HOMEBREW_TAP_REPO`：独立 tap 仓库的 `owner/repo`（如 `sabercomo/homebrew-mefinder`）。
- 密钥 `HOMEBREW_TAP_TOKEN`：对该 tap 仓库有 `contents:write` 权限的 PAT
  （细粒度 PAT 建议只授权该单仓库）。默认的 `GITHUB_TOKEN` 只能访问主仓库，跨仓库推送必须用它。

缺任一项时任务如实失败，不会静默跳过。

> 该 workflow 只推送**独立 tap 仓库**（用户侧安装源）。主仓库内的镜像
> `homebrew-tap/Casks/mefinder.rb` 与测试金样 `tests/test_homebrew_tap_cask.py`
> 的 `PUBLISHED_DMG_SHA256` **不由它更新**（避免向 `main` 回写），仍按上面第 2–3 步
> 在本地更新并随迭代提交。二者只是主仓库内的一致性镜像，不影响用户 `brew upgrade`。

注意：cask 已在本机 Homebrew 7.0.0 通过 `brew style` 与
`brew audit --cask mefinder --online`（实测下载并校验产物）。审计要求 cask 的
`depends_on macos` 不得低于产物内声明的 `LSMinimumSystemVersion`；发新版后若产物
系统要求变化，需同步调整渲染脚本中的门槛常量。
