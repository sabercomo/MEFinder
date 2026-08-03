from __future__ import annotations

import unittest
from pathlib import Path


class WindowsPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dev_build_script = Path("build_windows_dist.cmd").read_text(
            encoding="utf-8-sig"
        )
        cls.build_script = Path("build_windows_installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.portable_script = Path("build_portable_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.inno_script = Path("installer/MEFinder.iss").read_text(
            encoding="utf-8-sig"
        )
        cls.spec = Path("desktop.spec").read_text(encoding="utf-8-sig")

    def test_installer_and_portable_versions_come_from_package(self) -> None:
        self.assertIn("from src.me_finder import __version__", self.build_script)
        self.assertIn("from src.me_finder import __version__", self.portable_script)
        self.assertNotIn('[string]$Version = "0.1.', self.build_script)
        self.assertNotIn('[string]$Version = "0.1.', self.portable_script)
        self.assertIn('[string]$PythonExe = ""', self.portable_script)
        self.assertIn("& $pythonCommand @pythonLauncherArgs -m PyInstaller", self.portable_script)

    def test_installer_wizard_is_localized_and_branded(self) -> None:
        # 简体中文与英文双语可选；启动时的语言对话框负责切换。
        self.assertIn(
            r'MessagesFile: "compiler:Languages\ChineseSimplified.isl"',
            self.inno_script,
        )
        self.assertIn('MessagesFile: "compiler:Default.isl"', self.inno_script)
        self.assertIn('Name: "english"', self.inno_script)
        self.assertIn('Name: "chinesesimplified"', self.inno_script)
        # 自定义串（任务/运行项/数据目录页）两种语言都有，避免中英混排。
        self.assertIn("[CustomMessages]", self.inno_script)
        for key in ("DesktopIcon", "LaunchApp", "DataDirTitle", "DataDirBody"):
            self.assertIn(f"chinesesimplified.{key}=", self.inno_script)
            self.assertIn(f"english.{key}=", self.inno_script)
        self.assertIn("{cm:DesktopIcon}", self.inno_script)
        self.assertIn("{cm:LaunchApp,{#AppName}}", self.inno_script)
        self.assertIn("{cm:DataDirTitle}", self.inno_script)
        # 品牌向导图（欢迎/完成页左栏大图与页眉小图）替换 Inno 默认占位图。
        self.assertIn("WizardImageFile=wizard-large.bmp", self.inno_script)
        self.assertIn("WizardSmallImageFile=wizard-small.bmp", self.inno_script)
        self.assertIn("WizardStyle=modern", self.inno_script)
        for image in ("wizard-large.bmp", "wizard-small.bmp"):
            self.assertTrue(
                (Path("installer") / image).is_file(),
                f"缺少向导图 installer/{image}",
            )

    def test_installer_program_files_are_separate_from_user_data(self) -> None:
        self.assertIn(r"DefaultDirName={localappdata}\Programs\MEFinder", self.inno_script)
        self.assertNotIn(r"DefaultDirName={localappdata}\MEFinder", self.inno_script)
        self.assertIn("PrivilegesRequired=lowest", self.inno_script)
        self.assertIn("ArchitecturesAllowed=x64compatible", self.inno_script)
        self.assertIn('Source: "installed.flag"', self.inno_script)

    def test_silent_update_has_one_deterministic_relaunch_path(self) -> None:
        self.assertIn("CloseApplications=yes", self.inno_script)
        self.assertIn("RestartApplications=no", self.inno_script)
        self.assertIn("[Run]", self.inno_script)
        self.assertIn(
            "Flags: nowait skipifdoesntexist runascurrentuser",
            self.inno_script,
        )
        self.assertNotIn("/RESTARTAPPLICATIONS", self.build_script)

    def test_release_build_checks_private_data_and_writes_checksum(self) -> None:
        for forbidden in (
            "mineru_api.local.json",
            "vision_api.local.json",
            "preferences.json",
            "desktop.log",
            "portable.flag",
        ):
            self.assertIn(forbidden, self.build_script)
        self.assertIn("tools.create_empty_index", self.build_script)
        self.assertIn("Get-FileHash -Algorithm SHA256", self.build_script)
        self.assertIn('$HashPath = "$InstallerPath.sha256.txt"', self.build_script)

    def test_spec_packages_windows_integrations_and_version_resource(self) -> None:
        self.assertIn("src.me_finder.windows_desktop", self.spec)
        self.assertIn("src.me_finder.update_service", self.spec)
        self.assertIn("write_windows_version_info", self.spec)
        self.assertIn("version=str(version_info_path)", self.spec)

    def test_windows_builds_gate_anchor_compatibility(self) -> None:
        for script in (
            self.dev_build_script,
            self.build_script,
            self.portable_script,
        ):
            self.assertIn("tests.test_anchor_metadata", script)
            self.assertIn("tests.test_pdf_match_anchors", script)

    def test_windows_builds_gate_preferences_theme_and_desktop_contracts(self) -> None:
        for script in (
            self.dev_build_script,
            self.build_script,
            self.portable_script,
        ):
            self.assertIn("tests.test_preferences_concurrency", script)
            self.assertIn("tests.test_theme_system", script)
            self.assertIn("tests.test_desktop_portable", script)

    def test_windows_builds_gate_fts5_and_keep_loopback_local(self) -> None:
        for script in (
            self.dev_build_script,
            self.build_script,
            self.portable_script,
        ):
            self.assertIn("tests.test_fts_search_scalability", script)
            self.assertIn("paragraphs_fts", script)
            self.assertIn("NO_PROXY", script)

    def test_release_builds_restore_the_local_test_data_pointer(self) -> None:
        for script in (self.build_script, self.portable_script):
            self.assertIn('"dist\\MEFinderData"', script)
            self.assertIn("function Restore-LocalDevelopmentDataMarker", script)
            self.assertIn("Restore-LocalDevelopmentDataMarker", script.split("finally {", 1)[1])


if __name__ == "__main__":
    unittest.main()
