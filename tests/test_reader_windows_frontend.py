"""Execute the actual host and reader scripts with mocked native/HTTP boundaries."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node unavailable")
class ReaderWindowFrontendTests(unittest.TestCase):
    def test_reader_close_sends_one_way_notification(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const events = {}, state = {}, options = {sourceId:'book',anchorIndex:8};
let config, opened;
const context = {
  document:{documentElement:{dataset:{}}, getElementById(){return {}; }},
  settingsStore:{}, applyAppearance(){}, initAppearanceSystemWatch(){},
  addEventListener(name, fn){events[name]=fn;},
  fetch:async()=>({ok:true,json:async()=>({appearance:{custom_themes:{}},reader_line_mode:'physical'})}),
  pywebview:{state,api:{reader_options:async()=>options}},
  MEFinderReader:{configure(value){config=value;},restore:async()=>false,
    open:async value=>{opened=value;}}
};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
  await events.pywebviewready();
  assert.deepEqual(opened,options);
  config.onClose(); // no close_reader API exists: no reply can target a closed WebView
  assert.equal(state.readerClosed,true);
})().catch(e=>{console.error(e);process.exit(1);});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(root / "src/me_finder/static/reader-window.js")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_toggle_failure_restart_and_search_anchor_handoff(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const events = {}, opened = [], notifications = [], input = {};
let saved = false, fail = false, resolveSave, failOpen = false;
const context = {document: {readyState: 'loading', documentElement: {dataset: {}},
  getElementById(){return input;}, addEventListener(name, fn){(events[name] ||= []).push(fn);}},
  desktopShell:'macos', URLSearchParams, setTimeout, clearTimeout,
  location: {pathname:'/',search:''}, addEventListener(){},
  pywebview: {api: {async open_reader(options){if(failOpen) throw Error('窗口创建失败'); opened.push(options); return true;}}},
  showToast(message){notifications.push(message);},
  async fetch(url, options){const desired=JSON.parse(options.body).reader_window_enabled;
    await new Promise(r=>resolveSave=r); if(!fail) saved=desired;
    return {ok:!fail,json:async()=>fail?{error:'保存失败'}:{reader_window_enabled:saved}};},
  async loadPreferences(){context.MEFinder.readerHost.syncPreferences({reader_window_enabled:saved});}
};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
let route;
context.MEFinderReader={configure(options){route=options.openExternal;}};
events.DOMContentLoaded[0]();
(async()=>{
  assert.equal(await route({sourceId:'book'}),false); // default stays in the original reader
  assert.equal(input.checked,false);
  let saving=context.MEFinder.readerHost.setEnabled(true);
  assert.equal(input.disabled,true);
  await context.MEFinder.readerHost.setEnabled(true); // duplicate submit is suppressed
  resolveSave(); await saving;
  assert.equal(input.checked,true);
  vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
  context.MEFinderReader.configure({openExternal:route});
  const spans=[{pdf_page_id:'PAGE-12',page_char_start:2,page_char_end:4}];
  await context.MEFinderReader.openForSearchResult({source_file_id:'book',source_type:'pdf',
    pdf_page_start_index:12,page_match_spans:spans,match_quote:'𠮷😀',match_offset_unit:'unicode_codepoint'});
  assert.equal(opened.length,1);
  assert.deepEqual(opened[0].pageMatchSpans,spans);
  assert.equal(opened[0].matchQuote,'𠮷😀');
  assert.equal(context.MEFinderReader.isOpen(),false); // no overlay or main-page state mutation
  failOpen=true;
  await assert.rejects(()=>context.MEFinderReader.open({sourceId:'book'}),/窗口创建失败/);
  failOpen=false;
  fail=true; saving=context.MEFinder.readerHost.setEnabled(false); resolveSave(); await saving;
  assert.equal(input.checked,true); assert.equal(input.disabled,false); assert.equal(notifications.length,1);
  fail=false; saving=context.MEFinder.readerHost.setEnabled(false); resolveSave(); await saving;
  assert.equal(input.checked,false); assert.equal(await route({sourceId:'book'}),false);
  context.MEFinder.readerHost.syncPreferences({reader_window_enabled:true});
  context.desktopShell='';
  assert.equal(await route({sourceId:'book'}),false); // browser mode preserves the overlay
})().catch(e=>{console.error(e);process.exit(1);});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(root / "src/me_finder/static/js/16-reader-host.js"),
             str(root / "src/me_finder/static/reader.js")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
