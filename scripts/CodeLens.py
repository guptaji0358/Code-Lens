#!/usr/bin/env python3
"""
CodeLens.py - CodeLens GUI Edition

A modern glass-styled Qt desktop front-end for the same search engine
as CodeLensCLI.py (exact one-line search, whitespace-insensitive
multi-line search, line ranges, encoding-aware auto-reloading file
loading). All parsing/search logic is imported from CodeLensCLI.py so
every front-end shares one engine - only presentation lives here.

Icons: real vector files under Assets/DarkModeIcons/ and
Assets/LightModeIcons/ (same filenames in both, different palette).
Qt loads SVG natively via QIcon/QSvgRenderer - no rasterizer
dependency needed. Accent-role icons are re-tinted at runtime to match
whatever custom accent color the user picks in Settings.

Author: Robin Gupta
Run:    python CodeLens.py
"""

import datetime
import faulthandler
import json
import math
import os
import re
import sys
import traceback

from PySide6.QtCore import (
    Qt, QTimer, QThread, QObject, QPropertyAnimation, QEasingCurve, QRect, QRectF, QPoint, QPointF, QSize,
    Signal, QSequentialAnimationGroup, Property, QParallelAnimationGroup,
)
from PySide6.QtGui import (
    QIcon, QPainter, QColor, QPen, QPixmap, QLinearGradient, QShortcut, QKeySequence, QFont,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QToolButton, QListWidget, QListWidgetItem, QLineEdit,
    QTextEdit, QPlainTextEdit, QFileDialog, QSplitter, QFrame, QCheckBox,
    QRadioButton, QButtonGroup, QDialog, QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect, QAbstractButton, QStackedWidget, QKeySequenceEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
)

import CodeLensCLI as core

APP_NAME = "CodeLens"
APP_TAGLINE = "Precision line & symbol search"


def _resource_base_dir():
    """
    Directory to resolve Assets/ against. A plain `os.path.dirname(__file__)`
    breaks once this script is frozen into an .exe (e.g. via PyInstaller):
    __file__ then points inside the bootloader's temp extraction folder, not
    next to the real .exe, so Assets/ silently fails to resolve and every
    icon (including the window icon) quietly disappears. PyInstaller sets
    sys.frozen and sys._MEIPASS specifically to let frozen apps find their
    bundled data; fall back to the source file's folder when running as a
    normal .py script. Source runs live in scripts/, one level below the
    repo root that actually holds Assets/, so go up one directory in that
    case.
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_global_error_handling():
    """
    Route every unhandled exception (main thread) through a Qt message box
    instead of the default behavior: PyQt/PySide silently prints to stderr
    (invisible in a --windowed build) and leaves the app either frozen or
    dead with no explanation - the "app not responding" reports. A log file
    next to the exe also captures the traceback for later diagnosis, and
    faulthandler covers native/interpreter-level crashes the same way.
    """
    log_path = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "CodeLens_crash.log"
    )
    try:
        _fh = open(log_path, "a", encoding="utf-8")
        faulthandler.enable(_fh)
    except OSError:
        pass

    def _excepthook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n{text}")
        except OSError:
            pass
        app = QApplication.instance()
        if app is not None:
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(f"{APP_NAME} - Unexpected error")
            box.setText("Something went wrong and the action could not finish.")
            box.setDetailedText(text)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()
        else:
            sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


class _AsyncWorker(QObject):
    """Runs one callable on a background QThread so slow disk/regex work
    (loading many/large files, reloading, scanning big searches) can't
    freeze the Qt event loop and trigger Windows' "Not Responding" state."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception:
            self.failed.emit(traceback.format_exc())
        else:
            self.finished.emit(result)


BASE_DIR = _resource_base_dir()
ASSETS_DIR = os.path.join(BASE_DIR, "Assets")
ICON_DIRS = {
    "dark": os.path.join(ASSETS_DIR, "DarkModeIcons"),
    "light": os.path.join(ASSETS_DIR, "LightModeIcons"),
    "hover": os.path.join(ASSETS_DIR, "HoverIcons"),
}
APP_ICON_PATH = os.path.join(ASSETS_DIR, "app_icon.ico")

# Icons whose color should follow the user's chosen accent color instead
# of staying fixed (danger/warn/secondary icons keep their semantic
# color regardless of accent, so destructive/caution actions still read
# clearly no matter what accent is picked).
ACCENT_ICON_NAMES = {"search", "multi", "lines", "info", "add", "reload", "clear", "grid"}

# --------------------------------------------------------------------------
# KEYBOARD SHORTCUTS (editable in Settings, persisted to shortcuts.json)
# --------------------------------------------------------------------------
SHORTCUT_ACTIONS = [
    ("search", "Run Search", "Ctrl+Return"),
    ("add", "Add File(s)", "Alt+A"),
    ("reload", "Reload Files", "Ctrl+R"),
    ("reset", "Reset", "Ctrl+Shift+R"),
    ("clear_results", "Clear Results", "Shift+C"),
    ("case_sensitive", "Toggle Case Sensitive", "Alt+S"),
]
SHORTCUTS_FILE = os.path.join(BASE_DIR, "shortcuts.json")


def load_shortcuts():
    mapping = {key: default for key, _label, default in SHORTCUT_ACTIONS}
    if os.path.isfile(SHORTCUTS_FILE):
        try:
            with open(SHORTCUTS_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            mapping.update({k: v for k, v in saved.items() if k in mapping and v})
        except (OSError, json.JSONDecodeError):
            pass
    return mapping


def save_shortcuts(mapping):
    try:
        with open(SHORTCUTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=2)
    except OSError:
        pass


def icon_path(name, theme):
    return os.path.join(ICON_DIRS[theme], f"{name}.svg")


def icon(name, theme):
    return QIcon(icon_path(name, theme))


def stable_icon(qicon, size=28):
    """
    Re-wraps a QIcon so the SAME pixmap is registered for every
    QIcon.Mode (Normal/Active/Selected/Disabled). Without this, Qt's
    Fusion style auto-generates the Active/Selected pixmap (used the
    instant a QToolButton/QRadioButton is hovered or checked) by
    lightening the Normal pixmap - which washes a thin-stroke icon on a
    transparent background down to almost nothing. That was the actual
    cause of "icon disappears on hover/checked", not the icon's color.
    """
    pm = qicon.pixmap(size, size)
    out = QIcon()
    for mode in (QIcon.Normal, QIcon.Active, QIcon.Selected, QIcon.Disabled):
        out.addPixmap(pm, mode, QIcon.Off)
        out.addPixmap(pm, mode, QIcon.On)
    return out


def tinted_icon(name, theme, color_hex, size=24):
    """Recolors an SVG icon to `color_hex` while preserving its shape/
    alpha - used so accent-role icons match a custom picked accent
    color without needing a differently-colored SVG file per color."""
    renderer = QSvgRenderer(icon_path(name, theme))
    base = QPixmap(size, size)
    base.fill(Qt.transparent)
    p = QPainter(base)
    renderer.render(p)
    p.end()

    tinted = QPixmap(base.size())
    tinted.fill(Qt.transparent)
    p2 = QPainter(tinted)
    p2.drawPixmap(0, 0, base)
    p2.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p2.fillRect(tinted.rect(), QColor(color_hex))
    p2.end()
    return QIcon(tinted)


def with_alpha(hex_color, alpha):
    """'#rrggbb' + 0-255 alpha -> 'rgba(r, g, b, a)' for QSS."""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


# --------------------------------------------------------------------------
# THEMES
# --------------------------------------------------------------------------
THEMES = {
    "dark": {
        "bg_a": "#0b0e1a", "bg_b": "#1a1030", "bg_panel": "#161925", "bg_card": "#1d2130",
        "bg_card_hover": "#262c40", "fg": "#e6e9f0", "fg_dim": "#8a8fa3",
        "accent": "#4fd1ff", "accent_fg": "#0b0d13", "secondary": "#8b8bff",
        "ok": "#5be38a", "warn": "#ffd166", "danger": "#ff6b6b",
        "border": "#2a2f42", "hl_bg": "#ffd166", "hl_fg": "#141414",
        "glass_border": "#ffffff",
    },
    "light": {
        "bg_a": "#eef2fb", "bg_b": "#f7e9f7", "bg_panel": "#ffffff", "bg_card": "#eef1f8",
        "bg_card_hover": "#e2e7f5", "fg": "#1c2130", "fg_dim": "#5b6172",
        "accent": "#0969da", "accent_fg": "#ffffff", "secondary": "#6639ba",
        "ok": "#1a7f37", "warn": "#9a6700", "danger": "#cf222e",
        "border": "#d7dcea", "hl_bg": "#fff2b2", "hl_fg": "#1c2130",
        "glass_border": "#ffffff",
    },
}

ACCENT_PRESETS = ["#4fd1ff", "#8b8bff", "#5be38a", "#ffd166", "#ff8fb1",
                  "#ff9d5c", "#c792ea", "#66e0c4", "#f45b69", "#38bdf8"]

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


def contrast_fg(hex_color):
    c = QColor(hex_color)
    luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return "#0b0d13" if luma > 150 else "#f5f7ff"


def build_stylesheet(t, glass):
    """
    Two visual modes sharing the same color tokens:
      - glass=True:  translucent rgba panels/cards over a gradient
        backdrop, soft highlight borders - the "frosted glass" look.
      - glass=False: solid flat panels - the previous, higher-contrast
        look, kept as an option for anyone who prefers it or is on a
        low-contrast display.
    """
    if glass:
        panel_bg = with_alpha(t["bg_panel"], 130)
        card_bg = with_alpha(t["bg_card"], 110)
        card_hover_bg = with_alpha(t["bg_card_hover"], 150)
        input_bg = with_alpha(t["bg_card"], 150)
        border = with_alpha(t["glass_border"], 40)
        border_soft = with_alpha(t["glass_border"], 22)
    else:
        panel_bg = t["bg_panel"]
        card_bg = t["bg_card"]
        card_hover_bg = t["bg_card_hover"]
        input_bg = t["bg_card"]
        border = t["border"]
        border_soft = t["border"]

    accent = t["accent"]
    accent_fg = t["accent_fg"]

    return f"""
    QMainWindow, QWidget#root {{ background: transparent; }}
    QWidget {{ color: {t['fg']}; font-family: '{FONT_UI}'; }}
    QFrame#panel, QFrame#card {{
        background: {panel_bg}; border-radius: 14px; border: 1px solid {border};
    }}
    QFrame#dialogCardOuter {{ background: transparent; }}
    QLabel#sectionLabel {{ color: {t['fg_dim']}; font-weight: 600; font-size: 8.5pt; letter-spacing: 1px; }}
    QLabel#statusBar {{ background: {panel_bg}; color: {t['fg_dim']}; padding: 6px 12px; border-radius: 8px; }}
    QLabel#summary {{ color: {t['fg_dim']}; }}
    QLabel#brand {{ color: {t['fg']}; font-size: 13.5pt; font-weight: 700; }}
    QLabel#brandTag {{ color: {t['fg_dim']}; font-size: 8.5pt; }}
    QListWidget {{
        background: {card_bg}; border: 1px solid {border_soft}; border-radius: 10px; padding: 4px;
        outline: none;
    }}
    QListWidget::item {{ padding: 8px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {accent}; color: {accent_fg}; }}
    QListWidget::item:hover:!selected {{ background: {card_hover_bg}; }}
    QLineEdit, QTextEdit, QPlainTextEdit {{
        background: {input_bg}; border: 1px solid {border}; border-radius: 10px;
        padding: 8px; color: {t['fg']}; font-family: '{FONT_MONO}';
        selection-background-color: {accent}; selection-color: {accent_fg};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {accent}; }}
    QTextEdit#results {{ background: {with_alpha(t['bg_a'], 90) if glass else t['bg_a']}; border: 1px solid {border_soft}; border-radius: 10px; font-family: '{FONT_MONO}'; }}
    QToolButton {{
        background: {card_bg}; border: 1px solid {border}; border-radius: 10px;
        padding: 7px 12px; color: {t['fg']};
    }}
    QToolButton:hover {{ background: {accent}; color: {accent_fg}; border-color: {accent}; }}
    QToolButton:checked {{ background: {accent}; color: {accent_fg}; border-color: {accent}; }}
    QToolButton:pressed {{ background: {t['secondary']}; }}
    QToolButton#danger:hover {{ background: {t['danger']}; color: white; border-color: {t['danger']}; }}
    QToolButton#warn:hover {{ background: {t['warn']}; color: #1c1300; border-color: {t['warn']}; }}
    QToolButton#viewToggle {{ padding: 4px; border-radius: 8px; }}
    QRadioButton {{
        background: {card_bg}; border: 1px solid {border}; border-radius: 10px;
        padding: 7px 12px;
    }}
    QRadioButton::indicator {{ width: 0; height: 0; }}
    QRadioButton:checked {{ background: {accent}; color: {accent_fg}; border-color: {accent}; }}
    QCheckBox {{ color: {t['fg_dim']}; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; border-radius: 6px; }}
    QScrollBar::handle:vertical {{ background: {border}; border-radius: 6px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {accent}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def gradient_style(t):
    return (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {t['bg_a']}, stop:1 {t['bg_b']});")


# --------------------------------------------------------------------------
# GLASS BACKDROP (paints the gradient + soft blobs behind everything)
# --------------------------------------------------------------------------
class GlassBackdrop(QWidget):
    """
    Paints a soft gradient plus a couple of large, blurred-looking color
    blobs so the translucent glass panels above have something colorful
    to show through - a flat single color behind glass panels doesn't
    read as "glass" at all. When glass mode is off this just renders a
    plain flat theme background instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = THEMES["dark"]
        self.glass = True
        self.accent = self.theme["accent"]

    def set_theme(self, t, accent, glass):
        self.theme = t
        self.accent = accent
        self.glass = glass
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        grad = QLinearGradient(0, 0, rect.width(), rect.height())
        grad.setColorAt(0, QColor(self.theme["bg_a"]))
        grad.setColorAt(1, QColor(self.theme["bg_b"]))
        p.fillRect(rect, grad)

        if not self.glass:
            return

        def blob(cx, cy, r, color, alpha):
            c = QColor(color)
            c.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))

        w, h = rect.width(), rect.height()
        blob(w * 0.15, h * 0.1, max(w, h) * 0.28, self.accent, 55)
        blob(w * 0.9, h * 0.25, max(w, h) * 0.22, self.theme["secondary"], 45)
        blob(w * 0.75, h * 0.95, max(w, h) * 0.3, self.theme["accent"], 35)


# --------------------------------------------------------------------------
# GLASS CARD SHADOW HELPER
# --------------------------------------------------------------------------
def apply_glass_shadow(widget, color="#000000", blur=36, alpha=110, offset=(0, 10)):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(*offset)
    c = QColor(color)
    c.setAlpha(alpha)
    effect.setColor(c)
    widget.setGraphicsEffect(effect)


# --------------------------------------------------------------------------
# HOVER-AWARE ICON WIDGETS
#
# A themed icon tinted to match the button's *resting* background
# disappears the instant the button's hover/checked state paints a
# solid accent/danger/warn background behind it (same color on same
# color). These mixins swap to a fixed white icon (Assets/HoverIcons/)
# whenever the button is hovered or checked, and back to the normal
# themed/tinted icon otherwise.
# --------------------------------------------------------------------------
class HoverIconMixin:
    def _init_hover_icons(self):
        self._normal_icon = QIcon()
        self._hover_icon = QIcon()

    def set_icons(self, normal_icon, hover_icon):
        self._normal_icon = stable_icon(normal_icon)
        self._hover_icon = stable_icon(hover_icon)
        self._refresh_icon()

    def _refresh_icon(self):
        active = self.underMouse() or (self.isCheckable() and self.isChecked())
        self.setIcon(self._hover_icon if active else self._normal_icon)

    def enterEvent(self, event):
        self._refresh_icon()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon()
        super().leaveEvent(event)


class HoverToolButton(HoverIconMixin, QToolButton):
    def __init__(self, *args, **kwargs):
        QToolButton.__init__(self, *args, **kwargs)
        self._init_hover_icons()
        self.toggled.connect(lambda _c: self._refresh_icon())


class HoverRadioButton(HoverIconMixin, QRadioButton):
    def __init__(self, *args, **kwargs):
        QRadioButton.__init__(self, *args, **kwargs)
        self._init_hover_icons()
        self.toggled.connect(lambda _c: self._refresh_icon())


# --------------------------------------------------------------------------
# ANIMATED DARK/LIGHT TOGGLE SWITCH (reused for any on/off setting)
# --------------------------------------------------------------------------
class GlassSwitch(QAbstractButton):
    """A pill-shaped, animated on/off toggle - the knob glides across on
    click via QPropertyAnimation instead of snapping, and the track
    color eases between "off" and "on" colors at the same time. Used
    for both the dark/light toggle and the glass-effect toggle."""

    def __init__(self, parent=None, checked=False, off_color="#3a3f55", on_color="#4fd1ff",
                 glyphs=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self._off_color = QColor(off_color)
        self._on_color = QColor(on_color)
        self._glyphs = glyphs  # optional ("off_svg_name", "on_svg_name")-like callables
        self.setFixedSize(58, 30)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def set_colors(self, off_color, on_color):
        self._off_color = QColor(off_color)
        self._on_color = QColor(on_color)
        self.update()

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def getKnobPos(self):
        return self._pos

    def setKnobPos(self, value):
        self._pos = value
        self.update()

    knobPos = Property(float, getKnobPos, setKnobPos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        o, n = self._off_color, self._on_color
        track = QColor(
            round(o.red() + (n.red() - o.red()) * self._pos),
            round(o.green() + (n.green() - o.green()) * self._pos),
            round(o.blue() + (n.blue() - o.blue()) * self._pos),
        )
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(0, 0, w, h, h / 2, h / 2)

        knob_d = h - 6
        knob_x = 3 + (w - h) * self._pos
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawEllipse(int(knob_x), 3, knob_d, knob_d)


# --------------------------------------------------------------------------
# CUSTOM GLASSY CHECKBOX (replaces the stock QCheckBox everywhere)
# --------------------------------------------------------------------------
class GlassCheckbox(QAbstractButton):
    """A rounded, glassy checkbox that fills with the accent color and
    pops a checkmark in with an OutBack ease, instead of the native OS
    checkbox square. Drop-in for QCheckBox: supports text/isChecked/
    setChecked/toggled the same way."""

    def __init__(self, text="", parent=None, accent="#4fd1ff", fg="#e6e9f0"):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._text = text
        self._accent = QColor(accent)
        self._fg = QColor(fg)
        self._progress = 0.0
        self._anim = QPropertyAnimation(self, b"checkProgress", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutBack)
        self.toggled.connect(self._animate)
        self.setMinimumHeight(22)

    def setText(self, text):
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def set_colors(self, accent_hex, fg_hex):
        self._accent = QColor(accent_hex)
        self._fg = QColor(fg_hex)
        self.update()

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def getCheckProgress(self):
        return self._progress

    def setCheckProgress(self, value):
        self._progress = max(0.0, min(1.0, value))
        self.update()

    checkProgress = Property(float, getCheckProgress, setCheckProgress)

    def sizeHint(self):
        fm = self.fontMetrics()
        w = 18 + 8 + fm.horizontalAdvance(self._text) + 6
        return QSize(w, max(22, fm.height() + 6))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = 18
        box_rect = QRectF(1, (self.height() - box) / 2, box, box)
        prog = self._progress

        base = QColor(255, 255, 255, 30)
        fill = QColor(
            round(base.red() + (self._accent.red() - base.red()) * prog),
            round(base.green() + (self._accent.green() - base.green()) * prog),
            round(base.blue() + (self._accent.blue() - base.blue()) * prog),
            round(base.alpha() + (255 - base.alpha()) * prog),
        )
        border = self._accent if prog > 0.05 else QColor(255, 255, 255, 90)
        p.setPen(QPen(border, 1.6))
        p.setBrush(fill)
        p.drawRoundedRect(box_rect, 5, 5)

        if prog > 0.02:
            check_color = contrast_fg(self._accent.name())
            pen = QPen(QColor(check_color), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setOpacity(min(1.0, prog * 1.3))
            l, t = box_rect.left(), box_rect.top()
            pts = [QPointF(l + 4, t + 9.5), QPointF(l + 7.5, t + 13), QPointF(l + 14, t + 5)]
            p.drawPolyline(pts)
            p.setOpacity(1.0)

        p.setPen(self._fg)
        text_rect = QRectF(box + 8, 0, self.width() - box - 8, self.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._text)


# --------------------------------------------------------------------------
# CUSTOM GLASSY COLOR PICKER (replaces the stock QColorDialog)
# --------------------------------------------------------------------------
class ColorSwatch(QAbstractButton):
    """One round, glassy accent-color swatch - a soft radial highlight
    plus a glowing ring when selected, instead of a flat square."""

    def __init__(self, color_hex, parent=None):
        super().__init__(parent)
        self.color_hex = color_hex
        self.setCheckable(True)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(3, 3, -3, -3)
        base = QColor(self.color_hex)

        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, base.lighter(130))
        grad.setColorAt(1, base.darker(110))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(r)

        # glassy highlight arc
        p.setBrush(QColor(255, 255, 255, 90))
        p.drawEllipse(r.adjusted(4, 3, -12, -14))

        if self.isChecked():
            pen = QPen(QColor(255, 255, 255, 230), 2.4)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class AccentColorPicker(QDialog):
    """
    A frameless, glass-styled popup for choosing the app's accent color
    - a grid of preset glassy swatches plus a hex entry for anything
    custom, replacing the plain system color dialog everywhere the app
    needs an accent pick.
    """

    def __init__(self, parent, t, current_hex):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.chosen = current_hex

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        card = QFrame(self)
        card.setStyleSheet(f"""
            QFrame {{
                background: {with_alpha(t['bg_panel'], 235)};
                border-radius: 16px; border: 1px solid {with_alpha('#ffffff', 40)};
            }}
        """)
        apply_glass_shadow(card, blur=40, alpha=140)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        title = QLabel("Accent Color")
        title.setStyleSheet(f"color:{t['fg']}; font-weight:700; font-size:11pt;")
        lay.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.swatches = []
        for i, color_hex in enumerate(ACCENT_PRESETS):
            sw = ColorSwatch(color_hex)
            sw.setChecked(color_hex.lower() == current_hex.lower())
            sw.clicked.connect(lambda _c=False, h=color_hex: self._pick(h))
            grid.addWidget(sw, i // 5, i % 5)
            self.swatches.append(sw)
        lay.addLayout(grid)

        custom_row = QHBoxLayout()
        self.hex_entry = QLineEdit(current_hex)
        self.hex_entry.setStyleSheet(f"""
            QLineEdit {{ background: {with_alpha(t['bg_card'], 200)}; color:{t['fg']};
                border: 1px solid {with_alpha('#ffffff', 40)}; border-radius: 8px; padding: 6px 8px; }}
        """)
        self.preview = ColorSwatch(current_hex)
        self.preview.setEnabled(False)
        custom_row.addWidget(self.preview)
        custom_row.addWidget(self.hex_entry, 1)
        self.hex_entry.textChanged.connect(self._on_hex_edit)
        lay.addLayout(custom_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        apply_btn = QPushButton("Apply")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {current_hex}; color: {contrast_fg(current_hex)};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(apply_btn)
        lay.addLayout(btn_row)
        self._apply_btn = apply_btn

    def _pick(self, color_hex):
        self.chosen = color_hex
        self.hex_entry.setText(color_hex)
        for sw in self.swatches:
            sw.setChecked(sw.color_hex.lower() == color_hex.lower())
        self._refresh_preview()

    def _on_hex_edit(self, text):
        text = text.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
            self.chosen = text
            for sw in self.swatches:
                sw.setChecked(sw.color_hex.lower() == text.lower())
            self._refresh_preview()

    def _refresh_preview(self):
        self.preview.color_hex = self.chosen
        self.preview.update()
        self._apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {self.chosen}; color: {contrast_fg(self.chosen)};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)

    @staticmethod
    def pick(parent, t, current_hex):
        dlg = AccentColorPicker(parent, t, current_hex)
        dlg.adjustSize()
        btn = parent.btn_accent if hasattr(parent, "btn_accent") else None
        if btn is not None:
            gp = btn.mapToGlobal(QPoint(0, btn.height() + 6))
            dlg.move(gp)
        if dlg.exec() == QDialog.Accepted:
            return dlg.chosen
        return None


# --------------------------------------------------------------------------
# SHORTCUTS EDITOR (glass-styled, replaces a plain settings table)
# --------------------------------------------------------------------------
class ShortcutsDialog(QDialog):
    """Lists every editable shortcut with a QKeySequenceEdit box - click
    a box and press the new combination, then Save to rebind and persist
    it to shortcuts.json."""

    def __init__(self, parent, t, glass, current):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.result_map = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = QFrame(self)
        bg = with_alpha(t["bg_panel"], 235) if glass else t["bg_panel"]
        card.setStyleSheet(f"""
            QFrame {{ background: {bg}; border-radius: 16px; border: 1px solid {t['accent']}; }}
        """)
        apply_glass_shadow(card, blur=44, alpha=130)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel("Keyboard Shortcuts")
        title.setStyleSheet(f"color: {t['fg']}; font-size: 12.5pt; font-weight: 600;")
        lay.addWidget(title)
        hint = QLabel("Click a box below and press a new key combination to rebind it.")
        hint.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.edits = {}
        for row, (key, label, default) in enumerate(SHORTCUT_ACTIONS):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {t['fg']};")
            grid.addWidget(lbl, row, 0)
            edit = QKeySequenceEdit(QKeySequence(current.get(key, default)))
            edit.setStyleSheet(f"""
                QKeySequenceEdit {{ background: {with_alpha(t['bg_card'], 200)}; color: {t['fg']};
                    border: 1px solid {with_alpha('#ffffff', 40)}; border-radius: 8px; padding: 4px 8px; }}
            """)
            grid.addWidget(edit, row, 1)
            self.edits[key] = edit
        lay.addLayout(grid)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {t['fg_dim']};
                border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 16px; }}
            QPushButton:hover {{ background: {t['bg_card']}; color: {t['fg']}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{ background: {t['accent']}; color: {contrast_fg(t['accent'])};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

    def _save(self):
        defaults = {key: default for key, _label, default in SHORTCUT_ACTIONS}
        self.result_map = {key: edit.keySequence().toString() or defaults[key]
                            for key, edit in self.edits.items()}
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        self.adjustSize()
        if parent is not None:
            pg = parent.geometry()
            w, h = self.width(), self.height()
            self.setGeometry(pg.center().x() - w // 2, pg.center().y() - h // 2, w, h)

    @staticmethod
    def edit(parent, t, glass, current):
        dlg = ShortcutsDialog(parent, t, glass, current)
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_map
        return None


# --------------------------------------------------------------------------
# FILE PREVIEW DIALOG - multiple view modes + zoom
# --------------------------------------------------------------------------
class PreviewDialog(QDialog):
    """
    Opened by double-clicking a loaded file in the left panel. Gives two
    ways to look at the same file (Formatted, with line numbers colored
    like the results pane; Raw, exact plain text for easy copying),
    independent line-wrap, and zoom in/out - the things people actually
    reach for when a file is hard to read at the default size/format.
    A normal (non-frameless) resizable window, since a content viewer
    benefits from OS resize/maximize more than from matching the small
    glass popups used for quick confirmations elsewhere in the app.
    """

    ZOOM_MIN_STEPS, ZOOM_MAX_STEPS = -5, 20

    def __init__(self, parent, t, icon_theme, glass, entry):
        super().__init__(parent)
        self.entry = entry
        self.t = t
        self.zoom_steps = 0

        self.setWindowTitle(f"Preview - {os.path.basename(entry.path)}")
        self.resize(920, 680)
        self.setStyleSheet(f"""
            QDialog {{ background: {t['bg_panel']}; }}
            QLabel {{ color: {t['fg']}; }}
            QToolButton {{ background: {t['bg_card']}; border: 1px solid {t['border']};
                border-radius: 8px; color: {t['fg']}; padding: 6px; }}
            QToolButton:hover {{ background: {t['accent']}; border-color: {t['accent']}; }}
            QToolButton:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel(os.path.basename(entry.path))
        title.setStyleSheet(f"font-weight: 700; font-size: 12.5pt; color: {t['fg']};")
        header.addWidget(title)
        path_lbl = QLabel(entry.path)
        path_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        header.addWidget(path_lbl, 1)
        meta_lbl = QLabel(f"{len(entry.lines):,} lines - {entry.encoding}")
        meta_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        header.addWidget(meta_lbl)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.btn_formatted = self._small_button("lines", icon_theme, "Formatted view (with line numbers)")
        self.btn_raw = self._small_button("file", icon_theme, "Raw text view (exact file content)")
        self.btn_formatted.setChecked(True)
        self.view_group.addButton(self.btn_formatted)
        self.view_group.addButton(self.btn_raw)
        self.view_group.buttonToggled.connect(self._on_view_toggle)
        toolbar.addWidget(self.btn_formatted)
        toolbar.addWidget(self.btn_raw)

        toolbar.addSpacing(10)
        self.wrap_chk = GlassCheckbox("Wrap", accent=t["accent"], fg=t["fg"])
        self.wrap_chk.toggled.connect(self._apply_wrap)
        toolbar.addWidget(self.wrap_chk)

        toolbar.addStretch()
        self.btn_zoom_out = self._small_button("zoomout", icon_theme, "Zoom out  (Ctrl+-)")
        self.btn_zoom_out.setCheckable(False)
        self.btn_zoom_out.clicked.connect(self._zoom_out)
        self.zoom_lbl = QLabel("100%")
        self.zoom_lbl.setFixedWidth(42)
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        self.btn_zoom_in = self._small_button("zoomin", icon_theme, "Zoom in  (Ctrl++)")
        self.btn_zoom_in.setCheckable(False)
        self.btn_zoom_in.clicked.connect(self._zoom_in)
        toolbar.addWidget(self.btn_zoom_out)
        toolbar.addWidget(self.zoom_lbl)
        toolbar.addWidget(self.btn_zoom_in)
        layout.addLayout(toolbar)

        self.stack = QStackedWidget()
        self.formatted_view = QTextEdit()
        self.formatted_view.setReadOnly(True)
        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.stack.addWidget(self.formatted_view)
        self.stack.addWidget(self.raw_view)
        layout.addWidget(self.stack, 1)

        content_style = (f"background: {t['bg_card']}; color: {t['fg']}; "
                          f"border: 1px solid {t['border']}; border-radius: 8px; "
                          f"font-family: '{FONT_MONO}'; padding: 6px;")
        self.formatted_view.setStyleSheet(content_style)
        self.raw_view.setStyleSheet(content_style)

        self._build_formatted()
        self.raw_view.setPlainText("\n".join(entry.lines))
        self._apply_wrap(False)

        for seq in ("Ctrl+=", "Ctrl++"):
            QShortcut(QKeySequence(seq), self).activated.connect(self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self._zoom_reset)

    def _small_button(self, icon_name, icon_theme, tooltip):
        b = HoverToolButton()
        b.setCheckable(True)
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(30, 30)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tooltip)
        b.set_icons(icon(icon_name, icon_theme), icon(icon_name, "hover"))
        return b

    def _build_formatted(self):
        width = len(str(max(len(self.entry.lines), 1)))
        accent = self.t["accent"]
        parts = []
        for i, line in enumerate(self.entry.lines, start=1):
            num = str(i).rjust(width)
            esc = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            parts.append(f'<span style="color:{accent}">{num}:</span> {esc}')
        self.formatted_view.setHtml("<br>".join(parts) if parts else "")

    def _on_view_toggle(self, button, checked):
        if not checked:
            return
        self.stack.setCurrentIndex(0 if button is self.btn_formatted else 1)

    def _apply_wrap(self, checked):
        self.formatted_view.setLineWrapMode(QTextEdit.WidgetWidth if checked else QTextEdit.NoWrap)
        self.raw_view.setLineWrapMode(QPlainTextEdit.WidgetWidth if checked else QPlainTextEdit.NoWrap)

    # See FinderWindow's zoom methods for why zoomIn()/zoomOut() (not
    # setFont) are used - the content_style stylesheet above sets
    # font-family on these widgets, which defeats a plain setFont()
    # point-size change.
    def _zoom_in(self):
        if self.zoom_steps >= self.ZOOM_MAX_STEPS:
            return
        for w in (self.formatted_view, self.raw_view):
            w.zoomIn(1)
        self.zoom_steps += 1
        self.zoom_lbl.setText(f"{100 + self.zoom_steps * 10}%")

    def _zoom_out(self):
        if self.zoom_steps <= self.ZOOM_MIN_STEPS:
            return
        for w in (self.formatted_view, self.raw_view):
            w.zoomOut(1)
        self.zoom_steps -= 1
        self.zoom_lbl.setText(f"{100 + self.zoom_steps * 10}%")

    def _zoom_reset(self):
        for w in (self.formatted_view, self.raw_view):
            if self.zoom_steps > 0:
                w.zoomOut(self.zoom_steps)
            elif self.zoom_steps < 0:
                w.zoomIn(-self.zoom_steps)
        self.zoom_steps = 0
        self.zoom_lbl.setText("100%")

    @staticmethod
    def preview(parent, t, icon_theme, glass, entry):
        dlg = PreviewDialog(parent, t, icon_theme, glass, entry)
        dlg.exec()


# --------------------------------------------------------------------------
# FILE DETAILS DIALOG - full metadata for every loaded file, in one place
# --------------------------------------------------------------------------
class FileDetailsDialog(QDialog):
    """A separate window listing every loaded file's full metadata
    (lines, size, encoding, last-modified, full path) in a sortable
    table - opened via the toolbar's Details button rather than only
    getting a truncated one-line summary per file in the main list."""

    def __init__(self, parent, t, files):
        super().__init__(parent)
        self.setWindowTitle("File Details")
        self.resize(820, 440)
        self.setStyleSheet(f"""
            QDialog {{ background: {t['bg_panel']}; }}
            QLabel {{ color: {t['fg']}; }}
            QTableWidget {{ background: {t['bg_card']}; color: {t['fg']};
                gridline-color: {t['border']}; border: 1px solid {t['border']};
                border-radius: 8px; selection-background-color: {t['accent']};
                selection-color: {contrast_fg(t['accent'])}; }}
            QHeaderView::section {{ background: {t['bg_panel']}; color: {t['fg_dim']};
                padding: 6px; border: none; border-bottom: 1px solid {t['border']}; }}
            QPushButton {{ background: {t['accent']}; color: {contrast_fg(t['accent'])};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(f"File Details  -  {len(files)} loaded")
        title.setStyleSheet(f"font-weight: 700; font-size: 12.5pt; color: {t['fg']};")
        layout.addWidget(title)

        columns = ["Name", "Lines", "Size", "Encoding", "Modified", "Full Path"]
        table = QTableWidget(len(files), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setStretchLastSection(True)
        # Sorting must stay OFF while populating: QTableWidget re-sorts
        # after every setItem() call once enabled, and since a row's
        # columns are filled in one at a time, that mid-population
        # re-sort scrambles cells across rows before a row is complete.
        table.setSortingEnabled(False)

        for row, entry in enumerate(files):
            size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"
            try:
                modified = datetime.datetime.fromtimestamp(
                    os.path.getmtime(entry.path)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                modified = "?"
            values = [os.path.basename(entry.path), f"{len(entry.lines):,}", size,
                      entry.encoding or "?", modified, entry.path]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setToolTip(entry.path)
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def show_details(parent, t, files):
        dlg = FileDetailsDialog(parent, t, files)
        dlg.exec()


# --------------------------------------------------------------------------
# CUSTOM ANIMATED DIALOG (replaces stock QMessageBox everywhere)
# --------------------------------------------------------------------------
class AnimatedDialog(QDialog):
    """
    A frameless, rounded, glass-matched popup that fades and scales in
    instead of snapping open like a native message box. Used for every
    info/warning/error/confirm in the app so the whole UI feels like
    one designed surface instead of Qt/Windows chrome interrupting it.
    """

    ICONS = {"info": "info", "warn": "reset", "error": "remove", "question": "settings"}
    COLOR_KEYS = {"info": "accent", "warn": "warn", "error": "danger", "question": "secondary"}

    def __init__(self, parent, t, icon_theme, glass, kind, title, message, buttons):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.result_value = None
        color = t[self.COLOR_KEYS.get(kind, "accent")]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)

        card = QFrame(self)
        card.setObjectName("dialogCard")
        bg = with_alpha(t["bg_panel"], 235) if glass else t["bg_panel"]
        card.setStyleSheet(f"""
            QFrame#dialogCard {{
                background: {bg}; border-radius: 16px;
                border: 1px solid {color};
            }}
        """)
        apply_glass_shadow(card, blur=44, alpha=130)
        outer.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(10)

        head = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon(self.ICONS.get(kind, "info"), icon_theme).pixmap(28, 28))
        head.addWidget(icon_lbl)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color: {t['fg']}; font-size: 12.5pt; font-weight: 600;")
        head.addWidget(title_lbl)
        head.addStretch()
        card_layout.addLayout(head)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 10pt;")
        msg_lbl.setMinimumWidth(320)
        card_layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        for label, value, primary in buttons:
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            if primary:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {color}; color: {contrast_fg(color)};
                        border: none; border-radius: 8px; padding: 8px 18px; font-weight: 600; }}
                    QPushButton:hover {{ background: {t['fg']}; color: {t['bg_panel']}; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {t['fg_dim']};
                        border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 18px; }}
                    QPushButton:hover {{ background: {t['bg_card']}; color: {t['fg']}; }}
                """)
            b.clicked.connect(lambda _c=False, v=value: self._finish(v))
            btn_row.addWidget(b)
        card_layout.addLayout(btn_row)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0)

    def _finish(self, value):
        self.result_value = value
        self._play(reverse=True, on_done=self.accept)

    def _play(self, reverse=False, on_done=None):
        start_geo = self.geometry()
        cx, cy = start_geo.center().x(), start_geo.center().y()
        big = start_geo
        small = QRect(0, 0, int(start_geo.width() * 0.92), int(start_geo.height() * 0.92))
        small.moveCenter(QPoint(cx, cy))

        fade = QPropertyAnimation(self._opacity, b"opacity", self)
        fade.setDuration(180)
        geo = QPropertyAnimation(self, b"geometry", self)
        geo.setDuration(180)
        geo.setEasingCurve(QEasingCurve.OutCubic if not reverse else QEasingCurve.InCubic)

        if reverse:
            fade.setStartValue(1); fade.setEndValue(0)
            geo.setStartValue(big); geo.setEndValue(small)
        else:
            fade.setStartValue(0); fade.setEndValue(1)
            geo.setStartValue(small); geo.setEndValue(big)

        group = QParallelAnimationGroup(self)
        group.addAnimation(fade)
        group.addAnimation(geo)
        if on_done:
            group.finished.connect(on_done)
        group.start(QPropertyAnimation.DeleteWhenStopped)
        self._active_anim = group

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if parent is not None:
            pg = parent.geometry()
            w, h = self.sizeHint().width(), self.sizeHint().height()
            self.setGeometry(pg.center().x() - w // 2, pg.center().y() - h // 2, w, h)
        QTimer.singleShot(0, lambda: self._play(reverse=False))

    @staticmethod
    def show(parent, t, icon_theme, glass, kind, title, message, buttons=None):
        if buttons is None:
            buttons = [("OK", True, True)]
        dlg = AnimatedDialog(parent, t, icon_theme, glass, kind, title, message, buttons)
        dlg.exec()
        return dlg.result_value


# --------------------------------------------------------------------------
# SETTINGS PANEL (slide-down)
# --------------------------------------------------------------------------
class SettingsPanel(QFrame):
    themeChanged = Signal(str)
    glassChanged = Signal(bool)
    accentRequested = Signal()
    shortcutsRequested = Signal()

    def __init__(self, parent, theme, glass_enabled, shortcut_keys):
        super().__init__(parent)
        self.setObjectName("panel")
        self._max_height = 150

        outer_v = QVBoxLayout(self)
        outer_v.setContentsMargins(18, 12, 18, 12)
        outer_v.setSpacing(8)

        title = QLabel("SETTINGS")
        title.setObjectName("sectionLabel")
        outer_v.addWidget(title)

        row1 = QHBoxLayout()
        row1.setSpacing(28)

        col1 = QVBoxLayout()
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme"))
        self.theme_switch = GlassSwitch(self, checked=(theme == "light"),
                                         off_color="#232840", on_color="#ffd166")
        self.theme_switch.setToolTip("Switch between dark and light theme")
        self.theme_switch.toggled.connect(lambda checked: self.themeChanged.emit("light" if checked else "dark"))
        theme_row.addWidget(self.theme_switch)
        self.mode_lbl = QLabel("Dark mode" if theme == "dark" else "Light mode")
        self.mode_lbl.setStyleSheet("font-weight: 600;")
        theme_row.addWidget(self.mode_lbl)
        theme_row.addStretch()
        col1.addLayout(theme_row)
        row1.addLayout(col1)

        col2 = QVBoxLayout()
        glass_row = QHBoxLayout()
        glass_row.addWidget(QLabel("Glass Effect"))
        self.glass_switch = GlassSwitch(self, checked=glass_enabled,
                                         off_color="#3a3f55", on_color="#4fd1ff")
        self.glass_switch.setToolTip("Turn the frosted-glass look on or off")
        self.glass_switch.toggled.connect(self.glassChanged.emit)
        glass_row.addWidget(self.glass_switch)
        self.glass_lbl = QLabel("On" if glass_enabled else "Off")
        self.glass_lbl.setStyleSheet("font-weight: 600;")
        glass_row.addWidget(self.glass_lbl)
        glass_row.addStretch()
        col2.addLayout(glass_row)
        row1.addLayout(col2)

        col3 = QVBoxLayout()
        self.btn_accent = QPushButton("  Accent Color")
        self.btn_accent.setCursor(Qt.PointingHandCursor)
        self.btn_accent.setToolTip("Pick a custom accent color for buttons and highlights")
        self.btn_accent.clicked.connect(self.accentRequested.emit)
        col3.addWidget(self.btn_accent)
        row1.addLayout(col3)

        row1.addStretch()
        outer_v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        sc_title = QLabel("Shortcuts:")
        sc_title.setStyleSheet("font-weight: 600;")
        row2.addWidget(sc_title)
        self.shortcuts_lbl = QLabel()
        self.shortcuts_lbl.setObjectName("summary")
        self.shortcuts_lbl.setWordWrap(True)
        row2.addWidget(self.shortcuts_lbl, 1)
        self.btn_shortcuts = QPushButton("  Edit Shortcuts")
        self.btn_shortcuts.setCursor(Qt.PointingHandCursor)
        self.btn_shortcuts.setToolTip("View and rebind every keyboard shortcut")
        self.btn_shortcuts.clicked.connect(self.shortcutsRequested.emit)
        row2.addWidget(self.btn_shortcuts)
        outer_v.addLayout(row2)
        self.set_shortcuts_summary(shortcut_keys)

        self.setMaximumHeight(0)
        self.setMinimumHeight(0)
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._visible = False

    def set_theme_label(self, theme):
        self.mode_lbl.setText("Dark mode" if theme == "dark" else "Light mode")

    def set_glass_label(self, glass_enabled):
        self.glass_lbl.setText("On" if glass_enabled else "Off")

    def set_shortcuts_summary(self, shortcut_keys):
        parts = [f"{label} · {shortcut_keys.get(key, default)}"
                 for key, label, default in SHORTCUT_ACTIONS]
        self.shortcuts_lbl.setText("   |   ".join(parts))

    def set_accent_button_style(self, accent_hex, dim, panel_bg):
        self.btn_accent.setIcon(QIcon())
        self.btn_accent.setStyleSheet(f"""
            QPushButton {{ background: {accent_hex}; color: {contrast_fg(accent_hex)};
                border: none; border-radius: 8px; padding: 7px 14px; font-weight: 600; }}
            QPushButton:hover {{ background: {dim}; }}
        """)
        self.btn_shortcuts.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {accent_hex};
                border: 1px solid {accent_hex}; border-radius: 8px; padding: 6px 12px; font-weight: 600; }}
            QPushButton:hover {{ background: {accent_hex}; color: {contrast_fg(accent_hex)}; }}
        """)

    def toggle(self):
        self._visible = not self._visible
        self._anim.stop()
        self._anim.setStartValue(self.maximumHeight())
        self._anim.setEndValue(self._max_height if self._visible else 0)
        self._anim.start()


# --------------------------------------------------------------------------
# ICON BUTTON HELPER
# --------------------------------------------------------------------------
def make_tool_button(text, icon_name, on_click, object_name=None, tooltip=None):
    btn = HoverToolButton()
    btn.setText(" " + text)
    btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    btn.setIconSize(QSize(18, 18))
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(on_click)
    if object_name:
        btn.setObjectName(object_name)
    if tooltip:
        btn.setToolTip(tooltip)
    btn._icon_name = icon_name
    return btn


# --------------------------------------------------------------------------
# MAIN WINDOW
# --------------------------------------------------------------------------
class FinderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.state = core.FileState()
        self.theme = "dark"
        self.glass_enabled = True
        self.custom_accent = None  # None = use theme's default accent
        self.view_mode = "context"
        self.explorer_visible = True
        self._explorer_saved_width = 300
        self.results_zoom_steps = 0
        self.shortcut_keys = load_shortcuts()
        self._busy_threads = []  # keep QThread/worker refs alive until finished
        self._results_buffer = []

        self.setWindowTitle(f"{APP_NAME} - {APP_TAGLINE}")
        self.resize(1220, 780)
        self.setMinimumSize(880, 560)
        self.setAcceptDrops(True)

        self._tool_buttons = []

        self._build_ui()
        self._install_shortcuts()
        self._refresh_tooltips()
        self._apply_theme(animate=False)
        self._refresh_file_list()
        self._set_status("Ready - press Alt+A or click Add to load a file.", "ok")

    # ------------------------------------------------------------- UI --
    def _build_ui(self):
        self.backdrop = GlassBackdrop(self)
        self.setCentralWidget(self.backdrop)

        root = QWidget(self.backdrop)
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(8)

        def relayout_root():
            root.setGeometry(self.backdrop.rect())
        self.backdrop.resizeEvent = lambda e: relayout_root()

        # ---- brand row ---------------------------------------------------
        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setObjectName("brandIcon")
        self._brand_icon_lbl = brand_icon
        brand_row.addWidget(brand_icon)
        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        name_lbl = QLabel(APP_NAME)
        name_lbl.setObjectName("brand")
        tag_lbl = QLabel(APP_TAGLINE)
        tag_lbl.setObjectName("brandTag")
        brand_col.addWidget(name_lbl)
        brand_col.addWidget(tag_lbl)
        brand_row.addLayout(brand_col)
        brand_row.addStretch()
        outer.addLayout(brand_row)

        # ---- toolbar ---------------------------------------------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_explorer = make_tool_button("", "menu", self.on_toggle_explorer,
                                              tooltip="Hide/show the file list panel")
        self.btn_explorer.setObjectName("viewToggle")
        self.btn_explorer.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_explorer.setFixedSize(34, 34)
        toolbar.addWidget(self.btn_explorer)
        self._tool_buttons.append(self.btn_explorer)

        self.btn_add = make_tool_button("Add", "add", self.on_add,
                                         tooltip="Add file(s) to the working set")
        self.btn_remove = make_tool_button("Remove", "remove", self.on_remove, "danger",
                                            tooltip="Remove the selected file(s) from the working set")
        self.btn_change = make_tool_button("New Set", "change", self.on_change,
                                            tooltip="Clear everything and start a fresh file set")
        self.btn_reload = make_tool_button("Reload", "reload", self.on_reload,
                                            tooltip="Reload every loaded file from disk")
        self.btn_reset = make_tool_button("Reset", "reset", self.on_reset, "warn",
                                           tooltip="Reset search state, or clear everything")
        self.btn_clear = make_tool_button("Clear Results", "clear", self.on_clear_results,
                                           tooltip="Clear the results pane")
        self.btn_details = make_tool_button("Details", "details", self.on_show_details,
                                             tooltip="View full details for every loaded file")
        for b in (self.btn_add, self.btn_remove, self.btn_change, self.btn_reload,
                  self.btn_reset, self.btn_clear, self.btn_details):
            toolbar.addWidget(b)
            self._tool_buttons.append(b)
        toolbar.addStretch()

        self.btn_settings = make_tool_button("Settings", "settings", self.on_toggle_settings,
                                              tooltip="Open theme, glass, accent, and shortcut settings")
        toolbar.addWidget(self.btn_settings)
        self._tool_buttons.append(self.btn_settings)

        outer.addLayout(toolbar)

        # ---- settings (collapsible) ------------------------------------
        self.settings_panel = SettingsPanel(root, self.theme, self.glass_enabled, self.shortcut_keys)
        self.settings_panel.themeChanged.connect(self.set_theme)
        self.settings_panel.glassChanged.connect(self.set_glass)
        self.settings_panel.accentRequested.connect(self.on_pick_accent)
        self.settings_panel.shortcutsRequested.connect(self.on_edit_shortcuts)
        self.btn_accent = self.settings_panel.btn_accent
        outer.addWidget(self.settings_panel)

        # ---- body: crossfade container ---------------------------------
        self.fade_widget = QWidget()
        self.fade_effect = QGraphicsOpacityEffect(self.fade_widget)
        self.fade_widget.setGraphicsEffect(self.fade_effect)
        self.fade_effect.setOpacity(1.0)
        body_outer = QVBoxLayout(self.fade_widget)
        body_outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.fade_widget, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        splitter = self.splitter
        splitter.setHandleWidth(8)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        body_outer.addWidget(splitter)

        # left: file list
        left = QFrame()
        self.left_panel = left
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)

        header_row = QHBoxLayout()
        lbl = QLabel("LOADED FILES")
        lbl.setObjectName("sectionLabel")
        header_row.addWidget(lbl)
        header_row.addStretch()

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_buttons = {}
        for value, icon_name, tip in (
            ("context", "lines", "Detail list view"),
            ("grid", "grid", "Grid view"),
            ("path", "folder", "Group by folder"),
        ):
            vb = HoverToolButton()
            vb.setObjectName("viewToggle")
            vb.setCheckable(True)
            vb.setToolButtonStyle(Qt.ToolButtonIconOnly)
            vb.setIconSize(QSize(15, 15))
            vb.setFixedSize(28, 28)
            vb.setCursor(Qt.PointingHandCursor)
            vb.setToolTip(tip)
            vb._icon_name = icon_name
            self.view_group.addButton(vb)
            self.view_buttons[value] = vb
            self._tool_buttons.append(vb)
            header_row.addWidget(vb)
        self.view_buttons[self.view_mode].setChecked(True)
        self.view_group.buttonToggled.connect(self._on_view_mode_change)

        left_layout.addLayout(header_row)
        self.file_list = QListWidget()
        self.file_list.setToolTip(
            "Files currently loaded for searching - double-click to preview, "
            "select to scope a line-range search. Drag & drop files here to add them."
        )
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)
        left_layout.addWidget(self.file_list, 1)
        self.file_summary = QLabel("0 files loaded")
        self.file_summary.setObjectName("summary")
        left_layout.addWidget(self.file_summary)
        splitter.addWidget(left)
        self._glass_cards = [left]

        # right: search + results - a vertical splitter so the search
        # box and results box can be resized against each other, the
        # same "drag the divider" interaction as the file-explorer
        # splitter on the left.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.setHandleWidth(8)
        right_splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        right_layout.addWidget(right_splitter)

        search_card = QFrame()
        search_card.setObjectName("card")
        sc_layout = QVBoxLayout(search_card)
        sc_layout.setContentsMargins(16, 14, 16, 14)
        self._glass_cards.append(search_card)

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for value, icon_name, label, tip in (
            ("one", "search", "One Line", "Exact literal match on a single line"),
            ("multi", "multi", "Multi Line", "Whitespace/blank-line-insensitive snippet match"),
            ("range", "lines", "Line Range", "Show a specific range of lines from one file"),
        ):
            rb = HoverRadioButton(" " + label)
            rb.setCursor(Qt.PointingHandCursor)
            rb.setToolTip(tip)
            rb._icon_name = icon_name
            self.mode_group.addButton(rb)
            self.mode_buttons[value] = rb
            mode_row.addWidget(rb)
        self.mode_buttons["one"].setChecked(True)
        self.mode_group.buttonToggled.connect(self._on_mode_change)
        mode_row.addStretch()

        self.case_chk = GlassCheckbox("Case sensitive")
        mode_row.addWidget(self.case_chk)
        sc_layout.addLayout(mode_row)

        input_row = QHBoxLayout()
        self.query_entry = QLineEdit()
        self.query_entry.setPlaceholderText("Type exact text to search for...")
        self.query_entry.returnPressed.connect(self.on_search)

        self.query_multiline = QPlainTextEdit()
        self.query_multiline.setPlaceholderText("Paste or type a multi-line snippet...")
        self.query_multiline.setMinimumHeight(84)

        self.range_entry = QLineEdit()
        self.range_entry.setPlaceholderText("e.g. 1500-3000")
        self.range_entry.returnPressed.connect(self.on_search)

        self.search_btn = make_tool_button("Search", "search", self.on_search,
                                            tooltip="Run the search")

        self.input_stack = QStackedWidget()
        self.input_stack.addWidget(self.query_entry)
        self.input_stack.addWidget(self.query_multiline)
        self.input_stack.addWidget(self.range_entry)
        input_row.addWidget(self.input_stack, 1)
        input_row.addWidget(self.search_btn)
        sc_layout.addLayout(input_row)

        right_splitter.addWidget(search_card)

        results_card = QFrame()
        results_card.setObjectName("card")
        rc_layout = QVBoxLayout(results_card)
        rc_layout.setContentsMargins(16, 12, 16, 12)
        self._glass_cards.append(results_card)

        rc_header = QHBoxLayout()
        rlbl = QLabel("RESULTS")
        rlbl.setObjectName("sectionLabel")
        rc_header.addWidget(rlbl)
        rc_header.addStretch()
        self.results_summary = QLabel("")
        self.results_summary.setObjectName("summary")
        rc_header.addWidget(self.results_summary)

        rc_header.addSpacing(14)
        self.btn_zoom_out = make_tool_button("", "zoomout", self.on_zoom_out,
                                              tooltip="Zoom out results  (Ctrl+-)")
        self.btn_zoom_out.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_zoom_out.setFixedSize(28, 28)
        self.zoom_lbl = QLabel("100%")
        self.zoom_lbl.setObjectName("summary")
        self.zoom_lbl.setFixedWidth(38)
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        self.btn_zoom_in = make_tool_button("", "zoomin", self.on_zoom_in,
                                             tooltip="Zoom in results  (Ctrl++)")
        self.btn_zoom_in.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_zoom_in.setFixedSize(28, 28)
        for b in (self.btn_zoom_out, self.btn_zoom_in):
            b.setObjectName("viewToggle")
            self._tool_buttons.append(b)
        rc_header.addWidget(self.btn_zoom_out)
        rc_header.addWidget(self.zoom_lbl)
        rc_header.addWidget(self.btn_zoom_in)
        rc_layout.addLayout(rc_header)

        self.results = QTextEdit()
        self.results.setObjectName("results")
        self.results.setReadOnly(True)
        self.results.setLineWrapMode(QTextEdit.NoWrap)
        rc_layout.addWidget(self.results, 1)

        right_splitter.addWidget(results_card)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([180, 560])

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 900])

        # ---- status bar --------------------------------------------------
        self.status = QLabel("")
        self.status.setObjectName("statusBar")
        outer.addWidget(self.status)

        relayout_root()

    # ---------------------------------------------------------- theming --
    def _effective_theme(self):
        t = dict(THEMES[self.theme])
        if self.custom_accent:
            t["accent"] = self.custom_accent
            t["accent_fg"] = contrast_fg(self.custom_accent)
        return t

    def _apply_theme(self, animate=True):
        def swap():
            t = self._effective_theme()
            self.setStyleSheet(build_stylesheet(t, self.glass_enabled))
            self.backdrop.set_theme(THEMES[self.theme], t["accent"], self.glass_enabled)

            hover_icon_cache = {}

            def hover_icon(name):
                if name not in hover_icon_cache:
                    hover_icon_cache[name] = icon(name, "hover")
                return hover_icon_cache[name]

            for b in self._tool_buttons:
                if b._icon_name in ACCENT_ICON_NAMES and self.custom_accent:
                    normal = tinted_icon(b._icon_name, self.theme, self.custom_accent)
                else:
                    normal = icon(b._icon_name, self.theme)
                b.set_icons(normal, hover_icon(b._icon_name))
            for rb in self.mode_buttons.values():
                if rb._icon_name in ACCENT_ICON_NAMES and self.custom_accent:
                    normal = tinted_icon(rb._icon_name, self.theme, self.custom_accent)
                else:
                    normal = icon(rb._icon_name, self.theme)
                rb.set_icons(normal, hover_icon(rb._icon_name))

            self.settings_panel.set_theme_label(self.theme)
            self.settings_panel.set_glass_label(self.glass_enabled)
            self.settings_panel.set_accent_button_style(t["accent"], t["fg_dim"], t["bg_panel"])
            self.case_chk.set_colors(t["accent"], t["fg"])
            self._refresh_file_list()

            # NOTE: deliberately no QGraphicsDropShadowEffect on these
            # cards - Qt renders a translucent-QSS QFrame blank when a
            # drop-shadow effect sits on it while it also hosts child
            # widgets (splitter panel / list / text edit). The glass
            # look instead comes from the translucent background plus
            # the soft highlight border in build_stylesheet().

            self._brand_icon_lbl.setPixmap(QIcon(APP_ICON_PATH).pixmap(34, 34))
            try:
                self.setWindowIcon(QIcon(APP_ICON_PATH))
            except Exception:
                pass

        if not animate:
            swap()
            return

        fade_out = QPropertyAnimation(self.fade_effect, b"opacity", self)
        fade_out.setDuration(140)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.2)
        fade_out.setEasingCurve(QEasingCurve.InCubic)

        fade_in = QPropertyAnimation(self.fade_effect, b"opacity", self)
        fade_in.setDuration(200)
        fade_in.setStartValue(0.2)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.OutCubic)

        seq = QSequentialAnimationGroup(self)
        seq.addAnimation(fade_out)
        seq.addAnimation(fade_in)
        fade_out.finished.connect(swap)
        seq.start(QPropertyAnimation.DeleteWhenStopped)
        self._theme_anim = seq

    def set_theme(self, theme):
        if theme == self.theme:
            return
        self.theme = theme
        self._apply_theme(animate=True)
        self._set_status(f"Switched to {theme} mode.", "ok")

    def set_glass(self, enabled):
        if enabled == self.glass_enabled:
            return
        self.glass_enabled = enabled
        self._apply_theme(animate=True)
        self._set_status(f"Glass effect turned {'on' if enabled else 'off'}.", "ok")

    def on_pick_accent(self):
        current = self.custom_accent or THEMES[self.theme]["accent"]
        chosen = AccentColorPicker.pick(self, self._effective_theme(), current)
        if chosen:
            self.custom_accent = chosen
            self._apply_theme(animate=True)
            self._set_status(f"Accent color set to {chosen}.", "ok")

    def on_toggle_settings(self):
        self.settings_panel.toggle()

    def on_toggle_explorer(self):
        self.explorer_visible = not self.explorer_visible
        if self.explorer_visible:
            self.left_panel.show()
            sizes = self.splitter.sizes()
            total = sum(sizes) or 1
            self.splitter.setSizes([max(240, self._explorer_saved_width), total - max(240, self._explorer_saved_width)])
        else:
            sizes = self.splitter.sizes()
            self._explorer_saved_width = sizes[0] if sizes and sizes[0] > 0 else 300
            self.left_panel.hide()

    # ---------------------------------------------------------- shortcuts --
    def _install_shortcuts(self):
        handlers = {
            "search": self.on_search,
            "add": self.on_add,
            "reload": self.on_reload,
            "reset": self.on_reset,
            "clear_results": self.on_clear_results,
            "case_sensitive": lambda: self.case_chk.setChecked(not self.case_chk.isChecked()),
        }
        self._shortcut_objs = {}
        for key, _label, default in SHORTCUT_ACTIONS:
            sc = QShortcut(QKeySequence(self.shortcut_keys.get(key, default)), self)
            sc.activated.connect(handlers[key])
            self._shortcut_objs[key] = sc
        # Numpad Enter sends a different key than the main Return key in
        # Qt, so Ctrl+Enter needs its own (non-editable) binding to the
        # same search action alongside the editable "search" shortcut.
        numpad = QShortcut(QKeySequence("Ctrl+Enter"), self)
        numpad.activated.connect(self.on_search)
        self._shortcut_objs["search_numpad"] = numpad

        # Results zoom - fixed, not part of the editable shortcut list.
        for seq in ("Ctrl+=", "Ctrl++"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(self.on_zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self.on_zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self.on_zoom_reset)

    def _apply_shortcut(self, key, sequence_text):
        self.shortcut_keys[key] = sequence_text
        if key in self._shortcut_objs:
            self._shortcut_objs[key].setKey(QKeySequence(sequence_text))
        save_shortcuts(self.shortcut_keys)

    def _refresh_tooltips(self):
        keys = self.shortcut_keys
        self.btn_add.setToolTip(f"Add file(s) to the working set  ({keys['add']})")
        self.btn_reload.setToolTip(f"Reload every loaded file from disk  ({keys['reload']})")
        self.btn_reset.setToolTip(f"Reset search state, or clear everything  ({keys['reset']})")
        self.btn_clear.setToolTip(f"Clear the results pane  ({keys['clear_results']})")
        self.search_btn.setToolTip(f"Run the search  ({keys['search']})")
        self.case_chk.setToolTip(f"Toggle case-sensitive matching  ({keys['case_sensitive']})")

    def on_edit_shortcuts(self):
        result = ShortcutsDialog.edit(self, self._effective_theme(), self.glass_enabled, self.shortcut_keys)
        if result:
            for key, seq in result.items():
                self._apply_shortcut(key, seq)
            self._refresh_tooltips()
            self.settings_panel.set_shortcuts_summary(self.shortcut_keys)
            self._set_status("Shortcuts updated.", "ok")

    # ---------------------------------------------------------- status --
    def _set_status(self, text, kind="dim"):
        t = self._effective_theme()
        color = {"ok": t["ok"], "warn": t["warn"], "error": t["danger"], "dim": t["fg_dim"]}.get(kind, t["fg_dim"])
        self.status.setText(text)
        panel_bg = with_alpha(t["bg_panel"], 130) if self.glass_enabled else t["bg_panel"]
        self.status.setStyleSheet(f"color: {color}; background: {panel_bg}; padding: 6px 12px; border-radius: 8px;")

    def _refresh_file_list(self):
        """
        Renders self.state.files into the left panel according to
        self.view_mode:
          - "context": one row per file with line count/size/encoding/
            modified time - the original detailed list.
          - "grid": file tiles in an icon grid.
          - "path": files grouped under a non-selectable header item per
            parent directory, so files that share a folder (e.g. several
            .txt files loaded from the same place) read as one section.
        Every real file item carries its path in Qt.UserRole so
        selection-dependent actions (remove, line-range target) work
        the same regardless of which view is active.
        """
        self.file_list.clear()
        t = self._effective_theme()
        accent = t["accent"]
        file_icon = (tinted_icon("file", self.theme, accent) if self.custom_accent
                     else icon("file", self.theme))
        folder_icon = icon("folder", self.theme)
        mode = self.view_mode

        if mode == "grid":
            self.file_list.setViewMode(QListWidget.IconMode)
            self.file_list.setFlow(QListWidget.LeftToRight)
            self.file_list.setWrapping(True)
            self.file_list.setResizeMode(QListWidget.Adjust)
            self.file_list.setIconSize(QSize(40, 40))
            self.file_list.setSpacing(12)
            self.file_list.setMovement(QListWidget.Static)
            for entry in self.state.files:
                item = QListWidgetItem(file_icon, os.path.basename(entry.path))
                item.setData(Qt.UserRole, entry.path)
                item.setToolTip(entry.path)
                item.setTextAlignment(Qt.AlignHCenter)
                self.file_list.addItem(item)

        elif mode == "path":
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setFlow(QListWidget.TopToBottom)
            self.file_list.setWrapping(False)
            self.file_list.setIconSize(QSize(16, 16))
            self.file_list.setSpacing(0)
            groups = {}
            for entry in self.state.files:
                groups.setdefault(os.path.dirname(entry.path) or ".", []).append(entry)
            for directory in sorted(groups):
                header = QListWidgetItem(folder_icon, f"  {directory}  ({len(groups[directory])})")
                header.setFlags(Qt.ItemIsEnabled)
                f = header.font()
                f.setBold(True)
                header.setFont(f)
                header.setForeground(QColor(t["fg_dim"]))
                self.file_list.addItem(header)
                for entry in groups[directory]:
                    size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"
                    item = QListWidgetItem(
                        file_icon, f"      {os.path.basename(entry.path)}   {len(entry.lines):,} lines - {size}")
                    item.setData(Qt.UserRole, entry.path)
                    item.setToolTip(entry.path)
                    self.file_list.addItem(item)

        else:  # "context" - detailed list (default)
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setFlow(QListWidget.TopToBottom)
            self.file_list.setWrapping(False)
            self.file_list.setIconSize(QSize(20, 20))
            self.file_list.setSpacing(2)
            for entry in self.state.files:
                size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"
                try:
                    modified = datetime.datetime.fromtimestamp(
                        os.path.getmtime(entry.path)).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    modified = "?"
                item = QListWidgetItem(
                    file_icon,
                    f"{os.path.basename(entry.path)}\n"
                    f"{len(entry.lines):,} lines - {size} - {entry.encoding} - {modified}",
                )
                item.setData(Qt.UserRole, entry.path)
                item.setToolTip(entry.path)
                self.file_list.addItem(item)

        n = len(self.state.files)
        self.file_summary.setText(f"{n} file{'s' if n != 1 else ''} loaded")

    def _on_view_mode_change(self, button, checked):
        if not checked:
            return
        for value, b in self.view_buttons.items():
            if b is button:
                self.view_mode = value
                break
        self._refresh_file_list()

    def _selected_entries(self):
        """Loaded files currently selected in the file list, resolved by
        path (set on every real item's Qt.UserRole) rather than row
        index - the path/grid views insert header items or reorder rows,
        so row index alone can't identify which file was picked."""
        paths = {item.data(Qt.UserRole) for item in self.file_list.selectedItems()
                 if item.data(Qt.UserRole)}
        return [entry for entry in self.state.files if entry.path in paths]

    # ---------------------------------------------------------- background work --
    def _run_busy(self, work_fn, on_success, busy_text="Working..."):
        """Run work_fn() off the UI thread; call on_success(result) back on
        the UI thread when done. Keeps the window responsive (no "Not
        Responding") during slow disk I/O or large regex scans."""
        self.setCursor(Qt.WaitCursor)
        self._set_status(busy_text, "dim")
        self.setEnabled(False)

        thread = QThread(self)
        worker = _AsyncWorker(work_fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def _cleanup():
            self.setEnabled(True)
            self.unsetCursor()
            thread.quit()
            thread.wait()
            self._busy_threads.remove((thread, worker))

        def _on_finished(result):
            _cleanup()
            on_success(result)

        def _on_failed(tb_text):
            _cleanup()
            self._error("Unexpected error", tb_text)
            self._set_status("Action failed - see error details.", "error")

        worker.finished.connect(_on_finished)
        worker.failed.connect(_on_failed)
        self._busy_threads.append((thread, worker))
        thread.start()

    # ---------------------------------------------------------- dialogs helper --
    def _dialog(self, kind, title, message, buttons=None):
        return AnimatedDialog.show(self, self._effective_theme(), self.theme, self.glass_enabled,
                                    kind, title, message, buttons)

    def _info(self, title, message):
        self._dialog("info", title, message)

    def _error(self, title, message):
        self._dialog("error", title, message)

    def _confirm(self, title, message, yes_label="Yes", no_label="Cancel"):
        return self._dialog("question", title, message,
                             buttons=[(no_label, False, False), (yes_label, True, True)])

    # ---------------------------------------------------------- drag & drop --
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_status("Drop to add file(s)...", "ok")

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_status("Ready.", "dim")

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            self._set_status("Nothing droppable - only local files can be added this way.", "warn")
            return
        def work():
            ok_count, fail_msgs = 0, []
            for p in paths:
                ok, msg = core.add_file_to_state(p, self.state)
                if ok:
                    ok_count += 1
                else:
                    fail_msgs.append(msg)
            return ok_count, fail_msgs

        def done(result):
            ok_count, fail_msgs = result
            self._refresh_file_list()
            if fail_msgs:
                self._error("Some dropped files failed to load", "\n".join(fail_msgs))
            if ok_count:
                self._set_status(f"Added {ok_count} file(s) via drag & drop.", "ok")
            else:
                self._set_status("Ready.", "dim")

        self._run_busy(work, done, "Loading dropped file(s)...")
        event.acceptProposedAction()

    # ---------------------------------------------------------- toolbar --
    def on_add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to add")
        if not paths:
            return
        def work():
            ok_count, fail_msgs = 0, []
            for p in paths:
                ok, msg = core.add_file_to_state(p, self.state)
                if ok:
                    ok_count += 1
                else:
                    fail_msgs.append(msg)
            return ok_count, fail_msgs

        def done(result):
            ok_count, fail_msgs = result
            self._refresh_file_list()
            if fail_msgs:
                self._error("Some files failed to load", "\n".join(fail_msgs))
            if ok_count:
                self._set_status(f"Added {ok_count} file(s).", "ok")
            else:
                self._set_status("Ready.", "dim")

        self._run_busy(work, done, "Loading file(s)...")

    def on_remove(self):
        selected = self._selected_entries()
        if not selected:
            self._info("Remove file", "Select one or more files in the list first.")
            return
        names = [os.path.basename(e.path) for e in selected]
        if not self._confirm(
            "Remove file(s)",
            f"Remove {len(selected)} file(s) from the loaded set?\n\n" + "\n".join(names),
            yes_label="Remove",
        ):
            return
        self.state.files = [e for e in self.state.files if e not in selected]
        self.state.reset_search()
        self._refresh_file_list()
        self._set_status(f"Removed {len(selected)} file(s).", "warn")

    def on_change(self):
        if self.state.files and not self._confirm(
            "Start a new file set",
            "This clears every currently loaded file and lets you pick a fresh set. Continue?",
            yes_label="Continue",
        ):
            return
        self.state.clear_files()
        self._refresh_file_list()
        self._clear_results()
        self.on_add()

    def on_reload(self):
        if not self.state.is_loaded():
            self._set_status("No files loaded.", "warn")
            return
        def work():
            errors = []
            new_files = list(self.state.files)
            for i, entry in enumerate(new_files):
                new_entry, msg = core.load_single_file(entry.path)
                if new_entry:
                    new_files[i] = new_entry
                else:
                    errors.append(msg)
            return new_files, errors

        def done(result):
            new_files, errors = result
            self.state.files = new_files
            self.state.reset_search()
            self._refresh_file_list()
            if errors:
                self._error("Reload finished with errors", "\n".join(errors))
                self._set_status("Reload finished with errors.", "error")
            else:
                self._set_status(f"Reloaded {len(self.state.files)} file(s) from disk.", "ok")

        self._run_busy(work, done, "Reloading file(s) from disk...")

    def on_reset(self):
        if not self._confirm(
            "Reset search state",
            "This clears the current search state (your last search term/snippet "
            "will be forgotten). This cannot be undone.",
            yes_label="Reset",
        ):
            self._set_status("Reset cancelled.", "dim")
            return

        if self.state.is_loaded() and self._confirm(
            "Reset scope",
            f"Also remove ALL {len(self.state.files)} loaded file(s) and start over?\n\n"
            f"Yes = remove everything and relaunch file selection.\n"
            f"No = just reset the search state, keep files loaded.",
            yes_label="Remove all",
        ):
            self.state.clear_files()
            self._refresh_file_list()
            self._clear_results()
            self._set_status("All files removed - relaunching file selection.", "warn")
            self.on_add()
            return

        self.state.reset_search()
        self._clear_results()
        self._set_status("Search state reset.", "ok")

    def on_show_details(self):
        if not self.state.is_loaded():
            self._info("File Details", "No files loaded yet - add some first.")
            return
        FileDetailsDialog.show_details(self, self._effective_theme(), self.state.files)

    def on_clear_results(self):
        self._clear_results()
        self._set_status("Results cleared.", "dim")

    def _clear_results(self):
        self.results.clear()
        self.results_summary.setText("")
        self._results_buffer = []

    # ---------------------------------------------------------- zoom --
    # QTextEdit.setFont() alone doesn't work here: the global stylesheet
    # sets font-family on #results, and once a widget has any styled
    # font property Qt's style engine keeps re-resolving the widget's
    # font on every polish, silently discarding a plain setFont() point
    # size change. QTextEdit/QPlainTextEdit's built-in zoomIn()/zoomOut()
    # bypass that - they scale the document's rendered font size
    # directly - so those are used instead, with a step counter just for
    # the on-screen "N%" label and for zooming back out exactly on reset.
    ZOOM_MIN_STEPS, ZOOM_MAX_STEPS = -5, 20

    def on_zoom_in(self):
        if self.results_zoom_steps >= self.ZOOM_MAX_STEPS:
            return
        self.results.zoomIn(1)
        self.results_zoom_steps += 1
        self._update_zoom_label()

    def on_zoom_out(self):
        if self.results_zoom_steps <= self.ZOOM_MIN_STEPS:
            return
        self.results.zoomOut(1)
        self.results_zoom_steps -= 1
        self._update_zoom_label()

    def on_zoom_reset(self):
        if self.results_zoom_steps > 0:
            self.results.zoomOut(self.results_zoom_steps)
        elif self.results_zoom_steps < 0:
            self.results.zoomIn(-self.results_zoom_steps)
        self.results_zoom_steps = 0
        self._update_zoom_label()

    def _update_zoom_label(self):
        self.zoom_lbl.setText(f"{100 + self.results_zoom_steps * 10}%")

    # ---------------------------------------------------------- preview --
    def _on_file_double_clicked(self, item):
        path = item.data(Qt.UserRole)
        if not path:
            return
        entry = next((e for e in self.state.files if e.path == path), None)
        if entry is None:
            return
        PreviewDialog.preview(self, self._effective_theme(), self.theme, self.glass_enabled, entry)

    # ---------------------------------------------------------- search --
    def _on_mode_change(self, button, checked):
        if not checked:
            return
        index = {"one": 0, "multi": 1, "range": 2}[self._mode_value()]
        self.input_stack.setCurrentIndex(index)

    def _mode_value(self):
        for value, rb in self.mode_buttons.items():
            if rb.isChecked():
                return value
        return "one"

    def on_search(self):
        if not self.state.is_loaded():
            self._info("No files loaded", "Add at least one file first.")
            return

        # auto_reload_check() stats (and possibly re-reads) every loaded
        # file from disk on every single search - with several/large
        # files that disk I/O was the other big source of the "search
        # freezes the window" reports, since it ran synchronously on the
        # UI thread before any regex matching even started.
        def work():
            core.auto_reload_check(self.state)

        def done(_):
            self._refresh_file_list()
            mode = self._mode_value()
            if mode == "one":
                self._search_one_line()
            elif mode == "multi":
                self._search_multi_line()
            else:
                self._search_line_range()

        self._run_busy(work, done, "Checking files for changes...")

    def _t(self):
        return self._effective_theme()

    def _write(self, text, color=None, bold=False, mono=True, bg=None):
        text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br>"))
        style = []
        if color:
            style.append(f"color:{color}")
        if bg:
            style.append(f"background-color:{bg}")
        if bold:
            style.append("font-weight:600")
        if mono:
            style.append(f"font-family:'{FONT_MONO}'")
        style_attr = f' style="{";".join(style)}"' if style else ""
        # Buffered rather than a live self.results.insertHtml() call: each
        # insertHtml() forces QTextEdit to re-lay out the whole document,
        # so calling it per match/line (thousands of times for a big line
        # range or a common search term) turned into an O(n^2) UI stall -
        # the "becomes unresponsive" reports. One flush() at the end is
        # a single layout pass no matter how many lines were written.
        self._results_buffer.append(f"<span{style_attr}>{text}</span>")

    def _flush_results(self):
        if self._results_buffer:
            self.results.insertHtml("".join(self._results_buffer))
            self._results_buffer = []

    def _search_one_line(self):
        term = self.query_entry.text()
        if not term:
            self._info("Empty search", "Type something to search for.")
            return
        flags = 0 if self.case_chk.isChecked() else re.IGNORECASE
        pattern = re.compile(re.escape(term), flags)
        self.state.last_search = f"one: '{term}'"

        self._clear_results()
        total, files_hit = 0, 0
        for i, entry in enumerate(self.state.files, start=1):
            results = core._find_matches(entry.lines, pattern, multiline=False)
            if results:
                total += len(results)
                files_hit += 1
            self._render_file_card(i, len(self.state.files), entry, results)
        self._finish_search(total, files_hit)

    def _search_multi_line(self):
        snippet = self.query_multiline.toPlainText()
        if not snippet.strip():
            self._info("Empty search", "Type or paste a snippet to search for.")
            return
        flags = 0 if self.case_chk.isChecked() else re.IGNORECASE
        try:
            pattern_as_typed = re.compile(core.build_flexible_pattern(snippet), flags)
        except re.error as e:
            self._error("Pattern error", str(e))
            return

        pattern_stripped_snippet = None
        if core.has_comments(snippet):
            stripped = core.COMMENT_MARKER_RE.sub("", snippet)
            if stripped.strip():
                try:
                    pattern_stripped_snippet = re.compile(core.build_flexible_pattern(stripped), flags)
                except re.error:
                    pattern_stripped_snippet = None

        self.state.last_search = f"multi: {snippet!r}"
        self._clear_results()
        total, files_hit = 0, 0
        for i, entry in enumerate(self.state.files, start=1):
            results, note = core._search_file_with_comment_fallback(
                entry, pattern_as_typed, pattern_stripped_snippet)
            if results:
                total += len(results)
                files_hit += 1
            self._render_file_card(i, len(self.state.files), entry, results, note=note)
        self._finish_search(total, files_hit)

    def _search_line_range(self):
        entry = self._pick_range_file()
        if entry is None:
            return
        raw = self.range_entry.text()
        parsed = core.parse_range(raw, len(entry.lines))
        if not parsed:
            self._error("Invalid range", "Use e.g. '1500-3000' or a single line number.")
            return
        start, end = parsed
        if start > len(entry.lines) or end < 1:
            self._error("Out of bounds", f"File only has {len(entry.lines)} lines.")
            return

        # A big range (e.g. an entire large file) still froze the window
        # even after buffering: building one <span> pair of HTML per line
        # for tens/hundreds of thousands of lines makes QTextEdit's HTML
        # parser itself the bottleneck, not the insertHtml() call count.
        # Plain text has no per-line markup to parse, so build and insert
        # the body as plain text instead - the string building also runs
        # off the UI thread since it's pure Python with no widget access.
        def work():
            width = len(str(len(entry.lines)))
            lines = entry.lines[start - 1:end]
            return "\n".join(
                f"{str(start + i).rjust(width)}: {line}" for i, line in enumerate(lines)
            )

        def done(body):
            self._clear_results()
            t = self._t()
            self._write(f"{os.path.basename(entry.path)}: lines {start}-{end}\n", color=t["secondary"], bold=True)
            self._write(f"{entry.path}\n\n", color=t["fg_dim"])
            self._flush_results()
            self.results.insertPlainText(body)
            self.results_summary.setText(f"{end - start + 1} line(s) shown")
            self._set_status(f"Showing lines {start}-{end} of {os.path.basename(entry.path)}.", "ok")

        self._run_busy(work, done, "Loading line range...")

    def _pick_range_file(self):
        if len(self.state.files) == 1:
            return self.state.files[0]
        selected = self._selected_entries()
        if selected:
            return selected[0]
        self._info("Pick a file",
                   "Select which loaded file to show a line range from in the file list on the left.")
        return None

    def _render_file_card(self, index, total, entry, results, note=None):
        t = self._t()
        name = os.path.basename(entry.path)
        if not results:
            self._write(f"  [{index}/{total}] {name} - no matches\n", color=t["fg_dim"])
            return

        count = len(results)
        label = f"{count} match" if count == 1 else f"{count} matches"
        self._write(f"\n[{index}/{total}] ", color=t["fg_dim"])
        self._write(f"{name}", color=t["accent"], bold=True)
        self._write("  -  ")
        self._write(f"{label}\n", color=t["ok"])
        self._write(f"  {entry.path}\n", color=t["fg_dim"])
        if note:
            self._write(f"  ({note})\n", color=t["warn"])

        width = len(str(max(len(entry.lines), 1)))
        multiple = len(results) > 1
        for n, item in enumerate(results, start=1):
            if multiple:
                self._write(f"  -- match {n}/{len(results)} --\n", color=t["fg_dim"])
            if item[0] == "single":
                _, line_no, line_text, spans = item
                self._write_match_line(line_no, line_text, spans, width, indent="  ")
            else:
                _, start_no, end_no, per_line = item
                self._write(f"  Lines {start_no}-{end_no}:\n", color=t["accent"], bold=True)
                for li_no, line_text, spans in per_line:
                    self._write_match_line(li_no, line_text, spans, width, indent="    ")

    def _write_match_line(self, line_no, line_text, spans, width, indent):
        t = self._t()
        num_str = str(line_no).rjust(width)
        self._write(f"{indent}{num_str}: ", color=t["accent"])
        if not spans:
            self._write(f"{line_text}\n")
            return
        cursor = 0
        for start, end in spans:
            self._write(line_text[cursor:start])
            self._write(line_text[start:end], color=t["hl_fg"], bg=t["hl_bg"], bold=True)
            cursor = end
        self._write(line_text[cursor:] + "\n")

    def _finish_search(self, total, files_hit):
        n = len(self.state.files)
        if total:
            self.results_summary.setText(f"{total} match(es) in {files_hit}/{n} file(s)")
            self._set_status(f"Found {total} matching occurrence(s) in {files_hit}/{n} file(s).", "ok")
        else:
            self.results_summary.setText("0 matches")
            self._set_status("No matches found in any loaded file.", "warn")
            self._write("No matches found in any loaded file.\n", color=self._t()["warn"])
        self._flush_results()


def main():
    _install_global_error_handling()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(APP_ICON_PATH))
    # Base font size is set here, in code, rather than as `font-size` in
    # the QSS `QWidget {}` rule: a literal font-size on that universal
    # selector gets re-asserted by Qt's style engine on every polish,
    # which silently overrides any later setFont() call - including the
    # one QTextEdit.zoomIn()/zoomOut() make internally - so the results
    # pane's zoom controls looked like they did nothing.
    app.setFont(QFont(FONT_UI, 10))
    win = FinderWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
