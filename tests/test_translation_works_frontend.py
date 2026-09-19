"""译本对照页（作品—版本—统一阅读器）的前端守卫。

锁住产品决定里容易被「顺手改回去」的点：入口门控、状态措辞、最多勾两个、
删除作品先隐藏后提交可撤销、批量加入走单一接口、DOM 不拼 HTML。
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

    def test_at_most_two_versions_and_third_replaces_the_oldest(self) -> None:
        body = _function_body(WORKS_JS, "function togglePick(group, sourceId)")
        self.assertIn("pick.push(sourceId);", body)
        self.assertIn("if (pick.length > 2) pick.shift();", body)

    def test_compare_bar_actions_follow_pair_status(self) -> None:
        actions = _function_body(WORKS_JS, "function pairActions(group, a, b, status, compact)")
        self.assertIn("generate(compact ? '生成' : '生成对齐', false", actions)
        self.assertIn("generate('重新对齐', true", actions)
        self.assertIn("generate('生成直接对齐', false", actions)
        self.assertIn("'对照阅读'", actions)
        # 禁用必须给出原因。
        self.assertIn("el('span', {className: 'tw-state', text: reason})", actions)

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
        for fragment in ("'作品名称'", "type: 'radio'", "'版本名'", "'移出'", "'添加版本：搜索文献库'", "'对齐'", "'删除作品'"):
            self.assertIn(fragment, sheet)

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

    def test_startup_defers_the_heavy_overview_fetch(self) -> None:
        """总览是启动链路里唯一的秒级接口，不得回到启动关键路径上。"""

        # 启动只拉毫秒级的可用性；整页数据延后后台预取。
        self.assertNotRegex(INIT_JS, r"^MEFinder\.works\.load\(\);", re.MULTILINE)
        self.assertIn("MEFinder.works.refreshAvailability();", INIT_JS)
        self.assertRegex(
            INIT_JS, r"setTimeout\(function \(\) \{ MEFinder\.works\.load\(\); \}, \d+\);"
        )

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
