from __future__ import annotations

import unittest
from pathlib import Path


class WindowsPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
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


if __name__ == "__main__":
    unittest.main()
