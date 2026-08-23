# 本地 OCR 分发与合规清单

**状态：macOS Apple Silicon 代码与安装链路已验证；Windows x64 真机验证按 [local-ocr-spike.md](local-ocr-spike.md) G5 在另一台机器执行。**

可执行的平台数据表是 [`src/me_finder/local_ocr_manifest.json`](../src/me_finder/local_ocr_manifest.json)。本文记录人可读的来源、许可和卸载边界；若与数据表不一致，不得发布。

## 上游组件资产

| 组件 | tag | 固定来源 | bytes | SHA-256 |
|---|---:|---|---:|---|
| NDLOCR-Lite | 1.2.3 | `https://codeload.github.com/ndl-lab/ndlocr-lite/tar.gz/refs/tags/1.2.3` | 147,906,248 | `c96f3ab5cd03bc46b5be939d8e31a7a63b059ae150b393d3fa9a76e78789d2ce` |
| NDL古典籍OCR-Lite | 1.4.3 | `https://codeload.github.com/ndl-lab/ndlkotenocr-lite/tar.gz/refs/tags/1.4.3` | 80,177,931 | `b33a09d45c4e4cc2f2e037f07fcd010275c89d30c7c78372e0f27c0fb659b163` |

两个上游项目均以 CC BY 4.0 分发。安装后的 `source/LICENCE` 是上游许可证原文，`installed.json` 保留署名、来源 URL、字节数、摘要和修改说明。MEFinder **未修改上游源码**；只为页图 CLI 建立独立 venv，并从安装清单中剪除 `flet`、`reportlab`、`pypdfium2` 和 `pypdf` 等 GUI/PDF 输出依赖。

## uv 资产

固定 uv 0.12.1，来源为 Astral 官方 release mirror，许可标识为 `Apache-2.0 OR MIT`。

| 平台 | 资产 | bytes | SHA-256 |
|---|---|---:|---|
| macOS arm64 | `uv-aarch64-apple-darwin.tar.gz` | 17,679,560 | `77d2906988e8074fd43f2f329ec452ebbf9b0c257ba1c66451c71de70a6baf42` |
| macOS x86_64 | `uv-x86_64-apple-darwin.tar.gz` | 19,622,543 | `69d9f9a00337f25a50dcb13882052da08b8469bac11091c98c5694c3c6721467` |
| Windows x64 | `uv-x86_64-pc-windows-msvc.zip` | 19,073,343 | `8fcb0cb46e1229065e344758980924e569bef5882ef45f46fada8fb24e06b74a` |
| Linux glibc x64 | `uv-x86_64-unknown-linux-gnu.tar.gz` | 21,760,555 | `90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb` |

## 依赖与 SBOM

直接 pin 见平台数据表：macOS 使用 `onnxruntime==1.23.2`，Windows/Linux 使用 `onnxruntime==1.26.0`；其余包括 `numpy==2.2.2`、`pillow==12.1.1`，现代版另有 `opencv-python-headless==4.11.0.86`，以及上游源码模块实际 import 的固定运行时依赖。

每次安装会从最终 venv 的 `importlib.metadata` 生成 `sbom.spdx.json`：包含实际安装的直接与传递包版本、许可元数据、许可分类和 dist-info 许可文件路径。因此 SBOM 反映实际运行时，而不是仅复制顶层 pin。

## 安装、验证与卸载边界

- Windows 与 macOS 使用同一个设置页入口和同一个安装状态机；平台资产、venv Python 相对路径和 onnxruntime pin 只从 manifest 读取。
- 验证先执行 `<venv-python> <ocr.py> --help`，再把上游自带样图统一为单页 PNG，按 `--sourceimg <png> --output <dir>` 识别并解析 JSON。
- 安装位于机器本地的 `runtime/components/local-ocr/`；不写 PATH、注册表、plist 或系统 Python。
- 卸载删除该引擎的源码、模型和 venv；最后一个引擎卸载后同时删除共享的 uv 和 uv-managed Python。若用户已将设置切换为其他手动路径，卸载不清空该手动路径。
- `config/local_ocr.json`、组件目录、OCR 大结果与 parser manifest 不进入轻量备份；备份仍只保留 `pdf_imports.json` 中的可移植相对 attachment。

## macOS Apple Silicon 实机记录（2026-08-23）

- 在同一个包含中文、日文和空格的运行根目录中，先后安装 `ndlkotenocr-lite` 1.4.3 与 `ndlocr-lite` 1.2.3。
- 两个 codeload tarball 与 uv 0.12.1 arm64 资产均通过 bytes + SHA-256 校验；uv-managed Python 3.11、两个独立 venv 与固定 CPU wheel 安装成功。
- 两个引擎的 `--help` 和自带样图单页 PNG 识别均产生可解析 JSON；随后的“重新验证”也均通过。
- 卸载第一个引擎后共享 uv/Python 仍存在；卸载最后一个后 `components/local-ocr/` 完全消失，配置中不再有可用引擎。
