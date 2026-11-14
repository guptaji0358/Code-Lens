"""M8: AI Command Reference - metadata derivation, filtering, copy, GUI dialog.

The catalogue must stay derived from the real parser / MCP schemas / ops
metadata, so these tests compare it against those sources and execute every
example through the real operations.
"""

import os
import shlex
import sys
import unittest

from agent_helpers import REPO, SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from codelens_agent import cli, ops, reference  # noqa: E402
from codelens_agent.mcp_server import tools  # noqa: E402
from codelens_agent.sandbox import Sandbox  # noqa: E402

REQUIRED_FIELDS = ("id", "kind", "label", "identifier", "syntax", "summary", "args",
                   "example", "output", "permission", "notes", "search_text")


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = reference.build_catalogue()
        cls.by_id = {e["id"]: e for e in cls.entries}

    def test_covers_every_operation_and_tool(self):
        self.assertEqual({e["identifier"] for e in self.entries if e["kind"] == "cli"},
                         set(ops.OPERATIONS))
        self.assertEqual({e["identifier"] for e in self.entries if e["kind"] == "mcp"},
                         set(tools.TOOLS))

    def test_every_entry_complete(self):
        for e in self.entries:
            for f in REQUIRED_FIELDS:
                self.assertTrue(e[f] or f == "args", f"{e['id']} missing {f}")
            self.assertEqual(e["permission"], "Read-only", e["id"])
            self.assertIn(e["identifier"], e["syntax"])
            for a in e["args"]:
                for f in ("name", "type", "required", "default", "description"):
                    self.assertIn(f, a)

    def test_cli_args_match_ops_catalogue(self):
        for name, spec in ops.COMMANDS.items():
            got = {a["name"]: a for a in self.by_id["cli:" + name]["args"]}
            self.assertEqual(set(got), {a["name"] for a in spec["args"]}, name)
            for a in spec["args"]:
                self.assertEqual(got[a["name"]]["type"], a["type"], (name, a["name"]))
                self.assertEqual(got[a["name"]]["required"], a["required"], (name, a["name"]))
                self.assertEqual(got[a["name"]]["repeatable"], a["repeatable"], (name, a["name"]))
                self.assertEqual(got[a["name"]]["default"], a["default"])
                self.assertEqual(got[a["name"]]["description"], a["description"])

    def test_cli_flags_are_real_parser_flags(self):
        search = self.by_id["cli:search"]
        self.assertIn("[--no-comment-fallback]", search["syntax"])
        self.assertIn("[--mode {flexible|literal|word}]", search["syntax"])
        self.assertIn("QUERY [PATHS...]", search["syntax"])

    def test_mcp_args_match_tool_schema(self):
        for name, tool in tools.TOOLS.items():
            e = self.by_id["mcp:" + name]
            props = tool["input"]["properties"]
            self.assertEqual({a["name"] for a in e["args"]}, set(props))
            self.assertEqual({a["name"] for a in e["args"] if a["required"]},
                             set(tool["input"]["required"]))
            self.assertEqual(e["summary"], tool["description"])

    def test_mcp_defaults_follow_rename(self):
        ext = [a for a in self.by_id["mcp:search_code"]["args"] if a["name"] == "extensions"][0]
        self.assertEqual(ext["operation_arg"], "ext")
        mode = [a for a in self.by_id["mcp:search_code"]["args"] if a["name"] == "mode"][0]
        self.assertEqual(mode["default"], "flexible")
        self.assertEqual(mode["choices"], ["flexible", "literal", "word"])

    def test_output_shapes_reflect_real_responses(self):
        out = self.by_id["cli:search"]["output"]
        for key in ("next_cursor", "matches", "strategy", "success"):
            self.assertIn(key, out)
        self.assertIn("total_lines", self.by_id["mcp:read_file"]["output"])
        self.assertIn("line_count", self.by_id["mcp:get_file_info"]["output"])


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = reference.build_catalogue()

    def test_every_example_args_valid_against_real_ops(self):
        sandbox = Sandbox([REPO])
        for op, args in reference.EXAMPLE_ARGS.items():
            data, _ = ops.OPERATIONS[op](sandbox, dict(args))
            self.assertTrue(data, op)

    def test_cli_examples_round_trip_through_real_parser(self):
        for e in self.entries:
            if e["kind"] != "cli":
                continue
            argv = shlex.split(e["example"])
            self.assertEqual(argv[:3], ["python", "-m", "codelens_agent"])
            op, args = cli._args_from_flags(argv[3:])
            self.assertEqual(op, e["identifier"])
            self.assertEqual(args, reference.EXAMPLE_ARGS[op], e["id"])

    def test_mcp_examples_call_real_tools(self):
        import json
        for e in self.entries:
            if e["kind"] != "mcp":
                continue
            call = json.loads(e["example"])
            env, is_error = tools.call_tool(call["name"], call["arguments"], [REPO])
            self.assertFalse(is_error, (e["id"], env["errors"]))
            self.assertTrue(env["success"])


class FilterAndCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = reference.build_catalogue()

    def test_category_filter(self):
        self.assertEqual({e["kind"] for e in reference.filter_entries(self.entries, "", "mcp")},
                         {"mcp"})
        self.assertEqual(len(reference.filter_entries(self.entries, "", "cli")),
                         len(ops.OPERATIONS))
        self.assertEqual(len(reference.filter_entries(self.entries)), len(self.entries))

    def test_text_filter(self):
        ids = {e["id"] for e in reference.filter_entries(self.entries, "SEARCH")}
        self.assertTrue({"mcp:search_code", "cli:search"} <= ids)
        got = reference.filter_entries(self.entries, "cursor", "cli")
        self.assertEqual([e["id"] for e in got], ["cli:search"])
        self.assertEqual(reference.filter_entries(self.entries, "zzz-no-such-thing"), [])

    def test_filter_terms_are_anded(self):
        self.assertEqual(reference.filter_entries(self.entries, "read around", "cli")[0]["id"],
                         "cli:read")
        self.assertEqual(reference.filter_entries(self.entries, "read zzzz"), [])

    def test_copy_text(self):
        e = reference.filter_entries(self.entries, "get_file_info")[0]
        self.assertEqual(reference.copy_text(e), e["syntax"])
        self.assertEqual(reference.copy_text(e, "example"), e["example"])
        with self.assertRaises(ValueError):
            reference.copy_text(e, "output")

    def test_no_gui_or_sdk_imports(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); "
                "from codelens_agent import reference; reference.build_catalogue(); "
                "bad=[m for m in sys.modules if m.split('.')[0] in ('PySide6','mcp')]; "
                "sys.exit(1 if bad else 0)" % SCRIPTS)
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from theme import THEMES
        cls.theme = next(iter(THEMES.values()))

    def make(self):
        from dialogs import CommandReferenceDialog
        return CommandReferenceDialog(None, self.theme)

    def test_categories_and_search(self):
        d = self.make()
        self.assertEqual({e["kind"] for e in d.visible_entries()}, {"mcp"})
        d.set_category("cli")
        self.assertEqual(len(d.visible_entries()), len(ops.OPERATIONS))
        d._search.setText("cursor")
        self.assertEqual([e["id"] for e in d.visible_entries()], ["cli:search"])
        d._search.setText("zzz-nothing")
        self.assertEqual(d.visible_entries(), [])
        self.assertIsNone(d.current_entry())
        self.assertFalse(d._copy_syntax.isEnabled())
        self.assertFalse(d.copy("syntax"))

    def test_copy_syntax_and_example_to_clipboard(self):
        d = self.make()
        d._search.setText("read_file")
        e = d.current_entry()
        self.assertEqual(e["id"], "mcp:read_file")
        self.assertTrue(d.copy("syntax"))
        self.assertEqual(QApplication.clipboard().text(), e["syntax"])
        d.copy("example")
        self.assertEqual(QApplication.clipboard().text(), e["example"])

    def test_narrow_window_keeps_readable_detail_pane(self):
        d = self.make()
        d.show()
        d.resize(300, 300)   # far below the floor; the dialog must clamp
        self.app.processEvents()
        self.assertGreaterEqual(d.width(), 560)
        self.assertGreaterEqual(d._detail.viewport().width(), 280)
        d.close()

    def test_detail_shows_required_sections(self):
        d = self.make()
        text = d._detail.toPlainText()
        for needle in ("Syntax", "Arguments", "Example", "Expected output", "Read-only"):
            self.assertIn(needle, text)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class PolishTests(unittest.TestCase):
    """Spacing/layout + hover copy button behaviour of the dialog."""

    SIZES = ((560, 400), (980, 600), (1400, 850))

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from theme import THEMES
        cls.themes = THEMES

    def make(self, theme="dark", size=(980, 600), kind="mcp"):
        from dialogs import CommandReferenceDialog
        d = CommandReferenceDialog(None, self.themes[theme])
        d.set_category(kind)
        d.show()
        d.resize(*size)
        self.addCleanup(d.close)
        self.settle()
        return d

    def settle(self, ms=30):
        from PySide6.QtTest import QTest
        QTest.qWait(ms)
        self.app.processEvents()

    @staticmethod
    def centre(d, what):
        from PySide6.QtCore import QPoint
        r = d.box_rect(what)
        return QPoint(int(r.center().x()), int(r.center().y()))

    # -- layout ----------------------------------------------------------------
    def test_layout_all_themes_sizes_and_tabs(self):
        for theme in self.themes:
            for kind in ("mcp", "cli"):
                for size in self.SIZES:
                    d = self.make(theme, size, kind)
                    tag = (theme, kind, size)
                    self.assertGreaterEqual(d.width(), 560, tag)
                    self.assertGreaterEqual(d.height(), 400, tag)
                    self.assertGreaterEqual(d._detail.viewport().width(), 280, tag)
                    self.assertGreaterEqual(d._list.width(), 170, tag)
                    self.assertLessEqual(d._list.width(), 340, tag)
                    for w in (d._search, d._list, d._detail, d._copy_syntax):
                        self.assertTrue(d.rect().contains(w.mapTo(d, w.rect().topLeft())), tag)
                        self.assertTrue(d.rect().contains(w.mapTo(d, w.rect().bottomRight())), tag)

    def test_detail_text_keeps_margin_from_pane_border(self):
        d = self.make()
        self.assertGreaterEqual(d._detail.document().documentMargin(), 12)
        vp_w = d._detail.viewport().width()
        for what in ("syntax", "example"):
            r = d.box_rect(what)
            self.assertGreaterEqual(r.left(), 12, what)
            self.assertLessEqual(r.right(), vp_w - 12 + 1, what)

    def test_list_rows_have_padding_for_two_lines(self):
        from PySide6.QtWidgets import QStyleOptionViewItem
        d = self.make()
        opt = QStyleOptionViewItem()
        opt.font = d._list.font()
        opt.rect.setWidth(200)
        h = d._list.itemDelegate().sizeHint(opt, d._list.model().index(0, 0)).height()
        self.assertGreaterEqual(h, 2 * d._list.fontMetrics().height() + 2 * 8)

    def test_every_entry_has_boxes_inside_viewport(self):
        d = self.make(size=(560, 400))
        for kind in ("mcp", "cli"):
            d.set_category(kind)
            for row in range(len(d.visible_entries())):
                d._list.setCurrentRow(row)
                self.settle(10)
                for what in ("syntax", "example"):
                    r = d.box_rect(what)
                    self.assertIsNotNone(r, (kind, row, what))
                    self.assertGreater(r.height(), 20)
                    self.assertLessEqual(r.right(), d._detail.viewport().width())

    # -- hover copy button -----------------------------------------------------
    def test_button_hidden_until_hover_then_hides_again(self):
        from PySide6.QtCore import QPoint, Qt
        d = self.make()
        b = d._copy_btns["example"]
        self.assertEqual(b.reveal, 0.0)
        self.assertFalse(b.is_revealed())
        d._update_hover(self.centre(d, "example"))
        self.settle(400)
        self.assertTrue(b.is_revealed())
        self.assertEqual(b.reveal, 1.0)
        self.assertFalse(b.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertFalse(d._copy_btns["syntax"].is_revealed())   # other box untouched
        d._update_hover(QPoint(-5, -5))
        self.settle(400)
        self.assertEqual(b.reveal, 0.0)
        self.assertTrue(b.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_reveal_is_animated_not_instant(self):
        d = self.make()
        b = d._copy_btns["syntax"]
        d._update_hover(self.centre(d, "syntax"))
        self.assertLess(b.reveal, 1.0)          # not snapped open
        self.settle(400)
        self.assertEqual(b.reveal, 1.0)

    def test_button_sits_inside_its_box(self):
        from PySide6.QtCore import QRectF
        for size in self.SIZES:
            d = self.make(size=size)
            vp = QRectF(d._detail.viewport().rect())
            shown = 0
            for what, b in d._copy_btns.items():
                r = d.box_rect(what)
                # visible exactly when its box is (even partly) on screen
                self.assertEqual(b.isVisible(), r.intersects(vp), (size, what))
                if not b.isVisible():
                    continue
                shown += 1
                g = b.geometry()
                self.assertGreaterEqual(g.left(), r.left(), (size, what))
                self.assertLessEqual(g.right(), r.right(), (size, what))
                self.assertGreaterEqual(g.top(), r.top(), (size, what))
                self.assertLessEqual(g.bottom(), r.bottom(), (size, what))
            if size[0] >= 980:   # at the minimum size the boxes may start below the fold
                self.assertGreater(shown, 0, size)

    def test_click_copies_matching_content_and_flashes_then_returns(self):
        d = self.make()
        d._search.setText("read_file")
        self.settle()
        e = d.current_entry()
        for what in ("syntax", "example"):
            QApplication.clipboard().clear()
            d._update_hover(self.centre(d, what))
            self.settle(300)
            b = d._copy_btns[what]
            b.click()
            self.assertEqual(QApplication.clipboard().text(), e[what])
            self.assertTrue(b.is_confirming())
            self.settle(300)
            self.assertGreater(b.flash, 0.9)          # confirmation visible
            self.settle(1900)
            self.assertEqual(b.flash, 0.0)             # eased back
            self.assertFalse(b.is_confirming())

    def test_keyboard_focus_reveals_and_space_or_enter_copies(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        d = self.make()
        d.activateWindow()
        b = d._copy_btns["syntax"]
        self.assertEqual(b.focusPolicy(), Qt.StrongFocus)
        b.setFocus()
        self.settle(300)
        if not b.hasFocus():
            self.skipTest("platform did not grant focus to the offscreen window")
        self.assertTrue(b.is_revealed())
        d._update_hover(QPoint(-5, -5))
        self.settle(300)
        self.assertTrue(b.is_revealed())               # focus keeps it visible
        for key in (Qt.Key_Space, Qt.Key_Return):
            QApplication.clipboard().clear()
            QTest.keyClick(b, key)
            self.assertEqual(QApplication.clipboard().text(), d.current_entry()["syntax"])

    def test_tooltip_and_accessible_names(self):
        d = self.make()
        self.assertEqual(d._copy_btns["syntax"].toolTip(), "Copy syntax")
        self.assertEqual(d._copy_btns["example"].toolTip(), "Copy example")
        for b in d._copy_btns.values():
            self.assertEqual(b.accessibleName(), b.toolTip())

    def test_failed_clipboard_reports_and_does_not_confirm(self):
        d = self.make()
        d._set_clipboard = lambda text: False
        self.assertFalse(d.copy("syntax"))
        self.assertIn("Couldn", d._status.text())
        self.assertFalse(d._copy_btns["syntax"].is_confirming())

    def test_buttons_follow_scroll_and_hide_when_offscreen(self):
        from PySide6.QtCore import QRectF
        d = self.make(size=(560, 400))
        sb = d._detail.verticalScrollBar()
        vp = QRectF(d._detail.viewport().rect())
        seen = set()
        for v in range(0, sb.maximum() + 1, 25):
            sb.setValue(v)
            self.settle(5)
            for what, b in d._copy_btns.items():
                on = d.box_rect(what).intersects(vp)
                self.assertEqual(b.isVisible(), on, (v, what))
                if on:
                    seen.add(what)
        self.assertEqual(seen, {"syntax", "example"})   # both reachable by scrolling

    def test_footer_copy_buttons_still_work(self):
        d = self.make()
        d._copy_example.click()
        self.assertEqual(QApplication.clipboard().text(), d.current_entry()["example"])
        self.assertEqual(d._status.text(), "Copied example.")


if __name__ == "__main__":
    unittest.main()
