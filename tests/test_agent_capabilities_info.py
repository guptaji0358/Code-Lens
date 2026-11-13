import os
import subprocess
import sys
import unittest

from agent_helpers import AgentTestCase, run_agent, SCRIPTS


class CapabilitiesTests(AgentTestCase):
    def test_capabilities_catalogue(self):
        r = self.agent("capabilities")
        self.assert_envelope(r, True, "capabilities")
        d = r.data
        self.assertEqual(d["interface_version"], "1")
        self.assertEqual(d["schema_version"], "1.0")
        self.assertTrue(d["read_only"])
        self.assertTrue({"capabilities", "info", "read"} <= set(d["commands"]))  # M2: additive (search)
        self.assertEqual(d["roots"], [self.root])
        self.assertIn("path_outside_root", d["error_codes"])
        self.assertNotIn("search", d["planned_commands"])  # M2: implemented
        self.assertEqual(d["deprecations"], [])
        for spec in d["commands"].values():
            for a in spec["args"]:
                self.assertEqual(set(a), {"name", "type", "required", "repeatable",
                                          "default", "description"})

    def test_default_root_is_cwd(self):
        r = run_agent("capabilities", cwd=self.root)
        self.assertEqual(r.data["roots"], [self.root])

    def test_unknown_argument_rejected(self):
        r = self.agent("capabilities", "--bogus")
        self.assert_envelope(r, False, None)
        self.assertEqual(r.code, 2)
        self.assertEqual(r.error["code"], "invalid_argument")

    def test_unknown_command(self):
        r = self.agent("frobnicate")
        self.assert_envelope(r, False, None)
        self.assertEqual((r.code, r.error["code"]), (2, "unknown_command"))

    def test_no_command(self):
        r = self.agent()
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_nonexistent_root(self):
        r = run_agent("--root", os.path.join(self.root, "nope"), "capabilities")
        self.assertEqual((r.code, r.error["code"]), (3, "path_not_found"))

    def test_verbose_goes_to_stderr_only(self):
        r = self.agent("capabilities", "--verbose")
        self.assert_envelope(r, True, "capabilities")
        self.assertIn("operation=capabilities", r.err)

    def test_stdout_has_no_ansi_or_banner(self):
        r = self.agent("capabilities")
        self.assertNotIn(b"\x1b", r.raw_out)
        self.assertNotIn(b"CODELENS", r.raw_out)


class InfoTests(AgentTestCase):
    def test_info_text_file(self):
        self.write("a.py", "x = 1\r\ny = 2\r\n")
        r = self.agent("info", "a.py")
        self.assert_envelope(r, True, "info")
        d = r.data
        self.assertEqual((d["file"], d["kind"], d["is_text"]), ("a.py", "file", True))
        self.assertEqual((d["line_count"], d["line_ending"], d["ends_with_newline"]),
                         (2, "crlf", True))
        self.assertEqual(d["size_bytes"], 14)
        self.assertTrue(d["modified"].endswith("Z"))

    def test_info_empty_file(self):
        self.write("e.txt", "")
        d = self.agent("info", "e.txt").data
        self.assertEqual((d["line_count"], d["line_ending"]), (0, "none"))

    def test_info_mixed_endings_and_no_trailing_newline(self):
        self.write("m.txt", "a\nb\r\nc")
        d = self.agent("info", "m.txt").data
        self.assertEqual((d["line_count"], d["line_ending"], d["ends_with_newline"]),
                         (3, "mixed", False))

    def test_info_binary_file(self):
        self.write("b.bin", b"\x00\x01\x02" * 100, mode="wb")
        r = self.agent("info", "b.bin")
        self.assert_envelope(r, True, "info")
        self.assertFalse(r.data["is_text"])
        self.assertFalse(r.data["content_inspected"])
        self.assertIsNone(r.data["line_count"])

    def test_info_utf16_bom(self):
        self.write("u.txt", "héllo\nwörld\n".encode("utf-16"), mode="wb")
        d = self.agent("info", "u.txt").data
        self.assertEqual(d["line_count"], 2)
        self.assertTrue(d["encoding"].startswith("utf-16"))

    def test_info_large_file_not_inspected(self):
        self.write("big.txt", b"a" * (5 * 1024 * 1024 + 10), mode="wb")
        r = self.agent("info", "big.txt")
        self.assert_envelope(r, True, "info")
        self.assertFalse(r.data["content_inspected"])
        self.assertEqual(r.json["warnings"][0]["code"], "content_not_inspected")

    def test_info_missing_file(self):
        r = self.agent("info", "nope.py")
        self.assert_envelope(r, False, "info")
        self.assertEqual((r.code, r.error["code"]), (3, "path_not_found"))

    def test_info_directory(self):
        os.makedirs(os.path.join(self.root, "d"))
        r = self.agent("info", "d")
        self.assertEqual((r.code, r.error["code"]), (3, "not_a_file"))

    def test_info_missing_arg(self):
        r = self.agent("info")
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    def test_info_reflects_modification(self):
        p = self.write("f.txt", "one\n")
        self.assertEqual(self.agent("info", "f.txt").data["line_count"], 1)
        with open(p, "w", newline="") as fh:
            fh.write("one\ntwo\nthree\n")
        self.assertEqual(self.agent("info", "f.txt").data["line_count"], 3)


class SandboxTests(AgentTestCase):
    def test_traversal_rejected(self):
        sub = os.path.join(self.root, "proj")
        os.makedirs(sub)
        self.write("secret.txt", "s\n")
        r = run_agent("--root", sub, "info", "../secret.txt", cwd=sub)
        self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))

    def test_absolute_outside_rejected(self):
        outside = os.path.abspath(sys.executable)
        r = self.agent("info", outside)
        self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))

    def test_absolute_inside_allowed(self):
        p = self.write("a.txt", "x\n")
        r = self.agent("info", p)
        self.assertTrue(r.json["success"])

    def test_multiple_roots(self):
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        p = self.write("other/o.txt", "x\n")
        r = run_agent("--root", other, "info", p, cwd=other)
        self.assertTrue(r.json["success"])
        r = run_agent("--root", other, "--root", self.root, "info", "o.txt", cwd=self.root)
        self.assertTrue(r.json["success"])
        self.assertEqual(r.data["file"], "o.txt")
        self.assertTrue(r.json["success"])

    def test_symlink_escape_rejected(self):
        outside = os.path.join(os.path.dirname(self.root), "outside_" + os.path.basename(self.root))
        os.makedirs(outside)
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        with open(os.path.join(outside, "s.txt"), "w") as fh:
            fh.write("secret\n")
        link = os.path.join(self.root, "link.txt")
        try:
            os.symlink(os.path.join(outside, "s.txt"), link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this system")
        r = self.agent("info", "link.txt")
        self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))

    def test_nul_byte_path(self):
        r = self.agent("--request", "-", stdin=b'{"operation":"info","args":{"file":"a\\u0000b"}}')
        self.assertEqual((r.code, r.error["code"]), (2, "invalid_argument"))

    @unittest.skipUnless(os.name == "nt", "Windows-only")
    def test_other_drive_rejected(self):
        r = self.agent("info", "Z:\\definitely\\not\\here.txt")
        self.assertEqual((r.code, r.error["code"]), (3, "path_outside_root"))

    def test_permission_denied(self):
        p = self.write("locked.txt", "x\n")
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            rc = subprocess.run(["icacls", p, "/deny", f"{user}:(R)"],
                                capture_output=True).returncode
            if rc != 0:
                self.skipTest("cannot set ACL")
            self.addCleanup(lambda: subprocess.run(["icacls", p, "/remove:d", user],
                                                   capture_output=True))
        else:
            os.chmod(p, 0)
            self.addCleanup(lambda: os.chmod(p, 0o600))
            if os.access(p, os.R_OK):
                self.skipTest("running as a user that bypasses permissions")
        r = self.agent("info", "locked.txt")
        self.assert_envelope(r, False, "info")
        self.assertEqual((r.code, r.error["code"]), (3, "permission_denied"))


if __name__ == "__main__":
    unittest.main()
