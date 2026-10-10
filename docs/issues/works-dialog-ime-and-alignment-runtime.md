# 新建作品中文输入、菜单宽度与对齐启动失败

## 2026-10-10 — 复现与原因

### 事实

- 新建作品的文献搜索在每次 `input` 事件中调用 `draw()`，重建整个弹窗并替换活动输入框。真实 Chrome 的 `Input.imeSetComposition` 复现测试确认原输入框被移除，中文组合输入中断；普通文本中间插入也会把光标移到末尾。
- `.tw-select-menu` 使用固定定位时，`min-width: 100%` 相对于视口计算。在 1280px 宽窗口中，触发器宽 263px，菜单实测宽 1280px。详见 [核验报告](../../reports/works-dialog-runtime-2026-10-10.md)。
- 本机 `dist/MEFinderData/runtime/desktop.log` 的 14:23:07、14:23:12、14:23:24 三次《异化》任务均在 `runner.probe()` 失败，报“对齐计算进程未返回能力应答(exit=1)”，尚未进入正文计算。通用提示“请检查两本文献的解析文本”没有反映这一失败阶段。
- 独立计算环境的 `pyvenv.cfg` 仍指向旧 `E:\OneDrive\MeFinder\runtime\components\text-alignment\_python\...`。直接执行其 `python.exe --version` 报 `uv trampoline failed to spawn Python child process` / `entity not found`。当前目录内的托管 Python 可正常启动。

### 修复

- 文献搜索只替换候选列表，保留输入框、焦点、组合输入与光标；选择文献、切换目标等操作仍沿用原弹窗渲染。
- 将菜单 `min-width` 改为 `0`，由既有定位函数设置实际宽度。
- 能力探测阶段没有收到应答即退出，归类为 `WORKER_START_FAILED`；界面提示在设置的“译本对齐”中重新安装计算组件。计算阶段崩溃仍保留原失败分类。
- 本机先备份启动器，再用现有 uv、现有 Python，以 `venv --allow-existing --relocatable --offline --no-python-downloads` 修正环境启动路径。依赖与模型保留，真实文献数据库未写入。

### 边界

本轮修复当前本机环境及错误提示，没有实现跨电脑迁移 Python 环境。能力探测和小样本真实 E5 计算成功，不代表《异化》整书对齐或对齐准确性已经验证。
