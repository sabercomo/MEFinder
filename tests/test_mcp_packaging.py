from __future__ import annotations

import unittest
from pathlib import Path


class MCPPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sidecar_spec = Path("packaging/mcp_sidecar.spec").read_text(
            encoding="utf-8"
        )
        cls.windows_dev = Path("build_windows_dist.cmd").read_text(
            encoding="utf-8-sig"
        )
        cls.windows_installer = Path("build_windows_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.windows_portable = Path("build_portable_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.macos_build = Path("build_macos.sh").read_text(encoding="utf-8")
        cls.macos_spec = Path("packaging/desktop_macos.spec").read_text(
            encoding="utf-8"
        )
        cls.macos_requirements = Path("requirements-macos.txt").read_text(
            encoding="utf-8"
        )
        cls.sidecar_smoke = Path("tools/smoke_mcp_sidecar.py").read_text(
            encoding="utf-8"
        )
        cls.windows_workflow = Path(
            ".github/workflows/windows-release-smoke.yml"
        ).read_text(encoding="utf-8")
        cls.codex_guide = Path("docs/CODEX_MCP.md").read_text(encoding="utf-8")

    def test_sidecar_is_an_independent_console_onefile_build(self) -> None:
        self.assertIn("mefinder_mcp.py", self.sidecar_spec)
        self.assertIn('name="MEFinderMCP"', self.sidecar_spec)
        self.assertIn("console=True", self.sidecar_spec)
        self.assertIn("v0.5.0-mcp-v1-tools.json", self.sidecar_spec)
        self.assertNotIn("COLLECT(", self.sidecar_spec)

    def test_windows_builds_require_and_smoke_two_executables(self) -> None:
        for script in (
            self.windows_dev,
            self.windows_installer,
            self.windows_portable,
        ):
            self.assertIn(r"packaging\mcp_sidecar.spec", script)
            self.assertIn("MEFinderMCP.exe", script)
            self.assertIn("tools.smoke_mcp_sidecar", script)

        self.assertIn("must contain exactly two executables", self.windows_installer)

    def test_release_gates_run_the_whole_test_suite(self) -> None:
        # 每份发布脚本都用 `unittest discover` 跑整套 tests/，因此不必再逐个断言
        # 具体测试模块出现在脚本里——新增测试会自动纳入门禁。
        for script in (
            self.windows_dev,
            self.windows_installer,
            self.windows_portable,
            self.macos_build,
        ):
            self.assertIn("unittest discover -t . -s tests", script)

    def test_macos_bundle_contains_signed_smoked_sidecar_and_licenses(self) -> None:
        self.assertIn("packaging/mcp_sidecar.spec", self.macos_build)
        self.assertIn("Contents/MacOS/MEFinderMCP", self.macos_build)
        self.assertIn("tools.smoke_mcp_sidecar", self.macos_build)
        self.assertIn('codesign "${MEFINDER_CODESIGN_ARGS[@]}" "$1"', self.macos_build)
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_LICENSES"):
            self.assertIn(name, self.macos_spec)
        self.assertIn("local_ocr_manifest.json", self.macos_spec)
        self.assertIn(
            'cryptography==46.0.3; platform_machine == "x86_64"',
            self.macos_requirements,
        )

    def test_install_paths_and_portable_move_are_documented(self) -> None:
        self.assertIn(
            r"%LOCALAPPDATA%\Programs\MEFinder\MEFinderMCP.exe",
            self.codex_guide,
        )
        self.assertIn(
            "/Applications/MEFinder.app/Contents/MacOS/MEFinderMCP",
            self.codex_guide,
        )
        self.assertIn("移动绿色版目录", self.codex_guide)
        self.assertIn("command not found", self.codex_guide)

    def test_windows_hosted_release_gate_covers_distribution_lifecycle(self) -> None:
        self.assertIn("windows-2022", self.windows_workflow)
        self.assertIn("build_windows_installer.ps1", self.windows_workflow)
        self.assertIn("build_portable_release.ps1", self.windows_workflow)
        self.assertIn(
            "jrsoftware/issrc/is-6_7_1/Files/Languages/Unofficial/ChineseSimplified.isl",
            self.windows_workflow,
        )
        self.assertIn(
            "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278",
            self.windows_workflow,
        )
        self.assertIn(
            'Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\\ISCC.exe"',
            self.windows_workflow,
        )
        self.assertIn("gh release download v0.4.9", self.windows_workflow)
        self.assertIn("MEFinder-v0.5.0-windows-setup.exe", self.windows_workflow)
        self.assertIn("MEFinder-v0.5.0-windows-portable.zip", self.windows_workflow)
        self.assertIn("upgrade-sentinel.txt", self.windows_workflow)
        self.assertIn("unins000.exe", self.windows_workflow)
        self.assertIn("portable-moved", self.windows_workflow)
        self.assertIn('nargs="?"', self.sidecar_smoke)


if __name__ == "__main__":
    unittest.main()
