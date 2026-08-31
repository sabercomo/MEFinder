from __future__ import annotations

import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 remains supported for source mode.
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = PROJECT_ROOT / "docs" / "CODEX_MCP.md"
CLIENT_GUIDE_PATH = PROJECT_ROOT / "docs" / "MCP_CLIENT_SETUP.md"
EXAMPLE_PATH = PROJECT_ROOT / "docs" / "examples" / "mefinder-codex-source.toml"
WINDOWS_EXAMPLE_PATH = (
    PROJECT_ROOT / "docs" / "examples" / "mefinder-codex-windows-installed.toml"
)
MACOS_EXAMPLE_PATH = (
    PROJECT_ROOT / "docs" / "examples" / "mefinder-codex-macos-installed.toml"
)
E2E_REPORT_PATH = PROJECT_ROOT / "docs" / "mcp-v1-codex-e2e-report.md"
RELEASE_REPORT_PATH = PROJECT_ROOT / "docs" / "mcp-v1-concurrency-release-report.md"
V2_DECISION_PATH = PROJECT_ROOT / "docs" / "mcp-v2-decision.md"


class MCPDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guide = GUIDE_PATH.read_text(encoding="utf-8")
        cls.client_guide = CLIENT_GUIDE_PATH.read_text(encoding="utf-8")
        cls.example = EXAMPLE_PATH.read_text(encoding="utf-8")
        cls.windows_example = WINDOWS_EXAMPLE_PATH.read_text(encoding="utf-8")
        cls.macos_example = MACOS_EXAMPLE_PATH.read_text(encoding="utf-8")
        cls.e2e_report = E2E_REPORT_PATH.read_text(encoding="utf-8")
        cls.release_report = RELEASE_REPORT_PATH.read_text(encoding="utf-8")
        cls.v2_decision = V2_DECISION_PATH.read_text(encoding="utf-8")
        cls.readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_links_to_optional_packaged_read_only_integration(self) -> None:
        self.assertIn("[Codex MCP 配置、健康检查与隐私说明](docs/CODEX_MCP.md)", self.readme)
        self.assertIn("0.5.1，可选集成", self.readme)
        self.assertIn("Windows 安装版、绿色版和 macOS 发布包", self.readme)

    def test_guide_covers_all_supported_configuration_paths(self) -> None:
        self.assertIn(
            "https://learn.chatgpt.com/docs/extend/mcp?surface=cli",
            self.guide,
        )
        for expected in (
            "codex mcp add mefinder",
            "codex mcp get mefinder --json",
            "codex mcp list",
            "/mcp",
            "[mcp_servers.mefinder]",
            "ChatGPT/Codex 桌面端",
            "IDE 扩展",
        ):
            self.assertIn(expected, self.guide)

    def test_client_guide_gives_concrete_windows_steps_and_macos_appendix(self) -> None:
        for expected in (
            "# MEFinder MCP 配置教程",
            "## 二、Codex 配置",
            "## 三、Claude Code 配置",
            "## 四、WorkBuddy 配置",
            "## 七、macOS 配置",
            "设置 → 插件 → MCP → 添加 → 添加 MCP 服务器",
            '"type": "stdio"',
            '"command": "D:\\\\MEFinder\\\\MEFinderMCP.exe"',
            "Windows 普通路径中的反斜杠",
            "必须写成两个",
            "D:\\\\MEFinder\\\\MEFinderMCP.exe",
            "Claude Code 用一条命令完成配置",
            "`Win + R`",
            "输入 `powershell`",
            "`Command（⌘）+ 空格`",
            "输入“终端”或 `Terminal`",
            "codex mcp add mefinder",
            "codex mcp remove mefinder",
            "claude mcp add --transport stdio --scope user mefinder",
            "claude mcp remove --scope user mefinder",
            'Resolve-Path ".\\MEFinderMCP.exe"',
            "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
        ):
            self.assertIn(expected, self.client_guide)

    def test_readme_states_document_package_integrity_boundaries(self) -> None:
        self.assertIn("已入库 PDF", self.readme)
        self.assertIn("当前版本不导出 Word", self.readme)
        self.assertIn("未做数字签名", self.readme)
        self.assertNotIn("被改动过的文档包在入库前就会被拒绝", self.readme)

    def test_guide_freezes_source_runtime_and_health_check_boundaries(self) -> None:
        for expected in (
            "Python 3.10 或更高版本",
            'mcp==2.0.0',
            "src.me_finder.mcp_server",
            "--runtime-root",
            "data/index.sqlite3",
            "list_documents",
            "locate_quote",
            "read_document_window",
            "软件开启或关闭",
            "index_not_found",
            "index_unavailable",
        ):
            self.assertIn(expected, self.guide)

    def test_guide_freezes_packaged_paths_and_portable_move_boundary(self) -> None:
        for expected in (
            r"%LOCALAPPDATA%\Programs\MEFinder\MEFinderMCP.exe",
            "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
            "移动绿色版目录后",
            "command not found",
            "覆盖升级",
        ):
            self.assertIn(expected, self.guide)

    def test_guide_states_local_server_and_model_context_privacy_separately(self) -> None:
        self.assertIn("MCP Server 本身不访问网络", self.guide)
        self.assertIn("会进入 Codex 对话及模型上下文", self.guide)
        self.assertIn("九个 v1 工具均为只读", self.guide)
        self.assertIn("多个附近候选、定位和前后文", self.guide)
        self.assertIn("ambiguous", self.guide)
        self.assertIn("不需要 OpenAI API Key", self.guide)
        self.assertIn("MEFinder 不读取、创建或删除用户的 Codex 配置", self.guide)

    def test_guide_documents_reversible_removal(self) -> None:
        self.assertIn("codex mcp remove mefinder", self.guide)
        self.assertIn("不删除 MEFinder 文献、索引、设置或桌面功能", self.guide)

    def test_e2e_report_records_real_codex_boundaries_without_guessing_model(self) -> None:
        for expected in (
            "codex-cli 0.147.0-alpha.6.5",
            "没有报告明确模型标识",
            "MEFinder Web 关闭时",
            "MEFinder Web 开启时",
            "--ignore-user-config --ephemeral",
            "没有残留 MCP/Web 进程或 SQLite 文件句柄",
            "mcp-v1-concurrency-release-report.md",
        ):
            self.assertIn(expected, self.e2e_report)

    def test_release_and_v2_reports_expose_platform_limits_and_write_gate(self) -> None:
        for expected in (
            "Windows 10/11 x64 实机复验",
            "Developer ID/hardened runtime",
            "tests/test_mcp_concurrency.py",
            "无残留",
        ):
            self.assertIn(expected, self.release_report)
        for expected in (
            "0.4.4 保持 MCP v1 只读",
            "import_document",
            "save_bibliographic_metadata",
            'default_tools_approval_mode = "writes"',
            "跨进程",
        ):
            self.assertIn(expected, self.v2_decision)

    def test_example_uses_placeholders_instead_of_developer_paths(self) -> None:
        self.assertIn("/ABSOLUTE/PATH/TO/MEFINDER", self.example)
        self.assertNotIn("/Users/mercury", self.example)
        self.assertNotIn("文献原句定位器", self.example)

    @unittest.skipIf(tomllib is None, "tomllib is unavailable on Python 3.10")
    def test_config_example_is_valid_toml_with_expected_stdio_command(self) -> None:
        config = tomllib.loads(self.example)
        server = config["mcp_servers"]["mefinder"]
        self.assertTrue(server["command"].startswith("/"))
        self.assertEqual(
            server["args"][:2],
            ["-m", "src.me_finder.mcp_server"],
        )
        self.assertEqual(server["args"][2], "--runtime-root")
        self.assertTrue(server["cwd"].startswith("/"))
        self.assertEqual(server["startup_timeout_sec"], 10)
        self.assertEqual(server["tool_timeout_sec"], 60)

        windows = tomllib.loads(self.windows_example)["mcp_servers"]["mefinder"]
        macos = tomllib.loads(self.macos_example)["mcp_servers"]["mefinder"]
        self.assertTrue(windows["command"].endswith(r"MEFinder\MEFinderMCP.exe"))
        self.assertEqual(
            macos["command"],
            "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
        )


if __name__ == "__main__":
    unittest.main()
