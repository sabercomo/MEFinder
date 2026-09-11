"""Exercise real settings JavaScript: existing/migration, cancellation and retry."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class DataLocationSettingsUiTests(unittest.TestCase):
    def test_selection_modes_cancel_retry_and_restart_state(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const nodes = {}, calls = [], messages = [];
let finish, confirm = true;
const node = id => nodes[id] || (nodes[id] = {style: {}, hidden: false, disabled: false});
const context = {
  THEME_BUILTIN_CSS_IDS: [], THEME_PRESET_MAP: {light: {}}, THEME_MODE_DEFAULT: {light: 'light'},
  settingsStore: {appearanceState: {mode: 'light', light: 'light'}},
  initAppearanceSystemWatch() {},
  document: {documentElement: {dataset: {}}, getElementById(id) {return id.startsWith("data-location-") ? node(id) : null;}, querySelectorAll() {return [];}},
  fetch(url, options) {calls.push([url, options.body ? JSON.parse(options.body) : null]); return new Promise(resolve => {finish = resolve;});},
  showAppConfirm: async () => confirm,
  showToast(message) {messages.push(message);}
};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const reply = (body, ok=true) => finish({ok, status: ok ? 200 : 400, json: async () => body});
(async () => {
  let request = context.chooseDataLocation('existing');
  assert.equal(node('data-location-open').disabled, true);
  await context.chooseDataLocation('migrate'); assert.equal(calls.length, 1);
  reply({target_path: '/OneDrive/MEFinder', document_count: 65, paragraph_count: 63994}); await request;
  assert.equal(calls[0][1].mode, 'existing');
  assert.equal(node('data-location-migrate').textContent, '使用此资料库');
  assert.ok(node('data-location-pending-note').textContent.includes('65 部'));
  request = context.chooseDataLocation('migrate'); reply({cancelled: true}); await request;
  assert.equal(context.settingsStore.pendingDataLocationMode, 'existing');
  confirm = false; await context.migrateDataLocation(); assert.equal(calls.length, 2);
  confirm = true; request = context.migrateDataLocation(); await new Promise(setImmediate);
  assert.equal(calls.at(-1)[0], '/api/data-location/switch');
  reply({error: '同步尚未完成'}, false); await request;
  assert.equal(node('data-location-migrate').disabled, false); assert.equal(messages.length, 1);
  request = context.chooseDataLocation('migrate'); reply({target_path: '/new/MEFinder'}); await request;
  request = context.migrateDataLocation(); await new Promise(setImmediate);
  assert.equal(calls.at(-1)[0], '/api/data-location/migrate');
  reply({current_path: '/old', target_path: '/new/MEFinder', restart_required: true}); await request;
  assert.equal(node('data-location-current').textContent, '/old');
  assert.equal(node('data-location-migrate').hidden, true);
  assert.equal(node('data-location-open').disabled, true);
  const count = calls.length; await context.chooseDataLocation('existing'); assert.equal(calls.length, count);
  request = context.loadDataLocation(); reply({current_path: '/old', pending_path: '/new/MEFinder', restart_required: true}); await request;
  assert.equal(node('data-location-target').textContent, '/new/MEFinder');
  assert.equal(node('data-location-status').textContent, '重启后生效');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        source = Path(__file__).resolve().parents[1] / 'src/me_finder/static/js/60-settings.js'
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
