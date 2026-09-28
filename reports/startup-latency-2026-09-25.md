# 启动时文献库空白数秒（2026-09-25）

2026-09-25：已定位并修复。根因是 `0f1a47d` 往首屏轻量总览里加的正文范围过期核对（冷启动每次重识别全部版本，约 5 s，且持有索引锁），不是文献数量。修复后同一构建第二次起启动，文献库摘要 0.70 s、译本对照总览 0.02 s。

## 事实（真实书库，149 篇 / 20 个检测正文范围的分段集，D 盘 NVMe，便携包实测）

- 后端就绪一直很快（`loading index` → `backend ready` 60–250 ms），卡在就绪之后的首批请求。
- 热缓存下接口本身不慢：文献库摘要 0.36 s、总览 0.8–1 s（含统计）。
- 冷启动并发请求首屏接口（修复前）：文献库摘要 **5.51 s**、`translation-works/overview?include_statistics=0` **4.98 s**。
- cProfile：轻量总览 7.7 s 中 7.6 s 在 `_run_staleness → _body_range_changed → alignment_body_bounds`，对每个分段集全文跑标题识别（约 128 万次 `re.sub`）；结果只存进程内字典，每次启动重算。09-19 报告（`startup-reader-latency-2026-09-19.md`）测得同一路径 2.9 ms，当晚 `0f1a47d` 加入该核对后回归。
- 总览经 `IndexRuntime.run_when_ready` 持锁执行，文献库摘要要同一把锁，于是排在它后面；`90-init.js` 启动即 `MEFinder.works.load()`，首屏必然触发。
- 试过进程内后台线程预热（不持锁）：源码环境文献库摘要冷启动从 0.33 s 恶化到 6.95 s——预热是纯 CPU，摘要路径有 577 次 `nt.stat`，每次释放 GIL 后都要等切换间隔（GIL 护航效应）。**进程内预热不可取**，未采用为最终方案。

## 修复

- 前端：启动不再立即加载译本对照，改为文献库摘要返回、浏览器空闲后预取；进入「译本对照」页仍立即加载。
- 后端：`translation_works.warm_detected_body_bounds` 把检测结果按构建指纹写入 `runtime/data/alignment-body-bounds-cache.json`。指纹 = 冻结可执行文件路径 + 大小 + mtime，每次重打包即失效，保证「识别算法变了要重新核对」的语义不被缓存掩盖；源码运行（非冻结）从不读写该文件。分段集不可变，同一构建读回的值与现算一致。

## 修复后实测（同一便携包，连续两次冷启动）

| 请求 | 修复前 | 打包后首次启动 | 第二次起 |
|---|---:|---:|---:|
| `library?view=summary` | 5.51 s | 3.97 s | 0.70 s |
| `document-groups` | 0.35 s | 0.55 s | 0.20 s |
| `translation-works/overview?include_statistics=0` | 4.98 s | 1.97 s | 0.02 s |

- 首次启动仍需现算一次（日志 `detected body ranges for 15 segment sets`），这是按构建失效的代价。
- 限制：单机两轮测量，未控制系统负载；不据此承诺其他机器的具体耗时。

## 附带发现

- 库原先放在 OneDrive：OneDrive 进程 7 小时累计读 186 GB（3.4 GB 库被整份重读约 54 次）。库已迁至 `D:\ME_Finder\dist\MEFinderData`，见 `docs/issues/onedrive-placeholder-startup-hang.md` 的注意项。
- 强杀主进程会留下孤儿本地 MinerU 服务（`mineru-api`），它锁住 `dist\MEFinder\_internal\MSVCP140.dll` 导致打包 `--clean` 失败；应正常关闭窗口。
- 打包门禁偶发 `test_uninstall_waits_for_download_receipt_before_deleting` WinError 32（`installed.json` 被占用），重跑通过；无改动基线 10/10、有改动 21/22，差异不显著，按既有 Windows 句柄类偶发处理。
