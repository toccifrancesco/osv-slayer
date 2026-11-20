# PyInstaller spec for osv360 (one-dir bundle, wrapped into an AppImage by build_appimage.sh)
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

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
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="osv360", console=True, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="osv360")
