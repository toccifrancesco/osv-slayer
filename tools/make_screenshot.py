"""Regenerate docs/screenshot.png (staged queue, neutral paths, throwaway settings).

usage: QT_QPA_PLATFORM=offscreen python tools/make_screenshot.py path/to/any.OSV
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["OSV360_CONFIG"] = str(Path(tempfile.mkdtemp()) / "osv360.ini")  # never touch the real config

from PySide6.QtCore import QSize, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from osv360.gui import app as A  # noqa: E402
from osv360.gui import theme  # noqa: E402
from osv360.gui.jobs import Job  # noqa: E402

src = Path(sys.argv[1]).resolve()
qa = QApplication(sys.argv[:1])
qa.setStyle("Fusion")
qa.setPalette(theme.palette())
qa.setStyleSheet(theme.QSS)
w = A.MainWindow()
w.resize(1100, 640)
w.settings.output_dir = "~/Videos/osv360"
w.add_paths([src])


def stage():
    png = w.rows[str(src)][1].thumb.pixmap()
    w.list.clear()
    w.rows.clear()
    w.jobs.clear()
    specs = [("CAM_20260521182707_0012_D.OSV", "done", 5995, 5995, "Done in 6:34 · 15.2 fps"),
             ("CAM_20260521183344_0013_D.OSV", "running", 2310, 5996, ""),
             ("CAM_20260521184102_0014_D.OSV", "queued", 0, 0, "")]
    for name, st, d, t, msg in specs:
        j = Job(Path("/media/OSMO360/DCIM/DJI_001") / name, state=st, done=d, total=t, message=msg,
                stage="render" if st != "queued" else "", fps=15.2, duration_s=119.9, size_bytes=2_594_523_846)
        j.output = Path("~/Videos/osv360") / (Path(name).stem + "_horizon.mp4")
        r = A.FileRow(j)
        r.thumb.setPixmap(png)
        r.refresh()
        it = QListWidgetItem()
        it.setSizeHint(QSize(100, 116))
        w.list.addItem(it)
        w.list.setItemWidget(it, r)
        w.jobs.append(j)
        w.rows[name] = (it, r)
    w._update_bottom()
    w.total_label.setText("CAM_…_0013_D.OSV · 1 more in queue")
    w.go.setText("■  Stop")
    w.go.setObjectName("Danger")
    w.go.style().unpolish(w.go)
    w.go.style().polish(w.go)
    w.show()
    qa.processEvents()
    out = Path(__file__).resolve().parents[1] / "docs" / "screenshot.png"
    w.grab().save(str(out))
    print("wrote", out)
    qa.quit()


QTimer.singleShot(4000, stage)
qa.exec()
