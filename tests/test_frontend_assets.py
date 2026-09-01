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

TOP_LEVEL_JS_DECLARATION = re.compile(
    r"^(?:(?:async\s+)?function\s*\*?|class|let|const|var)\s+"
    r"([A-Za-z_$][\w$]*)"
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

    def test_mineru_brand_asset_is_packaged(self):
        """MinerU 账号与统计页共用官方矢量标识，打包时不能漏掉。"""

        logo = _PACKAGE_DIR / "static" / "brands" / "mineru.svg"
        self.assertTrue(logo.is_file())
        content = logo.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 24 24"', content)
        self.assertIn("fill-rule=\"evenodd\"", content)

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
            HTML.index("const searchStore = {"),
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

    def test_no_duplicate_top_level_js_identifiers(self):
        """共享全局作用域中的函数、类和变量均不得重名。"""

        definitions = {}
        for relative in _existing(JS_ASSETS):
            for line_number, line in enumerate(_read(relative).splitlines(), 1):
                match = TOP_LEVEL_JS_DECLARATION.match(line)
                if match:
                    definitions.setdefault(match.group(1), []).append(
                        f"{relative}:{line_number}"
                    )
        duplicates = {
            name: locations
            for name, locations in definitions.items()
            if len(locations) > 1
        }
        self.assertEqual(
            duplicates,
            {},
            f"顶层 JavaScript 标识符重名：{duplicates}",
        )

    def test_inline_handlers_have_definitions(self):
        """模板与 JS 生成的 on*= 处理器引用的函数必须真的存在。"""

        # 处理器目标既可以是顶层函数声明，也可以是 IIFE 模块（如 05-theme-engine.js）
        # 通过 `global.name =` / `window.name =` 显式挂到全局的公共符号 —— 后者在
        # 运行时同样让 onclick="name()" 可解析，所以两种都算「已定义」。
        defined = set(
            re.findall(
                r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                HTML,
                re.MULTILINE,
            )
        )
        defined.update(
            re.findall(
                r"^\s*(?:global|window)\.([A-Za-z_$][\w$]*)\s*=",
                HTML,
                re.MULTILINE,
            )
        )
        referenced = set()
        for attr in re.findall(r"\bon\w+\s*=\s*\"([^\"]*)\"", HTML):
            referenced.update(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", attr))
        builtins = {
            "if",
            "return",
            "getElementById",
            "querySelector",
            "stopPropagation",
            "preventDefault",
            "click",
            "setTimeout",
            "void",
        }
        missing = sorted(n for n in referenced - builtins if n not in defined)
        self.assertEqual(missing, [], f"内联处理器引用了不存在的函数：{missing}")

    def test_large_domain_modules_keep_bounded_global_command_surfaces(self):
        """大型领域模块必须留在 IIFE；直接全局命令不得重新无界增长。"""

        budgets = {
            # 作品组归组命令：一键归组 / 同名合并建议 / 内嵌加入 /「加入作品组」下拉 /
            # 「生成对照」版本自定义下拉的打开与选择 / 管理弹窗手风琴的组展开与对照折叠、
            # 下拉顶部命名新建（净 45）。
            # 0.5.2 +1：管理弹窗头部「＋ 新建作品组」切换 toggleGroupCreate（净 46）。
            # 0.5.2 +1：作品组搜索框 groupSearchInputAction（净 47）。
            "static/js/30-library.js": 47,
            # +1：书目「语言」自定义下拉的选择入口 pickBibLanguage。
            "static/js/40-bibliography.js": 27,
            "static/js/70-vision.js": 24,
            "static/js/71-vision-providers.js": 18,
            "static/js/80-import.js": 20,
        }
        for relative, budget in budgets.items():
            with self.subTest(asset=relative):
                source = _read(relative)
                name = Path(relative).name
                self.assertIn(f"(function (global) {{  // module: {name}", source)
                self.assertTrue(
                    source.rstrip().endswith(
                        "}(typeof window !== 'undefined' ? window : "
                        "(typeof globalThis !== 'undefined' ? globalThis : this)));"
                    )
                )
                direct_exports = re.findall(
                    r"^\s*global\.([A-Za-z_$][\w$]*)\s*=", source, re.MULTILINE
                )
                self.assertLessEqual(
                    len(direct_exports),
                    budget,
                    f"{relative} 直接全局命令超过预算：{direct_exports}",
                )

    def test_vision_runtime_and_remote_providers_remain_separate(self):
        runtime = _read("static/js/70-vision.js")
        providers = _read("static/js/71-vision-providers.js")
        self.assertIn("global.MEFinder.parserRuntime = parserRuntimeAPI", runtime)
        self.assertNotIn("function loadVisionProviders", runtime)
        self.assertIn("global.MEFinder.visionProviders = visionProvidersAPI", providers)
        self.assertNotIn("function loadMineruConfig", providers)
        self.assertNotIn("function loadLocalOCRConfig", providers)


class FrontendAssetBaselineTests(unittest.TestCase):
    """记录基线指纹。拆分前后此值必须一致；有意改动前端时同步更新。"""

    # 0.4.7 主题修正及本地 OCR 设置、导入状态接入后同步更新。
    # 0.4.7 后续：本地 OCR 一键安装、进度、取消、重新验证和卸载入口。
    # 0.4.7 后续：稳定安装状态，补充传输估时，并对齐解析设置保存操作。
    # 0.4.7 后续：统一 MinerU 与其他解析接口的编辑表单视觉结构。
    # 0.4.7 后续：补充本地 OCR 自动路由说明，收紧未配置的视觉 API 入口。
    # 0.4.7 后续：本地 MinerU Pipeline/VLM 托管安装与组件清单更新入口。
    # 0.4.7 后续：区分自行部署 MinerU 与 MEFinder 托管安装状态。
    # 0.4.7 后续：按硬件显示托管方案，并补充 MinerU 下载进度与估时。
    # 0.4.7 后续：传递系统代理并收敛 MinerU 安装失败文案。
    # 0.4.7 后续：收敛 Hugging Face 模型下载失败文案。
    # 0.4.7 后续：区分模型分片处理与实时传输速度。
    # 0.4.7 后续：MinerU 与其他解析 API 设置页改为使用完整内容宽度。
    # 0.4.7 后续：解析接口/账号的添加编辑器改为卡片 + 标签左/控件右两栏 + 底部操作条。
    # 0.4.7 后续：收敛编辑器说明文案，MinerU Token 增加"前往获取"外链（系统浏览器）。
    # 0.4.7 后续：编辑器说明与外链统一移到输入框下方（右列），左列只留标签。
    # 0.4.7 后续：导入队列“全部取消”覆盖所有未完成任务，并仅进行一次批量确认。
    # 0.4.7 后续：MinerU 本地部署默认展开，并同步托管服务停止后的汇总状态。
    # 0.4.7 后续：MinerU 本地部署对齐本地 OCR 的扁平行式布局，自部署设置默认折叠。
    # 0.4.7 后续：MinerU 本地部署标题字号与本地 OCR 设置标题对齐。
    # 0.4.8：搜索、文献库、解析、设置和导入核心状态迁入领域 Store。
    # 0.4.9：作品组自动对齐与结构化阅读器跨版本定位。
    # 0.4.9 后续：两版本结构化阅读器双栏对照与按 Segment 自动跟随。
    # 0.4.9 后续：导入、检索与文献库界面支持 EPUB。
    # 0.4.9 后续：中英直连对照、作品组工作台、Word/EPUB 分栏与双栏阅读修正。
    # 0.4.9 后续：双栏对照阅读器与跨版本语义定位前端接线（reader.js locate 流程等）。
    # 0.4.9 后续：设置页新增“导出清理与页码锚点”（格式中立，Markdown/未来 EPUB 共用）。
    # 0.4.9 后续：单书 EPUB 3 导出作为共享规范化层的第二个 renderer 接入。
    # 0.4.9 后续：三种页码锚点合并展示，Markdown/EPUB 页面噪声固定清理。
    # 0.4.9 后续：完整页码锚点改为 Markdown/EPUB 的统一默认。
    # 0.4.9 后续：作品组操作防重复提交，确认弹窗位于父弹窗上方。
    # 0.4.9 后续：作品组下拉菜单加高，并按窗口高度限制滚动区域。
    # 0.4.9 后续：取消页码模式控件，常规 Markdown/EPUB 使用隐藏印刷页码。
    # 0.4.9 后续：作品组下拉菜单只保留管理入口，新建仍在管理弹窗内。
    # 0.5.0：结构化阅读器隐藏页眉页脚与印刷页码（保留 text_raw 偏移坐标系），
    #        并新增“显示/隐藏页眉页脚”开关；reader.js/reader.css 相应改动。
    # 0.5.0：文献库操作菜单（三点）打开时取消其 SVG 的无效 180° 旋转；
    #        40-library.css 用 id 提优先级明确取消旋转。
    # 0.5.0：#7 前端全局作用域收敛试点——05-theme-engine.js 包进 IIFE，
    #        私有 helper 不再泄漏到全局，仅 17 个公共符号显式 global.* 导出。
    # 0.5.0：#7 试点续 —— 70-vision.js 同样包进 IIFE（85 私有 helper 收敛，46 公共导出）。
    # 0.5.0：#7 续 —— 10-shell/20-search/50-calibration/60-settings 四个无 node
    #        测试依赖的文件一并包进 IIFE（约 91 个私有 helper 收敛）。
    # 0.5.0：新增「通用本地模型」——本地OCR区块末尾的自部署 OpenAI 兼容端点卡片
    #        + 导入页专属解析单选项；后端复用 vision provider 路径。
    # 0.5.0：恢复检索 IIFE 中供动态 onclick 使用的公共入口；设置页移除无控件的
    #        Markdown/EPUB 固定清理策略说明；阅读器新增页眉页脚按钮后同步为五列头部。
    # 0.5.1：30-library.js 收进 IIFE，白盒测试改用显式依赖注入与 module.exports。
    # 0.5.1：内联处理器门禁覆盖 JS 动态模板，并补齐既有 IIFE 的事件公共面。
    # 0.5.1：40-bibliography/80-import 收进 IIFE，跨模块调用收口到 MEFinder 命名 API。
    # 0.5.1：70-vision 按解析器运行时/远程视觉供应商拆成 70/71，跨文件改走命名 API。
    # 0.5.1：应用补丁版本注入从 0.5.0 更新为 0.5.1；装配字节数不变。
    # 作品组：书名清洗盖到主库列表与组名、「加入作品组」下拉顶部命名新建 + 齿轮管理入口、
    # 管理弹窗改手风琴（组默认收成一行、对照默认折叠、成员行降噪）、页脚改「关闭」、
    # 「生成对照」版本改自定义下拉（fixed 菜单，避免滚动容器裁切）；
    # 书目信息新增「语言」下拉（人工覆盖自动识别）；vision 刷新统计改走 MEFinder.visionProviders.render。
    # 0.5.2：管理作品组弹窗重做——新建收进头部「＋ 新建作品组」（默认收起表单）、
    # 成员行改「radio 基准锚点 + 标题基准标签 + 语言短代码 chip（JA/ZH/EN）+ hover 设为基准」、
    # 次级动作右对齐；libLangCode 新增短代码。
    # 0.5.2 续：组容器去盒化——组不再各自成卡片，改发丝线分隔（弹窗为唯一容器）；
    # 组头标题改纯文本外观的衬线书名（聚焦才显编辑框）、语言摘要改灰字、删除组降为 hover 浮出的横排文字；
    # 成员书名用衬线。
    # 0.5.2 续二：版本名改行内小灰字（聚焦才显编辑框，不再占满整行）；基准行去掉整条色带；
    # 顶部加「搜索作品组」框；「生成对照」主操作移到底部动作条（实心强调色 + 「已直接对照」状态）；
    # 计数改裸数字；groupSearchInputAction 新增；成员元信息 chip 与版本名间加「·」分隔。
    # 0.5.2 续三：「＋ 新建作品组」从右上角移到搜索工具行（搜索占宽、新建在右）；
    # 弹窗标题保持「管理作品组」+ 副标题「整理同一作品的不同版本」；
    # 组标题 input 用 field-sizing/size 贴合内容宽度，语言摘要紧贴书名不再被顶远。
    # （reader.js 双栏对照重做尚未纳入本基线。）
    BASELINE_SHA256 = (
        "74c3f2bb0481e3f6bbef37aa2c835c27f4f9d0af3c7eb52be9fccbdcb744d89d"
    )
    BASELINE_BYTES = 1018399

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
