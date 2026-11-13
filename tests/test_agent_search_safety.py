import json
import os
import unittest

from agent_helpers import AgentTestCase, run_agent


class EncodingTests(AgentTestCase):
    def test_unicode(self):
        self.write("u.txt", "héllo wörld\n日本語のテキスト\n🙂 emoji 🙂\n")
        r = self.agent("search", "日本語", "u.txt")
        m = r.data["results"][0]["matches"][0]
        self.assertEqual((m["line"], m["column"], m["end_column"]), (2, 1, 3))
        self.assertEqual(m["matched_text"], "日本語")
        r = self.agent("search", "emoji", "u.txt")
        m = r.data["results"][0]["matches"][0]
        self.assertEqual(m["column"], 3)  # emoji counts as 1 character
        self.assertEqual(self.agent("search", "wörld", "u.txt", "--mode", "literal"
                                    ).data["total_matches"], 1)

    def test_cp1252_even_length(self):
        # Regression: even-length legacy files "decode" as UTF-16 CJK garbage in the core.
        raw = "caf\xe9 ok\n".encode("cp1252")
        self.assertEqual(len(raw) % 2, 0)
        self.write("c.txt", raw, mode="wb")
        r = self.agent("search", "café", "c.txt")
        self.assertEqual(r.data["total_matches"], 1)
        self.assertEqual(r.data["results"][0]["encoding"], "cp1252")
        self.assertEqual(r.data["results"][0]["matches"][0]["content"], "café ok")

    def test_cp1252_odd_length(self):
        raw = "caf\xe9 okk\n".encode("cp1252")
        self.assertEqual(len(raw) % 2, 1)
        self.write("c.txt", raw, mode="wb")
        r = self.agent("search", "café", "c.txt")
        self.assertEqual(r.data["total_matches"], 1)
        self.assertEqual(r.data["results"][0]["encoding"], "cp1252")

    def test_cp1252_smart_quotes_and_euro(self):
        self.write("c.txt", "\u201cquoted\u201d \u20ac5 price\n".encode("cp1252"), mode="wb")
        r = self.agent("search", "\u20ac5", "c.txt")
        self.assertEqual(r.data["total_matches"], 1)

    def test_cp1252_in_directory_walk(self):
        self.write("d/c.txt", "r\xe9sum\xe9 ab\n".encode("cp1252"), mode="wb")
        self.write("d/u.txt", "r\xe9sum\xe9\n")
        r = self.agent("search", "résumé", "d")
        self.assertEqual([f["file"] for f in r.data["results"]], ["d/c.txt", "d/u.txt"])

    def test_utf16_with_bom(self):
        for name, enc in (("le.txt", "utf-16"), ("be.txt", "utf-16-be")):
            data = ("\ufeff" if enc == "utf-16-be" else "") + "first\nsecret ünïcode\n"
            self.write(name, data.encode(enc), mode="wb")
        r = self.agent("search", "secret", "le.txt", "be.txt")
        self.assertEqual([f["file"] for f in r.data["results"]], ["be.txt", "le.txt"])
        for f in r.data["results"]:
            self.assertTrue(f["encoding"].startswith("utf-16"))
            self.assertEqual(f["matches"][0]["line"], 2)

    def test_utf8_bom(self):
        self.write("b.txt", b"\xef\xbb\xbfhello bom\n", mode="wb")
        r = self.agent("search", "hello", "b.txt")
        self.assertEqual(r.data["results"][0]["matches"][0]["column"], 1)

    def test_crlf_line_numbers_and_content(self):
        self.write("w.txt", "one\r\ntwo needle\r\nthree\r\n")
        m = self.agent("search", "needle", "w.txt").data["results"][0]["matches"][0]
        self.assertEqual((m["line"], m["content"]), (2, "two needle"))

    def test_binary_files_skipped(self):
        self.write("bin.dat", b"needle\x00\x01\x02", mode="wb")
        self.write("noext", b"needle\x00\x01" * 10, mode="wb")
        self.write("ok.txt", "needle\n")
        r = self.agent("search", "needle")
        self.assertEqual([f["file"] for f in r.data["results"]], ["ok.txt"])
        self.assertGreaterEqual(sum(r.data["files_skipped"].values()), 2)
        self.assertTrue(any(w["code"] == "file_skipped" for w in r.json["warnings"]))

    def test_explicit_binary_is_warning_not_crash(self):
        self.write("bin.dat", b"needle\x00", mode="wb")
        r = self.agent("search", "needle", "bin.dat")
        self.assert_envelope(r, True, "search")
        self.assertEqual(r.data["total_matches"], 0)

    def test_large_file_skipped(self):
        self.write("big.txt", b"needle\n" * (1024 * 1024), mode="wb")  # ~7 MiB
        self.write("small.txt", "needle\n")
        r = self.agent("search", "needle")
        self.assertEqual([f["file"] for f in r.data["results"]], ["small.txt"])
        self.assertEqual(r.data["files_skipped"].get("too_large"), 1)

    def test_long_line_clipped(self):
        self.write("l.txt", "x" * 10000 + " needle\n")
        m = self.agent("search", "needle", "l.txt").data["results"][0]["matches"][0]
        self.assertEqual(len(m["content"]), 4000)
        self.assertTrue(m["content_truncated"] or m["column"] > 4000)
        self.assertEqual(m["column"], 10002)


class InvalidQueryTests(AgentTestCase):
    def assert_usage(self, r):
        self.assert_envelope(r, False, "search")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_empty_and_blank(self):
        self.write("a.txt", "x\n")
        for q in ("", "   "):
            self.assert_usage(self.agent("search", q, "a.txt"))

    def test_bad_mode_and_types_via_request(self):
        self.write("a.txt", "x\n")
        for args in ({"query": "x", "mode": "regex"},
                     {"query": "x", "multiline": "yes"},
                     {"query": "x", "case_sensitive": 1},
                     {"query": "x", "max_results": "5"},
                     {"query": "x", "context": True},
                     {"query": 5},
                     {"query": "x", "bogus": 1},
                     {"query": "x", "ext": "a/b"}):
            with self.subTest(args=args):
                req = {"operation": "search", "args": dict(args, paths=["a.txt"])}
                self.assert_usage(self.agent("--request", "-", stdin=json.dumps(req).encode()))

    def test_regex_metacharacters_are_literal_not_regex(self):
        self.write("a.txt", "f(x) [a-z]+ .* \\d\n")
        for q in ("[a-z]+", ".*", "\\d", "f(x)", "(unclosed", "[bad"):
            with self.subTest(q=q):
                r = self.agent("search", q, "a.txt", "--mode", "literal")
                self.assert_envelope(r, True, "search")
        self.assertEqual(self.agent("search", ".*", "a.txt", "--mode", "literal"
                                    ).data["total_matches"], 1)
        self.assertEqual(self.agent("search", "(unclosed", "a.txt").data["total_matches"], 0)

    def test_query_too_long(self):
        self.write("a.txt", "x\n")
        r = self.agent("search", "a" * 10001, "a.txt")
        self.assertEqual((r.code, r.error["code"]), (4, "limit_exceeded"))

    def test_request_mode_search(self):
        self.write("a.txt", "Hello World\n")
        req = {"operation": "search",
               "args": {"query": "hello world", "paths": ["a.txt"], "mode": "literal",
                        "case_sensitive": False, "context": 1}}
        r = self.agent("--request", "-", stdin=json.dumps(req).encode())
        self.assert_envelope(r, True, "search")
        self.assertEqual(r.data["total_matches"], 1)


class SandboxTests(AgentTestCase):
    def test_path_outside_root(self):
        self.write("a.txt", "needle\n")
        outside = os.path.dirname(self.root)
        for p in ("..", "../x.txt", outside):
            with self.subTest(p=p):
                r = self.agent("search", "needle", p)
                self.assert_envelope(r, False, "search")
                self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))

    def test_one_bad_path_among_good_fails_whole_request(self):
        self.write("a.txt", "needle\n")
        r = self.agent("search", "needle", "a.txt", "..")
        self.assertEqual(r.error["code"], "path_outside_root")
        self.assertIsNone(r.json["data"])

    def test_missing_path(self):
        r = self.agent("search", "x", "nope.txt")
        self.assertEqual((r.code, r.error["code"]), (3, "path_not_found"))

    def test_symlink_escape_skipped(self):
        outside = os.path.join(os.path.dirname(self.root), "outside_" + os.path.basename(self.root))
        os.makedirs(outside)
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        with open(os.path.join(outside, "secret.txt"), "w") as fh:
            fh.write("needle\n")
        self.write("ok.txt", "needle\n")
        try:
            os.symlink(os.path.join(outside, "secret.txt"), os.path.join(self.root, "link.txt"))
            os.symlink(outside, os.path.join(self.root, "linkdir"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted")
        r = self.agent("search", "needle")
        self.assertEqual([f["file"] for f in r.data["results"]], ["ok.txt"])
        self.assertNotIn("secret", r.text)
        self.assertGreaterEqual(sum(r.data["files_skipped"].values()), 1)
        r = self.agent("search", "needle", "link.txt")
        self.assertEqual(r.error["code"], "path_outside_root")

    def test_extra_root_permits_other_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as other:
            other = os.path.realpath(other)
            with open(os.path.join(other, "o.txt"), "w") as fh:
                fh.write("needle\n")
            r = run_agent("--root", self.root, "--root", other, "search", "needle", other,
                          cwd=self.root)
            self.assertEqual(r.data["total_matches"], 1)

    def test_search_never_writes(self):
        self.write("a.txt", "needle\n")
        before = sorted(os.listdir(self.root))
        self.agent("search", "needle")
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_does_not_execute_source(self):
        self.write("evil.py", "import os\nopen('PWNED','w').write('x')\n# needle\n")
        self.agent("search", "needle")
        self.assertFalse(os.path.exists(os.path.join(self.root, "PWNED")))
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "PWNED")))


class StdoutIntegrityTests(AgentTestCase):
    def test_stdout_single_json_line_success_and_error(self):
        self.write("a.txt", "needle ünï\n")
        ok = self.agent("search", "needle", "--verbose")
        self.assert_envelope(ok, True, "search")
        self.assertNotIn(b"\x1b", ok.raw_out)
        bad = self.agent("search", "needle", "..", "--verbose")
        self.assert_envelope(bad, False, "search")

    def test_non_ascii_query_and_output_with_legacy_console_encoding(self):
        self.write("u.txt", "日本語 needle\n")
        import subprocess, sys
        from agent_helpers import SCRIPTS
        env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="cp1252")
        p = subprocess.run([sys.executable, "-m", "codelens_agent", "--root", self.root,
                            "search", "日本語"], capture_output=True, cwd=self.root,
                           env=env, timeout=60)
        doc = json.loads(p.stdout.decode("utf-8"))
        self.assertTrue(doc["success"])
        self.assertEqual(doc["data"]["total_matches"], 1)

    def test_m1_envelope_keys_unchanged(self):
        self.write("a.txt", "x\n")
        r = self.agent("search", "x", "a.txt")
        self.assertEqual(set(r.json["data"]) >= {"query", "results"}, True)
        d = r.data["results"][0]["matches"][0]
        self.assertTrue({"line", "end_line", "column", "end_column", "content", "strategy"}
                        <= set(d))


class PerformanceTests(AgentTestCase):
    def test_many_files(self):
        for i in range(300):
            self.write(f"d{i % 10}/f{i}.txt", f"line\nneedle {i}\n")
        r = self.agent("search", "needle", "--max-results", "1000")
        self.assertEqual(r.data["total_matches"], 300)
        self.assertEqual(r.data["files_searched"], 300)

    def test_vendor_dirs_not_walked(self):
        self.write("node_modules/x/a.txt", "needle\n")
        self.write(".git/config", "needle\n")
        self.assertEqual(self.agent("search", "needle").data["total_matches"], 0)


if __name__ == "__main__":
    unittest.main()
