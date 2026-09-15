# 阶段2C 主包精简 — 依赖归属与体积实证报告

- 日期：2026-09-15
- 主机：macOS 24.6.0 / Apple Silicon(arm64);解释器 `.venv-macos312-arm64/bin/python`(Python 3.12,PyInstaller 6.21)
- 结论:桌面主包排除对齐计算数值栈后,`MEFinder.app`(定向 PyInstaller 构建,不含 MCP sidecar)由 **206.4 MiB → 100.8 MiB**,净减 **105.6 MiB(−51%)**;`Frameworks/` 数值栈残留 **0**;新增纯 Python worker 源 3.22 MiB(已计入 100.8)。外部解释器可从产物 `_MEIPASS/me_finder` 导入 worker 并完成协议握手。
- 口径:逻辑大小(`stat -f%z` 求和),与 `reports/package-size-inventory-v0.5.4-2026-09-14.md` 的"逻辑大小"口径一致,可对比。
- **未验证(不外推)**:Windows / macOS Intel 冻结产物;完整 `build_macos.sh` 发布级 ZIP/DMG/签名/sidecar;真机 uv 安装独立运行时后的正向计算闭环。见 §5。

## 0. 被测对象与方法

同一分支、同一 staging、同一命令,只切换 spec 的**排除 + worker 源交付**两处,做 before/after 定向构建(仅 `desktop_macos.spec`,不建 sidecar、不签名、不打 DMG——**专为依赖归属与体积测量**,发布级构建随切版做)。

- before spec = `git show HEAD:packaging/desktop_macos.spec`(2C 改动前,无排除、无 worker 源交付)。
- after spec = 工作区 `packaging/desktop_macos.spec`(引入 `tools/slim_main_package.py` 的 `ALIGNMENT_COMPUTE_STACK` 与 `worker_source_datas`)。

### 复现命令

```bash
# 1) 最小 staging(等价 build_macos.sh 的 staging 段)
STAGE=build/macos-stage; rm -rf "$STAGE"; mkdir -p "$STAGE/data" "$STAGE/config"
.venv-macos312-arm64/bin/python -m tools.create_empty_index "$STAGE/data/index.sqlite3"
cp config/pdf_imports.empty.json "$STAGE/config/pdf_imports.json"
cp config/mineru_api.local.example.json "$STAGE/config/mineru_api.local.example.json"
PYLIC=$(.venv-macos312-arm64/bin/python -c 'from pathlib import Path;import sysconfig;print(Path(sysconfig.get_path("stdlib"))/"LICENSE.txt")')
cp "$PYLIC" "$STAGE/Python-runtime-LICENSE.txt"
ICSET="$STAGE/MEFinder.iconset"; mkdir -p "$ICSET"
for S in 16 32 128 256 512; do
  sips -s format png -z $S $S assets/app_icon.svg --out "$ICSET/icon_${S}x${S}.png"
  R=$((S*2)); sips -s format png -z $R $R assets/app_icon.svg --out "$ICSET/icon_${S}x${S}@2x.png"
done
iconutil -c icns "$ICSET" -o "$STAGE/app_icon.icns"

# 2) before / after 定向构建(同 staging)
git show HEAD:packaging/desktop_macos.spec > /tmp/desktop_macos_before.spec
PYTHONUTF8=1 MEFINDER_APP_VERSION=0.5.5 MEFINDER_TARGET_ARCH=arm64 \
  .venv-macos312-arm64/bin/python -m PyInstaller /tmp/desktop_macos_before.spec \
  --clean --noconfirm --distpath /tmp/before-dist --workpath /tmp/before-work
PYTHONUTF8=1 MEFINDER_APP_VERSION=0.5.5 MEFINDER_TARGET_ARCH=arm64 \
  .venv-macos312-arm64/bin/python -m PyInstaller packaging/desktop_macos.spec \
  --clean --noconfirm --distpath /tmp/after-dist --workpath /tmp/after-work

# 3) 体积
size(){ find "$1" -type f -exec stat -f%z {} + | awk '{s+=$1} END{printf "%.1f MiB\n",s/1048576}'; }
size /tmp/before-dist/MEFinder.app   # 206.4 MiB
size /tmp/after-dist/MEFinder.app    # 100.8 MiB
```

## 1. before / after 体积

| 产物(定向构建,不含 sidecar) | 逻辑大小 |
|---|---|
| `MEFinder.app` before(含数值栈) | **206.4 MiB** |
| `MEFinder.app` after(精简) | **100.8 MiB** |
| 净减 | **105.6 MiB(−51%)** |

## 2. 被移除的数值栈依赖归属(before `Contents/Frameworks/`)

| 依赖 | MiB | 归属 |
|---|---|---|
| onnxruntime | 70.7 | 对齐计算(嵌入推理) |
| tokenizers | 8.1 | 嵌入分词 |
| hf_xet | 7.5 | HuggingFace 模型下载加速 |
| numpy | 6.9 | 对齐计算 |
| py_rust_stemmers | 0.6 | fastembed 依赖 |
| **五者小计** | **93.9** | — |

净减(105.6)高于此五者(93.9),差额 ≈ fastembed 纯 Python 层 + huggingface_hub + onnxruntime 传递重依赖(sympy / mpmath / flatbuffers / coloredlogs / humanfriendly,均 `src/me_finder` 零引用)在 after 一并移除,减去新增 3.22 MiB worker 源。**after `Frameworks/` 中上述包残留为 0**(实测无残留)。

## 3. 新增:纯 Python worker 源交付

- after `Contents/Resources/me_finder/`:208 个 `.py`,**3.22 MiB**;`Contents/Frameworks/me_finder → ../Resources/me_finder` 符号链接,故 `sys._MEIPASS/me_finder` 正确解析。
- datas 不被 PyInstaller `Analysis` 分析,故交付源**不会**把被排除的数值栈打回主图(after 残留为 0 佐证)。

## 4. 冻结态 worker 源交付 —— 外部解释器导入实证

对 **after 真实构建产物**,用纯净环境的外部解释器(非冻结 exe)从 bundle 的 `_MEIPASS` 导入并探针:

```bash
MEIPASS=/tmp/after-dist/MEFinder.app/Contents/Frameworks
CTRL=/tmp/probe.ndjson; : > "$CTRL"
env -i PATH=/usr/bin:/bin PYTHONPATH="$MEIPASS" \
  .venv-macos312-arm64/bin/python -m me_finder.alignment_compute_worker --probe "$CTRL"
# EXIT=0
cat "$CTRL"
# {"type":"hello","protocol":1,"capabilities":{"numpy":true,"fastembed":true,"onnxruntime":true},"pid":...}
```

证明:被排除数值栈的主包,其交付源仍可被**独立解释器**以顶层 `me_finder` 包导入并完成 `ALIGNMENT_COMPUTE_PROTOCOL` 握手——即独立运行时 venv 将走的真实路径(此解释器带栈,故 capabilities 为真;**无栈**解释器由安装/`--verify` 路径与 `test_core_without_alignment` 覆盖)。

## 5. 已测 / 未测

- **已测(macOS ARM)**:before/after 定向构建体积、Frameworks 数值栈残留=0、worker 源交付与外部解释器导入+探针、`tests/test_slim_main_package`(装配不变量 + 导入边界 + 交付源可运行 + 模型下载守卫)、`tests/test_core_without_alignment`(禁栈冷启 HTTP)。
- **未测(不外推)**:
  - Windows onedir(datas 落 `_internal/me_finder`)与 macOS Intel 的 `_MEIPASS/me_finder` 解析、进程回收、路径语义;
  - 完整 `build_macos.sh` 发布级产物(ZIP/DMG/签名/sidecar)与全量门禁在本轮改动上的绿(定向构建已证精简与源交付成立;发布级冒烟随切版做);
  - 真机 uv 安装独立运行时后**装真栈** probe 通过 + 最小计算逐位一致、pin 可解析/离线加载(2B 遗留)。

## 6. sidecar(本轮不做,记录佐证)

`mcp_server` 只依赖 `LiteratureVerificationService`;`find_parallel_passages` 按契约"只把既有对齐当召回中心"读库,不即时嵌入。sidecar 内同样重复携带整套数值栈(v0.5.4 盘点:onnxruntime 19.5↓/70.8↑ 等),很可能可整体移除,但需传递依赖确认 + MCP 端到端回归,**另立主题**。
