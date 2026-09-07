# D 实验夹具重验（segmenter v13 / alignment v21，2026-09-07）

issue #18 的 D 实验第一步：把 11 条金标夹具按 **#17 修复后**（segmenter v13、
alignment v21，commit `1bed83f`）逐条重查，确定 D 的真实验收分母。**生产索引只读未动**，
重验在生产索引的一份可写副本上完成。

## 复现方法与保真校验

- 副本：`dist/MEFinderData/runtime/data/index.sqlite3` → 只读拷贝，`PRAGMA quick_check=ok`。
- 重分段与重对齐：`scripts/d_fixture_reverify.py realign`，对 6 个涉及夹具的 pair 调
  `text_alignment.generate_alignment(force=True, reviewed_body_ranges=…)`。分段器按需从
  **已入库文本**重建 v13 段集（不重解析、不 OCR）；E5 向量复用生产 model cache，只对
  真正新增的段字符串嵌入（**未重新嵌入既有语料**）。复核正文区取自
  `reports/alignment-reviewed-body-ranges-2026-09-05.json`，DE/EN 两本重分段侧用
  verbatim 段序映射迁移到 v13。
- 逐条定位：夹具 pivot 按其 **v12 order** 映射到 v13 定位（避免像 n11 的
  `どういうことだろうか。` 这类高频短句被 verbatim 文本匹配定位到错误出现处）。
- **保真锚点（与 issue-17 报告逐位一致）**：
  - DE→EN：DE 6034→6087（+53）、EN 10200→10226（+26）、heading anchors 183、unmatched 334。
  - R9 JA→ZH：unmatched 3913→170、anchors 7→31。
  - n93：pivot v13 order 2314=`§ 203`，候选落 target 4609–4611，conf **0.8299681544303894**，
    状态 `rejected`。

驱动与验证脚本随本分支入库（`scripts/d_fixture_reverify.py`、`scripts/d_fixture_verify.py`），
机器可读结果 `reports/d-fixture-reverify-2026-09-07.json`。

## 逐条前后对照

| n | pair | v20（旧错接） | v13/v21 新状态 | 判定 |
|---|---|---|---|---|
| 11 | R9 JA→ZH | 远处同人展段 T1942–44 | **automatic 0.942 → T3163–3164**，落在正确的性别/拉康段 T3162–3165 内 | **fixed-by-#17** |
| 24 | 法哲学 ZH→EN | T3553–55（§138 附释 “very!”） | automatic 0.865 → T3560–62（仍 “very!/defective” 乱码区），正确 T3542–43（“true conscience”）未接 | 仍错配 |
| 28 | 法哲学 DE→EN | T1198–1200（OCR 乱码） | automatic 0.838 → T1198–1200（**未变**），正确 T1193 未接 | 仍错配 |
| 41 | 法哲学 ZH→EPUB | T5119（Addition G/国家=伦理整体） | automatic 0.864 → T5119–20（**未变**），正确 T6503–08 未接 | 仍错配（远距漂移） |
| 53 | R9 JA→ZH | T291（“依恋”） | **unmatched** — 旧错链消失，正确 T562–564（完整中译）未重接 | 病灶消除·未重接 |
| 60 | 法哲学 DE→EPUB | T3079–81（下段附释） | automatic 0.832 → T3079–81（**未变**），正确 T3075 未接 | 仍错配 |
| 80 | 法哲学 DE→EN | T1861–63（§34 前段） | automatic 0.871 → T1868–70，正确 T1879–80（人格=自我为对象）未接 | 仍错配 |
| 82 | 法哲学 DE→EN | T4506–08（§191 comfortable 附释） | automatic 0.837 → T4516–18（仍 comfortable 区），正确 ~T4521（reelles Dasein）未接 | 仍错配（边界） |
| 88 | 法哲学 DE→EN | T3790–92（悲剧反讽） | automatic 0.918 → T3763–65（邻接“Such a law…”译句，pivot 的**下一句**） | allow-unfixable（命题不同/边界） |
| 93 | 法哲学 DE→EN | 英文 §202 正文 T4593–95 | **rejected 0.829968 → T4609–4611**，落在正确的 §202↔§204 走廊内 | 错节消除·卡阈值 |
| 98 | 法哲学 DE→ZH | T3120–22（信念=法义规则） | automatic 0.899 → T3133–35（更远的信念/意图段），正确 T3115–16（福利服从更高目的）未接 | 仍错配（反向漂移） |

## 结论：D 的验收分母

- **#17 直接修好：1 条**（n11，正确 automatic 重接）。
- **仍失败：10 条**（n24/n28/n41/n53/n60/n80/n82/n88/n93/n98）。其中：
  - **n88** 按金标 `allow-unfixable`（双生命题、可能无邻近对应），不计入严格通过分母。
  - **n93、n53** 为 D 最干净的靶：#17 已消除错节/错链，正确对应已在邻近（n93 候选就在
    §203 走廊、仅因 0.829968<0.83 被拒；n53 正确中译在 T562–564、pivot 现为 unmatched），
    D 的目标是走廊内重新成组并让正确配对过阈值/被接受。
  - 其余 7 条（n24/n28/n41/n60/n80/n82/n98）为 accepted 的走廊内序列/边界错配，是 D 的核心靶。

**D 验收分母 = 10（仍失败夹具）；其中 n88 计为允许失败。** 按 issue #18“仍失败夹具中
正确重接 ≥80%”，D 需在 {n24,n28,n41,n53,n60,n80,n82,n93,n98} 9 条中正确重接约 8 条
（n88 作允许失败）。回归控制不变：n74 不得降级、60 条正确金标回归 <2%、副文本/注释/噪声区
零新增配对。

> 说明：本重验的逐条文本级判定比 issue-17 报告的 100 条“同坐标 accepted-overlap”聚合口径更细。
> 聚合口径当时报“6 条旧错配降级、无错配重新进入”；逐条看，11 条旧错配链有 8 条已不在原坐标被
> 接受（n11 重接正确、n53/n93 降级、n24/n80/n82/n98/n88 迁到新的错/邻接落点），3 条（n28/n41/n60）
> 原错链未变。两者不矛盾，细口径用于确定 D 的逐条靶。
