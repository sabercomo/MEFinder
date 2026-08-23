# 本地 OCR 组件 — 一键安装实施方案

**状态：方案已定，落实 [local-ocr-plan.md](local-ocr-plan.md) §9 结尾与 [local-ocr-spike.md](local-ocr-spike.md) G5 承诺的“一键下载”。** 本文件只覆盖“组件如何被安装/配置”，不改动页图 runner、结果契约、路由与索引——那些仍按 local-ocr-plan.md 执行。

## 0. 结论先行

上游 `ndlocr-lite` 与 `ndlkotenocr-lite` 满足一键安装的全部前置条件（下节为查证事实）。首版采用 **uv 托管安装（源码 tarball + 独立 venv）**，不自建平台可执行体、不复用官方 GUI 包。四个桌面平台由 `uv` 自动分流 Python 与 onnxruntime wheel。

## 1. 上游查证事实（2026-08 核对）

| 维度 | 现代版 NDLOCR-Lite | 古籍版 NDL古典籍OCR-Lite |
|---|---|---|
| 仓库 | `github.com/ndl-lab/ndlocr-lite` | `github.com/ndl-lab/ndlkotenocr-lite` |
| 采用 tag | `1.2.3` | `1.4.3` |
| 运行时依赖 | onnxruntime，**无 torch** | onnxruntime，**无 torch、无 opencv** |
| 权重 | `src/model/` 内 4×onnx ≈157 MB | `src/model/` 内 2×onnx ≈83 MB |
| 权重来源 | git 直接提交，**不联网、非 LFS** | 同左 |
| 加载路径 | `Path(__file__).parent / "model" / "*.onnx"`，缺文件 assert 失败 | 同左 |
| CLI 入口 | `python src/ocr.py --sourceimg <img> --output <dir>` | 同左 |
| 单页 PNG | 支持（png/jpg/tiff/bmp/…） | 支持 |
| console_script | `ndlocr-lite = ocr:main`（pyproject） | 同族 |
| 许可证 | CC BY 4.0 | CC BY 4.0 |
| 官方开箱包 | Flet(Flutter+Python) GUI，内部解释器不可直调 | 同左 |

关键推论：

- **拿到源码 tarball ＝ 同时拿到权重**。GitHub codeload 的 tag tarball 含已提交的 onnx，无需 LFS smudge、无需单独抓权重。
- **纯 CPU、纯 onnxruntime**，全部依赖有 manylinux/win/mac wheel，无编译、无 CUDA。
- **不复用官方 GUI 包**：它是 Flet 打包，内部 python 不暴露为可调 `ocr.py`；复用它违背 local-ocr-plan.md §9“别把 GUI ZIP 当 CLI 包”。

## 2. 安装链路（B′：uv 托管）

对每个引擎独立执行、独立目录、可分别启用：

```text
1. 下载源码 tarball（按 tag）        -> ocr.py + src/model/*.onnx
   固定 URL：https://codeload.github.com/ndl-lab/<repo>/tar.gz/refs/tags/<tag>
2. 校验 SHA-256 -> 解压到组件目录     -> 校验失败即中止并回滚，不留半成品
3. uv venv + uv pip install（pin 清单）-> onnxruntime 等 CPU wheel，uv 自动分平台
4. 自动写入组件路径                  -> <venv-python> 与 <解压目录>/src/ocr.py
5. 验证：先 --help，再对自带样图跑一次 --sourceimg，确认 JSON 产出
```

`uv` 二进制随应用附带或首次按需拉取（每平台单文件，静态、约 30 MB），用于第 3 步创建 venv 与装依赖。四平台矩阵（Windows / macOS Apple Silicon / macOS Intel / Linux）由 uv 选择正确的 Python 与 onnxruntime wheel，MEFinder 不触碰编译与 Mac 代码签名。

## 3. 依赖 pin 清单

不直接沿用上游 `requirements.txt`（含 GUI 框架）。为每引擎维护 MEFinder 自己的 pin 清单，剔除 CLI 页图路径用不到的项：

- 保留：`onnxruntime`、`numpy`、`pillow`；现代版另加 `opencv-python-headless`。
- 剔除候选：`flet`（GUI）、`reportlab`/`pypdfium2`（PDF 输出，页图路径不用）。剔除前必须在 spike 中确认 `ocr.py --sourceimg --json-only` 不 import 这些模块，否则保留。
- onnxruntime 版本按平台条件：`1.26.0`（Py3.11+ 非 Darwin）/ `1.23.2`（Py3.10 或 macOS），与上游一致以规避 ABI 差异。

pin 清单、tarball URL、tag、每文件 SHA-256 合并为一份**组件清单（manifest）**，按平台各一份；应用运行时按当前平台选清单。清单是 G5 的产出物，冻结后随版本发布。

## 4. 与现有边界的对齐

- **§9 组件边界不变**：组件路径仍是机器状态，不进备份；parser manifest 与 OCR 大结果不进轻量备份；`pdf_imports.json` 只保留可移植相对 attachment。一键安装只是把“手填路径”替换为“下载后自动填同样的路径字段”，下游契约零改动。
- **设置页**：在原“本地 OCR 组件”分组内，为每引擎增加安装状态与操作：未装显示“下载安装”，已装显示版本 + tag + “重新验证/卸载”。手动填路径入口保留为高级回退。
- **卸载边界**：删除组件目录与其 venv 即可，无系统级写入、无 PATH 改动、无注册表/plist。

## 5. 下载器状态机

单引擎安装是可恢复状态机，任一步失败均回到 `not_installed` 且不留痕：

```text
not_installed
  -> downloading      （tarball，带进度、断点续传）
  -> verifying        （SHA-256 比对 manifest）
  -> extracting       （解压到组件目录）
  -> provisioning     （uv venv + uv pip install）
  -> validating       （--help 通过后跑样图，确认 JSON）
  -> installed        （写入路径字段 + 记录已装 tag/版本）
失败分支：任意步失败 -> cleaning -> not_installed（删目录/venv，报明确错误）
取消：终止子进程 -> cleaning -> not_installed
```

约束：

- 两引擎合计首次下载约 250 MB 级，进度条 + 断点续传 + 校验失败回滚为硬要求。
- 安装与既有导入 worker 隔离；安装期间同引擎的 OCR 任务被安装态互斥锁挡住。
- 组件目录路径覆盖中文/日文/空格/ASCII 的 Unicode 兼容性沿用 spike G2 的验证项，不在此假设已解决。

## 6. 合规产出（G5 放行前必须齐备）

一键安装启用前，必须产出并随清单发布：

- 每引擎 tarball 的精确 codeload URL、字节数、SHA-256。
- 上游 CC BY 4.0 署名、许可证原文与“MEFinder 未修改上游源码，仅在独立 venv 中运行”的说明；若剔除依赖属于打包裁剪而非改源码，需在说明中写明。
- 组件 venv 内第三方依赖的 SBOM 与许可证清单（onnxruntime、numpy、pillow、opencv 等）。
- Windows / macOS 各自的安装入口、验证步骤与卸载边界。
- `uv` 二进制的来源、版本与校验值。

官方 GUI release ZIP 不作为安装来源；安装来源固定为按 tag 的源码 tarball，并实际跑通页图命令后才可放行。

## 7. 验收标准

- 全新环境下，仅通过设置页操作即可完成任一引擎安装，无需用户接触命令行或预装 Python。
- 安装后自动写入的 `<venv-python>` 与 `ocr.py` 路径能被既有 runner 直接使用，`--sourceimg` 单页 PNG 跑通并产出结构化 JSON。
- 校验失败、下载中断、取消、磁盘不足均回到干净的 `not_installed`。
- 卸载后不残留组件目录、venv 或任何系统级更改。
- 四平台矩阵各自完成一次真实安装 + 样图识别（对齐 spike G4/G5 平台矩阵）。
- 手动填路径的既有回退在一键安装存在时仍可用。

质量与运行时验证仍见 [local-ocr-spike.md](local-ocr-spike.md)；本文件不重复 G1–G4，仅落实 G5 的分发与合规部分。
