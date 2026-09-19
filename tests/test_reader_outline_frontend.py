"""Chapter clicks synchronize once and ignore obsolete navigation responses."""

import shutil
import unittest

from tests import test_reader_comparison_state as reader_test


SETUP = """
const state={open:true,sourceId:'A',outline:{items:[
 {item_index:10,anchor_id:'a10',char_start:4,char_end:9},
 {item_index:20,anchor_id:'a20',char_start:8,char_end:13}]},
 outlineJumpSerial:0,outlineNavigating:false,citationRequestSerial:0,
 comparison:{open:true,targetSourceId:'B',autoFollow:false,locateSerial:0,requestSerial:0,
 followTimer:null,lastSourceRange:'',highlights:new Map([['old',[]]]),indexHighlights:new Map()},
 elements:{viewport:{focus:()=>{}},pending:{hidden:true},comparisonViewport:{scrollTop:321},
 alert:{textContent:''}}};
const closeMenus=()=>{},clearLinkedSelection=()=>{},prepareHighlights=()=>{};
const setAlert=text=>{state.elements.alert.textContent=text;};
const renderComparisonWindow=()=>{state.elements.comparisonViewport.scrollTop=0;};
const global={clearTimeout:()=>{}};
const calls=[];
"""


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ReaderOutlineTests(unittest.TestCase):
    run_js = reader_test.ReaderComparisonStateTests.run_js

    def test_chapter_synchronizes_exact_span_even_when_follow_is_paused(self):
        self.run_js([("  async function jumpToChapter(", "  function pickerFor(")], SETUP + """
const loadWindow=async()=>true;
const locateInAlignedVersion=async(target,selection)=>{
 state.comparison.locateSerial++; calls.push({target,selection}); return true;
};
(async()=>{
 await jumpToChapter(0);
 assert.deepEqual(calls,[{target:'B',selection:{startIndex:10,endIndex:10,startOffset:4,endOffset:9}}]);
 assert.equal(state.comparison.autoFollow,false);
 assert.equal(state.outlineNavigating,false);
})();
""")

    def test_rapid_chapter_clicks_or_close_do_not_apply_old_target(self):
        self.run_js([("  async function jumpToChapter(", "  function pickerFor(")], SETUP + """
const loads=[];const loadWindow=()=>new Promise(resolve=>loads.push(resolve));
const locateInAlignedVersion=async(target,selection)=>{
 state.comparison.locateSerial++;calls.push(selection.startIndex);return true;
};
(async()=>{
 const first=jumpToChapter(0),second=jumpToChapter(1);
 loads[1](true);await second;loads[0](true);await first;
 assert.deepEqual(calls,[20]);
 const third=jumpToChapter(0);state.open=false;state.outlineJumpSerial++;
 loads[2](true);await third;assert.deepEqual(calls,[20]);
})();
""")

    def test_unmatched_chapter_keeps_right_position_and_clears_old_highlight(self):
        self.run_js([("  async function jumpToChapter(", "  function pickerFor(")], SETUP + """
const loadWindow=async()=>true;
const locateInAlignedVersion=async()=>{state.comparison.locateSerial++;setAlert('没有对应段落');return false;};
(async()=>{
 await jumpToChapter(0);
 assert.equal(state.elements.comparisonViewport.scrollTop,321);
 assert.equal(state.comparison.highlights.size,0);
 assert.match(state.elements.alert.textContent,/没有对应段落/);
})();
""")

    def test_late_outline_from_previous_book_is_discarded(self):
        self.run_js([("  async function loadOutline(", "  async function jumpToChapter(")], """
const state={open:true,sourceId:'A',outline:{items:null,loading:false,error:''},openMenu:''};
const config={outlineEndpoint:'/outline'};let finish;
const readJSON=()=>new Promise(resolve=>finish=resolve);
const renderMenu=()=>{};
(async()=>{
 const request=loadOutline();const current={items:null,loading:false,error:''};
 state.sourceId='B';state.outline=current;
 finish({entries:[{title:'wrong book'}]});await request;
 assert.deepEqual(state.outline,current);assert.equal(current.items,null);
})();
""")
