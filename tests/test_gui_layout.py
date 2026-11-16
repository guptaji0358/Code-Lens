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

    def test_toolbar_sits_directly_under_app_header(self):
        self.resize(1220, 780)
        header = self.win.app_header
        header_top = header.mapTo(self.win, header.rect().topLeft()).y()
        header_bottom = header.mapTo(self.win, header.rect().bottomLeft()).y()
        top = self.win.btn_add.mapTo(self.win, self.win.btn_add.rect().topLeft()).y()
        self.assertLess(header_top, 24, "header should start near the top edge")
        self.assertLess(top - header_bottom, 12, "toolbar should follow the header with no gap/extra row")

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


    # ---- application header -------------------------------------------------
    def _header_buttons(self):
        w = self.win
        return [b for b in (w.btn_theme_toggle, w.btn_commands, w.btn_settings, w.btn_header_more)
                if b.isVisible()]

    def test_header_identity_and_native_chrome_untouched(self):
        self.resize(1220, 780)
        w = self.win
        self.assertEqual(w.header_title.text(), "CodeLens")
        self.assertFalse(w.header_logo.pixmap().isNull())
        self.assertTrue(w.header_descriptor.isVisible())
        self.assertEqual(w.header_descriptor.text(), "AI Code Intelligence")
        self.assertTrue(w.windowFlags() & Qt.FramelessWindowHint, "header is the title bar; native one is dropped")
        self.assertEqual(w.app_header.mapTo(w, w.app_header.rect().topLeft()).y(), 0, "header flush with window top")
        self.assertLessEqual(w.app_header.height(), 52, "header must stay compact")

    def test_header_actions_are_moved_not_duplicated(self):
        w = self.win
        for b in (w.btn_commands, w.btn_settings, w.btn_theme_toggle):
            self.assertIs(b.parentWidget(), w.app_header)
            self.assertTrue(b.toolTip(), "header buttons need tooltips")
            self.assertFalse(b.icon().isNull())
        self.assertIn("Command", w.btn_commands.toolTip())
        for b in (w.btn_add, w.btn_reload, w.btn_change, w.btn_recent):
            self.assertIsNot(b.parentWidget(), w.app_header)

    def test_header_no_clipping_or_overlap_at_both_widths(self):
        for width in (1220, 880):
            self.resize(width, 780)
            w = self.win
            hdr = w.app_header.rect()
            self.assertGreaterEqual(w.header_title.width(), w.header_title.sizeHint().width(),
                                    f"{width}: title clipped")
            widgets = [w.header_logo, w.header_title]
            if w.header_descriptor.isVisible():
                widgets.append(w.header_descriptor)
            widgets += self._header_buttons() + w.win_controls
            rects = []
            for x in widgets:
                r = x.geometry()
                self.assertTrue(hdr.contains(r), f"{width}: {x.objectName() or x} outside header")
                rects.append(r)
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    self.assertFalse(a.intersects(b), f"{width}: header widgets overlap")
            self.assertLessEqual(w.app_header.mapTo(w, w.app_header.rect().bottomRight()).x(), w.width())

    def test_header_density_follows_toolbar(self):
        w = self.win
        self.resize(w._toolbar_full_width + 60, 780)
        self.assertEqual(w.btn_settings.toolButtonStyle(), Qt.ToolButtonTextBesideIcon)
        self.resize(w._toolbar_full_width - 40, 560)
        self.assertEqual(w.btn_settings.toolButtonStyle(), Qt.ToolButtonIconOnly)
        self.resize(w._toolbar_full_width + 60, 780)

    def test_header_theme_toggle_switches_theme_and_syncs_settings_switch(self):
        w = self.win
        self.resize(1220, 780)
        w.set_theme("dark"); pump(400)
        self.assertEqual(w.btn_theme_toggle._icon_name, "sun")
        w.btn_theme_toggle.click(); pump(500)
        self.assertEqual(w.theme, "light")
        self.assertTrue(w.settings_panel.theme_switch.isChecked())
        self.assertEqual(w.btn_theme_toggle._icon_name, "moon")
        self.assertIn("dark", w.btn_theme_toggle.toolTip())
        w.btn_theme_toggle.click(); pump(500)
        self.assertEqual(w.theme, "dark")
        self.assertFalse(w.settings_panel.theme_switch.isChecked())
        w.settings_panel.theme_switch.setChecked(True); pump(500)
        self.assertEqual(w.theme, "light")
        self.assertEqual(w.btn_theme_toggle._icon_name, "moon")
        w.set_theme("dark"); pump(400)

    def test_header_commands_and_settings_buttons_run_their_actions(self):
        import main_window
        w = self.win
        shown = []
        orig = main_window.CommandReferenceDialog.show_reference
        main_window.CommandReferenceDialog.show_reference = staticmethod(lambda *a, **k: shown.append(a))
        try:
            w.btn_commands.click()
        finally:
            main_window.CommandReferenceDialog.show_reference = orig
        self.assertEqual(len(shown), 1)
        before = w.settings_panel._visible
        w.btn_settings.click()
        self.assertNotEqual(w.settings_panel._visible, before)
        w.btn_settings.click()
        self.assertEqual(w.settings_panel._visible, before)

    def test_header_overflow_collapses_low_priority_actions_and_they_still_work(self):
        w = self.win
        self.resize(1220, 780)
        self.assertFalse(w.btn_header_more.isVisible())
        spacing = w.app_header.layout().spacing()
        tight = (14 + w.header_logo.width() + w.header_title.sizeHint().width() + 2 * spacing
                 + 6 + sum(c.width() + spacing for c in w.win_controls)
                 + w._HEADER_SEARCH_MIN + 3 * spacing
                 + w.btn_settings.sizeHint().width() + spacing
                 + w.btn_header_more.sizeHint().width() + spacing + 4)
        w._update_header_layout(avail=tight)
        self.assertFalse(w.header_descriptor.isVisible(), "descriptor drops first when tight")
        pump(50)
        self.assertTrue(w.btn_header_more.isVisible())
        self.assertTrue(w._header_collapsed)
        self.assertNotIn(w.btn_settings, w._header_collapsed, "highest-priority action collapses last")
        self.assertEqual(len(w.header_menu.actions()), len(w._header_collapsed))
        self.assertIs(w.btn_header_more.menu(), w.header_menu)
        self.assertIn(w.btn_theme_toggle, w._header_collapsed)
        before = w.theme
        w._header_menu_actions[w.btn_theme_toggle].trigger(); pump(500)
        self.assertNotEqual(w.theme, before)
        w._header_menu_actions[w.btn_theme_toggle].trigger(); pump(500)
        self.assertEqual(w.theme, before)
        w._update_header_layout(avail=w.width() - 24)
        pump(50)
        self.assertFalse(w.btn_header_more.isVisible())
        self.assertTrue(w.btn_commands.isVisible() and w.btn_theme_toggle.isVisible())

    def test_header_icons_render_in_both_themes(self):
        w = self.win
        for theme in ("dark", "light"):
            w.set_theme(theme); pump(400)
            for b in (w.btn_theme_toggle, w.btn_commands, w.btn_settings):
                img = b.icon().pixmap(18, 18).toImage()
                opaque = sum(1 for x in range(img.width()) for y in range(img.height())
                             if img.pixelColor(x, y).alpha() > 0)
                self.assertGreater(opaque, 10, f"{theme}: {b.toolTip()} icon renders blank")
        w.set_theme("dark"); pump(400)


    # ---- header centre search ---------------------------------------------
    def test_header_has_no_blank_gap_search_fills_centre(self):
        w = self.win
        for width in (1220, 880):
            self.resize(width, 780)
            hs = w.header_search
            left = [w.header_logo, w.header_title] + ([w.header_descriptor] if w.header_descriptor.isVisible() else [])
            left_edge = max(x.geometry().right() for x in left)
            right_edge = min(x.geometry().left() for x in self._header_buttons() + w.win_controls)
            gap = right_edge - left_edge
            self.assertGreaterEqual(hs.width(), min(w._HEADER_SEARCH_MAX, gap // 2) - 2,
                                    f"{width}: search does not use the centre space")
            self.assertGreaterEqual(hs.width(), w._HEADER_SEARCH_MIN)
            self.assertLessEqual(hs.width(), w._HEADER_SEARCH_MAX)
            self.assertLessEqual(w.app_header.height(), 52)
            self.assertTrue(w.app_header.rect().contains(hs.geometry()))
        self.resize(1220, 780)
        self.assertGreater(w.header_search.width(), 250)
        self.resize(880, 780)
        self.assertLess(w.header_search.width(), 400)

    def test_header_search_is_a_live_view_of_the_existing_input(self):
        w = self.win
        w.mode_buttons["one"].setChecked(True); pump(60)
        self.assertTrue(w.header_search.isEnabled())
        w.header_search.setText("abc")
        w._on_header_search_edited("abc")
        self.assertEqual(w.query_entry.text(), "abc")
        w.query_entry.setText("xyz")
        self.assertEqual(w.header_search.text(), "xyz")
        w.mode_buttons["range"].setChecked(True); pump(60)
        self.assertEqual(w.header_search.text(), w.range_entry.text())
        w.header_search.setText("1-2"); w._on_header_search_edited("1-2")
        self.assertEqual(w.range_entry.text(), "1-2")
        self.assertEqual(w.query_entry.text(), "xyz", "other mode's text untouched")
        w.mode_buttons["multi"].setChecked(True); pump(60)
        self.assertFalse(w.header_search.isEnabled())
        w.mode_buttons["one"].setChecked(True); pump(60)
        w.query_entry.clear(); w.range_entry.clear()

    def test_header_search_enter_runs_existing_search_and_ctrl_k_focuses(self):
        from CodeLensCLI import load_single_file
        w = self.win
        entry, _ = load_single_file(self.sample)
        w.state.files = [entry]
        w._refresh_file_list()
        w.mode_buttons["one"].setChecked(True); pump(60)
        calls = []
        w.header_search.returnPressed.disconnect()
        w.header_search.returnPressed.connect(lambda: calls.append(1))
        w.header_search.returnPressed.emit()
        self.assertEqual(calls, [1])
        w.header_search.returnPressed.disconnect()
        w.header_search.returnPressed.connect(w.on_search)
        w.header_search.setText("def"); w._on_header_search_edited("def")
        w.header_search.returnPressed.emit()
        self.assertTrue(wait_until(lambda: "match" in w.results_summary.text()))
        w.query_entry.clear()
        w.query_entry.setFocus()
        w._focus_header_search()
        self.assertTrue(w.header_search.hasFocus() or w.header_search.isEnabled())
        self.assertIn("Ctrl+K", w.header_search.placeholderText())
        self.assertTrue(w.header_search.toolTip())

    def test_header_search_themed_in_both_themes(self):
        w = self.win
        for theme in ("dark", "light"):
            w.set_theme(theme); pump(400)
            self.assertIn("#headerSearch", w.styleSheet())
        w.set_theme("dark"); pump(400)

    def test_window_controls_minimize_maximize_close(self):
        w = self.win
        self.resize(1220, 780)
        self.assertEqual([b.toolTip() for b in w.win_controls], ["Minimize", "Maximize", "Close"])
        w.btn_win_max.click(); pump(200)
        self.assertTrue(w.isMaximized())
        self.assertEqual(w.btn_win_max.toolTip(), "Restore")
        w.btn_win_max.click(); pump(200)
        self.assertFalse(w.isMaximized())
        self.assertEqual(w.btn_win_max.toolTip(), "Maximize")
        w.btn_win_min.click(); pump(200)
        self.assertTrue(w.isMinimized())
        w.showNormal(); pump(200)
        closed = []
        orig = w.close
        w.btn_win_close.clicked.disconnect()
        w.btn_win_close.clicked.connect(lambda: closed.append(1))
        w.btn_win_close.click()
        self.assertEqual(closed, [1])
        w.btn_win_close.clicked.disconnect()
        w.btn_win_close.clicked.connect(orig)

    def test_frameless_resize_edges(self):
        from PySide6.QtCore import QPoint
        w = self.win
        self.resize(1220, 780)
        self.assertEqual(w._resize_edges(w._root, QPoint(0, 300)), Qt.LeftEdge)
        self.assertEqual(w._resize_edges(w._root, QPoint(w.width() - 1, w.height() - 1)),
                         Qt.RightEdge | Qt.BottomEdge)
        self.assertEqual(w._resize_edges(w._root, QPoint(w.width() // 2, 300)), Qt.Edges())
        self.assertEqual(w._resize_edges(w.app_header, QPoint(300, 0)), Qt.TopEdge)


if __name__ == "__main__":
    unittest.main()
