"""Execute reader functions with controlled network completion and scroll state."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


READER = (Path(__file__).resolve().parents[1] / "src/me_finder/static/reader.js").read_text(encoding="utf-8")


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

    def run_js(self, functions, script):
        bodies = []
        for start, end in functions:
            offset = READER.index(start)
            bodies.append(READER[offset:READER.index(end, offset)])
        program = "const assert = require('assert/strict');\n" + "\n".join(bodies) + "\n" + script
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

    JOB_WATCH = ("  /* ── 对齐任务：后端只跑一个", "  /* ── 新窗口")

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
const config={alignmentStatusEndpoint:'/status',linksEndpoint:'/links'};
const global={setTimeout:resolve=>resolve()};
const fetchFunction=()=>async()=>({status:200,ok:true,json:async()=>({ok:true})});
const pairKey=(a,b)=>[a,b].sort().join('|');
let notices=0;
const notify=()=>{notices++;}, setAlert=()=>{}, loadAlignmentTargets=async()=>{}, loadWorkContext=async()=>{};
const renderToolbar=()=>{},updateComparisonNotice=()=>{},renderFlags=()=>{},clearLinkedSelection=()=>{};
let reads=0, relocated=0;
const readJSON=async()=>{reads++;return {links:[]};};
const openComparisonWith=()=>{relocated++;loadLinkWindow();};
(async()=>{
 watchAlignmentJob('J',{origin:'reader',groupId:'G',key:'A|B'});
 await new Promise(resolve=>setImmediate(resolve));
 await new Promise(resolve=>setImmediate(resolve));
 loadLinkWindow();
 assert.notEqual(state.links,old);
 assert.ok(reads>0);
 assert.equal(relocated,1);
 // 发起方是阅读器，结局提示由阅读器给出，且只给一次。
 assert.equal(notices,1);
 assert.equal(runningAlignmentJob(),null);
})();
""")

    def test_one_job_keeps_one_watcher_and_its_first_owner(self):
        """两处认领同一个任务时只轮询一次、只广播一次，提示归第一个认领者。"""

        self.run_js([self.JOB_WATCH], """
const state={open:false, sourceId:'A', work:{groupId:'G'}, comparison:{open:false}};
const config={alignmentStatusEndpoint:'/status'};
const global={setTimeout:resolve=>resolve()};
let polls=0;
const fetchFunction=()=>async()=>{polls++;return {status:200,ok:true,json:async()=>({ok:true})};};
let notices=0;
const notify=()=>{notices++;}, setAlert=()=>{};
const loadAlignmentTargets=async()=>{}, loadWorkContext=async()=>{};
const renderToolbar=()=>{},updateComparisonNotice=()=>{},clearLinkedSelection=()=>{};
const refreshComparisonAfterStatusChange=()=>{}, openComparisonWith=()=>{};
const events=[];
subscribeAlignmentJob(event=>{events.push(event);});
(async()=>{
 watchAlignmentJob('J',{origin:'works',groupId:'G',key:'A|B'});
 watchAlignmentJob('J',{origin:'reader',groupId:'G',key:'A|B'});
 assert.equal(runningAlignmentJob().origin,'works');
 await new Promise(resolve=>setImmediate(resolve));
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(polls,1);
 assert.equal(events.length,1);
 assert.equal(events[0].meta.origin,'works');
 assert.equal(events[0].outcome,'ok');
 // 作品页发起的任务不由阅读器报告结果。
 assert.equal(notices,0);
})();
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
const updateComparisonControls=()=>{},renderToolbar=()=>{},loadLinkWindow=()=>{},scheduleReadingPositionSave=()=>{};
const loadComparisonWindow=()=>{targetMoves++;return true;};
showComparison({targetSourceId:'B',targetIndex:5,pageMatchSpans:[]},'B');
assert.equal(sourceMoves,0);
assert.equal(targetMoves,1);
""")
