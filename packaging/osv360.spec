# PyInstaller spec for osv360 (Linux, Windows, macOS).
#
#   Linux:   one console-capable binary `osv360` (GUI without args, CLI with a subcommand),
#            wrapped into an AppImage by packaging/build_appimage.sh
#   Windows: `osv360.exe` (windowed GUI, no console window) + `osv360-cli.exe` (console);
#            the GUI runs conversions through the CLI twin
#   macOS:   the same pair inside osv360.app (Contents/MacOS)
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
VERSION = re.search(r'__version__ = "(.+?)"', (SRC / "osv360" / "__init__.py").read_text()).group(1)
ICONS = ROOT / "build" / "icons"
icon = None
if IS_WIN and (ICONS / "osv360.ico").exists():
    icon = str(ICONS / "osv360.ico")
elif IS_MAC and (ICONS / "osv360.icns").exists():
    icon = str(ICONS / "osv360.icns")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    datas=[
        (str(SRC / "osv360" / "gpu" / "stitch.wgsl"), "osv360/gpu"),
        (str(SRC / "osv360" / "gui" / "assets"), "osv360/gui/assets"),
    ],
    hiddenimports=collect_submodules("osv360") + ["PySide6.QtSvg"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtNetwork", "PySide6.QtWebEngineCore", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exes = [EXE(pyz, a.scripts, [], exclude_binaries=True, name="osv360", icon=icon,
            console=not (IS_WIN or IS_MAC), strip=False, upx=False)]
if IS_WIN or IS_MAC:
    exes.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="osv360-cli", icon=icon,
                    console=True, strip=False, upx=False))

coll = COLLECT(*exes, a.binaries, a.datas, strip=False, upx=False, name="osv360")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="osv360.app",
        icon=icon,
        bundle_identifier="io.github.toccifrancesco.osv360",
        version=VERSION,
        info_plist={
            "CFBundleName": "osv360",
            "CFBundleDisplayName": "osv360",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSHumanReadableCopyright": "MIT License",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "DJI Osmo 360 video",
                "CFBundleTypeExtensions": ["osv", "OSV"],
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
            }],
        },
    )
