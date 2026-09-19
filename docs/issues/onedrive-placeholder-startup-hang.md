# OneDrive 云端占位文件导致启动加载页无限挂起

- 2026-09-19：定性完成,**非打包缺陷**。跨机共享的 index.sqlite3 被 OneDrive 降级为「仅云端」占位文件,启动首次读取等待 3.4 GB 水合;已固定文件回本地,并在应用侧加启动守卫(检测到占位直接给可操作错误页)。

## 事实

- 症状:Windows 打包版(v0.5.5,dist\MEFinder onedir 与 setup 安装版同源)启动后停留在「正在加载索引」闪屏,数十分钟不就绪、也无错误页。
- 日志证据(`E:\OneDrive\MeFinder\runtime\desktop.log`):
  - 2026-09-13 至 09-18 23:19 的历次 Windows 启动,`loading index` → `backend ready` 约 0.1–0.3 秒(含 v0.5.5 包),证明包本身可正常启动。
  - 09-19 22:22 与 22:57 两次 Windows 启动只到 `loading index from E:\OneDrive\MeFinder\runtime\data\index.sqlite3`,无 `backend ready`、无异常栈;期间数据库文件 mtime 停在 09-19 20:26(macOS 侧写入)、无 -wal/-shm 产生——挂起点在对 DB 的第一次写入之前。
  - 09-19 全部 `backend ready` 条目来自 macOS 侧(`/Users/mercury/.../MeFinder/runtime`,53ms 就绪):该库是经 OneDrive 同步跨机共享的同一份 SQLite。
- 文件属性证据(powershell `Get-Item`):`index.sqlite3` Attributes = 0x401620(Archive + SparseFile + ReparsePoint + Offline + RecallOnDataAccess),即 OneDrive「仅云端」占位;启动所需的 config/、parser_jobs.sqlite3 等其余文件均在本地。
- 触发链(日志时间戳):09-18 23:19 Windows 侧导入《导读巴特勒》写库 → 09-19 20:22–20:26 macOS 侧导入《知觉现象学》写同一库 → OneDrive 同步冲突,产生冲突副本 `index-DESKTOP-B0CAH26.sqlite3`(Windows 侧版本),存活的 `index.sqlite3` 为 macOS 侧版本且在 Windows 侧被降级为占位。
- 处置:`attrib +P -U` 将文件固定为「始终保留在此设备」,OneDrive 重新下载回本地,水合完成后启动恢复。

## 推断

- 挂起表现为「无限闪屏」的原因:首次 SQLite 读取阻塞在 OneDrive 过滤驱动等待水合,而 `DesktopBackend.start` 在 `loading index` 与 `backend ready` 之间既无超时也无占位检测;3.4 GB 下载远超闪屏文案「约 20–30 秒」的预期,用户只能看到卡死。
- Windows 侧改动(《导读巴特勒》导入及其解析结果)只存在于冲突副本,不在存活 `index.sqlite3` 中;SQLite 无自动合并,需重导入或人工捞取。

## 代码守卫

- `src/me_finder/desktop_backend.py` 新增 `cloud_placeholder_hint()`:以 `os.stat().st_file_attributes` 检测 Offline / RecallOnDataAccess 位;`DesktopBackend.start` 检测到占位即渲染错误页「索引数据库在云端,尚未同步到本机」并返回 False,不再进入等待水合的读取。
- 测试:`tests/test_desktop_backend_cloud_placeholder.py`(检测助手 4 例 + start 守卫 1 例 + posix stat 兼容 1 例)。

## 注意

- 云盘跨机共享同一 SQLite 库会再次因双写产生冲突副本;错误页文案已提示「避免两台机器同时启动并写入」,根治需改单机存放或错峰同步(未实施,待用户决定)。
- OneDrive 可能对未固定文件再次降级;固定(pin)状态为本机属性,重装系统或换机后需重新固定。
