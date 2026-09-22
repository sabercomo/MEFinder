# 独立对齐运行时安装:国内镜像 + 重试续传

关联:[[note-alignment-compute-runtime-2b]](按需 uv 运行时安装机制)、[[note-alignment-compute-runtime-2c]](主包精简,把数值栈移出主程序)。

## 1. 问题(事实)

主包精简后(2C),"生成译本对齐"从开箱即用变成需要**联网安装独立运行时**。该安装一次串三段境外下载:

1. uv 可执行文件 —— `releases.astral.sh`;
2. 托管 CPython —— `uv venv --managed-python`,实为 GitHub 上的 python-build-standalone;
3. 数值栈依赖(numpy / onnxruntime / fastembed)—— PyPI。

原实现的脆弱点(改动前 `managed_alignment_runtime.py`):

- `_download_file` 只有一次 `urlopen(timeout=30)`,**失败不重试、不断点续传**,单次读超时即整体失败(用户实测报错 `The read operation timed out`,卡在第 ① 步);
- `_install_environment` 只设缓存/安装目录,**没有任何国内镜像**,三段全走境外默认源。

对面向国内文献用户的产品,这条强制联境外网的安装路径与红线 1(本地优先、不引入必须联网的路径)相冲突。本轮不改"按需安装"这一架构决定,只提升其**可达性与鲁棒性**。

## 2. 方案(已实现)

用户 2026-09-22 选定:**自动镜像优先→官方回退,无 UI;清华 TUNA 全套**。

### A. 重试 + 断点续传(`managed_alignment_runtime.py`)

- 每读超时提高到 120s;每个候选 URL 最多重试 `_DOWNLOAD_MAX_ATTEMPTS=4` 次,退避 `(2, 5, 15)s`,退避等待走 `cancel_event.wait` 保持可取消。
- 断点续传:重试同一 URL 时按已落盘字节发 `Range: bytes=N-`;服务器不返回 `206` 则从头重下。
- 网络类异常(`URLError` / `TimeoutError` / `OSError`)在同一 URL 内重试;**大小 / SHA-256 不符**视为该源污染,不在同一 URL 重试,直接换下一候选。每个产物仍逐字节 SHA-256 校验——错误镜像只会拖慢,绝不发布损坏字节。

### B. 国内镜像(清华 TUNA,自动回退)

- uv 二进制:候选列表 = `[TUNA github-release 镜像, 官方]`,镜像仅对官方 host(`releases.astral.sh` / `github.com`)生成,`file://` 与自建清单不触发镜像(测试与私有部署不外连)。
- 托管 Python:`UV_PYTHON_INSTALL_MIRROR` 指向 TUNA 的 python-build-standalone 镜像。
- PyPI 依赖:`UV_DEFAULT_INDEX` 指向 `pypi.tuna.tsinghua.edu.cn/simple`。
- uv 子进程步骤(`uv venv` / `uv pip install`)先用镜像环境跑,失败则清理产物、改用官方源再跑一次(`_run_uv_command`)。
- 两处镜像 env 用 `setdefault`,真实环境里操作者的既有值优先于内置镜像。
- 总开关:环境变量 `MEFINDER_ALIGNMENT_MIRROR=off`(或 `0`/`false`/`no`)强制只走官方源;默认开启,无设置页 UI(不触前端基线门禁)。

## 3. 测试

`tests/test_managed_alignment_runtime.py` 新增:

- `AlignmentDownloadRobustnessTests`:超时后 `Range` 续传;镜像失败回退官方;完整性不符换源且不在同源重试;全部源失败抛聚合错误(含"镜像与官方源")。
- `AlignmentMirrorConfigTests`:`_uv_mirror_url` 官方 host 映射、`file://`/非官方 host 跳过;候选顺序(镜像优先/`off` 仅官方);`_install_environment` 镜像 env 仅在 `use_mirror` 时注入。
- `AlignmentUvStepFallbackTests`:`_run_uv_command` 镜像失败后带 cleanup 回退官方。

均以注入 opener / 假 `_run_command` 驱动,不触真实网络。

## 4. 待核实(不声称完成)

- **镜像 URL 的真机可达性未验证**:TUNA 的 uv / python-build-standalone github-release 路径与 `UV_PYTHON_INSTALL_MIRROR` 拼接、`UV_DEFAULT_INDEX` 对 uv 0.12.1 的实际生效,均**未在真机联网安装中跑通**;设计上镜像错/失效会自动回退官方,故最坏只是退回改动前的境外路径,不会更糟。真机装通后按需订正镜像常量。
- 同样的下载脆弱点也存在于 `managed_mineru.py`(同构的 `_download_file` / `_install_environment`),本轮未改;如要统一,另立主题并考虑抽公共下载层。
