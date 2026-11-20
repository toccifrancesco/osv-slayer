"""Build a release package for the current OS.

    python packaging/build.py            (run with the project venv: .venv/bin/python or .venv\\Scripts\\python)

    Linux   -> dist/osv360-<ver>-x86_64.AppImage   (delegates to packaging/build_appimage.sh)
    Windows -> dist/osv360-<ver>-windows-x64.zip
    macOS   -> dist/osv360-<ver>-macos-<arch>.dmg

PyInstaller cannot cross-compile: each package must be built on its own OS
(the GitHub Actions workflow in .github/workflows/release.yml does all three).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VERSION = re.search(r'__version__ = "(.+?)"', (ROOT / "src/osv360/__init__.py").read_text()).group(1)
SVG = ROOT / "src/osv360/gui/assets/osv360.svg"


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_icons() -> None:
    """render the SVG into .ico (Windows) / .icns (macOS) / .png with Qt."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QIcon, QImageWriter

    make_icons.app = QGuiApplication.instance() or QGuiApplication([])  # keep the Qt app alive
    out = BUILD / "icons"
    out.mkdir(parents=True, exist_ok=True)
    icon = QIcon(str(SVG))
    for name, size in (("osv360.png", 512), ("osv360.ico", 256), ("osv360.icns", 1024)):
        img = icon.pixmap(QSize(size, size)).toImage()
        w = QImageWriter(str(out / name))
        if not w.write(img):
            raise RuntimeError(f"cannot write {name}: {w.errorString()}")
    print(f"icons -> {out}")


def notices() -> Path:
    """THIRD_PARTY_LICENSES.txt for everything that ends up in the package (see osv360/licenses.py)."""
    BUILD.mkdir(exist_ok=True)
    out = BUILD / "THIRD_PARTY_LICENSES.txt"
    text = subprocess.run([sys.executable, "-m", "osv360.licenses"], check=True, capture_output=True).stdout
    out.write_bytes(text)
    print(f"notices -> {out} ({len(text) // 1024} KB)")
    return out


def pyinstaller() -> Path:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--workpath", BUILD / "pyinstaller", "--distpath", BUILD / "dist", ROOT / "packaging/osv360.spec"])
    return BUILD / "dist"


def smoke_test(cli: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    run([cli, "selftest"], env=env)
    run([cli, "--help"], env=env, stdout=subprocess.DEVNULL)


def package_windows() -> Path:
    dist = pyinstaller()
    folder = dist / "osv360"
    smoke_test(folder / "osv360-cli.exe")
    shutil.copy(ROOT / "packaging/README-windows.txt", folder / "README.txt")
    shutil.copy(ROOT / "LICENSE", folder / "LICENSE.txt")
    shutil.copy(BUILD / "THIRD_PARTY_LICENSES.txt", folder / "THIRD_PARTY_LICENSES.txt")
    DIST.mkdir(exist_ok=True)
    # note: not Path.with_suffix(): the version's dots would be taken for an extension
    archive = shutil.make_archive(str(DIST / f"osv360-{VERSION}-windows-x64"), "zip",
                                  root_dir=dist, base_dir="osv360")
    return Path(archive)


def package_macos() -> Path:
    dist = pyinstaller()
    app = dist / "osv360.app"
    smoke_test(app / "Contents/MacOS/osv360-cli")
    # ad-hoc signature: required for Apple Silicon to launch the binaries at all
    run(["codesign", "--force", "--deep", "--sign", "-", app])
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run(["ditto", app, stage / "osv360.app"])
    (stage / "Applications").symlink_to("/Applications")
    shutil.copy(ROOT / "packaging/README-macos.txt", stage / "READ ME FIRST.txt")
    shutil.copy(ROOT / "LICENSE", stage / "LICENSE.txt")
    shutil.copy(BUILD / "THIRD_PARTY_LICENSES.txt", stage / "THIRD_PARTY_LICENSES.txt")
    DIST.mkdir(exist_ok=True)
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    out = DIST / f"osv360-{VERSION}-macos-{arch}.dmg"
    out.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", f"osv360 {VERSION}", "-srcfolder", stage, "-ov", "-format", "UDZO", out])
    return out


def main() -> int:
    if sys.platform.startswith("linux"):
        run(["bash", ROOT / "packaging/build_appimage.sh"])
        return 0
    make_icons()
    notices()
    out = package_windows() if sys.platform == "win32" else package_macos()
    print(f"==> built {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
