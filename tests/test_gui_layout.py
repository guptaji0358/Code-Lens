"""GUI cleanup phase 1: regression tests for the main-window layout.

Guards the de-duplication / density work: no brand row repeating the window
title, one-row Settings panel, search card that hugs its content, a toolbar
that degrades to icon-only instead of eliding labels, and no status line that
repeats the results summary.
"""

import os
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QTimer, QEventLoop
    from PySide6.QtWidgets import QApplication, QLabel
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def pump(ms=120):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(condition, timeout_ms=5000, step_ms=20):
    """Pump the Qt event loop until condition() is truthy; returns whether it
    became true within timeout_ms. Polls instead of sleeping a fixed time, so
    slow/loaded machines still pass and fast ones don't wait."""
    waited = 0
    while waited <= timeout_ms:
        if condition():
            return True
        pump(step_ms)
        waited += step_ms
    return bool(condition())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MainWindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from main_window import FinderWindow
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sample = os.path.join(cls.tmp.name, "sample.txt")
        with open(cls.sample, "w", encoding="utf-8") as f:
            f.write("alpha\nbeta def\ngamma\n")
        cls.win = FinderWindow()
        cls.win.show()
        pump()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.tmp.cleanup()

    def resize(self, w, h):
        self.win.resize(w, h)
        pump()

    def test_no_brand_row_duplicating_window_title(self):
        names = {lbl.objectName() for lbl in self.win.findChildren(QLabel)}
        self.assertNotIn("brand", names)
        self.assertNotIn("brandTag", names)
        self.assertIn("CodeLens", self.win.windowTitle())

    def test_toolbar_is_first_row_at_top(self):
        self.resize(1220, 780)
        top = self.win.btn_add.mapTo(self.win, self.win.btn_add.rect().topLeft()).y()
        self.assertLess(top, 24, "toolbar should start near the top edge, not under a brand row")

    def test_toolbar_labels_when_wide_icons_when_narrow(self):
        # Font metrics differ per platform (offscreen vs. real), so the "wide"
        # size is derived from the measured width of the labelled toolbar.
        wide = self.win._toolbar_full_width + 60
        self.resize(wide, 780)
        self.assertEqual(self.win.btn_add.toolButtonStyle(), Qt.ToolButtonTextBesideIcon)
        self.resize(self.win._toolbar_full_width - 40, 560)
        for b in (self.win.btn_add, self.win.btn_commands, self.win.btn_settings):
            self.assertEqual(b.toolButtonStyle(), Qt.ToolButtonIconOnly)
            self.assertTrue(b.toolTip(), "icon-only buttons need a tooltip")
        right_edge = max(b.mapTo(self.win, b.rect().topRight()).x()
                         for b in self.win._tool_buttons if b.isVisible())
        self.assertLessEqual(right_edge, self.win.width())
        self.resize(wide, 780)
        self.assertEqual(self.win.btn_add.toolButtonStyle(), Qt.ToolButtonTextBesideIcon)

    def test_settings_is_one_compact_row_without_repeated_state(self):
        from theme import THEMES
        panel = self.win.settings_panel
        self.assertLessEqual(panel._max_height, 64)
        self.assertFalse(hasattr(panel, "shortcuts_lbl"), "shortcut list must not be repeated inline")
        tip = panel.btn_shortcuts.toolTip()
        for _key, label, _default in __import__("dialogs").SHORTCUT_ACTIONS:
            self.assertIn(label, tip)
        for theme in THEMES:
            self.win.set_theme(theme)
            pump(60)
            self.assertIn("theme", panel.theme_switch.toolTip().lower())
        self.assertFalse([w for w in panel.findChildren(QLabel) if w.text() in ("Dark mode", "Light mode", "On", "Off")])

    def test_search_card_hugs_content_in_every_mode(self):
        self.resize(1220, 780)
        for mode in ("one", "multi", "range", "one"):
            self.win.mode_buttons[mode].setChecked(True)
            pump(80)
            card_h = self.win.search_card.height()
            need = self.win.search_card.sizeHint().height()
            self.assertLessEqual(card_h, need + 4, f"{mode}: empty band inside search card")
            self.assertGreaterEqual(card_h, need - 4, f"{mode}: search card clipped")
            self.assertGreater(self.win.results.height(), card_h)

    def test_status_does_not_repeat_results_summary(self):
        from CodeLensCLI import load_single_file
        entry, _msg = load_single_file(self.sample)
        self.win.state.files = [entry]
        self.win._refresh_file_list()
        self.win.mode_buttons["one"].setChecked(True)
        self.win.query_entry.setText("def")
        self.win.on_search()
        pump(400)
        summary = self.win.results_summary.text()
        self.assertIn("match", summary)
        self.assertNotIn(summary, self.win.status.text())
        self.assertNotIn("matching occurrence", self.win.status.text())

    # ---- phase 2: Reset merged into New Set as a split button -------------
    def test_reset_button_merged_into_new_set_split_button(self):
        from PySide6.QtWidgets import QToolButton
        self.assertFalse(hasattr(self.win, "btn_reset"))
        btn = self.win.btn_change
        self.assertEqual(btn.popupMode(), QToolButton.MenuButtonPopup)
        self.assertEqual([a.text() for a in btn.menu().actions()],
                         ["New file set…", "Reset search state"])
        self.assertIn("fresh file set", btn.toolTip())
        self.assertIn(self.win.shortcut_keys["reset"], btn.toolTip())
        self.assertFalse(self.win.act_new_set.icon().isNull())
        self.assertFalse(self.win.act_reset_search.icon().isNull())
        # Clear Results and Remove remain separate actions.
        self.assertTrue(self.win.btn_clear.isVisible() and self.win.btn_remove.isVisible())

    def test_reset_search_state_keeps_files_and_clears_search(self):
        from CodeLensCLI import load_single_file
        entry, _ = load_single_file(self.sample)
        self.win.state.files = [entry]
        self.win._refresh_file_list()
        self.win.mode_buttons["one"].setChecked(True)
        self.win.query_entry.setText("def")
        self.win.on_search()
        arrived = wait_until(lambda: self.win.results.toPlainText().strip()
                             and self.win.results_summary.text())
        self.assertTrue(
            arrived,
            "search results never arrived within 5s: results pane="
            f"{self.win.results.toPlainText()[:80]!r}, summary={self.win.results_summary.text()!r}, "
            f"status={self.win.status.text()!r}")
        asked = []
        self.win._confirm = lambda *a, **k: asked.append(a) or True
        try:
            self.win.act_reset_search.trigger()
        finally:
            del self.win._confirm
        self.assertEqual(len(asked), 1, "exactly one confirm, not the old two-step chain")
        self.assertEqual(len(self.win.state.files), 1)
        self.assertIsNone(self.win.state.last_search)
        self.assertEqual(self.win.query_entry.text(), "")
        self.assertEqual(self.win.results.toPlainText(), "")
        self.assertEqual(self.win.results_summary.text(), "")
        # Nothing to reset -> no confirm at all.
        self.win._confirm = lambda *a, **k: self.fail("confirm shown for empty search state")
        try:
            self.win.on_reset_search()
        finally:
            del self.win._confirm

    def test_new_set_still_clears_everything_and_defers_add(self):
        from CodeLensCLI import load_single_file
        entry, _ = load_single_file(self.sample)
        self.win.state.files = [entry]
        calls = []
        orig_add = self.win.on_add
        self.win.on_add = lambda: calls.append("add")
        self.win._confirm = lambda *a, **k: True
        try:
            self.win.btn_change.click()
            self.assertEqual(self.win.state.files, [])
            self.assertEqual(calls, [], "Add dialog must be deferred via QTimer, not chained directly")
            pump(80)
            self.assertEqual(calls, ["add"])
        finally:
            del self.win._confirm
            self.win.on_add = orig_add

    def test_reset_shortcut_key_preserved_and_routed_to_reset_search(self):
        from persistence import SHORTCUT_ACTIONS
        keys = {k: (label, default) for k, label, default in SHORTCUT_ACTIONS}
        self.assertIn("reset", keys, "saved shortcuts.json key 'reset' must keep working")
        self.assertEqual(keys["reset"][1], "Ctrl+Shift+R")
        sc = self.win._shortcut_objs["reset"]
        self.assertEqual(sc.key().toString(), self.win.shortcut_keys["reset"])
        self.win.query_entry.setText("zzz")
        self.win._confirm = lambda *a, **k: True
        try:
            sc.activated.emit()
        finally:
            del self.win._confirm
        self.assertEqual(self.win.query_entry.text(), "")

    def test_toolbar_density_rules_cover_new_toolbar(self):
        wide = self.win._toolbar_full_width + 60
        self.resize(self.win._toolbar_full_width - 40, 560)
        self.assertEqual(self.win.btn_change.toolButtonStyle(), Qt.ToolButtonIconOnly)
        self.assertTrue(self.win.btn_change.menu().actions())
        self.resize(wide, 780)
        self.assertEqual(self.win.btn_change.toolButtonStyle(), Qt.ToolButtonTextBesideIcon)


if __name__ == "__main__":
    unittest.main()
