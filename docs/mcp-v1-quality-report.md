# MEFinder MCP v1 文献核对质量报告

更新日期：2026-08-14

适用版本：MEFinder 0.4.4

## 结论

MCP v1 的确定性质量矩阵已通过。三个只读工具可以在同一公共合成索引上表达精确、归一化、模糊、多候选、跨页、双开页、页码校准、Word、来源限定、无结果和索引故障，不需要新增工具或输出字段。

质量测试发现并修复了一个真实契约缺陷：搜索内核的 `ngram_fuzzy` 现在由应用适配层映射为 MCP 公共术语 `fuzzy`。搜索内核和冻结契约均未改变。

## 场景矩阵

| 场景 | 通过条件 | 结果 |
| --- | --- | --- |
| 完全精确 | `match_type=exact`，分数为 1 | 通过 |
| 忽略空格 | `match_type=space_insensitive` | 通过 |
| 忽略标点 | `match_type=punctuation_insensitive` | 通过 |
| NFKC | 全角查询命中半角原文，`match_type=normalized_exact` | 通过 |
| 少量错字 | 返回 `fuzzy`，分数低于 1 且达到内核阈值 | 通过 |
| 重复原句 | 两个来源和两个段落 ID 均保留 | 通过 |
| PDF 跨页 | 物理页范围 2–3、正式引用页范围 39–40 | 通过 |
| 双开页左/右/跨中缝 | 同一物理页分别解析为引用页 41、42、41–42 | 通过 |
| PDF 已校准 | `citation_page.status=calibrated` | 通过 |
| 只有物理页 | 正式页为空，`citation_page.status=uncalibrated` | 通过 |
| Word | 无 PDF 物理页，`citation_page.status=verified` | 通过 |
| 指定文献 | `source_file_id` 将重复候选限制为单一来源 | 通过 |
| 无结果 | 成功返回 `total=0` 和空 `matches` | 通过 |
| 索引不存在/暂不可用 | 分别返回 `index_not_found` / 可重试的 `index_unavailable` | 通过 |
| 证据追溯 | 搜索游标可读取对应来源位置，命中文本存在于阅读窗口 | 通过 |

自动化入口为 `tests/test_mcp_quality.py`，夹具为 `tests/mcp_v1_fixture.py`。夹具只使用合成文本，不依赖个人文献或受版权保护的测试语料。

## 模型上下文和调用基线

测量使用紧凑 JSON 的 `structuredContent` 字节数，加文本 `content` 的 UTF-8 字节数，不含 JSON-RPC 帧。机器可读基线位于 `tests/fixtures/mcp_v1_quality_baseline.json`。

| 项目 | 基线 |
| --- | ---: |
| Server instructions | 156 字符 / 326 UTF-8 字节 |
| instructions + 三个工具定义 | 9,215 UTF-8 字节 |
| 裁剪无关 `$defs` 前的工具上下文 | 16,744 UTF-8 字节 |
| 精确定位 | 1 次调用 / 975 字节结果 |
| 先解析指定文献再定位 | 2 次调用 / 1,210 字节结果 |
| 定位后扩展跨页上下文 | 2 次调用 / 2,701 字节结果 |
| 两个重复候选的定位结果 | 1,793 字节（结构化结果与简短文本合计） |
| 无结果 | 161 字节（结构化结果与简短文本合计） |

`tools/list` 现在只给每个 `outputSchema` 附加实际引用的 `$defs` 及其传递依赖。三个 schema 仍可脱离合同文件独立校验，模型可见工具上下文减少约 45%。

## 模型行为约束

Server instructions 的前 512 个字符已经完整包含以下约束：

- 不把 PDF 物理页称为正式引用页；
- 不隐藏多候选歧义；
- 搜索命中只证明文本在本地索引中出现，不自动证明题录元数据完全正确。

结构化结果对未校准页码返回空正式页和明确的 `uncalibrated` 状态，对无结果返回空候选，对重复结果保留全部候选。这样可以让最终回答逐项追溯到工具证据。

本报告验证的是 MCP 契约、服务输出和模型可见 instructions。后续里程碑 5 已完成真实 Codex 客户端的自然语言复验，结果见 [`mcp-v1-codex-e2e-report.md`](mcp-v1-codex-e2e-report.md)。

## 2026-09-29 补充：v0.5.8 只读结构查询

新增 `list_sections`、`describe_page_mapping` 两个只读工具；`read_document_window` 增加可选 `section_index`，`list_documents` 增加 `work`（所属作品与已对齐版本），`verify_quotes` 的命中增加 `section`（章节标题路径）。工具总数 13 → 15。

| 测量项（同一合成夹具，`tests/fixtures/mcp_v1_quality_baseline.json`） | 变更前 | 变更后 |
|---|---|---|
| 模型可见工具上下文（instructions + `tools/list`） | 41,385 字节 | 49,480 字节（+19.6%） |
| Server instructions | 384 字符 | 457 字符（仍在 512 字符内） |
| `list_documents` 按名称查询的结构化结果 | 214 字节 | 226 字节 |
| `verify_quotes` 两条批量核对的结构化结果 | 1,164 字节 | 1,179 字节 |
| `describe_page_mapping`（2 页 PDF） | — | 930 字节 |

增量几乎都来自每次会话都会下发的工具定义；单次调用结果基本不变。换来的是：页码偏移不再需要用 `read_document_window` 逐页试探（一次调用代替多次试读），通读可以按章节切分。未新增写工具，更长语境仍通过命中的 `reader.start` 调 `read_document_window` 获取，没有给 `verify_quotes` 加上下文参数，以免批量核对结果膨胀。

## 2026-09-29 补充：书目补全写工具

新增 `propose_bibliographic_update`、`confirm_bibliographic_update`（工具 15 → 17）；`read_bibliographic_metadata` 增加 `update_requests`。模型可见工具上下文 49,480 → 54,174 字节（相对最初 41,385 字节 +30.9%）；server instructions 510 字符，仍在 512 字符内。`read_bibliographic_metadata` 单次结果 1,152 → 1,173 字节，其余调用结果不变。
