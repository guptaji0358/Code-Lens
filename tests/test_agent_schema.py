"""Published JSON Schema vs. real runtime output.

Needs the test-only dependency `jsonschema` (tests/requirements-test.txt).
Strictness check: a second, derived schema forbids undeclared properties
inside every data object, so any field the runtime emits but the schema does
not document fails the test.
"""

import copy
import json
import os
import shutil
import tempfile
import unittest

from agent_helpers import REPO, run_agent
import doc_fixture

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

# The schema tests are part of the required verification. A missing
# `jsonschema` therefore FAILS the run (see JsonschemaRequiredTest) instead of
# silently skipping; opt out explicitly with CODELENS_ALLOW_MISSING_JSONSCHEMA=1.
ALLOW_MISSING = os.environ.get("CODELENS_ALLOW_MISSING_JSONSCHEMA") == "1"

SCHEMA_PATH = os.path.join(REPO, "docs", "schema", "codelens-agent-v1.schema.json")
_SKIP_STRICT = ("envelopeBase", "successBase", "request")


def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _strictify(node):
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node \
                and node.get("type") == "object":
            node["additionalProperties"] = False
        for v in node.values():
            _strictify(v)
    elif isinstance(node, list):
        for v in node:
            _strictify(v)


def strict_schema():
    s = copy.deepcopy(load_schema())
    for name, sub in s["$defs"].items():
        if name.endswith("Response") or name in _SKIP_STRICT:
            # response wrappers use allOf; only their `data` is strictified
            if "properties" in sub and "data" in sub["properties"]:
                _strictify(sub["properties"]["data"])
            continue
        _strictify(sub)
    return s


class JsonschemaRequiredTest(unittest.TestCase):
    def test_jsonschema_is_installed(self):
        if jsonschema is None and not ALLOW_MISSING:
            self.fail("jsonschema is not installed, so the published-schema tests cannot "
                      "run. Run: pip install -r tests/requirements-test.txt "
                      "(or set CODELENS_ALLOW_MISSING_JSONSCHEMA=1 to skip them knowingly).")


@unittest.skipIf(jsonschema is None, "jsonschema missing (reported by JsonschemaRequiredTest)")
class SchemaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.realpath(cls.tmp)
        doc_fixture.build(cls.root)
        with open(os.path.join(cls.root, "crlf.txt"), "wb") as fh:
            fh.write(b"a\r\nb\r\n// c\r\n")
        with open(os.path.join(cls.root, "big.txt"), "wb") as fh:
            fh.write(b"x" * (5 * 1024 * 1024 + 1))
        cls.schema = load_schema()
        cls.strict = strict_schema()
        jsonschema.Draft202012Validator.check_schema(cls.schema)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def agent(self, *argv, **kw):
        return run_agent("--root", self.root, *argv, cwd=self.root, **kw)

    def check(self, res, success=None):
        for sch in (self.schema, self.strict):
            jsonschema.Draft202012Validator(sch).validate(res.json)
        if success is not None:
            self.assertEqual(res.json["success"], success)
        return res

    def test_schema_files_valid(self):
        self.assertEqual(self.schema["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")

    def test_capabilities(self):
        self.check(self.agent("capabilities"), True)

    def test_info_text_binary_large(self):
        for f in ("src/app.py", "logo.bin", "crlf.txt", "big.txt"):
            self.check(self.agent("info", f), True)

    def test_read_variants(self):
        self.check(self.agent("read", "src/app.py"), True)
        self.check(self.agent("read", "src/app.py", "--lines", "2-3", "--lines", "9"), True)
        self.check(self.agent("read", "src/app.py", "--around", "5", "--context", "1"), True)
        self.check(self.agent("read", "src/app.py", "--lines", "5-9999"), True)

    def test_search_variants(self):
        for extra in ([], ["--context", "1"], ["--mode", "literal"], ["--mode", "word"],
                      ["--files-only"], ["--count-only"], ["--page-size", "2"],
                      ["--files-only", "--page-size", "2"], ["--ext", "py"],
                      ["--case-sensitive"]):
            self.check(self.agent("search", "parse_config", *extra), True)
        self.check(self.agent("search", "nothing_matches_this"), True)

    def test_search_multiline_and_comment_fallback(self):
        q = "a\nb\nc"
        self.check(self.agent("search", q, "crlf.txt", "--multiline"), True)
        self.check(self.agent("search", "a\n// zz\nb", "crlf.txt", "--multiline"), True)

    def test_pagination_walk_validates_every_page(self):
        cursor, pages = None, 0
        while True:
            argv = ["search", "parse_config", "--page-size", "2"]
            if cursor:
                argv += ["--cursor", cursor]
            r = self.check(self.agent(*argv), True)
            pages += 1
            cursor = r.data["next_cursor"]
            if not cursor:
                break
        self.assertGreater(pages, 1)

    def test_errors(self):
        cases = [
            (("read", "../x"), "path_outside_root"),
            (("read", "missing.py"), "path_not_found"),
            (("read", "big.txt"), "file_too_large"),
            (("read", "logo.bin"), "not_text_file"),
            (("read", "src"), "not_a_file"),
            (("read", "src/app.py", "--lines", "x"), "invalid_argument"),
            (("search", "x", "--cursor", "AAAA"), "invalid_cursor"),
            (("search", "x", "--count-only", "--files-only"), "invalid_argument"),
            (("bogus",), "unknown_command"),
            ((), "invalid_argument"),
        ]
        for argv, code in cases:
            r = self.check(self.agent(*argv), False)
            self.assertEqual(r.error["code"], code, argv)

    def test_stale_cursor_error(self):
        r = self.agent("search", "parse_config", "--page-size", "1")
        cur = r.data["next_cursor"]
        with open(os.path.join(self.root, "src", "app.py"), "a", encoding="utf-8") as fh:
            fh.write("# touched\n")
        try:
            e = self.check(self.agent("search", "parse_config", "--page-size", "1",
                                      "--cursor", cur), False)
            self.assertEqual(e.error["code"], "stale_cursor")
        finally:
            doc_fixture.build(self.root)

    def test_request_protocol_and_invalid_request(self):
        req = json.dumps({"operation": "read", "args": {"file": "src/app.py", "lines": ["1-2"]}})
        self.check(self.agent("--request", "-", stdin=req.encode()), True)
        self.check(self.agent("--request", "-", stdin=b"not json"), False)

    def test_negative_schema_rejects_bad_documents(self):
        r = self.agent("capabilities")
        bad = copy.deepcopy(r.json)
        bad["interface_version"] = "2"
        v = jsonschema.Draft202012Validator(self.schema)
        self.assertFalse(v.is_valid(bad))
        bad = copy.deepcopy(r.json)
        del bad["data"]["limits"]
        self.assertFalse(v.is_valid(bad))

    def test_argument_schemas_accept_documented_and_reject_invalid(self):
        defs = self.schema["$defs"]
        reg = {"$defs": defs}
        def valid(name, inst):
            return jsonschema.Draft202012Validator({"$ref": "#/$defs/" + name, **reg}).is_valid(inst)
        self.assertTrue(valid("args_search", {"query": "x", "page_size": 5, "mode": "word"}))
        self.assertFalse(valid("args_search", {"query": "x", "mode": "regex"}))
        self.assertFalse(valid("args_search", {"query": "x", "bogus": 1}))
        self.assertTrue(valid("args_read", {"file": "a", "lines": ["1-3"]}))
        self.assertFalse(valid("args_read", {"file": "a", "lines": ["a-b"]}))
        self.assertTrue(valid("request", {"operation": "info", "args": {"file": "a"}}))
        self.assertFalse(valid("request", {"operation": "write"}))


class CapabilitiesAccuracyTests(unittest.TestCase):
    """Capabilities must not advertise what the implementation lacks."""

    def test_advertised_commands_run_and_unsupported_ones_do_not(self):
        r = run_agent("capabilities")
        d = r.json["data"]
        for name in d["commands"]:
            self.assertIn(name, ("capabilities", "info", "read", "search"))
        for name in d["unsupported"]:
            if name in d["commands"]:
                self.fail(f"{name} advertised as both command and unsupported")
        self.assertEqual(d["pagination"]["default_page_size"], d["limits"]["default_search_results"])
        self.assertEqual(d["pagination"]["max_page_size"], d["limits"]["max_search_results"])
        modes = {m["name"] for m in d["search_modes"]}
        bad = run_agent("search", "x", "--mode", "regex")
        self.assertEqual(bad.json["errors"][0]["code"], "invalid_argument")
        self.assertEqual(modes, {"flexible", "literal", "word"})
        for w in ("output_truncated", "results_truncated", "file_skipped"):
            self.assertIn(w, d["warning_codes"])


if __name__ == "__main__":
    unittest.main()
