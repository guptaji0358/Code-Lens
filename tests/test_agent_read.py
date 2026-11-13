import json
import unittest

from agent_helpers import AgentTestCase


def numbered(n):
    return "".join(f"line {i}\n" for i in range(1, n + 1))


class ReadTests(AgentTestCase):
    def test_single_range(self):
        self.write("a.txt", numbered(10))
        r = self.agent("read", "a.txt", "--lines", "3-5")
        self.assert_envelope(r, True, "read")
        rg = r.data["ranges"][0]
        self.assertEqual([(x["line"], x["text"]) for x in rg["lines"]],
                         [(3, "line 3"), (4, "line 4"), (5, "line 5")])
        self.assertEqual(r.data["total_lines"], 10)
        self.assertFalse(r.data["truncated"])

    def test_single_line_form(self):
        self.write("a.txt", numbered(10))
        r = self.agent("read", "a.txt", "--lines", "7")
        self.assertEqual([x["line"] for x in r.data["ranges"][0]["lines"]], [7])

    def test_non_contiguous_ranges_in_order(self):
        self.write("a.txt", numbered(20))
        r = self.agent("read", "a.txt", "--lines", "15-16", "--lines", "2-3")
        got = [[x["line"] for x in rg["lines"]] for rg in r.data["ranges"]]
        self.assertEqual(got, [[15, 16], [2, 3]])

    def test_end_clamped_to_file(self):
        self.write("a.txt", numbered(5))
        rg = self.agent("read", "a.txt", "--lines", "4-99").data["ranges"][0]
        self.assertEqual((rg["start"], rg["end"], rg["clamped_to_file_end"]), (4, 5, True))

    def test_start_beyond_file(self):
        self.write("a.txt", numbered(5))
        r = self.agent("read", "a.txt", "--lines", "6-9")
        self.assert_envelope(r, False, "read")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_invalid_ranges(self):
        self.write("a.txt", numbered(5))
        for bad in ("abc", "5-2", "0-3", "-3", "1-2-3", ""):
            with self.subTest(bad=bad):
                r = self.agent("read", "a.txt", "--lines", bad)
                self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_around(self):
        self.write("a.txt", numbered(30))
        r = self.agent("read", "a.txt", "--around", "10", "--context", "2")
        self.assertEqual([x["line"] for x in r.data["ranges"][0]["lines"]],
                         [8, 9, 10, 11, 12])

    def test_around_clamps_at_edges(self):
        self.write("a.txt", numbered(4))
        r = self.agent("read", "a.txt", "--around", "1", "--context", "10")
        self.assertEqual([x["line"] for x in r.data["ranges"][0]["lines"]], [1, 2, 3, 4])

    def test_around_default_context(self):
        self.write("a.txt", numbered(30))
        r = self.agent("read", "a.txt", "--around", "15")
        self.assertEqual(len(r.data["ranges"][0]["lines"]), 11)

    def test_around_beyond_file(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt", "--around", "50")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_lines_and_around_exclusive(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt", "--lines", "1", "--around", "2")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_context_requires_around(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt", "--context", "2")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_context_out_of_bounds(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt", "--around", "1", "--context", "999")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_whole_file_default(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt")
        self.assertEqual(len(r.data["ranges"][0]["lines"]), 3)

    def test_empty_file(self):
        self.write("e.txt", "")
        r = self.agent("read", "e.txt")
        self.assert_envelope(r, True, "read")
        self.assertEqual((r.data["total_lines"], r.data["ranges"]), (0, []))

    def test_truncation_at_line_limit(self):
        self.write("big.txt", numbered(2500))
        r = self.agent("read", "big.txt")
        rg = r.data["ranges"][0]
        self.assertEqual(len(rg["lines"]), 2000)
        self.assertTrue(rg["truncated"] and r.data["truncated"])
        self.assertEqual(rg["next_start"], 2001)
        self.assertEqual(r.json["warnings"][0]["code"], "output_truncated")
        r2 = self.agent("read", "big.txt", "--lines", "2001-2500")
        self.assertEqual(len(r2.data["ranges"][0]["lines"]), 500)

    def test_too_many_ranges(self):
        self.write("a.txt", numbered(3))
        args = []
        for _ in range(51):
            args += ["--lines", "1"]
        r = self.agent("read", "a.txt", *args)
        self.assertEqual((r.code, r.error["code"]), (4, "limit_exceeded"))

    def test_long_line_truncated(self):
        self.write("l.txt", "x" * 10000 + "\nshort\n")
        rg = self.agent("read", "l.txt").data["ranges"][0]
        self.assertEqual(len(rg["lines"][0]["text"]), 4000)
        self.assertEqual(rg["truncated_long_lines"], [1])

    def test_file_too_large(self):
        self.write("big.txt", b"a\n" * (3 * 1024 * 1024), mode="wb")
        r = self.agent("read", "big.txt")
        self.assertEqual((r.code, r.error["code"]), (4, "file_too_large"))

    def test_binary_refused(self):
        self.write("b.bin", b"\x00\x01" * 50, mode="wb")
        r = self.agent("read", "b.bin")
        self.assertEqual((r.code, r.error["code"]), (3, "not_text_file"))

    def test_missing_file(self):
        r = self.agent("read", "nope.txt", "--lines", "1")
        self.assertEqual((r.code, r.error["code"]), (3, "path_not_found"))

    def test_unicode_content(self):
        self.write("u.txt", "héllo wörld\n日本語のテキスト\n🙂 emoji\n")
        r = self.agent("read", "u.txt")
        texts = [x["text"] for x in r.data["ranges"][0]["lines"]]
        self.assertEqual(texts, ["héllo wörld", "日本語のテキスト", "🙂 emoji"])
        # the core labels plain UTF-8 as "utf-8-sig" (first in its attempt list)
        self.assertIn(r.data["encoding"], ("utf-8", "utf-8-sig"))

    def test_cp1252_decoded(self):
        self.write("c.txt", "caf\xe9 ok\n".encode("cp1252"), mode="wb")
        r = self.agent("read", "c.txt")
        self.assertEqual(r.data["ranges"][0]["lines"][0]["text"], "café ok")

    def test_editor_compatible_line_numbers(self):
        # form feed and U+2028 are NOT line breaks for editors
        self.write("ff.txt", "one\x0ctwo\nthree four\nfive\n")
        r = self.agent("read", "ff.txt")
        self.assertEqual(r.data["total_lines"], 3)
        self.assertEqual(r.data["ranges"][0]["lines"][2], {"line": 3, "text": "five"})

    def test_crlf_and_lone_cr(self):
        self.write("x.txt", "a\r\nb\rc\n")
        self.assertEqual(self.agent("read", "x.txt").data["total_lines"], 3)

    def test_reads_fresh_after_modification(self):
        p = self.write("f.txt", "old\n")
        self.assertEqual(self.agent("read", "f.txt").data["ranges"][0]["lines"][0]["text"], "old")
        with open(p, "w", newline="") as fh:
            fh.write("new\n")
        self.assertEqual(self.agent("read", "f.txt").data["ranges"][0]["lines"][0]["text"], "new")

    def test_stdout_clean_with_stderr_noise_enabled(self):
        self.write("a.txt", numbered(3))
        r = self.agent("read", "a.txt", "--verbose")
        self.assert_envelope(r, True, "read")
        self.assertNotIn(b"\x1b", r.raw_out)


class RequestModeTests(AgentTestCase):
    def test_json_request_stdin(self):
        self.write("a.txt", numbered(10))
        req = {"operation": "read", "args": {"file": "a.txt", "lines": ["2-3"]}}
        r = self.agent("--request", "-", stdin=json.dumps(req).encode())
        self.assert_envelope(r, True, "read")
        self.assertEqual(len(r.data["ranges"][0]["lines"]), 2)

    def test_json_request_roots(self):
        self.write("a.txt", numbered(2))
        req = {"operation": "info", "args": {"file": "a.txt"}, "roots": [self.root]}
        from agent_helpers import run_agent
        r = run_agent("--request", "-", stdin=json.dumps(req).encode(), cwd=self.root)
        self.assertTrue(r.json["success"])

    def test_json_request_invalid(self):
        for bad in (b"not json", b"[]", b'{"args":{}}', b'{"operation":"read","args":[]}',
                    b'{"operation":"nope"}'):
            with self.subTest(bad=bad):
                r = self.agent("--request", "-", stdin=bad)
                self.assert_envelope(r, False, r.json["operation"])
                self.assertEqual(r.code, 2)

    def test_request_with_unknown_arg(self):
        self.write("a.txt", "x\n")
        req = {"operation": "info", "args": {"file": "a.txt", "evil": 1}}
        r = self.agent("--request", "-", stdin=json.dumps(req).encode())
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_request_type_errors(self):
        self.write("a.txt", "x\n")
        req = {"operation": "read", "args": {"file": "a.txt", "around": "3"}}
        r = self.agent("--request", "-", stdin=json.dumps(req).encode())
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_request_cannot_combine_with_command(self):
        r = self.agent("--request", "-", "info", "a.txt", stdin=b"{}")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))


class SchemaCompatibilityTests(AgentTestCase):
    """Pins the v1 field sets: removing/renaming any of these is breaking."""

    def test_read_data_fields(self):
        self.write("a.txt", "x\n")
        d = self.agent("read", "a.txt").data
        self.assertTrue({"file", "absolute_path", "total_lines", "encoding",
                         "line_ending", "ranges", "truncated"} <= set(d))
        rg = d["ranges"][0]
        self.assertTrue({"requested", "start", "end", "clamped_to_file_end", "truncated",
                         "next_start", "truncated_long_lines", "lines"} <= set(rg))
        self.assertEqual(set(rg["lines"][0]), {"line", "text"})

    def test_info_data_fields(self):
        self.write("a.txt", "x\n")
        d = self.agent("info", "a.txt").data
        self.assertTrue({"file", "absolute_path", "kind", "size_bytes", "modified",
                         "modified_epoch", "is_text", "content_inspected", "encoding",
                         "line_count", "line_ending", "ends_with_newline"} <= set(d))


if __name__ == "__main__":
    unittest.main()
