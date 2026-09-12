# MinerU 账号状态：未检测过被显示成「可用」

2026-09-12：前端已按 DESIGN.md §6 改为如实陈述；**后端缺口仍在，待补。**

## 事实

`parser_credentials.health_status` 在 `large_document/job_ledger.py` 的建表语句里
默认值就是 `'healthy'`，写入账号时即为该值。设置页原先据此把
`enabled && configured && healthy` 显示为「可用」——一个刚保存、从未检测过的账号
也会显示「可用」，与 DESIGN.md §6「保存成功不等于连接成功」冲突。

`MinerUAccountSummary`（`large_document/mineru_accounts.py`）不含任何检测时间字段，
因此前端无法区分「从未检测」「上次检测成功」「结论已过期」。

## 本轮处理（仅前端）

- 未检测过的账号显示「密钥已保存 · 尚未检测」，不套警告色块，也不写成可用。
- 「认证失效」「冷却中」「已停用」「缺少 Token」来自真实后端信号，保留原样。
- 本会话内点过「测试」的账号就地显示「刚刚检测：连接正常 / 连接失败」，
  状态存在 `parserStore.mineruProbeResults`，不落盘、不跨会话。
- 提交了新 Token 时作废该账号的结论；改服务地址时作废全部结论（地址是共用前提）。

## 待办（需要后端改动，未做）

给账号摘要补 `last_checked_at` 与 `last_check_result` 并持久化，才能跨会话如实显示
「上次检测：成功（时间）」。这需要 `parser_credentials` 加列 + 迁移 + API 字段，
属于后端契约改动，应单独立项，不要在 UI 层用默认值伪造。
