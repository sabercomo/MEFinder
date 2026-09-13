# 嵌入 batch 64 vs 16 前置实验结论(逐向量 + 完整对齐链接身份)

2026-09-13。工具修复后,在机器安静、**本地模型缓存就位**(`~/Library/Application Support/MEFinder/runtime/components/text-alignment/models`)时执行 `scripts/batch_size_compare.py`(真实 MiniLM,同模型文件/同文本与顺序/私有拷贝缓存,batch 之间清空 `document-vectors/*.npy` 强制各自重算)。真实对 group `document-group-c7e0…`,pivot/target = manifest `alignment_request`,3948 段文本(2047+1901)。数据:[JSON](batch-size-compare-2026-09-13.json)。

> **更正**:此前报告称"本地无模型缓存、实验未执行"——错误。模型在**真实 app 运行时目录**(`runtime/components/text-alignment/models`)而非我先前搜索的 `.codex-tmp`/HF/仓库 `components`。实验现已执行,以本报告为准。

## 结论:batch16 数值上与 batch64 近乎等价,对齐结果完全一致;但**不建议凭此单对证据直接改默认**

### 完整对齐链接身份(非只比 967 计数)
- 链接数 967 = 967;**结构完全一致**(`identical_structure=true`):每条链接的 order、pivot/target 成员段、review_status、anchor 全部相同;新增/消失/翻转均为 **0**。
- 分数逐条差异:`max_confidence_delta=8.7e-5`、`max_cost_delta=1.0e-4`——可忽略。

### 逐向量(3948 段,dim 384)
- 逐位相同比例 **96.5%**(3810/3948);其余 3.5% 有微小差异。
- 最大单元素绝对差 **3.26e-4**,平均 1.9e-6,**最小逐行余弦 0.99999982**(方向几乎不变)。
- 按文本长度分桶:**短文本(<20 字,397 段)逐位完全一致(max_abs=0)**;差异只出现在中/长文本(medium 3.26e-4 / long 3.18e-4)——与 batch 边界的分块/padding 数值路径一致。
- 超过 1e-4 保守噪声阈值的行:138 / 3948。

### 采纳判断(是否值得 batch16)
- **值得作为候选**:对齐输出在本对上**完全一致**,向量余弦≈1,叠加此前 batch 对照的**峰值 −~258 MiB(−21%)**内存收益(见 [内存报告](memory-alignment-export-2026-09-12.md))。
- **但不得凭本单对"差异很小"直接改默认**,须先满足(与本报告工具已支持):
  1. **多对/多样本泛化**:本轮仅一对真实文献。需对更多代表性对(不同语言、长短分布、边界样本)重跑 `batch_size_compare.py`,确认"链接身份一致"稳定成立——单对一致不等于全库一致。
  2. **缓存版本语义**:向量**非逐位相同**(3.5% 行有 ~3e-4 差异)。若把默认改到 16,新算向量与已缓存的 batch-64 `.npy` 会有微差;必须决定是否 bump `EMBEDDING_RUNTIME_VERSION`(否则同一 model_id 下新旧 batch 向量在 `document-vectors` 缓存里混用)。
  3. **下游可翻转性**:~3e-4 的向量漂移在本对未改变任何链接,但需确认它不会在其它对的**定位(locate)/复用(reuse)**边界处翻转结果——这正是 verdict 标为 `review-vectors-changed-links-stable`(而非 `safe-bitwise-identical`)的原因。
- **产品默认维持 batch 64,本报告不改任何代码。** 采纳与否是后续独立决策,须补 1–3 的证据后再定。

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

纯比较逻辑(逐向量/完整链接/verdict)由 `tests/test_batch_size_compare.py` 覆盖;模型运行环境:fastembed 对该模型给出"mean pooling 而非 CLS"的 UserWarning(两 batch 同环境,不影响对比)。
