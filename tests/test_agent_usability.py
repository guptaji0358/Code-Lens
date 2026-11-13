"""Installation/usability: directory-form launch, --help, UTF-8, cwd/root."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS


def run(argv, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, *argv], cwd=cwd, env=env,
                          capture_output=True, timeout=60)


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)

    def test_directory_form_works_from_any_cwd_without_pythonpath(self):
        p = run([os.path.join(SCRIPTS, "codelens_agent"), "capabilities"], self.root)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["data"]["roots"], [self.root])

    def test_dash_m_needs_scripts_on_path(self):
        p = run(["-m", "codelens_agent", "capabilities"], SCRIPTS)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_help_is_text_exit_zero_and_lists_globals(self):
        p = run([os.path.join(SCRIPTS, "codelens_agent"), "--help"], self.root)
        self.assertEqual(p.returncode, 0)
        out = p.stdout.decode("utf-8")
        for token in ("--root", "--request", "capabilities", "exit codes"):
            self.assertIn(token, out)

    def test_utf8_output_independent_of_console_encoding(self):
        with open(os.path.join(self.root, "u.txt"), "w", encoding="utf-8") as fh:
            fh.write("héllo wörld ✓ 日本語\n")
        p = run([os.path.join(SCRIPTS, "codelens_agent"), "--root", self.root,
                 "search", "日本語"], self.root,
                {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(d["data"]["results"][0]["matches"][0]["matched_text"], "日本語")

    def test_bad_root_is_json_error_exit_3(self):
        p = run([os.path.join(SCRIPTS, "codelens_agent"), "--root",
                 os.path.join(self.root, "nope"), "capabilities"], self.root)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(json.loads(p.stdout)["errors"][0]["code"], "path_not_found")


class _NoNativeJunction:
    """Wraps a DirEntry but hides is_junction(), as on Python < 3.12."""

    def __init__(self, ent):
        self._ent = ent

    def stat(self, **kw):
        return self._ent.stat(**kw)


@unittest.skipUnless(os.name == "nt", "directory junctions are Windows-only")
class JunctionFallbackTests(unittest.TestCase):
    def test_reparse_tag_fallback_detects_junction_and_not_plain_dirs(self):
        sys.path.insert(0, SCRIPTS)
        try:
            from codelens_agent import search
        finally:
            sys.path.remove(SCRIPTS)
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            link = os.path.join(tmp, "link")
            os.mkdir(real)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link, real],
                               capture_output=True)
            if r.returncode != 0:
                self.skipTest("cannot create junction")
            try:
                ents = {e.name: e for e in os.scandir(tmp)}
                self.assertTrue(search._is_junction(_NoNativeJunction(ents["link"])))
                self.assertFalse(search._is_junction(_NoNativeJunction(ents["real"])))
                self.assertEqual(search._is_junction(ents["link"]),
                                 search._is_junction(_NoNativeJunction(ents["link"])))
            finally:
                os.rmdir(link)


if __name__ == "__main__":
    unittest.main()
