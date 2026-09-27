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
