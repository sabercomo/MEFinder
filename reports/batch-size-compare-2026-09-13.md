# 嵌入 batch 64 vs 16 前置实验结论(逐向量 + 完整对齐链接身份)

2026-09-13。机器安静、本地模型缓存就位(`~/Library/Application Support/MEFinder/runtime/components/text-alignment/models`)时执行 `scripts/batch_size_compare.py`。真实对 group `document-group-c7e0…`、pivot/target = manifest `alignment_request`。数据:[JSON](batch-size-compare-2026-09-13.json)。

> **两处更正(以本版为准)**:
> 1. 此前称"本地无模型缓存、实验未执行"——**错误**。模型在真实 app 运行时目录,非我先前搜索的 `.codex-tmp`/HF/仓库 `components`。
> 2. 此前逐向量比较**另行把 3948 段原文直接交给 provider**,但生产 `embed_text_sequences` 会先**去重**(本对 3948 段→**3837 唯一**)再推理、按段回填;重复项改变 batch 分组,故旧的"96.5%逐位一致"不代表生产对齐所用向量。**现改为直接读取两次真实对齐各自写入的 `document-vectors/*.npy`**(即生产 去重→推理→回填 后的**实际逐段向量**),按稳定段序比较。

## 结论:verdict = **manual-review-required**(工具不自动判定"可采纳")

区分"结构一致"与"输出完全一致":本对**链接结构完全一致,但 confidence/cost 有极小非零差异、向量非逐位相同**,因此不是"完全一致",工具不给"safe"。

### 完整对齐链接(结构 + 成员 + 状态 + 锚 + 分数,非只比 967 计数)
- 967 = 967;`identical_structure=true`:每条链接 order、pivot/target 成员段、review_status、anchor_key 全一致;新增/消失/翻转 = 0。
- **分数不忽略**:`max_confidence_delta=8.68e-5`、`max_cost_delta=1.02e-4`——极小但**非零**,故 `scores_identical=false`、`outputs_fully_identical=false`。

### 逐向量(实际逐段向量,3948 段/3837 唯一,dim 384)
- 逐位相同比例 **96.8%**(3822/3948);`min_cosine 0.99999982`;`max_abs 3.5e-4`;`mean_abs 1.7e-6`。
- 按长度分桶:**短文本(<20 字,397 段)逐位完全一致**;差异只在中/长文本(medium 3.22e-4 / long 3.54e-4)——与 batch 边界分块/padding 数值路径一致。

### verdict 口径(修正后)
- 工具**只报告观察事实、差异与待审查项,绝不凭自设阈值自动判"可采纳"**;confidence/cost 任何非零差异都计为变化(即使向量与结构否则一致)。
- 本对:`observed_changes = [scores, vectors-not-bitwise-identical]` → `recommendation = manual-review-required`;`do_not_change_product_default = true`。
- **是否采纳 batch16 需人决策**,且须满足:①代表性多对/边界样本(本轮仅一对);②对齐/定位结果的质量门槛;③向量非逐位相同→是否 bump `EMBEDDING_RUNTIME_VERSION` 的缓存版本决策(否则同一 model_id 下新旧 batch 向量在 `document-vectors` 混用)。
- **产品默认维持 batch 64,本轮不改任何产品代码,不据"差异很小"采纳。** 内存收益(此前 batch 对照峰值 −~21%)是候选动机,但不构成采纳依据。

## 记录的执行身份(provenance,写入 JSON)
- code_revision `ac51698`;model_fileset_sha256(13 个模型文件的合并摘要);embedding_thread_count **3**;segment_count **3948**、unique_text_count **3837**;input_identity_sha256(段文本拼接);cache_state="每次对齐前清空 document-vectors,各 batch 独立新鲜推理"(脚本断言清空后为空,杜绝 .npy 复用);execution_order=[batch64, batch16];vectors_source="各对齐实际写入的逐段 .npy(生产去重+推理+回填),非另行原文重嵌入"。用户原始库/模型/缓存未改(私有拷贝到临时目录)。

## 复现

```bash
PY=.venv-macos312-arm64/bin/python
MODELS="$HOME/Library/Application Support/MEFinder/runtime/components/text-alignment/models"
NO_PROXY=localhost,127.0.0.1 $PY scripts/batch_size_compare.py \
  --db .codex-tmp/real-library-20260911/index.sqlite3 \
  --group document-group-c7e0214336a240d78b687802319cb1bf \
  --pivot pdf-import-e63bdea39567e3ac --target pdf-import-d574027d045ce657 \
  --models "$MODELS" --output reports/batch-size-compare-2026-09-13.json
```

纯比较逻辑(逐向量/完整链接/verdict,含"向量与结构一致但分数变化仍不 safe"的复现用例)由 `tests/test_batch_size_compare.py` 覆盖。fastembed 对该模型给"mean pooling 而非 CLS"的 UserWarning(两 batch 同环境,不影响对比)。**本轮只跑当前文献对,未扩展全库。**
