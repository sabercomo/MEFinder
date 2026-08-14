# macOS 构建说明

> 构建入口 `build_macos.sh` 与依赖清单仍位于仓库根目录。

macOS 版本沿用现有的 Python 后端和 HTML/CSS/JavaScript 界面，用 pywebview 的
Cocoa/WebKit 窗口封装，并由 PyInstaller 生成原生 `.app`。

## 环境

- Apple Silicon 发布包最低支持 macOS 14；
- Intel 发布包最低支持 macOS 12；
- Python 3.9 或更高版本；
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
4. 在系统临时目录中构建并签名 `MEFinder.app`；
5. 检查包内包含 PDFKit 桥接模块，且没有 API 密钥、偏好设置或日志；
6. 在系统临时目录中生成并验证 ZIP 与 DMG，避免“文稿”目录的 File Provider
   给 `.app` 重新附加 Finder 元数据；
7. 验证 DMG 中包含 `MEFinder.app` 和指向 `/Applications` 的快捷方式，挂载镜像后
   再次严格校验应用签名；
8. 生成以下发布文件：

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
