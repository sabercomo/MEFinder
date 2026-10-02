# 本地 MinerU 管线 venv 缺 `six`，与 2026-10-02 夜 MinerU 免费队列不收工

## 2026-10-02 夜（真实库实测）

**事实**：

- 用户切到「本地 MinerU」后，托管管线每个任务都在 `mineru/backend/pipeline/model_init.py` → `mineru/model/utils/pytorchocr/data/imaug/operators.py` 处 `ModuleNotFoundError: No module named 'six'` 失败（`runtime/components/mineru/pipeline/service.log` 有完整 traceback）。
- 该 venv 由 2026-08-24 的托管安装按配方 `mineru[pipeline]==3.4.5` 建成（`pipeline/installed.json`）；`site-packages` 里确无 `six`——mineru 3.4.5 的打包元数据未声明 `six`，代码却 import 它，属上游缺陷、配方未兜底。
- 处置：用组件自带 `_tools/uv-0.12.1-win32-x86_64/uv.exe pip install --python pipeline/venv/Scripts/python.exe six` 补 `six==1.17.0`，管线立即恢复解析（service.log 见 OCR 进度与 `/tasks/<id>` 轮询 200）。venv 无 pip，须走组件自带 uv。
- 配方侧兜底：`src/me_finder/local_ocr_manifest.json` series 3 的 pipeline default 增加 `six==1.17.0`；回归 `tests.test_managed_mineru.ManagedMinerUInstallTests.test_bundled_pipeline_recipe_carries_the_six_pin` 钉住，防止未来改配方时静默丢掉。
- 存量坏 venv 不会自动自愈：catalog 校验只比对版本与 mineru pin，`installed.json` 记录仍为旧 packages 列表；已手工补装的机器无需重装，新装机器由新配方直接装对。

**同夜另一事实（服务侧）**：MinerU 云端免费队列收活不开工。账本两个在途分片与一个 1 页合成探针（`batch_id` 正常登记、上传 HTTP 200）在 `/api/v4/extract-results/batch/<id>` 均为 `state=pending` 且 20 分钟以上不前进；同一账号当日早前完成过 4 个文件（来自 Mac），故非 token/账号失效。公开渠道无当日故障公告。推断：免费档调度当日异常或并发槽被僵尸任务占用；两者均在服务侧，本地代码无法修复，只能绕开（干净文字层走 native / 本地管线）或等待恢复。

**与代码缺陷的关系**：云端 pending 期间，进度文案原样显示「MinerU 解析中：0/1 个分段」，与真解析无法区分；该问题已修（引擎透出远端状态与等待时长），见 [release notes 0.5.8](../release-notes-0.5.8.md) 夜间一节。旧账本另见 9 月 18/19 日两个分片远端 `done` 而本地停 `waiting`（结果未回收）、8 月 23 日两个分片远端 `task not found or expire` 而本地停 `submitted`/`waiting`——同属"任务生命周期无人收尾"族，其中 zotero 行搁浅一支已修。
