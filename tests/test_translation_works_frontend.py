"""译本对照页（作品—版本—统一阅读器）的前端守卫。

锁住产品决定里容易被「顺手改回去」的点：入口门控、状态措辞、每对版本常显对齐状态、
换模型后全部重新对齐、删除作品先隐藏后提交可撤销、批量加入走单一接口、DOM 不拼 HTML。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "me_finder" / "static"
WORKS_JS = (STATIC / "js" / "35-works.js").read_text(encoding="utf-8")
WORKS_CSS = (STATIC / "css" / "45-works.css").read_text(encoding="utf-8")
LIBRARY_JS = (STATIC / "js" / "30-library.js").read_text(encoding="utf-8")
INIT_JS = (STATIC / "js" / "90-init.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "src" / "me_finder" / "templates" / "index.html").read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    following = re.search(r"\n  (?:async )?function ", source[start + len(signature):])
    end = start + len(signature) + following.start() if following else len(source)
    return source[start:end]


class TranslationWorksFrontendTests(unittest.TestCase):
    def test_sidebar_entry_follows_library_and_is_gated(self) -> None:
        library = INDEX_HTML.index('data-page="library"')
        works = INDEX_HTML.index('data-page="works"')
        imports = INDEX_HTML.index('data-page="import"')
        self.assertLess(library, works)
        self.assertLess(works, imports)
        entry = INDEX_HTML[works:INDEX_HTML.index("</button>", works)]
        self.assertIn("hidden", entry)
        self.assertIn('<span class="sidebar-item-tag" hidden>只读</span>', entry)
        visible = _function_body(WORKS_JS, "function entryVisible()")
        self.assertIn("state === 'ready' || state === 'model_missing'", visible)
        self.assertIn("return hasAnyAlignment();", visible)
        self.assertIn("works.availability.state === 'unavailable' && hasAnyAlignment()",
                      _function_body(WORKS_JS, "function isReadOnly()"))

    def test_old_group_ui_is_gone_from_the_library(self) -> None:
        for removed in ("group-manage-modal", "library-group-scope", "library-join-group", "管理作品组"):
            self.assertNotIn(removed, INDEX_HTML)
        self.assertIn('id="library-assign-work-btn"', INDEX_HTML)
        self.assertIn(">加入作品…</button>", INDEX_HTML)
        for removed in ("renderDocumentGroupManager", "openManageDocumentGroups", "renderJoinGroupMenu"):
            self.assertNotIn(removed, LIBRARY_JS)
        self.assertIn("MEFinder.works.open(", LIBRARY_JS)
        self.assertIn("MEFinder.works.readFromLibrary(", LIBRARY_JS)

    def test_status_wording_is_factual_and_never_claims_accuracy(self) -> None:
        status = _function_body(WORKS_JS, "function statusLine(group, status)")
        for phrase in (
            "'直接对齐'", "'已匹配段落 '", "' 处待检查'", "'间接关联'", "换算，未直接对齐",
            "'需重新对齐', '模型已更换，旧结果可读'", "'尚未对齐'", "'生成中'",
            "不代表对应一定准确",
        ):
            self.assertIn(phrase, status)
        combined = WORKS_JS + (STATIC / "reader.js").read_text(encoding="utf-8")
        self.assertNotIn("覆盖率", combined)
        self.assertNotIn("准确率", combined)
        # 没有真实批次进度，不显示百分比。
        self.assertNotRegex(status, r"生成中[^']*%")

    def test_every_pair_shows_its_alignment_state_without_picking(self) -> None:
        # 对齐状态逐对常显：不再要求先勾选两个版本、也没有底部对照栏。
        for removed in ("togglePick", "currentPick", "compareBar", "tw-compare-bar"):
            self.assertNotIn(removed, WORKS_JS)
            self.assertNotIn(removed, WORKS_CSS)
        pane = _function_body(WORKS_JS, "function workPane()")
        self.assertLess(pane.index("pairSection(group)"), pane.index("versionTable(group)"))
        section = _function_body(WORKS_JS, "function pairSection(group)")
        self.assertIn("workPairs(group).forEach", section)
        self.assertIn("statusLine(group, status)", section)
        self.assertIn("对照阅读按对齐结果逐段跟随", section)
        # 禁用必须给出原因：全区说明一次。
        self.assertIn("el('span', {className: 'tw-state', text: generateBlockedReason()})", section)
        # 含基准版本的对排在前。
        self.assertIn("p.withBase", _function_body(WORKS_JS, "function workPairs(group)"))

    def test_pair_actions_follow_pair_status(self) -> None:
        actions = _function_body(WORKS_JS, "function pairActions(group, a, b, status)")
        self.assertIn("generate('生成对齐', false", actions)
        self.assertIn("generate('重新对齐', true", actions)
        self.assertIn("generate('生成直接对齐', false", actions)
        self.assertIn("'对照阅读'", actions)
        self.assertIn("title: blocked ? generateBlockedReason() : null", actions)
        # 行内不出现第二个主按钮（DESIGN.md §5：一组操作至多一个主按钮）。
        self.assertNotIn("'primary'", actions)

    def test_stale_alignments_can_be_realigned_in_one_batch(self) -> None:
        notice = _function_body(WORKS_JS, "function realignNotice()")
        self.assertIn("'全部重新对齐'", notice)
        self.assertIn("staleQueueItems(visibleGroups())", notice)
        self.assertIn("'停止'", notice)
        # 只统计直接对齐；间接关联随两段直接对齐一起更新。
        stale = _function_body(WORKS_JS, "function staleDirectPairs(group)")
        self.assertIn("pair.status === 'direct' && !!pair.stale_reason", stale)
        # 沿用原对齐方向重跑，并强制重算。
        items = _function_body(WORKS_JS, "function staleQueueItems(groups)")
        self.assertIn("run.pivot_source_file_id, run.target_source_file_id", items)
        self.assertIn("startJob(group, item.pivot, item.target, true)",
                      _function_body(WORKS_JS, "async function runRealignQueue()"))
        self.assertIn("showAppConfirm", _function_body(WORKS_JS, "async function realignPairs(items, scope)"))
        # 队列中逐个任务不弹提示，结束时汇总一次。
        watch = _function_body(WORKS_JS, "async function onAlignmentJobEnd(event)")
        self.assertLess(watch.index("advanceRealignQueue(event.outcome"), watch.index("对齐已生成"))
        # 任务监听只有一份（在 reader.js 里），作品页只认领并订阅，不再自己轮询。
        self.assertNotIn("/api/text-alignments/status", WORKS_JS)
        self.assertIn("global.MEFinderReader.alignmentJobs.subscribe(", WORKS_JS)
        self.assertIn("jobs.watch(jobId, {", WORKS_JS)
        # 提示只由发起方给出，阅读器发起的任务由阅读器报告。
        self.assertIn("event.meta.origin === 'works'", watch)
        # 关闭阅读器：直接采用交回的位置，不清空缓存、不用定时器重查。
        close = _function_body(WORKS_JS, "function onReaderOpenChange(open, savedPosition)")
        self.assertIn("works.positions[savedPosition.document_group_id] = savedPosition.position", close)
        self.assertNotIn("setTimeout", close)
        self.assertNotIn("works.positions = {}", WORKS_JS)
        # 换模型后作品页的状态快照必须失效。
        settings = (STATIC / "js" / "60-settings.js").read_text(encoding="utf-8")
        self.assertIn("global.MEFinder.works.invalidate();", settings)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_realign_queue_runs_pairs_in_order_and_stops_on_cancel(self) -> None:
        start = WORKS_JS.index("  function staleDirectPairs(group) {")
        end = WORKS_JS.index("  function refreshViews() {", start)
        script = r"""
const assert=require('assert/strict');
const pairKey=(a,b)=>[a,b].sort().join('|');
const groups={G:{document_group_id:'G',title:'W',members:[],alignments:[{pivot_source_file_id:'B',target_source_file_id:'A'}]}};
const works={queue:null,running:null,pairsByGroup:{G:{'A|B':{source_file_ids:['A','B'],status:'direct',stale_reason:'model_changed'},
 'A|C':{source_file_ids:['A','C'],status:'direct',stale_reason:null},'B|C':{source_file_ids:['B','C'],status:'indirect',stale_reason:'model_changed'}}}};
const groupById=id=>groups[id], pivotFor=(g,a,b)=>[a,b], canGenerate=()=>true, refreshViews=()=>{};
const toasts=[], started=[];
const showToast=m=>toasts.push(m), showAppConfirm=async()=>true, cancelAlignment=()=>{};
const startJob=async(g,p,t,force)=>{started.push([p,t,force]);works.running={};return true;};
(async()=>{
 const items=staleQueueItems([groups.G]);
 assert.deepEqual(items.map(i=>[i.pivot,i.target]),[['B','A']],'only stale direct pairs, original direction');
 await realignPairs(items.concat([{groupId:'G',pivot:'A',target:'C'}]),'x');
 assert.deepEqual(started,[['B','A',true]]);
 assert.equal(queuedPair('G','C','A'),true);
 works.running=null;advanceRealignQueue('ok');await new Promise(r=>setImmediate(r));
 assert.deepEqual(started[1],['A','C',true]);
 works.running=null;advanceRealignQueue('cancelled');await new Promise(r=>setImmediate(r));
 assert.equal(works.queue,null);
 assert.equal(started.length,2);
 assert.match(toasts[0],/已停止重新对齐，完成 1\/2 组/);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", WORKS_JS[start:end] + script],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deleting_a_work_is_deferred_and_undoable(self) -> None:
        body = _function_body(WORKS_JS, "function deleteWork(group)")
        self.assertIn("works.hiddenGroupIds.add(groupId)", body)
        self.assertIn("'撤销'", body)
        self.assertNotIn("showAppConfirm", body)
        undo_index = body.index("'撤销'")
        post_index = body.index("postJSON('/api/document-groups/delete'")
        self.assertLess(undo_index, post_index)
        self.assertIn("async function ()", body[undo_index:post_index])

    def test_assigning_books_uses_the_single_move_members_call(self) -> None:
        dialog = _function_body(WORKS_JS, "function openAssignDialog(sourceIds, options)")
        self.assertIn("'/api/document-groups/move-members'", dialog)
        self.assertIn("'前往译本对照'", dialog)
        self.assertIn("第一本作为基准", dialog)
        self.assertIn("已在其他作品中，确认后会移到这里", dialog)
        self.assertIn("role: 'option'", dialog)
        self.assertIn("'aria-haspopup': 'listbox'", dialog)
        self.assertNotIn("add-member", dialog)

    def test_same_title_review_offers_three_explicit_choices(self) -> None:
        review = _function_body(WORKS_JS, "function openMergeReview(sources)")
        for label in ("'不是同一作品'", "'稍后'", "'归为一部作品'"):
            self.assertIn(label, review)
        self.assertIn("'/api/translation-works/dismiss-suggestion'", review)
        self.assertIn("is-different", review)

    def test_manage_sheet_covers_every_maintenance_action(self) -> None:
        sheet = _function_body(WORKS_JS, "function renderSheet()")
        for fragment in ("'作品名称'", "type: 'radio'", "'版本名'", "'移出'", "'添加版本：搜索文献库'", "'删除作品'"):
            self.assertIn(fragment, sheet)

    def test_body_range_entry_sits_on_the_pair_and_needs_no_compute_component(self) -> None:
        actions = _function_body(WORKS_JS, "function pairActions(group, a, b, status) {")
        self.assertIn("openBodyRangeDialog(group, a, b)", actions)
        self.assertIn("'正文范围'", actions)
        # 查看/修改范围只读已入库文本；缺计算组件时只挡提交，不挡入口。
        entry = actions[actions.index("'正文范围'"):]
        self.assertNotIn("blocked", entry[:entry.index("}));")])

    def test_body_range_submits_both_ranges_once_and_keeps_the_last_segment(self) -> None:
        dialog = _function_body(WORKS_JS, "function openBodyRangeDialog(group, a, b) {")
        # 界面的「结尾」是最后一段，库内是半开区间：+1 才不漏末段。
        self.assertIn("ranges[side.side] = [side.start, side.end + 1];", dialog)
        self.assertIn("startJob(group, order[0], order[1], true, ranges, sets)", dialog)
        self.assertIn("payload.expected_segment_set_ids = expectedSegmentSetIds;", WORKS_JS)
        # 两个范围一起提交，未修改的一本沿用当前显示范围。
        self.assertIn("state.sides.forEach(function (side) {", dialog)
        # 只有设置按钮改范围；点选只改当前选中段。
        self.assertIn("function setEdge(side, edge) {", dialog)
        self.assertIn("side.selected = index; draw(side);", dialog)
        # 单本确认环节已删除，不得回流。
        for removed in ("确认这本", "已确认这本", "范围待确认", "范围已确认", "confirmed"):
            self.assertNotIn(removed, dialog)
        # 结尾早于开头必须挡住提交，不偷偷移动另一端。
        self.assertIn("function invalid(side) { return side.end < side.start; }", dialog)
        self.assertIn("!state.sides.some(invalid)", dialog)

    def test_failed_body_range_submission_keeps_the_draft_for_retry(self) -> None:
        dialog = _function_body(WORKS_JS, "function openBodyRangeDialog(group, a, b) {")
        self.assertIn("works.rangeDrafts[draftKey] = {sets: sets, ranges: ranges};", dialog)
        # 草稿只在同一份分段数据上恢复，运行成功后由 clearRangeDraft 清掉。
        self.assertIn("draft.sets[side.side] === side.segment_set_id", dialog)
        self.assertIn("clearRangeDraft(running);", WORKS_JS)

    def test_body_range_css_follows_the_pair_layout_and_stacks_when_narrow(self) -> None:
        self.assertIn("Hallmark · component: 正文范围检查与修改", WORKS_CSS)
        self.assertIn(
            ".tw-range-pair { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);",
            WORKS_CSS,
        )
        narrow = WORKS_CSS[WORKS_CSS.index("@media (max-width: 860px)"):]
        self.assertIn(".tw-range-pair { grid-template-columns: minmax(0, 1fr);", narrow)

    def test_dom_is_built_without_html_strings_and_css_uses_tokens(self) -> None:
        self.assertNotIn("innerHTML", WORKS_JS)
        self.assertNotIn("insertAdjacentHTML", WORKS_JS)
        self.assertNotRegex(WORKS_CSS, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("transition: all", WORKS_CSS)
        self.assertIn("Hallmark · component: translation works", WORKS_CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)", WORKS_CSS)
        self.assertIn(".tw-btn:active:not(:disabled) { transform: scale(0.97); }", WORKS_CSS)

    def test_ui_copy_has_no_trailing_full_stops(self) -> None:
        literals = re.findall(r"'([^'\n]*[一-鿿][^'\n]*)'", WORKS_JS)
        self.assertTrue(literals)
        self.assertEqual([text for text in literals if text.endswith("。")], [])

    def test_startup_loads_lightweight_status_without_a_fixed_delay(self) -> None:
        self.assertRegex(INIT_JS, re.compile(r"^MEFinder\.works\.load\(\);", re.MULTILINE))
        self.assertNotIn("setTimeout(function () { MEFinder.works.load();", INIT_JS)
        overview = _function_body(WORKS_JS, "async function loadGroupsAndOverview()")
        self.assertIn("requestJSON('/api/translation-works/overview?include_statistics=0')", overview)
        library = _function_body(LIBRARY_JS, "async function loadLibrary(force)")
        self.assertLess(library.index("renderLibraryList();"), library.index("await loadDocumentGroups();"))

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_pair_statistics_are_scoped_and_obsolete_results_are_ignored(self) -> None:
        script = r"""
const assert=require('assert/strict');
const group={document_group_id:'G'}, status={source_file_ids:['A','B'],status:'direct'};
const works={pairsByGroup:{G:{'A:B':status}},currentId:'G'}, currentPage='works';
const pairKey=(a,b)=>[a,b].sort().join(':');
const urls=[],errors=[];let release, renders=0;
const requestJSON=url=>{urls.push(url);return new Promise(resolve=>{release=resolve;});};
const showToast=message=>errors.push(message), render=()=>{renders++;}, invalidate=()=>{};
(async()=>{
 const pending=loadPairStatistics(group,status);
 assert.equal(status.statisticsState,'loading');
 assert.deepEqual(urls,['/api/translation-works/overview?source_id=A&target_id=B']);
 const replacement={source_file_ids:['A','B'],status:'none'};
 works.pairsByGroup.G['A:B']=replacement;
 release({works:[{document_group_id:'G',pairs:[{source_file_ids:['A','B'],review_count:7}]}]});
 await pending;assert.equal(renders,0);assert.equal(replacement.review_count,undefined);
 const fresh=loadPairStatistics(group,replacement);
 release({works:[{document_group_id:'G',pairs:[{source_file_ids:['A','B'],status:'direct',review_count:2}]}]});
 await fresh;assert.equal(replacement.review_count,2);assert.equal(renders,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", _function_body(WORKS_JS, "async function loadPairStatistics(group, status)") + script],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_load_is_gated_and_deduplicated(self) -> None:
        load_body = _function_body(WORKS_JS, "function load(options)")
        # 已加载过不再整页重拉；进行中共用同一请求（切换页面不重付秒级查询）。
        self.assertIn("if (works.loaded && !options.force)", load_body)
        self.assertIn("if (currentPage === 'works')", load_body)
        self.assertIn("render();", load_body)
        self.assertIn("if (loadInflight) return loadInflight;", load_body)
        # 库结构变化（删除文献、恢复备份）必须能重置加载门。
        self.assertIn("works.loaded = false;", _function_body(WORKS_JS, "function invalidate()"))
        self.assertIn("library_changed", WORKS_JS)
        self.assertIn("invalidate: invalidate,", WORKS_JS)
        self.assertIn("invalidate();", _function_body(WORKS_JS, "function onReaderOpenChange(open, savedPosition)"))

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_close_handoff_wins_over_an_inflight_position_read(self) -> None:
        bodies = "\n".join(_function_body(WORKS_JS, signature) for signature in (
            "async function loadPosition(groupId)",
            "function onReaderOpenChange(open, savedPosition)",
        ))
        script = r"""
const assert=require('assert/strict');
const works={positions:{},currentId:'G'},currentPage='works';
const document={documentElement:{classList:{toggle:()=>{}}}};
let sidebarBeforeReader=null;
const render=()=>{},invalidate=()=>{};
let finishGet,failGet;
const requestJSON=()=>new Promise((resolve,reject)=>{finishGet=resolve;failGet=reject;});
(async()=>{
 for (const failed of [false,true]) {
  delete works.positions.G;
  const loading=loadPosition('G');
  const fresh={left_source_file_id:'A',right_source_file_id:'B',item_index:7,char_offset:0};
  onReaderOpenChange(false,{document_group_id:'G',position:fresh});
  if (failed) failGet(new Error('old request failed'));
  else finishGet({position:{item_index:1,right_source_file_id:null}});
  await loading;
  assert.equal(works.positions.G,fresh,'an obsolete read must not replace the close handoff');
 }
 // 正常首次读取仍可填充缓存。
 delete works.positions.G;
 const loading=loadPosition('G');
 finishGet({position:{item_index:3}});await loading;
 assert.equal(works.positions.G.item_index,3);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", bodies + script],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_load_retries_failure_and_refreshes_invalidated_inflight_data(self) -> None:
        start = WORKS_JS.index("  var loadInflight = null;")
        end = WORKS_JS.index("  async function refreshAvailability()", start)
        script = r"""
const assert = require('assert/strict');
const works = {loaded:false,loadSerial:0,currentId:'',hiddenGroupIds:new Set()};
const currentPage = 'library', libraryStore = {loaded:false}, global = {MEFinder:{}};
const showToast=()=>{}, renderSidebarEntry=()=>{}, syncLibraryAssignButton=()=>{};
const visibleGroups=()=>[], groupById=()=>null;
const loadAvailability=async()=>{}, ensureCatalog=async()=>{};
let queries=0, fail=true, release;
const loadGroupsAndOverview=()=>{
 queries++;
 if(fail) return Promise.reject(new Error('offline'));
 return new Promise(resolve=>{release=resolve;});
};
(async()=>{
 await load();
 assert.equal(works.loaded,false,'failed requests must remain retryable');
 fail=false;
 const pending=load(), duplicate=load();
 assert.equal(pending,duplicate,'deduplicate an active request');
 invalidate();
 release();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(queries,3,'invalidation during loading must fetch a fresh snapshot');
 assert.equal(works.loaded,false,'stale snapshot must not become loaded');
 release();await pending;
 assert.equal(works.loaded,true);
 await load();assert.equal(queries,3,'successful snapshot should be reused');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", WORKS_JS[start:end] + script],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_module_parses(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--check", str(STATIC / "js" / "35-works.js")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
