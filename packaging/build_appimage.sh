#!/usr/bin/env bash
# Build dist/osv360-<version>-x86_64.AppImage
#
#   packaging/build_appimage.sh
#
# Needs: uv, network access on first run (Python wheels, appimagetool, AppImage runtime).
# Everything downloaded or built stays inside this repository (.cache/, build/, dist/).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/osv360/__init__.py)"
ARCH=x86_64
APPIMAGETOOL_VERSION=1.9.1
RUNTIME_VERSION=20251108
TOOLS="$ROOT/.cache/appimage-tools"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
OUT="$ROOT/dist/osv360-$VERSION-$ARCH.AppImage"
export UV_CACHE_DIR="$ROOT/.cache/uv"

echo "==> python environment"
uv sync --extra build --extra dev

echo "==> tests"
.venv/bin/python -m pytest -q

echo "==> third-party license notices"
mkdir -p "$BUILD"
.venv/bin/python -m osv360.licenses > "$BUILD/THIRD_PARTY_LICENSES.txt"

echo "==> pyinstaller"
rm -rf "$BUILD/pyinstaller" "$BUILD/dist"  # (license notices above are kept)
.venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --workpath "$BUILD/pyinstaller" --distpath "$BUILD/dist" packaging/osv360.spec

echo "==> smoke test of the frozen binary"
QT_QPA_PLATFORM=offscreen "$BUILD/dist/osv360/osv360" selftest

echo "==> AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$BUILD/dist/osv360" "$APPDIR/usr/lib/osv360"
install -m755 packaging/AppRun "$APPDIR/AppRun"
install -m644 packaging/osv360.desktop "$APPDIR/osv360.desktop"
install -m644 packaging/osv360.desktop "$APPDIR/usr/share/applications/osv360.desktop"
install -m644 packaging/osv360.appdata.xml "$APPDIR/usr/share/metainfo/osv360.appdata.xml"
install -Dm644 LICENSE "$APPDIR/usr/share/doc/osv360/LICENSE"
install -Dm644 "$BUILD/THIRD_PARTY_LICENSES.txt" "$APPDIR/usr/share/doc/osv360/THIRD_PARTY_LICENSES.txt"
SVG=src/osv360/gui/assets/osv360.svg
install -m644 "$SVG" "$APPDIR/usr/share/icons/hicolor/scalable/apps/osv360.svg"
if command -v rsvg-convert >/dev/null; then
    rsvg-convert -w 256 -h 256 "$SVG" -o "$APPDIR/usr/share/icons/hicolor/256x256/apps/osv360.png"
else  # render with Qt (always available in the build env)
    QT_QPA_PLATFORM=offscreen .venv/bin/python - "$SVG" "$APPDIR/usr/share/icons/hicolor/256x256/apps/osv360.png" <<'PY'
import sys
from PySide6.QtGui import QGuiApplication, QIcon
app = QGuiApplication([])
QIcon(sys.argv[1]).pixmap(256, 256).save(sys.argv[2])
PY
fi
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/osv360.png" "$APPDIR/osv360.png"
ln -sf osv360.png "$APPDIR/.DirIcon"

echo "==> appimagetool / runtime $RUNTIME_VERSION"
mkdir -p "$TOOLS"
# use an installed appimagetool if there is one (or APPIMAGETOOL=/path/to/it), else a pinned download
TOOL="${APPIMAGETOOL:-$(command -v appimagetool || true)}"
if [ -z "$TOOL" ]; then
    TOOL="$TOOLS/appimagetool-$APPIMAGETOOL_VERSION-$ARCH.AppImage"
    [ -f "$TOOL" ] || curl -fL -o "$TOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-$ARCH.AppImage"
    chmod +x "$TOOL"
fi
echo "    using $TOOL"
RUNTIME="$TOOLS/runtime-$RUNTIME_VERSION-$ARCH"
[ -f "$RUNTIME" ] || curl -fL -o "$RUNTIME" \
    "https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_VERSION/runtime-$ARCH"

mkdir -p "$(dirname "$OUT")"
# extract-and-run: works without FUSE on the build machine
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=$ARCH "$TOOL" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
echo "==> built $OUT ($(du -h "$OUT" | cut -f1))"
