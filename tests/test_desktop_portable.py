from __future__ import annotations

import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import desktop
from src.me_finder.search import SearchEngine
from tools.create_empty_index import create_empty_index
from tools.create_portable_zip import create_portable_zip


class DesktopPortableTests(unittest.TestCase):
    def test_windows_main_window_uses_html_titlebar_and_scoped_drag_region(self) -> None:
        class Event:
            def __init__(self) -> None:
                self.callbacks = []

            def __iadd__(self, callback):
                self.callbacks.append(callback)
                return self

        events = types.SimpleNamespace(
            before_show=Event(),
            maximized=Event(),
            restored=Event(),
            loaded=Event(),
            moved=Event(),
            closed=Event(),
        )
        window = types.SimpleNamespace(events=events)
        webview = mock.Mock()
        webview.create_window.return_value = window

        with mock.patch.object(desktop.sys, "platform", "win32"):
            created, controller = desktop.create_main_window(webview, "midnight")

        self.assertIs(created, window)
        options = webview.create_window.call_args.kwargs
        self.assertTrue(options["frameless"])
        self.assertFalse(options["easy_drag"])
        self.assertTrue(options["shadow"])
        self.assertTrue(options["resizable"])
        self.assertTrue(options["text_select"])
        self.assertEqual(options["min_size"], (960, 640))
        self.assertIs(options["js_api"], controller)
        self.assertIs(controller._window, window)
        self.assertFalse(hasattr(controller, "window"))
        self.assertIn('data-desktop-shell="win32"', options["html"])
        self.assertEqual(events.before_show.callbacks, [desktop.configure_windows_main_window])

    def test_macos_main_window_keeps_native_transparent_titlebar(self) -> None:
        class Event:
            def __init__(self) -> None:
                self.callbacks = []

            def __iadd__(self, callback):
                self.callbacks.append(callback)
                return self

        events = types.SimpleNamespace(before_show=Event())
        window = types.SimpleNamespace(events=events)
        webview = mock.Mock()
        webview.create_window.return_value = window

        with mock.patch.object(desktop.sys, "platform", "darwin"):
            created, controller = desktop.create_main_window(webview, "sage-ivory")

        self.assertIs(created, window)
        self.assertIsNone(controller)
        options = webview.create_window.call_args.kwargs
        self.assertNotIn("frameless", options)
        self.assertNotIn("js_api", options)
        self.assertTrue(options["text_select"])
        self.assertEqual(options["min_size"], (960, 640))
        self.assertEqual(events.before_show.callbacks, [desktop.configure_macos_titlebar])

    def test_scan_directory_picker_enables_native_multiple_selection(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")

        self.assertIn("def choose_scan_directories()", desktop_source)
        self.assertIn(
            "choose_folders(Path.home() / \"Documents\", allow_multiple=True)",
            desktop_source,
        )

    def test_backup_picker_uses_native_single_zip_selection(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")

        self.assertIn("def choose_backup_file()", desktop_source)
        self.assertIn("webview.FileDialog.OPEN", desktop_source)
        self.assertIn('file_types=("MEFinder 备份 (*.zip)",)', desktop_source)
        self.assertIn(
            "native_backup_file_chooser=choose_backup_file",
            desktop_source,
        )

    def test_desktop_export_picker_uses_native_folder_selection(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")

        self.assertIn("def choose_export_directory()", desktop_source)
        self.assertIn(
            'choose_folders(Path.home() / "Downloads")',
            desktop_source,
        )
        self.assertIn(
            'if sys.platform in {"darwin", "win32"}',
            desktop_source,
        )

    def test_macos_release_builds_a_verified_drag_install_dmg(self) -> None:
        build_source = Path("build_macos.sh").read_text(encoding="utf-8")
        spec_source = Path("desktop_macos.spec").read_text(encoding="utf-8")

        self.assertIn(
            'minimum_system_version = "12.0" if target_arch == "x86_64" else "14.0"',
            spec_source,
        )
        self.assertIn(
            '"LSMinimumSystemVersion": minimum_system_version',
            spec_source,
        )
        self.assertIn(
            '("THIRD_PARTY_LICENSES", "THIRD_PARTY_LICENSES")',
            spec_source,
        )
        self.assertNotIn(
            'cp -R "THIRD_PARTY_LICENSES" "$MEFINDER_DMG_STAGE/THIRD_PARTY_LICENSES"',
            build_source,
        )
        self.assertIn("MEFINDER_PYTHON_LICENSE", build_source)

        self.assertIn('MEFINDER_DMG="release/${MEFINDER_PACKAGE}.dmg"', build_source)
        self.assertIn(
            'ln -s /Applications "$MEFINDER_DMG_STAGE/Applications"',
            build_source,
        )
        self.assertIn("hdiutil create", build_source)
        self.assertIn("hdiutil verify", build_source)
        self.assertIn(
            'verify_app_signature "$MEFINDER_DMG_MOUNT/MEFinder.app"',
            build_source,
        )
        self.assertIn(
            '"$(readlink "$MEFINDER_DMG_MOUNT/Applications")" != "/Applications"',
            build_source,
        )
        self.assertIn(
            'shasum -a 256 "${MEFINDER_PACKAGE}.dmg" > "${MEFINDER_PACKAGE}.dmg.sha256.txt"',
            build_source,
        )
        self.assertIn(
            'shasum -a 256 -c "${MEFINDER_PACKAGE}.dmg.sha256.txt"',
            build_source,
        )

    def test_macos_release_artifacts_do_not_use_file_provider_dist_copy(self) -> None:
        build_source = Path("build_macos.sh").read_text(encoding="utf-8")

        zip_command = (
            'ditto -c -k --keepParent \\\n'
            '  --norsrc \\\n'
            '  --noextattr \\\n'
            '  --noqtn \\\n'
            '  --noacl \\\n'
            '  "$MEFINDER_BUILT_APP" \\\n'
            '  "$MEFINDER_TEMP_ZIP"'
        )
        dmg_copy = (
            'ditto --norsrc --noextattr --noqtn --noacl \\\n'
            '  "$MEFINDER_BUILT_APP" \\\n'
            '  "$MEFINDER_DMG_STAGE/MEFinder.app"'
        )
        self.assertIn(zip_command, build_source)
        self.assertIn(dmg_copy, build_source)
        self.assertIn("ZIP contains AppleDouble metadata", build_source)
        self.assertNotIn('MEFINDER_APP="dist/MEFinder.app"', build_source)
        self.assertNotIn('"$MEFINDER_BUILT_APP" "$MEFINDER_APP"', build_source)
        self.assertIn(
            "com\\.apple\\.(FinderInfo|ResourceFork):",
            build_source,
        )

    def test_macos_release_preserves_configured_developer_id_signature(self) -> None:
        build_source = Path("build_macos.sh").read_text(encoding="utf-8")

        self.assertIn(
            'MEFINDER_CODESIGN_IDENTITY="${MEFINDER_CODESIGN_IDENTITY:--}"',
            build_source,
        )
        self.assertIn(
            'MEFINDER_CODESIGN_ARGS=(--force --deep --sign "$MEFINDER_CODESIGN_IDENTITY")',
            build_source,
        )
        self.assertIn(
            'MEFINDER_CODESIGN_ARGS+=(--options runtime --timestamp)',
            build_source,
        )
        self.assertIn(
            'codesign "${MEFINDER_CODESIGN_ARGS[@]}" "$MEFINDER_BUILT_APP"',
            build_source,
        )
        self.assertNotIn(
            'codesign --force --deep --sign - "$MEFINDER_BUILT_APP"',
            build_source,
        )

    def test_macos_icon_uses_rounded_system_scale_and_keeps_transparent_corners(self) -> None:
        icon_source = Path("assets/app_icon.svg").read_text(encoding="utf-8")
        build_source = Path("build_macos.sh").read_text(encoding="utf-8")

        self.assertIn('x="25" y="25" width="206" height="206" rx="46"', icon_source)
        self.assertNotIn("qlmanage -t", build_source)
        self.assertIn(
            'sips -s format png -z "$MEFINDER_SIZE" "$MEFINDER_SIZE" "assets/app_icon.svg"',
            build_source,
        )

    def test_macos_titlebar_extends_content_without_hiding_traffic_lights(self) -> None:
        class TitlebarView:
            def __init__(self) -> None:
                self.background = None

            def setBackgroundColor_(self, value: object) -> None:
                self.background = value

        class Subviews:
            def __init__(self, titlebar: TitlebarView) -> None:
                self.titlebar = titlebar

            def lastObject(self) -> TitlebarView:
                return self.titlebar

        class Superview:
            def __init__(self, titlebar: TitlebarView) -> None:
                self.titlebar = titlebar

            def subviews(self) -> Subviews:
                return Subviews(self.titlebar)

        class ContentView:
            def __init__(self, titlebar: TitlebarView) -> None:
                self.titlebar = titlebar

            def superview(self) -> Superview:
                return Superview(self.titlebar)

        class NativeWindow:
            def __init__(self) -> None:
                self.style_mask = 4
                self.movable = False
                self.transparent = False
                self.title_visibility = None
                self.separator_style = None
                self.titlebar = TitlebarView()
                self.standard_button_requests = []

            def styleMask(self) -> int:
                return self.style_mask

            def setStyleMask_(self, value: int) -> None:
                self.style_mask = value

            def setMovable_(self, value: bool) -> None:
                self.movable = value

            def setTitlebarAppearsTransparent_(self, value: bool) -> None:
                self.transparent = value

            def setTitleVisibility_(self, value: int) -> None:
                self.title_visibility = value

            def setTitlebarSeparatorStyle_(self, value: int) -> None:
                self.separator_style = value

            def contentView(self) -> ContentView:
                return ContentView(self.titlebar)

            def standardWindowButton_(self, button: object) -> None:
                self.standard_button_requests.append(button)

        native_window = NativeWindow()
        window = types.SimpleNamespace(native=native_window)
        appkit = types.SimpleNamespace(
            NSWindowStyleMaskFullSizeContentView=1 << 15,
            NSWindowTitleHidden=7,
            NSTitlebarSeparatorStyleNone=9,
            NSColor=types.SimpleNamespace(clearColor=lambda: "clear"),
        )
        with (
            mock.patch.object(desktop.sys, "platform", "darwin"),
            mock.patch.dict(desktop.sys.modules, {"AppKit": appkit}),
        ):
            desktop.configure_macos_titlebar(window)

        self.assertEqual(native_window.style_mask, 4 | (1 << 15))
        self.assertTrue(native_window.movable)
        self.assertTrue(native_window.transparent)
        self.assertEqual(native_window.title_visibility, 7)
        self.assertEqual(native_window.separator_style, 9)
        self.assertEqual(native_window.titlebar.background, "clear")
        self.assertEqual(native_window.standard_button_requests, [])

    def test_macos_native_drag_strip_uses_cocoa_window_dragging(self) -> None:
        class Anchor:
            def __init__(self, name: str) -> None:
                self.name = name

            def constraintEqualToAnchor_(self, other: "Anchor") -> tuple:
                return self.name, other.name, 0.0

            def constraintEqualToAnchor_constant_(
                self,
                other: "Anchor",
                constant: float,
            ) -> tuple:
                return self.name, other.name, constant

            def constraintEqualToConstant_(self, constant: float) -> tuple:
                return self.name, constant

        class FakeNSView:
            @classmethod
            def alloc(cls):
                return cls()

            def __init__(self) -> None:
                self.frame = None
                self.translates_mask = True
                self.children = []

            def initWithFrame_(self, frame: tuple):
                self.frame = frame
                return self

            def setTranslatesAutoresizingMaskIntoConstraints_(self, value: bool) -> None:
                self.translates_mask = value

            def addSubview_(self, child: "FakeNSView") -> None:
                self.children.append(child)

            def bounds(self):
                return types.SimpleNamespace(size=types.SimpleNamespace(width=1000.0))

            def leadingAnchor(self) -> Anchor:
                return Anchor("leading")

            def trailingAnchor(self) -> Anchor:
                return Anchor("trailing")

            def topAnchor(self) -> Anchor:
                return Anchor("top")

            def heightAnchor(self) -> Anchor:
                return Anchor("height")

        class NativeWindow:
            def __init__(self) -> None:
                self.movable = False
                self.content = FakeNSView()

            def setMovable_(self, value: bool) -> None:
                self.movable = value

            def contentView(self) -> FakeNSView:
                return self.content

        activated = []
        appkit = types.SimpleNamespace(
            NSView=FakeNSView,
            NSMakeRect=lambda x, y, width, height: (x, y, width, height),
            NSLayoutConstraint=types.SimpleNamespace(
                activateConstraints_=lambda constraints: activated.extend(constraints)
            ),
        )
        native_window = NativeWindow()
        window = types.SimpleNamespace()

        with mock.patch.object(desktop, "_macos_drag_strip_class", None):
            desktop._install_macos_native_drag_strip(window, native_window, appkit)

        strip = window._mefinder_native_drag_strip
        self.assertTrue(native_window.movable)
        self.assertEqual(native_window.content.children, [strip])
        self.assertFalse(strip.translates_mask)
        self.assertEqual(strip.frame, (82.0, 0.0, 918.0, 28.0))
        self.assertTrue(strip.acceptsFirstMouse_(None))
        self.assertTrue(strip.mouseDownCanMoveWindow())
        self.assertEqual(len(activated), 4)
        self.assertEqual(window._mefinder_native_drag_constraints, activated)

    def test_macos_app_uses_bundle_resources(self) -> None:
        executable = "/Applications/MEFinder.app/Contents/MacOS/MEFinder"
        with (
            mock.patch.object(desktop.sys, "frozen", True, create=True),
            mock.patch.object(desktop.sys, "platform", "darwin"),
            mock.patch.object(desktop.sys, "executable", executable),
        ):
            self.assertEqual(
                desktop.app_root(),
                Path("/Applications/MEFinder.app/Contents/Resources").resolve(),
            )

    def test_macos_app_data_uses_application_support(self) -> None:
        with (
            mock.patch.dict(desktop.os.environ, {}, clear=True),
            mock.patch.object(desktop.sys, "platform", "darwin"),
        ):
            self.assertEqual(
                desktop.local_app_data_root(Path("/Users/example")),
                Path("/Users/example/Library/Application Support/MEFinder"),
            )

    def test_macos_app_data_uses_saved_custom_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            default = home / "Library" / "Application Support" / "MEFinder"
            custom = Path(directory) / "OneDrive" / "MEFinder"
            default.mkdir(parents=True)
            (default / "data_root.txt").write_text(
                str(custom) + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch.dict(desktop.os.environ, {}, clear=True),
                mock.patch.object(desktop.sys, "platform", "darwin"),
            ):
                self.assertEqual(desktop.local_app_data_root(home), custom.resolve())

    def test_app_data_override_supports_isolated_smoke_tests(self) -> None:
        configured = Path("/private/tmp/mefinder-smoke-data")
        with mock.patch.dict(
            desktop.os.environ,
            {"ME_FINDER_APP_DATA_ROOT": str(configured)},
            clear=True,
        ):
            self.assertEqual(desktop.local_app_data_root(), configured.resolve())

    def test_windows_installer_marker_relocates_the_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            chosen = Path(directory) / "D-drive-stand-in" / "MEFinderData"
            (bundle / "data_root.txt").write_text(str(chosen), encoding="utf-8")
            with (
                mock.patch.dict(desktop.os.environ, {}, clear=True),
                mock.patch.object(desktop.sys, "platform", "win32"),
                mock.patch.object(desktop.sys, "frozen", True, create=True),
                mock.patch.object(desktop, "app_root", return_value=bundle),
            ):
                self.assertEqual(desktop.local_app_data_root(), chosen.resolve())

    def test_windows_stable_pointer_takes_priority_after_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bundle = base / "app"
            bundle.mkdir()
            installed = base / "installer-choice"
            custom = base / "external" / "MEFinder"
            local_app_data = base / "LocalAppData"
            default = local_app_data / "MEFinder"
            default.mkdir(parents=True)
            (bundle / "data_root.txt").write_text(
                str(installed), encoding="utf-8"
            )
            (default / "data_root.txt").write_text(
                str(custom), encoding="utf-8"
            )
            with (
                mock.patch.dict(
                    desktop.os.environ,
                    {"LOCALAPPDATA": str(local_app_data)},
                    clear=True,
                ),
                mock.patch.object(desktop.sys, "platform", "win32"),
                mock.patch.object(desktop.sys, "frozen", True, create=True),
                mock.patch.object(desktop, "app_root", return_value=bundle),
            ):
                self.assertEqual(
                    desktop.local_app_data_root(),
                    custom.resolve(),
                )

    def test_windows_installer_marker_is_ignored_for_portable_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / desktop.PORTABLE_MARKER).touch()
            (bundle / "data_root.txt").write_text("Z:\\should-not-be-used", encoding="utf-8")
            with (
                mock.patch.object(desktop.sys, "platform", "win32"),
                mock.patch.object(desktop.sys, "frozen", True, create=True),
            ):
                self.assertIsNone(desktop.installed_data_root_override(bundle))

    def test_windows_installer_marker_absent_falls_back_to_localappdata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            with (
                mock.patch.object(desktop.sys, "platform", "win32"),
                mock.patch.object(desktop.sys, "frozen", True, create=True),
            ):
                self.assertIsNone(desktop.installed_data_root_override(bundle))

    def test_frozen_macos_resources_seed_writable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            resources = base / "MEFinder.app" / "Contents" / "Resources"
            runtime_parent = base / "Application Support" / "MEFinder"
            (resources / "data").mkdir(parents=True)
            (resources / "config").mkdir()
            (resources / "data" / "index.sqlite3").write_bytes(b"blank-index")
            (resources / "config" / "pdf_imports.json").write_text(
                '{"documents": []}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(desktop.sys, "frozen", True, create=True),
                mock.patch.object(desktop, "local_app_data_root", return_value=runtime_parent),
            ):
                runtime = desktop.prepare_runtime_root(resources)
            self.assertEqual(runtime, runtime_parent / "runtime")
            self.assertEqual(
                (runtime / "data" / "index.sqlite3").read_bytes(),
                b"blank-index",
            )
            self.assertTrue((runtime / "config" / "pdf_imports.json").is_file())

    def test_optional_vision_credentials_use_local_private_config(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")
        release_source = Path("build_portable_release.ps1").read_text(encoding="utf-8")
        self.assertIn("ME_FINDER_VISION_CONFIG", desktop_source)
        self.assertIn("vision_api.local.json", desktop_source)
        self.assertIn('"vision_api.local.json"', release_source)

    def test_desktop_uses_bundled_ca_certificates(self) -> None:
        desktop_source = Path("desktop.py").read_text(encoding="utf-8")
        build_source = Path("build_macos.sh").read_text(encoding="utf-8")
        self.assertIn("import certifi", desktop_source)
        self.assertIn(
            'os.environ.setdefault("SSL_CERT_FILE", certifi.where())',
            desktop_source,
        )
        self.assertIn("*/certifi/cacert.pem", build_source)

    def test_portable_marker_keeps_frozen_runtime_beside_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / desktop.PORTABLE_MARKER).touch()
            with mock.patch.object(desktop.sys, "frozen", True, create=True):
                self.assertTrue(desktop.is_portable_bundle(bundle))
                self.assertEqual(desktop.installation_kind(bundle), "portable")
                self.assertEqual(desktop.prepare_runtime_root(bundle), bundle)
                self.assertEqual(desktop.webview_storage_path(bundle, True), str(bundle / "webview-data"))

    def test_installed_webview_storage_follows_selected_data_root(self) -> None:
        selected_root = Path("G:/MEFinderData/runtime")
        self.assertEqual(
            desktop.webview_storage_path(selected_root, False),
            str(selected_root / "webview-data"),
        )

    def test_only_installer_marker_enables_self_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            with mock.patch.object(desktop.sys, "frozen", True, create=True):
                self.assertEqual(desktop.installation_kind(bundle), "standalone")
                (bundle / desktop.INSTALLED_MARKER).touch()
                self.assertEqual(desktop.installation_kind(bundle), "installed")
            with mock.patch.object(desktop.sys, "frozen", False, create=True):
                self.assertEqual(desktop.installation_kind(bundle), "source")

    def test_public_blank_index_opens_as_an_empty_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            create_empty_index(database)
            engine = SearchEngine(database)
            try:
                self.assertEqual(engine.index["metadata"]["source_count"], 0)
                self.assertEqual(engine.index["source_files"], [])
                self.assertEqual(engine.search("任意文本")["total"], 0)
            finally:
                engine.close()

    def test_portable_zip_uses_standard_paths_and_one_top_level_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "MEFinder-test-windows-portable"
            (source / "config").mkdir(parents=True)
            (source / "portable.flag").touch()
            (source / "config" / "pdf_imports.json").write_text('{"documents": []}', encoding="utf-8")
            target = root / "release.zip"
            create_portable_zip(source, target)

            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertTrue(all("\\" not in name for name in names))
            self.assertTrue(all(name.startswith(source.name + "/") for name in names))


if __name__ == "__main__":
    unittest.main()
