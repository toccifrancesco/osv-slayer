"""Third-party license notices for the software bundled in the osv360 packages.

`build_notices()` walks the runtime dependency tree of osv360 (via the installed
package metadata) and concatenates every license / notice file each package ships
(those files also cover the native libraries the wheels vendor, e.g. OpenBLAS,
libgfortran, FFmpeg in OpenCV, Qt in PySide6). Python's own license and short
notices for the native libraries of the Python runtime are appended.

The packaging scripts write the result to THIRD_PARTY_LICENSES.txt inside every
package; the app shows it in Settings -> About.
"""
from __future__ import annotations

import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

ROOTS = ["numpy", "scipy", "wgpu", "opencv-python-headless", "pyside6-essentials"]
LICENSE_NAME = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)
BUNDLED_FILE = Path(__file__).parent / "THIRD_PARTY_LICENSES.txt"
TEXTS = Path(__file__).parent / "license_texts"  # texts the packages themselves do not ship

EXTRA_SECTIONS = [  # (title, license label, [files in TEXTS])
    ("Qt 6 / PySide6 / shiboken6 (The Qt Company Ltd. and other contributors)",
     "LGPL-3.0-only; the LGPL-3.0 is a set of additional permissions on top of the GPL-3.0, "
     "so both texts are reproduced", ["LGPL-3.0.txt", "GPL-3.0.txt"]),
    ("wgpu-native (bundled in the wgpu package; includes the wgpu/naga crates)",
     "MIT OR Apache-2.0", ["wgpu-native-LICENSE.MIT"]),
]

HEADER = """\
osv360 — third-party software notices
=====================================

osv360 itself is released under the MIT License (see LICENSE).

The osv360 packages (AppImage / Windows zip / macOS dmg) also contain the
third-party software listed below, each under its own license. Their license
texts and notices follow.

Qt / PySide6 (LGPL-3.0): Qt and PySide6 are used as separate, dynamically
loaded libraries (the libQt6* / Qt6*.dll / Qt*.framework and PySide6 files in
the package), which you may replace with your own builds. Source code:
https://code.qt.io  and  https://code.qt.io/cgit/pyside/pyside-setup.git
FFmpeg libraries bundled inside OpenCV (LGPL-2.1+): source at https://ffmpeg.org .

ffmpeg/ffprobe programs are NOT included; osv360 runs the copy installed on
your system.
"""

RUNTIME_NOTES = """\
Native libraries of the Python runtime (present depending on the platform):
  OpenSSL (Apache-2.0, https://www.openssl.org) · SQLite (public domain) ·
  bzip2 (BSD-style) · xz/liblzma (0BSD / public domain) · mpdecimal (BSD-2-Clause) ·
  libffi (MIT) · zlib (zlib license) · ncurses (MIT-X11) · ICU (Unicode License V3,
  shipped with Qt). The full Apache-2.0 text is included in the OpenCV section above.
"""


def _dist(name: str):
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def runtime_distributions(roots=ROOTS) -> list[metadata.Distribution]:
    """runtime dependency closure (extras and environment-specific deps ignored)."""
    seen: dict[str, metadata.Distribution] = {}
    todo = list(roots)
    while todo:
        name = todo.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen:
            continue
        d = _dist(name)
        if d is None:
            continue
        seen[key] = d
        for req in d.requires or []:
            if "extra ==" in req:
                continue
            m = re.match(r"\s*([A-Za-z0-9._-]+)", req)
            marker = req.split(";", 1)[1] if ";" in req else ""
            if m and _marker_ok(marker):
                todo.append(m.group(1))
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def _marker_ok(marker: str) -> bool:
    """tiny evaluator for the platform markers that occur in our dependency tree."""
    marker = marker.strip()
    if not marker:
        return True
    try:
        from packaging.markers import Marker
        return Marker(marker).evaluate()
    except Exception:  # noqa: BLE001  (packaging missing or odd marker: include to be safe)
        return True


def _license_files(d: metadata.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in d.files or []:
        p = str(f)
        if LICENSE_NAME.search(p) and not p.endswith((".py", ".pyc")):
            try:
                text = Path(f.locate()).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if text.strip():
                out.append((p, text.strip()))
    return out


def _license_label(d: metadata.Distribution) -> str:
    m = d.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    classifier = next((c.split("::")[-1].strip() for c in m.get_all("Classifier") or []
                       if c.startswith("License ::")), None)
    if classifier:
        return classifier
    lines = (m.get("License") or "").strip().splitlines()
    return lines[0][:60] if lines else "see text"


def build_notices() -> str:
    dists = runtime_distributions()
    parts = [HEADER, "Contents\n--------"]
    for d in dists:
        parts.append(f"  {d.metadata['Name']} {d.version} — {_license_label(d)}")
    parts.append(f"  Python {sys.version.split()[0]} — PSF-2.0\n")
    seen_texts: set[int] = set()
    for d in dists:
        files = [(p, t) for p, t in _license_files(d) if hash(t) not in seen_texts]
        seen_texts.update(hash(t) for _p, t in files)
        parts.append("=" * 78)
        parts.append(f"{d.metadata['Name']} {d.version}   ({_license_label(d)})")
        parts.append("=" * 78)
        if not files:
            parts.append(f"License: {_license_label(d)}" + (" (full text in a section below)"
                                                            if "pyside" in d.metadata["Name"].lower()
                                                            or "shiboken" in d.metadata["Name"].lower()
                                                            else " (no license file in the package metadata)"))
        for path, text in files:
            parts.append(f"--- {path} ---\n{text}\n")
    for title, label, names in EXTRA_SECTIONS:
        parts.append("=" * 78 + f"\n{title}   ({label})\n" + "=" * 78)
        for n in names:
            parts.append(f"--- {n} ---\n{(TEXTS / n).read_text(encoding='utf-8').strip()}\n")
    parts.append("=" * 78 + "\nPython " + sys.version.split()[0] + "   (PSF-2.0)\n" + "=" * 78)
    py_license = Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"
    if py_license.is_file():
        parts.append(py_license.read_text(encoding="utf-8", errors="replace").strip())
    else:
        parts.append("Python Software Foundation License Version 2: https://docs.python.org/3/license.html")
    parts.append("\n" + RUNTIME_NOTES)
    return "\n".join(parts) + "\n"


def notices_text() -> str:
    """bundled notices (packaged app) or freshly generated ones (running from source)."""
    if BUNDLED_FILE.is_file():
        return BUNDLED_FILE.read_text(encoding="utf-8", errors="replace")
    return build_notices()


if __name__ == "__main__":  # python -m osv360.licenses > THIRD_PARTY_LICENSES.txt
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(build_notices())
