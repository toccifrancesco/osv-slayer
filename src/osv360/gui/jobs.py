"""Conversion queue: one `osv360 convert --progress-json` subprocess at a time.

Running each file in its own process keeps the GUI responsive, isolates GPU /
ffmpeg failures, and makes Stop a clean SIGTERM (the CLI removes partial output).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, Signal

from .settings import AppSettings


def self_command() -> list[str]:
    """argv prefix that runs this same osv360 (frozen AppImage binary or python -m)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "osv360"]


@dataclass
class Job:
    path: Path
    output: Path | None = None
    state: str = "queued"  # queued | running | done | failed | cancelled | skipped
    stage: str = ""  # calibrate | render
    done: int = 0
    total: int = 0
    fps: float = 0.0
    message: str = ""
    duration_s: float = 0.0
    size_bytes: int = 0
    started: float = 0.0
    elapsed: float = 0.0
    log: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        if self.state == "done":
            return 1.0
        if self.stage == "render" and self.total:
            return self.done / self.total
        return 0.0

    @property
    def eta_s(self) -> float | None:
        if self.stage == "render" and self.fps > 0 and self.total:
            return (self.total - self.done) / self.fps
        return None


class Runner(QObject):
    job_changed = Signal(object)  # Job
    log_line = Signal(str)
    queue_started = Signal()
    queue_finished = Signal(int, int)  # done, failed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[Job] = []
        self.proc: QProcess | None = None
        self.current: Job | None = None
        self.settings: AppSettings | None = None
        self._stopping = False
        self._buf = b""
        self._counts = [0, 0]

    @property
    def running(self) -> bool:
        return self.proc is not None

    def start(self, jobs: list[Job], settings: AppSettings) -> None:
        if self.running:
            return
        self.jobs, self.settings = jobs, settings
        self._stopping = False
        self._counts = [0, 0]
        self.queue_started.emit()
        self._next()

    def stop(self) -> None:
        self._stopping = True
        if self.proc is not None:
            self.proc.terminate()  # SIGTERM -> the CLI cancels and cleans up
            QProcessKiller(self.proc).arm(8000)

    # ------------------------------------------------------------------ internals
    def _next(self) -> None:
        job = next((j for j in self.jobs if j.state == "queued"), None)
        if job is None or self._stopping:
            self.proc = None
            self.current = None
            self.queue_finished.emit(*self._counts)
            return
        s = self.settings
        job.output = s.output_for(job.path)
        job.output.parent.mkdir(parents=True, exist_ok=True)
        job.state, job.stage, job.done, job.total, job.message = "running", "calibrate", 0, 0, "Preparing…"
        job.started = time.time()
        self.current = job
        args = self_command()[1:] + ["convert", str(job.path), "-o", str(job.output), "--progress-json"] \
            + s.cli_args()
        self.proc = QProcess(self)
        self.proc.setProgram(self_command()[0])
        self.proc.setArguments(args)
        self.proc.setProcessChannelMode(QProcess.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._stdout)
        self.proc.readyReadStandardError.connect(self._stderr)
        self.proc.finished.connect(self._finished)
        self._buf = b""
        self.log_line.emit(f"$ osv360 {' '.join(args[args.index('convert'):])}")
        self.proc.start()
        self.job_changed.emit(job)

    def _stderr(self) -> None:
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        for line in data.splitlines():
            if line.strip():
                self.log_line.emit(line)
                if self.current:
                    self.current.log.append(line)

    def _stdout(self) -> None:
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        job = self.current
        for raw in lines:
            try:
                ev = json.loads(raw)
            except ValueError:
                if raw.strip():
                    self.log_line.emit(raw.decode("utf-8", "replace"))
                continue
            kind = ev.get("event")
            if kind == "log":
                self.log_line.emit(ev["msg"])
                job.log.append(ev["msg"])
                if ev["msg"].startswith("calibration: refining"):
                    job.message = "Calibrating lenses (first run on this clip)…"
            elif kind == "progress":
                job.stage, job.done, job.total, job.fps = ev["stage"], ev["done"], ev["total"], ev["fps"]
                if job.stage == "calibrate" and not job.message.startswith("Calibrating"):
                    job.message = "Reading metadata…"
            elif kind == "done":
                job.state, job.message = "done", f"Done in {_fmt_time(ev.get('seconds', 0))} · {ev.get('fps', 0):.1f} fps"
            elif kind == "error":
                job.state, job.message = "failed", ev.get("msg", "error")
            elif kind == "cancelled":
                job.state, job.message = "cancelled", "Cancelled"
            elif kind == "skipped":
                job.state, job.message = "skipped", "Skipped: output exists (enable overwrite in Settings)"
            job.elapsed = time.time() - job.started
            self.job_changed.emit(job)

    def _finished(self, code: int, _status) -> None:
        job = self.current
        if job is not None and job.state == "running":
            if self._stopping:
                job.state, job.message = "cancelled", "Cancelled"
            else:
                job.state = "failed"
                job.message = job.log[-1] if job.log else f"converter exited with code {code}"
        if job is not None:
            if job.state == "done":
                self._counts[0] += 1
            elif job.state == "failed":
                self._counts[1] += 1
            self.job_changed.emit(job)
        self.proc.deleteLater()
        self.proc = None
        self._next()


class QProcessKiller(QObject):
    """SIGKILL a process that ignores SIGTERM for too long."""

    def __init__(self, proc: QProcess):
        super().__init__(proc)
        self.proc = proc

    def arm(self, ms: int) -> None:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(ms, self._kill)

    def _kill(self) -> None:
        try:
            if self.proc.state() != QProcess.NotRunning:
                self.proc.kill()
        except RuntimeError:  # already deleted
            pass


def _fmt_time(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


fmt_time = _fmt_time


# ---------------------------------------------------------------------- thumbnails / probing
class _ProbeSignals(QObject):
    ready = Signal(str, bytes, float, str)  # path, png bytes, duration, error


class ProbeTask(QRunnable):
    """duration from the MP4 header + the camera's embedded equirect thumbnail."""

    def __init__(self, path: Path, signals: _ProbeSignals):
        super().__init__()
        self.path, self.signals = path, signals

    def run(self) -> None:
        dur, png, err = 0.0, b"", ""
        try:
            from ..ffio import sysenv
            from ..mp4 import MP4
            with MP4(self.path) as m:
                v = [t for t in m.tracks if t.handler == "vide" and t.codec in ("hvc1", "hev1")]
                if len(v) != 2:
                    err = "not a dual-lens Osmo 360 file"
                if v:
                    dur = v[0].duration / v[0].timescale
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_name",
                                    "-of", "json", str(self.path)], capture_output=True, text=True, timeout=20,
                                   env=sysenv())
            streams = json.loads(probe.stdout or "{}").get("streams", [])
            mj = [s["index"] for s in streams if s.get("codec_name") in ("mjpeg", "png")]
            if mj:
                png = subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.path), "-map", f"0:{mj[0]}",
                                      "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2pipe",
                                      "-vcodec", "png", "-"], capture_output=True, timeout=30, env=sysenv()).stdout
        except Exception as e:  # noqa: BLE001
            err = err or str(e)
        self.signals.ready.emit(str(self.path), png, dur, err)


class Prober(QObject):
    ready = Signal(str, bytes, float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _ProbeSignals()
        self._signals.ready.connect(self.ready)
        self.pool = QThreadPool.globalInstance()

    def probe(self, path: Path) -> None:
        self.pool.start(ProbeTask(path, self._signals))
