# Changelog

## 0.1.0 — 2026-11-20

First release.

- `.OSV` container + `dvtm_oq101` metadata decoding (lens calibration, 1 kHz attitude).
- Per-clip lens self-calibration from the overlap (seam ≈ 2 px at 8K).
- IMU stabilization: horizon lock with smoothed heading, full lock, smooth, off.
- Vulkan (wgpu) stitch shader, VA-API decode/encode, HEVC Main10 `hvc1` output with Spherical Video V1/V2 metadata.
- Lens exposure/colour matching.
- CLI (`convert`, `still`, `info`, `doctor`) with JSON progress and clean cancellation.
- PySide6 desktop app: drag & drop batch queue, thumbnails, progress/ETA, settings, notifications.
- Packages: Linux AppImage; Windows zip and macOS dmg (experimental), built by GitHub Actions.
- Hardware encoder auto-selection (VA-API, NVENC, AMF, Quick Sync, VideoToolbox) with CPU x265 fallback.
