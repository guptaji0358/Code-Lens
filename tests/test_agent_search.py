import json
import os
import unittest

from agent_helpers import AgentTestCase


def hits(r):
    return [(f["file"], m["line"], m["end_line"], m["column"], m["end_column"], m["strategy"])
            for f in r.data["results"] for m in f["matches"]]


class SearchBasicTests(AgentTestCase):
    def test_exact_literal(self):
        self.write("a.py", "x = a+b\ny = a + b\n")
        r = self.agent("search", "a+b", "a.py", "--mode", "literal")
        self.assert_envelope(r, True, "search")
        self.assertEqual(hits(r), [("a.py", 1, 1, 5, 7, "literal")])
        m = r.data["results"][0]["matches"][0]
        self.assertEqual(m["matched_text"], "a+b")
        self.assertEqual(m["content"], "x = a+b")
        self.assertFalse(m["fallback_used"])

    def test_flexible_as_typed_vs_whitespace_insensitive(self):
        self.write("a.py", "foo(a, b)\nfoo( a,b )\n")
        r = self.agent("search", "foo(a, b)", "a.py")
        self.assertEqual([h[5] for h in hits(r)], ["as_typed", "whitespace_insensitive"])
        ms = r.data["results"][0]["matches"]
        self.assertTrue(ms[0]["exact_text_match"])
        self.assertFalse(ms[1]["exact_text_match"])

    def test_literal_does_not_relax_whitespace(self):
        self.write("a.py", "foo( a,b )\n")
        r = self.agent("search", "foo(a, b)", "a.py", "--mode", "literal")
        self.assertEqual(r.data["total_matches"], 0)

    def test_word_boundaries(self):
        self.write("a.py", "cat\nconcat\ncat_x\n(cat)\nCAT cats\n")
        r = self.agent("search", "cat", "a.py", "--mode", "word")
        self.assertEqual([h[1] for h in hits(r)], [1, 4, 5])
        self.assertTrue(all(h[5] == "word" for h in hits(r)))
        r = self.agent("search", "cat", "a.py", "--mode", "word", "--case-sensitive")
        self.assertEqual([h[1] for h in hits(r)], [1, 4])

    def test_word_mode_rejects_phrases(self):
        self.write("a.py", "x\n")
        r = self.agent("search", "two words", "a.py", "--mode", "word")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_multiline_flexible(self):
        self.write("a.py", "if (x) {\n    run();\n}\n")
        r = self.agent("search", "if (x) {\nrun();", "a.py", "--multiline")
        self.assertEqual(hits(r), [("a.py", 1, 2, 1, 10, "whitespace_insensitive")])
        m = r.data["results"][0]["matches"][0]
        self.assertEqual(m["matched_text"], "if (x) {\n    run();")
        self.assertEqual(m["content"], "if (x) {\n    run();")

    def test_multiline_literal_exact(self):
        self.write("a.py", "a\nb\n\na\n  b\n")
        r = self.agent("search", "a\nb", "a.py", "--mode", "literal", "--multiline")
        self.assertEqual(hits(r), [("a.py", 1, 2, 1, 1, "literal")])

    def test_newline_without_multiline_rejected(self):
        self.write("a.py", "a\nb\n")
        for mode in ("flexible", "literal"):
            r = self.agent("search", "a\nb", "a.py", "--mode", mode)
            self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_single_line_flexible_does_not_cross_lines(self):
        self.write("a.py", "foo(\na)\n")
        self.assertEqual(self.agent("search", "foo(a)", "a.py").data["total_matches"], 0)
        self.assertEqual(
            self.agent("search", "foo(a)", "a.py", "--multiline").data["total_matches"], 1)

    def test_case_sensitivity(self):
        self.write("a.txt", "Hello\nhello\nHELLO\n")
        self.assertEqual(self.agent("search", "hello", "a.txt").data["total_matches"], 3)
        r = self.agent("search", "hello", "a.txt", "--case-sensitive")
        self.assertEqual([h[1] for h in hits(r)], [2])
        self.assertTrue(r.data["case_sensitive"])

    def test_multiple_matches_per_line_ordered(self):
        self.write("a.txt", "ab ab ab\n")
        r = self.agent("search", "ab", "a.txt", "--mode", "literal")
        self.assertEqual([h[3] for h in hits(r)], [1, 4, 7])

    def test_empty_results(self):
        self.write("a.txt", "nothing here\n")
        r = self.agent("search", "zzz", "a.txt")
        self.assert_envelope(r, True, "search")
        self.assertEqual((r.data["results"], r.data["total_matches"], r.data["truncated"]),
                         ([], 0, False))
        self.assertEqual(r.json["warnings"], [])

    def test_default_path_is_root_recursive(self):
        self.write("sub/deep/a.txt", "needle\n")
        r = self.agent("search", "needle")
        self.assertEqual(hits(r)[0][0], "sub/deep/a.txt")


class CommentFallbackTests(AgentTestCase):
    def test_query_comment_removed(self):
        self.write("a.c", "int x = 1;\nint y = 2;\n")
        r = self.agent("search", "int x = 1; // my note", "a.c", "--multiline")
        m = r.data["results"][0]["matches"][0]
        self.assertEqual(m["strategy"], "snippet_comments_removed")
        self.assertTrue(m["fallback_used"] and m["comments_stripped"])
        self.assertEqual(m["comments_stripped_from"], "query")
        self.assertFalse(m["exact_text_match"])

    def test_file_comment_stripped(self):
        self.write("a.c", "int x = /* hidden */ 1;\n")
        r = self.agent("search", "int x = 1;", "a.c", "--multiline")
        m = r.data["results"][0]["matches"][0]
        self.assertEqual(m["strategy"], "file_comments_stripped")
        self.assertEqual(m["comments_stripped_from"], "file")
        self.assertIn("/* hidden */", m["matched_text"])  # original source shown

    def test_fallback_not_used_when_direct_match(self):
        self.write("a.c", "int x = 1; // note\n")
        r = self.agent("search", "int x = 1;", "a.c", "--multiline")
        self.assertFalse(r.data["results"][0]["matches"][0]["fallback_used"])

    def test_fallback_can_be_disabled(self):
        self.write("a.c", "int x = /* hidden */ 1;\n")
        r = self.agent("search", "int x = 1;", "a.c", "--multiline", "--no-comment-fallback")
        self.assertEqual(r.data["total_matches"], 0)
        self.assertFalse(r.data["comment_fallback"])

    def test_no_fallback_for_literal_or_single_line(self):
        self.write("a.c", "int x = /* hidden */ 1;\n")
        self.assertEqual(self.agent("search", "int x = 1;", "a.c").data["total_matches"], 0)
        r = self.agent("search", "int x = 1;", "a.c", "--mode", "literal", "--multiline")
        self.assertEqual(r.data["total_matches"], 0)


class FilterAndLimitTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/a.py", "token\n")
        self.write("src/b.js", "token\n")
        self.write("src/sub/c.py", "token\n")
        self.write("docs/d.md", "token\n")
        self.write("node_modules/e.js", "token\n")

    def files(self, *extra):
        r = self.agent("search", "token", *extra)
        self.assertTrue(r.json["success"], r.json)
        return [f["file"] for f in r.data["results"]]

    def test_recursive_sorted_and_skips_vendor_dirs(self):
        self.assertEqual(self.files(),
                         ["docs/d.md", "src/a.py", "src/b.js", "src/sub/c.py"])

    def test_multiple_paths(self):
        self.assertEqual(self.files("src/a.py", "docs"), ["docs/d.md", "src/a.py"])

    def test_ext_filter(self):
        self.assertEqual(self.files("--ext", "py"), ["src/a.py", "src/sub/c.py"])
        self.assertEqual(self.files("--ext", ".js", "--ext", "md"), ["docs/d.md", "src/b.js"])

    def test_include_glob(self):
        # fnmatch semantics: '*' also matches '/', so src/*.py covers subdirs
        self.assertEqual(self.files("--include", "src/*.py"), ["src/a.py", "src/sub/c.py"])
        self.assertEqual(self.files("--include", "*.md"), ["docs/d.md"])

    def test_exclude_glob(self):
        self.assertEqual(self.files("--exclude", "src/*"), ["docs/d.md"])
        self.assertEqual(self.files("--exclude", "sub"), ["docs/d.md", "src/a.py", "src/b.js"])
        self.assertEqual(self.files("--exclude", "*.py", "--ext", "py"), [])

    def test_deterministic(self):
        a = self.agent("search", "token").text
        b = self.agent("search", "token").text
        self.assertEqual(a, b)

    def test_context_lines(self):
        self.write("ctx.txt", "".join(f"l{i}\n" for i in range(1, 11)).replace("l5", "HIT"))
        r = self.agent("search", "HIT", "ctx.txt", "--context", "2")
        m = r.data["results"][0]["matches"][0]
        self.assertEqual([x["line"] for x in m["context_before"]], [3, 4])
        self.assertEqual([x["text"] for x in m["context_after"]], ["l6", "l7"])

    def test_context_clamped_at_edges_and_bounds(self):
        self.write("ctx.txt", "HIT\nx\n")
        m = self.agent("search", "HIT", "ctx.txt", "--context", "5").data["results"][0]["matches"][0]
        self.assertEqual((m["context_before"], len(m["context_after"])), ([], 1))
        r = self.agent("search", "HIT", "ctx.txt", "--context", "999")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_multiline_context_surrounds_whole_match(self):
        self.write("m.txt", "a\nb\nc\nd\ne\n")
        m = self.agent("search", "b\nc", "m.txt", "--multiline", "--context", "1"
                       ).data["results"][0]["matches"][0]
        self.assertEqual([x["line"] for x in m["context_before"]], [1])
        self.assertEqual([x["line"] for x in m["context_after"]], [4])

    def test_result_limit(self):
        self.write("many.txt", "hit\n" * 50)
        r = self.agent("search", "hit", "many.txt", "--max-results", "5")
        self.assertEqual(r.data["total_matches"], 5)
        self.assertTrue(r.data["truncated"])
        self.assertEqual(r.data["truncated_reason"], "max_results")
        self.assertEqual(r.json["warnings"][0]["code"], "results_truncated")

    def test_result_limit_across_files(self):
        r = self.agent("search", "token", "--max-results", "2")
        self.assertEqual(r.data["total_matches"], 2)
        self.assertEqual(len(r.data["results"]), 2)

    def test_exact_limit_not_truncated(self):
        self.write("many.txt", "hit\n" * 5)
        r = self.agent("search", "hit", "many.txt", "--max-results", "5")
        self.assertFalse(r.data["truncated"])

    def test_invalid_limits(self):
        for bad in ("0", "-1", "1001"):
            r = self.agent("search", "token", "--max-results", bad)
            self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"), bad)


if __name__ == "__main__":
    unittest.main()
