"""Execute the Zotero settings tree and shared PDF parse mode controls."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
JS = ROOT / "src" / "me_finder" / "static" / "js"


@unittest.skipUnless(NODE, "node is required for frontend behavior tests")
class ZoteroSettingsFrontendTests(unittest.TestCase):
    def run_sync_script(self, body):
        script = r"""
const fs = require('fs');
const assert = require('assert/strict');
const path = require('path');
const noop = () => {};
let active = false;
const section = {classList: {contains: () => active}};
global.document = {getElementById: id => id === 'zotero-settings' ? section : null,
  querySelector: () => null, querySelectorAll: () => [], addEventListener: noop};
global.window = global;
global.location = {search: '?page=library'};
global.addEventListener = noop;
global.currentPage = 'library';
global.settingsStore = {};
global.searchStore = {};
global.libraryStore = {};
global.MEFinderActions = {register: noop, registerInline: noop};
const timers = new Map();
let timerId = 0;
global.setTimeout = (fn, ms) => { timers.set(++timerId, {fn, ms}); return timerId; };
global.clearTimeout = id => timers.delete(id);
const flush = () => new Promise(resolve => setImmediate(resolve));
async function tick() {
  assert.equal(timers.size, 1, 'one status poll must remain scheduled');
  const [id, timer] = timers.entries().next().value;
  timers.delete(id);
  timer.fn();
  await flush();
}
let status = {phase: 'idle', rows: [], documents: {linked: 0, pending: 0}};
let overviewCalls = 0, libraryCalls = 0, catalogCalls = 0, statusCalls = 0;
let failStatus = false, holdStatus = false, releaseStatus;
const toasts = [];
global.showToast = message => toasts.push(message);
global.MEFinderApi = {
  async requestJSON(url) {
    if (url === '/api/zotero/status') {
      statusCalls++;
      if (failStatus) throw Error('status unavailable');
      if (holdStatus) return new Promise(resolve => { releaseStatus = resolve; });
      return structuredClone(status);
    }
    if (url === '/api/zotero/overview') { overviewCalls++; return {collections: []}; }
    if (url === '/api/zotero/sync') return {};
    if (url === '/api/preferences') return {};
    if (url === '/api/zotero/preview') return {known: true};
    throw Error(url);
  },
  async fetch(url) {
    if (url === '/api/library?view=summary') {
      catalogCalls++;
      return {ok: true, json: async () => ({items: [{id: 'new'}]})};
    }
    if (url === '/api/index-meta') return {json: async () => ({})};
    throw Error(url);
  }
};
global.MEFinder = {library: {async load() {
  libraryCalls++;
  await fetchLibraryCatalog();
  libraryStore.loaded = true;
}}, imports: {normalizePdfParseMode: x => x, renderPdfParseMode: noop},
  visionProviders: {}, parserRuntime: {}, works: {load: async () => {}}};
function load(name) { eval.call(global, fs.readFileSync(path.join(process.argv[1], name), 'utf8')); }
load('14-task-state.js');
load('20-search.js');
load('62-zotero.js');
function cacheOldCatalog() {
  searchStore.libraryCatalog = {items: [{id: 'old'}]};
  searchStore.libraryCatalogPromise = Promise.resolve(searchStore.libraryCatalog);
  searchStore.documentsLoaded = true;
  libraryStore.loaded = true;
}
""" + body
        result = subprocess.run(
            [NODE, "-e", script, str(JS)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_completion_refreshes_visible_library_and_stops_reloading_when_unchanged(self):
        self.run_sync_script(r"""
(async () => {
  status.documents.pending = 2;
  await MEFinder.zotero.start();
  cacheOldCatalog();
  status.documents = {linked: 1, pending: 1};
  await tick();
  assert.equal(libraryCalls, 1);
  assert.equal(catalogCalls, 1);
  assert.equal(searchStore.libraryCatalog.items[0].id, 'new');
  assert.equal(overviewCalls, 0, 'hidden settings must not fetch the collection tree');
  await tick();
  assert.equal(libraryCalls, 1, 'unchanged status must preserve the catalog cache');
  status.documents = {linked: 2, pending: 0};
  await tick();
  assert.equal(libraryCalls, 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_import_after_leaving_settings_invalidates_existing_catalog(self):
        self.run_sync_script(r"""
(async () => {
  active = true;
  status.rows = [{tone: 'busy'}];
  await MEFinder.zotero.open();
  cacheOldCatalog();
  overviewCalls = 0;
  active = false;
  status.documents.linked = 1;
  status.rows = [{tone: 'ok', status_text: '已导入'}];
  await tick();
  assert.equal(libraryCalls, 1, 'Zotero completion must invalidate the catalog just like manual imports');
  assert.equal(overviewCalls, 0);
  assert.equal(timers.size, 1, 'leaving settings must not stop the monitor');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_stale_status_cannot_overwrite_newer_catalog_observation(self):
        self.run_sync_script(r"""
(async () => {
  holdStatus = true;
  const stale = MEFinder.zotero.start();
  holdStatus = false;
  status.documents.linked = 1;
  await MEFinder.zotero.start();
  releaseStatus({phase: 'idle', rows: [], documents: {linked: 0}});
  await stale;
  assert.equal(libraryCalls, 1);
  await tick();
  assert.equal(libraryCalls, 1, 'late old response must not roll back the observed stamp');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_manual_sync_discards_in_flight_poll_and_keeps_one_timer(self):
        self.run_sync_script(r"""
(async () => {
  holdStatus = true;
  const stale = MEFinder.zotero.start();
  holdStatus = false;
  await MEFinder.zotero.sync();
  releaseStatus({phase: 'done', rows: [], documents: {linked: 7}});
  await stale;
  assert.equal(libraryCalls, 0);
  assert.equal(timers.size, 1);
  status.documents.linked = 1;
  await tick();
  assert.equal(libraryCalls, 1);
  assert.equal(timers.size, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_removal_and_same_count_sync_invalidate_hidden_library(self):
        self.run_sync_script(r"""
(async () => {
  currentPage = 'search';
  status.documents.linked = 2;
  await MEFinder.zotero.start();
  cacheOldCatalog();
  status.documents.linked = 1;
  await tick();
  assert.equal(searchStore.libraryCatalog, null);
  assert.equal(searchStore.libraryCatalogPromise, null);
  assert.equal(libraryStore.loaded, false);
  assert.equal(searchStore.documentsLoaded, false);
  assert.equal(libraryCalls, 0);
  cacheOldCatalog();
  status.last_success_at = '2026-10-03T01:00:00Z';
  await tick();
  assert.equal(libraryStore.loaded, false, 'same-count replacements/metadata changes also invalidate');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_idle_poll_detects_later_automatic_sync_and_first_completed_status(self):
        self.run_sync_script(r"""
(async () => {
  cacheOldCatalog();
  status.documents.linked = 1;
  await MEFinder.zotero.start();
  assert.equal(libraryCalls, 1, 'first response may already contain a completed launch import');
  await tick();
  assert.equal(libraryCalls, 1);
  status.phase = 'running';
  await tick();
  status.phase = 'done';
  status.documents.linked = 2;
  await tick();
  assert.equal(libraryCalls, 2, 'automatic sync must be observed without opening settings');
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_status_failure_keeps_polling_and_reports_error(self):
        self.run_sync_script(r"""
(async () => {
  await MEFinder.zotero.start();
  cacheOldCatalog();
  failStatus = true;
  await tick();
  assert.match(toasts[0], /status unavailable/);
  assert.equal(libraryStore.loaded, true);
  failStatus = false;
  status.documents.linked = 1;
  await tick();
  assert.equal(libraryCalls, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_bootstrap_starts_monitor_after_initial_catalog_without_opening_settings(self):
        self.run_sync_script(r"""
(async () => {
  for (const name of ['configureDesktopPlatformOptions', 'setupScanDirectoryControls',
    'renderScanDirectories', 'loadPreferences', 'updateSearchDocumentLabel', 'navigateTo']) global[name] = noop;
  document.getElementById = id => id === 'index-count' ? {} : null;
  MEFinder.imports = {setupLibraryDragSelection: noop, setupScanResultDragSelection: noop,
    loadResumableImports: noop, loadActiveImports: noop, initDropZone: noop};
  MEFinder.library.setupKeyboardNav = noop;
  MEFinder.library.syncViewButtons = noop;
  MEFinder.parserRuntime.bindMineruAccountDialogDismissal = noop;
  let releaseCatalog;
  searchStore.libraryCatalogPromise = new Promise(resolve => { releaseCatalog = resolve; });
  global.renderSearchDocumentOptions = noop;
  load('90-init.js');
  assert.equal(statusCalls, 0);
  releaseCatalog({items: []});
  await flush();
  assert.equal(statusCalls, 1);
  assert.ok([...timers.values()].some(timer => timer.ms === 10000));
})().catch(error => { console.error(error); process.exitCode = 1; });
""")

    def test_summary_pause_recovery_and_disconnected_controls(self):
        script = r"""
const fs = require('fs');
const assert = require('assert/strict');
function element() {
  return {children: [], listeners: {}, attributes: {}, style: {setProperty() {}},
    classList: {toggle() {}, contains() { return false; }},
    setAttribute(key, value) { this.attributes[key] = value; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    addEventListener(name, fn) { this.listeners[name] = fn; }};
}
const ids = ['zotero-tree', 'zotero-tree-foot', 'zotero-selection-summary',
  'zotero-sync-enabled', 'zotero-sync-enabled-state', 'zotero-paused-note',
  'zotero-collection-controls', 'zotero-frequency-controls', 'zotero-sync-button',
  'zotero-log', 'zotero-log-details', 'zotero-sync-note', 'zotero-progress'];
const nodes = Object.fromEntries(ids.map(id => [id, element()]));
global.document = {createElement: element, createElementNS: element,
  getElementById(id) { return nodes[id] || null; }, querySelectorAll() { return []; }};
global.window = global;
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.settingsStore = {};
global.MEFinder = {imports: {normalizePdfParseMode: x => x, renderPdfParseMode() {}}};
let prefs = {zotero_sync_enabled: true, zotero_sync_collections: ['parent']};
let connected = true, rejectSave = false;
const toasts = [];
global.showToast = message => toasts.push(message);
global.MEFinderApi = {async requestJSON(url, options) {
  if (url === '/api/preferences') {
    if (options) {
      if (rejectSave) throw Error('save rejected');
      prefs = {...prefs, ...JSON.parse(options.body)};
    }
    return prefs;
  }
  if (url === '/api/zotero/status') return {phase: 'idle', rows: [
    {action:'导入', title:'文献', status_text:'已导入', tone:'ok'}]};
  if (url === '/api/zotero/overview') return {
    connection: {state: connected ? 'connected' : 'not_running'},
    collections: [{key:'parent', name:'分类', parent:null, item_count:3},
                  {key:'child', name:'子分类', parent:'parent', item_count:2}]};
  if (url === '/api/zotero/preview') return {known:true, remove_count:1, unlink_count:2};
  if (url === '/api/zotero/sync') return {};
  throw Error(url);
}};
eval(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]), '14-task-state.js'), 'utf8'));
eval(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  await MEFinder.zotero.open();
  assert.equal(nodes['zotero-selection-summary'].textContent, '已选 2 个分类（含子分类）');
  assert.equal(nodes['zotero-collection-controls'].disabled, false);
  assert.equal(nodes['zotero-log-details'].hidden, false);
  assert.equal(nodes['zotero-log'].children.length, 1);
  assert.match(nodes['zotero-tree-foot'].children[1].textContent, /移除 1 篇/);
  assert.match(nodes['zotero-tree-foot'].children[2].textContent, /只解除关联/);
  // 取消父分类同时取消子分类，摘要与实际选择一致。
  nodes['zotero-tree'].children[0].children[1].listeners.click();
  assert.equal(nodes['zotero-selection-summary'].textContent, '还没有选择分类');
  await MEFinder.zotero.setEnabled(false);
  assert.equal(nodes['zotero-paused-note'].hidden, false);
  assert.equal(nodes['zotero-collection-controls'].disabled, true);
  assert.equal(nodes['zotero-frequency-controls'].disabled, true);
  assert.equal(nodes['zotero-sync-button'].disabled, true);
  rejectSave = true;
  await MEFinder.zotero.setEnabled(true);
  assert.equal(nodes['zotero-sync-enabled'].checked, false);
  assert.equal(nodes['zotero-collection-controls'].disabled, true);
  assert.match(toasts[0], /同步开关保存失败/);
  rejectSave = false;
  await MEFinder.zotero.setEnabled(true);
  assert.equal(nodes['zotero-paused-note'].hidden, true);
  assert.equal(nodes['zotero-collection-controls'].disabled, false);
  assert.equal(nodes['zotero-sync-button'].disabled, false);
  connected = false;
  await MEFinder.zotero.recheck();
  assert.equal(nodes['zotero-collection-controls'].disabled, true);
  assert.equal(nodes['zotero-sync-button'].disabled, true);
  assert.equal(nodes['zotero-frequency-controls'].disabled, false);
  assert.notEqual(nodes['zotero-sync-enabled'].disabled, true);
  connected = true;
  await MEFinder.zotero.recheck();
  await MEFinder.zotero.sync();
  assert.equal(nodes['zotero-sync-button'].disabled, true);
  assert.equal(nodes['zotero-progress'].hidden, false);
  assert.equal(nodes['zotero-sync-note'].textContent, '正在读取 Zotero');
  assert.equal(nodes['zotero-log-details'].hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [NODE, "-e", script, str(JS / "62-zotero.js")],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tree_starts_folded_and_parse_mode_updates_after_save(self):
        script = r"""
const fs = require('fs');
function element(tag) {
  return {tag, children: [], listeners: {}, attributes: {}, style: {setProperty() {}}, classList: {
    toggle() {}, add() {}, remove() {}, contains() { return false; }
  }, setAttribute(name, value) { this.attributes[name] = value; },
  appendChild(child) { this.children.push(child); return child; },
  replaceChildren(...children) { this.children = children; },
  addEventListener(name, callback) { this.listeners[name] = callback; }};
}
const tree = element('div');
const parse = element('span');
const nodes = {'zotero-tree': tree, 'zotero-parse-mode': parse};
const labels = {auto: '自动选择', mineru: '强制 MinerU'};
const radios = Object.keys(labels).map(value => ({value, checked: false,
  parentElement: {querySelector() { return {textContent: labels[value]}; }}}));
global.document = {
  createElement: element, createElementNS(_ns, tag) { return element(tag); },
  getElementById(id) { return nodes[id] || null; },
  querySelectorAll(selector) { return selector === 'input[name="pdf-parse-mode"]' ? radios : []; }
};
global.window = global;
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.settingsStore = {currentPdfParseMode: 'auto', pdfParseModeSaving: false,
  preferencesLoadPromise: null, preferencesLoaded: true};
global.MEFinderActions = {register() {}, registerInline() {}};
global.MEFinderApi = {
  requestJSON(url) {
    if (url === '/api/preferences') return Promise.resolve({zotero_sync_collections: []});
    if (url === '/api/zotero/status') return Promise.resolve({phase: 'idle', rows: []});
    if (url === '/api/zotero/overview') return Promise.resolve({
      connection: {state: 'connected', label: '已连接'},
      collections: [{key: 'parent', name: '其他', parent: null, item_count: 2},
                    {key: 'child', name: '子分类', parent: 'parent', item_count: 1}],
      pdf_parse_mode: {mode: 'auto', label: '自动选择'}
    });
    if (url === '/api/zotero/preview') return Promise.resolve({known: true});
    throw Error(url);
  },
  fetch() { return Promise.resolve({ok: true, json: async () => ({pdf_parse_mode: 'mineru'})}); }
};
global.showToast = () => {};
eval(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]), '14-task-state.js'), 'utf8'));
eval(fs.readFileSync(process.argv[1], 'utf8'));
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  await MEFinder.zotero.open();
  const collapsed = {rows: tree.children.length,
    expanded: tree.children[0].attributes['aria-expanded']};
  tree.children[0].children[0].listeners.click();
  const expandedRows = tree.children.length;
  await setPdfParseMode('mineru');
  process.stdout.write(JSON.stringify({collapsed, expandedRows, parseLabel: parse.textContent}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [NODE, "-e", script, str(JS / "62-zotero.js"), str(JS / "80-import.js")],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["collapsed"], {"rows": 1, "expanded": "false"})
        self.assertEqual(observed["expandedRows"], 2)
        self.assertEqual(observed["parseLabel"], "强制 MinerU")
