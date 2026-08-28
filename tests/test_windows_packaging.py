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
        cls.spec = Path("packaging/desktop.spec").read_text(encoding="utf-8-sig")

    def test_project_declares_agpl_3_only(self) -> None:
        license_text = Path("LICENSE").read_text(encoding="utf-8")
        notices = Path("THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")

        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3, 19 November 2007", license_text)
        self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", notices)
        self.assertIn("does not use the Artifex commercial license", notices)

    def test_readme_does_not_claim_unapproved_signpath_signing(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertNotIn("Free code signing provided by SignPath.io", readme)

    def test_windows_release_payloads_include_license_files(self) -> None:
        for filename in ("LICENSE", "THIRD_PARTY_NOTICES.txt"):
            self.assertIn(f'-LiteralPath "{filename}"', self.build_script)
            self.assertIn(f'-LiteralPath "{filename}"', self.portable_script)
            self.assertIn(f'Join-Path $DistPath "{filename}"', self.build_script)
            self.assertIn(f'Join-Path $StagePath "{filename}"', self.portable_script)

        self.assertIn(r'Source: "..\dist\MEFinder\*"', self.inno_script)
        self.assertIn('-LiteralPath "THIRD_PARTY_LICENSES"', self.build_script)
        self.assertIn('-LiteralPath "THIRD_PARTY_LICENSES"', self.portable_script)
        self.assertIn("Required license material is missing", self.build_script)
        self.assertIn("Required license material is missing", self.portable_script)
        self.assertIn("Python-runtime-LICENSE.txt", self.build_script)
        self.assertIn("Python-runtime-LICENSE.txt", self.portable_script)
        self.assertIn("Portable ZIP does not contain the required license materials", self.portable_script)

    def test_third_party_notice_has_complete_license_companions(self) -> None:
        license_dir = Path("THIRD_PARTY_LICENSES")
        for filename in (
            "PyMuPDF-1.26.5-COPYING.txt",
            "PyInstaller-6.21.0-COPYING.txt",
            "Python-3.12.13-LICENSE.txt",
            "pywebview-6.2.1-LICENSE.txt",
            "Apache-2.0.txt",
            "BSD-3-Clause.txt",
            "MIT.txt",
            "setuptools-vendored__autocommand-2.2.2.dist-info__LICENSE",
            "MCP-2.0.0-runtime-NOTICES.txt",
        ):
            path = license_dir / filename
            self.assertTrue(path.is_file(), f"missing third-party license: {path}")
            self.assertGreater(path.stat().st_size, 50)

    def test_installer_and_portable_versions_come_from_package(self) -> None:
        self.assertIn("from src.me_finder import __version__", self.build_script)
        self.assertIn("from src.me_finder import __version__", self.portable_script)
        self.assertNotIn('[string]$Version = "0.1.', self.build_script)
        self.assertNotIn('[string]$Version = "0.1.', self.portable_script)
        self.assertIn('[string]$PythonExe = ""', self.portable_script)
        self.assertIn('[string]$PackagerPythonExe = ""', self.portable_script)
        self.assertIn(
            "& $packagerPythonCommand @packagerPythonArgs -m PyInstaller",
            self.portable_script,
        )

    def test_portable_build_includes_windows_unblock_fallback(self) -> None:
        launcher = Path("packaging/portable_first_run.cmd").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("portable_first_run.cmd", self.portable_script)
        self.assertIn("0-首次启动-程序打不开时运行.cmd", self.portable_script)
        self.assertIn("Unblock-File", launcher)
        self.assertIn("_internal", launcher)
        self.assertIn("Start-Process", launcher)

    def test_portable_build_includes_plain_text_user_guide(self) -> None:
        guide = Path("packaging/PORTABLE_README.txt")

        self.assertTrue(guide.is_file())
        self.assertIn('"packaging\\PORTABLE_README.txt"', self.portable_script)
        self.assertIn('"README.txt"', self.portable_script)
        self.assertNotIn('"README.md"', self.portable_script)
        content = guide.read_text(encoding="utf-8-sig")
        self.assertIn("Get-FileHash -Algorithm SHA256", content)
        self.assertIn("保护历史记录", content)

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
        self.assertIn("local_ocr_manifest.json", self.spec)

    def test_windows_builds_run_the_whole_test_suite(self) -> None:
        # 发布门禁通过 `unittest discover` 运行整个 tests/，而不是手工维护模块名单
        # ——手工名单会静默漏掉新增测试。这里只需断言每份脚本都跑 discover。
        for script in (
            self.dev_build_script,
            self.build_script,
            self.portable_script,
        ):
            self.assertIn("unittest discover -t . -s tests", script)

    def test_windows_builds_keep_loopback_local_and_verify_fts5(self) -> None:
        for script in (
            self.dev_build_script,
            self.build_script,
            self.portable_script,
        ):
            self.assertIn("paragraphs_fts", script)
            self.assertIn("NO_PROXY", script)

    def test_release_builds_restore_the_local_test_data_pointer(self) -> None:
        for script in (self.build_script, self.portable_script):
            self.assertIn('"dist\\MEFinderData"', script)
            self.assertIn("function Restore-LocalDevelopmentDataMarker", script)
            self.assertIn("Restore-LocalDevelopmentDataMarker", script.split("finally {", 1)[1])


if __name__ == "__main__":
    unittest.main()
