# 素材标签按需读取：同请求响应体对照

2026-11-23：本机 macOS arm64、项目 Python 3.12、临时 SQLite 库，通过实际本地 HTTP 服务读取响应体。未读取真实书库、未测用户端延迟。

| 已保存摘录数 | 完整快照 | 标签路径 | 固定命中段落的摘录和标签 |
|---:|---:|---:|---:|
| 1 | 1,051 B | 39 B | 727 B |
| 1,000 | 919,711 B | 39 B | 727 B |

增加 999 条无关摘录后，两个窄接口的响应均逐字节相同。对实测 SQL 做 `EXPLAIN QUERY PLAN`：枚举的是请求组合（`json_each`），按来源、类型、起止锚点使用摘录表原有唯一索引，再按摘录 ID 查标签表索引。输出按摘录 ID 与标签路径排序。未添加索引或迁移。

## 复跑

1. 用临时索引启动本地 Web 服务。建立标签「实验」，写入一条摘录：`excerpt_id=1`、`source_id=s`、`kind=paragraph`、起止锚点均为 `p1`、偏移 `0–200`、正文为「甲乙」重复 100 次、题名「示例」、作者「作者」、时间 `2026-11-23`，挂同一标签。
2. 读取并按响应主体的字节数计量：`GET /api/materials`、`GET /api/materials/tags`、`POST /api/materials/paragraph-excerpts`（JSON：`{"paragraphs":[["s","p1"]]}`）。
3. 新增相同字段的摘录，仅把摘录 ID 改为 2–1,000、各自起止锚点改为 `p2`–`p1000`。保持请求不变，逐字节比较两次窄接口响应。
4. 自动回归：`<项目 Python> -m unittest tests.test_materials.MaterialsTests.test_narrow_material_reads_ignore_unrelated_rows tests.test_materials.MaterialsTests.test_v11_migration_preserves_existing_index`。这两项同时覆盖同一段落的整段、部分摘录、非 BMP 字符后的偏移、跨来源的相同锚点、重复请求键，以及缺表的旧库不触发迁移。浏览器路径：`MEFINDER_BROWSER_TESTS=1 <项目 Python> -m unittest tests.test_materials.MaterialsBrowserTests`。
