# 阶段 2A 第三轮复审与直接修复

2026-09-14：复审 `da9983e` 后直接修复四处遗漏，范围限于取消、退出、临时文件与错误提示；不推进 2B，不发布安装包。

## 复审依据

- 被审提交：`da9983e797ce685ef494e9408a43fc077f6b710b`；远端 CI [34830924529](https://github.com/sabercomo/MEFinder/actions/runs/34830924529) 已核验成功。这是修复前基线的 CI，不作为本次修改后的验证。
- 前轮四项修复保留：探测进入 durable operation、启动失败清理、模型缓存与可写回执分离、组件错误通过控制器返回 HTTP 503。
- 本次先新增复现测试，在未修产品代码时确认失败，再做最小修复。

## 事实、复现与修复

| 问题 | 修复前复现 | 本次行为与测试 |
| --- | --- | --- |
| 排队请求清除关闭信号 | 当前任务持有 mutation 锁且已收到取消，后来的同步请求在等待锁前清除该信号 | 重置移入 mutation 锁并置于 durable 准入前；准入后才创建 runner。`test_queued_request_does_not_clear_active_shutdown_cancel` 验证取消保持且关闭后的排队请求不创建 runner |
| 未安装模型仍显示解析失败 | 真实 coordinator → controller 返回 HTTP 500 和“检查解析文本” | 归为 `TextAlignmentComponentUnavailable`；`test_uninstalled_model_surfaces_install_hint_from_real_coordinator` 验证 HTTP 503、`component_unavailable` 和下载模型提示 |
| 强制终止后尚未回收进程 | 真实子进程忽略 SIGTERM；发送 SIGKILL 后 `_terminate` 返回时 `process.returncode` 仍为 None | 强制终止后调用 `wait()`；`test_forced_termination_reaps_running_worker` 验证退出码为 SIGKILL。该测试在 Windows 跳过，Windows 不采用 POSIX 信号路径 |
| 清理失败被忽略 | 注入目录删除 PermissionError，调用返回成功 | 清理错误直接传播；`test_failed_temp_cleanup_is_reported` 验证失败不被吞掉 |

另新增 `tests.test_alignment_compute_lifecycle`，用真实 ApplicationRuntime 和后台任务入口驱动真实测试子进程，分别在探测、计算阶段关闭运行时。测试在无模型环境也可执行；只用故障 worker 模拟等待，不伪称验证了数值算法。

该测试同时验证：运行中的任务被 durable gate 计入、关闭成功返回前所有 worker 已回收、请求/结果/控制临时目录已删除、任务状态为取消、数据库无新增 completed 成果。模型计算等价性继续由已有冷缓存、暖缓存与无 NumPy 主进程闭环测试覆盖。

## 本轮验证命令

结果：macOS arm64 全量 unittest **2311 项，23 跳过，0 失败**（123.611 秒）；包含前端装配/预算守卫与真实模型冷暖缓存、无 NumPy 主进程闭环测试。新增的 5 项测试全部通过。Ruff 与 `git diff --check` 通过，lint 的工作区范围说明见下文。

在仓库根目录执行：

```sh
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PYTHONUTF8=1 \
  .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests
.venv-macos312-arm64/bin/python -m ruff check . --extend-exclude .codex-tmp
git diff --check
```

本机 `.codex-tmp/bertalign-upstream` 是既有未跟踪外部代码，裸 `ruff check .` 会报其中 20 项既有告警。本次 lint 仅排除该既有临时目录，不修改用户文件或仓库 lint 配置；修改文件及其余项目代码无新增告警。

## 验收边界

- 算法、阈值、默认 batch64、缓存版本、数据库结构和分发方式均未修改。
- 本轮验证平台为 macOS arm64。此前 `c61b627` 的冻结 worker 传输/计算冒烟仍是历史证据，本轮未重建安装包，不将其描述成本次源码的完整冻结应用验收。
- Windows 与 macOS x86_64 的目标平台冻结启动/取消/退出冒烟仍未完成；不能声明跨平台交付或正式发布完成。
- 2B 的已确认需求另行实施：卸载组件默认删除其模型、不弹窗、保留文献和已有成果；正在计算时等待任务结束后自动卸载。
