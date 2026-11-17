"""Regression tests: each search input (Find Files header box, One-Line,
Line Range, Multi Line) keeps its own independent text and search parameter."""

import os
import sys
import tempfile
import unittest
from unittest import mock

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QTimer, QEventLoop
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def pump(ms=60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout_ms=3000, step=20):
    waited = 0
    while waited <= timeout_ms:
        if cond():
            return True
        pump(step)
        waited += step
    return bool(cond())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SearchInputIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import persistence
        cls.persistence = persistence
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig_file = persistence.VIEW_SETTINGS_FILE
        persistence.VIEW_SETTINGS_FILE = os.path.join(cls.tmp.name, "view_settings.json")
        cls.sample = os.path.join(cls.tmp.name, "sample.txt")
        with open(cls.sample, "w", encoding="utf-8") as fh:
            fh.write("alpha def\nbeta\ngamma\n")
        from main_window import FinderWindow
        w = FinderWindow()
        w.resize(1300, 800)
        w.show()
        w.activateWindow()
        pump()
        cls.win = w

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.persistence.VIEW_SETTINGS_FILE = cls._orig_file
        cls.tmp.cleanup()

    def setUp(self):
        w = self.win
        w.activateWindow()
        w.mode_buttons["one"].setChecked(True); pump(60)
        w.query_entry.clear()
        w.range_entry.clear()
        w.header_search.clear()
        w.query_multiline.clear()
        w.state.last_search = None
        w.state.files = []

    # -- helpers ------------------------------------------------------------
    def texts(self):
        w = self.win
        return {
            "find": w.header_search.text(),
            "one": w.query_entry.text(),
            "range": w.range_entry.text(),
            "multi": w.query_multiline.toPlainText(),
        }

    def seed(self, find="", one="", rng="", multi=""):
        w = self.win
        w.header_search.setText(find)
        w.query_entry.setText(one)
        w.range_entry.setText(rng)
        w.query_multiline.setPlainText(multi)

    def focus(self, widget):
        widget.setFocus()
        wait_until(widget.hasFocus)
        self.assertTrue(widget.hasFocus())

    def assert_only_changed(self, changed, before):
        after = self.texts()
        for key, value in before.items():
            if key == changed:
                continue
            self.assertEqual(after[key], value, f"{key} was modified")

    # -- 1-3: typing isolation ----------------------------------------------
    def test_typing_in_find_files_does_not_modify_one_line_or_range(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(one="one-text", rng="1-2")
        before = self.texts()
        self.focus(w.header_search)
        QTest.keyClicks(w.header_search, "xyz")
        self.assertEqual(w.header_search.text(), "xyz")
        self.assert_only_changed("find", before)

    def test_typing_in_one_line_does_not_modify_find_files_or_range(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="find-text", rng="1-2")
        before = self.texts()
        self.focus(w.query_entry)
        QTest.keyClicks(w.query_entry, "abc")
        self.assertEqual(w.query_entry.text(), "abc")
        self.assert_only_changed("one", before)

    def test_typing_in_line_range_does_not_modify_find_files_or_one_line(self):
        w = self.win
        w.mode_buttons["range"].setChecked(True); pump(60)
        self.seed(find="find-text", one="one-text")
        before = self.texts()
        self.focus(w.range_entry)
        QTest.keyClicks(w.range_entry, "5-9")
        self.assertEqual(w.range_entry.text(), "5-9")
        self.assert_only_changed("range", before)
        w.mode_buttons["one"].setChecked(True); pump(60)

    # -- 4: clearing ---------------------------------------------------------
    def test_clearing_one_input_does_not_clear_others(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="find-text", one="one-text", rng="1-2")
        self.focus(w.query_entry)
        QTest.keyClick(w.query_entry, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(w.query_entry, Qt.Key_Backspace)
        self.assertEqual(w.query_entry.text(), "")
        self.assertEqual(w.header_search.text(), "find-text")
        self.assertEqual(w.range_entry.text(), "1-2")

    def test_clearing_find_files_does_not_clear_one_line(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="find-text", one="one-text", rng="1-2")
        self.focus(w.header_search)
        QTest.keyClick(w.header_search, Qt.Key_Escape)
        self.assertEqual(w.header_search.text(), "")
        self.assertEqual(w.query_entry.text(), "one-text")
        self.assertEqual(w.range_entry.text(), "1-2")

    # -- 5: pasting ----------------------------------------------------------
    def test_pasting_into_one_line_does_not_modify_others(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="find-text", rng="1-2")
        self.app.clipboard().setText("pasted")
        before = self.texts()
        self.focus(w.query_entry)
        QTest.keyClick(w.query_entry, Qt.Key_V, Qt.ControlModifier)
        self.assertEqual(w.query_entry.text(), "pasted")
        self.assert_only_changed("one", before)

    def test_pasting_into_line_range_does_not_modify_others(self):
        w = self.win
        w.mode_buttons["range"].setChecked(True); pump(60)
        self.seed(find="find-text", one="one-text")
        self.app.clipboard().setText("7-8")
        before = self.texts()
        self.focus(w.range_entry)
        QTest.keyClick(w.range_entry, Qt.Key_V, Qt.ControlModifier)
        self.assertEqual(w.range_entry.text(), "7-8")
        self.assert_only_changed("range", before)
        w.mode_buttons["one"].setChecked(True); pump(60)

    # -- 6: multiline unchanged ----------------------------------------------
    def test_multiline_text_is_independent(self):
        w = self.win
        w.mode_buttons["multi"].setChecked(True); pump(60)
        self.seed(find="find-text", one="one-text", rng="1-2", multi="snippet")
        before = self.texts()
        self.focus(w.query_multiline)
        QTest.keyClicks(w.query_multiline, "!")
        self.assertEqual(w.query_multiline.toPlainText(), "snippet!")
        self.assert_only_changed("multi", before)
        w.mode_buttons["one"].setChecked(True); pump(60)

    def test_typing_elsewhere_does_not_modify_multiline(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(multi="snippet")
        self.focus(w.query_entry)
        QTest.keyClicks(w.query_entry, "zz")
        self.assertEqual(w.query_multiline.toPlainText(), "snippet")

    # -- 7: each input keeps its search parameter ----------------------------
    def test_header_enter_searches_with_its_own_text(self):
        from CodeLensCLI import load_single_file
        w = self.win
        entry, _ = load_single_file(self.sample)
        w.state.files = [entry]
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="beta", one="alpha")
        w.header_search.returnPressed.emit()
        self.assertEqual(w.state.last_search, "one: 'beta'")
        self.assertEqual(w.query_entry.text(), "alpha")
        self.assertEqual(w.header_search.text(), "beta")

    def test_one_line_enter_searches_with_its_own_text(self):
        from CodeLensCLI import load_single_file
        w = self.win
        entry, _ = load_single_file(self.sample)
        w.state.files = [entry]
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.seed(find="beta", one="alpha")
        w.query_entry.returnPressed.emit()
        self.assertEqual(w.state.last_search, "one: 'alpha'")
        self.assertEqual(w.header_search.text(), "beta")

    def test_range_search_uses_its_own_text(self):
        w = self.win
        w.mode_buttons["range"].setChecked(True); pump(60)
        self.seed(find="3-4", rng="1-2")
        with mock.patch.object(w, "_search_line_range") as spy:
            w.header_search.returnPressed.emit()
            spy.assert_called_with("3-4")
            w.on_search()
            spy.assert_called_with(None)
        w.mode_buttons["one"].setChecked(True); pump(60)


if __name__ == "__main__":
    unittest.main()
