# 可重复性能基线

2026-09-10：建立 0.5.4 后端性能测量协议，支持后续优化前后比较；本协议不设跨机器通用的耗时门槛。

## 运行

在仓库根目录使用安装了项目依赖的 Python 3.12 venv。测量额外依赖只有 `psutil==7.2.2`，不加入产品依赖。
本机解释器为 `/Users/mercury/文献原句定位器/.venv-macos312-arm64/bin/python`；Windows 使用 `.venv-windows/Scripts/python.exe`。

```bash
# 将下列解释器替换为本机项目 venv 的绝对路径。
<venv-python> -m pip install psutil==7.2.2
<venv-python> scripts/bench_responsiveness.py \
  --models "<已有 MiniLM 模型缓存根目录>" \
  --output reports/performance-before.json

# 优化后，同一台机器、相同解释器/模型/参数，再运行；输出包含逐查询比值。
<venv-python> scripts/bench_responsiveness.py \
  --models "<同一模型缓存根目录>" \
  --compare reports/performance-before.json \
  --output reports/performance-after.json

# 小型检查可只运行普通搜索；不能将其称为完整性能基线。
<venv-python> scripts/bench_responsiveness.py \
  --documents 2 --paragraphs 12 --alignment-paragraphs 8 \
  --rounds 1 --repeats 1 --scenario normal --output /tmp/performance-smoke.json
```

模型根目录应包含 `models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`。
macOS 常见位置为 `~/Library/Application Support/MEFinder/runtime/components/text-alignment/models`。
脚本只读取并复制该模型目录，不复制用户文献向量、不修改原模型缓存；被测进程拒绝外部网络连接。
模型缺失或不完整时明确失败，不下载、不用假向量代替实际计算。
输出文件已存在时拒绝覆盖。临时语料、数据库、模型副本与导出文件在运行后自动清理。

## 固定测试集

`scripts/performance_fixture.py` 定义版本化生成器、固定随机种子 `20260910` 和八条查询。
默认生成 32 部检索文献，每部 2,000 段，另有中英 EPUB 两部，各 320 段；总计 34 部、64,640 段、5,500 个 PDF 页面。
通过真实 `build_database` 建立当前 schema 与 FTS；PDF 段落携带页标识和字符区间。合成 EPUB 不虚构出版方页码。
不解析源文件、不使用生产库，不携带私人文献。生成前后的语料哈希、规模、schema、库大小与查询参数写入 JSON。

| 查询 ID | 覆盖路径 |
|---|---|
| zh_exact | 唯一中文原句，精确匹配 |
| script_variant | 繁体查询命中简体原文，繁简统一开启 |
| en_exact | 唯一英文原句，精确匹配 |
| common_zh | 高频“社会”，auto，limit 10 |
| common_en | 高频英文短语，auto，limit 10 |
| normalized | 查询额外加入标点，auto 逐级检索 |
| no_hit | 确定无命中的精确查询 |
| scoped | “社会”限定 PDF 格式 |

该语料用于稳定复现资源竞争，不用于推断真实文学/哲学文献的对齐准确率。即使对齐产生 accepted 链接，也不代表经过人工质量确认。

## 场景与计时

每个场景每轮都复制未运行过的夹具并启动新进程。默认 3 轮，轮间轮换场景顺序。

- `normal`：仅搜索。
- `export_markdown`：真实 `/api/document/export-markdown` 连续导出 `bench-000` 的 500 页，同时搜索。
- `export_epub`：同一本书，经真实 `/api/document/export-epub` 连续导出，同时搜索。
- `alignment`：真实后台 start/status 协议、默认 MiniLM 及完整生成/写入路径；首次没有文献向量缓存，后续 `force=true` 可复用向量、重新生成链接。每次任务的时间和结果计数单独记录。

搜索计时覆盖 localhost HTTP 建连、请求、后端处理、响应读取和 JSON 解码。
一个顺序搜索客户端，请求开始间隔至少 100ms，前一个请求较慢时等待它完成；后台至多一个任务。
每条查询预热一次，正式采样至少 5 次；若首个后台任务尚未结束，继续整轮八条查询，直到覆盖首个任务完成。
结束采样后等待在途后台任务自然完成，再对八条查询检查恢复及结果一致性，最后退出。
这是一种有请求速率上限的交互负载，不是多客户端吞吐压测，也不测带任务强制退出。

后台计时探针包装真实导出函数和对齐函数，不替换计算、插入延迟或修改生产代码。
仅当搜索起止区间与服务端实际工作区间相交，才纳入“任务期间”的统计；排队或任务间隙请求保留在原始数据中。
时间来自同机 `perf_counter` 单调时钟，保存为相对子进程创建的秒数。

## 指标与正确性

- 成功请求延迟：按场景和查询分别报告 n、p50、p95、p99、max。采用 nearest-rank 定义；小样本的 p99 通常等于最大值，不能当作稳定尾延迟估计。
- 可用性：单独保留 HTTP 状态分布、失败率及失败延迟。快速返回 503 绝不计入成功延迟，也不隐去。
- 结果一致性：固定命中数量、顺序、段落文本、匹配字符区间、页内锚点与页码状态的摘要；比较每次成功搜索与本轮预热结果，并核对各轮及各场景的预热摘要。
- 内存：父进程每 50ms 采样被测进程 RSS 及子进程 RSS 总和，保留原始样本；同时取得操作系统进程 RSS 高水位（macOS/Linux `ru_maxrss`、Windows peak working set）。采样峰值可能漏掉更短的尖峰；子进程 RSS 相加可能重复计算共享页。两种口径分别标记，不能混为实际物理内存总量。
- 启动：从创建 Python 子进程前，到真实后端初始化、HTTP 服务启动且首条确定有命中的搜索完整返回。包含解释器与模块加载；夹具生成、数据库及模型复制不计入。
- 退出：空闲状态发送退出命令，到后端执行 begin_shutdown、停止 HTTP、等待持久任务、关闭数据库并且进程实际退出。必须退出码 0，不以“清理完成”日志代替退出。服务循环默认 500ms 轮询会影响这个数值。

`valid=true` 表示测量覆盖、夹具和结果一致性检查通过；不表示产品没有 503。HTTP 错误是基线需要保存的产品表现。
工作任务失败、无法正常启动/退出、成功搜索结果改变、无实际重叠或任务后无法恢复，不能作为合格比较依据。
`--compare` 会拒绝协议、参数、语料、模型、环境不同以及无效的基线；逐条查询比较成功延迟，同时比较失败率和内存、生命周期指标。

## 解释边界

本轮覆盖桌面与 Web 共用的后端，未测原生窗口首次显示、WebView 绘制、PyInstaller 解包、安装包启动或有任务时退出。
每轮为新进程及新向量缓存，但操作系统文件缓存未清空；因此不称为磁盘冷启动。
生成数据的重复度、候选分布和布局复杂度与真实库不同，不能直接套用到生产库，也不能与既有 Windows 真实库报告横比绝对耗时。
优化前后应在同机、相同电源与负载状态运行；不要同时跑全量测试、构建、其他模型计算。
若要修改测试集或请求节奏，建立新的协议基线，不将两种负载的数值解释成优化收益。

机制测试：`<venv-python> -B -m unittest tests.test_performance_baseline`。CI 只验证测量逻辑与夹具正确性，模型基准需按上面命令显式执行。

## Windows 同步真实库（独立协议）

2026-09-11：新增 `scripts/bench_real_library.py`。保留上面的合成协议；真实书库结果不能与合成数据直接计算优化收益。

先用 SQLite 在线备份从已有库只读取得独立快照，包含已提交的 WAL 数据。只复制索引，不读取原库凭据、不复制源文件或向量缓存；导出和对齐读取已入库内容。快照与查询清单属于私人资料，应放在本机忽略目录，不能提交仓库。

```bash
# 首次准备；所有 ID 从本机已有库选择。目录必须尚不存在。
<venv-python> scripts/bench_real_library.py \
  --source-library "<同步完成的 MEFinder 文件夹>" \
  --snapshot "<本机私人快照目录>" \
  --export-source "<导出文献 ID>" \
  --group "<已有作品组 ID>" --pivot "<基准文献 ID>" --target "<中文目标文献 ID>" \
  --english-source "<英文查询来源 ID>"

# 三轮、每条至少五次，四种场景；实际任务期间的请求才计入并发统计。
<venv-python> scripts/bench_real_library.py \
  --snapshot "<同一私人快照目录>" --models "<已有 MiniLM 模型缓存根目录>" \
  --output reports/performance-real-before.json

# 后续优化使用同一快照、模型、解释器及参数。
<venv-python> scripts/bench_real_library.py \
  --snapshot "<同一私人快照目录>" --models "<同一模型缓存根目录>" \
  --compare reports/performance-real-before.json --output reports/performance-real-after.json
```

真实查询集固定为：中文正文片段、繁体“社會”、英文正文片段、高频“社会”、英文“gender”、增加标点的中文片段、确定无命中字符串、限定 PDF 的“社会”。中英文片段从指定文献第 10 段之后、长度超过 200 字符的首个可检索规范化正文中确定抽取；实际查询在首次准备时冻结。预检要求命中条件、字符区间和 PDF 页内锚点全部有效，失败时修正测试集并重新建立基线，不修改原库来满足测试。

每轮从同一快照复制数据库，保留其中已有对齐历史；通过 `force=true` 重新计算指定版本对。模型文件复制到临时目录、文献向量缓存为空，首轮计算真实嵌入。禁止外部网络。原始测量 JSON 仅含查询摘要、数量和计时，不含书籍正文、标题、原库路径或 API 配置。`configuration.manifest_sha256` 冻结工作负载，`fixture.content_sha256` 冻结数据库；组合驱动哈希同时涵盖真实库脚本和 HTTP 测量脚本。

启动/退出仍是 Python 后端生命周期，不包含 macOS 启动动画、WebView 首屏或打包程序的解压时间。指标和限制沿用上文。对齐期间若出现 503，应作为产品可用性结果保留，不能混进成功响应时间。

快照复制和查询预热会影响操作系统文件缓存；“新进程/新向量缓存”不等于冷盘或冷机启动。重复测量保留这一准备顺序，不应与重启机器后首次打开应用的耗时混用。
