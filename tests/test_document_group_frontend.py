"""Execute group actions with delayed responses to catch duplicate submissions."""

import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
LIBRARY_JS = Path(__file__).resolve().parents[1] / "src/me_finder/static/js/30-library.js"
HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const button = {disabled: false, textContent: 'original'};
const input = {value: 'Work', focus() {}};
let resolveFetch, resolveConfirm;
let requests = 0, confirmations = 0, refreshes = 0;
const toasts = [];
const context = {
  libraryStore: {deleteSelection: new Set(['source-1'])},
  document: {getElementById(id) { return id === 'grp-create-btn' ? button : input; }},
  fetch() { requests++; return new Promise(resolve => {resolveFetch = resolve;}); },
  showToast(message) { toasts.push(message); },
  showAppConfirm() { confirmations++; return new Promise(resolve => {resolveConfirm = resolve;}); }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.documentGroupById = () => ({title: 'Work'});
context.loadDocumentGroups = async () => {refreshes++;};
context.renderGroupScopeSelector = context.renderDocumentGroupManager = context.renderLibraryList = () => {};
context.clearLibrarySelection = () => context.libraryStore.deleteSelection.clear();
const action = process.argv[2], outcome = process.argv[3];
const invoke = () => action === 'create' ? context.createDocumentGroupInline()
  : action === 'assign' ? context.assignSelectedToGroupAction('group-1', button)
  : context.deleteDocumentGroupAction('group-1', button);
(async () => {
  const pending = invoke();
  assert.equal(button.disabled, true);
  assert.notEqual(button.textContent, 'original');
  await invoke();
  if (action === 'delete') {
    assert.equal(confirmations, 1);
    assert.equal(requests, 0);
    resolveConfirm(outcome !== 'cancel');
    await new Promise(resolve => setImmediate(resolve));
  }
  if (outcome !== 'cancel') {
    assert.equal(requests, 1);
    resolveFetch({ok: outcome === 'success', json: async () =>
      outcome === 'success' ? {} : {error: 'Request failed'}});
  }
  await pending;
  assert.equal(button.disabled, false);
  assert.equal(refreshes, outcome === 'success' ? 1 : 0);
  if (outcome === 'error') assert.deepEqual(toasts, ['Request failed']);
  if (action === 'assign') assert.equal(context.libraryStore.deleteSelection.size,
    outcome === 'success' ? 0 : 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "node is required for frontend execution tests")
class DocumentGroupActionTests(unittest.TestCase):
    def test_group_manager_keeps_creation_and_distinguishes_same_title_versions(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const body = {innerHTML: ''};
const sources = [
  {source_file_id: 'native', title: 'Same book', parser_type: 'native_text', parser_label: 'PDF'},
  {source_file_id: 'mineru', title: 'Same book', parser_type: 'mineru_structured', parser_label: 'MinerU'}
];
const context = {
  libraryStore: {deleteSelection: new Set(), sources, documentGroups: [{
    document_group_id: 'group', title: 'Work', base_source_file_id: 'native',
    members: sources.map(s => ({source_file_id: s.source_file_id, display_name: 'Same version'}))
  }]},
  document: {getElementById() {return body;}},
  esc: value => value, sourceFormatLabel: () => 'PDF',
  libLangChipLabel: () => '英语'
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.documentSupportsTextAlignment = () => true;
context.libraryLanguageCode = () => 'en';
context.syncDocumentGroupPairAction = () => {};
const menu = context.groupScopeManageOptionsHTML();
assert.equal(Array.from(menu.matchAll(/<button\b/g)).length, 1);
assert.ok(menu.includes('onclick="closeAppSelects();openManageDocumentGroups()">管理作品组…</button>'));
context.renderDocumentGroupManager();
assert.ok(body.innerHTML.includes('id="grp-create-input"'));
assert.ok(body.innerHTML.includes('onclick="createDocumentGroupInline()">新建</button>'));
for (const parser of ['原生文本', 'MinerU']) {
  assert.ok(body.innerHTML.includes('<span>PDF · ' + parser + '</span>'));
  assert.ok(body.innerHTML.includes('Same version · 英语 · PDF · ' + parser + '</option>'));
}
"""
        result = subprocess.run(
            [NODE, "-e", script, str(LIBRARY_JS)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pending_actions_submit_once_and_restore_buttons(self) -> None:
        for action in ("create", "assign", "delete"):
            for outcome in ("success", "error"):
                with self.subTest(action=action, outcome=outcome):
                    self._run(action, outcome)

    def test_cancelled_delete_restores_button_without_request(self) -> None:
        self._run("delete", "cancel")

    def _run(self, action, outcome):
        result = subprocess.run(
            [NODE, "-e", HARNESS, str(LIBRARY_JS), action, outcome],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
