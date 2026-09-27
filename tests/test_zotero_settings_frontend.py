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
