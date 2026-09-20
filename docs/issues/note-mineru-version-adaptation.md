# MinerU 版本与接口自适应

## 2026-09-20：把「写死版本」拆成两件事(接口代数 / 安装版本)

### 事实(实测)

- MinerU 4.x 换掉了整套 HTTP 契约。4.0.4 wheel 里 `mineru/parser/api_server.py:1575` 是
  `APIRouter(prefix="/v1")`,路由为 `/v1/health`、`/v1/models`、`/v1/tiers`、`/v1/uploads`、
  `/v1/parse/jobs`、`/v1/files/{id}/content`;**整个包 grep 不到 `"/tasks"`**。
- 4.x 的 `POST /v1/parse/jobs` 收 JSON body(`CreateJobRequest`),文件先经
  `POST /v1/uploads` → `PUT /v1/uploads/{id}/content` → `POST /v1/uploads/{id}/complete`
  换成 `file_id`;3.x 的 multipart 直投任务端点不再存在。上传注册时带 `sha256sum`,
  服务端命中同哈希会直接返回 `file`,可跳过传字节。
- API key 可选:`application.state.api_key` 未设置时中间件不校验;设置后要
  `Authorization: Bearer <key>`。
- 4.x 的输出工件只有 `markdown` / `middle_json` / `structured_content`(+html/latex/docx/zip),
  **HTTP 层不再暴露 content_list v1**。`structured_content` 由 docvortex 渲染,形状是
  `{"pages": [{"page_idx": N, "blocks": [{type, bbox?, content, captions?, footnotes?, level?}]}], ...}`,
  每个块的正文是一段渲染好的 Markdown 字符串。
- **坐标系变了**:docvortex 的 `bbox` 是归一化 `0..1`(`docvortex/schema.py:468` 起校验
  `0.0 <= v <= 1.0`),页对象不带宽高;3.x content_list 用的是 1000 单位画布。
- PyPI 元数据:4.0.4 的 `provides_extra` 只有 `dev/test/torch/full/all`,3.x 的
  `pipeline`/`core` 已不存在。`TierDependencyError` 的提示给出映射:裸包 = ONNX/llama.cpp,
  `[torch]` = Torch,`[full]` = vLLM/LMDeploy,MLX 另需 `mlx-vlm>=0.7.0,<0.8.0`。
- 两代的 console scripts 都有 `mineru-api` 与 `mineru-models-download`,但
  下载参数从 `-m <model_type>` 变成 `--tier <basic|standard>`;配置机制从
  `MINERU_TOOLS_CONFIG_JSON`(JSON)变成 `MINERU_HOME` 下的 `config.yaml`(YAML)。
- 档位映射(`mineru/parser/tier.py`):`flash`/`basic`/`standard`/`advanced` 对应 hybrid effort
  `flash`/`medium`/`high`/`xhigh`。

### 结论

「写死 3.4.x」其实是两个独立问题,必须分开解:

1. **接口代数**不能靠版本号猜,要握手探测。provider 先探 `/v1/health`,404/405 再探
   `/health`,据此选 `v1-jobs` 或 `tasks` 适配器;显式锁定时不回落(错了要报错,不能静默降级)。
   用户自行升级本地 MinerU 时,应用自动适配,不需要发新版。
2. **安装版本**不该是单个常量,而是「兼容区间 + 按大版本系列的安装配方」。清单声明
   `auto_upgrade: {minimum, below, metadata_url}` 与 `series: {"3": {...}, "4": {...}}`,
   安装时从 PyPI 取区间内最新版本并按系列组装 requirement 列表;PyPI 不可达即回落清单固定版本。
   区间外的大版本必须显式报「缺少安装配方」,不能装上去再炸。

不做「无脑装 latest」:上游一次改名(extras)+改路由就会让每个用户在自己机器上炸,且版本各不相同、无法复现。

### 推断(待验证)

- 4.x 托管安装的 extras 映射(pipeline→裸包、vlm→`[torch]`+`mlx-vlm` / `[full]`)依据的是
  PyPI extras 清单与 `TierDependencyError` 的提示文本,**尚未在本机实跑过一次 4.x 托管安装**。
  首次真机安装需复核模型下载参数与 `MINERU_HOME` 下的模型落盘路径。
- 4.x 的 `middle_json` 是 `MineruResult.to_dict()`(严格 MiddleJson),与 3.x 的 middle.json 不是同一
  结构;当前通道只消费 `structured_content`,未依赖 middle_json。

### 引用定位影响

归一化坐标按 1000 画布换算后入库(与既有文献同坐标系),同时把精确归一化值带到
`bbox_normalized`,经解析任务桥接进 `content_list.json` 与入库块;`auto_page_mapping` 与
`search_anchors` 本就优先读 `bbox_normalized`,因此 4.x 的页码识别不依赖换算取整值。

## 2026-09-20 晚：外部审计四项修复(c4acc61 → 本轮)

审计对象 `c4acc61`，四项均已复现并修复。

### 事实

1. **已安装版本与安装目标混用**(P1)。`_installed()` 与 `_runtime_environment()` 原本按**当前 manifest** 的 `config_style` 判断，而不是按该组件自己的安装回执：装了 4.x 后重建管理器(manifest 回到清单固定的 3.4.5)会去找 4.x 从不写入的 `mineru.json`，组件被误判成未安装；反之只要执行过「检查新版本」切到 4.x，尚未升级的 3.x 组件就会被塞 `MINERU_HOME` 启动。
   → 回执新增 `config_style`(并保留 `series` 作回退)，识别与启动一律读回执；无 `series` 的旧回执按 3.x 处理。
2. **检查更新可在安装中途换配方**(P1)。`refresh_available_version()` 不受操作锁约束就替换 `self.manifest`，而安装流程在多个阶段继续读它 → 回执可能写成混版。
   → 操作开始时冻结整份 manifest 传给 worker，安装全程只用这一份；有操作在途时「检查新版本」直接拒绝并说明原因。
3. **普通安装没接版本解析**(P2)。只有 `check-updates` 才访问 PyPI，`install`/`update` 仍以清单固定版本为目标，与 release notes 的说法不符；前端也没有任何入口发 `check-updates`。
   → `install`/`update` 在 **worker 线程**(不卡 HTTP 请求，且此时 `state.operation` 已置位，不会与 check-updates 抢)先解析一次目标版本；设置页补「检查新版本」按钮与「安装目标 … (兼容区间内最新/清单固定版本)」说明。
4. **把 Markdown 当原文入库**(P2)。`structured_content` 的 `content` 是渲染后的 Markdown，原实现直接拼接入库 → 「劳动是`**`价值`**`的实体」按无标记原句精确匹配失配，引文也会带出标记。
   → 新增 `plain_text_from_markdown()`：先把上游转义过的字面字符(`\\*` `\\_` `` \\` `` `\\~` `\\$` `\\#` …)藏入哨兵，再去掉 HTML 包装(`<strong>/<u>/<sup>/<s>/<br>/&nbsp;`)、图片、链接(保留标签文字)、行内代码 fence、`$…$` 与 `\\(…\\)` 公式界定符、`**`/`*`/`***`/`~~` 强调、列表与标题标记、表格竖线与分隔行，最后还原哨兵。依据是 docvortex 的 `_SIMPLE_STYLE_WRAPPERS`、`_apply_html_styles`、`escape_conservative_markdown_text` 三处实现，不是猜。

### 仍未验证

- 真实 MinerU 4.x 端到端解析(通道 + 托管安装)。本轮把测试假服务改成**只暴露 `/v1/health`** 的 4.x 形态，托管安装—模型下载—启动—协议探测整条链已在假服务上走通，但仍不等于真实 4.x。
- `c4acc61` 的 CI 是**已取消**，不是通过。
