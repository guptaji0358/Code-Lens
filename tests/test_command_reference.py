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


if __name__ == "__main__":
    unittest.main()
