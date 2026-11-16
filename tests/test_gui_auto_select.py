"""Regression tests: configurable auto-select of the header search text on focus."""

import json
import os
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QPoint, QTimer, QEventLoop
    from PySide6.QtGui import QFocusEvent
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
class AutoSelectSearchFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import persistence
        cls.persistence = persistence
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig_file = persistence.VIEW_SETTINGS_FILE
        persistence.VIEW_SETTINGS_FILE = os.path.join(cls.tmp.name, "view_settings.json")
        cls.win = cls.make_window()

    @classmethod
    def make_window(cls):
        from main_window import FinderWindow
        w = FinderWindow()
        w.resize(1300, 800)
        w.show()
        w.activateWindow()
        pump()
        w.mode_buttons["one"].setChecked(True)
        pump()
        return w

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.persistence.VIEW_SETTINGS_FILE = cls._orig_file
        cls.tmp.cleanup()

    def setUp(self):
        w = self.win
        w.activateWindow()
        w.set_auto_select_search(False)
        w.header_search.clearFocus()
        w.query_entry.setFocus()
        w.header_search.setText("")
        w.header_search.setText("hello world")
        pump(20)

    def hs(self):
        return self.win.header_search

    def click(self, x=None):
        hs = self.hs()
        QTest.mouseClick(hs, Qt.LeftButton, Qt.NoModifier,
                         QPoint(x if x is not None else 20, hs.height() // 2))

    # -- setting ----------------------------------------------------------
    def test_default_is_off(self):
        d = self.persistence.VIEW_SETTINGS_DEFAULTS
        self.assertIs(d["auto_select_search_focus"], False)
        self.assertFalse(self.persistence.load_view_settings()["auto_select_search_focus"])

    def test_setting_in_settings_panel_with_label_and_description(self):
        chk = self.win.settings_panel.auto_select_chk
        self.assertEqual(chk.text(), "Automatically select text when search input receives focus")
        self.assertEqual(chk.toolTip(), "Select all existing search text when focus enters the search field.")
        self.assertFalse(chk.isChecked())

    def test_user_can_enable_and_disable(self):
        w = self.win
        chk = w.settings_panel.auto_select_chk
        chk.setChecked(True)
        self.assertTrue(w.auto_select_search)
        self.assertTrue(self.hs().auto_select_on_focus)
        chk.setChecked(False)
        self.assertFalse(w.auto_select_search)
        self.assertFalse(self.hs().auto_select_on_focus)

    def test_setting_persists_after_reload(self):
        w = self.win
        w.settings_panel.auto_select_chk.setChecked(True)
        with open(self.persistence.VIEW_SETTINGS_FILE, encoding="utf-8") as fh:
            self.assertTrue(json.load(fh)["auto_select_search_focus"])
        w2 = self.make_window()
        try:
            self.assertTrue(w2.auto_select_search)
            self.assertTrue(w2.header_search.auto_select_on_focus)
            self.assertTrue(w2.settings_panel.auto_select_chk.isChecked())
        finally:
            w2.close()
        w.settings_panel.auto_select_chk.setChecked(False)
        w3 = self.make_window()
        try:
            self.assertFalse(w3.header_search.auto_select_on_focus)
        finally:
            w3.close()

    # -- focus behaviour --------------------------------------------------
    def test_programmatic_focus_selects_all_when_enabled(self):
        self.win.set_auto_select_search(True)
        self.hs().setFocus()
        self.assertTrue(wait_until(self.hs().hasFocus))
        self.assertEqual(self.hs().selectedText(), "hello world")

    def test_mouse_focus_selects_all_when_enabled(self):
        self.win.set_auto_select_search(True)
        self.click()
        self.assertTrue(self.hs().hasFocus())
        self.assertEqual(self.hs().selectedText(), "hello world",
                         "the press that gave focus must not cancel the selection")

    def test_tab_navigation_selects_all_when_enabled(self):
        w = self.win
        w.set_auto_select_search(True)
        self.hs().setFocus(); pump(20)
        # Walk the tab chain backwards from the search box and forwards back
        # into it, so focus truly arrives via Tab/Backtab.
        QTest.keyClick(self.hs(), Qt.Key_Tab)
        self.assertFalse(self.hs().hasFocus())
        self.assertIsNotNone(self.app.focusWidget())
        for _ in range(60):
            if self.hs().hasFocus():
                break
            fw = self.app.focusWidget()
            self.assertIsNotNone(fw, "focus was lost during the Backtab walk")
            QTest.keyClick(fw, Qt.Key_Backtab)
        self.assertTrue(self.hs().hasFocus(), "Backtab chain never reached the header search")
        self.assertEqual(self.hs().selectedText(), "hello world")

    def test_focus_does_not_select_when_disabled(self):
        self.hs().setCursorPosition(3)
        self.hs().setFocus()
        self.assertTrue(wait_until(self.hs().hasFocus))
        self.assertEqual(self.hs().selectedText(), "")
        self.hs().clearFocus(); self.win.query_entry.setFocus(); pump(20)
        self.click(10)
        self.assertTrue(self.hs().hasFocus())
        self.assertEqual(self.hs().selectedText(), "")

    def test_click_inside_focused_input_positions_cursor_not_select_all(self):
        self.win.set_auto_select_search(True)
        self.click()
        self.assertEqual(self.hs().selectedText(), "hello world")
        self.click(8)                      # already focused: normal click
        self.assertEqual(self.hs().selectedText(), "")
        pos1 = self.hs().cursorPosition()
        self.click(self.hs().width() - 60)  # right side
        self.assertEqual(self.hs().selectedText(), "")
        self.assertGreater(self.hs().cursorPosition(), pos1)

    def test_window_reactivation_does_not_reselect(self):
        self.win.set_auto_select_search(True)
        self.hs().setFocus(); pump(20)
        self.hs().deselect(); self.hs().setCursorPosition(2)
        self.hs().focusInEvent(QFocusEvent(QFocusEvent.FocusIn, Qt.ActiveWindowFocusReason))
        self.assertEqual(self.hs().selectedText(), "")

    def test_ctrl_a_typing_and_editing_still_work(self):
        self.win.set_auto_select_search(True)
        self.hs().setFocus(); pump(20)
        QTest.keyClick(self.hs(), Qt.Key_Right)            # collapses selection
        self.assertEqual(self.hs().selectedText(), "")
        QTest.keyClick(self.hs(), Qt.Key_A, Qt.ControlModifier)
        self.assertEqual(self.hs().selectedText(), "hello world")
        QTest.keyClicks(self.hs(), "xy")                    # replaces selection
        self.assertEqual(self.hs().text(), "xy")
        QTest.keyClick(self.hs(), Qt.Key_Backspace)
        self.assertEqual(self.hs().text(), "x")
        self.assertEqual(self.win.query_entry.text(), "x")

    def test_search_inputs_follow_setting_and_nothing_else_does(self):
        from PySide6.QtWidgets import QLineEdit
        w = self.win
        inputs = w._auto_select_inputs()
        self.assertEqual(len(inputs), 4)
        for on in (True, False):
            w.set_auto_select_search(on)
            self.assertTrue(all(i.auto_select_on_focus is on for i in inputs))
        for e in w.findChildren(QLineEdit):
            if e not in inputs:
                self.assertFalse(getattr(e, "auto_select_on_focus", False), e.objectName())

    def _check_focus_and_click(self, widget, mode, text, click_at):
        w = self.win
        w.mode_buttons[mode].setChecked(True); pump(80)
        w.set_auto_select_search(True)
        widget.setPlainText(text) if hasattr(widget, "setPlainText") else widget.setText(text)
        w.file_list.setFocus(); pump(20)
        self.assertFalse(widget.hasFocus())
        QTest.mouseClick(widget if not hasattr(widget, "viewport") else widget.viewport(),
                         Qt.LeftButton, Qt.NoModifier, click_at)
        self.assertTrue(widget.hasFocus())
        sel = lambda: (widget.textCursor().selectedText() if hasattr(widget, "textCursor")
                       else widget.selectedText())
        self.assertEqual(sel().replace(chr(0x2029), chr(10)), text, "first click selects all")
        QTest.mouseClick(widget if not hasattr(widget, "viewport") else widget.viewport(),
                         Qt.LeftButton, Qt.NoModifier, click_at)
        self.assertEqual(sel(), "", "click inside focused input moves cursor")
        # programmatic focus
        w.file_list.setFocus(); pump(20)
        widget.setFocus(); pump(20)
        self.assertEqual(sel().replace(chr(0x2029), chr(10)), text)

    def test_one_line_input_selects_on_focus(self):
        self._check_focus_and_click(self.win.query_entry, "one", "needle", QPoint(10, 10))
        self.win.query_entry.clear()

    def test_range_input_selects_on_focus(self):
        self._check_focus_and_click(self.win.range_entry, "range", "10-20", QPoint(10, 10))
        self.win.range_entry.clear()

    def test_multiline_input_selects_on_focus(self):
        self._check_focus_and_click(self.win.query_multiline, "multi", "line one" + chr(10) + "line two", QPoint(10, 10))
        self.win.query_multiline.clear()
        self.win.mode_buttons["one"].setChecked(True); pump(60)

    def test_inputs_do_not_select_when_disabled(self):
        w = self.win
        w.mode_buttons["multi"].setChecked(True); pump(80)
        w.query_multiline.setPlainText("abc def")
        w.file_list.setFocus(); pump(20)
        w.query_multiline.setFocus(); pump(20)
        self.assertEqual(w.query_multiline.textCursor().selectedText(), "")
        w.query_multiline.clear()
        w.mode_buttons["one"].setChecked(True); pump(60)

    def test_existing_search_behaviour_unchanged(self):
        w = self.win
        w.set_auto_select_search(True)
        w.header_search.setText("abc"); w._on_header_search_edited("abc")
        self.assertEqual(w.query_entry.text(), "abc")
        w.query_entry.setText("zzz")
        self.assertEqual(w.header_search.text(), "zzz")
        w._focus_header_search()                         # Ctrl+K still focuses + selects
        self.assertTrue(w.header_search.hasFocus())
        self.assertEqual(w.header_search.selectedText(), "zzz")
        w.query_entry.clear()


if __name__ == "__main__":
    unittest.main()
