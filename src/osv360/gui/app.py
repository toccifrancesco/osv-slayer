"""osv360 desktop app: batch-convert DJI Osmo 360 .OSV files with one click."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSpinBox, QStackedWidget, QTabWidget,
                               QVBoxLayout, QWidget)

from . import theme
from .jobs import Job, Prober, Runner, fmt_time, self_command
from .settings import CODECS, ENCODERS, PRORES_PROFILES, QUALITY_PRESETS, RESOLUTIONS, STAB_MODES, AppSettings

ASSETS = Path(__file__).parent / "assets"
OSV_EXT = (".osv",)


def app_icon() -> QIcon:
    return QIcon(str(ASSETS / "osv360.svg"))


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return ""


def rounded(pix: QPixmap, radius: int = 8) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pix.width(), pix.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


# ====================================================================== widgets
class DropZone(QFrame):
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("DropZone")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(96, 96))
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("background: transparent;")
        t = QLabel("Drop your Osmo 360 .OSV files here")
        t.setObjectName("DropTitle")
        t.setAlignment(Qt.AlignCenter)
        h = QLabel("or click to browse · folders work too")
        h.setObjectName("DropHint")
        h.setAlignment(Qt.AlignCenter)
        for w in (icon, t, h):
            lay.addWidget(w)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def set_hover(self, on: bool):
        self.setProperty("hover", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class FileRow(QFrame):
    remove_requested = Signal(object)
    reveal_requested = Signal(object)

    STATE_TEXT = {"queued": "Queued", "running": "Converting", "done": "Done", "failed": "Failed",
                  "cancelled": "Cancelled", "skipped": "Skipped"}
    STATE_CHIP = {"queued": "", "running": "run", "done": "ok", "failed": "err", "cancelled": "warn",
                  "skipped": "warn"}

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.setObjectName("Row")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(14)
        self.thumb = QLabel()
        self.thumb.setObjectName("Thumb")
        self.thumb.setFixedSize(176, 88)
        self.thumb.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.thumb)
        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.title = QLabel(job.path.name)
        self.title.setObjectName("RowTitle")
        self.meta = QLabel(str(job.path.parent))
        self.meta.setObjectName("RowMeta")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("RowStatus")
        for w in (self.title, self.meta):
            mid.addWidget(w)
        mid.addStretch(1)
        mid.addWidget(self.bar)
        mid.addWidget(self.status)
        lay.addLayout(mid, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        self.chip = QLabel("Queued")
        self.chip.setObjectName("Chip")
        self.chip.setAlignment(Qt.AlignCenter)
        btns = QHBoxLayout()
        self.reveal = QPushButton("📂")
        self.reveal.setObjectName("Icon")
        self.reveal.setToolTip("Show the converted file")
        self.reveal.clicked.connect(lambda: self.reveal_requested.emit(self.job))
        self.remove = QPushButton("✕")
        self.remove.setObjectName("Icon")
        self.remove.setToolTip("Remove from the list")
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self.job))
        btns.addStretch(1)
        btns.addWidget(self.reveal)
        btns.addWidget(self.remove)
        right.addWidget(self.chip, 0, Qt.AlignRight)
        right.addStretch(1)
        right.addLayout(btns)
        lay.addLayout(right)
        self.refresh()

    def set_thumbnail(self, png: bytes):
        pix = QPixmap()
        if png and pix.loadFromData(png):
            pix = pix.scaled(self.thumb.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self.thumb.setPixmap(rounded(pix, 8))
        else:
            self.thumb.setText("360°")

    def refresh(self):
        j = self.job
        bits = []
        if j.duration_s:
            bits.append(fmt_time(j.duration_s))
        if j.size_bytes:
            bits.append(human_size(j.size_bytes))
        bits.append(str(j.path.parent))
        self.meta.setText("  ·  ".join(bits))
        self.chip.setText(self.STATE_TEXT.get(j.state, j.state))
        self.chip.setProperty("state", self.STATE_CHIP.get(j.state, ""))
        self.setProperty("state", j.state)
        for w in (self.chip, self):
            w.style().unpolish(w)
            w.style().polish(w)
        self.bar.setVisible(j.state in ("running", "done"))
        self.bar.setValue(int(j.fraction * 1000))
        if j.state == "running" and j.stage == "render":
            eta = j.eta_s
            self.chip.setText(f"{j.fraction * 100:.0f}%")
            self.status.setText(f"Rendering {j.done}/{j.total} frames · {j.fps:.1f} fps"
                                + (f" · {fmt_time(eta)} left" if eta is not None else ""))
        elif j.state == "running":
            self.bar.setRange(0, 0)  # busy indicator while calibrating
            self.status.setText(j.message)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(j.fraction * 1000))
            self.status.setText(j.message if j.state != "queued" else
                                ("→ " + j.output.name if j.output else ""))
        if j.state == "running" and j.stage == "render":
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(j.fraction * 1000))
        self.reveal.setVisible(j.state == "done")
        self.remove.setEnabled(j.state != "running")


class SettingsDialog(QDialog):
    def __init__(self, s: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("osv360 — Settings")
        self.setMinimumWidth(560)
        self.s = s
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        def page():
            w = QWidget()
            w.setObjectName("TabPage")
            f = QFormLayout(w)
            f.setContentsMargins(18, 18, 18, 18)
            f.setHorizontalSpacing(18)
            f.setVerticalSpacing(12)
            f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return w, f

        def hint(text):
            l = QLabel(text)
            l.setObjectName("Hint")
            l.setWordWrap(True)
            return l

        # --- output
        w, f = page()
        self.out_dir = QLineEdit(s.output_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.out_dir, 1)
        row.addWidget(browse)
        self.same_dir = QCheckBox("Save next to each source file instead")
        self.same_dir.setChecked(s.use_source_dir)
        self.same_dir.toggled.connect(lambda on: (self.out_dir.setEnabled(not on), browse.setEnabled(not on)))
        self.same_dir.toggled.emit(s.use_source_dir)
        self.template = QLineEdit(s.name_template)
        self.overwrite = QCheckBox("Overwrite existing files")
        self.overwrite.setChecked(s.overwrite)
        self.open_done = QCheckBox("Open the output folder when the batch finishes")
        self.open_done.setChecked(s.open_folder_when_done)
        self.notify = QCheckBox("Desktop notification when the batch finishes")
        self.notify.setChecked(s.notify_when_done)
        f.addRow("Output folder", row)
        f.addRow("", self.same_dir)
        f.addRow("File name", self.template)
        f.addRow("", hint("Placeholders: {name} source name, {mode} stabilization, {codec}."))
        f.addRow("", self.overwrite)
        f.addRow("", self.open_done)
        f.addRow("", self.notify)
        tabs.addTab(w, "Output")

        # --- stabilization
        w, f = page()
        self.mode = QComboBox()
        for k, v in STAB_MODES.items():
            self.mode.addItem(v, k)
        self.mode.setCurrentIndex(list(STAB_MODES).index(s.stabilize))
        self.yaw = QDoubleSpinBox()
        self.yaw.setRange(0.1, 10.0)
        self.yaw.setSingleStep(0.1)
        self.yaw.setSuffix(" s")
        self.yaw.setValue(s.yaw_smooth)
        self.smooth = QDoubleSpinBox()
        self.smooth.setRange(0.05, 5.0)
        self.smooth.setSingleStep(0.05)
        self.smooth.setSuffix(" s")
        self.smooth.setValue(s.smooth)
        f.addRow("Mode", self.mode)
        f.addRow("", hint("Horizon lock removes roll and pitch completely using the camera's 1 kHz IMU, while the "
                          "view follows your direction of travel. Full lock always faces the starting direction."))
        f.addRow("Heading smoothing", self.yaw)
        f.addRow("", hint("Horizon mode: how lazily the view follows turns (higher = smoother)."))
        f.addRow("Rotation smoothing", self.smooth)
        f.addRow("", hint("Smooth mode only: low-pass applied to all three axes."))
        tabs.addTab(w, "Stabilization")

        # --- quality
        w, f = page()
        self.codec = QComboBox()
        for k, v in CODECS.items():
            self.codec.addItem(v, k)
        self.codec.setCurrentIndex(list(CODECS).index(s.codec) if s.codec in CODECS else 0)
        self.quality = QComboBox()
        for k, v in QUALITY_PRESETS.items():
            self.quality.addItem(k, v)
        self.quality.addItem("Custom", -1)
        idx = list(QUALITY_PRESETS.values()).index(s.qp) if s.qp in QUALITY_PRESETS.values() else len(QUALITY_PRESETS)
        self.quality.setCurrentIndex(idx)
        self.qp = QSpinBox()
        self.qp.setRange(10, 45)
        self.qp.setValue(s.qp)
        self.quality.currentIndexChanged.connect(self._quality_changed)
        self.qp.valueChanged.connect(self._qp_changed)
        self.prores = QComboBox()
        for k, v in PRORES_PROFILES.items():
            self.prores.addItem(k, v)
        self.prores.setCurrentIndex(list(PRORES_PROFILES.values()).index(s.prores_profile))
        self.res = QComboBox()
        for k, v in RESOLUTIONS.items():
            self.res.addItem(k, v)
        self.res.setCurrentIndex(next((i for i, v in enumerate(RESOLUTIONS.values()) if abs(v - s.scale) < 1e-6), 0))
        self.gop = QSpinBox()
        self.gop.setRange(1, 300)
        self.gop.setSuffix(" frames")
        self.gop.setValue(s.gop)
        f.addRow("Codec", self.codec)
        f.addRow("Quality", self.quality)
        f.addRow("HEVC QP", self.qp)
        f.addRow("ProRes profile", self.prores)
        f.addRow("Resolution", self.res)
        f.addRow("Keyframe interval", self.gop)
        f.addRow("", hint("HEVC 10-bit is decoded by Kdenlive and FilmCraft (hardware-accelerated). ProRes is "
                          "smoother to scrub but ~10× larger. Shorter keyframe intervals scrub faster."))
        self.codec.currentIndexChanged.connect(self._codec_changed)
        self._codec_changed()
        tabs.addTab(w, "Quality")

        # --- advanced
        w, f = page()
        self.blend = QDoubleSpinBox()
        self.blend.setRange(1.0, 15.0)
        self.blend.setSingleStep(0.5)
        self.blend.setSuffix(" °")
        self.blend.setValue(s.blend)
        self.expo = QCheckBox("Match brightness and colour between the two lenses")
        self.expo.setChecked(s.exposure_match)
        self.readout = QDoubleSpinBox()
        self.readout.setRange(0.0, 30.0)
        self.readout.setSingleStep(0.5)
        self.readout.setSuffix(" ms")
        self.readout.setValue(s.readout_ms)
        self.encoder = QComboBox()
        for k, v in ENCODERS.items():
            self.encoder.addItem(v, k)
        self.encoder.setCurrentIndex(list(ENCODERS).index(s.encoder) if s.encoder in ENCODERS else 0)
        self.recal = QCheckBox("Recalibrate lenses even if a cached calibration exists")
        self.recal.setChecked(s.recalibrate)
        f.addRow("Seam blend width", self.blend)
        f.addRow("", self.expo)
        f.addRow("Rolling-shutter readout", self.readout)
        f.addRow("", hint("0 disables rolling-shutter correction (default). Only change it if you know your "
                          "sensor's readout time."))
        f.addRow("HEVC encoder", self.encoder)
        f.addRow("", hint("Automatic tests the hardware encoders of this computer and picks the first that "
                          "works at the chosen resolution, falling back to the CPU."))
        f.addRow("", self.recal)
        reset = QPushButton("Restore defaults")
        reset.clicked.connect(self._reset)
        f.addRow("", reset)
        tabs.addTab(w, "Advanced")

        # --- about
        w, f = page()
        from .. import __version__
        title = QLabel(f"<b>osv360 {__version__}</b>")
        title.setStyleSheet("font-size: 14pt; background: transparent;")
        about = QLabel(
            "Converts DJI Osmo 360 recordings into stabilized equirectangular 360° video.<br><br>"
            "Released under the MIT License.<br>"
            '<a href="https://github.com/toccifrancesco/osv-slayer" style="color:#8b7bff;">'
            "github.com/toccifrancesco/osv-slayer</a><br><br>"
            "Not affiliated with or endorsed by DJI. “DJI” and “Osmo” are trademarks of their owner.")
        about.setWordWrap(True)
        about.setOpenExternalLinks(True)
        about.setStyleSheet("background: transparent;")
        notices = QPushButton("Third-party licenses…")
        notices.clicked.connect(self._show_notices)
        f.addRow(title)
        f.addRow(about)
        f.addRow(hint("osv360 includes Qt (LGPL-3.0), NumPy, SciPy, OpenCV, wgpu and other open-source "
                      "software. Their licenses and notices:"))
        f.addRow(notices)
        tabs.addTab(w, "About")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _show_notices(self):
        from ..licenses import notices_text
        dlg = QDialog(self)
        dlg.setWindowTitle("osv360 — third-party licenses")
        dlg.resize(820, 640)
        lay = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(notices_text())
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _quality_changed(self):
        v = self.quality.currentData()
        if v and v > 0:
            self.qp.blockSignals(True)
            self.qp.setValue(v)
            self.qp.blockSignals(False)

    def _qp_changed(self, v):
        vals = list(QUALITY_PRESETS.values())
        self.quality.blockSignals(True)
        self.quality.setCurrentIndex(vals.index(v) if v in vals else len(vals))
        self.quality.blockSignals(False)

    def _codec_changed(self):
        c = self.codec.currentData()
        self.quality.setEnabled(c != "prores")
        self.qp.setEnabled(c == "hevc")
        self.prores.setEnabled(c == "prores")

    def _reset(self):
        d = AppSettings()
        d.output_dir = self.out_dir.text() or d.output_dir
        self.s = d
        self.done(2)

    def result_settings(self) -> AppSettings:
        s = self.s
        s.output_dir = self.out_dir.text().strip() or s.output_dir
        s.use_source_dir = self.same_dir.isChecked()
        s.name_template = self.template.text().strip() or "{name}_{mode}"
        s.overwrite = self.overwrite.isChecked()
        s.open_folder_when_done = self.open_done.isChecked()
        s.notify_when_done = self.notify.isChecked()
        s.stabilize = self.mode.currentData()
        s.yaw_smooth = self.yaw.value()
        s.smooth = self.smooth.value()
        s.codec = self.codec.currentData()
        s.qp = self.qp.value()
        s.prores_profile = self.prores.currentData()
        s.scale = self.res.currentData()
        s.gop = self.gop.value()
        s.blend = self.blend.value()
        s.exposure_match = self.expo.isChecked()
        s.readout_ms = self.readout.value()
        s.recalibrate = self.recal.isChecked()
        s.encoder = self.encoder.currentData()
        return s


# ====================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("osv360")
        self.setWindowIcon(app_icon())
        self.resize(1040, 720)
        self.setAcceptDrops(True)
        self.settings = AppSettings.load()
        self.jobs: list[Job] = []
        self.rows: dict[str, tuple[QListWidgetItem, FileRow]] = {}
        self.runner = Runner(self)
        self.runner.job_changed.connect(self._job_changed)
        self.runner.log_line.connect(self._log)
        self.runner.queue_finished.connect(self._queue_finished)
        self.prober = Prober(self)
        self.prober.ready.connect(self._probed)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        header = QWidget()
        header.setObjectName("Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 18, 24, 10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(44, 44))
        logo.setStyleSheet("background: transparent;")
        tbox = QVBoxLayout()
        tbox.setSpacing(0)
        t = QLabel("osv360")
        t.setObjectName("Title")
        st = QLabel("DJI Osmo 360 → stabilized 360° video for Kdenlive & FilmCraft")
        st.setObjectName("Subtitle")
        tbox.addWidget(t)
        tbox.addWidget(st)
        self.sys_chip = QLabel("Checking system…")
        self.sys_chip.setObjectName("Chip")
        add_files = QPushButton("＋ Add files")
        add_files.clicked.connect(self._add_files_dialog)
        add_dir = QPushButton("Add folder")
        add_dir.clicked.connect(self._add_folder_dialog)
        self.clear_btn = QPushButton("Clear finished")
        self.clear_btn.clicked.connect(self._clear_finished)
        gear = QPushButton("⚙  Settings")
        gear.clicked.connect(self._open_settings)
        hl.addWidget(logo)
        hl.addSpacing(8)
        hl.addLayout(tbox)
        hl.addStretch(1)
        hl.addWidget(self.sys_chip)
        hl.addSpacing(10)
        for b in (add_files, add_dir, self.clear_btn, gear):
            hl.addWidget(b)
        root.addWidget(header)

        # body: drop zone <-> list
        self.stack = QStackedWidget()
        self.drop = DropZone()
        self.drop.clicked.connect(self._add_files_dialog)
        dz = QWidget()
        dzl = QVBoxLayout(dz)
        dzl.setContentsMargins(24, 12, 24, 24)
        dzl.addWidget(self.drop)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setSpacing(5)
        lw = QWidget()
        lwl = QVBoxLayout(lw)
        lwl.setContentsMargins(19, 6, 19, 8)
        lwl.addWidget(self.list)
        self.stack.addWidget(dz)
        self.stack.addWidget(lw)
        root.addWidget(self.stack, 1)

        # log drawer
        self.logview = QPlainTextEdit()
        self.logview.setReadOnly(True)
        self.logview.setMaximumBlockCount(5000)
        self.logview.setFixedHeight(170)
        self.logview.hide()
        lw2 = QWidget()
        l2 = QVBoxLayout(lw2)
        l2.setContentsMargins(24, 0, 24, 10)
        l2.addWidget(self.logview)
        root.addWidget(lw2)

        # bottom bar
        bottom = QWidget()
        bottom.setObjectName("BottomBar")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(24, 14, 24, 14)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.out_label = QLabel()
        self.out_label.setObjectName("RowTitle")
        self.preset_label = QLabel()
        self.preset_label.setObjectName("RowMeta")
        info.addWidget(self.out_label)
        info.addWidget(self.preset_label)
        change = QPushButton("Change folder")
        change.clicked.connect(self._choose_output)
        self.log_btn = QPushButton("Log")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(self.logview.setVisible)
        self.total_label = QLabel("")
        self.total_label.setObjectName("RowMeta")
        self.go = QPushButton("Convert")
        self.go.setObjectName("Primary")
        self.go.setMinimumWidth(170)
        self.go.clicked.connect(self._go_clicked)
        bl.addLayout(info, 1)
        bl.addWidget(change)
        bl.addWidget(self.log_btn)
        bl.addSpacing(12)
        bl.addWidget(self.total_label)
        bl.addSpacing(6)
        bl.addWidget(self.go)
        root.addWidget(bottom)

        quit_act = QAction(self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        self.addAction(quit_act)

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start(1000)
        self._update_bottom()
        self._run_doctor()

    # ------------------------------------------------------------------ files
    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = Path(p)
            files = sorted(x for x in p.rglob("*") if x.suffix.lower() in OSV_EXT) if p.is_dir() else [p]
            for f in files:
                if f.suffix.lower() not in OSV_EXT or not f.exists() or str(f) in self.rows:
                    continue
                job = Job(f.resolve())
                job.size_bytes = f.stat().st_size
                job.output = self.settings.output_for(job.path)
                self.jobs.append(job)
                row = FileRow(job)
                row.remove_requested.connect(self._remove_job)
                row.reveal_requested.connect(self._reveal)
                item = QListWidgetItem()
                item.setSizeHint(QSize(100, 116))
                self.list.addItem(item)
                self.list.setItemWidget(item, row)
                self.rows[str(job.path)] = (item, row)
                self.prober.probe(job.path)
                added += 1
        if added:
            self._log(f"added {added} file(s)")
        self._update_bottom()

    def _add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose Osmo 360 recordings", str(Path.home()),
                                                "DJI Osmo 360 (*.OSV *.osv);;All files (*)")
        self.add_paths(files)

    def _add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Folder with .OSV files", str(Path.home()))
        if d:
            self.add_paths([d])

    def _remove_job(self, job: Job):
        if job.state == "running":
            return
        item, _row = self.rows.pop(str(job.path))
        self.list.takeItem(self.list.row(item))
        self.jobs.remove(job)
        self._update_bottom()

    def _clear_finished(self):
        for j in [j for j in self.jobs if j.state in ("done", "skipped", "cancelled", "failed")]:
            self._remove_job(j)

    def _probed(self, path: str, png: bytes, dur: float, err: str):
        if path not in self.rows:
            return
        _item, row = self.rows[path]
        row.job.duration_s = dur
        row.set_thumbnail(png)
        if err:
            row.job.state, row.job.message = "failed", err
        row.refresh()
        self._update_bottom()

    # ------------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.drop.set_hover(True)

    def dragLeaveEvent(self, e):
        self.drop.set_hover(False)

    def dropEvent(self, e):
        self.drop.set_hover(False)
        self.add_paths(u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile())

    # ------------------------------------------------------------------ settings
    def _open_settings(self):
        dlg = SettingsDialog(AppSettings(**vars(self.settings)), self)
        r = dlg.exec()
        if r == QDialog.Accepted:
            self.settings = dlg.result_settings()
        elif r == 2:  # restore defaults
            self.settings = dlg.s
        else:
            return
        self.settings.save()
        for j in self.jobs:
            if j.state == "queued":
                j.output = self.settings.output_for(j.path)
                self.rows[str(j.path)][1].refresh()
        self._update_bottom()
        if r == 2:
            self._open_settings()

    def _choose_output(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.settings.output_dir)
        if d:
            self.settings.output_dir = d
            self.settings.use_source_dir = False
            self.settings.save()
            for j in self.jobs:
                if j.state == "queued":
                    j.output = self.settings.output_for(j.path)
                    self.rows[str(j.path)][1].refresh()
            self._update_bottom()

    # ------------------------------------------------------------------ run
    def _go_clicked(self):
        if self.runner.running:
            self._stop_requested = True
            self.go.setEnabled(False)
            self.go.setText("Stopping…")
            self.runner.stop()
            return
        self._stop_requested = False
        for j in self.jobs:  # allow re-running failed / cancelled ones
            if j.state in ("failed", "cancelled") and not j.message.startswith("not a dual"):
                j.state, j.message, j.done, j.total, j.stage = "queued", "", 0, 0, ""
                self.rows[str(j.path)][1].refresh()
        queued = [j for j in self.jobs if j.state == "queued"]
        if not queued:
            return
        if not self.settings.use_source_dir:
            Path(self.settings.output_dir).mkdir(parents=True, exist_ok=True)
        self.runner.start(self.jobs, self.settings)
        self._update_bottom()

    def _job_changed(self, job: Job):
        if str(job.path) in self.rows:
            self.rows[str(job.path)][1].refresh()
        self._update_bottom()

    def _queue_finished(self, ok: int, failed: int):
        self._stop_requested = False
        self.go.setEnabled(True)
        self._update_bottom()
        if ok + failed == 0:
            return
        msg = f"{ok} file(s) converted" + (f", {failed} failed" if failed else "")
        self._log(msg)
        if self.settings.notify_when_done:
            self._notify(msg)
        QApplication.alert(self)
        if self.settings.open_folder_when_done and ok:
            folder = Path(self.settings.output_dir) if not self.settings.use_source_dir else \
                next(j.output.parent for j in self.jobs if j.state == "done")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _notify(self, msg: str):
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            import subprocess

            from ..ffio import sysenv
            subprocess.Popen(["notify-send", "-a", "osv360", "-i", str(ASSETS / "osv360.svg"), "osv360", msg],
                             env=sysenv(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        from PySide6.QtWidgets import QSystemTrayIcon
        if QSystemTrayIcon.isSystemTrayAvailable():
            if not hasattr(self, "_tray"):
                self._tray = QSystemTrayIcon(app_icon(), self)
            self._tray.show()
            self._tray.showMessage("osv360", msg, app_icon(), 8000)

    def _reveal(self, job: Job):
        if job.output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.output.parent)))

    def _tick(self):
        if self.runner.current is not None:
            self._update_bottom()

    def _update_bottom(self):
        self.stack.setCurrentIndex(1 if self.jobs else 0)
        s = self.settings
        self.out_label.setText("Saving next to the source files" if s.use_source_dir else f"Saving to  {s.output_dir}")
        self.preset_label.setText(s.summary())
        queued = [j for j in self.jobs if j.state in ("queued", "failed", "cancelled")
                  and not j.message.startswith("not a dual")]
        total_s = sum(j.duration_s for j in queued)
        if self.runner.running:
            cur = self.runner.current
            left = len([j for j in self.jobs if j.state == "queued"])
            self.total_label.setText(f"{cur.path.name if cur else ''} · {left} more in queue")
            if not self._stop_requested:
                self.go.setText("■  Stop")
                self.go.setObjectName("Danger")
                self.go.setEnabled(True)
        else:
            self.total_label.setText(f"{len(queued)} file(s) · {fmt_time(total_s)} of footage" if queued else "")
            self.go.setText(f"▶  Convert {len(queued)} file{'s' if len(queued) != 1 else ''}" if queued
                            else "Convert")
            self.go.setObjectName("Primary")
            self.go.setEnabled(bool(queued) and self._system_ok is not False)
        self.go.style().unpolish(self.go)
        self.go.style().polish(self.go)
        self.clear_btn.setEnabled(any(j.state in ("done", "skipped", "cancelled", "failed") for j in self.jobs))

    # ------------------------------------------------------------------ system check
    _system_ok: bool | None = None
    _stop_requested = False

    def _run_doctor(self):
        cmd = self_command()
        self._doc = QProcess(self)
        self._doc.finished.connect(self._doctor_done)
        self._doc.start(cmd[0], cmd[1:] + ["doctor", "--json"])

    def _doctor_done(self, *_):
        out = bytes(self._doc.readAllStandardOutput()).decode("utf-8", "replace").strip().splitlines()
        try:
            r = json.loads(out[-1])
        except (ValueError, IndexError):
            r = {"ok": False, "ffmpeg": None, "gpu": None}
        if not r.get("ffmpeg"):
            state, text = "err", "ffmpeg not found"
            tip = ("osv360 needs ffmpeg. Linux: install it with your package manager · macOS: "
                   "`brew install ffmpeg` · Windows: `winget install Gyan.FFmpeg`. Then restart osv360.")
        elif not r.get("gpu"):
            state, text = "err", "No GPU"
            tip = "A GPU with Vulkan, Metal or Direct3D 12 support is required. " + r.get("gpu_error", "")
        elif not r.get("hardware_encoder"):
            state, text = "warn", "● CPU encoding (slow)"
            tip = (f"GPU: {r['gpu']}\n{r['ffmpeg']}\nNo working hardware HEVC encoder was found, so the "
                   "CPU encoder (x265) will be used. It works but is several times slower.")
        else:
            state, text = "ok", "● Ready · " + r["gpu"].split(" (")[0].replace("AMD ", "")
            tip = f"GPU: {r['gpu']}\n{r['ffmpeg']}\nHardware encoder: {r['encoder']}"
        self._system_ok = state != "err"
        self.sys_chip.setText(text)
        self.sys_chip.setToolTip(tip)
        self.sys_chip.setProperty("state", state)
        self.sys_chip.style().unpolish(self.sys_chip)
        self.sys_chip.style().polish(self.sys_chip)
        self._log(tip.replace("\n", " | "))
        self._update_bottom()

    # ------------------------------------------------------------------ misc
    def _log(self, line: str):
        self.logview.appendPlainText(line)

    def closeEvent(self, e):
        if self.runner.running:
            r = QMessageBox.question(self, "Conversion running",
                                     "A conversion is still running. Stop it and quit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.runner.stop()
            if self.runner.proc is not None:
                self.runner.proc.waitForFinished(10000)
        self.settings.save()
        e.accept()


def run(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    QApplication.setApplicationName("osv360")
    QApplication.setOrganizationName("osv360")
    QApplication.setDesktopFileName("osv360")
    app = QApplication(argv)
    app.setStyle("Fusion")
    app.setPalette(theme.palette())
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(app_icon())
    w = MainWindow()
    w.show()
    w.add_paths(a for a in argv[1:] if a.lower().endswith(".osv"))
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
