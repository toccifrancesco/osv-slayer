"""End-to-end conversion: .OSV -> stabilized equirectangular video."""
from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as R

from . import ffio, spherical
from .calibrate import refine
from .djmd import OSVMeta, read_meta
from .lens import Lens, Rig
from .motion import Attitude, StabParams, body_attitude, view_rotations
from .mp4 import MP4

def _cache_dir() -> Path:
    """per-clip calibration cache: $OSV360_CACHE, else ./.cache in a source checkout,
    else $XDG_CACHE_HOME/osv360 (AppImage / installed)."""
    import os
    if os.environ.get("OSV360_CACHE"):
        return Path(os.environ["OSV360_CACHE"]) / "calib"
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists() and os.access(repo, os.W_OK):
        return repo / ".cache" / "calib"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "osv360" / "calib"


CACHE = _cache_dir()


@dataclass
class ConvertOptions:
    width: int = 7680
    height: int = 3840
    start_s: float = 0.0
    duration_s: float | None = None
    stab: StabParams = field(default_factory=StabParams)
    encode: ffio.EncodeSettings = field(default_factory=ffio.EncodeSettings)
    calib_frames: int = 6
    recalibrate: bool = False
    readout_s: float = 0.0  # rolling-shutter readout time; 0 disables RS correction
    exposure_match: bool = True
    blend_deg: float = 6.0


# ---------------------------------------------------------------- source info
@dataclass
class Source:
    path: Path
    meta: OSVMeta
    video_streams: list[int]
    audio_stream: int | None
    lens_size: int
    fps_num: int
    fps_den: int
    n_frames: int


def open_source(path: str | Path) -> Source:
    path = Path(path)
    with MP4(path) as m:
        vids = [i for i, t in enumerate(m.tracks) if t.handler == "vide" and t.codec in ("hvc1", "hev1")]
        auds = [i for i, t in enumerate(m.tracks) if t.handler == "soun"]
        vt = m.tracks[vids[0]]
        n = vt.sample_count
        # constant frame rate: duration / count in the track timescale
        fps_num, fps_den = vt.timescale * n, vt.duration
        g = np.gcd(fps_num, fps_den)
        fps_num, fps_den = fps_num // g, fps_den // g
        size = vt.width
    meta = read_meta(path)
    if len(vids) != 2:
        raise ValueError(f"{path}: expected 2 video tracks, found {len(vids)}")
    return Source(path, meta, vids, auds[0] if auds else None, size, fps_num, fps_den, n)


# ---------------------------------------------------------------- calibration
def factory_rig(meta: OSVMeta, blend_deg: float) -> Rig:
    lenses = [Lens(c.fx, c.fy, c.cx, c.cy, list(c.k), c.q_body_xyzw, max_theta_deg=100.0).extended()
              for c in meta.lenses]
    return Rig(lenses, blend_deg=blend_deg)


def _gray_frame(path: Path, stream: int, t: float, size: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(path),
                          "-map", f"0:{stream}", "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
                         check=True, capture_output=True, env=ffio.sysenv()).stdout
    return np.frombuffer(raw, np.uint8).reshape(size, size)


def calibrated_rig(src: Source, opts: ConvertOptions, log=print) -> Rig:
    st = src.path.stat()
    # location independent: moving the footage keeps the cached calibration
    key = hashlib.sha1(f"{src.path.name}|{st.st_size}|{st.st_mtime_ns}|v2".encode()).hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{src.path.stem}-{key}.json"
    if cache.exists() and not opts.recalibrate:
        rig = Rig.load(cache)
        rig.blend_deg = opts.blend_deg
        # IMU mounting/offset are camera-model constants: always take the current defaults
        d = Rig(rig.lenses)
        rig.q_body_from_imu, rig.imu_offset_s = d.q_body_from_imu, d.imu_offset_s
        log(f"calibration: cached {cache.name}")
        return rig
    rig = factory_rig(src.meta, opts.blend_deg)
    dur = src.n_frames * src.fps_den / src.fps_num
    times = np.linspace(0.08, 0.92, opts.calib_frames) * dur
    log(f"calibration: refining lens model on {len(times)} frames ...")
    frames = [(_gray_frame(src.path, src.video_streams[0], t, src.lens_size),
               _gray_frame(src.path, src.video_streams[1], t, src.lens_size)) for t in times]
    rig = refine(frames, rig, iterations=4, verbose=True)
    rig.save(cache)
    log(f"calibration: saved {cache}")
    return rig


# ---------------------------------------------------------------- motion
def frame_motion(src: Source, rig: Rig, opts: ConvertOptions):
    """per-frame body_from_view and body-frame angular velocity (for the whole clip)."""
    meta = src.meta
    att = Attitude(meta.imu_ts_us, meta.imu_quat_wxyz)
    body_from_imu = R.from_quat(rig.q_body_from_imu)
    ts = meta.frame_ts_us[:src.n_frames]
    wb = body_attitude(att, ts, body_from_imu, rig.imu_offset_s)
    fps = src.fps_num / src.fps_den
    forward = rig.view_from_body.inv().apply([0, 0, 1])
    if opts.stab.mode == "none":
        bv = R.from_quat(np.tile(rig.view_from_body.inv().as_quat(), (len(ts), 1)))
    elif opts.stab.mode == "smooth":
        body_from_smooth = view_rotations(wb, fps, forward, opts.stab)
        bv = body_from_smooth * rig.view_from_body.inv()
    else:
        bv = view_rotations(wb, fps, forward, opts.stab)
    h = 0.002
    w1 = body_attitude(att, ts, body_from_imu, rig.imu_offset_s, dt_s=h)
    w0 = body_attitude(att, ts, body_from_imu, rig.imu_offset_s, dt_s=-h)
    omega_body = (w0.inv() * w1).as_rotvec() / (2 * h)
    return bv, omega_body


# ---------------------------------------------------------------- exposure match
class ExposureMatcher:
    """Per-lens luma gain + chroma offsets from the overlap band, smoothed in time."""

    def __init__(self, rig: Rig, tau_frames: float = 25.0):
        from .cpu_ref import stitch_maps
        self.alpha = 1.0 / max(tau_frames, 1.0)
        W, H = 512, 256
        maps, _w, (t0, t1) = stitch_maps(rig, W, H)
        lat = (0.5 - (np.arange(H) + 0.5) / H) * 180
        band = (np.abs(t0 - t1) < np.radians(4)) & (lat > -45)[:, None]
        band &= (t0 < np.radians(rig.lenses[0].max_theta_deg)) & (t1 < np.radians(rig.lenses[1].max_theta_deg))
        ys, xs = np.nonzero(band)
        self.p = [np.stack([maps[i][0][ys, xs], maps[i][1][ys, xs]], -1) for i in range(2)]
        self.state = None

    def _sample(self, p010: np.ndarray, size: int, pts: np.ndarray):
        x = np.clip(np.round(pts[:, 0]).astype(int), 0, size - 1)
        y = np.clip(np.round(pts[:, 1]).astype(int), 0, size - 1)
        Y = (p010[:size * size].reshape(size, size)[y, x] >> 6).astype(np.float64)
        C = p010[size * size:].reshape(size // 2, size // 2, 2)[y // 2, x // 2] >> 6
        return Y, C.astype(np.float64)

    def update(self, f0: np.ndarray, f1: np.ndarray, size: int):
        y0, c0 = self._sample(f0, size, self.p[0])
        y1, c1 = self._sample(f1, size, self.p[1])
        ok = (y0 > 80) & (y0 < 1000) & (y1 > 80) & (y1 < 1000)
        if ok.sum() < 50:
            return self.current()
        # robust ratio of black-subtracted luma: split the correction between both lenses
        r = np.median((y1[ok] - 64) / np.maximum(y0[ok] - 64, 1))
        r = float(np.clip(r, 0.8, 1.25))
        du, dv = np.median(c1[ok] - c0[ok], axis=0) / 1023.98
        meas = np.array([np.sqrt(r), 1 / np.sqrt(r), du / 2, dv / 2])
        self.state = meas if self.state is None else self.state + self.alpha * (meas - self.state)
        return self.current()

    def current(self):
        if self.state is None:
            return (1.0, 1.0, 0.0, 0.0)
        g0, g1, du, dv = self.state
        return (g0, g1, du, dv)


# ---------------------------------------------------------------- convert
class Cancelled(Exception):
    """raised when a conversion is cancelled through the `cancel` event."""


def convert(src_path: str | Path, out_path: str | Path, opts: ConvertOptions, log=print,
            progress=None, cancel: threading.Event | None = None, sidecar: bool = False) -> dict:
    """progress(stage, done, total, fps) is called with stage 'calibrate' or 'render'.
    Setting `cancel` stops the job; the partial output is removed and Cancelled is raised."""
    from .gpu.stitcher import GPUStitcher, LensFrameParams

    progress = progress or (lambda *a: None)
    cancel = cancel or threading.Event()
    ffio.require_ffmpeg()
    src = open_source(src_path)
    fps = src.fps_num / src.fps_den
    progress("calibrate", 0, 1, 0.0)
    rig = calibrated_rig(src, opts, log)
    if cancel.is_set():
        raise Cancelled()
    bv_all, omega_all = frame_motion(src, rig, opts)

    first = int(round(opts.start_s * fps))
    last = src.n_frames if opts.duration_s is None else min(src.n_frames, first + int(round(opts.duration_s * fps)))
    n = last - first
    start_s = first / fps
    log(f"source: {src.path.name}  {src.lens_size}x{src.lens_size} x2 @ {fps:.3f} fps, "
        f"frames {first}..{last - 1} ({n})")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gpu = GPUStitcher(rig, opts.width, opts.height, src.lens_size, readout_s=opts.readout_s)
    log(f"gpu: {gpu.adapter_info['device']} ({gpu.adapter_info['backend_type']})")
    decs = [ffio.LensDecoder(src.path, s, src.lens_size, start_s, n) for s in src.video_streams]
    enc = ffio.Encoder(out_path, opts.width, opts.height, src.fps_num, src.fps_den, opts.encode,
                       src.path, src.audio_stream, start_s, n / fps)
    expo = ExposureMatcher(rig) if opts.exposure_match else None

    lens_q = [queue.Queue(maxsize=3), queue.Queue(maxsize=3)]
    out_q: queue.Queue = queue.Queue(maxsize=3)
    errors: list[BaseException] = []

    # reusable frame buffers: queue depth + one being filled + one being consumed
    in_pool = [[np.empty(decs[k].frame_elems, np.uint16) for _ in range(6)] for k in range(2)]
    out_pool = [np.empty(opts.width * opts.height * 3 // 2, np.uint16) for _ in range(6)]

    def reader(k):
        try:
            for j in range(n):
                f = decs[k].read(in_pool[k][j % len(in_pool[k])])
                if f is None:
                    break
                lens_q[k].put(f)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            lens_q[k].put(None)

    def writer():
        try:
            while (fr := out_q.get()) is not None:
                enc.write(fr)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def frames():
        while True:
            a, b = lens_q[0].get(), lens_q[1].get()
            if a is None or b is None:
                return
            yield a, b

    threads = [threading.Thread(target=reader, args=(0,), daemon=True),
               threading.Thread(target=reader, args=(1,), daemon=True),
               threading.Thread(target=writer, daemon=True)]
    for t in threads:
        t.start()

    t0 = time.time()
    done = 0
    lens_rot_inv = [l.rot.inv() for l in rig.lenses]
    try:
        for a, b in frames():
            i = first + done
            if expo is not None:
                g0, g1, du, dv = expo.update(a, b, src.lens_size)
            else:
                g0, g1, du, dv = 1.0, 1.0, 0.0, 0.0
            om = omega_all[i]
            lp = [LensFrameParams(gain_y=g0, off_u=du, off_v=dv, omega_lens=tuple(lens_rot_inv[0].apply(om))),
                  LensFrameParams(gain_y=g1, off_u=-du, off_v=-dv, omega_lens=tuple(lens_rot_inv[1].apply(om)))]
            gpu.upload(0, a)
            gpu.upload(1, b)
            out_q.put(gpu.render(bv_all[i], lp, out=out_pool[done % len(out_pool)]))
            done += 1
            if done % 10 == 0 or done == n:
                el = time.time() - t0
                progress("render", done, n, done / el)
            if done % 250 == 0 or done == n:
                el = time.time() - t0
                log(f"  {done}/{n} frames  {done / el:.1f} fps  eta {(n - done) / max(done / el, 1e-6):.0f}s")
            if errors or cancel.is_set():
                break
    except BaseException:
        cancel.set()  # KeyboardInterrupt / SIGTERM / GPU error: tear everything down
        raise
    finally:
        if cancel.is_set():
            enc.abort()
            for d in decs:
                d.close()
            for q in lens_q:  # unblock readers waiting on a full queue
                while not q.empty():
                    q.get_nowait()
        else:
            out_q.put(None)
            threads[2].join()
            rc = enc.close()
            for d in decs:
                d.close()
    if cancel.is_set():
        raise Cancelled()
    if errors:
        raise errors[0]
    if rc != 0:
        raise RuntimeError(f"encoder failed ({rc}): {' '.join(enc.cmd)}")
    if out_path.suffix.lower() in (".mp4", ".mov"):
        spherical.inject(out_path)
    el = time.time() - t0
    stats = {"frames": done, "seconds": el, "fps": done / el if el else 0.0, "output": str(out_path)}
    log(f"done: {done} frames in {el:.1f}s ({stats['fps']:.1f} fps) -> {out_path}")
    if sidecar:
        (out_path.with_suffix(out_path.suffix + ".json")).write_text(json.dumps(
            {"source": str(src.path), "stats": stats, "stabilize": opts.stab.__dict__,
             "encode": opts.encode.__dict__, "readout_s": opts.readout_s}, indent=2))
    return stats
