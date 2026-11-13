"""M3: pagination, count-only, files-only, result-limit semantics."""

import os
import sys
import unittest

from agent_helpers import AgentTestCase, SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def match_keys(data):
    """Ordered (file, line, column) identity of every match on a page."""
    return [(f["file"], m["line"], m["column"])
            for f in data["results"] for m in f["matches"]]


class PagingBase(AgentTestCase):
    def collect(self, *argv, page_size=7, max_pages=500):
        """Follows next_cursor to the end. -> (pages, flat match keys)."""
        pages, keys, cursor = [], [], None
        for _ in range(max_pages):
            extra = ["--cursor", cursor] if cursor else []
            r = self.agent("search", *argv, "--page-size", str(page_size), *extra)
            self.assertTrue(r.json["success"], r.text[:300])
            pages.append(r.data)
            keys.extend(match_keys(r.data))
            cursor = r.data["next_cursor"]
            if cursor is None:
                return pages, keys
        self.fail("pagination did not terminate")

    def seed(self, nfiles=5, per_file=5):
        for i in range(nfiles):
            self.write(f"d{i % 2}/f{i:03d}.txt",
                       "".join(f"line {j} needle needle\n" for j in range(per_file)))


class PaginationTests(PagingBase):
    def test_pages_equal_full_listing_no_dup_no_skip(self):
        self.seed(5, 5)  # 5 files * 5 lines * 2 matches/line = 50
        full = self.agent("search", "needle", "--max-results", "1000").data
        self.assertEqual(full["total_matches"], 50)
        pages, keys = self.collect("needle", page_size=7)
        self.assertEqual(keys, match_keys(full))           # same order, nothing lost
        self.assertEqual(len(keys), len(set(keys)))        # no duplicates
        self.assertEqual([p["total_matches"] for p in pages], [7] * 7 + [1])
        for p in pages[:-1]:
            self.assertEqual(p["more_results"], "confirmed")
            self.assertTrue(p["truncated"])
            self.assertTrue(p["limit_reached"])
        last = pages[-1]
        self.assertIsNone(last["next_cursor"])
        self.assertEqual(last["more_results"], "none")
        self.assertTrue(last["complete"])
        self.assertFalse(last["truncated"])

    def test_page_boundary_inside_a_line_and_file(self):
        self.write("a.txt", "needle needle needle\n")   # 3 matches on one line
        pages, keys = self.collect("needle", page_size=1)
        self.assertEqual(keys, [("a.txt", 1, 1), ("a.txt", 1, 8), ("a.txt", 1, 15)])
        self.assertEqual(len(pages), 3)

    def test_page_size_exact_multiple_has_no_phantom_page(self):
        self.write("a.txt", "needle\n" * 6)
        pages, keys = self.collect("needle", page_size=3)
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(keys), 6)
        self.assertIsNone(pages[-1]["next_cursor"])

    def test_deterministic_cursor(self):
        self.seed(3, 4)
        a = self.agent("search", "needle", "--page-size", "5").data["next_cursor"]
        b = self.agent("search", "needle", "--page-size", "5").data["next_cursor"]
        self.assertEqual(a, b)

    def test_page_size_changes_between_pages_still_consistent(self):
        self.write("a.txt", "needle\n" * 10)
        p1 = self.agent("search", "needle", "--page-size", "4").data
        p2 = self.agent("search", "needle", "--page-size", "3",
                        "--cursor", p1["next_cursor"]).data
        self.assertEqual([m["line"] for f in p2["results"] for m in f["matches"]], [5, 6, 7])

    def test_default_behaviour_unchanged_without_cursor(self):
        self.write("a.txt", "hit\n" * 3)
        d = self.agent("search", "hit", "a.txt").data
        self.assertEqual(d["total_matches"], 3)
        self.assertIsNone(d["next_cursor"])
        self.assertEqual(d["output"], "matches")

    def test_page_size_and_max_results_conflict(self):
        r = self.agent("search", "x", "--page-size", "5", "--max-results", "5")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))
        for bad in ("0", "1001"):
            r = self.agent("search", "x", "--page-size", bad)
            self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_resume_after_time_budget(self):
        """A time-budget stop is 'unknown', yields a cursor, and resuming
        returns exactly the rest."""
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.seed(4, 3)
        base = {"query": "needle"}
        full = ops.op_search(Sandbox([self.root]), dict(base, max_results=1000))[0]
        old = sb.SEARCH_TIME_BUDGET_SECONDS
        sb.SEARCH_TIME_BUDGET_SECONDS = -1
        try:
            first, warns = ops.op_search(Sandbox([self.root]), dict(base))
        finally:
            sb.SEARCH_TIME_BUDGET_SECONDS = old
        self.assertEqual(first["more_results"], "unknown")
        self.assertEqual(first["truncated_reason"], "time_budget_exceeded")
        self.assertFalse(first["complete"])
        self.assertTrue(first["truncated"])
        self.assertTrue(any(w["code"] == "results_truncated" for w in warns))
        self.assertIsNotNone(first["next_cursor"])
        rest = ops.op_search(Sandbox([self.root]),
                             dict(base, cursor=first["next_cursor"], max_results=1000))[0]
        self.assertEqual(match_keys(first) + match_keys(rest), match_keys(full))


class CapabilitiesM3Tests(AgentTestCase):
    def test_new_args_and_codes_additive(self):
        d = self.agent("capabilities").data
        names = {a["name"] for a in d["commands"]["search"]["args"]}
        self.assertTrue({"page_size", "cursor", "count_only", "files_only"} <= names)
        self.assertTrue({"invalid_cursor", "stale_cursor"} <= set(d["error_codes"]))
        self.assertEqual((d["interface_version"], d["schema_version"]), ("1", "1.0"))
        self.assertIn("5", d["exit_codes"])


class LimitSemanticsTests(PagingBase):
    def limit(self, n_matches, limit):
        self.write("m.txt", "hit\n" * n_matches)
        return self.agent("search", "hit", "m.txt", "--max-results", str(limit)).data

    def test_exactly_n_with_limit_n(self):
        d = self.limit(5, 5)
        self.assertEqual(d["total_matches"], 5)
        self.assertTrue(d["limit_reached"])
        self.assertEqual(d["more_results"], "none")
        self.assertTrue(d["complete"])
        self.assertFalse(d["truncated"])
        self.assertIsNone(d["next_cursor"])

    def test_n_plus_one_with_limit_n(self):
        d = self.limit(6, 5)
        self.assertEqual(d["total_matches"], 5)
        self.assertTrue(d["limit_reached"])
        self.assertEqual(d["more_results"], "confirmed")
        self.assertFalse(d["complete"])
        self.assertTrue(d["truncated"])
        self.assertEqual(d["truncated_reason"], "max_results")
        r = self.agent("search", "hit", "m.txt", "--max-results", "5",
                       "--cursor", d["next_cursor"])
        self.assertEqual([m["line"] for f in r.data["results"] for m in f["matches"]], [6])
        self.assertTrue(r.data["complete"])

    def test_fewer_than_n(self):
        d = self.limit(3, 5)
        self.assertEqual(d["total_matches"], 3)
        self.assertFalse(d["limit_reached"])
        self.assertTrue(d["complete"])
        self.assertFalse(d["truncated"])

    def test_empty_results(self):
        d = self.limit(0, 5)
        self.assertEqual((d["total_matches"], d["results"]), (0, []))
        self.assertFalse(d["limit_reached"])
        self.assertEqual(d["more_results"], "none")
        self.assertTrue(d["complete"])
        self.assertIsNone(d["next_cursor"])

    def test_limit_reached_during_large_search_stops_early(self):
        for i in range(400):
            self.write(f"d{i % 8}/f{i:04d}.txt", "needle\n")
        d = self.agent("search", "needle", "--max-results", "10").data
        self.assertEqual(d["total_matches"], 10)
        self.assertEqual(d["more_results"], "confirmed")
        self.assertLessEqual(d["files_searched"], 11)   # did not read all 400
        self.assertIsNotNone(d["next_cursor"])

    def test_budget_stop_is_never_reported_complete(self):
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.seed(3, 2)
        old = sb.MAX_SEARCH_TOTAL_BYTES
        sb.MAX_SEARCH_TOTAL_BYTES = 10
        try:
            d, _ = ops.op_search(Sandbox([self.root]), {"query": "needle"})
        finally:
            sb.MAX_SEARCH_TOTAL_BYTES = old
        self.assertEqual(d["truncated_reason"], "byte_budget_exceeded")
        self.assertEqual(d["more_results"], "unknown")
        self.assertFalse(d["complete"])

    def test_file_limit_is_never_reported_complete(self):
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.seed(6, 1)
        old = sb.MAX_SEARCH_FILES
        sb.MAX_SEARCH_FILES = 3
        try:
            d, w = ops.op_search(Sandbox([self.root]), {"query": "needle"})
        finally:
            sb.MAX_SEARCH_FILES = old
        self.assertFalse(d["complete"])
        self.assertEqual(d["more_results"], "unknown")
        self.assertEqual(d["truncated_reason"], "file_limit_reached")
        self.assertIsNone(d["next_cursor"])   # unexamined files are not addressable
        self.assertTrue(any(x["code"] == "file_limit_reached" for x in w))


class CountOnlyTests(PagingBase):
    def test_count_matches_full_search_and_has_no_snippets(self):
        self.seed(5, 5)
        full = self.agent("search", "needle", "--max-results", "1000").data
        r = self.agent("search", "needle", "--count-only")
        d = r.data
        self.assertEqual(d["total_matches"], full["total_matches"])
        self.assertEqual(d["files_with_matches"], 5)
        self.assertEqual(d["results"], [])
        self.assertTrue(d["count_complete"])
        self.assertIsNone(d["partial_reason"])
        self.assertTrue(d["complete"])
        self.assertNotIn("line 0", r.text)             # no source text returned

    def test_count_exceeds_max_results_and_is_exact(self):
        self.write("a.txt", "hit\n" * 1500)             # more than the 1000 cap
        d = self.agent("search", "hit", "--count-only").data
        self.assertEqual(d["total_matches"], 1500)
        self.assertTrue(d["count_complete"])

    def test_count_zero(self):
        self.write("a.txt", "x\n")
        d = self.agent("search", "needle", "--count-only").data
        self.assertEqual((d["total_matches"], d["count_complete"]), (0, True))

    def test_partial_count_reports_reason(self):
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.seed(6, 1)
        old = sb.MAX_SEARCH_FILES
        sb.MAX_SEARCH_FILES = 3
        try:
            d, _ = ops.op_search(Sandbox([self.root]),
                                 {"query": "needle", "count_only": True})
        finally:
            sb.MAX_SEARCH_FILES = old
        self.assertFalse(d["count_complete"])
        self.assertEqual(d["partial_reason"], "file_limit_reached")
        self.assertEqual((d["files_with_matches"], d["total_matches"]), (3, 6))  # only 3 files examined
        old = sb.SEARCH_TIME_BUDGET_SECONDS
        sb.SEARCH_TIME_BUDGET_SECONDS = -1
        try:
            d, _ = ops.op_search(Sandbox([self.root]),
                                 {"query": "needle", "count_only": True})
        finally:
            sb.SEARCH_TIME_BUDGET_SECONDS = old
        self.assertFalse(d["count_complete"])
        self.assertEqual(d["partial_reason"], "time_budget_exceeded")
        self.assertIsNone(d["next_cursor"])

    def test_count_only_rejects_cursor_and_files_only(self):
        r = self.agent("search", "x", "--count-only", "--files-only")
        self.assertEqual(r.error["code"], "invalid_argument")
        r = self.agent("search", "x", "--count-only", "--cursor", "abc")
        self.assertEqual(r.error["code"], "invalid_argument")


class FilesOnlyTests(PagingBase):
    def test_files_vs_matches_not_confused(self):
        self.write("a.txt", "needle\nneedle\nneedle\n")
        self.write("b.txt", "needle\n")
        self.write("c.txt", "nothing\n")
        r = self.agent("search", "needle", "--files-only")
        d = r.data
        self.assertEqual(d["files_with_matches"], 2)
        self.assertEqual(d["total_matches"], 4)
        self.assertEqual(d["files_searched"], 3)
        self.assertEqual([(f["file"], f["match_count"]) for f in d["files"]],
                         [("a.txt", 3), ("b.txt", 1)])
        self.assertTrue(d["total_matches_known"])
        self.assertTrue(d["complete"])
        self.assertFalse(d["truncated"])
        self.assertEqual(d["results"], [])
        self.assertNotIn('"content"', r.text)

    def test_files_only_paginates(self):
        for i in range(7):
            self.write(f"f{i}.txt", "needle\n" * (i + 1))
        seen, cursor, totals = [], None, set()
        for _ in range(10):
            extra = ["--cursor", cursor] if cursor else []
            d = self.agent("search", "needle", "--files-only", "--page-size", "3", *extra).data
            seen += [f["file"] for f in d["files"]]
            totals.add((d["files_with_matches"], d["total_matches"]))
            cursor = d["next_cursor"]
            if not cursor:
                break
        self.assertEqual(seen, [f"f{i}.txt" for i in range(7)])
        self.assertEqual(totals, {(7, 28)})

    def test_files_only_total_unknown_when_scan_cut_short(self):
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.seed(4, 1)
        old = sb.SEARCH_TIME_BUDGET_SECONDS
        sb.SEARCH_TIME_BUDGET_SECONDS = -1
        try:
            d, _ = ops.op_search(Sandbox([self.root]),
                                 {"query": "needle", "files_only": True})
        finally:
            sb.SEARCH_TIME_BUDGET_SECONDS = old
        self.assertIsNone(d["total_matches"])
        self.assertFalse(d["total_matches_known"])
        self.assertFalse(d["complete"])
        self.assertEqual(d["more_results"], "unknown")


if __name__ == "__main__":
    unittest.main()
