"""C4 事件委托：动态按钮只携带数据，点击时调用已注册的动作。"""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIONS_JS = ROOT / "src/me_finder/static/js/08-actions.js"
LIBRARY_JS = ROOT / "src/me_finder/static/js/30-library.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node 不可用，跳过事件委托测试")
class DelegatedActionTests(unittest.TestCase):
    def test_library_facets_keep_unsaved_edit_guard(self):
        script = r"""
const calls = [];
global.MEFinder = {bibliography: {
  async guardLeaveDetail() { calls.push('guard'); return false; }
}};
global.MEFinderActions = {actions: {}, register(name, callback) {
  this.actions[name] = callback;
}};
require(process.argv[1]);
let stopped = 0;
const event = {stopImmediatePropagation() { stopped++; }};
(async () => {
  await MEFinderActions.actions.setLibraryFacet(event, {
    dataset: {kind: 'lang', value: "a'\"<b>&"}
  });
  await MEFinderActions.actions.removeLibraryFacet(event, {
    dataset: {kind: 'lang'}
  });
  if (stopped !== 2 || JSON.stringify(calls) !== JSON.stringify(['guard', 'guard'])) {
    throw new Error(JSON.stringify({stopped, calls}));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run([NODE, "-e", script, str(LIBRARY_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def test_library_nested_controls_do_not_open_entry(self):
        script = r"""
global.libraryStore = {
  sources: [{source_file_id: "a'\"<b>&", source_type: 'pdf'}],
  deleteSelection: new Set(), exportRunning: false
};
global.isLibraryDeleteSelectable = source => !!source && source.source_type === 'pdf';
global.document = {querySelectorAll() { return []; }, getElementById() { return null; }};
const opened = [];
global.MEFinder = {works: {
  open(id) { opened.push(id); }, syncLibraryAssignButton() {}
}};
global.MEFinderActions = {actions: {}, register(name, callback) {
  this.actions[name] = callback;
}};
require(process.argv[1]);
let stopped = 0;
const event = {stopImmediatePropagation() { stopped++; }};
const id = "a'\"<b>&";
MEFinderActions.actions.toggleLibraryEntrySelection(event, {
  dataset: {sourceId: id}, checked: true
});
if (!libraryStore.deleteSelection.has(id)) throw new Error('checkbox did not select');
MEFinderActions.actions.toggleLibraryEntrySelection(event, {
  dataset: {sourceId: id}, checked: false
});
if (libraryStore.deleteSelection.has(id)) throw new Error('checkbox did not clear');
MEFinderActions.actions.openLibraryWork(event, {dataset: {workId: id}});
if (JSON.stringify(opened) !== JSON.stringify([id]) || stopped !== 3) {
  throw new Error(JSON.stringify({opened, stopped}));
}
"""
        subprocess.run([NODE, "-e", script, str(LIBRARY_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def test_library_entry_click_is_registered_and_preserves_suppression(self):
        script = r"""
global.libraryStore = {suppressSelectionClick: true};
global.MEFinderActions = {actions: {}, register(name, callback) {
  this.actions[name] = callback;
}};
require(process.argv[1]);
let prevented = 0;
let stopped = 0;
MEFinderActions.actions.openLibraryEntry({
  preventDefault() { prevented++; },
  stopImmediatePropagation() { stopped++; }
}, {dataset: {id: "a'\"<b>&"}});
if (prevented !== 1 || stopped !== 1) {
  throw new Error(JSON.stringify({prevented, stopped}));
}
"""
        subprocess.run([NODE, "-e", script, str(LIBRARY_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def test_click_uses_nearest_action_and_passes_original_event(self):
        script = r"""
const fs = require('fs');
let listener;
global.document = {addEventListener(type, callback) {
  if (type === 'click') listener = callback;
}};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const calls = [];
MEFinderActions.register('applyBookCandidate', (event, button) => {
  calls.push([event.marker, button.dataset.sourceId, button.dataset.index]);
});
const button = {dataset: {action: 'applyBookCandidate', sourceId: "a'\"<b>&", index: '2'}};
const child = {closest(selector) {
  if (selector !== '[data-action]') throw new Error('wrong selector');
  return button;
}};
listener({target: child, marker: 'click'});
listener({target: {closest() {return null;}}, marker: 'ignored'});
listener({target: document, marker: 'document'});
if (JSON.stringify(calls) !== JSON.stringify([['click', "a'\"<b>&", '2']])) {
  throw new Error(JSON.stringify(calls));
}
"""
        subprocess.run([NODE, "-e", script, str(ACTIONS_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")

    def test_role_button_supports_enter_and_space(self):
        script = r"""
const fs = require('fs');
const listeners = {};
global.document = {addEventListener(type, callback) { listeners[type] = callback; }};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const calls = [];
MEFinderActions.register('enterBibEdit', (event, button) => calls.push(button.dataset.focusField));
const row = {dataset: {action: 'enterBibEdit', focusField: 'title'}, role: 'button'};
const child = {closest(selector) {
  if (selector !== '[data-action][role="button"]') throw new Error(selector);
  return row;
}};
let prevented = 0;
for (const key of ['Enter', ' ', 'Escape']) {
  listeners.keydown({target: child, key, preventDefault() { prevented++; }});
}
listeners.keydown({target: document, key: 'Enter', preventDefault() { prevented++; }});
if (JSON.stringify(calls) !== JSON.stringify(['title', 'title']) || prevented !== 2) {
  throw new Error(JSON.stringify({calls, prevented}));
}
"""
        subprocess.run([NODE, "-e", script, str(ACTIONS_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")
