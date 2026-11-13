"""M3: cursor validation/binding/staleness and sandbox regression tests
(directory junctions, file symlinks, root changes, tampering)."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from agent_helpers import AgentTestCase, run_agent


def b64(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


class CursorBase(AgentTestCase):
    def first_page(self, *extra, query="needle"):
        self.write("a.txt", "needle\n" * 4)
        self.write("b/c.txt", "needle\n" * 4)
        r = self.agent("search", query, "--page-size", "3", *extra)
        self.assertTrue(r.json["success"], r.text[:300])
        self.assertIsNotNone(r.data["next_cursor"])
        return r.data["next_cursor"]

    def page(self, cursor, *argv, query="needle"):
        return self.agent("search", query, "--page-size", "3", *argv, "--cursor", cursor)


class CursorBindingTests(CursorBase):
    def assert_foreign(self, r):
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_cursor"), r.text[:300])

    def test_same_request_accepted(self):
        c = self.first_page()
        self.assertTrue(self.page(c).json["success"])

    def test_query_mode_case_multiline_comment_changes_rejected(self):
        c = self.first_page()
        self.assert_foreign(self.page(c, query="needl"))
        self.assert_foreign(self.page(c, "--mode", "literal"))
        self.assert_foreign(self.page(c, "--case-sensitive"))
        self.assert_foreign(self.page(c, "--multiline"))
        self.assert_foreign(self.page(c, "--no-comment-fallback"))

    def test_filters_paths_context_changes_rejected(self):
        c = self.first_page()
        self.assert_foreign(self.page(c, "--ext", "txt"))
        self.assert_foreign(self.page(c, "--include", "*.txt"))
        self.assert_foreign(self.page(c, "--exclude", "zzz"))
        self.assert_foreign(self.page(c, "--context", "2"))
        self.assert_foreign(self.agent("search", "needle", "b", "--page-size", "3",
                                       "--cursor", c))

    def test_match_cursor_not_usable_for_files_only(self):
        c = self.first_page()
        self.assert_foreign(self.page(c, "--files-only"))

    def test_root_change_between_pages_rejected(self):
        c = self.first_page()
        with tempfile.TemporaryDirectory() as other:
            other = os.path.realpath(other)
            for name in ("a.txt", "b/c.txt"):
                p = os.path.join(other, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write("needle\n" * 4)
            r = run_agent("--root", other, "search", "needle", "--page-size", "3",
                          "--cursor", c, cwd=other)
            self.assert_foreign(r)
            r = run_agent("--root", self.root, "--root", other, "search", "needle",
                          "--page-size", "3", "--cursor", c, cwd=self.root)
            self.assert_foreign(r)


class CursorStaleTests(CursorBase):
    def assert_stale(self, r):
        self.assertEqual((r.code, r.error["code"]), (5, "stale_cursor"), r.text[:300])
        self.assertIn("without --cursor", r.error["hint"])      # tells agent to restart
        self.assertIsNone(r.json["data"])

    def test_modified_file_content(self):
        c = self.first_page()
        time.sleep(0.05)
        self.write("b/c.txt", "needle\n" * 4 + "more\n")
        self.assert_stale(self.page(c))

    def test_modified_file_before_position(self):
        c = self.first_page()
        self.write("a.txt", "needle\n" * 3)       # size change in an already-served file
        self.assert_stale(self.page(c))

    def test_added_file(self):
        c = self.first_page()
        self.write("a0.txt", "needle\n")           # sorts before the cursor position
        self.assert_stale(self.page(c))

    def test_deleted_file(self):
        c = self.first_page()
        os.remove(os.path.join(self.root, "b", "c.txt"))
        self.assert_stale(self.page(c))

    def test_mtime_only_change(self):
        c = self.first_page()
        p = os.path.join(self.root, "a.txt")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assert_stale(self.page(c))

    def test_unchanged_files_not_stale(self):
        c = self.first_page()
        self.assertTrue(self.page(c).json["success"])
        self.assertTrue(self.page(c).json["success"])     # repeatable / stateless

    def test_files_only_cursor_goes_stale_too(self):
        for i in range(5):
            self.write(f"f{i}.txt", "needle\n")
        d = self.agent("search", "needle", "--files-only", "--page-size", "2").data
        self.write("f9.txt", "needle\n")
        r = self.agent("search", "needle", "--files-only", "--page-size", "2",
                       "--cursor", d["next_cursor"])
        self.assertEqual(r.error["code"], "stale_cursor")

    def test_unknown_limit_of_metadata_fingerprint_documented(self):
        """Same size + restored mtime is NOT detected (documented limitation
        of metadata fingerprints). Asserted so the limitation stays honest."""
        c = self.first_page()
        p = os.path.join(self.root, "b", "c.txt")
        st = os.stat(p)
        with open(p, "w", newline="") as fh:
            fh.write("needle\n" * 4)               # identical size
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(self.page(c).json["success"])


class CursorMalformedTests(CursorBase):
    def bad(self, cursor):
        r = self.agent("search", "needle", "--cursor", cursor)
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_cursor"),
                         f"{cursor[:40]!r}: {r.text[:200]}")
        return r

    def test_garbage_variants(self):
        self.write("a.txt", "needle\n")
        for c in ("not a cursor", "!!!!", "%00", "../../etc/passwd", "C:\\Windows",
                  "A" * 600, "=", "e30", b64(b"\xff\xfe\x00"), b64(b"not json"),
                  b64("[]"), b64([1, 2]), b64({}), b64("null")):
            self.bad(c)

    def test_empty_cursor_not_silently_ignored_in_request(self):
        self.write("a.txt", "needle\n")
        r = run_agent("--root", self.root, "--request", "-", cwd=self.root,
                      stdin=json.dumps({"operation": "search",
                                        "args": {"query": "needle", "cursor": 5}}).encode())
        self.assertEqual(r.error["code"], "invalid_argument")

    def test_structure_tampering(self):
        c = decode(self.first_page())
        cases = [
            dict(c, v=2), dict(c, k="x"), dict(c, p="zz" * 16), dict(c, s="ab"),
            dict(c, f=-1), dict(c, o=-5), dict(c, f=True), dict(c, o="1"),
            dict(c, f=10 ** 12), dict(c, f=1.5),
            dict(c, path="C:/Windows/win.ini"),            # extra key
            {k: v for k, v in c.items() if k != "o"},       # missing key
        ]
        for case in cases:
            self.bad(b64(case))

    def test_fingerprint_tampering(self):
        c = decode(self.first_page())
        flipped_p = dict(c, p=("0" if c["p"][0] != "0" else "1") + c["p"][1:])
        self.assertEqual(self.agent("search", "needle", "--cursor", b64(flipped_p)
                                    ).error["code"], "invalid_cursor")
        flipped_s = dict(c, s=("0" if c["s"][0] != "0" else "1") + c["s"][1:])
        self.assertEqual(self.agent("search", "needle", "--cursor", b64(flipped_s)
                                    ).error["code"], "stale_cursor")

    def test_out_of_range_position(self):
        c = decode(self.first_page())
        r = self.agent("search", "needle", "--cursor", b64(dict(c, f=9999)))
        self.assertEqual(r.error["code"], "invalid_cursor")

    def test_in_range_tamper_cannot_leak_outside_roots(self):
        """f/o are plain indices into the server-derived, sandboxed file list;
        tampering can only move within it."""
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "secret.txt"), "w") as fh:
            fh.write("needle TOPSECRET\n")
        c = decode(self.first_page())
        for f, o in ((0, 0), (1, 0), (2, 0), (0, 3), (1, 999)):
            r = self.agent("search", "needle", "--cursor", b64(dict(c, f=f, o=o)))
            self.assertNotIn("TOPSECRET", r.text)
            if r.json["success"]:
                for res in r.data["results"]:
                    self.assertFalse(os.path.isabs(res["file"]), res["file"])

    def test_cursor_does_not_expose_paths(self):
        raw = base64.urlsafe_b64decode(self.first_page() + "==")
        self.assertNotIn(os.path.basename(self.root).encode(), raw)
        self.assertNotIn(b"a.txt", raw)
        self.assertNotIn(b"\\", raw)
        self.assertLess(len(raw), 300)


def _mk_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True)
    return r.returncode == 0 and os.path.isdir(link)


class SandboxRegressionTests(AgentTestCase):
    """AUTOMATED. Junctions need no elevation on Windows (mklink /J)."""

    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="cl_out_")
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        with open(os.path.join(self.outside, "secret.txt"), "w") as fh:
            fh.write("needle TOPSECRET\n")
        self.write("ok.txt", "needle\n")

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows-only")
    def test_junction_inside_root_pointing_outside_not_walked(self):
        link = os.path.join(self.root, "jn")
        if not _mk_junction(link, self.outside):
            self.skipTest("cannot create junction")
        # os.rmdir removes the junction itself without touching the target.
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        for argv in (("search", "needle"), ("search", "needle", "--count-only"),
                     ("search", "needle", "--files-only")):
            r = self.agent(*argv)
            self.assertNotIn("TOPSECRET", r.text)
            self.assertNotIn("secret.txt", r.text)
            d = r.data
            if "files" in d and d["files"]:
                self.assertEqual([f["file"] for f in d["files"]], ["ok.txt"])
            self.assertEqual(d["total_matches"], 1)
            self.assertEqual(d["files_skipped"].get("outside_root"), 1)
        # explicit paths through the junction are refused
        for sub in ("jn", "jn/secret.txt"):
            r = self.agent("search", "needle", sub)
            self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))
        for cmd in (("info", "jn/secret.txt"), ("read", "jn/secret.txt")):
            self.assertEqual(self.agent(*cmd).error["code"], "path_outside_root")
        # a skip warning must not reveal the outside absolute path
        self.assertNotIn(self.outside.replace("\\", "/"), self.agent("search", "needle").text)

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows-only")
    def test_junction_to_inside_root_dir_not_followed_twice(self):
        os.makedirs(os.path.join(self.root, "real"))
        self.write("real/x.txt", "needle\n")
        link = os.path.join(self.root, "alias")
        if not _mk_junction(link, os.path.join(self.root, "real")):
            self.skipTest("cannot create junction")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        d = self.agent("search", "needle").data
        self.assertEqual(sorted(f["file"] for f in d["results"]), ["ok.txt", "real/x.txt"])

    @unittest.skipUnless(os.name == "nt", "directory junctions are Windows-only")
    def test_paginating_does_not_leak_via_junction(self):
        link = os.path.join(self.root, "jn")
        if not _mk_junction(link, self.outside):
            self.skipTest("cannot create junction")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        self.write("m.txt", "needle\n" * 5)
        cursor, seen = None, []
        for _ in range(10):
            extra = ["--cursor", cursor] if cursor else []
            r = self.agent("search", "needle", "--page-size", "2", *extra)
            self.assertNotIn("TOPSECRET", r.text)
            seen += [f["file"] for f in r.data["results"]]
            cursor = r.data["next_cursor"]
            if not cursor:
                break
        self.assertNotIn("secret.txt", seen)

    def test_file_symlink_escape_when_permitted(self):
        link = os.path.join(self.root, "flink.txt")
        try:
            os.symlink(os.path.join(self.outside, "secret.txt"), link)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks need Developer Mode/elevation here "
                          "(junction tests above cover directory escapes)")
        for argv in (("search", "needle"), ("search", "needle", "--files-only")):
            self.assertNotIn("TOPSECRET", self.agent(*argv).text)
        self.assertEqual(self.agent("search", "needle", "flink.txt").error["code"],
                         "path_outside_root")

    def test_paths_outside_roots_with_cursor_features(self):
        for argv in (("search", "needle", self.outside, "--count-only"),
                     ("search", "needle", self.outside, "--files-only"),
                     ("search", "needle", "..", "--page-size", "2"),
                     ("search", "needle", "../" + os.path.basename(self.outside))):
            r = self.agent(*argv)
            self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"), argv)
            self.assertNotIn("TOPSECRET", r.text)

    def test_cursor_cannot_widen_roots_or_paths(self):
        self.write("b.txt", "needle\n" * 3)
        c = self.agent("search", "needle", "--page-size", "1").data["next_cursor"]
        r = self.agent("search", "needle", self.outside, "--cursor", c)
        self.assertEqual(r.error["code"], "path_outside_root")


if __name__ == "__main__":
    unittest.main()
