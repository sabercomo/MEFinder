# 译本阅读审核修复

2026-09-18：审核发现的三项问题及搜索高亮干扰跟随已修复；此报告不代替跨平台冻结包验收。

## 复现与修复

| 场景 | 修复前 | 修复后证据 |
| --- | --- | --- |
| 两个源分段组成一个链接，校正后定位其中一个字符 | `no_counterpart` 链接仍返回 automatic 目标 | `test_multi_source_correction_applies_to_scroll_selection` 验证有目标校正及无对应均生效 |
| 校正范围包含选区、存在更精确校正或已撤销 | 需要明确读取优先级且不改变代理提案语义 | `test_contained_correction_keeps_scope_precedence_and_revocation` |
| 切换请求未结束便关闭或替换右栏 | 过期响应重新打开旧译本 | 执行真实 JS 函数，`test_obsolete_open_cannot_restore_closed_or_replaced_comparison` |
| 当前页重新生成对齐 | 仍命中旧链接缓存，无新链接请求 | `test_completed_alignment_invalidates_links_and_relocates_open_pair` |
| 从检索命中进入，跟随响应完成且原高亮仍可见 | 回调再次移动左栏到原搜索命中 | `test_follow_response_does_not_scroll_source_back_to_search_hit` 验证只移动右栏 |

上述核心复现测试在修复前失败，修复后通过。没有更改对齐算法、模型、batch64、正式书库或数据库 schema。

## 浏览器检查

- 使用已有书库快照的 APFS 克隆副本，运行根隔离到临时目录；通过真实 HTTP 接口读取已有对齐。
- 法哲学原理英德直接对照：英文跳到 PDF 第 100 页后，右侧定位到德文对应段；左栏再向下滚动一屏，右栏从可见索引 52/53 移到 53，对应高亮更新，左栏保持滚动后的位置。
- 暂停跟随后，左栏再滚动一屏，右栏 scrollTop 保持 19844；恢复跟随后右栏重新定位，左栏保持 31462.5。
- 模型未安装的预览环境仍能读取已有成果。预览没有生成新模型结果，任务完成刷新使用受控网络回归测试验证。

## 自动门禁

- `PYTHONUTF8=1 NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv-macos312-arm64/bin/python -m unittest discover -t . -s tests`：2399 项通过，23 项跳过。
- `.venv-macos312-arm64/bin/python -m ruff check . --extend-exclude .codex-tmp`：通过；前端指纹和结构守卫包含在全量测试中。
- 已有提交 `45d0e9a` 的 CI 35253558327 已核验成功；本次修复的远端 CI 与本地门禁分别记录。

## 范围与剩余验收

本轮不实现章节目录，不重跑 Windows 发布冒烟。真实桌面的新窗口往返、生成/取消完整操作、冻结包退出与卸载只读保留此前的验收待办。
