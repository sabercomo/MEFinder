"""C5 设置页 DOM 渲染保持主题语义和目录路径原值。"""

import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
SETTINGS_JS = Path(__file__).resolve().parents[1] / "src/me_finder/static/js/60-settings.js"


@unittest.skipUnless(NODE, "node 不可用，跳过 DOM 渲染测试")
class SettingsDomTests(unittest.TestCase):
    def test_theme_and_scan_directory_nodes(self) -> None:
        script = r"""
const fs = require('fs');
const vm = require('vm');
function element(tag) {
  return {tag, children: [], dataset: {}, attrs: {}, style: {}, classList: {toggle() {}},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attrs[name] = value; },
    querySelectorAll() { return []; }};
}
const options = element('div');
const scan = element('div');
const elements = {'theme-options': options, 'scan-dir-list': scan};
const context = {
  document: {getElementById(id) { return elements[id] || null; },
    querySelectorAll() { return []; }, createElement: element,
    createElementNS(ns, tag) { if (ns !== 'http://www.w3.org/2000/svg') throw Error(ns); return element(tag); }},
  settingsStore: {appearanceState: {mode: 'light', light: 'dawn', dark: 'night', customThemes: {}},
    appearanceEditMode: 'light', scanDirectories: ["a'\"<b>&"]},
  THEME_BUILTIN_CSS_IDS: ['dawn'],
  THEME_PRESETS: [{id: 'dawn', label: '晨', mode: 'light', builtinCss: true, desc: '安静'}],
  THEME_PRESET_MAP: {dawn: {id: 'dawn', label: '晨', mode: 'light'}},
  initAppearanceSystemWatch() {}, teSystemPrefersDark() { return false; },
  MEFinderActions: {register() {}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const button = options.children[0];
if (button.dataset.themeChoice !== 'dawn' || button.dataset.action !== 'selectThemeChoice'
    || button.attrs.role !== 'radio' || button.attrs['aria-checked'] !== 'false'
    || button.children[0].dataset.previewTheme !== 'dawn'
    || button.children[1].children[0].children[0].textContent !== '晨') throw Error('theme option differs');
context.renderScanDirectories();
const row = scan.children[0];
if (row.title !== "a'\"<b>&" || row.children[1].textContent !== "a'\"<b>&"
    || row.children[2].dataset.action !== 'removeScanDirectory'
    || row.children[2].dataset.index !== '0') throw Error('scan directory differs');
"""
        subprocess.run([NODE, "-e", script, str(SETTINGS_JS)], check=True,
                       capture_output=True, text=True, encoding="utf-8")
