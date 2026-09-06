from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "me_finder"


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_web_boundary_stays_split_by_responsibility(self) -> None:
        # web.py is now only the HTTP composition root + platform PDF openers;
        # service wiring lives in web_runtime.py and domain route assembly in
        # http_routes.py.  Caps only ratchet
        # down — when a file hits its cap, move a real responsibility out.
        limits = {
            "web.py": 700,
            "web_runtime.py": 725,
            "http_routes.py": 260,
            "web_http.py": 800,
            "web_assets.py": 120,
            # 备份轮转与身份核对/去重已迁出，上限随之下调（只降不升）。
            # 段落行/payload 形状转换已下沉到 persistence，上限随之下调。
            "database.py": 1325,
            "persistence/paragraph_payload.py": 100,
            "database_backup.py": 220,
            "index_identity.py": 240,
            # MCP 用例层：0.5.0 一轮加了 5 个工具就从 431 涨到 951 行，
            # 是增长最快却唯一不受约束的文件，纳入门禁。
            "application/literature_verification_service.py": 1050,
            # 协议层拆出后应保持配置无关且稳定。
            "openai_compatible.py": 850,
            "vision_api.py": 950,
            # 领域层四大模块此前完全不受约束，因而长到 1800~2900 行。
            # 先按当前行数封顶止血，再按既定顺序逐个拆分：
            # semantic_alignment 锚点抽取器 → text_alignment 覆盖/快照
            # → bibliographic_metadata 写库路径 → search 上帝类。
            # 人工覆盖与配方快照已拆出，上限随之下调（只降不升）。
            "text_alignment.py": 2400,
            "alignment_snapshots.py": 150,
            "alignment_overrides.py": 425,
            # 锚点抽取与结构识别已拆出，上限随之下调（只降不升）。
            "semantic_alignment.py": 1675,
            "alignment_anchors.py": 875,
            "alignment_structure.py": 500,
            # 对齐产出的段质量门（噪声段/脚注块降级）拆为独立叶模块。
            "alignment_segment_quality.py": 200,
            # 共用取值底座与三类文献抽取器已拆出，上限随之下调。
            "bibliographic_metadata.py": 1550,
            "bibliographic_values.py": 325,
            "bibliographic_thesis.py": 300,
            "bibliographic_journal.py": 350,
            "bibliographic_marx_engels.py": 175,
            "search.py": 1850,
        }
        for relative, limit in limits.items():
            lines = (PACKAGE / relative).read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(
                len(lines),
                limit,
                f"{relative} 已超过 {limit} 行，请先拆出新的明确边界。",
            )

    def test_http_routes_are_assembled_by_product_domain_without_container(self) -> None:
        source = (PACKAGE / "http_routes.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            functions,
            {
                "assemble_archive_routes",
                "assemble_bibliography_routes",
                "assemble_import_routes",
                "assemble_library_routes",
                "assemble_parser_settings_routes",
                "assemble_preference_routes",
                "assemble_reader_routes",
                "assemble_shell_routes",
            },
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in tree.body),
            "路由装配应使用显式领域函数，不应引入通用容器。",
        )
        runtime = (PACKAGE / "web_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("controller_get_routes = {", runtime)
        self.assertNotIn("controller_post_routes = {", runtime)

    def test_http_transport_does_not_import_concrete_adapters(self) -> None:
        source = (PACKAGE / "web_http.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        internal_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(internal_modules, {"application", "http_range"})

    def test_document_query_application_service_contains_no_sql(self) -> None:
        source = (
            PACKAGE / "application" / "document_query_service.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("sqlite3", imported_modules)
        self.assertNotIn("SELECT ", source)

    def test_application_layer_does_not_depend_on_web_transport(self) -> None:
        violations = []
        for path in sorted((PACKAGE / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(
                    module.endswith((".web", ".web_http"))
                    or module in {"web", "web_http"}
                    for module in modules
                ):
                    violations.append(path.name)
        self.assertEqual(violations, [])

    def test_application_layer_does_not_import_sqlite_adapters(self) -> None:
        violations = []
        for path in sorted((PACKAGE / "application").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").endswith("persistence")
                ):
                    violations.append(path.name)
        self.assertEqual(violations, [])

    def test_no_new_import_cycles_appear(self) -> None:
        """包内循环依赖只能减少，不能新增。

        0.5.0 曾因 general_model 与 vision_api 互相 import（一侧用函数内懒
        import 掩盖）而悄悄新增一条环；协议层 openai_compatible 拆出后消除。
        这里冻结剩余的已知环，任何新环都会让门禁失败。
        """

        edges: dict[str, set[str]] = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            relative = path.relative_to(PACKAGE).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            if not module:
                continue
            targets = edges.setdefault(module, set())
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                base = module.split(".")
                prefix = (
                    ".".join(base[: -node.level])
                    if node.level <= len(base)
                    else ""
                )
                target = (
                    f"{prefix}.{node.module}".strip(".")
                    if node.module
                    else prefix
                )
                if target:
                    targets.add(target)

        colors: dict[str, int] = {}
        cycles: list[tuple[str, ...]] = []

        def visit(node: str, stack: list[str]) -> None:
            colors[node] = 1
            stack.append(node)
            for peer in sorted(edges.get(node, ())):
                if colors.get(peer) == 1:
                    cycles.append(tuple(sorted(set(stack[stack.index(peer):]))))
                elif colors.get(peer, 0) == 0:
                    visit(peer, stack)
            stack.pop()
            colors[node] = 2

        for module in sorted(edges):
            if colors.get(module, 0) == 0:
                visit(module, [])

        # 0.4.x 遗留的
        # database <-> text_alignment <-> calibration_library <-> bibliographic_metadata
        # 已消除：根因是 bibliographic_metadata 为了一个纯函数
        # (paragraph_payload_for_storage) 顶层依赖整个 database；该函数与段落行
        # 补水一并下沉到 persistence/paragraph_payload.py 后，这条边不复存在。
        # 包内现无循环依赖，任何新环都会让门禁失败。
        known: set[tuple[str, ...]] = set()
        self.assertEqual(set(cycles), known)

    def test_persistence_layer_does_not_import_domain_modules(self) -> None:
        # persistence owns connection policy, schema and migrations; it must not
        # depend *upward* on domain modules.  Schema DDL that domain code needs
        # lives in persistence.schema_installers, imported downward instead.
        violations = []
        for path in sorted((PACKAGE / "persistence").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level >= 2:
                    violations.append(f"{path.name}: from {'.' * node.level}{node.module or ''}")
        self.assertEqual(violations, [])

    def test_openai_transport_layer_knows_nothing_about_config_stores(self) -> None:
        """协议层必须保持配置无关，否则环会绕回来。

        openai_compatible 只负责线上格式（端点形状、鉴权头、请求体、模型列表
        归一化、客户端）。它一旦 import 任何配置模块（vision_api /
        general_model），vision_api ↔ general_model 的环就会重新出现。
        """

        tree = ast.parse(
            (PACKAGE / "openai_compatible.py").read_text(encoding="utf-8")
        )
        internal = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(internal, set())

    def test_frontend_core_state_is_grouped_by_domain(self) -> None:
        state = (PACKAGE / "static" / "js" / "00-state.js").read_text(
            encoding="utf-8"
        )
        for store in (
            "searchStore",
            "libraryStore",
            "parserStore",
            "settingsStore",
            "importStore",
        ):
            self.assertIn(f"const {store} =", state)


if __name__ == "__main__":
    unittest.main()
