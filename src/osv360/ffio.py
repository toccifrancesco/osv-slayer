"""ffmpeg subprocess pipes: VA-API decode of the lens tracks, encode of the equirect."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VAAPI_DEVICE = "/dev/dri/renderD128"


def sysenv() -> dict:
    """environment for *system* tools (ffmpeg, ffprobe, notify-send).

    Inside a PyInstaller/AppImage bundle LD_LIBRARY_PATH points at the bundled
    libraries; system binaries must not load those, so restore the original value."""
    import os
    import sys
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        for k in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "PYTHONHOME", "PYTHONPATH"):
            env.pop(k, None)
    return env


def _grow_pipe(fileobj, size: int = 1 << 20) -> None:
    """enlarge a pipe buffer (Linux F_SETPIPE_SZ) to cut syscalls for 40-90 MB frames."""
    try:
        import fcntl
        fcntl.fcntl(fileobj.fileno(), 1031, size)  # F_SETPIPE_SZ
    except (OSError, ImportError, AttributeError):
        pass


def has_vaapi() -> bool:
    return Path(VAAPI_DEVICE).exists()


class LensDecoder:
    """Streams one lens track of the .OSV as raw P010 frames (uint16 arrays)."""

    def __init__(self, osv: str | Path, stream: int, size: int, start_s: float = 0.0,
                 frames: int | None = None, hwaccel: bool = True):
        self.size = size
        self.frame_elems = size * size * 3 // 2
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin"]
        if hwaccel and has_vaapi():
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi"]
        if start_s > 0:
            cmd += ["-ss", f"{start_s:.6f}"]
        cmd += ["-i", str(osv), "-map", f"0:{stream}"]
        if frames is not None:
            cmd += ["-frames:v", str(frames)]
        vf = "hwdownload,format=p010le" if hwaccel and has_vaapi() else "format=p010le"
        cmd += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "p010le", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0, env=sysenv())
        _grow_pipe(self.proc.stdout)

    def read(self, out: np.ndarray | None = None) -> np.ndarray | None:
        buf = np.empty(self.frame_elems, np.uint16) if out is None else out
        mv = memoryview(buf).cast("B")
        got = 0
        while got < len(mv):
            n = self.proc.stdout.readinto(mv[got:])
            if not n:
                return None
            got += n
        return buf

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


@dataclass
class EncodeSettings:
    codec: str = "hevc"  # hevc | prores | x265
    qp: int = 27  # hevc_vaapi constant QP: 27 ~ 350-450 Mbit/s, ~40-41 dB on busy 8K50 street footage
    gop: int = 25  # short GOP = smoother scrubbing in editors
    prores_profile: int = 3  # 0 proxy, 1 lt, 2 standard, 3 hq
    x265_crf: int = 16
    x265_preset: str = "medium"
    color_trc: str = "bt709"


class Encoder:
    """Receives P010 equirect frames on stdin, muxes audio from the source.

    hevc (VA-API): the elementary stream is first written to a temporary .hevc file and
    remuxed afterwards. ffmpeg's hevc_vaapi writes a global header whose PPS init_qp
    differs from the in-band PPS; any decoder trusting the MP4 `hvcC` box (mandatory for
    the widely compatible `hvc1` tag) would decode garbage. Remuxing from Annex-B makes
    ffmpeg rebuild `hvcC` from the real in-band parameter sets.
    """

    def __init__(self, out: str | Path, width: int, height: int, fps_num: int, fps_den: int,
                 settings: EncodeSettings, audio_src: str | Path | None = None,
                 audio_stream: int | None = None, start_s: float = 0.0, duration_s: float | None = None):
        s = settings
        self.out = Path(out)
        self.rate = f"{fps_num}/{fps_den}"
        self.audio = (audio_src, audio_stream, start_s, duration_s)
        self.settings = s
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostdin"]
        if s.codec == "hevc":
            cmd += ["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "p010le", "-s", f"{width}x{height}", "-framerate", self.rate,
                "-i", "pipe:0"]
        tag = f"setparams=range=tv:color_primaries=bt709:color_trc={s.color_trc}:colorspace=bt709"
        self.tmp = None
        if s.codec == "hevc":
            self.tmp = self.out.with_name(self.out.name + ".tmp.hevc")
            cmd += ["-map", "0:v", "-vf", f"{tag},format=p010le,hwupload", "-c:v", "hevc_vaapi",
                    "-profile:v", "main10", "-rc_mode", "CQP", "-qp", str(s.qp), "-g", str(s.gop),
                    "-f", "hevc", str(self.tmp)]
        else:
            cmd += self._audio_inputs()
            if s.codec == "x265":
                cmd += ["-vf", tag, "-c:v", "libx265", "-preset", s.x265_preset, "-crf", str(s.x265_crf),
                        "-pix_fmt", "yuv420p10le",
                        "-x265-params", f"keyint={s.gop}:min-keyint=1:log-level=error", "-tag:v", "hvc1"]
            elif s.codec == "prores":
                cmd += ["-vf", tag, "-c:v", "prores_ks", "-profile:v", str(s.prores_profile),
                        "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
            else:
                raise ValueError(f"unknown codec {s.codec}")
            cmd += self._mux_flags() + [str(self.out)]
        self.cmd = cmd
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0, env=sysenv())
        _grow_pipe(self.proc.stdin)

    def _audio_inputs(self, video_input: int = 0) -> list[str]:
        src, stream, start_s, duration_s = self.audio
        if src is None or stream is None:
            return ["-map", f"{video_input}:v"]
        a = []
        if start_s > 0:
            a += ["-ss", f"{start_s:.6f}"]
        if duration_s is not None:
            a += ["-t", f"{duration_s:.6f}"]
        return a + ["-i", str(src), "-map", f"{video_input}:v", "-map", f"{video_input + 1}:{stream}",
                    "-c:a", "copy"]

    def _mux_flags(self) -> list[str]:
        s = self.settings
        flags = ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
                 "-color_trc", s.color_trc]
        if self.out.suffix.lower() in (".mp4", ".mov"):
            flags += ["-movflags", "+write_colr"]
        return flags

    def write(self, frame: np.ndarray) -> None:
        # unbuffered pipe: write() may be partial, loop until the whole frame is sent
        mv = memoryview(frame).cast("B")
        while len(mv):
            n = self.proc.stdin.write(mv)
            mv = mv[n:]

    def abort(self) -> None:
        """stop immediately and remove partial files."""
        try:
            self.proc.kill()
        finally:
            self.proc.wait()
            for f in (self.tmp, self.out):
                if f is not None:
                    Path(f).unlink(missing_ok=True)

    def close(self) -> int:
        self.proc.stdin.close()
        rc = self.proc.wait()
        if rc != 0 or self.tmp is None:
            return rc
        mux = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostdin", "-framerate", self.rate,
               "-i", str(self.tmp)] + self._audio_inputs() + ["-c:v", "copy", "-tag:v", "hvc1"] \
            + self._mux_flags() + [str(self.out)]
        rc = subprocess.run(mux, env=sysenv()).returncode
        if rc == 0:
            self.tmp.unlink(missing_ok=True)
        return rc


def ffprobe_streams(path: str | Path) -> list[dict]:
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True, env=sysenv()).stdout
    return json.loads(out)["streams"]


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found in PATH")
