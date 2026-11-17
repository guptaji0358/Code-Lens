"""Regression tests: switching search modes must never change or exchange the
placeholders of the Find Files header box and the three mode inputs."""

import os
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer, QEventLoop
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def pump(ms=60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PlaceholderStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import persistence
        cls.persistence = persistence
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig_file = persistence.VIEW_SETTINGS_FILE
        persistence.VIEW_SETTINGS_FILE = os.path.join(cls.tmp.name, "view_settings.json")
        from main_window import FinderWindow
        w = FinderWindow()
        w.resize(1300, 800)
        w.show()
        pump()
        cls.win = w
        cls.initial = cls.snapshot(w)

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.persistence.VIEW_SETTINGS_FILE = cls._orig_file
        cls.tmp.cleanup()

    @staticmethod
    def snapshot(w):
        return {
            "header": w.header_search.placeholderText(),
            "one": w.query_entry.placeholderText(),
            "multi": w.query_multiline.placeholderText(),
            "range": w.range_entry.placeholderText(),
        }

    def switch(self, mode):
        self.win.mode_buttons[mode].setChecked(True)
        pump(60)

    def test_initial_placeholders(self):
        s = self.initial
        self.assertEqual(s["header"], "Search loaded files…  (Ctrl+K)")
        self.assertIn("exact text", s["one"])
        self.assertIn("multi-line", s["multi"])
        self.assertIn("1500-3000", s["range"])
        self.assertEqual(len({s["header"], s["one"], s["multi"], s["range"]}), 4)

    def test_header_placeholder_unchanged_in_each_mode(self):
        for mode in ("one", "multi", "range"):
            with self.subTest(mode=mode):
                self.switch(mode)
                self.assertEqual(self.win.header_search.placeholderText(),
                                 self.initial["header"])
                self.assertEqual(self.snapshot(self.win), self.initial)

    def test_repeated_switching_keeps_placeholders_on_own_widgets(self):
        for mode in ("range", "multi", "one", "multi", "range", "one", "range", "multi", "one"):
            self.switch(mode)
            self.assertEqual(self.snapshot(self.win), self.initial)


if __name__ == "__main__":
    unittest.main()
