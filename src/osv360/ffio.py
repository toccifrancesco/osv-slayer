"""ffmpeg subprocess pipes: hardware decode of the lens tracks, hardware encode of the equirect.

Platform backends
  decode: Linux VA-API (explicit), elsewhere `-hwaccel auto` (D3D11VA/DXVA2, VideoToolbox, ...)
  encode: first working of  Linux: hevc_vaapi, hevc_nvenc, hevc_qsv
                            Windows: hevc_nvenc, hevc_amf, hevc_qsv
                            macOS: hevc_videotoolbox
          then libx265 (CPU) as the always-available fallback.
Every hardware encoder is verified with a 2-frame test encode at the real output size
before use, so an encoder that is compiled in but unusable on this machine is skipped.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VAAPI_DEVICE = "/dev/dri/renderD128"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console flashes from the GUI


def sysenv() -> dict:
    """environment for *system* tools (ffmpeg, ffprobe, notify-send).

    Inside a PyInstaller/AppImage bundle LD_LIBRARY_PATH points at the bundled
    libraries; system binaries must not load those, so restore the original value."""
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


@functools.cache
def tool(name: str) -> str:
    """absolute path of ffmpeg / ffprobe.

    Search order: $OSV360_FFMPEG_DIR, an `ffmpeg/` folder next to the app (for bundled
    builds), PATH, then well-known install locations (GUI apps on macOS do not inherit
    the shell PATH, so Homebrew's folders are checked explicitly)."""
    exe = name + (".exe" if IS_WINDOWS else "")
    dirs: list[Path] = []
    if os.environ.get("OSV360_FFMPEG_DIR"):
        dirs.append(Path(os.environ["OSV360_FFMPEG_DIR"]))
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        dirs += [here / "ffmpeg", here.parent / "Resources" / "ffmpeg"]
    for d in dirs:
        if (d / exe).is_file():
            return str(d / exe)
    found = shutil.which(name)
    if found:
        return found
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"]
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        extra = [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                 os.path.join(local, "Microsoft", "WinGet", "Links"),
                 os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims"),
                 r"C:\ProgramData\chocolatey\bin"]
    for d in extra:
        p = Path(d) / exe
        if p.is_file():
            return str(p)
    return name  # let the OS report it missing


def run_tool(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run([tool(args[0]), *args[1:]], env=sysenv(), creationflags=NO_WINDOW, **kw)


def popen_tool(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen([tool(args[0]), *args[1:]], env=sysenv(), creationflags=NO_WINDOW, **kw)


def _grow_pipe(fileobj, size: int = 1 << 20) -> None:
    """enlarge a pipe buffer (Linux F_SETPIPE_SZ) to cut syscalls for 40-90 MB frames."""
    try:
        import fcntl
        fcntl.fcntl(fileobj.fileno(), 1031, size)  # F_SETPIPE_SZ
    except (OSError, ImportError, AttributeError):
        pass


def has_vaapi() -> bool:
    return IS_LINUX and Path(VAAPI_DEVICE).exists()


class LensDecoder:
    """Streams one lens track of the .OSV as raw P010 frames (uint16 arrays)."""

    def __init__(self, osv: str | Path, stream: int, size: int, start_s: float = 0.0,
                 frames: int | None = None, hwaccel: bool = True):
        self.size = size
        self.frame_elems = size * size * 3 // 2
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin"]
        vaapi = hwaccel and has_vaapi()
        if vaapi:
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi"]
        elif hwaccel:
            cmd += ["-hwaccel", "auto"]  # frames are downloaded to system memory automatically
        if start_s > 0:
            cmd += ["-ss", f"{start_s:.6f}"]
        cmd += ["-i", str(osv), "-map", f"0:{stream}"]
        if frames is not None:
            cmd += ["-frames:v", str(frames)]
        vf = "hwdownload,format=p010le" if vaapi else "format=p010le"
        cmd += ["-vf", vf, "-f", "rawvideo", "-pix_fmt", "p010le", "pipe:1"]
        self.proc = popen_tool(cmd, stdout=subprocess.PIPE, bufsize=0)
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


# ---------------------------------------------------------------------- encoders
@dataclass
class EncodeSettings:
    codec: str = "hevc"  # hevc (hardware, auto-selected; CPU x265 fallback) | x265 | prores
    qp: int = 27  # constant QP: 27 ~ 350-450 Mbit/s, ~40-41 dB on busy 8K50 street footage
    gop: int = 25  # short GOP = smoother scrubbing in editors
    prores_profile: int = 3  # 0 proxy, 1 lt, 2 standard, 3 hq
    x265_preset: str = "medium"
    color_trc: str = "bt709"
    encoder: str = "auto"  # auto | vaapi | nvenc | amf | qsv | videotoolbox | x265


HW_PRIORITY = {
    "linux": ["vaapi", "nvenc", "qsv"],
    "win32": ["nvenc", "amf", "qsv"],
    "darwin": ["videotoolbox"],
}


def qp_to_mbps(qp: int) -> float:
    """bitrate equivalent of a QP for encoders without constant-QP mode (measured on 8K50)."""
    return float(np.interp(qp, [18, 22, 27, 30, 33, 38], [900, 650, 420, 300, 200, 110]))


def x265_crf(qp: int) -> int:
    return max(0, qp - 9)  # x265 CRF ~ hardware QP - 9 at similar quality (QP 27 -> CRF 18)


def video_args(encoder: str, s: EncodeSettings, tag: str) -> tuple[list[str], list[str]]:
    """(args before the input, video encoding args) for one encoder backend."""
    q, g = str(s.qp), str(s.gop)
    if encoder == "vaapi":
        return (["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va"],
                ["-vf", f"{tag},format=p010le,hwupload", "-c:v", "hevc_vaapi", "-profile:v", "main10",
                 "-rc_mode", "CQP", "-qp", q, "-g", g])
    if encoder == "nvenc":
        return ([], ["-vf", tag, "-pix_fmt", "p010le", "-c:v", "hevc_nvenc", "-profile:v", "main10",
                     "-preset", "p5", "-rc", "constqp", "-qp", q, "-g", g])
    if encoder == "amf":
        return ([], ["-vf", tag, "-pix_fmt", "p010le", "-c:v", "hevc_amf", "-profile:v", "main10",
                     "-quality", "quality", "-rc", "cqp", "-qp_i", q, "-qp_p", q, "-g", g])
    if encoder == "qsv":
        return ([], ["-vf", tag, "-pix_fmt", "p010le", "-c:v", "hevc_qsv", "-profile:v", "main10",
                     "-global_quality", q, "-g", g])
    if encoder == "videotoolbox":
        mb = qp_to_mbps(s.qp)
        return ([], ["-vf", tag, "-pix_fmt", "p010le", "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
                     "-b:v", f"{mb:.0f}M", "-maxrate", f"{mb * 1.5:.0f}M", "-bufsize", f"{mb * 2:.0f}M",
                     "-g", g, "-allow_sw", "0"])
    if encoder == "x265":
        return ([], ["-vf", tag, "-c:v", "libx265", "-preset", s.x265_preset, "-crf", str(x265_crf(s.qp)),
                     "-pix_fmt", "yuv420p10le", "-x265-params", f"keyint={s.gop}:min-keyint=1:log-level=error"])
    raise ValueError(f"unknown encoder {encoder}")


@functools.cache
def _compiled_encoders() -> str:
    try:
        return run_tool(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except OSError:
        return ""


@functools.cache
def encoder_works(encoder: str, width: int, height: int) -> bool:
    """2-frame test encode at the real output size (catches missing drivers / size limits)."""
    if encoder == "x265":
        return "libx265" in _compiled_encoders()
    if f"hevc_{encoder}" not in _compiled_encoders():
        return False
    if encoder == "vaapi" and not has_vaapi():
        return False
    s = EncodeSettings()
    pre, vid = video_args(encoder, s, "setparams=range=tv")
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", *pre, "-f", "lavfi",
           "-i", f"color=c=gray:s={width}x{height}:r=50", *vid, "-frames:v", "2", "-f", "null", "-"]
    try:
        return run_tool(cmd, capture_output=True, timeout=90).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_encoder(settings: EncodeSettings, width: int, height: int) -> str:
    """resolve settings.encoder ('auto' -> first working hardware encoder, else x265)."""
    if settings.codec == "x265":
        return "x265"
    if settings.encoder != "auto":
        if not encoder_works(settings.encoder, width, height):
            raise RuntimeError(f"the '{settings.encoder}' encoder is not available on this computer at "
                               f"{width}x{height} (choose 'auto' to pick a working one)")
        return settings.encoder
    for enc in HW_PRIORITY.get(sys.platform, []):
        if encoder_works(enc, width, height):
            return enc
    return "x265"


class Encoder:
    """Receives P010 equirect frames on stdin, muxes audio from the source.

    HEVC is first written as an Annex-B elementary stream to a temporary file and remuxed
    afterwards with the `hvc1` tag. ffmpeg's hevc_vaapi writes a global header whose PPS
    init_qp differs from the in-band PPS; decoders trusting the MP4 `hvcC` box (mandatory
    for `hvc1`) would decode garbage. Remuxing from Annex-B makes ffmpeg rebuild `hvcC`
    from the real in-band parameter sets; doing it for every HEVC backend keeps one path.
    """

    def __init__(self, out: str | Path, width: int, height: int, fps_num: int, fps_den: int,
                 settings: EncodeSettings, audio_src: str | Path | None = None,
                 audio_stream: int | None = None, start_s: float = 0.0, duration_s: float | None = None):
        s = settings
        self.out = Path(out)
        self.rate = f"{fps_num}/{fps_den}"
        self.audio = (audio_src, audio_stream, start_s, duration_s)
        self.settings = s
        tag = f"setparams=range=tv:color_primaries=bt709:color_trc={s.color_trc}:colorspace=bt709"
        self.tmp = None
        if s.codec == "prores":
            self.backend = "prores"
            pre, vid = [], ["-vf", tag, "-c:v", "prores_ks", "-profile:v", str(s.prores_profile),
                            "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
        else:
            self.backend = select_encoder(s, width, height)
            pre, vid = video_args(self.backend, s, tag)
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-nostdin", *pre,
               "-f", "rawvideo", "-pix_fmt", "p010le", "-s", f"{width}x{height}", "-framerate", self.rate,
               "-i", "pipe:0"]
        if self.backend == "prores":
            cmd += self._audio_inputs() + vid + self._mux_flags() + [str(self.out)]
        else:
            self.tmp = self.out.with_name(self.out.name + ".tmp.hevc")
            cmd += ["-map", "0:v", *vid, "-f", "hevc", str(self.tmp)]
        self.cmd = cmd
        self.proc = popen_tool(cmd, stdin=subprocess.PIPE, bufsize=0)
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
        rc = run_tool(mux).returncode
        if rc == 0:
            self.tmp.unlink(missing_ok=True)
        return rc


def ffprobe_streams(path: str | Path) -> list[dict]:
    import json
    out = run_tool(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                   check=True, capture_output=True, text=True).stdout
    return json.loads(out)["streams"]


def require_ffmpeg() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if not Path(tool(name)).is_file() and shutil.which(tool(name)) is None:
            raise SystemExit(f"{name} not found: install ffmpeg (see README) or set OSV360_FFMPEG_DIR")
