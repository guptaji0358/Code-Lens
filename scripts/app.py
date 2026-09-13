"""
app.py - process entry point: crash/hang handling installers, the
startup splash screen, and main().
"""

import datetime
import faulthandler
import os
import sys
import threading
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPainter, QColor, QLinearGradient, QFont
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QMessageBox

from theme import THEMES, APP_ICON_PATH, FONT_UI, with_alpha
from main_window import FinderWindow, APP_NAME, APP_TAGLINE

APP_VERSION = "1.3.1"


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


def _install_hang_watchdog():
    """
    Reports say the window goes fully "Not Responding" - Windows' own
    signal that the UI thread's message loop has actually stopped
    pumping, not just a slow/disabled busy state. That kind of hang
    produces no exception and no faulthandler crash (nothing faults,
    it just never comes back), so there is nothing in the crash log to
    diagnose it from. This starts a small always-on watchdog: a QTimer
    on the UI thread pats a shared timestamp every 500ms, and a
    background thread checks it every 2s. If the timestamp goes stale
    for more than 5s, the UI thread is genuinely stuck somewhere -
    dump every thread's exact stack to the crash log (once, not
    repeatedly) so the next hang report comes with a real answer
    instead of another guess.
    """
    log_path = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "CodeLens_crash.log"
    )
    last_alive = [time.monotonic()]
    dumped = [False]

    timer = QTimer()
    timer.setInterval(500)
    timer.timeout.connect(lambda: last_alive.__setitem__(0, time.monotonic()))
    timer.start()

    def watch():
        while True:
            time.sleep(2)
            stale_for = time.monotonic() - last_alive[0]
            if stale_for > 5 and not dumped[0]:
                dumped[0] = True
                try:
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n"
                                f"[hang-watchdog] UI thread unresponsive for {stale_for:.1f}s - "
                                f"all thread stacks follow:\n")
                        faulthandler.dump_traceback(file=f, all_threads=True)
                except OSError:
                    pass
            elif stale_for <= 5:
                dumped[0] = False  # recovered - allow reporting a future hang again

    threading.Thread(target=watch, daemon=True).start()
    return timer  # caller must keep a reference alive


# --------------------------------------------------------------------------
# STARTUP SPLASH
# --------------------------------------------------------------------------
class SplashScreen(QWidget):
    """
    Frameless branded splash shown while the main window is being built
    (and, for a 'CodeLens' / 'CodeLens.exe' launch from Explorer's
    context menu, while the picked file(s)/folder are being read off
    disk). Always uses the dark theme's palette regardless of the
    user's saved theme preference, the way a splash's brand identity
    normally stays fixed rather than following in-app settings.
    """

    def __init__(self):
        super().__init__()
        t = THEMES["dark"]
        self._bg_a, self._bg_b = t["bg_a"], t["bg_b"]
        self._accent = t["accent"]

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(420, 260)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 36, 0, 28)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignHCenter)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(APP_ICON_PATH).pixmap(72, 72))
        icon_lbl.setAlignment(Qt.AlignHCenter)
        lay.addWidget(icon_lbl)

        lay.addSpacing(6)

        name_lbl = QLabel(APP_NAME)
        name_lbl.setAlignment(Qt.AlignHCenter)
        name_lbl.setStyleSheet(f"color: {t['fg']}; font-size: 26px; font-weight: 700; "
                                f"font-family: '{FONT_UI}';")
        lay.addWidget(name_lbl)

        tag_lbl = QLabel(APP_TAGLINE)
        tag_lbl.setAlignment(Qt.AlignHCenter)
        tag_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 12px; font-family: '{FONT_UI}';")
        lay.addWidget(tag_lbl)

        lay.addStretch()

        bar = QProgressBar()
        bar.setRange(0, 0)  # indeterminate - a busy pulse, no fixed step count to report
        bar.setFixedHeight(4)
        bar.setTextVisible(False)
        bar.setStyleSheet(f"""
            QProgressBar {{ background: {with_alpha(t['fg'], 30)}; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {self._accent}; border-radius: 2px; }}
        """)
        margined = QHBoxLayout()
        margined.setContentsMargins(60, 0, 60, 0)
        margined.addWidget(bar)
        lay.addLayout(margined)

        ver_lbl = QLabel(f"v{APP_VERSION}")
        ver_lbl.setAlignment(Qt.AlignHCenter)
        ver_lbl.setStyleSheet(f"color: {with_alpha(t['fg_dim'], 180)}; font-size: 10px; "
                               f"font-family: '{FONT_UI}';")
        lay.addSpacing(8)
        lay.addWidget(ver_lbl)

        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, QColor(self._bg_a))
        grad.setColorAt(1, QColor(self._bg_b))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(self.rect(), 18, 18)


def main():
    _install_global_error_handling()
    app = QApplication(sys.argv)
    _watchdog_timer = _install_hang_watchdog()  # noqa: F841 - keep QTimer alive
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

    splash = SplashScreen()
    splash.show()
    app.processEvents()

    win = FinderWindow()

    # Keep the splash up for a minimum stretch so it reads as an
    # intentional brand moment rather than a one-frame flicker, even
    # when the window builds (and, for an Explorer-launched file/folder
    # open, the background file load kicks off) near-instantly.
    def _reveal():
        win.show()
        splash.close()

    QTimer.singleShot(700, _reveal)
    sys.exit(app.exec())
