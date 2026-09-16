# 阶段2C —— 主包精简与译本对齐组件闭环

- 日期：2026-09-15
- 目标版本：**0.5.5**(组件化版本；`src/me_finder/__init__.py` `__version__` 已提升至 0.5.5,连带 `test_mcp_v1_baseline`、`test_http_api_contract`+新契约 `docs/contracts/v0.5.5-http-api.json`、`test_mcp_packaging`+`windows-release-smoke.yml` 一并同步;各平台构建产物由此命名为 `v0.5.5`,无需再传 `MEFINDER_APP_VERSION`/`-Version`)
- 范围：把 2B 的"可选独立对齐运行时"推进到**主程序不携带数值栈**——`numpy` / `onnxruntime` / `fastembed` 及其重传递依赖不再打进桌面主包;计算与模型探针只在独立解释器里跑。本轮**做**:主包精简闭环(spec 排除 + 冻结态 worker 源交付 + 导入边界钉死 + 模型下载精简守卫 + before/after 体积实测)。本轮**不做**(单列 §6):设置页安装/升级/卸载**大按钮 UI**、sidecar 精简、Windows / macOS Intel 冻结验证。
- 不变量:算法、阈值、缓存版本、成果格式、worker 协议(`ALIGNMENT_COMPUTE_PROTOCOL=1`)、错误码与取消语义**均未改动**;已有数据库、文献、对齐结果与引用定位信息保留,不重解析、不引入联网必需路径。

## 1. 根因:数值栈为何仍在主包(基线审计)

PyInstaller 的 `Analysis` **静态跟随 import**。主包 `hiddenimports` 里的 `alignment_compute` / `alignment_kernel` / `alignment_compute_worker` 把 `semantic_alignment` / `text_alignment` / `edition_folio_anchors` 拉进主图,于是 numpy / onnxruntime / fastembed / tokenizers / hf_xet(v0.5.4 实测 ≈94 MiB)被打进 `Frameworks/`。**惰性(函数内)导入不改变这一点**——PyInstaller 扫字节码,函数内 `import numpy` 一样会被打包。唯一有效手段是 spec `excludes`。

### 1a. 更正既有报告一处事实

`reports/package-size-inventory-v0.5.4-2026-09-14.md` §4 称"numpy 由 5 个模块 import,全部函数内 lazy"。AST 复核(见 §3 复现命令):
- 3 处在 `if TYPE_CHECKING:` 守卫内(`alignment_anchor_validation` / `edition_folio_anchors` / `semantic_alignment`),运行时不执行,PyInstaller 也不跟随;
- **`alignment_corridor_refine.py:23` 是真·模块级 `import numpy as np`**——但该模块在 `src/me_finder` 内**零引用**(D 实验遗留,仅 `scripts/` 与实验测试使用),不在主图,也不在核心路径。

结论不变(排除数值栈安全),但"全部 lazy"的表述需以上修正。

## 2. 实现

### 2a. 单一真相:`tools/slim_main_package.py`

新增共享模块,承载两处**不可漂移**的清单(历史教训:跨平台打包清单在两份 spec 里各写一份必然漂移):
- `ALIGNMENT_COMPUTE_STACK`:排除清单 = 计算核心(numpy / onnxruntime / fastembed)+ 其只被 ONNX Runtime/fastembed 拉入、`src/me_finder` 零引用的重传递依赖(tokenizers / huggingface_hub / hf_xet / py_rust_stemmers / sympy / mpmath / flatbuffers / coloredlogs / humanfriendly)。
- `worker_source_datas(project_root)`:把 `src/me_finder/**/*.py`(去 `__pycache__`)重映射为顶层 `me_finder/` 树,作为 **datas** 交付(datas 不被 Analysis 分析,故绝不把被排除的栈打回主图)。

两份 spec(`packaging/desktop_macos.spec`、`packaging/desktop.spec`)都 `from tools.slim_main_package import ...` 并把 `*ALIGNMENT_COMPUTE_STACK` splat 进 `excludes`、`*worker_source_datas(...)` splat 进 `datas`。

### 2b. 冻结态 worker 源交付(2B 遗留缺口)

冻结主包把 `me_finder` 编译进 PYZ 字节码,**独立 venv 的解释器无法从 PYZ import**。故必须把纯 Python 源作为 datas 落盘。`default_worker_context()` 冻结分支改为返回 `("me_finder.alignment_compute_worker", Path(sys._MEIPASS))`——`sys._MEIPASS` 是各平台 datas 的落点(macOS BUNDLE 上 `Contents/Frameworks/me_finder → ../Resources/me_finder` 符号链接,`_MEIPASS/me_finder` 正确解析)。外部解释器以 `PYTHONPATH=_MEIPASS` 运行 `python -m me_finder.alignment_compute_worker`。

**实证(真实构建产物,见 §4)**:纯净环境的外部解释器从 bundle 的 `_MEIPASS/me_finder` 导入 worker 并完成 `--probe` 协议握手,退出 0。

### 2c. 精简主包下的模型下载守卫

`make_model_downloader` 未装独立运行时时原本回退**进程内** `download_embedding_model`。精简主包进程内无 fastembed,该回退会抛 `ImportError`。改为:未装运行时且 `_builtin_stack_present()` 为假 → 抛明确可操作原因(`对齐计算运行时未安装:请先安装对齐计算组件后再下载模型`),**不用"捕获所有异常后回退"掩盖**;自带栈(开发态)仍走进程内回退。

### 2d. 诚实不可用语义

精简冻结包 `_builtin_stack_present()` 恒为假。未装独立运行时时:`compute_status` → `{available:false, provider:"none"}`;生成对照经 worker `--probe` → `COMPONENT_MISSING` → `TextAlignmentComponentUnavailable`(具体、可显示,**绝不静默退回进程内计算**)。设置页大按钮**安装入口**留 §6 下一轮(端点 `/api/text-alignment/runtime` 2B 已就位)。

## 3. 复现命令(审计与守卫)

```bash
# 模块级 vs 函数内数值栈导入边界(AST)
.venv-macos312-arm64/bin/python - <<'PY'
import ast, pathlib
STACK={"numpy","onnxruntime","fastembed","tokenizers","hf_xet","huggingface_hub","py_rust_stemmers"}
# ...(见 reports/alignment-compute-runtime-2c-2026-09-15.md 完整脚本)
PY

# 主进程路径模块在禁栈下可导入 + 装配不变量
.venv-macos312-arm64/bin/python -m unittest tests.test_slim_main_package
.venv-macos312-arm64/bin/python -m unittest tests.test_core_without_alignment

# 定向 before/after 体积构建(见 §4 与报告)
```

## 4. 体积证据

见 `reports/alignment-compute-runtime-2c-2026-09-15.md`(依赖归属、构建命令、before/after `.app` 体积、Frameworks 数值栈残留=0、外部解释器导入实证、未验证平台)。

## 5. 测试

- 新增 `tests/test_slim_main_package.py`:两份 spec 排除栈 + 交付 worker 源;`worker_source_datas` 顶层 `me_finder` 树/保留子包/去字节码;主进程路径模块禁栈可导入;**交付源树可被外部解释器以顶层 `me_finder` 导入并 `--probe`**;`default_worker_context` 冻结走 `_MEIPASS`;精简下模型下载守卫两态。
- 既有 `tests/test_core_without_alignment.py`:冷启 HTTP 后端禁 numpy/fastembed/onnxruntime 仍能搜索 + 读已存对齐 + 生成明确失败 + 优雅退出。
- 运行器 unittest(非 pytest);Ruff 仅 `["F"]`,零新增告警。

## 6. 未完成 / 待核实(不声称完成)

- 设置页安装/升级/卸载**大按钮 UI**(镜像 mineru-local;守指纹/全局预算/主题快照)——下一轮。
- **sidecar 数值栈精简**:`mcp_server` 只依赖 `LiteratureVerificationService`,`find_parallel_passages` 按契约读既有对齐;很可能整套栈可从 sidecar 移除,另立主题(需传递依赖确认 + MCP 端到端回归)。
- **Windows / macOS Intel 冻结冒烟**:本轮仅 macOS ARM 定向构建实测;`_MEIPASS/me_finder` 在 Windows onedir(`_internal/me_finder`)的解析、进程回收与路径待核实。
- **真机 uv 安装独立运行时后的正向计算闭环**(2B 遗留):装真栈后 probe 通过 + 最小计算逐位一致、pin 可解析/离线加载;仍待网络/真机。
- **完整 `build_macos.sh` 发布产物 + 全量门禁在本轮改动上的绿**:见报告;定向构建证明精简与源交付成立,发布级冒烟随切版做。

## 2026-09-16 — 补齐精简包依赖的运行时修复

已在临时目录真实安装独立运行时；使用随包源码布局完成离线结果等价验证，并在禁止导入数值栈的 ApplicationRuntime 中生成、发布 8 条完整等价链接。旧缓存缺少对齐定义时改用内置对齐清单，不再因精简包无栈而无法安装。详见 [审计修复报告](../../reports/alignment-runtime-review-fixes-2026-09-16.md)。本轮不重建正式分发包，不扩展 UI 或 sidecar 主题。
