#!/usr/bin/env python3
"""
installer_app.py - CodeLens Setup

A custom PySide6 installer/uninstaller for CodeLens, styled to match the
app it installs (dark glass theme, same accent palette, same icon) instead
of a generic wizard skin. One executable serves both roles: run normally
to install, run with --uninstall (which the installer registers as the
Windows "Uninstall" entry) to remove everything it put down.

Author: Robin Gupta
Run:    python installer_app.py            (install wizard)
        python installer_app.py --uninstall (uninstall flow)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import winreg

from PySide6.QtCore import (
    Qt, QThread, Signal, QSize, QPropertyAnimation, QEasingCurve, QRectF, QPointF, Property,
    QTimer,
)
from PySide6.QtGui import QIcon, QPainter, QColor, QLinearGradient, QFont, QPen
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QFileDialog, QStackedWidget, QFrame, QTextEdit,
    QProgressBar, QAbstractButton, QSizePolicy, QMessageBox,
)

APP_NAME = "CodeLens"
APP_VERSION = "1.3.0"
APP_PUBLISHER = "Robin Gupta Studio (RGSTM)"
APP_COPYRIGHT = "Copyright \u00a9 Robin Gupta"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodeLens"
# "Open with CodeLens" File Explorer context menu, registered per-user under
# Classes\* so it shows up for any file type without needing admin rights.
CONTEXT_MENU_KEY = r"Software\Classes\*\shell\CodeLens"
# "Open folder with CodeLens" on a right-clicked folder - CodeLens.py
# expands a folder argument into every text-based file under it.
DIR_CONTEXT_MENU_KEY = r"Software\Classes\Directory\shell\CodeLens"
# Right-clicking empty space *inside* an open folder (background) and
# right-clicking empty space on the Desktop are two separate shell
# classes from a regular folder item - both use %V (current folder)
# rather than %1.
DIR_BG_CONTEXT_MENU_KEY = r"Software\Classes\Directory\Background\shell\CodeLens"
DESKTOP_BG_CONTEXT_MENU_KEY = r"Software\Classes\DesktopBackground\Shell\CodeLens"
# Windows 11's compact context menu hides every classic/unpackaged verb
# (ours included) behind "Show more options" unless this CLSID's
# InprocServer32 default is blanked out - a well-known, reversible
# per-user tweak that restores the classic full menu everywhere.
CLASSIC_MENU_CLSID_KEY = r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32"

COMPONENTS = [
    ("desktop", "CodeLens", "Desktop app - glassy PySide6 GUI for searching files visually.", "CodeLens.exe"),
    ("cli", "CodeLensCLI", "Terminal app - fast console tool, no GUI dependencies.", "CodeLensCLI.exe"),
]
# Shortcut display names, shared between install (creating them) and
# uninstall (finding them again to remove) so the two can never drift.
COMPONENT_LABELS = {"desktop": APP_NAME, "cli": f"{APP_NAME} CLI"}


# --------------------------------------------------------------------------
# RESOURCE RESOLUTION (frozen-exe safe)
# --------------------------------------------------------------------------
def _resource_base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # Source runs live in scripts/, but Assets/ and LICENSE.txt still live
    # next to this script's old home in Installer/.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Installer")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _resource_base_dir()
ASSETS_DIR = os.path.join(BASE_DIR, "Assets")
LICENSE_PATH = os.path.join(BASE_DIR, "LICENSE.txt")
ICON_PATH = os.path.join(ASSETS_DIR, "app_icon.ico")
# Built app folders and the uninstaller .exe live in out/ at the repo root
# when running from source; a frozen installer has them bundled next to
# its own exe instead.
_OUT_DIR = os.path.join(_repo_root(), "out") if not getattr(sys, "frozen", False) else BASE_DIR
PAYLOAD_DIR = _OUT_DIR if not getattr(sys, "frozen", False) else os.path.join(BASE_DIR, "payload")
# A separate, payload-free build of this same script, bundled inside the
# full installer just so it has something small to hand off as the
# uninstaller (see the _write_uninstall_registry comment for why).
UNINSTALLER_SRC = (
    os.path.join(_OUT_DIR, "CodeLens_Uninstall.exe") if not getattr(sys, "frozen", False)
    else os.path.join(BASE_DIR, "uninstaller", "CodeLens_Uninstall.exe")
)


# --------------------------------------------------------------------------
# THEME - same tokens as CodeLens.py's dark glass palette
# --------------------------------------------------------------------------
BG_A = "#0b0e1a"
BG_B = "#1a1030"
PANEL = "#161925"
CARD = "#1d2130"
CARD_HOVER = "#262c40"
BORDER = "#2a2f42"
FG = "#e6e9f0"
FG_DIM = "#8a8fa3"
ACCENT = "#4fd1ff"
ACCENT_INK = "#0b0d13"
SECONDARY = "#8b8bff"
OK = "#5be38a"
DANGER = "#ff6b6b"
FONT_UI = "Segoe UI"

STYLESHEET = f"""
QWidget {{ color: {FG}; font-family: '{FONT_UI}'; font-size: 10pt; background: transparent; }}
QFrame#sidebar {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#stepLabel {{ padding: 10px 16px; border-radius: 8px; color: {FG_DIM}; font-weight: 600; }}
QLabel#stepLabelActive {{ padding: 10px 16px; border-radius: 8px; color: {ACCENT_INK};
    background: {ACCENT}; font-weight: 700; }}
QLabel#stepLabelDone {{ padding: 10px 16px; border-radius: 8px; color: {OK}; font-weight: 600; }}
QLabel#title {{ font-size: 17pt; font-weight: 700; color: {FG}; }}
QLabel#subtitle {{ font-size: 10.5pt; color: {FG_DIM}; }}
QLineEdit {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QTextEdit {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px; }}
QPushButton {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 9px 20px; font-weight: 600; }}
QPushButton:hover {{ background: {CARD_HOVER}; }}
QPushButton:disabled {{ color: {FG_DIM}; background: {PANEL}; }}
QPushButton#primary {{ background: {ACCENT}; color: {ACCENT_INK}; border: none; }}
QPushButton#primary:hover {{ background: #7fe0ff; }}
QPushButton#primary:disabled {{ background: {BORDER}; color: {FG_DIM}; }}
QPushButton#danger {{ background: {DANGER}; color: white; border: none; }}
QProgressBar {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    text-align: center; color: {FG}; height: 22px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 7px; }}
"""


class GlassBackdrop(QWidget):
    """Gradient + soft color blobs behind the wizard, same visual language
    as CodeLens.py's own GlassBackdrop - keeps the installer's first
    impression consistent with the app it's about to install."""

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        grad = QLinearGradient(0, 0, rect.width(), rect.height())
        grad.setColorAt(0, QColor(BG_A))
        grad.setColorAt(1, QColor(BG_B))
        p.fillRect(rect, grad)

        def blob(cx, cy, r, color, alpha):
            c = QColor(color)
            c.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

        w, h = rect.width(), rect.height()
        blob(w * 0.1, h * 0.85, max(w, h) * 0.32, ACCENT, 40)
        blob(w * 0.95, h * 0.1, max(w, h) * 0.26, SECONDARY, 35)


class GlassCheckbox(QAbstractButton):
    """Same animated rounded checkbox as CodeLens.py's GlassCheckbox,
    reimplemented standalone so the installer has no runtime dependency
    on the app it's installing."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._text = text
        self._progress = 0.0
        self._anim = QPropertyAnimation(self, b"checkProgress", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutBack)
        self.toggled.connect(self._animate)
        self.setMinimumHeight(24)

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def getCheckProgress(self):
        return self._progress

    def setCheckProgress(self, v):
        self._progress = max(0.0, min(1.0, v))
        self.update()

    checkProgress = Property(float, getCheckProgress, setCheckProgress)

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(20 + 8 + fm.horizontalAdvance(self._text) + 6, max(24, fm.height() + 6))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = 19
        box_rect = QRectF(1, (self.height() - box) / 2, box, box)
        prog = self._progress
        base = QColor(255, 255, 255, 30)
        accent = QColor(ACCENT)
        fill = QColor(
            round(base.red() + (accent.red() - base.red()) * prog),
            round(base.green() + (accent.green() - base.green()) * prog),
            round(base.blue() + (accent.blue() - base.blue()) * prog),
            round(base.alpha() + (255 - base.alpha()) * prog),
        )
        border = accent if prog > 0.05 else QColor(255, 255, 255, 90)
        p.setPen(QPen(border, 1.6))
        p.setBrush(fill)
        p.drawRoundedRect(box_rect, 5, 5)
        if prog > 0.02:
            pen = QPen(QColor(ACCENT_INK), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setOpacity(min(1.0, prog * 1.3))
            l, t = box_rect.left(), box_rect.top()
            p.drawPolyline([QPointF(l + 4, t + 9.5), QPointF(l + 7.5, t + 13), QPointF(l + 14, t + 5)])
            p.setOpacity(1.0)
        p.setPen(QColor(FG))
        p.drawText(QRectF(box + 8, 0, self.width() - box - 8, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)


def card(inner_layout=None):
    f = QFrame()
    f.setObjectName("card")
    if inner_layout:
        f.setLayout(inner_layout)
    return f


# --------------------------------------------------------------------------
# INSTALL STATE
# --------------------------------------------------------------------------
class InstallState:
    def __init__(self):
        self.components = {key: True for key, *_ in COMPONENTS}
        self.install_dir = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\Public"),
                                         "Programs", APP_NAME)
        self.desktop_shortcut = True
        self.startmenu_shortcut = True
        self.context_menu = True
        self.classic_context_menu = True
        self.license_accepted = False
        self.launch_after = True


# --------------------------------------------------------------------------
# WIZARD PAGES
# --------------------------------------------------------------------------
class WelcomePage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 20, 0, 0)
        lay.setSpacing(14)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(ICON_PATH).pixmap(72, 72))
        lay.addWidget(icon_lbl)

        title = QLabel(f"Welcome to {APP_NAME} Setup")
        title.setObjectName("title")
        lay.addWidget(title)

        body = QLabel(
            f"This will install {APP_NAME} on your computer - a precision line &amp; "
            f"symbol search tool, in both a desktop and a terminal edition.<br><br>"
            f"It's recommended you close other applications before continuing."
        )
        body.setObjectName("subtitle")
        body.setWordWrap(True)
        lay.addWidget(body)
        lay.addStretch()


class LicensePage(QWidget):
    accepted_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        title = QLabel("License Agreement")
        title.setObjectName("title")
        lay.addWidget(title)
        sub = QLabel("Please read the following license agreement carefully.")
        sub.setObjectName("subtitle")
        lay.addWidget(sub)

        text = QTextEdit()
        text.setReadOnly(True)
        try:
            with open(LICENSE_PATH, encoding="utf-8") as fh:
                text.setPlainText(fh.read())
        except OSError:
            text.setPlainText("License file not found.")
        lay.addWidget(text, 1)

        self.accept_chk = GlassCheckbox("I accept the terms in the License Agreement")
        self.accept_chk.toggled.connect(self.accepted_changed.emit)
        lay.addWidget(self.accept_chk)


class ComponentsPage(QWidget):
    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        title = QLabel("Select Components")
        title.setObjectName("title")
        lay.addWidget(title)
        sub = QLabel("Choose which parts of CodeLens to install.")
        sub.setObjectName("subtitle")
        lay.addWidget(sub)

        self.checks = {}
        for key, label, desc, _exe in COMPONENTS:
            row = QVBoxLayout()
            row.setContentsMargins(16, 12, 16, 12)
            row.setSpacing(4)
            chk = GlassCheckbox(label)
            chk.setChecked(state.components[key])
            chk.toggled.connect(lambda checked, k=key: state.components.__setitem__(k, checked))
            self.checks[key] = chk
            row.addWidget(chk)
            desc_lbl = QLabel(desc)
            desc_lbl.setObjectName("subtitle")
            desc_lbl.setContentsMargins(28, 0, 0, 0)
            row.addWidget(desc_lbl)
            lay.addWidget(card(row))

        lay.addStretch()


class LocationPage(QWidget):
    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        title = QLabel("Choose Install Location")
        title.setObjectName("title")
        lay.addWidget(title)
        sub = QLabel(f"Setup will install {APP_NAME} into the following folder.")
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        row = QHBoxLayout()
        self.path_edit = QLineEdit(state.install_dir)
        self.path_edit.textChanged.connect(self._on_path_changed)
        row.addWidget(self.path_edit, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        row.addWidget(browse_btn)
        lay.addLayout(row)
        lay.addStretch()

    def _on_path_changed(self, text):
        self.state.install_dir = text

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select install folder", self.state.install_dir)
        if chosen:
            # Qt's file dialogs always return forward-slash paths, even on
            # Windows - os.path.join() below would then mix in a backslash
            # for the appended part, and that mixed-separator string breaks
            # ShellExecute (the "Windows cannot access the specified
            # device, path, or file" error from the Explorer context menu
            # command we register from this path). Normalize immediately.
            chosen = os.path.normpath(chosen)
            target = os.path.join(chosen, APP_NAME) if os.path.basename(chosen) != APP_NAME else chosen
            self.path_edit.setText(target)


class ShortcutsPage(QWidget):
    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 0)
        lay.setSpacing(12)

        title = QLabel("Additional Shortcuts")
        title.setObjectName("title")
        lay.addWidget(title)
        sub = QLabel("Choose the shortcuts Setup should create.")
        sub.setObjectName("subtitle")
        lay.addWidget(sub)

        self.desktop_chk = GlassCheckbox("Create a desktop shortcut")
        self.desktop_chk.setChecked(state.desktop_shortcut)
        self.desktop_chk.toggled.connect(lambda c: setattr(state, "desktop_shortcut", c))
        lay.addWidget(self.desktop_chk)

        self.startmenu_chk = GlassCheckbox("Create a Start Menu shortcut")
        self.startmenu_chk.setChecked(state.startmenu_shortcut)
        self.startmenu_chk.toggled.connect(lambda c: setattr(state, "startmenu_shortcut", c))
        lay.addWidget(self.startmenu_chk)

        self.context_menu_chk = GlassCheckbox(
            "Add \"Open with CodeLens\" (files and folders) to the right-click menu in File Explorer")
        self.context_menu_chk.setChecked(state.context_menu)
        self.context_menu_chk.toggled.connect(lambda c: setattr(state, "context_menu", c))
        lay.addWidget(self.context_menu_chk)

        self.classic_menu_chk = GlassCheckbox(
            "Show it on a single right-click (restores Windows' classic full context menu everywhere, "
            "instead of hiding it under \"Show more options\")")
        self.classic_menu_chk.setChecked(state.classic_context_menu)
        self.classic_menu_chk.toggled.connect(lambda c: setattr(state, "classic_context_menu", c))
        lay.addWidget(self.classic_menu_chk)
        lay.addStretch()


class ReadyPage(QWidget):
    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 12, 0, 0)
        self.lay.setSpacing(12)

        title = QLabel("Ready to Install")
        title.setObjectName("title")
        self.lay.addWidget(title)
        sub = QLabel("Setup is now ready to install CodeLens on your computer.")
        sub.setObjectName("subtitle")
        self.lay.addWidget(sub)

        self.summary = QLabel()
        self.summary.setObjectName("subtitle")
        self.summary.setWordWrap(True)
        self.lay.addWidget(card(_wrap(self.summary)))
        self.lay.addStretch()

    def refresh(self):
        names = [label for key, label, *_ in COMPONENTS if self.state.components[key]]
        lines = [
            f"<b>Components:</b> {', '.join(names) if names else 'none'}",
            f"<b>Destination:</b> {self.state.install_dir}",
            f"<b>Desktop shortcut:</b> {'Yes' if self.state.desktop_shortcut else 'No'}",
            f"<b>Start Menu shortcut:</b> {'Yes' if self.state.startmenu_shortcut else 'No'}",
            f"<b>\"Open with CodeLens\" context menu (files, folders &amp; folder background):</b> "
            f"{'Yes' if self.state.context_menu else 'No'}",
            f"<b>Classic full context menu (single right-click, system-wide):</b> "
            f"{'Yes' if self.state.classic_context_menu else 'No'}",
        ]
        self.summary.setText("<br><br>".join(lines))


def _wrap(widget):
    lay = QVBoxLayout()
    lay.setContentsMargins(16, 16, 16, 16)
    lay.addWidget(widget)
    return lay


class ProgressPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 40, 0, 0)
        lay.setSpacing(16)
        lay.addStretch()

        title = QLabel("Installing CodeLens...")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        self.status_lbl = QLabel("Preparing...")
        self.status_lbl.setObjectName("subtitle")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        lay.addStretch()


class FinishPage(QWidget):
    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 20, 0, 0)
        lay.setSpacing(14)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(ICON_PATH).pixmap(64, 64))
        lay.addWidget(icon_lbl)

        self.title = QLabel(f"{APP_NAME} Setup completed")
        self.title.setObjectName("title")
        lay.addWidget(self.title)

        self.body = QLabel("CodeLens has been installed on your computer.")
        self.body.setObjectName("subtitle")
        self.body.setWordWrap(True)
        lay.addWidget(self.body)

        self.launch_chk = GlassCheckbox(f"Launch {APP_NAME} now")
        self.launch_chk.setChecked(True)
        self.launch_chk.toggled.connect(lambda c: setattr(state, "launch_after", c))
        lay.addWidget(self.launch_chk)
        lay.addStretch()

    def refresh(self):
        """Labels the launch checkbox with whichever component will
        actually be launched, so it never silently promises a Desktop
        launch when only the CLI was installed (or vice versa)."""
        desktop_on = self.state.components.get("desktop")
        cli_on = self.state.components.get("cli")
        if desktop_on:
            label = f"Launch {APP_NAME} now"
        elif cli_on:
            label = f"Launch {APP_NAME} CLI now"
        else:
            label = f"Launch {APP_NAME} now"
        self.launch_chk._text = label
        self.launch_chk.update()
        self.launch_chk.setVisible(desktop_on or cli_on)

    def set_failure(self, message):
        self.title.setText("Setup could not finish")
        self.body.setText(message)
        self.launch_chk.hide()
        self.state.launch_after = False


# --------------------------------------------------------------------------
# INSTALL WORKER (runs off the UI thread so the progress bar stays alive)
# --------------------------------------------------------------------------
class InstallWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(self, state: InstallState):
        super().__init__()
        self.state = state

    def run(self):
        try:
            # normpath guards against a mixed-separator path (e.g. typed by
            # hand, or containing a stray '/') ending up in a registry
            # command or shortcut target, which Windows fails to resolve.
            install_dir = os.path.normpath(self.state.install_dir)
            os.makedirs(install_dir, exist_ok=True)

            selected = [c for c in COMPONENTS if self.state.components[c[0]]]
            if not selected:
                self.finished_error.emit("No components were selected - nothing to install.")
                return

            all_files = []
            for _key, folder_name, _desc, _exe in selected:
                src = os.path.join(PAYLOAD_DIR, folder_name)
                for root, _dirs, files in os.walk(src):
                    for f in files:
                        all_files.append((os.path.join(root, f), src, folder_name))

            total = max(len(all_files), 1)
            for i, (src_file, src_root, folder_name) in enumerate(all_files, start=1):
                rel = os.path.relpath(src_file, src_root)
                dest_file = os.path.join(install_dir, folder_name, rel)
                os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                shutil.copy2(src_file, dest_file)
                pct = int(i / total * 85)
                self.progress.emit(pct, f"Copying files... ({i}/{total})")

            self.progress.emit(88, "Creating shortcuts...")
            self._create_shortcuts(install_dir, selected)

            self.progress.emit(91, "Registering Explorer context menu...")
            self._register_context_menu(install_dir, selected)

            self.progress.emit(94, "Registering uninstaller...")
            self._write_uninstall_registry(install_dir)

            self.progress.emit(100, "Done.")
            self.finished_ok.emit(install_dir)
        except Exception as e:
            self.finished_error.emit(str(e))

    def _create_shortcuts(self, install_dir, selected):
        if not (self.state.desktop_shortcut or self.state.startmenu_shortcut):
            return
        try:
            import win32com.client
        except ImportError:
            return

        shell = win32com.client.Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")
        start_menu = os.path.join(shell.SpecialFolders("Programs"), APP_NAME)

        for key, folder_name, _desc, exe_name in selected:
            target = os.path.join(install_dir, folder_name, exe_name)
            label = COMPONENT_LABELS[key]

            if self.state.desktop_shortcut:
                sc = shell.CreateShortCut(os.path.join(desktop, f"{label}.lnk"))
                sc.TargetPath = target
                sc.WorkingDirectory = os.path.dirname(target)
                sc.IconLocation = target
                sc.Save()

            if self.state.startmenu_shortcut:
                os.makedirs(start_menu, exist_ok=True)
                sc = shell.CreateShortCut(os.path.join(start_menu, f"{label}.lnk"))
                sc.TargetPath = target
                sc.WorkingDirectory = os.path.dirname(target)
                sc.IconLocation = target
                sc.Save()

    def _write_launcher_vbs(self, install_dir, target):
        """
        Writes a tiny VBScript launcher and returns the wscript.exe command
        line that invokes it. Three problems, one fix:

        1. Windows silently cancels a ShellExecute launch of an unsigned
           .exe through a registered shell verb (the same command run via
           plain CreateProcess - a double-click-equivalent launch, not
           through the file-association machinery - works fine). Routing
           through a trusted, signed system binary sidesteps whatever
           heuristic gates the direct path.
        2. cmd.exe (the first fix that worked) is itself a console host,
           so `cmd /c start ...` briefly flashes a console window before
           handing off. wscript.exe is a GUI-subsystem host, so there's no
           console to flash.
        3. WScript.Shell.Run (the first wscript-based fix) does its own COM
           Automation call to launch the target - and CodeLens enables
           drag-and-drop (setAcceptDrops), which needs OLE initialized on
           its own message loop. Launched that way, the two collided with
           a native "Windows fatal exception: code 0x8001010d"
           (RPC_E_CANTCALLOUT_ININPUTSYNCCALL) almost immediately after
           the splash screen appeared - confirmed in CodeLens_crash.log's
           faulthandler dump. Shell.Application.ShellExecute launches
           through the normal shell-execution path instead (the same one
           a double-click or Explorer's own verb dispatch uses), which
           doesn't hit this conflict.
        """
        vbs_path = os.path.join(install_dir, "OpenWithCodeLens.vbs")
        vbs_content = (
            'Set objShell = CreateObject("Shell.Application")\r\n'
            f'objShell.ShellExecute "{target}", """" & WScript.Arguments(0) & """", "", "open", 1\r\n'
        )
        with open(vbs_path, "w", encoding="utf-8") as fh:
            fh.write(vbs_content)
        return f'wscript.exe "{vbs_path}"'

    def _register_context_menu(self, install_dir, selected):
        if not self.state.context_menu:
            return
        desktop_component = next((c for c in selected if c[0] == "desktop"), None)
        if not desktop_component:
            return
        _key, folder_name, _desc, exe_name = desktop_component
        target = os.path.join(install_dir, folder_name, exe_name)
        launcher = self._write_launcher_vbs(install_dir, target)

        # (registry base key, label, placeholder) - %1 is "the clicked
        # item"; %V is "the folder currently open" (used by the two
        # background/empty-space entries, which have no clicked item).
        verbs = [
            (CONTEXT_MENU_KEY, f"Open with {APP_NAME}", "%1"),
            (DIR_CONTEXT_MENU_KEY, f"Open with {APP_NAME}", "%1"),
            (DIR_BG_CONTEXT_MENU_KEY, f"Open this folder with {APP_NAME}", "%V"),
            (DESKTOP_BG_CONTEXT_MENU_KEY, f"Open this folder with {APP_NAME}", "%V"),
        ]
        try:
            for base_key, label, placeholder in verbs:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, target)
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key + r"\command") as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'{launcher} "{placeholder}"')
        except OSError:
            pass

        if self.state.classic_context_menu:
            self._enable_classic_context_menu()

    def _enable_classic_context_menu(self):
        """Blanks out the CLSID Windows 11 checks to decide whether to
        collapse unpackaged/classic verbs under "Show more options" -
        restoring the classic full context menu everywhere so entries
        like ours (and everyone else's) show on a single right-click."""
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLASSIC_MENU_CLSID_KEY) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "")
        except OSError:
            pass

    def _write_uninstall_registry(self, install_dir):
        # Earlier version copied the running *installer* exe itself into
        # the install dir to double as the uninstaller - but that exe
        # carries the entire ~130MB app payload bundled inside it via
        # PyInstaller onefile, so every uninstall run had to re-extract
        # all of that to a temp dir before it could even show the
        # confirm window. That's what "the uninstaller isn't working"
        # actually was - not broken, just slow enough to look hung.
        # UNINSTALLER_SRC is a separate, payload-free build of this same
        # script (see build_installer.py) - copy that instead.
        uninstaller_path = os.path.join(install_dir, "Uninstall CodeLens.exe")
        try:
            if os.path.isfile(UNINSTALLER_SRC):
                shutil.copy2(UNINSTALLER_SRC, uninstaller_path)
                uninstall_cmd = f'"{uninstaller_path}" --uninstall'
            elif getattr(sys, "frozen", False):
                # Fallback if the lightweight uninstaller wasn't bundled
                # for some reason - still works, just slower to launch.
                shutil.copy2(sys.executable, uninstaller_path)
                uninstall_cmd = f'"{uninstaller_path}" --uninstall'
            else:
                self_path = os.path.abspath(__file__)
                uninstall_cmd = f'"{sys.executable}" "{self_path}" --uninstall'
        except OSError:
            uninstall_cmd = f'"{sys.executable}"'

        size_kb = 0
        for root, _dirs, files in os.walk(install_dir):
            for f in files:
                try:
                    size_kb += os.path.getsize(os.path.join(root, f)) // 1024
                except OSError:
                    pass

        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_PUBLISHER)
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, uninstaller_path)
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_cmd)
                winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        except OSError:
            pass


# --------------------------------------------------------------------------
# MAIN WIZARD WINDOW
# --------------------------------------------------------------------------
STEP_TITLES = ["Welcome", "License", "Components", "Location", "Shortcuts", "Ready", "Installing", "Finish"]


class SetupWizard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.state = InstallState()
        self.setWindowTitle(f"{APP_NAME} Setup")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(760, 520)
        self.setMinimumSize(700, 480)
        self.setStyleSheet(STYLESHEET)

        self.backdrop = GlassBackdrop()
        self.setCentralWidget(self.backdrop)
        outer = QHBoxLayout(self.backdrop)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sidebar ----
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(190)
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(14, 24, 14, 24)
        sb_lay.setSpacing(4)

        brand_row = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(ICON_PATH).pixmap(26, 26))
        brand_row.addWidget(icon_lbl)
        brand_lbl = QLabel(APP_NAME)
        brand_lbl.setStyleSheet("font-weight:700; font-size:12pt;")
        brand_row.addWidget(brand_lbl)
        brand_row.addStretch()
        sb_lay.addLayout(brand_row)
        sb_lay.addSpacing(18)

        self.step_labels = []
        for title in STEP_TITLES:
            lbl = QLabel(title)
            lbl.setObjectName("stepLabel")
            sb_lay.addWidget(lbl)
            self.step_labels.append(lbl)
        sb_lay.addStretch()
        outer.addWidget(sidebar)

        # ---- content ----
        content_wrap = QVBoxLayout()
        content_wrap.setContentsMargins(28, 26, 28, 20)
        content_wrap.setSpacing(18)

        self.stack = QStackedWidget()
        self.welcome_page = WelcomePage()
        self.license_page = LicensePage()
        self.components_page = ComponentsPage(self.state)
        self.location_page = LocationPage(self.state)
        self.shortcuts_page = ShortcutsPage(self.state)
        self.ready_page = ReadyPage(self.state)
        self.progress_page = ProgressPage()
        self.finish_page = FinishPage(self.state)

        for page in (self.welcome_page, self.license_page, self.components_page, self.location_page,
                     self.shortcuts_page, self.ready_page, self.progress_page, self.finish_page):
            self.stack.addWidget(page)
        content_wrap.addWidget(self.stack, 1)

        nav_row = QHBoxLayout()
        nav_row.addStretch()
        self.back_btn = QPushButton("< Back")
        self.back_btn.clicked.connect(self.go_back)
        self.next_btn = QPushButton("Next >")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self.go_next)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.close)
        nav_row.addWidget(self.back_btn)
        nav_row.addWidget(self.next_btn)
        nav_row.addWidget(self.cancel_btn)
        content_wrap.addLayout(nav_row)

        outer.addLayout(content_wrap, 1)

        self.license_page.accepted_changed.connect(self._on_license_toggle)
        self.current_index = 0
        self._refresh_nav()

    # ---------------------------------------------------------- navigation
    def _refresh_sidebar(self):
        for i, lbl in enumerate(self.step_labels):
            if i == self.current_index:
                lbl.setObjectName("stepLabelActive")
            elif i < self.current_index:
                lbl.setObjectName("stepLabelDone")
            else:
                lbl.setObjectName("stepLabel")
            lbl.setStyleSheet("")  # force re-polish
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)

    def _refresh_nav(self):
        self.stack.setCurrentIndex(self.current_index)
        self._refresh_sidebar()
        self.back_btn.setVisible(0 < self.current_index < 5)
        self.cancel_btn.setVisible(self.current_index < 6)

        if self.current_index == 1:  # license
            self.next_btn.setEnabled(self.state.license_accepted)
        else:
            self.next_btn.setEnabled(True)

        if self.current_index == 5:
            self.next_btn.setText("Install")
        elif self.current_index == 6:
            self.next_btn.setVisible(False)
        elif self.current_index == 7:
            self.next_btn.setVisible(True)
            self.next_btn.setText("Finish")
        else:
            self.next_btn.setVisible(True)
            self.next_btn.setText("Next >")

        if self.current_index == 5:
            self.ready_page.refresh()

    def _on_license_toggle(self, accepted):
        self.state.license_accepted = accepted
        self.next_btn.setEnabled(accepted)

    def go_back(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._refresh_nav()

    def go_next(self):
        if self.current_index == 2:
            if not any(self.state.components.values()):
                QMessageBox.warning(self, APP_NAME, "Select at least one component to continue.")
                return
        if self.current_index == 3:
            if not self.state.install_dir.strip():
                QMessageBox.warning(self, APP_NAME, "Choose an install location to continue.")
                return

        if self.current_index == 5:
            self._start_install()
            return
        if self.current_index == 7:
            self._finish()
            return

        if self.current_index < len(STEP_TITLES) - 1:
            self.current_index += 1
            self._refresh_nav()

    def _start_install(self):
        self.current_index = 6
        self._refresh_nav()
        self.worker = InstallWorker(self.state)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_install_ok)
        self.worker.finished_error.connect(self._on_install_error)
        self.worker.start()

    def _on_progress(self, pct, text):
        self.progress_page.bar.setValue(pct)
        self.progress_page.status_lbl.setText(text)

    def _on_install_ok(self, install_dir):
        self.finish_page.refresh()
        self.current_index = 7
        self._refresh_nav()

    def _on_install_error(self, message):
        self.finish_page.set_failure(f"An error occurred during installation:\n{message}")
        self.current_index = 7
        self._refresh_nav()

    def _finish(self):
        if self.state.launch_after:
            # Launch whichever component was actually installed - the
            # old code only ever tried CodeLens.exe, so a CLI-only
            # install would silently launch nothing at all. Desktop
            # wins if both were installed, since that's the app most
            # people mean by "launch it now".
            for key, folder_name, _desc, exe_name in COMPONENTS:
                if not self.state.components[key]:
                    continue
                target = os.path.join(self.state.install_dir, folder_name, exe_name)
                if os.path.isfile(target):
                    try:
                        os.startfile(target)
                    except OSError:
                        pass
                    break
        self.close()


# --------------------------------------------------------------------------
# UNINSTALL WORKER (runs off the UI thread - the old synchronous version
# froze the window while it deleted ~2000 files, which looked like it had
# hung right at 100%)
# --------------------------------------------------------------------------
class UninstallWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(bool, str)   # fully_removed, install_dir
    finished_error = Signal(str)

    def __init__(self, install_dir, installed_components, selected_keys):
        super().__init__()
        self.install_dir = install_dir
        self.installed_components = installed_components
        self.selected_keys = selected_keys

    def run(self):
        try:
            to_remove = [c for c in self.installed_components if c[0] in self.selected_keys]
            if not to_remove:
                self.finished_error.emit("No components were selected to uninstall.")
                return

            self.progress.emit(10, "Removing shortcuts...")
            self._remove_shortcuts(to_remove)

            if any(c[0] == "desktop" for c in to_remove):
                self.progress.emit(20, "Removing Explorer context menu...")
                self._remove_context_menu()

            self.progress.emit(35, "Removing files...")
            self._remove_component_folders(to_remove)

            remaining = [c for c in self.installed_components if c[0] not in self.selected_keys]
            fully_removed = not remaining

            if fully_removed:
                self.progress.emit(75, "Removing registry entry...")
                self._remove_registry()
                self.progress.emit(90, "Cleaning up...")
                self._self_delete_and_cleanup()
            else:
                self.progress.emit(85, "Updating installation record...")
                self._update_registry_size()

            self.progress.emit(100, "Done.")
            self.finished_ok.emit(fully_removed, self.install_dir)
        except Exception as e:
            self.finished_error.emit(str(e))

    def _remove_shortcuts(self, components):
        try:
            import win32com.client
        except ImportError:
            return
        shell = win32com.client.Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")
        start_menu_group = os.path.join(shell.SpecialFolders("Programs"), APP_NAME)

        for key, _folder_name, _desc, _exe in components:
            label = COMPONENT_LABELS[key]
            for base in (desktop, start_menu_group):
                p = os.path.join(base, f"{label}.lnk")
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        if os.path.isdir(start_menu_group) and not os.listdir(start_menu_group):
            shutil.rmtree(start_menu_group, ignore_errors=True)

    def _remove_context_menu(self):
        for base_key in (CONTEXT_MENU_KEY, DIR_CONTEXT_MENU_KEY,
                          DIR_BG_CONTEXT_MENU_KEY, DESKTOP_BG_CONTEXT_MENU_KEY):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base_key + r"\command")
            except OSError:
                pass
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base_key)
            except OSError:
                pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, CLASSIC_MENU_CLSID_KEY)
        except OSError:
            pass
        vbs_path = os.path.join(self.install_dir, "OpenWithCodeLens.vbs")
        try:
            if os.path.isfile(vbs_path):
                os.remove(vbs_path)
        except OSError:
            pass

    def _remove_component_folders(self, components):
        for _key, folder_name, _desc, _exe in components:
            full = os.path.join(self.install_dir, folder_name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)

    def _remove_registry(self):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except OSError:
            pass

    def _update_registry_size(self):
        size_kb = 0
        for root, _dirs, files in os.walk(self.install_dir):
            for f in files:
                try:
                    size_kb += os.path.getsize(os.path.join(root, f)) // 1024
                except OSError:
                    pass
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        except OSError:
            pass

    def _self_delete_and_cleanup(self):
        """
        Deletes this running uninstaller exe and its now-empty install
        folder without waiting for a reboot. A running exe can't delete
        its own file on Windows while it's still executing - the old
        code used MoveFileEx's delay-until-reboot flag, which is why the
        exe was "never" actually deleted (it sat there until the machine
        happened to restart). Instead, spawn a small detached helper
        script that waits for THIS process to exit (releasing the file
        lock), deletes the exe, removes the folder, then deletes itself.
        """
        self_exe = sys.executable if getattr(sys, "frozen", False) else None
        if not self_exe:
            return

        bat_path = os.path.join(tempfile.gettempdir(), "codelens_uninstall_cleanup.bat")
        bat_contents = (
            "@echo off\r\n"
            ":wait\r\n"
            f'del /f /q "{self_exe}" >nul 2>&1\r\n'
            f'if exist "{self_exe}" (\r\n'
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'rmdir "{self.install_dir}" >nul 2>&1\r\n'
            'del "%~f0" >nul 2>&1\r\n'
        )
        with open(bat_path, "w", encoding="utf-8") as fh:
            fh.write(bat_contents)

        # CREATE_NO_WINDOW and DETACHED_PROCESS are contradictory (one asks
        # for a hidden console, the other for no console at all) - passing
        # both together is what caused the console to flash on screen for
        # an instant before Windows tore it back down. CREATE_NO_WINDOW
        # alone, plus an explicit SW_HIDE startupinfo, keeps it fully
        # invisible.
        CREATE_NO_WINDOW = 0x08000000
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=CREATE_NO_WINDOW,
            startupinfo=startupinfo,
            close_fds=True,
        )


class UninstallWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.install_dir = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__))
        self.installed_components = [
            c for c in COMPONENTS if os.path.isdir(os.path.join(self.install_dir, c[1]))
        ]

        self.setWindowTitle(f"Uninstall {APP_NAME}")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(480, 380)
        self.setStyleSheet(STYLESHEET)

        self.backdrop = GlassBackdrop()
        self.setCentralWidget(self.backdrop)
        lay = QVBoxLayout(self.backdrop)
        lay.setContentsMargins(30, 30, 30, 24)
        lay.setSpacing(14)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(ICON_PATH).pixmap(56, 56))
        lay.addWidget(icon_lbl)

        self.title = QLabel(f"Uninstall {APP_NAME}")
        self.title.setObjectName("title")
        lay.addWidget(self.title)

        self.body = QLabel("Choose what to remove from this computer.")
        self.body.setObjectName("subtitle")
        self.body.setWordWrap(True)
        lay.addWidget(self.body)

        self.checks = {}
        if self.installed_components:
            for key, _folder_name, desc, _exe in self.installed_components:
                chk = GlassCheckbox(COMPONENT_LABELS[key])
                chk.setChecked(True)
                self.checks[key] = chk
                lay.addWidget(chk)
                desc_lbl = QLabel(desc)
                desc_lbl.setObjectName("subtitle")
                desc_lbl.setContentsMargins(28, 0, 0, 0)
                lay.addWidget(desc_lbl)
        else:
            none_lbl = QLabel("No CodeLens components were found in this folder.")
            none_lbl.setObjectName("subtitle")
            lay.addWidget(none_lbl)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("subtitle")
        self.status_lbl.hide()
        lay.addWidget(self.status_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        lay.addWidget(self.bar)
        lay.addStretch()

        row = QHBoxLayout()
        row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.close)
        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setObjectName("danger")
        self.uninstall_btn.clicked.connect(self._run)
        self.uninstall_btn.setEnabled(bool(self.installed_components))
        row.addWidget(self.cancel_btn)
        row.addWidget(self.uninstall_btn)
        lay.addLayout(row)

    def _run(self):
        selected = {key for key, chk in self.checks.items() if chk.isChecked()}
        if not selected:
            QMessageBox.warning(self, APP_NAME, "Select at least one component to uninstall.")
            return

        self.uninstall_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        for chk in self.checks.values():
            chk.setEnabled(False)
        self.bar.show()
        self.status_lbl.show()

        self.worker = UninstallWorker(self.install_dir, self.installed_components, selected)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.finished_error.connect(self._on_error)
        self.worker.start()

    def _on_progress(self, pct, text):
        self.bar.setValue(pct)
        self.status_lbl.setText(text)

    def _on_done(self, fully_removed, _install_dir):
        self.title.setText("Uninstall complete")
        if fully_removed:
            self.body.setText(f"{APP_NAME} has been removed from this computer.")
        else:
            self.body.setText("The selected component(s) were removed. The rest of "
                               f"{APP_NAME} is still installed.")
        self.status_lbl.hide()
        self.uninstall_btn.setText("Close")
        self.uninstall_btn.setEnabled(True)
        self.uninstall_btn.clicked.disconnect()
        self.uninstall_btn.clicked.connect(self.close)
        # Don't just sit there once the work is done - close on its own
        # like the installer does, instead of waiting on a click that's
        # easy to miss once the window's behind other apps.
        QTimer.singleShot(2500, self.close)

    def _on_error(self, message):
        self.title.setText("Uninstall failed")
        self.body.setText(message)
        self.status_lbl.hide()
        self.cancel_btn.setEnabled(True)
        for chk in self.checks.values():
            chk.setEnabled(True)


def _is_uninstall_mode():
    """
    True if this run should show the uninstall flow. Checking only for
    the --uninstall argument isn't enough: Control Panel's "Uninstall"
    button passes it correctly (via the registered UninstallString), but
    anyone who just double-clicks "Uninstall CodeLens.exe" straight from
    the install folder or Start Menu - the obvious, expected way to run
    an uninstaller - launches it with no arguments at all, and it fell
    through to the install wizard instead. So also recognize the copied
    uninstaller by its own filename, regardless of how it was started.
    """
    if "--uninstall" in sys.argv:
        return True
    self_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    exe_name = os.path.basename(self_path).lower()
    return exe_name.startswith("uninstall") or exe_name == "codelens_uninstall.exe"


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont(FONT_UI, 10))

    if _is_uninstall_mode():
        win = UninstallWindow()
    else:
        win = SetupWizard()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
