# How osv360 works

```
.OSV ─┬─ ffmpeg VA-API decode, lens 0 ─ P010 frames ─┐
      ├─ ffmpeg VA-API decode, lens 1 ─ P010 frames ─┤→ Vulkan compute shader (wgpu), one pass per frame:
      │                                              │    output pixel → view ray → stabilizing rotation
      └─ MP4 parser → djmd protobuf ─────────────────┘    → lens frame → fisheye pixel (both lenses)
         (lens calibration + 1 kHz attitude)              → feathered, exposure-matched blend (10-bit YUV)
                                                     → P010 → ffmpeg hevc_vaapi → remux (+ audio, 360 metadata)
```

The `.OSV` is read in place: nothing is extracted to disk, and frames exist only in RAM.

## 1. Container (`mp4.py`)

An `.OSV` is an ISO-BMFF (MP4) file with these tracks:

| track | content |
|---|---|
| 1, 2 | HEVC Main10 3840×3840 @ 50 fps, one per lens |
| 3 | AAC audio |
| 4, 5 | `djmd`: DJI metadata (protobuf), one sample per video frame |
| 6, 7 | `dbgi`: debug info (unused) |

There is also an embedded 688×344 equirect thumbnail (the GUI shows it).
`mp4.py` is a minimal box reader. It builds the sample tables (`stsz`, `stco`/`co64`, `stsc`), so any metadata sample can be read directly from the file.

## 2. Metadata (`djmd.py`)

`djmd` is protobuf (`dvtm_oq101.proto`), with no published schema. The fields used are:

| path | meaning |
|---|---|
| `1.1.1` / `1.1.10` | proto name / model (`Osmo 360`) |
| `1.10.1` | IMU rate: **1000 Hz** |
| `2.6.{1,2}` | per-lens calibration (see the table below) |
| `3.1.2` | frame timestamp, µs (20 000 µs steps) |
| `3.2.9` | attitude quaternion at frame time (w, x, y, z) |
| `3.3.2.1.1` | timestamp of the first IMU sample in this packet, µs |
| `3.3.2.1.3` | ~20 attitude quaternions per frame (w, x, y, z), 1 ms apart |

These are the fields of the lens calibration entry (`2.6.N`):

| field | meaning |
|---|---|
| 1, 2, 3, 4 | fx, fy, cx, cy (px, for 3840×3840) |
| 5–8 | k1…k4 of a Kannala–Brandt-style polynomial `r = f·θ(1 + k1θ² + k2θ⁴ + k3θ⁶ + k4θ⁸)` |
| 12, 13, 14 | lens yaw, pitch, roll (degrees) |
| 21, 28 | lens→body quaternion (x, y, z, w) |
| 22, 23 | image-circle boundary polyline |

The attitude world frame is **NED-like: +z points down**. This was verified against the footage.

## 3. Lens model and per-clip calibration (`lens.py`, `calibrate.py`)

The stored polynomial is accurate only up to about 60–70° from the lens axis. It peaks at 90° (r ≈ 1639 px), while the image circle reaches about 1850 px (~100°). Used as-is, the two lenses disagree by ~430 px at the seam in 8K. So each clip is calibrated like this:

1. **Extend.** Refit the polynomial so it matches the factory curve up to 60° and continues monotonically to ~110°.
2. **Match.** Render each lens on its own into an equirect at 6 frames spread over the clip. Then match SIFT features inside the overlap band, which tolerates the large initial misalignment, and map them back to fisheye pixel pairs.
3. **Fit.** Solve for both lenses' (f, cx, cy, k1…k4) plus the rotation of lens 0 relative to lens 1, using robust least squares on the angular disagreement of matched rays. The priors are:
   - the factory curve below 60°;
   - the factory principal point within ~5 px;
   - monotonicity up to 108°.

   They are scaled with the number of matches. Without them, the fit can trade a principal-point shift for a lens tilt, which the overlap alone cannot tell apart.
4. Repeat 4 times with a narrowing band.

The result is a seam misalignment of **~0.6 px at 2048 width (~2 px at 8K)**. Two different clips from the same camera converge to the same parameters. The calibration is cached per file in `$XDG_CACHE_HOME/osv360/calib`, or in `./.cache/calib` in a source checkout.

## 4. Motion and stabilization (`motion.py`)

- The attitude is slerp-interpolated at `frame_ts + 7 ms`.
- It is converted from the IMU to the lens body with a fixed mounting rotation (≈ 90° about y).
- Both constants were measured with `tools/imu_align.py`, a hand-eye calibration:
  - The rotation between frames is measured visually on the stitched sphere, with a robust two-view fit that separates rotation from the camera's forward translation.
  - It is compared with the IMU over a scan of time offsets.
  - Residual: **0.020° per 0.1 s step**. A 0 ms offset would leave 0.11°.
- Modes:
  - **horizon** (default): world-down is mapped to view-down exactly, so there is no roll or pitch. The heading follows the camera's heading through a zero-phase Gaussian (σ = `--yaw-smooth`). This is a swing–twist split around gravity, so there are no Euler-angle singularities.
  - **lock**: the same, with the heading frozen at the first frame.
  - **smooth**: the full rotation quaternion is low-passed, so intentional tilts are kept.
  - **none**: the raw camera orientation.

Residual rotation measured on the output with `tools/stab_metric.py` (clip 1, cycling, 12 s):

| | roll p95/step | pitch p95/step |
|---|---|---|
| unstabilized | 1.26° | 3.36° |
| lock | 0.022° | 0.022° |
| horizon | 0.022° | 0.027° |
| DJI's own export of a similar clip | 0.025° | 0.028° |

## 5. The GPU pass (`gpu/stitch.wgsl`, `gpu/stitcher.py`)

- Each invocation produces one 2×2 luma block plus its 4:2:0 chroma sample.
- For each output pixel:
  1. Compute the equirect ray.
  2. Rotate it into each lens frame (`lens_from_view` = lens⁻¹ · body_from_view(t)).
  3. Project it with the radial polynomial.
  4. Sample the P010 textures bilinearly.
  5. Blend with a smoothstep feather of `--blend` degrees across the seam, where both lenses see the ray at the same angle from their axes.
- Per-lens luma gain and chroma offsets, measured on the overlap pixels and smoothed over ~0.5 s, equalise the exposure.
- Optional rolling-shutter correction re-projects each sample with the rotation accumulated between mid-frame and that sensor row.
- The shader matches the NumPy reference (`cpu_ref.py`) to within 0.2 codes (median) on a 10-bit scale.

## 6. Encoding and container (`ffio.py`, `spherical.py`)

- Frames go to `ffmpeg … hevc_vaapi -profile main10 -rc_mode CQP` and are written as an Annex-B elementary stream. They are then remuxed with `-tag:v hvc1`.
  The two steps are needed because `hevc_vaapi`'s global header carries a different PPS `init_qp` than the in-band PPS. Decoders that trust the MP4 `hvcC` box (mandatory for `hvc1`) would otherwise decode garbage.
- BT.709 limited-range tags are set in the stream and in `colr`.
- 360° metadata is injected into the trailing `moov`, so no chunk offsets change. Both flavours are written:
  - Spherical Video V1: the `uuid` XMP box;
  - Spherical Video V2: `st3d` + `sv3d` boxes in the sample entry.

### Quality vs. bitrate (8K50 busy street, VA-API HEVC)

| QP | Mbit/s | PSNR-Y |
|---|---|---|
| 20 | 928 | 45.8 dB |
| 24 | 631 | 42.8 dB |
| **27 (default)** | 455 | 40.6 dB |
| 30 | 307 | 38.3 dB |
| 33 | 198 | 36.2 dB |

## 7. Performance

On a Ryzen AI 9 HX 370 with a Radeon 890M, a full 8K50 clip converts at about **15 fps**: 2 minutes of footage takes about 6.5 min, plus ~70 s for the first calibration. The stages alone run at:
- dual decode: 36 fps;
- GPU stitch with upload/readback: 49 fps;
- encode: 42 fps.

They share one memory bus on an iGPU. The 2 × 44 MB of input and 88 MB of output per frame travel through pipes, because Python offers no zero-copy path between VA-API and Vulkan.

## 8. Things that did not work (so far)

- **Rolling-shutter readout estimation** (`tools/estimate_readout.py`): the residual changed by only ~5% over 0–40 ms, which is not a reliable minimum. RS correction therefore stays opt-in.
- **Gyroflow**: version 1.7 reads Osmo 360 motion data only, cannot play `.OSV` files and has no equirect output. Rotation-only 360° stabilization is simple enough to do inside the stitch shader, with a single resample.
