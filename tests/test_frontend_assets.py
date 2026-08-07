"""前端资源装配的回归网。

`web.py` 在导入时把 templates/index.html 与 static/ 下的 CSS、JS 拼成一个单文件
HTML 字符串。本模块守住这条装配链本身：占位标记必须全部被替换、每个资源文件的
正文必须真的进入产物、拼装长度必须与各部分长度吻合。

拆分 app.js / app.css 为多个小文件时，这些断言证明"搬家"没有丢内容、没有改顺序。
"""

import hashlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.me_finder import __version__
from src.me_finder.web import HTML, _PACKAGE_DIR, render_html

# 装配后必须一个都不剩的占位标记。
PLACEHOLDERS = (
    "/*__APP_CSS__*/",
    "/*__READER_CSS__*/",
    "//__APP_JS__",
    "//__READER_JS__",
    "__APP_VERSION__",
)

def _split_dir_assets(subdir, suffix):
    """某个资源目录下的拆分文件，按文件名排序 —— 顺序即加载顺序。"""

    directory = _PACKAGE_DIR / subdir
    if not directory.is_dir():
        return ()
    return tuple(
        f"{subdir}/{path.name}"
        for path in sorted(directory.glob(f"*{suffix}"), key=lambda p: p.name)
    )


def _split_js_assets():
    return _split_dir_assets("static/js", ".js")


def _split_css_assets():
    return _split_dir_assets("static/css", ".css")


# 必须被装进产物的静态资源；拆分后在此追加新文件即可。
CSS_ASSETS = _split_css_assets() + ("static/reader.css",)

JS_ASSETS = _split_js_assets() + ("static/reader.js",)


def _read(relative):
    return (_PACKAGE_DIR / relative).read_text(encoding="utf-8")


def _existing(relatives):
    return [rel for rel in relatives if (_PACKAGE_DIR / rel).is_file()]


class FrontendAssetAssemblyTests(unittest.TestCase):
    def test_no_placeholder_survives_assembly(self):
        for marker in PLACEHOLDERS:
            self.assertNotIn(
                marker,
                HTML,
                f"占位标记 {marker} 未被替换，说明资源装配漏掉了一步",
            )

    def test_version_is_injected(self):
        self.assertIn(f"MEFinder v{__version__}", HTML)
        self.assertIn(f"v{__version__}", HTML)

    def test_every_asset_body_reaches_the_document(self):
        """每个资源的首尾实质内容都必须出现在产物里。"""

        for relative in _existing(CSS_ASSETS + JS_ASSETS):
            text = _read(relative)
            lines = [line for line in text.splitlines() if line.strip()]
            self.assertTrue(lines, f"{relative} 是空文件")
            self.assertIn(
                lines[0],
                HTML,
                f"{relative} 的首行未进入产物",
            )
            self.assertIn(
                lines[-1],
                HTML,
                f"{relative} 的末行未进入产物",
            )

    def test_assembled_length_matches_sum_of_parts(self):
        """产物长度 = 模板 + 各资源 - 占位标记，容差为版本号替换带来的差值。"""

        template = _read("templates/index.html")
        assets = sum(len(_read(rel)) for rel in _existing(CSS_ASSETS + JS_ASSETS))
        consumed = sum(
            len(marker) for marker in PLACEHOLDERS if marker != "__APP_VERSION__"
        )
        version_delta = template.count("__APP_VERSION__") * (
            len("__APP_VERSION__") - len(__version__)
        )
        expected = len(template) + assets - consumed - version_delta
        self.assertEqual(
            len(HTML),
            expected,
            "装配长度与各部分之和不符：可能有资源被漏掉、重复拼入或顺序被改动",
        )

    def test_scripts_and_styles_are_single_blocks(self):
        """资源内联进 <style>/<script>，不引入额外的外部请求。"""

        self.assertEqual(HTML.count("<style>"), 2)
        self.assertEqual(HTML.count("<script>"), 3)
        self.assertNotIn('src="/static/app.js', HTML)
        self.assertNotIn('href="/static/app.css', HTML)
        self.assertNotIn("<script src=", HTML)
        self.assertNotIn('rel="stylesheet"', HTML)

    def test_js_assets_land_inside_script_blocks(self):
        """JS 必须在 <script> 内，CSS 必须在 <style> 内，顺序不能颠倒。"""

        style_end = HTML.index("</style>")
        first_script = HTML.index("<script>", style_end)
        code_line = re.compile(
            r"^(?:async\s+function|function|let|const|var|\(function)\b"
        )
        for relative in _existing(JS_ASSETS):
            marker = next(
                line
                for line in _read(relative).splitlines()
                if code_line.match(line)
            )
            self.assertGreater(
                HTML.index(marker),
                first_script,
                f"{relative} 的内容落在 <script> 之外",
            )

    def test_reader_js_loads_after_app_js(self):
        """reader.js 依赖 app.js 的 showToast 兜底，顺序不能反。"""

        self.assertLess(
            HTML.index("function callWindowsWindow"),
            HTML.index("global.MEFinderReader"),
        )

    def test_split_js_files_load_in_filename_order(self):
        """拆分文件共享全局作用域，加载顺序必须严格等于文件名排序。"""

        split = _split_js_assets()
        self.assertGreater(len(split), 1, "static/js/ 下应有多个拆分文件")
        code_line = re.compile(
            r"^(?:async\s+function|function|let|const|var|\(function)\b"
        )
        positions = []
        for relative in split:
            marker = next(
                line
                for line in _read(relative).splitlines()
                if code_line.match(line)
            )
            positions.append((relative, HTML.index(marker)))
        self.assertEqual(
            positions,
            sorted(positions, key=lambda item: item[1]),
            f"拆分文件在产物里的先后与文件名排序不一致：{[p[0] for p in positions]}",
        )

    def test_split_css_files_load_in_filename_order(self):
        """CSS 后写的规则覆盖先写的，级联顺序必须严格等于文件名排序。"""

        split = _split_css_assets()
        self.assertGreater(len(split), 1, "static/css/ 下应有多个拆分文件")
        positions = []
        for relative in split:
            marker = next(
                line for line in _read(relative).splitlines() if line.strip()
            )
            positions.append((relative, HTML.index(marker)))
        self.assertEqual(
            positions,
            sorted(positions, key=lambda item: item[1]),
            f"拆分样式在产物里的先后与文件名排序不一致：{[p[0] for p in positions]}",
        )
        # 主题变量必须最先落地，对话框/toast 收尾。
        self.assertTrue(split[0].endswith("00-themes.css"), split[:1])
        self.assertTrue(split[-1].endswith("90-dialogs-toast.css"), split[-1:])

    def test_state_loads_first_and_init_loads_last(self):
        """00-state 定义全局变量，90-init 立即执行；两端顺序错了会直接白屏。"""

        split = _split_js_assets()
        self.assertTrue(split[0].endswith("00-state.js"), split[:1])
        self.assertTrue(split[-1].endswith("90-init.js"), split[-1:])
        self.assertLess(
            HTML.index("let currentMode"),
            HTML.index("async function loadMeta"),
        )

    def test_theme_injection_preserves_length(self):
        """render_html 只换 <html> 标签，不得改变其余字节。"""

        rendered = render_html("midnight")
        self.assertIn('data-theme="midnight"', rendered)
        self.assertNotIn('data-theme="frost-blue"', rendered.split(">", 1)[0])
        self.assertEqual(
            len(rendered) - len('data-theme="midnight"'),
            len(HTML) - len('data-theme="frost-blue"'),
        )

    def test_no_duplicate_function_definitions(self):
        """同名顶层函数只能定义一次：拆分时最容易犯的错是把一段代码复制两份。"""

        names = re.findall(
            r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            HTML,
            re.MULTILINE,
        )
        duplicates = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(duplicates, [], f"顶层函数被重复定义：{duplicates}")

    def test_inline_handlers_have_definitions(self):
        """模板与 JS 生成的 on*= 处理器引用的函数必须真的存在。"""

        defined = set(
            re.findall(
                r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                HTML,
                re.MULTILINE,
            )
        )
        template = _read("templates/index.html")
        referenced = set()
        for attr in re.findall(r"\bon\w+\s*=\s*\"([^\"]*)\"", template):
            referenced.update(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", attr))
        builtins = {
            "if",
            "return",
            "getElementById",
            "querySelector",
            "stopPropagation",
            "click",
            "void",
        }
        missing = sorted(n for n in referenced - builtins if n not in defined)
        self.assertEqual(missing, [], f"内联处理器引用了不存在的函数：{missing}")


class FrontendAssetBaselineTests(unittest.TestCase):
    """记录基线指纹。拆分前后此值必须一致；有意改动前端时同步更新。"""

    BASELINE_SHA256 = (
        "0ef5849307ff466d420f3951b736d9e986cc1b7a8ad3ae1e091ed1209f282c09"
    )
    BASELINE_BYTES = 574909

    def test_assembled_document_matches_baseline(self):
        payload = HTML.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(
            (len(payload), digest),
            (self.BASELINE_BYTES, self.BASELINE_SHA256),
            "装配产物与基线不一致。纯搬移时应完全相同；"
            "若确实改了前端内容，请更新本类的基线常量。",
        )


if __name__ == "__main__":
    unittest.main()
