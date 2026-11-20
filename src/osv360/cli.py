"""osv360 command line.

    osv360                      -> opens the GUI
    osv360 info FILE.OSV
    osv360 convert A.OSV [B.OSV ...] [-o OUT | --output-dir DIR] [options]
    osv360 still FILE.OSV --time 30 -o frame.png [options]
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

import numpy as np

DEFAULT_NAME = "{name}_{mode}"


def cmd_info(a):
    from .pipeline import open_source
    for path in a.inputs:
        src = open_source(path)
        m = src.meta
        fps = src.fps_num / src.fps_den
        print(f"{src.path.name}: {m.model} ({m.proto}), serial {m.serial}")
        print(f"  video: 2 x {src.lens_size}x{src.lens_size} HEVC @ {fps:.3f} fps, {src.n_frames} frames "
              f"({src.n_frames / fps:.2f}s); audio stream: {src.audio_stream}")
        print(f"  IMU: {len(m.imu_ts_us)} samples @ {m.imu_rate} Hz")
        for i, l in enumerate(m.lenses):
            print(f"  lens {i}: f=({l.fx:.1f},{l.fy:.1f}) c=({l.cx:.1f},{l.cy:.1f}) "
                  f"k={np.round(l.k, 5).tolist()} ypr={np.round(l.ypr_deg, 2).tolist()}")


def build_options(a):
    from .ffio import EncodeSettings
    from .motion import StabParams
    from .pipeline import ConvertOptions
    w = int(round(7680 * a.scale / 2)) * 2
    return ConvertOptions(
        width=w, height=w // 2, start_s=a.start, duration_s=getattr(a, "duration", None),
        stab=StabParams(mode=a.stabilize, yaw_smooth_s=a.yaw_smooth, smooth_s=a.smooth),
        encode=EncodeSettings(codec=a.codec, qp=a.qp, gop=a.gop, prores_profile=a.prores_profile),
        recalibrate=a.recalibrate, readout_s=a.readout_ms / 1000.0,
        exposure_match=not a.no_exposure_match, blend_deg=a.blend)


def output_path(src: str | Path, out_dir: str | Path, template: str, mode: str, codec: str) -> Path:
    ext = ".mov" if codec == "prores" else ".mp4"
    name = template.format(name=Path(src).stem, mode=mode, codec=codec)
    return Path(out_dir) / f"{name}{ext}"


class JsonEvents:
    """machine-readable progress on stdout (one JSON object per line) for the GUI."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def emit(self, **kw):
        if self.enabled:
            sys.stdout.write(json.dumps(kw) + "\n")
            sys.stdout.flush()

    def log(self, msg: str):
        if self.enabled:
            self.emit(event="log", msg=str(msg))
        else:
            print(msg, flush=True)


def cmd_convert(a):
    from .pipeline import Cancelled, convert

    ev = JsonEvents(a.progress_json)
    cancel = threading.Event()

    def on_signal(_sig, _frame):
        cancel.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if a.output and len(a.inputs) > 1:
        raise SystemExit("-o/--output takes a single input; use --output-dir for batches")
    opts = build_options(a)
    failures = 0
    for idx, src in enumerate(a.inputs):
        out = Path(a.output) if a.output else output_path(src, a.output_dir, a.name, a.stabilize, a.codec)
        if out.exists() and not a.overwrite:
            ev.log(f"skip {src}: {out} exists (use --overwrite)")
            ev.emit(event="skipped", input=str(src), output=str(out), index=idx)
            continue
        ev.emit(event="start", input=str(src), output=str(out), index=idx, total=len(a.inputs))

        def progress(stage, done, total, fps, _idx=idx):
            ev.emit(event="progress", stage=stage, done=done, total=total, fps=round(fps, 2), index=_idx)

        try:
            stats = convert(src, out, opts, log=ev.log, progress=progress, cancel=cancel, sidecar=a.sidecar)
            ev.emit(**{**stats, "event": "done", "input": str(src), "output": str(out), "index": idx})
        except Cancelled:
            ev.log("cancelled")
            ev.emit(event="cancelled", input=str(src), index=idx)
            return 130
        except Exception as e:  # noqa: BLE001  (report and continue with the batch)
            failures += 1
            ev.log(f"error converting {src}: {e}")
            ev.emit(event="error", input=str(src), index=idx, msg=str(e))
    return 1 if failures else 0


def cmd_still(a):
    """render one frame to an 8-bit image via the GPU path."""
    import cv2

    from .ffio import LensDecoder
    from .gpu.stitcher import GPUStitcher, LensFrameParams
    from .pipeline import ExposureMatcher, calibrated_rig, frame_motion, open_source

    opts = build_options(a)
    src = open_source(a.input)
    fps = src.fps_num / src.fps_den
    idx = min(int(round(a.time * fps)), src.n_frames - 1)
    rig = calibrated_rig(src, opts)
    bv, omega = frame_motion(src, rig, opts)
    fr = []
    for s in src.video_streams:
        d = LensDecoder(src.path, s, src.lens_size, idx / fps, 1)
        fr.append(d.read())
        d.close()
    g = GPUStitcher(rig, opts.width, opts.height, src.lens_size, readout_s=opts.readout_s)
    g0, g1, du, dv = ExposureMatcher(rig, tau_frames=1).update(fr[0], fr[1], src.lens_size) \
        if opts.exposure_match else (1, 1, 0, 0)
    for i in (0, 1):
        g.upload(i, fr[i])
    inv = [l.rot.inv() for l in rig.lenses]
    out = g.render(bv[idx], [
        LensFrameParams(g0, off_u=du, off_v=dv, omega_lens=tuple(inv[0].apply(omega[idx]))),
        LensFrameParams(g1, off_u=-du, off_v=-dv, omega_lens=tuple(inv[1].apply(omega[idx])))])
    bgr = p010_to_bgr(out, opts.width, opts.height)
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(a.output, bgr)
    print(f"wrote {a.output} (frame {idx})")


def doctor() -> dict:
    """check the runtime requirements: ffmpeg (+ hevc_vaapi), VA-API render node, Vulkan GPU."""
    import shutil
    import subprocess

    from . import __version__
    from .ffio import VAAPI_DEVICE, sysenv

    r = {"version": __version__, "ffmpeg": None, "hevc_vaapi": False, "prores": False,
         "vaapi_device": Path(VAAPI_DEVICE).exists(), "gpu": None, "ok": False}
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, env=sysenv()).stdout
        r["ffmpeg"] = out.splitlines()[0] if out else "unknown"
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, env=sysenv()).stdout
        r["hevc_vaapi"] = "hevc_vaapi" in enc
        r["prores"] = "prores_ks" in enc
    try:
        import wgpu
        a = wgpu.gpu.request_adapter_sync(power_preference="high-performance")
        r["gpu"] = f"{a.info['device']} ({a.info['backend_type']})" if a else None
    except Exception as e:  # noqa: BLE001
        r["gpu_error"] = str(e)
    r["ok"] = bool(r["ffmpeg"] and r["gpu"])
    return r


def cmd_doctor(a):
    r = doctor()
    if a.json:
        print(json.dumps(r))
    else:
        for k, v in r.items():
            print(f"{k:14s} {v}")
    return 0 if r["ok"] else 1


def p010_to_bgr(p010: np.ndarray, W: int, H: int) -> np.ndarray:
    import cv2
    y = (p010[:W * H].reshape(H, W) >> 6).astype(np.float32)
    c = (p010[W * H:].reshape(H // 2, W // 2, 2) >> 6).astype(np.float32)
    c = cv2.resize(c, (W, H), interpolation=cv2.INTER_LINEAR)
    y = (y - 64) / 876
    u = (c[..., 0] - 512) / 896
    v = (c[..., 1] - 512) / 896
    r = y + 1.5748 * v
    g = y - 0.1873 * u - 0.4681 * v
    b = y + 1.8556 * u
    return np.clip(np.dstack([b, g, r]) * 255, 0, 255).astype(np.uint8)


def add_render_options(sp):
    sp.add_argument("--stabilize", choices=["horizon", "lock", "smooth", "none"], default="horizon",
                    help="horizon: level + smoothed heading (default); lock: fixed direction; "
                         "smooth: low-pass all axes; none: raw camera orientation")
    sp.add_argument("--yaw-smooth", type=float, default=1.0, help="heading smoothing sigma in s (horizon)")
    sp.add_argument("--smooth", type=float, default=0.5, help="rotation smoothing sigma in s (smooth)")
    sp.add_argument("--scale", type=float, default=1.0, help="output size relative to 7680x3840")
    sp.add_argument("--codec", choices=["hevc", "prores", "x265"], default="hevc")
    sp.add_argument("--qp", type=int, default=27, help="HEVC constant QP (lower = bigger/better; 30 ~ 300 Mbit/s)")
    sp.add_argument("--gop", type=int, default=25, help="keyframe interval in frames")
    sp.add_argument("--prores-profile", type=int, default=3, help="0 proxy, 1 LT, 2 422, 3 HQ")
    sp.add_argument("--readout-ms", type=float, default=0.0,
                    help="rolling-shutter readout time in ms (0 = no RS correction)")
    sp.add_argument("--blend", type=float, default=6.0, help="seam feather width in degrees")
    sp.add_argument("--no-exposure-match", action="store_true")
    sp.add_argument("--recalibrate", action="store_true", help="ignore the cached lens calibration")
    sp.add_argument("--start", type=float, default=0.0, help="start time (s)")


def build_parser():
    p = argparse.ArgumentParser(prog="osv360",
                                description="DJI Osmo 360 .OSV -> stabilized equirectangular video. "
                                            "Run without arguments to open the GUI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("info", help="show metadata of .OSV files")
    pi.add_argument("inputs", nargs="+")
    pi.set_defaults(func=cmd_info)

    pc = sub.add_parser("convert", help="stitch + stabilize + encode one or more files")
    pc.add_argument("inputs", nargs="+")
    add_render_options(pc)
    pc.add_argument("-o", "--output", help="output file (single input only)")
    pc.add_argument("--output-dir", default="output", help="output folder for batches (default: ./output)")
    pc.add_argument("--name", default=DEFAULT_NAME, help="file name template: {name} {mode} {codec}")
    pc.add_argument("--overwrite", action="store_true")
    pc.add_argument("--duration", type=float, default=None, help="seconds to render (default: all)")
    pc.add_argument("--sidecar", action="store_true", help="write a .json with the settings next to the output")
    pc.add_argument("--progress-json", action="store_true", help="machine-readable progress on stdout")
    pc.set_defaults(func=cmd_convert)

    pd = sub.add_parser("doctor", help="check ffmpeg / VA-API / GPU availability")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_doctor)

    ps = sub.add_parser("still", help="render a single frame to an image")
    ps.add_argument("input")
    add_render_options(ps)
    ps.add_argument("--time", type=float, default=0.0)
    ps.add_argument("-o", "--output", required=True)
    ps.set_defaults(func=cmd_still)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or all(a.lower().endswith(".osv") for a in argv):
        # no arguments, or opened with files from a file manager / desktop entry -> GUI
        from .gui.app import run
        return run([sys.argv[0], *argv])
    a = build_parser().parse_args(argv)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
