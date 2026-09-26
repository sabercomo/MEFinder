"""C4 事件委托：动态按钮只携带数据，点击时调用已注册的动作。"""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIONS_JS = ROOT / "src/me_finder/static/js/08-actions.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node 不可用，跳过事件委托测试")
class DelegatedActionTests(unittest.TestCase):
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
if (JSON.stringify(calls) !== JSON.stringify([['click', "a'\"<b>&", '2']])) {
  throw new Error(JSON.stringify(calls));
}
"""
        subprocess.run([NODE, "-e", script, str(ACTIONS_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")
