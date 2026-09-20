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
