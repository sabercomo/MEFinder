# macOS 构建说明

macOS 版本沿用现有的 Python 后端和 HTML/CSS/JavaScript 界面，用 pywebview 的
Cocoa/WebKit 窗口封装，并由 PyInstaller 生成原生 `.app`。

项目支持两条并行的 macOS 构建流程：

- **默认 Apple Silicon / macOS 14 (`arm64`)** —— 现有流程，不受本文新增内容影响；
- **独立的 macOS 12 Intel (`x86_64`)** —— 见下方 [macOS 12 Intel (x86_64) 独立构建](#macos-12-intel-x86_64-独立构建)。

两条流程共用同一个 `build_macos.sh` 和 `desktop_macos.spec`，通过环境变量区分目标：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MEFINDER_TARGET_ARCH` | 构建机架构 | 目标架构：`arm64` 或 `x86_64`。 |
| `MEFINDER_MIN_MACOS_VERSION` | `14.0` | 写入 `Info.plist` 的 `LSMinimumSystemVersion`，同时作为 `MACOSX_DEPLOYMENT_TARGET` 和 Mach-O 校验的上限。 |
| `MEFINDER_PYTHON` | `python3` | 用于构建的解释器；x86_64 构建需指向能在 Rosetta 下运行的解释器。 |

## 环境

- macOS 14 或更高版本（默认 `arm64` 构建的 Python 运行库最低要求为 14.0）；
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

1. 校验 `MEFINDER_PYTHON` 实际运行架构与 `MEFINDER_TARGET_ARCH` 一致（防止在
   Apple Silicon 上用 arm64 解释器“伪造” x86_64 构建）；
2. 生成不含私人语料的空白 SQLite 索引；
3. 从 SVG 生成 macOS `.icns` 图标；
4. 运行桌面、PDFKit 与索引回归测试；
5. 在系统临时目录中构建并签名 `MEFinder.app`；
6. 检查包内包含 PDFKit 桥接模块，且没有 API 密钥、偏好设置或日志；
7. 递归检查 `.app` 内全部 Mach-O 文件（`tools/verify_macos_binaries.py`）：
   逐个运行 `file` / `lipo -info` / `vtool -show-build`，确保每个二进制都含目标架构、
   不含其它架构（即 x86_64 构建里不存在仅 arm64 的二进制），且没有任何 slice 的最低
   系统版本高于 `MEFINDER_MIN_MACOS_VERSION`；
8. 在系统临时目录中生成并验证 ZIP 与 DMG，避免“文稿”目录的 File Provider
   给 `.app` 重新附加 Finder 元数据；
9. 验证 DMG 中包含 `MEFinder.app` 和指向 `/Applications` 的快捷方式，挂载镜像后
   再次严格校验应用签名；
10. 生成以下发布文件：

```text
release/MEFinder-v<版本>-macos-<架构>.dmg
release/MEFinder-v<版本>-macos-<架构>.dmg.sha256.txt
release/MEFinder-v<版本>-macos-<架构>.zip
release/MEFinder-v<版本>-macos-<架构>.zip.sha256.txt
```

## macOS 12 Intel (x86_64) 独立构建

这条流程在 Apple Silicon 机器上通过 Rosetta 生成面向 **macOS 12 Intel** 的
`x86_64` 应用，与默认的 `arm64` / macOS 14 流程完全独立，互不影响。

### 为什么需要单独的 Python

默认构建使用的 Command Line Tools / Xcode 版 Python（universal2）虽然能在
`arch -x86_64` 下运行，但它的 **x86_64 slice 的最低系统版本是 macOS 14.0**
（`vtool -arch x86_64 -show-build` 可见 `minos 14.0`）。用它打包出的
`lib-dynload/*.so` 和 `Python3.framework` 会强制要求 macOS 14，无法在 macOS 12 上启动。

因此 x86_64 构建必须使用 **Python.org 官方 universal2 安装包**，其 x86_64 slice 的
部署目标为 macOS 10.9，远低于 12.0。请勿改用 Homebrew 的 arm64 Python 假装生成
x86_64 构建——`build_macos.sh` 的架构自检会直接拒绝。

### 1. 安装 Python.org universal2 Python

从 <https://www.python.org/downloads/macos/> 下载 **macOS 64-bit universal2
installer**（当前使用 3.12.10，是 3.12 系列最后一个提供官方二进制安装包的版本）：

```bash
cd /tmp
curl -LO https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg
sudo installer -pkg python-3.12.10-macos11.pkg -target /
```

安装后确认 x86_64 slice 的最低系统版本不高于 12.0：

```bash
vtool -arch x86_64 -show-build \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
# 期望看到 platform MACOS / minos 10.9（≤ 12.0）
```

### 2. 在 Rosetta 下创建独立 x86_64 虚拟环境

关键点：universal2 解释器在 Apple Silicon 上默认运行 arm64 slice，必须用
`arch -x86_64` 强制走 Rosetta，虚拟环境和其中安装的所有二进制才会是 x86_64。

```bash
PYORG=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12

# 用 x86_64 slice 创建虚拟环境
arch -x86_64 "$PYORG" -m venv .venv-macos12-x86_64

# 写入一个强制 Rosetta 的包装器，供 build_macos.sh 使用
cat > .venv-macos12-x86_64/bin/python-x86_64 <<'SH'
#!/bin/bash
exec arch -x86_64 "$(dirname "$0")/python" "$@"
SH
chmod +x .venv-macos12-x86_64/bin/python-x86_64

# 安装依赖（全部解析为 x86_64 / universal2 wheel）
.venv-macos12-x86_64/bin/python-x86_64 -m pip install --upgrade pip
.venv-macos12-x86_64/bin/python-x86_64 -m pip install -r requirements-macos.txt
```

> **证书说明**：Python.org 的独立 Python 默认不使用系统钥匙串校验 TLS，需要运行
> 安装包附带的 `Install Certificates.command` 或安装 `certifi` 才能让 `pip` 访问
> PyPI。若尚未配置证书，最简单的办法是指向系统自带的 CA bundle：
>
> ```bash
> export SSL_CERT_FILE=/etc/ssl/cert.pem
> export PIP_CERT=/etc/ssl/cert.pem
> ```
>
> 同一环境变量也建议在运行 `build_macos.sh` 时导出，以防联网的元数据回归测试
> 走到 TLS 校验。

确认解释器与依赖确实是 x86_64：

```bash
.venv-macos12-x86_64/bin/python-x86_64 -c 'import platform; print(platform.machine())'
# 期望输出 x86_64
```

### 3. 构建 macOS 12 Intel 版本

```bash
SSL_CERT_FILE=/etc/ssl/cert.pem \
MEFINDER_PYTHON=.venv-macos12-x86_64/bin/python-x86_64 \
MEFINDER_TARGET_ARCH=x86_64 \
MEFINDER_MIN_MACOS_VERSION=12.0 \
./build_macos.sh
```

脚本会执行与默认流程相同的步骤，并额外用 `MEFINDER_MIN_MACOS_VERSION=12.0`
约束 `Info.plist` 与全部 Mach-O 的最低系统版本；架构自检和递归 Mach-O 校验
（第 1、7 步）保证产物为纯 x86_64 且无 14.0 遗留二进制。

### 4. 手动复核 Mach-O（可选）

构建过程已自动调用校验脚本；如需单独复核任意 `.app`：

```bash
python3 -m tools.verify_macos_binaries \
  --require-arch x86_64 --max-min-version 12.0 --forbid-extra-arch \
  release-stage/MEFinder.app
```

### 5. 在 Apple Silicon 上用 Rosetta 冒烟测试

```bash
arch -x86_64 /Volumes/…/MEFinder.app/Contents/MacOS/MEFinder   # 或从 DMG 拖出后双击
```

### 产物

```text
release/MEFinder-v<版本>-macos-x86_64.dmg
release/MEFinder-v<版本>-macos-x86_64.dmg.sha256.txt
release/MEFinder-v<版本>-macos-x86_64.zip
release/MEFinder-v<版本>-macos-x86_64.zip.sha256.txt
```

### 已知限制

- 需要在装有 Rosetta 2 的 Apple Silicon 机器上，或原生 Intel Mac 上构建；纯
  arm64 环境无法生成 x86_64 产物。
- x86_64 应用在 Apple Silicon 上通过 Rosetta 运行，性能低于原生 arm64 版本；
  面向 Apple Silicon 用户应继续分发 `arm64` 版本。
- 产物的最低系统版本由所打包二进制的部署目标共同决定，`MEFINDER_MIN_MACOS_VERSION`
  只保证不低估；若某个第三方 wheel 的 slice 目标高于 12.0，校验会失败而不是被绕过，
  需改用目标更低的 wheel 版本（见下方“依赖与最低系统版本”）。

### 依赖与最低系统版本

使用 Python.org universal2 Python 3.12.10 + `requirements-macos.txt`（未改动任何
版本）安装后，各二进制的 x86_64 slice 最低系统版本实测如下，全部 ≤ macOS 12.0，
无需调整依赖版本：

| 组件 | wheel 标签 | x86_64 slice 最低系统版本 |
| --- | --- | --- |
| Python 运行库（Python.framework） | universal2 | 10.13 |
| PyInstaller 6.21.0 引导器 | `macosx_10_13_universal2` | 10.13 |
| pyobjc-core / Cocoa / Quartz(PDFKit) / WebKit / Security 11.1 | `macosx_10_13_universal2` | 10.13 |
| PyMuPDF 1.26.5（`_mupdf`/`libmupdf` 等） | `macosx_10_9_x86_64` | 10.9 |
| pywebview 6.2.1 | 纯 Python | 不适用 |

对比：默认构建所用的 Command Line Tools / Xcode Python，其 `lib-dynload/*.so` 与
`Python3.framework` 的 x86_64 slice 最低系统版本为 **14.0**，这正是不能直接复用它
生成 macOS 12 产物的原因。

### 已验证结果（v0.3.6，2026-08-04）

在 Apple Silicon（macOS 15.7.3）+ Rosetta 2 上完成并通过：

- 559 项回归测试在 x86_64 解释器下运行，结果 `OK`；
- 递归 Mach-O 校验：79 个 Mach-O 文件全部为纯 `x86_64`，无 arm64 slice，最高
  最低系统版本 10.13（≤ 12.0）；
- 产物 `Info.plist` 的 `LSMinimumSystemVersion = 12.0`；
- 从 DMG 拖出应用后在 Rosetta 下启动，后端在 `127.0.0.1` 就绪，`/api/library`
  与 `POST /api/search`（FTS5）返回正确 JSON，退出无崩溃报告。

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
