"""Protect the assembled reader's public contract before splitting its source."""

import shutil
import subprocess
import unittest

from src.me_finder.web_assets import HTML, READER_WINDOW_HTML
from tests.reader_source import reader_js_source


class ReaderPublicContractTests(unittest.TestCase):
    def test_both_windows_embed_the_same_reader_script_once(self) -> None:
        source = reader_js_source()
        self.assertTrue(source)
        self.assertEqual(HTML.count(source), 1)
        self.assertEqual(READER_WINDOW_HTML.count(source), 1)
        self.assertNotIn("//__READER_JS__", HTML)
        self.assertNotIn("//__READER_JS__", READER_WINDOW_HTML)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_public_methods_are_frozen_and_state_shape_is_stable(self) -> None:
        script = r"""
const assert = require('assert/strict');
const vm = require('vm');
const fs = require('fs');
const context = {
  document: {
    readyState: 'loading',
    documentElement: {dataset: {}},
    addEventListener() {}
  },
  location: {pathname: '/', search: ''},
  addEventListener() {}
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(0, 'utf8'), context);
const reader = context.MEFinderReader;
assert.deepEqual(Object.keys(reader).sort(), [
  'alignmentJobs', 'close', 'codePointToUtf16Index', 'configure',
  'copyCitation', 'destroy', 'getState', 'goTo', 'isOpen', 'open',
  'openForSearchResult', 'restore'
].sort());
assert.equal(Object.isFrozen(reader), true);
assert.deepEqual(Object.keys(reader.alignmentJobs).sort(), ['running', 'subscribe', 'watch']);
assert.equal(Object.isFrozen(reader.alignmentJobs), true);
assert.equal(reader.isOpen(), false);
assert.equal(reader.codePointToUtf16Index('A😀B', 2), 3);
assert.deepEqual(Object.keys(reader.getState()).sort(), [
  'alignmentTargetCount', 'citationRange', 'comparisonAutoFollow',
  'comparisonOpen', 'comparisonPending', 'comparisonTargetSourceId',
  'currentAnchorId', 'currentIndex', 'hasMore', 'hasPrevious',
  'lastDeepLink', 'lastPosition', 'linkCount', 'mountedItemCount',
  'nextStart', 'open', 'previousStart', 'sourceId', 'total',
  'windowEnd', 'windowStart', 'workGroupId'
].sort());
assert.equal(reader.getState().open, false);
assert.equal(reader.getState().mountedItemCount, 0);
assert.equal(reader.alignmentJobs.running(), null);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=reader_js_source(), capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_late_page_responses_cannot_restore_a_replaced_or_closed_book(self) -> None:
        script = r"""
const assert = require('assert/strict');
const vm = require('vm');
const fs = require('fs');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.dataset = {}; this.style = {};
    this.children = []; this.hidden = false; this.isConnected = true;
    this.classList = {add(){}, remove(){}, toggle(){}};
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
  remove() { this.isConnected = false; }
}
const body = new Element('body');
const document = {
  body, activeElement: body, readyState: 'loading',
  documentElement: {dataset: {}},
  createElement: tag => new Element(tag),
  createElementNS: (ns, tag) => new Element(tag),
  querySelector: () => null,
  addEventListener() {}
};
const context = {
  document, URLSearchParams, AbortController,
  location: {pathname:'/', search:''},
  history: {replaceState() {}},
  addEventListener() {}, setTimeout, clearTimeout
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(0, 'utf8'), context);
const reader = context.MEFinderReader;
const requests = [];
reader.configure({fetch(url) {
  return new Promise(resolve => requests.push({url, resolve}));
}});
const page = {ok:true, json:async()=>({items:[], total:0, start:0, has_more:false})};
(async()=>{
  const first = reader.open({sourceId:'A'});
  const second = reader.open({sourceId:'B'});
  assert.equal(requests.length, 2);
  requests[0].resolve(page);
  assert.equal(await first, false);
  assert.equal(reader.getState().sourceId, 'B');
  reader.close();
  requests[1].resolve(page);
  assert.equal(await second, false);
  assert.equal(reader.isOpen(), false);
  assert.equal(reader.getState().mountedItemCount, 0);
})().catch(error=>{console.error(error);process.exit(1);});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=reader_js_source(), capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_alignment_job_subscribers_share_polling_and_status_outcomes(self) -> None:
        script = r"""
const assert = require('assert/strict');
const vm = require('vm');
const fs = require('fs');
const context = {
  document: {
    readyState:'loading', documentElement:{dataset:{}}, addEventListener(){}
  },
  location:{pathname:'/',search:''},
  addEventListener(){}, setTimeout(resolve){resolve();}
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(0,'utf8'), context);
const reader = context.MEFinderReader;
const responses = {
  J1:[{status:202,ok:true,payload:{}},{status:200,ok:true,payload:{ok:true}}],
  J2:[{status:200,ok:true,payload:{cancelled:true}}],
  J3:[{status:404,ok:false,payload:{}}],
  J4:[{status:500,ok:false,payload:{error:'failed'}}]
};
const polls = {};
reader.configure({fetch:async url=>{
  const id = new URL(url,'http://localhost').searchParams.get('job_id');
  polls[id] = (polls[id] || 0) + 1;
  const response = responses[id].shift();
  return {status:response.status, ok:response.ok, json:async()=>response.payload};
}});
let events = [];
const unsubscribe = reader.alignmentJobs.subscribe(event=>events.push(event));
(async()=>{
  reader.alignmentJobs.watch('J1',{origin:'works',groupId:'G'});
  reader.alignmentJobs.watch('J1',{origin:'reader',groupId:'G'});
  assert.equal(reader.alignmentJobs.running().origin,'works');
  for (let i=0;i<5 && !events.length;i++) await new Promise(setImmediate);
  assert.equal(polls.J1,2);
  assert.equal(events.length,1);
  assert.equal(events[0].meta.origin,'works');
  assert.equal(events[0].outcome,'ok');
  for (const [id,expected] of [['J2','cancelled'],['J3','unknown'],['J4','failed']]) {
    reader.alignmentJobs.watch(id,{origin:'works'});
    for (let i=0;i<5 && events.length < Number(id.slice(1));i++) await new Promise(setImmediate);
    assert.equal(events.at(-1).outcome,expected);
    assert.equal(polls[id],1);
  }
  assert.equal(events.length,4);
  assert.equal(reader.alignmentJobs.running(),null);
  unsubscribe();
})().catch(error=>{console.error(error);process.exit(1);});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=reader_js_source(), capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
