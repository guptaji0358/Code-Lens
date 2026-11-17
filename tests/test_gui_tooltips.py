"""Regression tests for the app-wide CodeLens tooltip (scripts/tooltips.py):
hover, keyboard focus, both themes, shortcut accuracy, edge-of-window
placement, dismissal, sequential tips, and dialog/toolbar compatibility."""

import os
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QTimer, QEventLoop, QEvent, QPoint, QPointF, QRect
    from PySide6.QtGui import QKeySequence, QShortcut, QFocusEvent, QHelpEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def pump(ms=60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TooltipManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import persistence
        cls.persistence = persistence
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig_view = persistence.VIEW_SETTINGS_FILE
        persistence.VIEW_SETTINGS_FILE = os.path.join(cls.tmp.name, "view_settings.json")
        from main_window import FinderWindow
        from tooltips import TooltipManager
        w = FinderWindow()
        w.resize(1100, 640)
        w.show()
        pump()
        cls.win = w
        cls.mgr = TooltipManager.instance()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.persistence.VIEW_SETTINGS_FILE = cls._orig_view
        cls.tmp.cleanup()

    def setUp(self):
        self.mgr.hide_tip()
        self.win.theme = "dark"
        self.win._apply_theme(animate=False)
        self.win._set_shortcuts_enabled(True)

    def bubble(self):
        return self.mgr._bubble

    def hover(self, widget):
        """Deliver Qt's own tooltip help event, exactly what hovering sends."""
        ev = QHelpEvent(QEvent.ToolTip, QPoint(2, 2), widget.mapToGlobal(QPoint(2, 2)))
        QApplication.sendEvent(widget, ev)
        return ev

    # hover ------------------------------------------------------------
    def test_hover_shows_custom_bubble_and_consumes_native_tooltip(self):
        ev = self.hover(self.win.btn_add)
        self.assertTrue(ev.isAccepted(), "native tooltip must be consumed")
        self.assertTrue(self.bubble().isVisible())
        self.assertIs(self.mgr.current_target(), self.win.btn_add)
        self.assertEqual(self.bubble()._title, "Add files")

    def test_widget_without_any_tip_gets_no_bubble(self):
        plain = QPushButton()
        plain.show()
        self.mgr.hide_tip()
        self.hover(plain)
        self.assertFalse(self.bubble().isVisible())
        plain.deleteLater()

    # keyboard focus ---------------------------------------------------
    def test_keyboard_focus_shows_tip_after_delay_and_hides_on_focus_out(self):
        btn = self.win.btn_reload
        btn.setFocus()
        QApplication.sendEvent(btn, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
        self.assertFalse(self.bubble().isVisible(), "focus tips wait a short delay")
        pump(450)
        self.assertTrue(self.bubble().isVisible())
        self.assertIs(self.mgr.current_target(), btn)
        QApplication.sendEvent(btn, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
        self.assertFalse(self.bubble().isVisible())

    def test_mouse_focus_does_not_show_keyboard_tip(self):
        btn = self.win.btn_reload
        QApplication.sendEvent(btn, QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason))
        pump(450)
        self.assertFalse(self.bubble().isVisible())

    # themes -----------------------------------------------------------
    def test_bubble_uses_active_theme_tokens_in_both_themes(self):
        from theme import THEMES
        for theme in ("dark", "light"):
            self.win.theme = theme
            self.win._apply_theme(animate=False)
            self.mgr.show_for(self.win.btn_add)
            self.assertEqual(self.bubble()._theme["bg_panel"], THEMES[theme]["bg_panel"], theme)
            self.assertEqual(self.bubble()._theme["fg"], THEMES[theme]["fg"], theme)
            self.assertGreater(self.bubble().width(), 40)

    def test_theme_change_dismisses_visible_tip(self):
        self.mgr.show_for(self.win.btn_add)
        self.assertTrue(self.bubble().isVisible())
        self.win.theme = "light"
        self.win._apply_theme(animate=False)
        self.assertFalse(self.bubble().isVisible())

    # shortcut accuracy ------------------------------------------------
    def test_registered_shortcut_chip_matches_live_binding(self):
        self.mgr.show_for(self.win.btn_add)
        self.assertEqual(self.bubble()._shortcut,
                         QKeySequence(self.win.shortcut_keys["add"]).toString(QKeySequence.NativeText))

    def test_disabled_shortcut_is_not_shown(self):
        self.win._set_shortcuts_enabled(False)
        self.mgr.show_for(self.win.btn_add)
        self.assertEqual(self.bubble()._shortcut, "")
        self.win._set_shortcuts_enabled(True)

    def test_chip_follows_rebinding(self):
        from theme import THEMES  # noqa: F401  (ensures theme import chain is warm)
        sc = self.win._shortcut_objs["add"]
        original = sc.key().toString()
        try:
            sc.setKey(QKeySequence("Ctrl+Alt+Y"))
            self.mgr.show_for(self.win.btn_add)
            self.assertEqual(self.bubble()._shortcut, "Ctrl+Alt+Y")
        finally:
            sc.setKey(QKeySequence(original))

    def test_empty_shortcut_key_shows_no_chip(self):
        probe = QWidget()
        sc = QShortcut(QKeySequence(), probe)
        probe.setToolTip("Probe")
        from tooltips import set_tooltip_info
        set_tooltip_info(probe, title="Probe", shortcut=lambda: sc)
        self.mgr.show_for(probe)
        self.assertEqual(self.bubble()._shortcut, "")
        probe.deleteLater()

    # placement --------------------------------------------------------
    def _assert_inside_screen(self, widget):
        self.mgr.show_for(widget)
        b = self.bubble()
        avail = b.screen().availableGeometry()
        geom = QRect(b.pos(), b.size())
        self.assertTrue(avail.contains(geom), f"{widget.objectName() or widget}: {geom} outside {avail}")

    def test_bubble_stays_on_screen_for_window_edge_widgets(self):
        for widget in (self.win.btn_win_close, self.win.btn_win_max, self.win.btn_add,
                       self.win.search_btn, self.win.btn_header_more):
            self._assert_inside_screen(widget)

    def test_bubble_does_not_cover_hovered_widget(self):
        btn = self.win.btn_add
        self.mgr.show_for(btn)
        b = self.bubble()
        btn_rect = QRect(btn.mapToGlobal(QPoint(0, 0)), btn.size())
        bubble_rect = QRect(b.pos(), b.size())
        # The shadow margin is transparent, so compare the body only.
        from tooltips import _SHADOW
        body = bubble_rect.adjusted(_SHADOW, _SHADOW, -_SHADOW, -_SHADOW)
        self.assertFalse(body.intersects(btn_rect))

    # dismissal --------------------------------------------------------
    def test_leave_hides_tip(self):
        self.hover(self.win.btn_add)
        self.assertTrue(self.bubble().isVisible())
        QApplication.sendEvent(self.win.btn_add, QEvent(QEvent.Leave))
        self.assertFalse(self.bubble().isVisible())

    def test_mouse_press_and_key_press_dismiss(self):
        from PySide6.QtGui import QKeyEvent
        self.mgr.show_for(self.win.btn_add)
        QApplication.sendEvent(self.win.btn_add, QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier))
        self.assertFalse(self.bubble().isVisible())
        self.mgr.show_for(self.win.btn_add)
        QApplication.sendEvent(self.win.btn_add, QMouseEvent(
            QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        self.assertFalse(self.bubble().isVisible())

    # sequential -------------------------------------------------------
    def test_sequential_tips_replace_each_other(self):
        self.hover(self.win.btn_add)
        self.hover(self.win.btn_reload)
        self.assertTrue(self.bubble().isVisible())
        self.assertIs(self.mgr.current_target(), self.win.btn_reload)
        self.assertEqual(self.bubble()._title, "Reload")

    # compatibility ----------------------------------------------------
    def test_toolbar_and_header_widgets_keep_plain_tooltip_text(self):
        for b in (self.win.btn_add, self.win.btn_reload, self.win.btn_theme_toggle,
                  self.win.btn_commands, self.win.btn_settings):
            self.assertTrue(b.toolTip(), b.objectName() or b.text())

    def test_shortcuts_dialog_buttons_get_tooltips_through_manager(self):
        panel = self.win.settings_panel
        self.assertTrue(panel.btn_shortcuts.toolTip())
        self.hover(panel.btn_shortcuts)
        self.assertTrue(self.bubble().isVisible())
        self.assertIs(self.mgr.current_target(), panel.btn_shortcuts)

    def test_single_manager_instance_no_extra_filters(self):
        from tooltips import TooltipManager
        self.assertIs(TooltipManager.install(self.app, self.win._effective_theme), self.mgr)


if __name__ == "__main__":
    unittest.main()
