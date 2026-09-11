# 原生宿主验证(独立进程 + AppKit/WKWebView 原型)

2026-09-12:按约定顺序完成两项验证——后台作为独立进程的验收,以及小范围 AppKit/WKWebView 手写宿主原型;并给出"原生体验收益是否抵偿维护成本"的判定。

## 第一项:后台独立进程(通过)

- 验收测试 `tests/test_backend_standalone_process.py`:以性能协议同一 worker 入口(`scripts/bench_responsiveness.py --worker`)把真实后端跑成独立子进程,HTTP 逐项驱动:
  - 搜索:exact 与 auto 均返回带字符区间与 PDF 页内锚点的结果;
  - 对照目标列表:`/api/text-alignments/targets` 读取已有成果;
  - 对齐:符号链接指向本机 MiniLM 组件后,`start`→`status` 完成真实离线生成,`alignment_links` 落库;
  - 退出:stdin `stop` → begin_shutdown/durable 等待/关闭运行时,进程退出码 0。
- 与任务 4 的 `desktop_backend.DesktopBackend` 语义一致:窗口进程与后端进程可以分离,业务页面只见 `http://127.0.0.1` URL。

## 第二项:AppKit/WKWebView 原型(五项验收通过)

- 原型 `prototypes/native_host_appkit.py`(329 行,含自验收状态机):独立后端子进程 + 手写 AppKit 宿主,业务页面为真实 SPA,零产品代码改动。
- 自驱动验收报告(全部 ok,进程退出码 0):
  - 搜索:主窗 WKWebView 内对真实 SPA 执行 `runSearch()`,3 行结果渲染;
  - 定位:`selectResult(0)` 打开详情面板,含页码锚点信息;
  - 多窗口:第二个 WKWebView 原生窗加载 `/reader-window`;
  - PDF 打开:PDFKit `PDFView` 打开生成的 PDF 并停在第 1 页;
  - 退出:后端优雅停止(退出码 0)+ `NSApplication.terminate`。
- 过程记录的技术事实:PyObjC 的 WKWebView 完成回调必须返回 void;`evaluateJavaScript` 不支持 Promise 返回值(WKErrorDomain Code=5);delegate/定时器对象必须由 Python 侧持强引用。

## 判定:现阶段不替换 pywebview(收益不抵成本)

原型证明方向可行,但 329 行只覆盖了 pywebview 免费能力的很小一角。替换宿主需要自建并双平台回归:

| pywebview 现成提供 | 原型未覆盖/需自建 |
|---|---|
| `window.expose` 的 JS↔Python 双向桥(独立阅读窗的 `open_reader` 入口) | WKWebView script message handler + promise 桥 |
| 文件/文件夹对话框(FOLDER/OPEN、file_types、多选) | NSOpenPanel 封装与错误语义 |
| macOS 透明标题栏 + 原生拖拽条适配 | 已有 AppKit 代码需重接到自有窗口类 |
| Windows 无框窗口、边框调整大小、DWM 主题(`windows_desktop.py` 784 行基于 pywebview 句柄) | 全部重写或改接 |
| WebView2/WKWebView 差异屏蔽、存储路径、事件模型 | 各平台自行处理 |

- **收益侧**:直接持有 WKWebView 可更早介入导航/手势/首帧;但 pywebview 6.2.1 在 macOS 本就是 WKWebView,当前没有必须绕过它的体验缺陷。
- **成本侧**:上述表格 ≈ 数千行平台代码 + 双平台回归 + 打包(解耦 PyInstaller 隐藏导入)改造;`windows_desktop.py` 的大量边框/主题逻辑与 pywebview 对象模型耦合。
- **结论**:维持 pywebview 宿主;已验证可保留的资产是(1)后台独立进程架构与验收测试,(2)`DesktopBackend` 生命周期,(3)本原型作为未来评估的对照样本。若未来出现 pywebview 无法满足的具体需求(如 WKWebView 级别的手势/扩展),按本原型的接缝小步替换单窗口,而非整体迁移。

## 复现

```bash
# 独立进程验收
.venv-macos312-arm64/bin/python -m unittest tests.test_backend_standalone_process
# 原型(屏幕上会短暂出现三个窗口,约 10–20 秒)
.venv-macos312-arm64/bin/python prototypes/native_host_appkit.py --report /tmp/native-host-report.json
```
