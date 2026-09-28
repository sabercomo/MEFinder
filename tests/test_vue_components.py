"""Render the settings-page Vue components with the vendored Vue in node.

These tests execute the real ``static/vendor/vue.global.prod.js`` against a minimal
DOM (``tests/fixtures/vue_mini_dom.js``), so template typos, wrong bindings and
broken click wiring fail in CI instead of only in a browser preview.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
STATIC = ROOT / "src" / "me_finder" / "static"
MINI_DOM = ROOT / "tests" / "fixtures" / "vue_mini_dom.js"

PRELUDE = r"""
const fs = require('fs');
const path = require('path');
const dom = require(process.argv[1]);
const STATIC = process.argv[2];
const body = dom.install(global);
function mountPoint(id, tag) {
  const element = document.createElement(tag || 'div');
  element.id = id;
  body.appendChild(element);
  return element;
}
function load(relative) {
  eval.call(global, fs.readFileSync(path.join(STATIC, relative), 'utf8'));
}
const calls = [];
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function done(value) { process.stdout.write(JSON.stringify(value)); }
load('vendor/vue.global.prod.js');
load('js/06-pure.js');
"""


@unittest.skipUnless(NODE, "node is required for Vue render tests")
class VueComponentRenderTests(unittest.TestCase):
    def render(self, body):
        result = subprocess.run(
            [NODE, "-e", PRELUDE + "(async () => {\n" + body + "\n})().catch(e => {"
             " console.error(e); process.exitCode = 1; });",
             str(MINI_DOM), str(STATIC)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_alignment_model_rows_render_state_and_wire_actions(self):
        observed = self.render(r"""
mountPoint('embedding-model-options');
const status = mountPoint('alignment-model-status', 'span');
global.downloadAlignmentModel = id => calls.push(['download', id]);
global.deleteAlignmentModel = id => calls.push(['delete', id]);
global.setAlignmentEmbeddingModel = id => calls.push(['pick', id]);
load('js/64-settings-model-view.js');
const view = MEFinderAlignmentModelView;
await tick();
const before = {mini: dom.text(body, 'embedding-model-state-minilm-l12-v2'), status: status.textContent};
view.setSelection('minilm-l12-v2', false);
view.setComponent({models: [
  {id: 'minilm-l12-v2', installed: true, state: 'installed'},
  {id: 'multilingual-e5-large', installed: false, state: 'downloading',
   downloaded_bytes: 500000000, total_bytes: 2000000000}]});
await tick();
const e5Progress = dom.byId(body, 'embedding-model-progress-multilingual-e5-large');
const e5Button = dom.byId(body, 'embedding-model-download-multilingual-e5-large');
const miniButton = dom.byId(body, 'embedding-model-download-minilm-l12-v2');
dom.click(miniButton);
dom.click(e5Button);
const radios = document.querySelectorAll('input[name="alignment-embedding-model"]');
done({
  before,
  mini: dom.text(body, 'embedding-model-state-minilm-l12-v2'),
  miniButton: [miniButton.textContent, miniButton.className],
  e5: dom.text(body, 'embedding-model-state-multilingual-e5-large'),
  e5Hint: dom.text(body, 'embedding-model-hint-multilingual-e5-large'),
  e5Progress: [e5Progress.hidden, e5Progress.getAttribute('aria-valuenow'),
    e5Progress.firstChild.style.width],
  e5Button: [e5Button.textContent, e5Button.disabled],
  status: [status.textContent, status.className],
  radios: radios.map(input => [input.value, input.checked]),
  calls
});
""")
        self.assertEqual(observed["before"], {"mini": "读取中…", "status": "读取中…"})
        self.assertEqual(observed["mini"], "已下载")
        self.assertEqual(observed["miniButton"], ["删除模型", "action-btn danger"])
        self.assertEqual(observed["e5"], "下载中 25%")
        self.assertEqual(observed["e5Hint"], "已下载 500 MB / 2.00 GB · 25%")
        self.assertEqual(observed["e5Progress"], [False, "25", "25%"])
        self.assertEqual(observed["e5Button"], ["正在下载…", True])
        self.assertEqual(observed["status"], ["当前模型已下载", "settings-status ready"])
        self.assertEqual(observed["radios"],
                         [["minilm-l12-v2", True], ["multilingual-e5-large", False]])
        # 下载中按钮不可点（disabled 时 action 为 null），只有删除被调用。
        self.assertEqual(observed["calls"], [["delete", "minilm-l12-v2"]])

    def test_rejected_model_switch_restores_radio_selection(self):
        observed = self.render(r"""
mountPoint('embedding-model-options');
global.setAlignmentEmbeddingModel = id => calls.push(['pick', id]);  // 拒绝：不改 store
load('js/64-settings-model-view.js');
await tick();
const e5 = document.querySelectorAll('input[name="alignment-embedding-model"]')[1];
dom.change(e5);
await tick();
done({calls, radios: document.querySelectorAll('input[name="alignment-embedding-model"]')
  .map(input => [input.value, input.checked])});
""")
        self.assertEqual(observed["calls"], [["pick", "multilingual-e5-large"]])
        self.assertEqual(observed["radios"],
                         [["minilm-l12-v2", True], ["multilingual-e5-large", False]])

    def test_managed_mineru_card_renders_profiles_and_wires_actions(self):
        observed = self.render(r"""
mountPoint('managed-mineru', 'section');
global.manageMineruComponent = (profile, action) => calls.push([profile, action]);
global.checkManagedMineruUpdates = () => calls.push(['check']);
load('js/70-managed-mineru-view.js');
const view = MEFinderManagedMineruView;
await tick();
const initial = {
  hardware: dom.text(body, 'managed-mineru-hardware'),
  pipeline: dom.text(body, 'managed-mineru-pipeline-state'),
  installHidden: dom.byId(body, 'managed-mineru-pipeline-install').hidden,
  startHidden: dom.byId(body, 'managed-mineru-pipeline-start').hidden
};
view.setRuntime({
  supported: true, version: '2.5.4', version_source: 'pypi',
  hardware: {name: 'RTX 4070', vram_mb: 12288, vlm_supported: true, recommended_profile: 'vlm'},
  service: {running: false},
  profiles: [
    {profile: 'pipeline', display_name: 'Pipeline', supported: true, installed: true, state: 'installed'},
    {profile: 'vlm', display_name: 'VLM', supported: true, installed: false, state: 'downloading_models',
     progress: 0.4, downloaded_bytes: 1073741824, total_bytes: 2147483648,
     download_speed_bps: 10485760, eta_seconds: 103}]
}, {enabled: true, managed: true});
await tick();
const vlmBar = dom.byId(body, 'managed-mineru-vlm-progress-bar');
const running = {
  hardware: dom.text(body, 'managed-mineru-hardware'),
  pipeline: dom.text(body, 'managed-mineru-pipeline-state'),
  pipelineButtons: ['install', 'start', 'stop', 'uninstall', 'cancel']
    .filter(key => !dom.byId(body, 'managed-mineru-pipeline-' + key).hidden),
  vlm: dom.text(body, 'managed-mineru-vlm-state'),
  vlmProgress: dom.text(body, 'managed-mineru-vlm-progress'),
  vlmBar: [vlmBar.hidden, vlmBar.firstChild.style.width],
  vlmButtons: ['install', 'start', 'stop', 'uninstall', 'cancel']
    .filter(key => !dom.byId(body, 'managed-mineru-vlm-' + key).hidden),
  auto: [dom.text(body, 'managed-mineru-auto-install'), dom.byId(body, 'managed-mineru-auto-install').disabled],
  check: dom.byId(body, 'managed-mineru-check-updates').disabled,
  hint: dom.text(body, 'managed-mineru-hint')
};
dom.click(dom.byId(body, 'managed-mineru-pipeline-start'));
dom.click(dom.byId(body, 'managed-mineru-vlm-cancel'));
view.setPending('pipeline', 'start', true);
view.setNotice('读取托管运行时失败：连接中断');
await tick();
const pendingState = {
  startDisabled: dom.byId(body, 'managed-mineru-pipeline-start').disabled,
  hint: dom.text(body, 'managed-mineru-hint')
};
view.setRuntime({supported: true, hardware: {vlm_supported: false}, service: {}, profiles: []}, {});
await tick();
const vlmArticle = document.querySelectorAll('.managed-mineru-profile')[1];
done({initial, running, pendingState, vlmHidden: vlmArticle.hidden, calls});
""")
        self.assertEqual(observed["initial"], {
            "hardware": "正在检测硬件…", "pipeline": "未安装",
            "installHidden": False, "startHidden": True})
        running = observed["running"]
        self.assertEqual(running["hardware"], "当前设备：RTX 4070 · 12GB 显存 · 推荐 VLM")
        self.assertEqual(running["pipeline"], "已安装")
        self.assertEqual(running["pipelineButtons"], ["start", "uninstall"])
        self.assertEqual(running["vlm"], "下载模型中")
        self.assertEqual(running["vlmProgress"],
                         "已下载 1.00 GB / 2.00 GB · 10.0 MB/s · 预计剩余约 2 分钟")
        self.assertEqual(running["vlmBar"], [False, "40%"])
        self.assertEqual(running["vlmButtons"], ["cancel"])
        self.assertEqual(running["auto"], ["安装推荐配置", True])
        self.assertTrue(running["check"])
        self.assertEqual(running["hint"],
                         "安装需要约 20GB 可用空间，请保持应用开启 · 安装目标 MinerU 2.5.4（兼容区间内最新）")
        self.assertEqual(observed["pendingState"],
                         {"startDisabled": True, "hint": "读取托管运行时失败：连接中断"})
        self.assertTrue(observed["vlmHidden"])
        self.assertEqual(observed["calls"], [["pipeline", "start"], ["vlm", "cancel"]])


if __name__ == "__main__":
    unittest.main()
