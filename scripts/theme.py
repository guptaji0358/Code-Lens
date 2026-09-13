"""
theme.py - colors, stylesheet, and icon helpers shared across CodeLens's
GUI. Split out of CodeLens.py so the visual/theming layer can be read
and changed without wading through FinderWindow.
"""

import os
import sys

from PySide6.QtCore import Qt, QFileInfo
from PySide6.QtGui import QIcon, QPainter, QColor, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFileIconProvider


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
ACCENT_ICON_NAMES = {"search", "multi", "lines", "info", "add", "reload", "clear", "grid", "filter"}

_FILE_ICON_PROVIDER = QFileIconProvider()
_FILE_TYPE_ICON_CACHE = {}  # extension (lowercase, with dot) -> QIcon


def real_file_icon(path):
    """
    The actual Windows shell icon registered for this file's extension -
    Notepad's icon for a .txt, VS Code's for a .py if it's the registered
    handler, the generic blank-page icon for an unknown extension, etc. -
    instead of one generic icon for every file regardless of type.
    Cached per-extension since QFileIconProvider hits the shell each call
    and the file list can have hundreds of rows sharing a handful of
    extensions.
    """
    ext = os.path.splitext(path)[1].lower()
    cached = _FILE_TYPE_ICON_CACHE.get(ext)
    if cached is not None:
        return cached
    qicon = _FILE_ICON_PROVIDER.icon(QFileInfo(path))
    _FILE_TYPE_ICON_CACHE[ext] = qicon
    return qicon


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
    /* Group-by-folder ("path") view rows paint their own flat, edge-to-edge
       selection fill (TreeGuideRow.paintEvent) since they're setItemWidget
       rows the rules above can't reach anyway - but those rules still paint
       their inset/rounded/colored background *behind* every such row
       regardless, which showed through as a floating pill with a shadow
       layered under the row's real highlight. Zero them out only while the
       path view is active (main_window.py toggles this property on view-mode
       switch) so TreeGuideRow's own paint is the only selection drawn. */
    QListWidget#fileList[pathMode="true"]::item {{ padding: 0px; border-radius: 0px; background: transparent; }}
    QListWidget#fileList[pathMode="true"]::item:selected {{ background: transparent; color: {t['fg']}; }}
    QListWidget#fileList[pathMode="true"]::item:hover:!selected {{ background: transparent; }}
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
    QProgressBar#loadProgress {{
        background: {input_bg}; border: 1px solid {border}; border-radius: 3px;
    }}
    QProgressBar#loadProgress::chunk {{ background: {accent}; border-radius: 3px; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; border-radius: 6px; }}
    QScrollBar::handle:vertical {{ background: {border}; border-radius: 6px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {accent}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def gradient_style(t):
    return (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {t['bg_a']}, stop:1 {t['bg_b']});")
