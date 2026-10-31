from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "me_finder"

# 棘轮基线(2026-09-25,v0.5.7 重构 A0):persistence 之外的 SQLite 散落点。
# 只许删不许增——迁走一处就把这里对应的计数减掉(减到 0 删掉条目),
# 新增调用点或新文件都会让门禁失败。目标见 docs/refactor-v0.5.7-plan.md 阶段 A。
# 2026-09-25 A2 完成:connect 已清零,连接一律走 persistence/connection.py。
# 2026-09-29 架构后续第 2 步:structured_reader.py / translation_works.py 的读 SQL
# 移入 persistence/structured_reader_reads.py、translation_work_reads.py,移出基线。
SQLITE_CONNECT_OUTSIDE_PERSISTENCE: dict[str, int] = {}

# 仍待收进 persistence 仓储的散落 SQL。只许删不许增。
SQL_EXECUTE_OUTSIDE_PERSISTENCE = {
    "data_location.py": 5,
    "document_deletion.py": 1,
    "export/service.py": 10,
    "alignment/edition_folio_anchors.py": 4,
    "index_publisher.py": 1,
    "indexer.py": 1,
    "parsers/statistics.py": 3,
}

# 2026-10-31(0.6.1)已核实的例外:自带一个独立数据库的仓储模块。
# large_document/job_ledger.py 不是"业务代码里散落 SQL",它就是
# data/parser_jobs.sqlite3 这个**另一个库**的仓储——自带 LEDGER_SCHEMA_VERSION
# 与迁移,连接仍走 persistence/connection.py。整体搬进 persistence/ 会把
# large_document 的领域 dataclass 和状态词表一并拖进存储层,而且它需要
# large_document.slicing.SliceDescriptor,那是 persistence 的红线(禁止 from ..
# 上行 import)。因此留在 large_document/,改为登记例外并用下面三条守卫钉住:
# 单一 SQL 执行者、单一 schema 所有者、读路径仍走 persistence。
# 它同样是棘轮:新增 SQL 调用点必须在这里把计数改对。
SQL_EXECUTE_REVIEWED_STORES = {
    "large_document/job_ledger.py": 43,
}

# 已核实的非 SQL 调用只按文件 + 完整调用目标豁免，不放行整个模块。
# 其余 execute* 一律计入棘轮；新增同名服务需要审阅后登记，避免漏掉动态 SQL。
NON_SQL_EXECUTE_TARGETS = {
    "application/import_orchestrator.py": {"self._parser_executor.execute"},
    "application/literature_verification_service.py": {"SearchService.execute"},
    "application/parallel_passage_service.py": {"SearchService.execute"},
    "application/script_search.py": {"SearchService.execute"},
}

# C3: direct thread creation must have an explicit lifecycle owner. Startup
# warm-up and Zotero sync moved to tasks/background_tasks.py.
THREAD_CREATION_OWNERS = {
    "components/catalog.py",
    "desktop_backend.py",
    "import_queue.py",
    "components/local_ocr_installer.py",
    "components/alignment_runtime.py",
    "components/embedding_models.py",
    "components/mineru.py",
    "onefile_cleanup.py",
    "tasks/background_tasks.py",
    "text_alignment_controller.py",
}

# 2026-10-31(0.6.1):通用的用户可见错误改为 errors.UserFacingError。MinerUError
# 只留给确实与 MinerU 解析打交道的模块(读取 retry_with_new_task /
# allow_parser_fallback 或抛出 MinerU 任务错误);其他模块不得再借用它。
MINERU_ERROR_IMPORTERS = {
    "application/import_parser_executor.py",
    "large_document/mineru_accounts.py",
    "parsers/mineru_local_settings.py",
    "parsers/mineru_provider.py",
    "pdf/parser_adapters.py",
}


# 建表 / 改表语句里的表名。用于钉住"一个库只有一个 schema 所有者"。
DDL_TABLE = re.compile(
    r"(?:CREATE|ALTER)\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)",
    re.IGNORECASE,
)


def _package_module(dotted: str) -> str | None:
    """Map an absolute import onto a package-relative module name.

    ``me_finder.search.engine`` and ``src.me_finder.search.engine`` both become
    ``search.engine``; anything outside the package returns ``None``.
    """

    for root in ("src.me_finder", "me_finder"):
        if dotted == root:
            return ""
        if dotted.startswith(f"{root}."):
            return dotted[len(root) + 1 :]
    return None


def _sqlite_usage_outside_persistence() -> tuple[dict[str, int], dict[str, int]]:
    """Count connection and execute calls, excluding reviewed service targets."""

    connects: dict[str, int] = {}
    executes: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if "__pycache__" in relative.parts or relative.parts[0] == "persistence":
            continue
        name = relative.as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            func = node.func
            if (
                func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                connects[name] = connects.get(name, 0) + 1
            if func.attr in {"execute", "executemany", "executescript"}:
                if ast.unparse(func) not in NON_SQL_EXECUTE_TARGETS.get(name, set()):
                    executes[name] = executes.get(name, 0) + 1
    return connects, executes


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_thread_creation_has_an_explicit_owner(self) -> None:
        owners = set()
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Thread"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "threading"
                ):
                    owners.add(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(owners, THREAD_CREATION_OWNERS)

    def test_mineru_error_is_imported_only_by_mineru_modules(self) -> None:
        importers = set()
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "MinerUError" for alias in node.names
                ):
                    importers.add(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(
            importers,
            MINERU_ERROR_IMPORTERS,
            "通用的用户可见错误请用 errors.UserFacingError;MinerUError 只用于 "
            "MinerU 解析相关模块。",
        )

    def test_component_installers_share_install_mechanics(self) -> None:
        # 2026-10-31(0.6.1):uv 日志进度、下载测速、校验、uv 解包与子进程停止
        # 收进 components/install.py;三个安装器只保留清单、状态与安装步骤。
        duplicated = {
            "_UV_DOWNLOAD_PATTERN",
            "_UV_DOWNLOADED_PATTERN",
            "_BINARY_SIZE_MULTIPLIERS",
            "_stop_process",
            "_verify_download",
        }
        for name in (
            "components/local_ocr_installer.py",
            "components/alignment_runtime.py",
            "components/mineru.py",
        ):
            tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
            defined = set()
            imports_kernel = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.Assign):
                    defined.update(
                        target.id for target in node.targets
                        if isinstance(target, ast.Name)
                    )
                elif isinstance(node, ast.ImportFrom) and node.module == "install":
                    imports_kernel = True
            self.assertEqual(defined & duplicated, set(), name)
            self.assertTrue(imports_kernel, name)

    def test_component_installers_share_cancel_and_shutdown(self) -> None:
        """取消与关停只有一处实现。

        2026-10-31(0.6.1)续:三个安装器此前各写一遍"等待取消事件→停子进程→抛
        自己的取消错误"和"取消全部→join 工作线程"。收进 components/install.py
        后,这里钉住两件事:取消文案不许再硬编码,关停的 join 循环不许回到安装
        器里。alignment_runtime 的 close() 是单实例状态 + begin_shutdown,结构
        本就不同,登记为例外。
        """

        installers = (
            "components/local_ocr_installer.py",
            "components/alignment_runtime.py",
            "components/mineru.py",
        )
        kernel = (PACKAGE / "components/install.py").read_text(encoding="utf-8")
        for message in ("操作已取消。", "正在取消并清理…"):
            self.assertIn(message, kernel)
        for name in installers:
            source = (PACKAGE / name).read_text(encoding="utf-8")
            for message in ("操作已取消。", "正在取消并清理…"):
                self.assertNotIn(
                    message,
                    source,
                    f"{name}:取消文案请用 install.CANCELLED_MESSAGE / "
                    "CANCELLING_MESSAGE，不要再硬编码。",
                )

        join_owners = set()
        for path in sorted((PACKAGE / "components").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "join"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "thread"
                ):
                    join_owners.add(path.name)
        self.assertEqual(
            join_owners,
            {
                "install.py",
                # 单实例状态 + begin_shutdown,且要避开 join 当前线程。
                "alignment_runtime.py",
                # 组件目录与模型下载各有自己的线程账本,不走安装器的关停路径。
                "catalog.py",
                "embedding_models.py",
            },
            "安装器的关停 join 循环请用 install.cancel_and_join_operations。",
        )

    def test_every_package_module_is_tracked_in_git(self) -> None:
        """源码包里的每个 .py 都必须在版本控制里。

        2026-10-31：`.gitignore` 的 `components/` 规则（本意是从源码跑 serve 时
        在工作目录生成的组件状态）顺带命中了源码子包
        `src/me_finder/components/`，分子包那轮的 `__init__.py` 因此从未入库 ——
        命名空间包让导入照常工作，冻结包也照常打出来，CI 没有红过。规则已把
        该目录显式放回，这条守卫钉住不再复发。
        """

        if not (ROOT / ".git").exists():
            self.skipTest("不是 Git 检出（源码导出副本）")
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "src/me_finder"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        tracked = {ROOT / name for name in listed}
        untracked = sorted(
            path.relative_to(ROOT).as_posix()
            for path in PACKAGE.rglob("*.py")
            if "__pycache__" not in path.parts and path not in tracked
        )
        self.assertEqual(
            untracked,
            [],
            "这些模块在磁盘上但不在 Git 里；清洁检出会缺它们。"
            "先确认不是 .gitignore 顺带命中了源码目录。",
        )

    def test_every_source_subpackage_declares_an_init(self) -> None:
        # 六个子包都有 __init__.py，components 当初漏了；缺 __init__ 时 Python
        # 按命名空间包照常导入，所以只能靠守卫发现，不会自己报错。
        missing = sorted(
            directory.relative_to(PACKAGE).as_posix()
            for directory in PACKAGE.rglob("*")
            if directory.is_dir()
            and "__pycache__" not in directory.parts
            and any(directory.glob("*.py"))
            and not (directory / "__init__.py").is_file()
        )
        self.assertEqual(missing, [])

    def test_root_entry_scripts_import_existing_modules(self) -> None:
        # 2026-10-31：模块移入子包后，desktop.py 冻结包的对齐计算进程分派曾漏改；
        # 根目录入口不在 unittest 导入范围内，这里静态核对其包内导入目标存在。
        missing = []
        for name in ("desktop.py", "mefinder_mcp.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("src.me_finder"):
                    continue
                parts = node.module.split(".")[2:]
                target = PACKAGE.joinpath(*parts)
                if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
                    missing.append(f"{name}: {node.module}")
        self.assertEqual(missing, [])

    def test_modules_do_not_import_each_others_private_names(self) -> None:
        # 2026-10-31(0.6.1):跨模块共用的辅助函数/常量一律公开命名，并从定义
        # 它的模块直接导入，不经旧模块转手。下划线名只在本模块内使用。
        violations = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        violations.append(
                            f"{path.relative_to(PACKAGE).as_posix()}: "
                            f"{'.' * node.level}{node.module or ''}.{alias.name}"
                        )
        self.assertEqual(violations, [])

    def test_sqlite_connect_outside_persistence_only_shrinks(self) -> None:
        connects, _executes = _sqlite_usage_outside_persistence()
        self.assertEqual(
            connects,
            SQLITE_CONNECT_OUTSIDE_PERSISTENCE,
            "persistence 之外不得新增 sqlite3.connect;连接请走 "
            "persistence/connection.py。迁走调用点后请同步下调本文件的基线。",
        )

    def test_table_exists_helper_has_single_definition(self) -> None:
        definitions = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.FunctionDef) and node.name in {
                    "table_exists",
                    "_table_exists",
                }:
                    definitions.append(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(definitions, ["persistence/connection.py"])

    def test_sql_execute_calls_outside_persistence_only_shrink(self) -> None:
        _connects, executes = _sqlite_usage_outside_persistence()
        self.assertEqual(
            executes,
            SQL_EXECUTE_OUTSIDE_PERSISTENCE | SQL_EXECUTE_REVIEWED_STORES,
            "persistence 之外不得新增执行 SQL 的调用点;SQL 请收进 persistence 仓储。"
            "迁走调用后请下调计数，清零后删项；非 SQL execute 调用须逐目标核实。"
            "自带独立数据库的仓储模块登记进 SQL_EXECUTE_REVIEWED_STORES，"
            "并须同时满足下面三条 ledger 守卫。",
        )

    def test_reviewed_sql_stores_stay_separate_from_the_backlog(self) -> None:
        # 例外不是口子:登记过的仓储模块不得同时留在待收口清单里，
        # 否则"还剩多少散落 SQL"这个数字就不可信了。
        self.assertEqual(
            set(SQL_EXECUTE_OUTSIDE_PERSISTENCE) & set(SQL_EXECUTE_REVIEWED_STORES),
            set(),
        )

    def test_job_ledger_is_the_only_sql_owner_in_large_document(self) -> None:
        """账本是 large_document 下唯一执行 SQL 的模块。

        例外只对"一个模块独占一个库"成立。一旦 engine / credential_pool /
        mineru_accounts 也开始自己写 SQL，例外的前提就不存在了。
        """

        executors = set()
        for path in sorted((PACKAGE / "large_document").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"execute", "executemany", "executescript"}
                ):
                    executors.add(path.name)
        self.assertEqual(executors, {"job_ledger.py"})

    def test_parser_job_ledger_has_one_schema_owner(self) -> None:
        """账本的建表与迁移只许写在 job_ledger.py 里。"""

        ledger_tables = {"document_jobs", "slice_jobs"}
        owners = set()
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tables = DDL_TABLE.findall(path.read_text(encoding="utf-8"))
            if ledger_tables & set(tables):
                owners.add(path.relative_to(PACKAGE).as_posix())
        self.assertEqual(owners, {"large_document/job_ledger.py"})
        ledger = (PACKAGE / "large_document/job_ledger.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("LEDGER_SCHEMA_VERSION", ledger)

    def test_parser_job_ledger_reads_from_outside_go_through_persistence(self) -> None:
        """账本以外的模块只读账本时走 persistence/parser_job_reads.py。

        写入口仍是 JobLedger;后续阶段读账本不得再在业务模块里裸写 SELECT。
        """

        tree = ast.parse(
            (PACKAGE / "persistence/parser_job_reads.py").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level],
            ["connection"],
        )

    def test_web_boundary_stays_split_by_responsibility(self) -> None:
        # web.py is now only the HTTP composition root + platform PDF openers;
        # service wiring lives in web_runtime.py and domain route assembly in
        # http_routes.py.  Caps only ratchet
        # down — when a file hits its cap, move a real responsibility out.
        limits = {
            "web.py": 700,
            "web_runtime.py": 280,
            "import_assembly.py": 290,
            "alignment_assembly.py": 85,
            "library_assembly.py": 85,
            "settings_assembly.py": 195,
            "http_routes.py": 260,
            # v0.5.7 B2：导入/上传/搜索/校准移出，分发只查 RouteTable（原 763 行）。
            "web_http.py": 405,
            "http_route_table.py": 200,
            "upload_import_controller.py": 220,
            "search_controller.py": 100,
            "calibration_config_controller.py": 60,
            "web_assets.py": 120,
            # 备份轮转与身份核对/去重已迁出，上限随之下调（只降不升）。
            # 段落行/payload 形状转换已下沉到 persistence，上限随之下调。
            "database.py": 470,
            "persistence/fts_index.py": 195,
            "persistence/index_build.py": 265,
            "persistence/storage_optimization.py": 170,
            "persistence/source_replace.py": 380,
            "persistence/paragraph_payload.py": 100,
            "persistence/short_gram_index.py": 245,
            # SQLite 能力探测与只登记待补的触发器拆为叶模块（旧 SQLite 兼容）。
            "persistence/short_gram_schema.py": 100,
            "database_backup.py": 220,
            "index_identity.py": 240,
            # MCP 用例层：0.5.0 一轮加了 5 个工具就从 431 涨到 951 行，
            # 是增长最快却唯一不受约束的文件，纳入门禁。
            "application/literature_verification_service.py": 1050,
            # 协议层拆出后应保持配置无关且稳定。
            "parsers/openai_compatible.py": 850,
            "parsers/vision_api.py": 950,
            # 领域层四大模块此前完全不受约束，因而长到 1800~2900 行。
            # 先按当前行数封顶止血，再按既定顺序逐个拆分：
            # semantic_alignment 锚点抽取器 → text_alignment 覆盖/快照
            # → bibliographic_metadata 写库路径 → search 上帝类。
            # 人工覆盖与配方快照已拆出，上限随之下调（只降不升）。
            "alignment/text_alignment.py": 960,
            "alignment/generation.py": 820,
            "alignment/segmentation.py": 350,
            "alignment/snapshots.py": 150,
            "alignment/overrides.py": 425,
            # 锚点抽取与结构识别已拆出，上限随之下调（只降不升）。
            "alignment/semantic.py": 1675,
            "alignment/anchors.py": 875,
            "alignment/structure.py": 500,
            # 对齐产出的段质量门（噪声段/脚注块降级）拆为独立叶模块。
            "alignment/segment_quality.py": 200,
            # 共有词假锚点移除（leave-one-out + 源端更优替代门）拆为独立叶模块。
            "alignment/anchor_validation.py": 150,
            # 共用取值底座与三类文献抽取器已拆出，上限随之下调。
            "bibliographic/metadata.py": 1550,
            "bibliographic/values.py": 325,
            "bibliographic/thesis.py": 300,
            "bibliographic/journal.py": 350,
            "bibliographic/marx_engels.py": 175,
            # 任务 2 已把 search 上帝类拆为 recall/scoring/anchors/citation/
            # assembly/contract 六个单向依赖模块；门面 317 行，上限随之下调，
            # 新模块按当前行数封顶。
            # 2026-10-31(0.6.1)：JSON 索引降级为纯导出格式，内存召回整支删除
            # （recall_memory.py 不复存在），三个模块的上限按删后行数重封。
            "search/engine.py": 281,
            # search_passages 的相关性召回拆为 search_recall_passages，SQL 下沉到
            # persistence/passage_reads.py 与 paragraph_scope.py；
            # search_recall.py 的上限随之只降不升。
            "search/recall.py": 351,
            # 2026-09-30：两字扫描改为截断后才解析段落、子串计重合度（gram_overlap）；
            # 同日改为先读排序列、截断后按 rowid 取整行（persistence.passage_reads）。
            "search/recall_passages.py": 117,
            "search/scoring.py": 250,
            "search/anchors.py": 240,
            "search/citation.py": 140,
            "search/assembly.py": 295,
            "persistence/passage_reads.py": 245,
            # 2026-10-31(0.6.1)：安装器共用内核。它是"机制"的去处，不是杂物抽屉：
            # 清单、状态模型、重试策略与安装步骤仍归各安装器。
            "components/install.py": 409,
            # 0.6.1：导入编排、原生解析、段落/页码锚点构造分别归属。
            "pdf/extractors.py": 900,
            "pdf/native.py": 580,
            "pdf/paragraphs.py": 440,
            # 2026-10-01：普通版式跨页段落按命中页收窄引用页码，独立成模块。
            "search/cross_page.py": 70,
            "search/contract.py": 60,
            "application/document_heading_enrichment.py": 330,
        }
        for relative, limit in limits.items():
            lines = (PACKAGE / relative).read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(
                len(lines),
                limit,
                f"{relative} 已超过 {limit} 行，请先拆出新的明确边界。",
            )

    def test_runtime_root_has_at_most_twenty_internal_dependencies(self) -> None:
        tree = ast.parse((PACKAGE / "web_runtime.py").read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        # C2 is staged by domain; each extraction tightens this cap toward 20.
        self.assertLessEqual(len(modules), 12)

    def test_application_runtime_fields_have_concrete_types(self) -> None:
        tree = ast.parse((PACKAGE / "web_runtime.py").read_text(encoding="utf-8"))
        runtime = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ApplicationRuntime"
        )
        untyped = [
            node.target.id for node in runtime.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.annotation, ast.Name)
            and node.annotation.id == "object"
        ]
        self.assertEqual(untyped, [])

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
                "assemble_source_routes",
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
        # 传输层只认路由表与 Range 解析;搜索等用例已移入 controller(v0.5.7 B2c)。
        self.assertEqual(internal_modules, {"http_range", "http_route_table"})

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
                # 连 persistence 子模块(如 persistence.connection)也不许直接
                # import:SQL 适配器一律由组合根(web_runtime)注入。
                if isinstance(node, ast.ImportFrom) and (
                    (node.module or "").endswith("persistence")
                    or (node.module or "").split(".")[0] == "persistence"
                ):
                    violations.append(path.name)
        self.assertEqual(violations, [])

    def test_no_new_import_cycles_appear(self) -> None:
        """包内循环依赖只能减少，不能新增。

        0.5.0 曾因 general_model 与 vision_api 互相 import（一侧用函数内懒
        import 掩盖）而悄悄新增一条环；协议层 openai_compatible 拆出后消除。
        这里冻结剩余的已知环，任何新环都会让门禁失败。
        """

        # 先建模块名索引。`from . import native as pdf_native` 的真实目标是子
        # 模块,不是父包的 __init__;只看 node.module 会把这条边记成父包,顶层
        # 模块(prefix 为空)更是整条丢掉——环就能从这个盲区里长出来。
        # 根 __init__.py 只有 __version__、不 import 任何模块,故不建节点。
        modules: dict[Path, str] = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            name = ".".join(parts)
            if name:
                modules[path] = name
        known_modules = set(modules.values())

        edges: dict[str, set[str]] = {}
        for path, module in sorted(modules.items()):
            targets = edges.setdefault(module, set())
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    # 绝对写法 `import me_finder.x`:包内当前为零,守住未来。
                    for alias in node.names:
                        absolute = _package_module(alias.name)
                        if absolute in known_modules:
                            targets.add(absolute)
                    continue
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level:
                    base = module.split(".")
                    if node.level > len(base):
                        continue
                    prefix = ".".join(base[: -node.level])
                    head = (
                        f"{prefix}.{node.module}".strip(".")
                        if node.module
                        else prefix
                    )
                else:
                    head = _package_module(node.module or "")
                    if head is None:
                        continue
                if head in known_modules:
                    targets.add(head)
                # `from <head> import name` 的 name 本身可能就是子模块。
                for alias in node.names:
                    candidate = f"{head}.{alias.name}".strip(".")
                    if candidate in known_modules:
                        targets.add(candidate)

        # 盲区回归钉子:这两条边只有解析了 import 的 names 才看得到。
        self.assertIn("pdf.native", edges["pdf.extractors"])
        self.assertIn("alignment_assembly", edges["web_runtime"])

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
            (PACKAGE / "parsers/openai_compatible.py").read_text(encoding="utf-8")
        )
        internal = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(internal, set())

    def test_import_queue_has_one_private_owner(self) -> None:
        scripts = PACKAGE / "static/js"
        owner = scripts / "78-import-jobs.js"
        for path in scripts.glob("*.js"):
            if path != owner:
                self.assertNotIn("importStore", path.read_text(encoding="utf-8"), path.name)
        source = owner.read_text(encoding="utf-8")
        for dependency in ("document.", "libraryStore", "parserStore", "calTransientStatus"):
            self.assertNotIn(dependency, source)
        self.assertLessEqual(len(source.splitlines()), 620)
        self.assertLessEqual(len((scripts / "80-import.js").read_text(encoding="utf-8").splitlines()), 950)

    def test_frontend_core_state_is_grouped_by_domain(self) -> None:
        state = (PACKAGE / "static" / "js" / "00-state.js").read_text(
            encoding="utf-8"
        )
        for store in (
            "searchStore",
            "libraryStore",
            "parserStore",
            "settingsStore",
        ):
            self.assertIn(f"const {store} =", state)


if __name__ == "__main__":
    unittest.main()
