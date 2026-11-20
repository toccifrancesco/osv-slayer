# Development

## Setup

```bash
uv sync --extra gui --extra dev          # .venv with numpy, scipy, wgpu, opencv, PySide6, pytest
.venv/bin/python -m pytest -q            # tests (footage-dependent ones skip without an .OSV in footage/)
.venv/bin/osv360                         # run the app from source
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
packaging/        PyInstaller spec, AppRun, desktop entry, AppImage build script
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

## Building the AppImage

```bash
packaging/build_appimage.sh          # -> dist/osv360-<version>-x86_64.AppImage
```

The script:
1. syncs the environment and runs the tests;
2. builds a PyInstaller one-dir bundle (`packaging/osv360.spec`);
3. assembles `build/AppDir` (AppRun, desktop entry, icons, AppStream metadata);
4. packs it with **appimagetool 1.9.1** and the **type2-runtime 20251108**, both pinned. They are downloaded once into `.cache/appimage-tools/`.

Things to know:
- **Not bundled:**
  - ffmpeg: VA-API drivers and the encoder must match the user's system;
  - the Vulkan loader and drivers.

  Child processes for system tools get the original `LD_LIBRARY_PATH` (`ffio.sysenv()`), so they never load the bundle's libraries.
- **glibc:** the AppImage needs a glibc at least as new as the build machine's. For a widely portable release, build in an older base image, e.g. a manylinux or Ubuntu 22.04 container.
- **One binary:** the frozen binary is both the GUI and the CLI. The GUI runs conversions by launching itself with `convert --progress-json`.

## Releasing

1. Bump `__version__` in `src/osv360/__init__.py` and `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. `packaging/build_appimage.sh`, then attach `dist/*.AppImage` to the GitHub release.
