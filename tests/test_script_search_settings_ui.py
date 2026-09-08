"""Execute the shipped settings script to verify save failure and race handling."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class ScriptSearchSettingsUiTests(unittest.TestCase):
    def test_save_pending_duplicate_failure_and_unavailable(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const input = {}, status = {}, messages = [], calls = [];
let finish;
const context = {
  THEME_BUILTIN_CSS_IDS: [], THEME_PRESET_MAP: {light: {}}, THEME_MODE_DEFAULT: {light: 'light'},
  settingsStore: {appearanceState: {mode: 'light', light: 'light'},
    scriptFoldingEnabled: true, scriptFoldingAvailable: true, scriptFoldingSaving: false},
  initAppearanceSystemWatch() {},
  document: {getElementById(id) { return id === 'script-folding-enabled' ? input : id === 'script-folding-status' ? status : null; }, querySelectorAll() {return [];}},
  fetch(url, options) { calls.push([url, JSON.parse(options.body)]); return new Promise(resolve => {finish = resolve;}); },
  showToast(message) {messages.push(message);}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  const first = context.setScriptFolding(false);
  assert.equal(input.disabled, true); assert.equal(input.checked, false);
  await context.setScriptFolding(true); assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ['/api/preferences', {script_folding: false}]);
  finish({ok: true, json: async () => ({script_folding: false})}); await first;
  assert.equal(input.disabled, false); assert.equal(input.checked, false);
  const failure = context.setScriptFolding(true);
  finish({ok: false, json: async () => ({error: '磁盘不可写'})}); await failure;
  assert.equal(input.checked, false); assert.equal(input.disabled, false);
  assert.equal(messages.length, 1);
  context.settingsStore.scriptFoldingAvailable = false;
  await context.setScriptFolding(true);
  assert.equal(input.disabled, true); assert.equal(calls.length, 2);
  assert.ok(status.textContent.includes('未能加载'));
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        source = Path(__file__).resolve().parents[1] / 'src/me_finder/static/js/60-settings.js'
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
