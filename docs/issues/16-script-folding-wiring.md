> 2026-09-08：以下原始接线指引作为历史记录保留；本 PR 已完成接线，现状与修正见文末。

# 繁简折叠检索：剩余接线指引（issue #16）

本分支已完成（可直接复用，勿重复实现）：

- `src/me_finder/script_conversion.py`：OpenCC 封装（词级 `to_simplified` /
  `to_traditional`、句段折叠 `fold_to_simplified_with_map`、查询变体
  `query_variants`）；OpenCC 缺失时全部恒等降级。
- `src/me_finder/application/script_search.py`：`execute_with_script_folding`
  在 SearchService 层做繁简双轨执行与合并，**`search.py`、FTS 索引、页码锚定
  零改动**；每个变体走完整现有管线（trigram FTS、`bm25`、SequenceMatcher 精配、
  短查询 `_search_sql` 回退自动覆盖），返回偏移始终针对 `text_raw`。
- `tests/test_script_conversion.py`、`tests/test_script_search.py`。

## 剩余四处接线

### 1. 搜索调用点：`src/me_finder/application/index_runtime.py`

现状（片段）：`return SearchService.execute(self._engine, request)`

改为：

```python
from .script_search import execute_with_script_folding

return execute_with_script_folding(
    self._engine, request, enabled=<当前偏好值，默认 True>
)
```

偏好值在调用点从 preferences 读取后传入（wrapper 保持纯函数、可测）。
`parallel_passage_service.py` / `literature_verification_service.py` / MCP 路径
本期**不接**，保持对照组行为，后续需要再开。

### 2. 设置键：`src/me_finder/preferences.py`

新增布尔键 `script_folding`，默认 `True`（纯召回增益，无副作用），经
`preferences_controller.py` 暴露给前端读写。参照现有 legacy 字段 + 嵌套对象
的存取模式。

### 3. 设置页复选框：`src/me_finder/templates/index.html`

设置区加复选框「繁简统一检索（以简体折叠繁体）」。只需走现有偏好保存通道
写入 `script_folding`；**不要**改 `/api/search` 的 payload——单用户本地应用，
服务端在调用点读偏好即可，前端搜索结果渲染逻辑一行不动。

### 4. 依赖与打包

- `pyproject.toml` / `requirements-windows.txt` / `requirements-macos.txt`：
  加 `opencc-python-reimplemented`（纯 Python；不要用 PyOpenCC，C 扩展会破坏
  现有打包链路）。
- `build_macos.sh` / `build_portable_release.ps1` / `build_windows_installer.ps1`：
  PyInstaller 需打包 OpenCC 词典数据，加 `--collect-data opencc`（或等效 datas
  配置）。
- `THIRD_PARTY_NOTICES.txt`：补 OpenCC 的 Apache-2.0 声明。

## 验收

1. `python -m unittest tests.test_script_conversion tests.test_script_search -v`
   全绿（无 OpenCC 环境下转换类用例自动 skip，降级路径用例仍跑）。
2. 导入一本繁体 PDF（如臺版書），开关开：简体词能命中繁体段落，高亮落在
   原文正确位置，页码跳转不变；开关关：行为与 main 完全一致。
3. 已知限制（写进 issue 回复即可，不 blocking）：s2t 有天然歧义
   （"发"→"發/髮"取 OpenCC 默认），词组跨标点不折叠；引文与导出永远用
   `text_raw` 原文，不受开关影响。

## 2026-09-08：接线与审查修复

事实：Web/桌面全文搜索已接入实时偏好，默认启用，可在“文献检索”关闭。原文、引文、高亮位置、页码与导出保持原样，不修改索引或数据库。

原实现在每个变体内独立执行 auto，可能将模糊命中排在另一变体的精确命中前；现改为每种精度先执行所有变体。去重键加入字符区间；被截断的变体只提供统计下界，不将返回数量冒充精确总数。

更正：通用 t2s 将軟體转为软体，不承诺软件等地区词汇替换。`fold_to_simplified_with_map` 是保留的独立工具，不参与搜索偏移计算。MCP 与对齐路径尚不使用该偏好。

复现、修复验证及限制见 [验证报告](../../reports/issue-16-script-search-validation-2026-09-08.md)；契约见 [繁简检索契约](../contracts/v0.5.3-script-search.md)。
