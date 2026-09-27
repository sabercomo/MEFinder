"""Execute component-state requests to prove stale responses cannot win."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
JS = ROOT / "src" / "me_finder" / "static" / "js"

# 最小 DOM 桩：只提供对齐模型行用到的元素与属性。
DOM_STUB = r"""
const fs = require('fs');
function element(id) {
  const classes = new Set();
  return {id, hidden: false, disabled: false, textContent: '', title: '', className: '',
    onclick: null, attributes: {}, style: {},
    classList: {add(n) { classes.add(n); }, remove(n) { classes.delete(n); },
      toggle(n, on) { if (on === undefined ? !classes.has(n) : on) classes.add(n); else classes.delete(n); },
      contains(n) { return classes.has(n); }},
    setAttribute(n, v) { this.attributes[n] = v; }, removeAttribute(n) { delete this.attributes[n]; },
    querySelector() { return this.fill || (this.fill = {style: {}}); }};
}
const nodes = {};
function node(id) { return nodes[id] || (nodes[id] = element(id)); }
const MODEL_IDS = ['minilm-l12-v2', 'multilingual-e5-large'];
MODEL_IDS.forEach(id => ['download', 'state', 'hint', 'progress']
  .forEach(part => node('embedding-model-' + part + '-' + id)));
node('alignment-model-status');
global.document = {getElementById(id) { return nodes[id] || null; }, querySelectorAll() { return []; }};
global.window = global;
global.settingsStore = {currentAlignmentEmbeddingModel: 'minilm-l12-v2',
  alignmentEmbeddingModelSaving: false, alignmentModelComponent: null,
  alignmentModelPollTimer: null, preferencesLoadPromise: null};
global.showToast = () => {};
global.formatFileSize = n => n + ' B';
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
global.clearTimeout = handle => { if (timers[handle - 1]) timers[handle - 1].live = false; };
const livePolls = () => timers.filter(t => t.live).length;
function deferred() {
  let resolve; const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function reply(body) { return {ok: true, status: 200, json: async () => body}; }
function component(state) {
  return {models: MODEL_IDS.map(id => ({id, display_name: id, installed: false,
    state: id === 'minilm-l12-v2' ? state : 'idle',
    downloaded_bytes: 0, total_bytes: state === 'downloading' ? 1000 : 0}))};
}
const pending = [];
global.MEFinderApi = {fetch(url, options) {
  const call = deferred();
  pending.push({url, method: (options && options.method) || 'GET', call});
  return call.promise;
}};
function load(name) { eval.call(global, fs.readFileSync(require('path').join(process.argv[1], name), 'utf8')); }
load('06-pure.js');
load('14-task-state.js');
load('64-settings-model-view.js');
load('64-settings-model.js');
const view = global.MEFinderAlignmentModelView;
const flush = () => new Promise(r => setImmediate(r));
"""


@unittest.skipUnless(NODE, "node is required for frontend behavior tests")
class ComponentRequestOrderingTests(unittest.TestCase):
    def run_script(self, body):
        result = subprocess.run(
            [NODE, "-e", DOM_STUB + body, str(JS)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_stale_poll_cannot_overwrite_download_that_started_later(self):
        # 复现：轮询 GET 先发出、晚返回，带回下载开始前的「未下载」快照。
        # 修复前它会覆盖下载 POST 的「下载中」并停掉轮询，界面卡在「未下载」。
        observed = self.run_script(r"""
(async () => {
  loadAlignmentModelComponent();
  const staleGet = pending[0];
  const download = downloadAlignmentModel('minilm-l12-v2', node('embedding-model-download-minilm-l12-v2'));
  const post = pending[1];
  post.call.resolve(reply(component('downloading')));
  await download; await flush();
  const pollsAfterPost = livePolls();
  staleGet.call.resolve(reply(component('idle')));
  await flush(); await flush();
  process.stdout.write(JSON.stringify({
    methods: pending.map(p => p.method),
    state: view.rowView('minilm-l12-v2').stateText,
    summary: view.summaryView().text,
    pollsAfterPost, pollsAtEnd: livePolls()
  }));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(observed["methods"], ["GET", "POST"])
        self.assertTrue(observed["state"].startswith("下载中"), observed)
        self.assertTrue(observed["summary"].startswith("下载中"), observed)
        self.assertEqual(observed["pollsAfterPost"], 1)
        self.assertEqual(observed["pollsAtEnd"], 1)

    def test_stale_poll_cannot_resurrect_model_after_delete(self):
        observed = self.run_script(r"""
(async () => {
  global.showAppConfirm = async () => true;
  const installed = component('idle'); installed.models[0].installed = true;
  renderAlignmentModelComponent(installed);
  loadAlignmentModelComponent();
  const staleGet = pending[0];
  const removal = deleteAlignmentModel('minilm-l12-v2', node('embedding-model-download-minilm-l12-v2'));
  await flush();
  pending[1].call.resolve(reply(component('idle')));
  await removal; await flush();
  staleGet.call.resolve(reply(installed));
  await flush(); await flush();
  process.stdout.write(JSON.stringify({state: view.rowView('minilm-l12-v2').stateText}));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(observed["state"], "未下载")

    def test_latest_token_only_accepts_the_newest_begin(self):
        observed = self.run_script(r"""
const latest = MEFinderTaskState.createLatest();
const first = latest.begin();
const second = latest.begin();
const results = [latest.isCurrent(first), latest.isCurrent(second)];
latest.invalidate();
results.push(latest.isCurrent(second));
process.stdout.write(JSON.stringify(results));
""")
        self.assertEqual(observed, [False, True, False])


# 设置页解析组件（本地 OCR / 托管 MinerU）用的通用 DOM 桩：任何 id 都按需造元素。
PARSER_STUB = r"""
const fs = require('fs');
function element(id) {
  const classes = new Set();
  return {id, hidden: false, disabled: false, checked: false, value: '', textContent: '', title: '',
    className: '', onclick: null, style: {}, firstElementChild: {style: {}},
    classList: {add(n) { classes.add(n); }, remove(n) { classes.delete(n); },
      toggle(n, on) { if (on === undefined ? !classes.has(n) : on) classes.add(n); else classes.delete(n); }},
    closest() { return null; }, setAttribute() {}, removeAttribute() {}};
}
const nodes = {};
function node(id) { return nodes[id] || (nodes[id] = element(id)); }
global.document = {getElementById: node, querySelector() { return null; }, querySelectorAll() { return []; }};
global.window = global;
global.settingsStore = {currentPdfParseMode: 'auto'};
global.parserStore = {localOCRConfig: null, localOCRPollTimer: null, managedMineruPollTimer: null,
  managedMineruWasBusy: false, mineruLocalConfig: {}, mineruAccounts: []};
global.MEFinderActions = {register() {}};
global.showToast = () => {};
global.showAppConfirm = async () => true;
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
global.clearTimeout = handle => { if (timers[handle - 1]) timers[handle - 1].live = false; };
const livePolls = () => timers.filter(t => t.live).length;
function deferred() {
  let resolve; const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function reply(body) { return {ok: true, status: 200, json: async () => body}; }
const pending = [];
global.MEFinderApi = {fetch(url, options) {
  const call = deferred();
  pending.push({url, method: (options && options.method) || 'GET', call});
  return call.promise;
}};
const calls = (url, method) => pending.filter(p => p.url === url && p.method === (method || 'GET'));
function load(name) { eval.call(global, fs.readFileSync(require('path').join(process.argv[1], name), 'utf8')); }
global.module = {exports: {}};
load('06-pure.js');
load('14-task-state.js');
load('70-managed-mineru-view.js');
load('70-vision.js');
const flush = () => new Promise(r => setImmediate(r));
function ocr(state) {
  return {engines: [{provider_id: 'ndlocr-lite', enabled: false, configured: false}],
    installer: {supported: true, engines: [{provider_id: 'ndlocr-lite', state, managed: false}]}};
}
function mineru(state) {
  return {supported: true, hardware: {}, service: {},
    profiles: [{profile: 'pipeline', display_name: 'Pipeline', state, installed: false, supported: true}]};
}
"""


@unittest.skipUnless(NODE, "node is required for frontend behavior tests")
class ParserComponentRequestOrderingTests(unittest.TestCase):
    def run_script(self, body):
        result = subprocess.run(
            [NODE, "-e", PARSER_STUB + body, str(JS)],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_stale_local_ocr_poll_cannot_undo_install(self):
        observed = self.run_script(r"""
(async () => {
  loadLocalOCRConfig();
  const staleGet = calls('/api/local-ocr')[0];
  const action = manageLocalOCRComponent('ndlocr-lite', 'install', node('local-ocr-modern-install'));
  await flush();
  calls('/api/local-ocr/component', 'POST')[0].call.resolve(reply({ok: true}));
  await flush(); await flush();
  calls('/api/local-ocr')[1].call.resolve(reply(ocr('downloading')));
  await action; await flush();
  staleGet.call.resolve(reply(ocr('not_installed')));
  await flush(); await flush();
  process.stdout.write(JSON.stringify({
    state: node('local-ocr-modern-managed-state').textContent, polls: livePolls()}));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(observed, {"state": "下载中", "polls": 1})

    def test_stale_mineru_poll_cannot_undo_install(self):
        observed = self.run_script(r"""
(async () => {
  // 上一轮渲染处于忙碌态，排出了一次轮询；轮询到点发出 GET。
  module.exports.renderManagedMineru(mineru('downloading_models'));
  timers[timers.length - 1].fn();
  const staleGet = calls('/api/mineru-local/component')[0];
  const action = manageMineruComponent('pipeline', 'install', node('managed-mineru-pipeline-install'));
  await flush();
  calls('/api/mineru-local/component', 'POST')[0].call.resolve(
    reply({ok: true, managed_runtime: mineru('provisioning')}));
  await action; await flush();
  staleGet.call.resolve(reply(mineru('not_installed')));
  await flush(); await flush();
  process.stdout.write(JSON.stringify({
    state: MEFinderManagedMineruView.profileView('pipeline').stateText, polls: livePolls()}));
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(observed, {"state": "安装依赖中", "polls": 1})


if __name__ == "__main__":
    unittest.main()
