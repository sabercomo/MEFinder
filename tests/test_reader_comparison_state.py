"""Execute reader functions with controlled network completion and scroll state."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


READER = (Path(__file__).resolve().parents[1] / "src/me_finder/static/reader.js").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ReaderComparisonStateTests(unittest.TestCase):
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

    def test_completed_alignment_invalidates_links_and_relocates_open_pair(self):
        self.run_js([
            ("  function refreshComparisonAfterStatusChange(", "  /* ── 新窗口"),
            ("  function loadLinkWindow(", "  // 低置信"),
        ], """
const old = {key:'A:B:0:0',items:[{target_segment_ids:['old-target']}]};
const state = {open:true, sourceId:'A', work:{groupId:'G'}, pollingJobId:'',
  generation:{jobId:'J',groupId:'G',key:'A|B'},items:new Map([[0,{}]]),
  elements:{pending:{hidden:true}},comparison:{open:true,targetSourceId:'B',lastSourceRange:'old'},
  links:old,linkRequestSerial:0};
const config={alignmentStatusEndpoint:'/status',linksEndpoint:'/links'};
const global={setTimeout:resolve=>resolve()};
const fetchFunction=()=>async()=>({status:200,ok:true,json:async()=>({ok:true})});
const pairKey=(a,b)=>[a,b].sort().join('|');
const notify=()=>{}, setAlert=()=>{}, loadAlignmentTargets=async()=>{}, loadWorkContext=async()=>{};
const renderToolbar=()=>{},updateComparisonNotice=()=>{},renderFlags=()=>{},clearLinkedSelection=()=>{};
let reads=0, relocated=0;
const readJSON=async()=>{reads++;return {links:[]};};
const openComparisonWith=()=>{relocated++;loadLinkWindow();};
(async()=>{
 await pollComparisonAlignment('J');
 loadLinkWindow();
 assert.notEqual(state.links,old);
 assert.ok(reads>0);
 assert.equal(relocated,1);
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
