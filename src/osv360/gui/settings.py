"""Persistent application settings (QSettings) and their mapping to CLI arguments."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PySide6.QtCore import QSettings

QUALITY_PRESETS = {  # label -> HEVC QP
    "Maximum (QP 22, ~650 Mbit/s)": 22,
    "High (QP 27, ~400 Mbit/s)": 27,
    "Balanced (QP 30, ~300 Mbit/s)": 30,
    "Compact (QP 33, ~200 Mbit/s)": 33,
}
RESOLUTIONS = {  # label -> scale of 7680x3840
    "8K  7680×3840": 1.0,
    "6K  5760×2880": 0.75,
    "4K  3840×1920": 0.5,
}
STAB_MODES = {
    "horizon": "Horizon lock + smooth heading",
    "lock": "Full lock (fixed direction)",
    "smooth": "Smooth all axes",
    "none": "Off (raw camera orientation)",
}
CODECS = {
    "hevc": "HEVC 10-bit MP4 (GPU, recommended)",
    "prores": "ProRes 422 MOV (large, intra-frame)",
    "x265": "HEVC 10-bit MP4 (CPU x265, slow)",
}
PRORES_PROFILES = {"Proxy": 0, "LT": 1, "422": 2, "HQ": 3}
ENCODERS = {
    "auto": "Automatic (best available)",
    "vaapi": "VA-API (Linux: AMD / Intel)",
    "nvenc": "NVENC (NVIDIA)",
    "amf": "AMF (AMD, Windows)",
    "qsv": "Quick Sync (Intel)",
    "videotoolbox": "VideoToolbox (macOS)",
    "x265": "CPU (x265, slow)",
}


def _qsettings() -> QSettings:
    """$OSV360_CONFIG=/path/file.ini overrides the location (portable mode, tests)."""
    import os
    if os.environ.get("OSV360_CONFIG"):
        return QSettings(os.environ["OSV360_CONFIG"], QSettings.IniFormat)
    return QSettings("osv360", "osv360")


def default_output_dir() -> str:
    from PySide6.QtCore import QStandardPaths
    movies = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)  # ~/Videos, ~/Movies, ...
    base = Path(movies) if movies else Path.home()
    return str(base / "osv360")


@dataclass
class AppSettings:
    # output
    output_dir: str = ""  # "" -> next to each source file
    use_source_dir: bool = False
    name_template: str = "{name}_{mode}"
    overwrite: bool = False
    open_folder_when_done: bool = False
    notify_when_done: bool = True
    # stabilization
    stabilize: str = "horizon"
    yaw_smooth: float = 1.0
    smooth: float = 0.5
    # quality
    codec: str = "hevc"
    qp: int = 27
    prores_profile: int = 3
    scale: float = 1.0
    gop: int = 25
    encoder: str = "auto"
    # advanced
    blend: float = 6.0
    exposure_match: bool = True
    readout_ms: float = 0.0
    recalibrate: bool = False

    def __post_init__(self):
        if not self.output_dir:
            self.output_dir = default_output_dir()

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls) -> "AppSettings":
        qs = _qsettings()
        s = cls()
        for f in fields(cls):
            if qs.contains(f.name):
                v = qs.value(f.name)
                t = type(getattr(s, f.name))
                try:
                    if t is bool:
                        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
                    else:
                        v = t(v)
                    setattr(s, f.name, v)
                except (TypeError, ValueError):
                    pass
        return s

    def save(self) -> None:
        qs = _qsettings()
        for k, v in asdict(self).items():
            qs.setValue(k, v)
        qs.sync()

    # ------------------------------------------------------------------ cli
    def output_for(self, src: str | Path) -> Path:
        from ..cli import output_path
        folder = Path(src).parent if self.use_source_dir else Path(self.output_dir)
        return output_path(src, folder, self.name_template, self.stabilize, self.codec)

    def cli_args(self) -> list[str]:
        a = ["--stabilize", self.stabilize, "--yaw-smooth", f"{self.yaw_smooth:g}", "--smooth", f"{self.smooth:g}",
             "--codec", self.codec, "--qp", str(self.qp), "--prores-profile", str(self.prores_profile),
             "--scale", f"{self.scale:g}", "--gop", str(self.gop), "--blend", f"{self.blend:g}",
             "--readout-ms", f"{self.readout_ms:g}", "--encoder", self.encoder]
        if not self.exposure_match:
            a.append("--no-exposure-match")
        if self.recalibrate:
            a.append("--recalibrate")
        if self.overwrite:
            a.append("--overwrite")
        return a

    def summary(self) -> str:
        res = next((k.split()[0] for k, v in RESOLUTIONS.items() if abs(v - self.scale) < 1e-6), f"{self.scale:g}×")
        if self.codec == "prores":
            q = "ProRes " + next((k for k, v in PRORES_PROFILES.items() if v == self.prores_profile), "?")
        else:
            q = ("x265" if self.codec == "x265" else "HEVC") + f" QP {self.qp}"
        return f"{res} · {q} · {STAB_MODES[self.stabilize].split(' (')[0]}"
