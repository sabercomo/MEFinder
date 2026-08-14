#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script must run on macOS." >&2
  exit 1
fi

MEFINDER_PYTHON="${MEFINDER_PYTHON:-python3}"
MEFINDER_ARCH="${MEFINDER_TARGET_ARCH:-$(uname -m)}"
MEFINDER_CODESIGN_IDENTITY="${MEFINDER_CODESIGN_IDENTITY:--}"
MEFINDER_STAGE="build/macos-stage"
MEFINDER_VERSION="$("$MEFINDER_PYTHON" -c 'from src.me_finder import __version__; print(__version__)')"
MEFINDER_PACKAGE="MEFinder-v${MEFINDER_VERSION}-macos-${MEFINDER_ARCH}"
MEFINDER_TEMP_ROOT="$(mktemp -d "${TMPDIR:-/private/tmp}/mefinder-build.XXXXXX")"
MEFINDER_TEMP_DIST="$MEFINDER_TEMP_ROOT/dist"
MEFINDER_BUILT_APP="$MEFINDER_TEMP_DIST/MEFinder.app"
MEFINDER_SIDECAR_DIST="$MEFINDER_TEMP_ROOT/mcp-dist"
MEFINDER_SIDECAR_WORK="$MEFINDER_TEMP_ROOT/mcp-work"
MEFINDER_BUILT_SIDECAR="$MEFINDER_SIDECAR_DIST/MEFinderMCP"
MEFINDER_ZIP="release/${MEFINDER_PACKAGE}.zip"
MEFINDER_DMG="release/${MEFINDER_PACKAGE}.dmg"
MEFINDER_TEMP_ZIP="$MEFINDER_TEMP_ROOT/${MEFINDER_PACKAGE}.zip"
MEFINDER_TEMP_DMG="$MEFINDER_TEMP_ROOT/${MEFINDER_PACKAGE}.dmg"
MEFINDER_DMG_STAGE="$MEFINDER_TEMP_ROOT/dmg-stage"
MEFINDER_DMG_MOUNT="$MEFINDER_TEMP_ROOT/dmg-mount"
MEFINDER_DMG_VERIFY_COPY="$MEFINDER_TEMP_ROOT/dmg-verify-copy/MEFinder.app"
MEFINDER_DMG_ATTACHED=0
MEFINDER_CODESIGN_ARGS=(--force --deep --sign "$MEFINDER_CODESIGN_IDENTITY")
if [[ "$MEFINDER_CODESIGN_IDENTITY" != "-" ]]; then
  MEFINDER_CODESIGN_ARGS+=(--options runtime --timestamp)
fi

# macOS network proxy settings are inherited by urllib during tests.  Local
# HTTP regression servers must never be routed through a user's proxy.
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"
export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost"

cleanup() {
  if [[ "$MEFINDER_DMG_ATTACHED" == "1" ]]; then
    hdiutil detach "$MEFINDER_DMG_MOUNT" >/dev/null 2>&1 || true
  fi
  rm -rf "$MEFINDER_TEMP_ROOT"
}
trap cleanup EXIT

clean_app_metadata() {
  local app_path="$1"
  local app_attributes
  xattr -crs "$app_path"
  app_attributes="$(xattr -lrs "$app_path" 2>/dev/null || true)"
  if LC_ALL=C grep -Eq \
    'com\.apple\.(FinderInfo|ResourceFork):' \
    <<<"$app_attributes"; then
    echo "Build failed: disallowed Finder metadata remains in $app_path." >&2
    return 1
  fi
  if find "$app_path" -name '._*' -print -quit | LC_ALL=C grep -q .; then
    echo "Build failed: AppleDouble metadata remains in $app_path." >&2
    return 1
  fi
}

verify_app_signature() {
  codesign --verify --deep --strict "$1"
}

sign_sidecar() {
  codesign "${MEFINDER_CODESIGN_ARGS[@]}" "$1"
  codesign --verify --strict "$1"
}

smoke_sidecar() {
  "$MEFINDER_PYTHON" -m tools.smoke_mcp_sidecar "$1" "$MEFINDER_STAGE"
}

"$MEFINDER_PYTHON" -c \
  "import PyInstaller, mcp, webview; from Quartz.PDFKit import PDFDocument, PDFView" \
  >/dev/null

rm -rf "$MEFINDER_STAGE"
mkdir -p "$MEFINDER_STAGE/data" "$MEFINDER_STAGE/config"

"$MEFINDER_PYTHON" -m tools.create_empty_index "$MEFINDER_STAGE/data/index.sqlite3"
"$MEFINDER_PYTHON" - "$MEFINDER_STAGE/data/index.sqlite3" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'paragraphs_fts'"
    ).fetchone()
    if table is None:
        raise SystemExit(
            "Build failed: this Python/SQLite runtime has no FTS5 trigram support."
        )
finally:
    connection.close()
PY
cp "config/pdf_imports.empty.json" "$MEFINDER_STAGE/config/pdf_imports.json"
cp "config/mineru_api.local.example.json" "$MEFINDER_STAGE/config/mineru_api.local.example.json"
MEFINDER_PYTHON_LICENSE="$($MEFINDER_PYTHON -c 'from pathlib import Path; import sysconfig; print(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt")')"
if [[ ! -f "$MEFINDER_PYTHON_LICENSE" ]]; then
  echo "Build failed: Python runtime license was not found: $MEFINDER_PYTHON_LICENSE" >&2
  exit 1
fi
cp "$MEFINDER_PYTHON_LICENSE" "$MEFINDER_STAGE/Python-runtime-LICENSE.txt"

MEFINDER_ICONSET="$MEFINDER_STAGE/MEFinder.iconset"
mkdir -p "$MEFINDER_ICONSET"
# Rasterize each size directly from SVG. qlmanage thumbnails flatten transparent
# corners onto white, which makes the Dock icon appear as a sharp-edged square.
for MEFINDER_SIZE in 16 32 128 256 512; do
  sips -s format png -z "$MEFINDER_SIZE" "$MEFINDER_SIZE" "assets/app_icon.svg" \
    --out "$MEFINDER_ICONSET/icon_${MEFINDER_SIZE}x${MEFINDER_SIZE}.png" >/dev/null
  MEFINDER_RETINA_SIZE=$((MEFINDER_SIZE * 2))
  sips -s format png -z "$MEFINDER_RETINA_SIZE" "$MEFINDER_RETINA_SIZE" "assets/app_icon.svg" \
    --out "$MEFINDER_ICONSET/icon_${MEFINDER_SIZE}x${MEFINDER_SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$MEFINDER_ICONSET" -o "$MEFINDER_STAGE/app_icon.icns"

"$MEFINDER_PYTHON" -m unittest \
  tests.test_anchor_metadata \
  tests.test_api_fallback_recovery \
  tests.test_backup_service \
  tests.test_backup_file_picker \
  tests.test_batch_document_removal \
  tests.test_batch_directory_import \
  tests.test_calibration_library_ui \
  tests.test_citations \
  tests.test_cnki_citation \
  tests.test_journal_metadata_lookup \
  tests.test_foreign_book_lookup \
  tests.test_crossref_lookup \
  tests.test_book_metadata_lookup \
  tests.test_chunked_upload \
  tests.test_data_location \
  tests.test_database_resilience \
  tests.test_desktop_portable \
  tests.test_desktop_shell_controller \
  tests.test_directory_scan \
  tests.test_fts_search_scalability \
  tests.test_import_config_concurrency \
  tests.test_preferences_concurrency \
  tests.test_import_queue \
  tests.test_import_resume_mineru \
  tests.test_import_resume_queue \
  tests.test_import_resume_vision \
  tests.test_import_resume_web \
  tests.test_mineru_config \
  tests.test_mineru_local_settings \
  tests.test_mineru_local_provider \
  tests.test_mineru_engine_import_bridge \
  tests.test_parser_settings_controller \
  tests.test_import_job_controller \
  tests.test_import_parser_executor \
  tests.test_import_orchestrator \
  tests.test_macos_update \
  tests.test_macos_pdf_viewer \
  tests.test_large_index_resilience \
  tests.test_library_startup_performance \
  tests.test_long_filename_import \
  tests.test_platform_open \
  tests.test_pdf_match_anchors \
  tests.test_pdf_import_config \
  tests.test_page_display \
  tests.test_runtime_page_mapping \
  tests.test_scan_directory_picker \
  tests.test_scan_skips_media_libraries \
  tests.test_search_match_spans \
  tests.test_search_occurrence_identity \
  tests.test_search_service \
  tests.test_api_request_limits \
  tests.test_source_streaming \
  tests.test_app_context \
  tests.test_database_page_anchors \
  tests.test_index_publication_guard \
  tests.test_normalization \
  tests.test_search_controls_and_views \
  tests.test_structured_reader \
  tests.test_structured_reader_frontend \
  tests.test_structured_reader_web \
  tests.test_theme_system \
  tests.test_toast_presentation \
  tests.test_vision_api \
  tests.test_frontend_assets \
  tests.test_frontend_pure_logic \
  tests.test_portable_index_rebuild \
  tests.test_runtime_location \
  tests.test_literature_verification_service \
  tests.test_mcp_v1_baseline \
  tests.test_mcp_server \
  tests.test_mcp_quality \
  tests.test_mcp_documentation \
  tests.test_mcp_packaging \
  tests.test_mcp_concurrency

if command -v node >/dev/null 2>&1; then
  # app.js 已按功能拆分到 static/js/，逐个检查以免新增文件漏检。
  frontend_scripts=(src/me_finder/static/js/*.js)
  if [ ! -e "${frontend_scripts[0]}" ]; then
    echo "static/js contains no JavaScript files." >&2
    exit 1
  fi
  for script in "${frontend_scripts[@]}"; do
    node --check "$script"
  done
  node --check src/me_finder/static/reader.js
fi

MEFINDER_APP_VERSION="$MEFINDER_VERSION" \
MEFINDER_TARGET_ARCH="$MEFINDER_ARCH" \
  "$MEFINDER_PYTHON" -m PyInstaller desktop_macos.spec \
    --clean \
    --noconfirm \
    --distpath "$MEFINDER_TEMP_DIST"

if [[ ! -d "$MEFINDER_BUILT_APP" ]]; then
  echo "Build failed: $MEFINDER_BUILT_APP was not created." >&2
  exit 1
fi

MEFINDER_TARGET_ARCH="$MEFINDER_ARCH" \
MEFINDER_CODESIGN_IDENTITY="$MEFINDER_CODESIGN_IDENTITY" \
  "$MEFINDER_PYTHON" -m PyInstaller mcp_sidecar.spec \
    --clean \
    --noconfirm \
    --distpath "$MEFINDER_SIDECAR_DIST" \
    --workpath "$MEFINDER_SIDECAR_WORK"
if [[ ! -x "$MEFINDER_BUILT_SIDECAR" ]]; then
  echo "Build failed: $MEFINDER_BUILT_SIDECAR was not created." >&2
  exit 1
fi
cp "$MEFINDER_BUILT_SIDECAR" "$MEFINDER_BUILT_APP/Contents/MacOS/MEFinderMCP"
chmod 755 "$MEFINDER_BUILT_APP/Contents/MacOS/MEFinderMCP"
smoke_sidecar "$MEFINDER_BUILT_APP/Contents/MacOS/MEFinderMCP"

if ! find "$MEFINDER_BUILT_APP" -type f \
  -path '*/Quartz/PDFKit/_PDFKit*.so' -print -quit | grep -q .; then
  echo "Build failed: the app does not contain the PyObjC PDFKit bridge." >&2
  exit 1
fi

if ! find "$MEFINDER_BUILT_APP" -type f \
  -path '*/certifi/cacert.pem' -print -quit | grep -q .; then
  echo "Build failed: the app does not contain the HTTPS CA bundle." >&2
  exit 1
fi

if find "$MEFINDER_BUILT_APP" -type f \( \
  -name "mineru_api.local.json" -o \
  -name "vision_api.local.json" -o \
  -name "preferences.json" -o \
  -name "desktop.log" \
\) -print -quit | grep -q .; then
  echo "Build failed: the app contains private or generated state." >&2
  exit 1
fi

clean_app_metadata "$MEFINDER_BUILT_APP"
sign_sidecar "$MEFINDER_BUILT_APP/Contents/MacOS/MEFinderMCP"
codesign "${MEFINDER_CODESIGN_ARGS[@]}" "$MEFINDER_BUILT_APP"
verify_app_signature "$MEFINDER_BUILT_APP"
smoke_sidecar "$MEFINDER_BUILT_APP/Contents/MacOS/MEFinderMCP"

# Build every release artifact under the local temporary directory. A project
# stored in Documents/iCloud Drive can have FinderInfo reattached asynchronously
# by File Provider, which makes an otherwise valid signed bundle fail strict
# verification. Never package from the workspace's dist/ copy.
mkdir -p release
ditto -c -k --keepParent \
  --norsrc \
  --noextattr \
  --noqtn \
  --noacl \
  "$MEFINDER_BUILT_APP" \
  "$MEFINDER_TEMP_ZIP"
MEFINDER_ZIP_CONTENTS="$(zipinfo -1 "$MEFINDER_TEMP_ZIP")"
if LC_ALL=C grep -Eq '(^|/)\._' <<<"$MEFINDER_ZIP_CONTENTS"; then
  echo "Build failed: ZIP contains AppleDouble metadata." >&2
  exit 1
fi
mkdir -p "$MEFINDER_TEMP_ROOT/verify"
ditto -x -k "$MEFINDER_TEMP_ZIP" "$MEFINDER_TEMP_ROOT/verify"
verify_app_signature "$MEFINDER_TEMP_ROOT/verify/MEFinder.app"
smoke_sidecar "$MEFINDER_TEMP_ROOT/verify/MEFinder.app/Contents/MacOS/MEFinderMCP"

mkdir -p "$MEFINDER_DMG_STAGE" "$MEFINDER_DMG_MOUNT" "$(dirname "$MEFINDER_DMG_VERIFY_COPY")"
ditto --norsrc --noextattr --noqtn --noacl \
  "$MEFINDER_BUILT_APP" \
  "$MEFINDER_DMG_STAGE/MEFinder.app"
ln -s /Applications "$MEFINDER_DMG_STAGE/Applications"
clean_app_metadata "$MEFINDER_DMG_STAGE/MEFinder.app"
verify_app_signature "$MEFINDER_DMG_STAGE/MEFinder.app"

hdiutil create \
  -volname "MEFinder ${MEFINDER_VERSION}" \
  -srcfolder "$MEFINDER_DMG_STAGE" \
  -ov \
  -format UDZO \
  "$MEFINDER_TEMP_DMG"
hdiutil verify "$MEFINDER_TEMP_DMG"
hdiutil attach \
  -readonly \
  -nobrowse \
  -noautoopen \
  -mountpoint "$MEFINDER_DMG_MOUNT" \
  "$MEFINDER_TEMP_DMG" >/dev/null
MEFINDER_DMG_ATTACHED=1

if [[ ! -L "$MEFINDER_DMG_MOUNT/Applications" ]] \
  || [[ "$(readlink "$MEFINDER_DMG_MOUNT/Applications")" != "/Applications" ]]; then
  echo "Build failed: the DMG does not contain an /Applications shortcut." >&2
  exit 1
fi
verify_app_signature "$MEFINDER_DMG_MOUNT/MEFinder.app"
smoke_sidecar "$MEFINDER_DMG_MOUNT/MEFinder.app/Contents/MacOS/MEFinderMCP"

# Simulate copying the app out of the mounted image and verify that the copied
# application remains valid.
ditto "$MEFINDER_DMG_MOUNT/MEFinder.app" "$MEFINDER_DMG_VERIFY_COPY"
verify_app_signature "$MEFINDER_DMG_VERIFY_COPY"
smoke_sidecar "$MEFINDER_DMG_VERIFY_COPY/Contents/MacOS/MEFinderMCP"

hdiutil detach "$MEFINDER_DMG_MOUNT" >/dev/null
MEFINDER_DMG_ATTACHED=0

rm -f \
  "$MEFINDER_ZIP" \
  "${MEFINDER_ZIP}.sha256.txt" \
  "$MEFINDER_DMG" \
  "${MEFINDER_DMG}.sha256.txt"
ditto --norsrc --noextattr --noqtn --noacl "$MEFINDER_TEMP_ZIP" "$MEFINDER_ZIP"
ditto --norsrc --noextattr --noqtn --noacl "$MEFINDER_TEMP_DMG" "$MEFINDER_DMG"
hdiutil verify "$MEFINDER_DMG"
(
  cd release
  shasum -a 256 "${MEFINDER_PACKAGE}.zip" > "${MEFINDER_PACKAGE}.zip.sha256.txt"
  shasum -a 256 "${MEFINDER_PACKAGE}.dmg" > "${MEFINDER_PACKAGE}.dmg.sha256.txt"
  shasum -a 256 -c "${MEFINDER_PACKAGE}.zip.sha256.txt"
  shasum -a 256 -c "${MEFINDER_PACKAGE}.dmg.sha256.txt"
)

echo "Release ZIP: $MEFINDER_ZIP"
echo "Installer DMG: $MEFINDER_DMG"
echo "Runtime data: ~/Library/Application Support/MEFinder"
