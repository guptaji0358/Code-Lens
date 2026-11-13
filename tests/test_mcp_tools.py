"""M5: tool catalogue + call handling, in-process. Needs NO MCP SDK.

Parity is the main property: an MCP tool call must return exactly the
envelope the existing CLI returns for the same request.
"""

import json
import os
import sys
import unittest

from agent_helpers import AgentTestCase, SCRIPTS, ENVELOPE_KEYS, run_agent

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from codelens_agent.mcp_server import tools  # noqa: E402
from test_agent_schema import jsonschema, load_schema  # noqa: E402


class CatalogueTests(unittest.TestCase):
    def test_exact_initial_catalogue(self):
        self.assertEqual(set(tools.TOOL_NAMES),
                         {"get_capabilities", "search_code", "read_file", "get_file_info"})

    def test_no_get_next_page_tool(self):
        # Deliberately absent: a cursor is bound to the full search arguments, so
        # a separate tool would only repeat search_code with the same arguments.
        self.assertNotIn("get_next_page", tools.TOOLS)

    def test_search_code_has_every_required_argument(self):
        props = tools.TOOLS["search_code"]["input"]["properties"]
        self.assertEqual(set(props), {
            "query", "paths", "mode", "multiline", "case_sensitive", "extensions",
            "include", "exclude", "context", "max_results", "page_size", "cursor",
            "count_only", "files_only", "comment_fallback"})
        self.assertEqual(tools.TOOLS["search_code"]["input"]["required"], ["query"])

    def test_read_and_info_arguments(self):
        self.assertEqual(set(tools.TOOLS["read_file"]["input"]["properties"]),
                         {"file", "lines", "around", "context"})
        self.assertEqual(set(tools.TOOLS["get_file_info"]["input"]["properties"]), {"file"})
        self.assertEqual(tools.TOOLS["get_capabilities"]["input"]["properties"], {})

    def test_schemas_reject_additional_properties(self):
        for name, t in tools.TOOLS.items():
            self.assertIs(t["input"]["additionalProperties"], False, name)

    def test_every_tool_maps_to_an_existing_operation(self):
        from codelens_agent import ops
        for name, t in tools.TOOLS.items():
            self.assertIn(t["operation"], ops.OPERATIONS, name)

    def test_schema_limits_mirror_sandbox_limits(self):
        from codelens_agent import sandbox as sb
        p = tools.TOOLS["search_code"]["input"]["properties"]
        self.assertEqual(p["max_results"]["maximum"], sb.MAX_SEARCH_RESULTS)
        self.assertEqual(p["page_size"]["maximum"], sb.MAX_SEARCH_RESULTS)
        self.assertEqual(p["context"]["maximum"], sb.MAX_CONTEXT_LINES)
        self.assertEqual(p["query"]["maxLength"], sb.MAX_QUERY_CHARS)
        self.assertEqual(tools.TOOLS["read_file"]["input"]["properties"]["lines"]["maxItems"],
                         sb.MAX_READ_RANGES)

    def test_no_tool_accepts_a_root_or_write_argument(self):
        banned = {"root", "roots", "write", "content", "delete", "exec", "command"}
        for name, t in tools.TOOLS.items():
            self.assertFalse(banned & set(t["input"]["properties"]), name)


class CallTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "def foo():\n    return 1\n# foo again\n")
        self.write("sub/b.txt", "nothing here\nfoo\n")

    def call(self, name, args=None):
        return tools.call_tool(name, args, [self.root])

    def test_search_parity_with_cli(self):
        env, is_err = self.call("search_code", {"query": "foo", "context": 1})
        self.assertFalse(is_err)
        cli = self.agent("search", "foo", "--context", "1").json
        self.assertEqual(env, cli)

    def test_extensions_maps_to_ext(self):
        env, _ = self.call("search_code", {"query": "foo", "extensions": ["py"]})
        cli = self.agent("search", "foo", "--ext", "py").json
        self.assertEqual(env, cli)
        self.assertEqual({f["file"] for f in env["data"]["results"]}, {"a.py"})

    def test_ext_is_not_an_mcp_argument(self):
        env, is_err = self.call("search_code", {"query": "foo", "ext": ["py"]})
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "invalid_argument")

    def test_read_and_info_parity_with_cli(self):
        env, _ = self.call("read_file", {"file": "a.py", "lines": ["1-2"]})
        self.assertEqual(env, self.agent("read", "a.py", "--lines", "1-2").json)
        env, _ = self.call("read_file", {"file": "a.py", "around": 2, "context": 1})
        self.assertEqual(env, self.agent("read", "a.py", "--around", "2", "--context", "1").json)
        env, _ = self.call("get_file_info", {"file": "a.py"})
        self.assertEqual(env, self.agent("info", "a.py").json)

    def test_capabilities_unchanged_plus_mcp_block(self):
        env, is_err = self.call("get_capabilities")
        self.assertFalse(is_err)
        cli = self.agent("capabilities").json["data"]
        d = dict(env["data"])
        mcp_block = d.pop("mcp")
        unsupported = d.pop("unsupported")
        cli_unsupported = cli.pop("unsupported")
        self.assertEqual(d, cli)  # everything else is byte-for-byte the CLI payload
        self.assertEqual(unsupported, [u for u in cli_unsupported if u != "mcp_server"])
        for key in ("tool", "interface_version", "schema_version", "search_modes",
                    "limits", "pagination", "read_only"):
            self.assertIn(key, d)
        self.assertEqual([t["name"] for t in mcp_block["tools"]], list(tools.TOOL_NAMES))
        self.assertIsNone(mcp_block["pagination"]["next_page_tool"])
        self.assertTrue(mcp_block["limitations"])
        self.assertEqual(d["roots"], [self.root])

    def test_zero_matches_is_success_not_error(self):
        env, is_err = self.call("search_code", {"query": "no-such-text-xyz"})
        self.assertFalse(is_err)
        self.assertTrue(env["success"])
        self.assertEqual(env["data"]["total_matches"], 0)
        self.assertEqual(env["data"]["results"], [])
        self.assertEqual(env["data"]["more_results"], "none")

    def test_null_arguments_are_treated_as_absent(self):
        env, is_err = self.call("search_code", {"query": "foo", "mode": None,
                                                "cursor": None, "paths": None})
        self.assertFalse(is_err)

    def test_envelope_keys_and_schema_on_success_and_error(self):
        cases = [("search_code", {"query": "foo"}), ("read_file", {"file": "a.py"}),
                 ("get_file_info", {"file": "a.py"}), ("get_capabilities", {}),
                 ("read_file", {"file": "missing.py"}), ("search_code", {"query": ""})]
        for name, args in cases:
            env, is_err = self.call(name, args)
            self.assertEqual(set(env), ENVELOPE_KEYS, name)
            self.assertEqual(is_err, not env["success"], name)
            # the text form is exactly the envelope
            self.assertEqual(json.loads(tools.envelope_text(env)), env)
            if jsonschema and name != "get_capabilities":
                jsonschema.Draft202012Validator(load_schema()).validate(env)
            jsonschema and jsonschema.validate(env, tools.OUTPUT_SCHEMA)

    def test_errors_keep_cli_codes(self):
        cases = [
            ("read_file", {"file": "missing.py"}, "path_not_found"),
            ("read_file", {"file": "../../etc/passwd"}, "path_outside_root"),
            ("get_file_info", {"file": "sub"}, "not_a_file"),
            ("search_code", {"query": "x", "mode": "nope"}, "invalid_argument"),
            ("search_code", {"query": "x", "page_size": 0}, "invalid_argument"),
            ("search_code", {"query": "x", "page_size": 5, "max_results": 5}, "invalid_argument"),
            ("search_code", {"query": "x", "cursor": "garbage!"}, "invalid_cursor"),
            ("read_file", {"file": "a.py", "lines": ["1"], "around": 1}, "invalid_argument"),
            ("read_file", {"file": "a.py", "context": 2}, "invalid_argument"),
            ("search_code", {"query": "x" * 10001}, "limit_exceeded"),
        ]
        for name, args, code in cases:
            env, is_err = self.call(name, args)
            self.assertTrue(is_err, (name, args))
            self.assertEqual(env["errors"][0]["code"], code, (name, args))
            self.assertIsNone(env["data"])

    def test_wrong_types_are_invalid_argument_not_crash(self):
        for args in ({"query": 5}, {"query": "x", "multiline": "yes"},
                     {"query": "x", "paths": {"a": 1}}, {"query": "x", "context": True}):
            env, is_err = self.call("search_code", args)
            self.assertTrue(is_err, args)
            self.assertEqual(env["errors"][0]["code"], "invalid_argument", args)

    def test_non_object_arguments(self):
        env, is_err = tools.call_tool("search_code", ["x"], [self.root])
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "invalid_argument")

    def test_unknown_tool_and_unknown_argument(self):
        env, is_err = self.call("write_file", {"file": "a", "content": "x"})
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "unknown_command")
        env, is_err = self.call("read_file", {"file": "a.py", "root": "C:\\"})
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "invalid_argument")
        self.assertIn("root", env["errors"][0]["message"])

    def test_internal_failure_becomes_internal_error_envelope(self):
        from codelens_agent import ops
        orig = ops.OPERATIONS["info"]
        ops.OPERATIONS["info"] = lambda s, a: 1 / 0
        try:
            env, is_err = self.call("get_file_info", {"file": "a.py"})
        finally:
            ops.OPERATIONS["info"] = orig
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "internal_error")
        # same wording as the CLI: type name only, no exception text or traceback
        self.assertEqual(env["errors"][0]["message"], "Unexpected ZeroDivisionError.")
        self.assertNotIn("division by zero", json.dumps(env))
        self.assertNotIn("Traceback", json.dumps(env))

    def test_invalid_root_is_reported_not_raised(self):
        env, is_err = tools.call_tool("get_file_info", {"file": "a"},
                                      [os.path.join(self.root, "nope")])
        self.assertTrue(is_err)
        self.assertEqual(env["errors"][0]["code"], "path_not_found")


class NoSdkImportTests(unittest.TestCase):
    def test_tools_module_does_not_import_the_sdk(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r);"
                "import codelens_agent.mcp_server.tools as t;"
                "assert 'mcp' not in sys.modules and 'mcp_types' not in sys.modules;"
                "print('ok')" % SCRIPTS)
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)


if __name__ == "__main__":
    unittest.main()
