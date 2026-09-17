# 阶段2E MCP sidecar 精简 — 依赖归属与体积实证报告

- 日期:2026-09-16
- 范围:接 2C §6 预告的「另立主题」。MCP sidecar(`MEFinderMCP`,入口 `mefinder_mcp.py` → `src.me_finder.mcp_server`)此前与桌面主包**各自重复携带整套数值栈**;本轮确认 sidecar 只读已有对齐、不做对齐计算,故把 `ALIGNMENT_COMPUTE_STACK` 从 `packaging/mcp_sidecar.spec` 排除,并用运行时禁栈测试钉死读链。
- 结论(macOS ARM 定向构建):sidecar 单可执行由 **77.9 MiB → 45.9 MiB**,净减 **32.0 MiB(−41%)**;slim 产物中 `site-packages/{numpy,onnxruntime,fastembed}/` 字符串**残留 0**;`MEFinderMCP --help` 正常启动;读已有对齐的完整调用链在**禁用 numpy/fastembed/onnxruntime 的解释器**下全绿。
- **关键定性**:此 32 MiB 是**独立于** 2C 桌面主包(206.4→100.8 MiB)的收益——sidecar 是分发包里**第二份**数值栈。桌面主包缩小的数字不能当作整个分发包的收益;整分发收益须把 sidecar 这份单独计入。
- **未验证(不外推)**:Windows / macOS Intel 冻结 sidecar 的构建体积与冒烟;`build_macos.sh` 发布级签名/公证后的 sidecar。见 §4。

## 1. 依赖归属:为什么 sidecar 不需要计算栈

`mcp_server` 的全部工具经 `LiteratureVerificationService` 分发。对齐相关工具(`find_parallel_passages`、`list_alignment_corrections`、`propose/confirm/revoke_alignment_correction`)按 MCP 契约**只把既有对齐当召回中心读库**,不即时嵌入、不重算(符合「不重解析」红线)。计算栈只在真正的嵌入/单调对齐路径(`text_alignment_coordinator` → `alignment_compute_worker`)用到,而 sidecar 不含该路径。

运行时事实(`.venv-macos312-arm64`,追踪 `sys.modules`):

- `import src.me_finder.mcp_server` 后计算栈被拉入:**无**。
- `import src.me_finder.text_alignment`(含 `AlignmentNotFound / list_alignment_targets / locate_alignment` 读取符号)后计算栈被拉入:**无**。

PyInstaller 会**静态跟随**所有 import(含函数内懒加载),所以仅靠「运行时不导入」不足以让产物不带栈——必须显式 `excludes`。这与 2C 主包排除同理,复用同一 single source of truth `tools/slim_main_package.py::ALIGNMENT_COMPUTE_STACK`。

## 2. before / after 体积(macOS ARM)

同一分支、同一入口,只切换 sidecar spec 的 `excludes` 是否含 `*ALIGNMENT_COMPUTE_STACK`,做 before/after 定向 onefile 构建(专为依赖归属与体积测量,不签名/不公证)。

| 产物 | 大小 |
|---|---|
| `MEFinderMCP` before(未排除,复刻 2E 前) | **77.9 MiB**(81,697,296 B) |
| `MEFinderMCP` after(排除计算栈) | **45.9 MiB**(48,148,448 B) |
| 净减 | **32.0 MiB(−41%)** |

复现命令(解释器 `.venv-macos312-arm64/bin/python`,PyInstaller 6.21.0):

```bash
# after(当前 spec,带排除)
PYTHONUTF8=1 python -m PyInstaller packaging/mcp_sidecar.spec \
  --distpath <scratch>/dist-slim --workpath <scratch>/build-slim -y --clean
# before:临时移除 spec 里 "from tools.slim_main_package import ALIGNMENT_COMPUTE_STACK"
#         与 excludes 中的 "*ALIGNMENT_COMPUTE_STACK" 后同法构建(对照 spec 已删除,不入库)
```

## 3. slim 产物验证

- `strings dist-slim/MEFinderMCP | grep -iE "site-packages/(numpy|onnxruntime|fastembed)/"` → **无匹配**(计算栈目录未打入)。
- `dist-slim/MEFinderMCP --help` → 正常输出 usage,启动无缺依赖报错。
- 读链禁栈证明:`tests/test_mcp_sidecar_without_alignment.py::MCPSidecarWithoutAlignmentTests` 在装入 `NoCompute` MetaPathFinder(禁 import numpy/fastembed/onnxruntime)的子进程里,用真实 `_call_tool` 跑 `find_parallel_passages`(读持久化跨语言对齐,断言英文对应句逐字一致)、`locate_quote`、`list_alignment_corrections`、`list_documents`,全部成功且断言计算栈从未进入 `sys.modules`。
- spec 钉死:`tests/test_mcp_sidecar_without_alignment.py::SidecarSpecWiringTests` 断言 sidecar spec 从 `tools.slim_main_package` 导入并 splat 排除计算栈,防止将来漂移。

## 4. 已测 / 未测

- **已测(macOS ARM)**:before/after 定向构建体积、slim 产物计算栈目录残留=0、`--help` 启动、读链禁栈运行时测试、spec 钉死测试。MCP 工具行为不变由上述读链测试 + 全量 `tests/test_mcp_*` 覆盖。
- **未测(不外推)**:
  - Windows / macOS Intel 的冻结 sidecar 构建体积与冒烟(单机无法产,如实标注 `未测`);
  - `build_macos.sh` 发布级签名/公证后 sidecar 的完整冒烟(发布级构建随切版做)。
- 结论只在 macOS ARM 定向构建成立;跨平台数字待各自平台真机构建后回填,不由本平台外推。

## 2026-09-17 — 补充真实冻结 STDIO 与归档检查

本轮重新构建 macOS ARM sidecar（48,148,288 B），CArchiveReader 检查内嵌 PYZ 与归档条目均无 NumPy / ONNX Runtime / fastembed；经真实 MCP SDK STDIO 调用跨译本查询、引文定位、修正列表、文献列表全部通过。此证据补充此前 strings / --help / 禁栈解释器测试；Windows / Intel 不由此推断。详见 [第二阶段审核报告](alignment-phase2-review-2026-09-17.md)。
