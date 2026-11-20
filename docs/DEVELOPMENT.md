# Development

## Setup

```bash
uv sync --extra gui --extra dev          # .venv with numpy, scipy, wgpu, opencv, PySide6, pytest
uv run python -m pytest -q               # tests (footage-dependent ones skip without an .OSV in footage/)
uv run osv360                            # run the app from source
uv run osv360 selftest                   # quick check that all components load
```

`pyproject.toml` keeps uv's cache inside the repo (`.cache/uv`). Put sample `.OSV` files in `footage/`; it is git-ignored.

## Layout

```
src/osv360/
  mp4.py          ISO-BMFF reader (track tables, sample access)
  djmd.py         DJI protobuf metadata decoder
  lens.py         lens model, rig, equirect helpers
  calibrate.py    per-clip lens refinement from the overlap
  motion.py       IMU attitude, stabilization modes
  cpu_ref.py      NumPy reference stitch (stills, calibration, test oracle)
  gpu/            WGSL compute shader + wgpu host code
  ffio.py         ffmpeg decode/encode pipes
  spherical.py    360° metadata injection
  pipeline.py     end-to-end conversion (progress + cancellation)
  vision.py       two-view rotation estimation (analysis tools)
  cli.py          command line (GUI when started without arguments)
  gui/            PySide6 app: settings, job runner (one CLI subprocess per file), theme, assets
tests/            pytest
tools/            analysis scripts used to derive the calibration constants, screenshot generator
packaging/        PyInstaller spec, build.py (Windows/macOS), build_appimage.sh (Linux), desktop entry, readmes
.github/workflows CI tests + multi-platform release builds
legacy/           the original prototype scripts, kept for reference
```

## Analysis tools

| script | purpose |
|---|---|
| `tools/imu_align.py` | IMU→body mounting rotation + time offset (hand-eye). Feed it exactly sub-sampled frames (`select=not(mod(n\,5))`, **not** `fps=`) |
| `tools/stab_metric.py VIDEO` | residual roll/pitch/yaw of an equirect video |
| `tools/fit_lens_vs_reference.py` | fit lens models against a known-good equirect stitch |
| `tools/estimate_readout.py` | rolling-shutter readout estimate (inconclusive so far) |
| `tools/make_screenshot.py` | regenerate `docs/screenshot.png` |

## Building packages

PyInstaller cannot cross-compile: each package is built on its own OS.

| OS | command | output |
|---|---|---|
| Linux | `packaging/build_appimage.sh` | `dist/osv360-<ver>-x86_64.AppImage` |
| Windows | `uv run python packaging/build.py` | `dist/osv360-<ver>-windows-x64.zip` |
| macOS | `uv run python packaging/build.py` | `dist/osv360-<ver>-macos-arm64.dmg` |

Every build runs the tests, freezes the app with PyInstaller (`packaging/osv360.spec`) and runs `osv360 selftest` on the frozen binary before packaging. Platform details:
- **Linux**: a single console-capable binary inside an AppImage. **appimagetool** is taken from your `PATH` or `$APPIMAGETOOL`, or downloaded (pinned 1.9.1). The **type2-runtime** is pinned to 20251108. Downloads go to `.cache/appimage-tools/`.
- **Windows and macOS**: two executables share one bundle. `osv360` is the windowed GUI with no console window; `osv360-cli` is the console twin the GUI uses to run conversions. Icons (`.ico`/`.icns`) are rendered from the SVG with Qt at build time.
- **macOS**: the `.app` is ad-hoc signed, not notarized. That needs an Apple Developer account; add `codesign --sign "Developer ID…"` plus `notarytool` to `package_macos()` if you get one.

Things to know:
- **Not bundled:**
  - ffmpeg: hardware encoders and drivers must match the user's system;
  - GPU drivers and the Vulkan loader.

  `ffio.tool()` looks for ffmpeg in this order:
  - `$OSV360_FFMPEG_DIR`;
  - an `ffmpeg/` folder next to the app;
  - `PATH`;
  - Homebrew's and common Windows install folders (GUI apps on macOS don't inherit the shell `PATH`).

  System tools run with the original `LD_LIBRARY_PATH` (`ffio.sysenv()`), never the bundle's.
- **Encoder selection** (`ffio.select_encoder`): it tries the platform's hardware HEVC encoders in order with a real 2-frame encode at the output resolution, and falls back to libx265.
- **Cancellation**: the GUI writes `cancel` to the CLI's stdin. This works on Windows, where SIGTERM can't reach console programs. SIGTERM/SIGINT are also handled.
- **glibc**: the AppImage needs a glibc at least as new as the build machine's. CI builds on Ubuntu 22.04 for that reason.

## Continuous integration and releases

- `.github/workflows/ci.yml`: tests on every push and pull request (Linux).
- `.github/workflows/release.yml`: builds Linux, Windows and macOS packages on GitHub's machines.
  - **Push a tag** `vX.Y.Z` → the three packages are built and published as a GitHub release, with `CHANGELOG.md` as the notes.
  - **Actions tab → Release → Run workflow** → it only builds. Download the packages from the run's *Artifacts* to test them before tagging.

Release checklist:
1. Bump `__version__` in `src/osv360/__init__.py` and `version` in `pyproject.toml`.
2. Update `CHANGELOG.md` and commit.
3. `git tag v0.1.0 && git push origin v0.1.0`.
