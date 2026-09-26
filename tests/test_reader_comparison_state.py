"""Execute reader functions with controlled network completion and scroll state."""

import json
import shutil
import subprocess
import unittest

from tests.reader_source import alignment_jobs_source, reader_js_source

READER = reader_js_source()


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ReaderComparisonStateTests(unittest.TestCase):
    def test_body_is_loaded_before_work_metadata_and_overview_is_scoped(self):
        body = READER[READER.index("  async function openReader("):READER.index("  async function goTo(")]
        self.assertLess(body.index("await loadWindow("), body.index("loadWorkContext(sourceId)"))
        self.assertLess(body.index("await loadWindow("), body.index("loadAlignmentTargets(sourceId)"))
        self.run_js([("  async function loadWorkContext(", "  /* ── 自绘下拉")], """
const state={sourceId:'A',workRequestSerial:0,comparison:{open:false}};
const config={groupsEndpoint:'/groups',overviewEndpoint:'/overview',currentJobEndpoint:'/job'};
const urls=[];
const readJSON=async url=>{urls.push(url);return {document_groups:[],works:[],running:false};};
const loadAvailability=async()=>{},renderToolbar=()=>{};
(async()=>{
 await loadWorkContext('A');
 assert.deepEqual(urls,['/groups','/overview?include_statistics=0&source_id=A','/job']);
})();
""")

    def run_js(self, functions, script, prelude=""):
        bodies = []
        for start, end in functions:
            offset = READER.index(start)
            bodies.append(READER[offset:READER.index(end, offset)])
        program = (
            "const assert = require('assert/strict');\n" + prelude + "\n"
            + "\n".join(bodies) + "\n" + script
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", program],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_obsolete_open_cannot_restore_closed_or_replaced_comparison(self):
        for replacement in ("", "D"):
            with self.subTest(replacement=replacement):
                self.run_js([("  function openComparisonWith(", "  function markComparisonOpen(")], """
const state = {open:true, sourceId:'A', comparison:{open:true,targetSourceId:'B',locateSerial:0}};
const pairInfo = () => ({status:'direct'}), pairReadable = () => true;
const rememberComparisonTarget = () => {}, showPendingPane = () => {};
const sourceCenterRange = () => ({}), alignmentTargetName = () => '';
let finish; const pending = new Promise(resolve => {finish=resolve;});
const locateInAlignedVersion = () => {state.comparison.locateSerial++; return pending;};
const shown = []; const showComparison = payload => shown.push(payload.targetSourceId);
const setAlert = () => {};
openComparisonWith('C');
state.comparison.locateSerial++;
state.comparison.targetSourceId = REPLACEMENT;
state.comparison.open = !!REPLACEMENT;
finish(false);
setImmediate(() => assert.deepEqual(shown, []));
""".replace("REPLACEMENT", json.dumps(replacement)))

    JOB_WATCH = ("  /* ── 对齐任务：监听在 MEFinderAlignmentJobs", "  /* ── 新窗口")
    # 阅读器片段读 global.MEFinderAlignmentJobs：先把真实任务服务装进受控的 global。
    JOB_SERVICE_PRELUDE = (
        "const global={setTimeout:resolve=>resolve()};\n"
        "new Function('window', " + json.dumps(alignment_jobs_source()) + ")(global);"
    )

    def test_finished_job_does_not_invalidate_the_next_books_context(self):
        self.run_js([
            ("  async function loadWorkContext(", "  /* ── 自绘下拉"),
            ("  async function applyAlignmentJobEnd(", "  /* ── 新窗口"),
        ], """
const state={open:true,sourceId:'A',workRequestSerial:0,work:{groupId:''},comparison:{open:false}};
const config={groupsEndpoint:'/groups',overviewEndpoint:'/overview',currentJobEndpoint:'/current'};
const renderToolbar=()=>{},loadAvailability=async()=>{},notify=()=>{},setAlert=()=>{};
const refreshComparisonAfterStatusChange=()=>{};
let finishTargets;const loadAlignmentTargets=()=>new Promise(r=>finishTargets=r);
const reads=[];const readJSON=url=>new Promise(resolve=>reads.push({url,resolve}));
(async()=>{
 const ended=applyAlignmentJobEnd({outcome:'ok',meta:{origin:'works',groupId:'GA'}});
 state.sourceId='B';
 const newWork=loadWorkContext('B');
 finishTargets(); await new Promise(setImmediate);
 const groups={document_groups:[{document_group_id:'GB',title:'B work',members:[{source_file_id:'B'}]}]};
 reads.forEach(x=>x.resolve(x.url==='/groups'?groups:{works:[],running:false}));
 await Promise.all([ended,newWork]);
 assert.equal(state.work.groupId,'GB','old job must not invalidate the new book context');
 assert.ok(!reads.some(x=>x.url.endsWith('source_id=A')));
 // 关闭期间结束的刷新也不能发起后续作品请求。
 const closing=applyAlignmentJobEnd({outcome:'ok',meta:{origin:'works',groupId:'GB'}});
 state.open=false; const readCount=reads.length;
 finishTargets(); await closing;
 assert.equal(reads.length,readCount);
})();
""")

    def test_completed_alignment_invalidates_links_and_relocates_open_pair(self):
        self.run_js([
            self.JOB_WATCH,
            ("  function refreshComparisonAfterStatusChange(", "  /* ── 对齐任务"),
            ("  function loadLinkWindow(", "  // 低置信"),
        ], """
const old = {key:'A:B:0:0',items:[{target_segment_ids:['old-target']}]};
const state = {open:true, sourceId:'A', work:{groupId:'G'},
  items:new Map([[0,{}]]),
  elements:{pending:{hidden:true}},comparison:{open:true,targetSourceId:'B',lastSourceRange:'old'},
  links:old,linkRequestSerial:0};
const config={linksEndpoint:'/links'};
global.MEFinderAlignmentJobs.configure({fetch:async()=>({status:200,ok:true,json:async()=>({ok:true})})});
const pairKey=(a,b)=>[a,b].sort().join('|');
let notices=0;
const notify=()=>{notices++;}, setAlert=()=>{}, loadAlignmentTargets=async()=>{}, loadWorkContext=async()=>{};
const renderToolbar=()=>{},updateComparisonNotice=()=>{},renderFlags=()=>{},clearLinkedSelection=()=>{};
let reads=0, relocated=0;
const readJSON=async()=>{reads++;return {links:[]};};
const openComparisonWith=()=>{relocated++;loadLinkWindow();};
(async()=>{
 global.MEFinderAlignmentJobs.watch('J',{origin:'reader',groupId:'G',key:'A|B'});
 await new Promise(resolve=>setImmediate(resolve));
 await new Promise(resolve=>setImmediate(resolve));
 loadLinkWindow();
 assert.notEqual(state.links,old);
 assert.ok(reads>0);
 assert.equal(relocated,1);
 // 发起方是阅读器，结局提示由阅读器给出，且只给一次。
 assert.equal(notices,1);
 assert.equal(global.MEFinderAlignmentJobs.running(),null);
})();
""", prelude=self.JOB_SERVICE_PRELUDE)

    def test_deep_link_carries_the_comparison_pane(self) -> None:
        """会话记录包含右栏：刷新或重开独立窗口后对照不会丢。"""

        self.run_js([("  function parseReaderDeepLink(", "  function deepLinkRange(")], """
const global={location:null};
const codePointLength=value=>Array.from(value).length;
const inferIndexFromAnchor=()=>0;
const parseDeepLinkOffset=()=>null;
const read=search=>parseReaderDeepLink({pathname:'/reader',search:search});
assert.equal(read('?source=A&page=A-P1&c=B').compareWith,'B');
assert.equal(read('?source=A&page=A-P1').compareWith,'');
// 对照目标不能是自己，也不能给两个。
assert.equal(read('?source=A&page=A-P1&c=A'),null);
assert.equal(read('?source=A&page=A-P1&c=B&c=C'),null);
assert.equal(read('?source=A&page=A-P1&c=%20'),null);
""")

    def test_closing_hands_the_position_it_just_saved_to_the_host(self) -> None:
        """位置只写一次，宿主直接采用；不再清空缓存后靠定时器重新查询。"""

        self.run_js([
            ("  /* ── 阅读会话", "  function openInNewWindow("),
            ("  function saveReadingPositionNow(", "  function scheduleReadingPositionSave("),
        ], """
const state={open:true,positionTimer:null,sourceId:'A',title:'T',currentIndex:7,
 currentAnchorId:'A-P8',work:{groupId:'G'},items:new Map([[7,{}]]),
 comparison:{open:true,targetSourceId:'B'}};
const config={readingPositionEndpoint:'/pos'};
const global={clearTimeout:()=>{}};
const posted=[];
const postJSON=(url,body)=>{posted.push(body);return Promise.resolve({});};
const saved=saveReadingPositionNow();
assert.deepEqual(saved,{document_group_id:'G',position:{
 left_source_file_id:'A',right_source_file_id:'B',item_index:7,char_offset:0}});
// 交回宿主的形状与写出去的一致，宿主无需再查一次。
assert.deepEqual(posted[0],Object.assign({document_group_id:'G'},saved.position));
state.work.groupId='';
assert.equal(saveReadingPositionNow(),null);
""")

    def test_follow_response_does_not_scroll_source_back_to_search_hit(self):
        self.run_js([("  function showComparison(", "  function closeComparison(")], """
const comparison={targetSourceId:'B',indexHighlights:new Map(),currentIndex:0};
const state={comparison,elements:{content:{querySelector:()=>({})}}};
let sourceMoves=0, targetMoves=0;
const visibleSourceHighlightRange=()=>({startIndex:0});
const positionSourceTarget=()=>{sourceMoves++;};
const markComparisonOpen=()=>{},showPendingPane=()=>{},rememberComparisonTarget=()=>{};
const clampInteger=value=>value, setComparisonHighlights=()=>{},updateComparisonNotice=()=>{};
const updateComparisonControls=()=>{},renderToolbar=()=>{},loadLinkWindow=()=>{},noteReadingSessionChanged=()=>{};
const loadComparisonWindow=()=>{targetMoves++;return true;};
showComparison({targetSourceId:'B',targetIndex:5,pageMatchSpans:[]},'B');
assert.equal(sourceMoves,0);
assert.equal(targetMoves,1);
""")
