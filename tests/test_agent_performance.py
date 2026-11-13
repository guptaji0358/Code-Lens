"""M3: scale and resource behaviour. Timings are printed to stderr for the
completion report; assertions use generous bounds so they do not flake."""

import os
import sys
import time
import tracemalloc
import unittest

from agent_helpers import AgentTestCase, SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def note(msg):
    sys.stderr.write(f"\n[perf] {msg}\n")


class ScaleTests(AgentTestCase):
    def make_tree(self, n, dirs=20):
        for i in range(n):
            self.write(f"d{i % dirs}/f{i:05d}.txt", f"alpha\nneedle {i}\nomega\n")

    def test_1200_files_count_files_and_pages(self):
        self.make_tree(1200)
        t = time.perf_counter()
        d = self.agent("search", "needle", "--count-only").data
        t_count = time.perf_counter() - t
        self.assertEqual((d["total_matches"], d["files_searched"], d["count_complete"]),
                         (1200, 1200, True))
        t = time.perf_counter()
        d = self.agent("search", "needle", "--files-only", "--page-size", "1000").data
        t_files = time.perf_counter() - t
        self.assertEqual((d["files_with_matches"], d["total_matches"], len(d["files"])),
                         (1200, 1200, 1000))
        self.assertIsNotNone(d["next_cursor"])
        # repeated pagination over every match
        t = time.perf_counter()
        cursor, keys, pages = None, [], 0
        while True:
            extra = ["--cursor", cursor] if cursor else []
            r = self.agent("search", "needle", "--page-size", "100", *extra)
            keys += [(f["file"], m["line"]) for f in r.data["results"] for m in f["matches"]]
            pages += 1
            cursor = r.data["next_cursor"]
            if not cursor:
                break
        t_pages = time.perf_counter() - t
        self.assertEqual((pages, len(keys), len(set(keys))), (12, 1200, 1200))
        note(f"1200 files: count-only {t_count:.2f}s, files-only {t_files:.2f}s, "
             f"{pages} pages {t_pages:.2f}s ({t_pages / pages:.2f}s/page, each a fresh process)")
        self.assertLess(t_count, 30)
        self.assertLess(t_pages / pages, 15)

    def test_deep_directory_tree(self):
        parts = ["d"] * 40
        rel = "/".join(parts)
        self.write(f"{rel}/deep.txt", "needle deep\n")
        self.write("top.txt", "needle top\n")
        d = self.agent("search", "needle", "--page-size", "1").data
        self.assertEqual(d["more_results"], "confirmed")
        d2 = self.agent("search", "needle", "--page-size", "1", "--cursor", d["next_cursor"]).data
        files = [f["file"] for f in d["results"]] + [f["file"] for f in d2["results"]]
        self.assertEqual(sorted(files), sorted([f"{rel}/deep.txt", "top.txt"]))
        self.assertTrue(d2["complete"])

    def test_large_file_many_matches_in_memory_pagination(self):
        self.write("big.txt", "needle needle needle\n" * 20000)   # 60k matches, ~420 KB
        from codelens_agent import ops
        from codelens_agent.sandbox import Sandbox
        sb = Sandbox([self.root])
        ops.op_search(sb, {"query": "needle", "page_size": 10})    # warm imports
        tracemalloc.start()
        t = time.perf_counter()
        d, _ = ops.op_search(sb, {"query": "needle", "count_only": True})
        t_count = time.perf_counter() - t
        _, peak_count = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        page, _ = ops.op_search(sb, {"query": "needle", "page_size": 1000})
        _, peak_page = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(d["total_matches"], 60000)
        self.assertEqual(page["total_matches"], 1000)
        note(f"60k matches in one file: count-only {t_count:.2f}s peak {peak_count / 1e6:.1f} MB; "
             f"1000-match page peak {peak_page / 1e6:.1f} MB")
        self.assertLess(peak_page, 200 * 1024 * 1024)

    def test_search_timeout_via_real_budget(self):
        """With a tiny time budget the search stops, says so, and does not
        claim completeness."""
        from codelens_agent import ops, sandbox as sb
        from codelens_agent.sandbox import Sandbox
        self.make_tree(300)
        old = sb.SEARCH_TIME_BUDGET_SECONDS
        sb.SEARCH_TIME_BUDGET_SECONDS = 0.02
        try:
            d, w = ops.op_search(Sandbox([self.root]), {"query": "needle", "max_results": 1000})
        finally:
            sb.SEARCH_TIME_BUDGET_SECONDS = old
        self.assertEqual(d["truncated_reason"], "time_budget_exceeded")
        self.assertFalse(d["complete"])
        self.assertLess(d["files_searched"], 300)
        self.assertEqual(d["more_results"], "unknown")
        self.assertIsNotNone(d["next_cursor"])
        self.assertTrue(any(x["code"] == "results_truncated" for x in w))

    def test_limits_unchanged(self):
        from codelens_agent import sandbox as sb
        self.assertEqual((sb.MAX_FILE_BYTES, sb.MAX_SEARCH_FILES, sb.MAX_SEARCH_RESULTS,
                          sb.DEFAULT_SEARCH_RESULTS, sb.MAX_SEARCH_TOTAL_BYTES,
                          sb.SEARCH_TIME_BUDGET_SECONDS),
                         (5 * 1024 * 1024, 20000, 1000, 100, 256 * 1024 * 1024, 30.0))


if __name__ == "__main__":
    unittest.main()
