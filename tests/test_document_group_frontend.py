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
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set(['source-1'])},
  document: {getElementById(id) { return id === 'grp-create-btn' ? button : input; }},
  fetch() { requests++; return new Promise(resolve => {resolveFetch = resolve;}); },
  showToast(message) { toasts.push(message); },
  showAppConfirm() { confirmations++; return new Promise(resolve => {resolveConfirm = resolve;}); }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
const dependencies = {
  documentGroupById: () => ({title: 'Work'}),
  loadDocumentGroups: async () => {refreshes++;},
  renderGroupScopeSelector: () => {},
  renderDocumentGroupManager: () => {},
  renderLibraryList: () => {},
  clearLibrarySelection: () => context.libraryStore.deleteSelection.clear()
};
const action = process.argv[2], outcome = process.argv[3];
const invoke = () => action === 'create' ? library.createDocumentGroupInline(dependencies)
  : action === 'assign' ? library.assignSelectedToGroupAction('group-1', button, dependencies)
  : library.deleteDocumentGroupAction('group-1', button, dependencies);
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
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set(), sources, documentGroups: [{
    document_group_id: 'group', title: 'Work', base_source_file_id: 'native',
    members: sources.map(s => ({source_file_id: s.source_file_id, display_name: 'Same version'}))
  }]},
  document: {getElementById() {return body;}},
  esc: value => value, sourceFormatLabel: () => 'PDF',
  libLangChipLabel: () => '英语', libLangCode: () => 'EN'
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
const dependencies = {
  documentSupportsTextAlignment: () => true,
  libraryLanguageCode: () => 'en',
  syncDocumentGroupPairAction: () => {}
};
const menu = library.groupScopeManageOptionsHTML();
assert.equal(Array.from(menu.matchAll(/<button\b/g)).length, 1);
assert.ok(menu.includes('onclick="closeAppSelects();openManageDocumentGroups()"'), menu);
assert.ok(menu.includes('管理作品组…'), menu);
library.renderDocumentGroupManager(dependencies);
assert.ok(body.innerHTML.includes('id="grp-create-input"'));
assert.ok(body.innerHTML.includes('onclick="createDocumentGroupInline()">新建</button>'));
// 手风琴默认展开首个作品组，成员行降噪：解析器/格式收进标题 tooltip，不再单独占一行。
for (const parser of ['原生文本', 'MinerU']) {
  assert.ok(body.innerHTML.includes('title="Same book · PDF · ' + parser + '"'), body.innerHTML);
}
// 语言以短代码 chip 呈现在成员行内。
assert.ok(body.innerHTML.includes('grp-lang-chip">EN'), body.innerHTML);
// 基准版有「基准」标签；单选锚点用 radio。
assert.ok(body.innerHTML.includes('grp-base-tag">基准'), body.innerHTML);
assert.ok(body.innerHTML.includes('class="grp-base-radio is-base"'), body.innerHTML);
"""
        result = subprocess.run(
            [NODE, "-e", script, str(LIBRARY_JS)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_combine_auto_title_and_base_pick_original_language(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set()},
  document: {getElementById() { return null; }},
  libraryLanguageCode(src) { return String((src && src.language_code) || ''); }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
const de = {source_file_id: 'de', title: 'Grundlinien', language_code: 'de'};
const zh = {source_file_id: 'zh', title: '法哲学原理', language_code: 'zh-Hans'};
const en = {source_file_id: 'en', title: 'Philosophy of Right', language_code: 'en'};
// Title prefers the Chinese member; base prefers the non-Chinese, non-English original.
assert.equal(library.autoGroupTitle([de, zh, en]), '法哲学原理');
assert.equal(library.autoGroupBaseId([de, zh, en]), 'de');
// No foreign original: base falls to the non-Chinese English edition.
assert.equal(library.autoGroupBaseId([zh, en]), 'en');
// Two Chinese editions: base falls to the first.
assert.equal(library.autoGroupBaseId([zh, {source_file_id: 'zh2', language_code: 'zh-Hant'}]), 'zh');
"""
        result = subprocess.run(
            [NODE, "-e", script, str(LIBRARY_JS)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_add_version_picker_rows_are_click_to_add_typeahead(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const body = {innerHTML: '', focus() {}};
const sources = [
  {source_file_id: 'de', title: 'Grundlinien', language_code: 'de'},
  {source_file_id: 'zh', title: '法哲学原理', language_code: 'zh-Hans'},
  {source_file_id: 'candidate', title: 'Philosophy of Right', language_code: 'en'}
];
const context = {
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set(), sources, documentGroups: [{
    document_group_id: 'group', title: 'Work', base_source_file_id: 'de',
    members: [{source_file_id: 'de'}, {source_file_id: 'zh'}]
  }]},
  document: {getElementById() { return body; }},
  esc: value => value, sourceFormatLabel: () => 'PDF',
  libLangChipLabel: code => code, libLangCode: code => code,
  libraryLanguageCode: src => String((src && src.language_code) || ''),
  documentSupportsTextAlignment: () => true,
  showToast() {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
// Opening the picker renders the candidate list into the (stubbed) container.
library.toggleGroupPicker('group');
const markup = body.innerHTML;
// The only candidate (not already a member) is a click-to-add button, not a checkbox.
assert.ok(markup.includes('onclick="addGroupMemberDirect(\'group\', \'candidate\''), markup);
assert.ok(markup.includes('＋ 加入'), markup);
assert.ok(!markup.includes('type="checkbox"'), markup);
assert.ok(!markup.includes('加入所选'), markup);
"""
        result = subprocess.run(
            [NODE, "-e", script, str(LIBRARY_JS)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_title_suggestion_banner_offers_one_click_combine(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const body = {innerHTML: '', focus() {}};
const sources = [
  {source_file_id: 'a', title: '法哲学原理', language_code: 'zh-Hans'},
  {source_file_id: 'b', title: '法哲学原理 ', language_code: 'zh-Hant'},
  {source_file_id: 'grouped', title: '法哲学原理', language_code: 'en'},
  {source_file_id: 'other', title: '利维坦', language_code: 'zh-Hans'}
];
const context = {
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set(), sources, documentGroups: [{
    document_group_id: 'g', title: 'Existing', base_source_file_id: 'grouped',
    members: [{source_file_id: 'grouped'}]
  }]},
  document: {getElementById() { return body; }},
  esc: value => value, sourceFormatLabel: () => 'PDF',
  libLangChipLabel: code => code, libLangCode: code => code,
  libraryLanguageCode: src => String((src && src.language_code) || ''),
  documentSupportsTextAlignment: () => true, showToast() {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
library.renderDocumentGroupManager({
  documentSupportsTextAlignment: () => true,
  libraryLanguageCode: src => String((src && src.language_code) || ''),
  syncDocumentGroupPairAction: () => {}
});
const markup = body.innerHTML;
// The two ungrouped same-title editions cluster; the already-grouped one is excluded.
assert.ok(markup.includes('class="grp-suggest"'), markup);
assert.ok(markup.includes('有 2 份同名文献没有归组'), markup);
assert.ok(markup.includes('onclick="combineSuggestedGroupAction(0, this)"'), markup);
// A single-title work ("利维坦") is not suggested.
assert.ok(!markup.includes('《利维坦》'), markup);
"""
        result = subprocess.run(
            [NODE, "-e", script, str(LIBRARY_JS)], capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_realign_all_recomputes_each_existing_pair_once(self) -> None:
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const button = {disabled: false, textContent: ''};
const body = {innerHTML: ''};
const groups = [
  {
    document_group_id: 'g1', title: 'Work 1', members: [],
    alignments: [
      {status: 'completed', pivot_source_file_id: 'a', target_source_file_id: 'b'},
      {status: 'completed', pivot_source_file_id: 'b', target_source_file_id: 'a'},
      {status: 'superseded', pivot_source_file_id: 'a', target_source_file_id: 'c'}
    ]
  },
  {
    document_group_id: 'g2', title: 'Work 2', members: [],
    alignments: [
      {status: 'completed', pivot_source_file_id: 'd', target_source_file_id: 'e'}
    ]
  }
];
const requests = [];
const confirmations = [];
const toasts = [];
const context = {
  module: {exports: {}},
  libraryStore: {deleteSelection: new Set(), sources: [], documentGroups: groups, groupScopeId: ''},
  searchStore: {groupId: ''},
  document: {getElementById(id) {
    if (id === 'group-manage-body') return body;
    if (id === 'group-realign-all') return button;
    return null;
  }},
  fetch: async (url, options) => {
    if (url === '/api/document-groups') {
      return {ok: true, json: async () => ({document_groups: groups})};
    }
    requests.push(JSON.parse(options.body));
    return {ok: true, json: async () => ({result: {status: 'completed'}})};
  },
  showAppConfirm: async message => {confirmations.push(message); return true;},
  showToast: (message, tone) => toasts.push([message, tone]),
  esc: value => value,
  sourceFormatLabel: () => 'PDF',
  libLangChipLabel: code => code,
  libLangCode: code => code,
  libraryLanguageCode: () => 'en'
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const library = context.module.exports;
(async () => {
  assert.equal(library.documentGroupExistingAlignmentPairs(groups).length, 2);
  await library.realignAllTextAlignmentsAction(button);
  assert.equal(confirmations.length, 1);
  assert.ok(confirmations[0].includes('2 组'));
  assert.equal(requests.length, 2);
  assert.ok(requests.every(request => request.force === true));
  assert.deepEqual(
    requests.map(request => [request.document_group_id, request.pivot_source_file_id, request.target_source_file_id]),
    [['g1', 'a', 'b'], ['g2', 'd', 'e']]
  );
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '重新对齐已有译本（2 组）');
  assert.deepEqual(toasts, [['已重新对齐 2 组译本', 'success']]);
})().catch(error => { console.error(error); process.exitCode = 1; });
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
