"""Documentation example verification for docs/AI_AGENT_INTEGRATION.md.

* every ```bash verify block: each `python -m codelens_agent ...` line is run
  against the example project; `# exit=N` states the expected exit code
  (default 0), stdout must be one JSON envelope.
* every ```json envelope block: must be a complete envelope that validates
  against the published schema (needs `jsonschema`).
"""

import json
import os
import re
import shlex
import shutil
import tempfile
import unittest

from agent_helpers import REPO, run_agent
import doc_fixture
from test_agent_schema import jsonschema, load_schema

DOC = os.path.join(REPO, "docs", "AI_AGENT_INTEGRATION.md")


def _doc_text():
    with open(DOC, encoding="utf-8") as fh:
        return fh.read()


def _blocks(info):
    text = _doc_text()
    pat = re.compile(r"^```" + re.escape(info) + r"[ \t]*\n(.*?)^```", re.S | re.M)
    return [m.group(1) for m in pat.finditer(text)]


class DocExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.realpath(cls.tmp)
        doc_fixture.build(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_documented_commands_behave_as_documented(self):
        blocks = _blocks("bash verify")
        self.assertTrue(blocks, "no verifiable command blocks found")
        n = 0
        for block in blocks:
            for line in block.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.search(r"#\s*exit=(\d+)\s*$", line)
                expected = int(m.group(1)) if m else 0
                cmd = line[:m.start()].strip() if m else line
                parts = shlex.split(cmd)
                self.assertEqual(parts[:3], ["python", "-m", "codelens_agent"], line)
                r = run_agent("--root", self.root, *parts[3:], cwd=self.root)
                self.assertEqual(r.code, expected, line)
                self.assertEqual(r.json["success"], expected == 0, line)
                if jsonschema:
                    jsonschema.Draft202012Validator(load_schema()).validate(r.json)
                n += 1
        self.assertGreaterEqual(n, 10)

    @unittest.skipIf(jsonschema is None, "jsonschema missing (reported by JsonschemaRequiredTest)")
    def test_documented_json_responses_match_schema(self):
        blocks = _blocks("json envelope")
        self.assertGreaterEqual(len(blocks), 6)
        v = jsonschema.Draft202012Validator(load_schema())
        for b in blocks:
            doc = json.loads(b)
            v.validate(doc)

    def test_documented_agent_loop_works(self):
        """Walks the §6 loop: search -> read -> cursor -> stale -> restart."""
        a = lambda *x: run_agent("--root", self.root, *x, cwd=self.root)
        first = a("search", "parse_config", "--mode", "word", "--page-size", "3")
        hit = first.data["results"][-1]["matches"][-1]
        self.assertEqual(first.data["more_results"], "confirmed")
        rd = a("read", first.data["results"][1]["file"], "--around", "4", "--context", "3")
        self.assertEqual(rd.data["ranges"][0]["lines"][3]["line"], 4)
        self.assertTrue(hit["line"] >= 1)
        nxt = a("search", "parse_config", "--mode", "word", "--page-size", "3",
                "--cursor", first.data["next_cursor"])
        pages = 2
        while nxt.data["next_cursor"]:
            nxt = a("search", "parse_config", "--mode", "word", "--page-size", "3",
                    "--cursor", nxt.data["next_cursor"])
            self.assertTrue(nxt.json["success"])
            pages += 1
        self.assertTrue(nxt.data["complete"])
        self.assertEqual(pages, 3)

    def test_every_documented_error_code_is_registered(self):
        text = _doc_text()
        caps = run_agent("capabilities", cwd=self.root).data
        for code in caps["error_codes"]:
            self.assertIn("`" + code + "`", text, code)


if __name__ == "__main__":
    unittest.main()
