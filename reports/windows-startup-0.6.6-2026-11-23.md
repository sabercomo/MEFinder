# Windows 启动排查：0.6.5 对照

2026-11-23：在桌面启动代码复现了加载页可能失去切换通知的时序问题；移出首屏不需要的 PDF 渲染库导入。没有 Windows 真机计时。

## 证据

- v0.6.5 的公开 Windows 安装包和便携包均来自 onedir 构建。本轮没有证据将这次反馈归因到自解压、SmartScreen、WebView2、云盘或文献库规模。此前 [无库 Windows 启动测量](windows-startup-blank-2026-09-28.md) 是旧版本不同阶段数据，不可作为本轮前后对照。
- v0.6.5 在固定两秒的后台等待之前同步导入 Web 模块，且导入时因素材图片模块而加载 PyMuPDF；首页和普通文本不需要该图片解码器。0.6.6 将 Web 导入纳入现有后台等待，并把素材的 PyMuPDF 加载推迟到首次 PDF 截图或图像校验。其余窗口显示及两秒等待策略不变。
- 人工固定事件顺序：后台等待返回时未就绪，开始用加载页创建窗口，创建过程中后端宣布就绪。0.6.5 重新读取后端状态后跳过导航分支，实际 `load_url` 调用为 0；修复后根据开窗时的快照决定是否需要导航，收到 URL 后只调用一次。见 `test_backend_ready_while_window_is_being_created_still_loads_home`。

## 可复现的源码侧数据

同一台 macOS arm64、项目 Python 3.12。分别在 `d532424` 源码目录和 0.6.6 工作区各起 10 次独立 Python 进程，交错顺序，量测 `import src.me_finder.web`。没有打开数据库或建立 GUI，未控制系统文件缓存，不代表 Windows 冷启动时间：

| 版本 | 中位数 | 每轮导入是否加载 PyMuPDF |
| --- | ---: | --- |
| 0.6.5 | 147.94 ms | 是 |
| 0.6.6（工作区） | 118.31 ms | 否 |

```bash
# 在对应版本仓库根目录，使用项目 Python 3.12；每次命令启动一个新进程。
.venv-macos312-arm64/bin/python -c 'import sys,time; t=time.perf_counter(); import src.me_finder.web; print(round((time.perf_counter()-t)*1000,2), "fitz" in sys.modules)'
```

本轮 macOS 全量 unittest：2,929 项（57 项按条件跳过），Chrome 回归 33 项通过。

后续在报告者 Windows 机器用相同数据目录分别测试已发布版和 0.6.6 测试版。记录安装版或便携版、双击到可操作首页实测时长，以及数据目录 `desktop.log` 中的 `startup:` 行（目录不可写时回退到系统临时目录），用于区分数据目录和设置、后端就绪、启动 WebView 与请求显示主窗口；日志起点不含 Python 入口之前的系统等待。不要关闭系统防护或挪动真实文献库来做归因。
