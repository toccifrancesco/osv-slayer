"""Dark theme for the osv360 window (Qt style sheet + palette)."""
from PySide6.QtGui import QColor, QPalette

ACCENT = "#7c6cff"
ACCENT_2 = "#5ee7ff"
BG = "#11121a"
SURFACE = "#191b26"
SURFACE_2 = "#222534"
BORDER = "#2c3042"
TEXT = "#e8e9f1"
MUTED = "#8b8fa7"
OK = "#3ddc97"
WARN = "#ffb347"
ERR = "#ff5c7a"

QSS = f"""
* {{ font-size: 10.5pt; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

#Header {{ background: transparent; }}
#Title {{ font-size: 17pt; font-weight: 700; }}
#Subtitle {{ color: {MUTED}; }}
#Chip {{ border-radius: 10px; padding: 3px 10px; background: {SURFACE_2}; color: {MUTED}; font-size: 9pt; }}
#Chip[state="ok"] {{ color: {OK}; }}
#Chip[state="warn"] {{ color: {WARN}; }}
#Chip[state="err"] {{ color: {ERR}; }}
#Chip[state="run"] {{ color: {ACCENT_2}; }}

#DropZone {{ border: 2px dashed {BORDER}; border-radius: 18px; background: {SURFACE}; }}
#DropZone[hover="true"] {{ border-color: {ACCENT}; background: #1c1d2c; }}
#DropTitle {{ font-size: 14pt; font-weight: 600; background: transparent; }}
#DropHint {{ color: {MUTED}; background: transparent; }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ border: none; margin: 0px; padding: 0px; }}
QListWidget::item:selected {{ background: transparent; }}
#Row {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
#Row[state="running"] {{ border-color: {ACCENT}; }}
#Row QLabel {{ background: transparent; }}
#Thumb {{ border-radius: 8px; background: {SURFACE_2}; }}
#RowTitle {{ font-weight: 600; font-size: 11pt; }}
#RowMeta {{ color: {MUTED}; font-size: 9pt; }}
#RowStatus {{ color: {MUTED}; font-size: 9pt; }}

QProgressBar {{ background: {SURFACE_2}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ border-radius: 3px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
    stop:0 {ACCENT_2}, stop:1 {ACCENT}); }}

QPushButton {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 10px; padding: 8px 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: #2a2d40; }}
QPushButton:disabled {{ color: #555a70; border-color: #24273a; }}
QPushButton#Primary {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6a5cff, stop:1 #8b5cff);
    border: none; color: white; font-weight: 700; padding: 10px 22px; }}
QPushButton#Primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7b6dff, stop:1 #9d72ff); }}
QPushButton#Primary:disabled {{ background: #2e2b52; color: #8d88b8; }}
QPushButton#Danger {{ background: #3a1f2a; border: 1px solid #5a2a3a; color: {ERR}; font-weight: 700;
    padding: 10px 22px; }}
QPushButton#Icon {{ background: transparent; border: none; padding: 4px; border-radius: 8px; color: {MUTED};
    font-size: 13pt; }}
QPushButton#Icon:hover {{ background: {SURFACE_2}; color: {TEXT}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {SURFACE_2}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 6px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0px; border: none; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; outline: none; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {BORDER};
    background: {SURFACE_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {SURFACE_2}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 12px; top: -1px; background: {SURFACE}; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border: none; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabWidget > QWidget, QTabWidget QWidget#TabPage {{ background: {SURFACE}; }}
#TabPage QLabel, #TabPage QCheckBox {{ background: transparent; }}
#Hint {{ color: {MUTED}; font-size: 9pt; background: transparent; }}

#BottomBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
#BottomBar QLabel {{ background: transparent; }}
QPlainTextEdit {{ background: #0b0c12; border: 1px solid {BORDER}; border-radius: 10px;
    font-family: monospace; font-size: 9pt; color: #b9bdd4; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QDialog {{ background: {BG}; }}
"""


def palette() -> QPalette:
    p = QPalette()
    for role, c in ((QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, SURFACE_2),
                    (QPalette.AlternateBase, SURFACE), (QPalette.Text, TEXT), (QPalette.Button, SURFACE_2),
                    (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"),
                    (QPalette.ToolTipBase, SURFACE_2), (QPalette.ToolTipText, TEXT),
                    (QPalette.PlaceholderText, MUTED)):
        p.setColor(role, QColor(c))
    return p
