# 包体积盘点 — v0.5.4 macOS arm64（阶段1 验收基线）

- 日期：2026-09-14
- 结论：**主应用与 MCP sidecar 各自携带一份完整计算/解析栈,存在大量重复**;这是阶段2「对齐计算组件独立分发 + 精简主包」最直接的收益来源。
- **被测产物(固定)**:`MEFinder-v0.5.4-macos-arm64.zip`
  - 字节:166,229,655
  - **SHA-256:`9d20bf801d4db54ee62f6c7b127592af5e7edab8a6e74d1f235473889a0278b4`**（与 `docs/release-notes-0.5.4.md` 当前权威条目一致；构建源 `b1f0082`）
- 方法:`ditto -x -k` 解包 → `du -sk`(磁盘占用) + `stat -f%z` 求和(逻辑大小);sidecar 用 `PyInstaller.archive.readers.CArchiveReader` 只读枚举条目(**未执行二进制**)。

## 0. 三种体积口径（不可混用）

| 口径 | 含义 | 工具 |
|---|---|---|
| **压缩包大小** | 分发下载体积（ZIP/DMG 字节） | 文件字节数 |
| **逻辑大小** | 各文件内容字节之和（解压后应用真实内容） | `stat -f%z` 求和 |
| **磁盘占用** | 文件系统分配的块（含块对齐/目录开销，随卷簇大小变化） | `du -sk` |

## 1. 分发体积（压缩包）

| 产物 | 字节 | MiB | 本轮状态 |
|---|---|---|---|
| macOS arm64 ZIP | 166,229,655 | 158.5 | **已测（被测产物，SHA 见上）** |
| macOS arm64 DMG | 176,372,120 | 168.2 | 仅记字节，未解包 |
| macOS x86_64 ZIP | 171,323,096 | 163.4 | 仅记字节，安装体积**未测** |
| macOS x86_64 DMG | 180,602,404 | 172.2 | 仅记字节，安装体积**未测** |
| Windows | — | — | **本轮未盘点(未测)** |

> x86_64 安装体积与内部依赖分布**本轮未测**,不由 arm64 外推(不同架构的原生库体积不同,如 onnxruntime)。Windows 双件套本轮完全未盘点。

## 2. 安装体积（arm64 `MEFinder.app`，解包后）

| 口径 | 值 |
|---|---|
| 逻辑大小 | 298,037,416 B ≈ **284.2 MiB** |
| 磁盘占用 | 291,944 KiB ≈ **285.1 MiB** |

四大组成(逻辑大小 / 磁盘占用):

| 组件 | 逻辑 MiB | 磁盘 MiB | 说明 |
|---|---|---|---|
| `Contents/MacOS/MEFinder` | 14.5 | 14.5 | 主应用引导 exe（运行时/依赖在 Frameworks） |
| `Contents/MacOS/MEFinderMCP` | **77.9** | 77.9 | **MCP sidecar onefile,自带独立 Python 运行时 + 全套依赖** |
| `Contents/Frameworks` | 185.9 | 186.2 | 主应用的 Python 运行时与全部第三方依赖 |
| `Contents/Resources` | 5.8 | 6.4 | 模板/静态资源/题录数据等 |

### 2.1 主应用 `Frameworks/` 重头依赖（磁盘占用 MiB）

| 依赖 | MiB | 归属 |
|---|---|---|
| onnxruntime | 70.8 | 对齐计算（阶段2 候选移出） |
| pymupdf | 44.0 | PDF 导入期解析核心（**保留主包**） |
| cryptography | 11.0 | 配置/凭据加密（保留） |
| python3.12 | 8.4 | 运行时（保留） |
| tokenizers | 8.1 | 嵌入/对齐（阶段2 候选） |
| PIL | 8.0 | 图像处理 |
| Python.framework | 7.6 | 运行时（保留） |
| hf_xet | 7.5 | HuggingFace 模型下载加速（仅模型下载用，阶段2 候选） |
| numpy | 7.0 | 对齐计算（阶段2 候选） |
| pydantic_core | 4.0 | — |
| py_rust_stemmers | 0.6 | fastembed 依赖（阶段2 候选） |

## 3. sidecar 内部条目（CArchiveReader 只读枚举，不执行）

`MEFinderMCP` 是 PyInstaller **onefile**:249 个条目,**未压缩合计 198.2 MiB**,归档内**压缩后 77.2 MiB**(加 bootloader 后磁盘文件 77.9 MiB)。

按顶层条目(压缩 d / 未压缩 u，MiB):

| 条目 | 压缩 d | 未压缩 u | 类别 |
|---|---|---|---|
| onnxruntime | 19.5 | 70.8 | 计算 |
| pymupdf | 20.3 | 43.9 | PDF 解析（保留） |
| **PYZ.pyz** | 12.6 | 12.6 | **纯 Python 模块归档(混合，不可单独归属)** |
| cryptography | 3.6 | 11.3 | 共享 |
| python3.12 | 2.6 | 8.3 | 共享运行时 |
| tokenizers | 2.7 | 8.1 | 计算 |
| PIL | 3.1 | 8.0 | 共享 |
| Python.framework | 2.2 | 7.6 | 共享运行时 |
| hf_xet | 3.5 | 7.5 | 模型下载 |
| numpy | 2.0 | 6.9 | 计算 |
| py_rust_stemmers | 0.2 | 0.6 | 计算 |

**开销与共享条目单列**:`PYZ.pyz`(12.6 MiB 压缩)是所有纯 Python 模块(含 fastembed、numpy 的 .py 层、mcp、bottle 等)的合并归档,**无法按依赖切分**;`base_library.zip`(0.4 MiB)、`Python.framework`/`python3.12`/`libcrypto`/`libssl` 等是**共享运行时**,并非某一依赖独占。

## 4. 阶段2 精简候选(测算,非承诺)

**主应用 `Frameworks/`** 内**仅供嵌入/对齐计算**的依赖(磁盘占用):

| 依赖 | MiB |
|---|---|
| onnxruntime | 70.8 |
| tokenizers | 8.1 |
| hf_xet | 7.5 |
| numpy | 7.0 |
| py_rust_stemmers | 0.6 |
| **小计** | **≈94.0** |

**关于 sidecar 的重复:sidecar 内含同一套计算依赖(onnxruntime 19.5↓/70.8↑、tokenizers 2.7/8.1、numpy 2.0/6.9、hf_xet 3.5/7.5、py_rust_stemmers 0.2/0.6),但 sidecar 的总大小(77.9 MiB)不能当作"对齐依赖的可移除大小"**——其中 pymupdf(PDF 解析)、cryptography、Python 运行时、PYZ.pyz 都是非对齐/共享条目;可移除量只能按上列计算条目计,且 PYZ 内的对齐 Python 代码无法单独切分,故这是**下限**。sidecar 是否真的需要 onnxruntime(取决于 MCP 侧是否做嵌入重排)另行核查,不在本轮结论内。

**约束与佐证**

- `numpy` 由 5 个模块 import,全部对齐相关且均为函数内 lazy import:`text_alignment`(仅 `align_segment_sequences` 内) / `semantic_alignment` / `alignment_corridor_refine` / `alignment_anchor_validation` / `edition_folio_anchors`。`tests/test_core_without_alignment.py` 用 `MetaPathFinder` 禁 import `numpy`/`fastembed`/`onnxruntime`,冷启 HTTP 后端仍能搜索 + 读取已存对齐 + 优雅退出(2026-09-14 实跑通过)——佐证核心路径不依赖计算栈。
- `pymupdf`(44 MiB)是导入期 PDF 解析核心依赖(下游只读不重解析,红线2),**不是**可移除候选,须留主包。
- 候选测算不等于承诺;阶段2 须在干净环境分别构建主程序 / sidecar / 计算组件,实测计算依赖是否被间接打回主包。

## 5. 已测 / 未测

- **已测**:macOS arm64 ZIP(SHA 固定)解包后的安装体积、主应用 Frameworks 依赖分布、sidecar 内部条目(只读)。
- **未测(不外推)**:macOS x86_64 安装体积与依赖分布;Windows 全部;DMG 内部(只记字节);sidecar onefile 各条目的运行时自解压落盘体积。
