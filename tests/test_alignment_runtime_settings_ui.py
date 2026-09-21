"""Run the shipped settings UI through installation and recovery transitions."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable")
class AlignmentRuntimeSettingsUiTests(unittest.TestCase):
    def run_ui(self, scenario):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const nodes = {}, timers = new Map(), calls = [];
let timerId = 0, runtimeError = false, modelInstalled = false, pendingRuntimeRead = null;
let runtime = {supported:true, installed:false, state:'not_installed',
  compute:{available:false, provider:'none', detail:''}};
function node(id) {
  if (!nodes[id]) {
    const classes = new Set();
    nodes[id] = {hidden:id === 'alignment-runtime-component', disabled:false,
      textContent:'', style:{}, attributes:{},
      classList:{add(x){classes.add(x);}, remove(x){classes.delete(x);},
        contains(x){return classes.has(x);},
        toggle(x, yes){if(yes) classes.add(x); else classes.delete(x);}},
      setAttribute(k,v){this.attributes[k]=v;}, removeAttribute(k){delete this.attributes[k];},
      querySelector(){return node(id + '-fill');}};
  }
  return nodes[id];
}
const context = {
  THEME_BUILTIN_CSS_IDS:[], THEME_PRESET_MAP:{light:{}}, THEME_MODE_DEFAULT:{light:'light'},
  settingsStore:{appearanceState:{mode:'light',light:'light'}, currentAlignmentEmbeddingModel:'minilm-l12-v2'},
  MEFinderBertalignSettings:{load(){}},
  initAppearanceSystemWatch(){}, alignmentModelDownloadProgress(){return null;},
  showToast(){}, showAppConfirm:async()=>true,
  document:{getElementById(id){return /^(alignment-|embedding-|text-alignment-)/.test(id) ? node(id) : null;},
    querySelectorAll(){return [];}, querySelector(){return null;}},
  setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},
  clearTimeout(id){timers.delete(id);},
  async fetch(url, options={}) {
    calls.push([url, options.method || 'GET']);
    if (url.endsWith('/runtime')) {
      if(runtimeError) throw new Error('connection interrupted');
      if(pendingRuntimeRead && !options.method) return new Promise(resolve=>{pendingRuntimeRead=resolve;});
      return {ok:true,json:async()=>({...runtime})};
    }
    return {ok:true,json:async()=>({compute:runtime.compute, models:[{
      id:'minilm-l12-v2', installed:modelInstalled,
      state:modelInstalled?'installed':'not_installed', error:''}]})};
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const settle = async()=>{for(let i=0;i<15;i++) await Promise.resolve();};
const enter = async()=>{context.showSettingsCategory('text-alignment-settings');await settle();};
const poll = async()=>{const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());await settle();};
(async()=>{
''' + scenario + r'''
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        source = Path(__file__).resolve().parents[1] / "src/me_finder/static/js/60-settings.js"
        result = subprocess.run([shutil.which("node"), "-e", script, str(source)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_completion_refreshes_compute_availability(self):
        self.run_ui(r'''
await enter();
runtime={...runtime,state:'provisioning',operation:'install'};
node('alignment-runtime-action').onclick();await settle();
runtime={...runtime,state:'installed',operation:null,installed:true,
  compute:{available:true,provider:'independent'}};
await poll();
assert.match(node('alignment-compute-status').textContent,/可用 · 独立运行时/);
''')

    def test_uninstall_completion_refreshes_model_files(self):
        self.run_ui(r'''
modelInstalled=true;
runtime={...runtime,installed:true,state:'installed',compute:{available:true,provider:'independent'}};
await enter();
runtime={...runtime,state:'cleaning',operation:'uninstall'};
node('alignment-runtime-uninstall').onclick();await settle();
modelInstalled=false;
runtime={...runtime,installed:false,state:'not_installed',operation:null,compute:{available:false,provider:'none'}};
await poll();
assert.equal(node('embedding-model-state-minilm-l12-v2').textContent,'未下载');
assert.match(node('alignment-compute-status').textContent,/不可用/);
''')

    def test_initial_read_failure_has_a_working_retry(self):
        self.run_ui(r'''
runtimeError=true;await enter();
assert.equal(node('alignment-runtime-component').hidden,false);
assert.equal(node('alignment-runtime-state').textContent,'读取失败');
assert.equal(node('alignment-runtime-action').hidden,false);
runtimeError=false;node('alignment-runtime-action').onclick();await settle();
assert.equal(node('alignment-runtime-state').textContent,'未安装');
''')

    def test_old_status_read_cannot_stop_install_polling(self):
        self.run_ui(r'''
await enter();
const old={...runtime};
pendingRuntimeRead=true;context.showSettingsCategory('text-alignment-settings');await settle();
const complete=pendingRuntimeRead;pendingRuntimeRead=null;
runtime={...runtime,state:'provisioning',operation:'install'};
node('alignment-runtime-action').onclick();await settle();
complete({ok:true,json:async()=>old});await settle();
assert.match(node('alignment-runtime-state').textContent,/安装中/);
assert.ok(timers.size>0,'polling must continue');
''')

    def test_unsupported_platform_does_not_claim_bundled_runtime(self):
        self.run_ui(r'''
runtime={...runtime,supported:false};await enter();
assert.doesNotMatch(node('alignment-compute-note').textContent,/随应用提供/);
assert.match(node('alignment-compute-note').textContent,/不支持|不可用/);
''')

    def test_incompatible_install_does_not_claim_ready(self):
        self.run_ui(r'''
runtime={...runtime,installed:true,state:'installed',update_available:true,
  compute:{available:false,provider:'none',detail:'已安装的独立运行时不兼容，请重新安装'}};
await enter();
assert.match(node('alignment-runtime-state').textContent,/不兼容|不可用/);
assert.doesNotMatch(node('alignment-compute-note').textContent,/已独立安装在本机，离线生成/);
''')
