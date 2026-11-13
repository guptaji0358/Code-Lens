"""M5: stdout protocol integrity, malformed input, legacy protocol versions,
shutdown, and operation WITHOUT the MCP SDK. Raw JSON-RPC over real pipes."""

import json
import os
import subprocess
import sys
import time
import unittest

from agent_helpers import SCRIPTS, run_agent
from mcp_helpers import McpTestCase, RawServer, requires_mcp, server_env, server_argv


@requires_mcp
class StdioIntegrityTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "def foo():\n    return 1\n")

    def test_stdout_is_only_jsonrpc_and_diagnostics_go_to_stderr(self):
        srv = self.raw(extra=["--verbose"])
        srv.handshake()
        srv.call("search_code", {"query": "foo"})
        srv.call("read_file", {"file": "missing"})
        self.assertEqual(srv.close(), 0)
        self.assertTrue(srv.all_out)
        for line in srv.all_out:
            msg = json.loads(line)  # every stdout line is one JSON document
            self.assertEqual(msg["jsonrpc"], "2.0")
        self.assertIn("codelens-mcp", srv.stderr)

    def test_stray_stdout_write_cannot_corrupt_protocol(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import codelens_agent.ops as o\n"
                "orig = o.OPERATIONS['info']\n"
                "def noisy(s, a):\n"
                "    print('STRAY-STDOUT-NOISE'); sys.stdout.write('more\\n')\n"
                "    return orig(s, a)\n"
                "o.OPERATIONS['info'] = noisy\n"
                "from codelens_agent.mcp_server.__main__ import main\n"
                "sys.exit(main(['--root', %r]))\n" % (SCRIPTS, self.root))
        srv = self.raw(argv=[sys.executable, "-c", code])
        srv.handshake()
        r = srv.call("get_file_info", {"file": "a.py"})
        self.assertFalse(r["isError"])
        srv.close()
        for line in srv.all_out:
            self.assertNotIn(b"STRAY", line)
            json.loads(line)

    def test_malformed_input_is_survived(self):
        srv = self.raw()
        srv.handshake()
        for bad in ("{not json", "[]", "42", '"s"', "\x01\x02"):
            srv.send_raw(bad)
        r = srv.request("nope/method")
        self.assertEqual(r["error"]["code"], -32601)
        r = srv.request("tools/call", {})
        self.assertIn("error", r)
        r = srv.request("tools/call", {"name": "search_code", "arguments": "x"})
        self.assertIn("error", r)
        # still fully functional afterwards
        self.assertFalse(srv.call("search_code", {"query": "foo"})["isError"])
        self.assertEqual(srv.close(), 0)

    def test_legacy_protocol_versions_negotiate(self):
        for ver in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"):
            srv = self.raw()
            r = srv.handshake(version=ver)
            self.assertEqual(r["result"]["protocolVersion"], ver)
            self.assertIn("tools", r["result"]["capabilities"])
            self.assertEqual(r["result"]["serverInfo"]["name"], "codelens")
            self.assertFalse(srv.call("get_file_info", {"file": "a.py"})["isError"])
            self.assertEqual(srv.close(), 0)

    def test_unknown_protocol_version_gets_a_supported_one(self):
        srv = self.raw()
        r = srv.handshake(version="1999-01-01")
        self.assertRegex(r["result"]["protocolVersion"], r"^\d{4}-\d{2}-\d{2}$")
        srv.close()

    def test_clean_shutdown_on_stdin_close(self):
        srv = self.raw()
        srv.handshake()
        t = time.time()
        self.assertEqual(srv.close(), 0)
        self.assertLess(time.time() - t, 10)
        self.assertNotIn("Traceback", srv.stderr)

    def test_shutdown_without_any_request(self):
        srv = self.raw()
        self.assertEqual(srv.close(), 0)
        self.assertEqual(srv.all_out, [])

    def test_invalid_root_exits_2_with_empty_stdout(self):
        p = subprocess.run(server_argv(os.path.join(self.root, "nope")),
                           capture_output=True, env=server_env(), timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, b"")
        self.assertIn(b"invalid root", p.stderr)

    def test_unknown_flag_exits_2_stdout_empty(self):
        p = subprocess.run(server_argv(self.root, extra=["--bogus"]),
                           capture_output=True, env=server_env(), timeout=30)
        self.assertEqual((p.returncode, p.stdout), (2, b""))

    def test_help_goes_to_stderr(self):
        p = subprocess.run(server_argv(extra=["--help"]), capture_output=True,
                           env=server_env(), timeout=30)
        self.assertEqual(p.stdout, b"")
        self.assertIn(b"--root", p.stderr)

    def test_check_flag(self):
        p = subprocess.run(server_argv(self.root, extra=["--check"]),
                           capture_output=True, env=server_env(), timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, b""))
        self.assertIn(b"OK", p.stderr)

    def test_no_root_defaults_to_cwd_with_notice(self):
        srv = RawServer(cwd=self.root, argv=server_argv())
        self.addCleanup(srv.kill)
        srv.handshake()
        env = srv.call("get_capabilities")["structuredContent"]
        self.assertEqual([os.path.normcase(r) for r in env["data"]["roots"]],
                         [os.path.normcase(self.root)])
        srv.close()
        self.assertIn("no --root given", srv.stderr)

    def test_concurrent_requests(self):
        for i in range(5):
            self.write(f"c/f{i}.txt", "needle\n" * 20)
        srv = self.raw()
        srv.handshake()
        ids = [srv.send("tools/call", {"name": "search_code",
                                       "arguments": {"query": "needle"}})
               for _ in range(8)]
        got = {}
        while len(got) < len(ids):
            m = srv.read(30)
            self.assertIsNotNone(m)
            got[m["id"]] = m["result"]
        self.assertEqual(set(got), set(ids))
        self.assertTrue(all(not r["isError"] for r in got.values()))
        srv.close()


class WithoutSdkTests(McpTestCase):
    """Runs with an import hook that makes the MCP SDK unimportable, whether
    or not it is installed."""

    BLOCK = ("import sys\n"
             "class _B:\n"
             "    def find_spec(self, name, path=None, target=None):\n"
             "        if name.split('.')[0] in ('mcp', 'mcp_types'):\n"
             "            raise ImportError('blocked for test: ' + name)\n"
             "sys.meta_path.insert(0, _B())\n"
             "sys.path.insert(0, %r)\n" % SCRIPTS)

    def run_blocked(self, body, argv=()):
        code = self.BLOCK + body
        return subprocess.run([sys.executable, "-I", "-c", code, *argv], capture_output=True,
                              text=True, timeout=60, cwd=self.root,
                              env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})

    def test_cli_works_and_never_imports_mcp(self):
        self.write("a.py", "foo\n")
        body = ("import json, io\n"
                "from codelens_agent.cli import main\n"
                "rc = main(['--root', %r, 'search', 'foo'])\n"
                "assert 'mcp' not in sys.modules and 'mcp_types' not in sys.modules\n"
                "assert not any(m.startswith('codelens_agent.mcp_server') for m in sys.modules)\n"
                "sys.exit(rc)\n" % self.root)
        r = self.run_blocked(body)
        self.assertEqual(r.returncode, 0, r.stderr)
        env = json.loads(r.stdout)
        self.assertTrue(env["success"])
        self.assertEqual(env["data"]["total_matches"], 1)

    def test_cli_capabilities_unchanged_without_sdk(self):
        body = ("from codelens_agent.cli import main\n"
                "sys.exit(main(['capabilities']))\n")
        r = self.run_blocked(body)
        self.assertEqual(r.returncode, 0, r.stderr)
        got, want = json.loads(r.stdout)["data"], run_agent("capabilities").data
        got.pop("roots"), want.pop("roots")  # cwd differs between the two runs
        self.assertEqual(got, want)

    def test_mcp_server_without_sdk_explains_how_to_install(self):
        body = ("from codelens_agent.mcp_server.__main__ import main\n"
                "sys.exit(main(['--root', %r]))\n" % self.root)
        r = self.run_blocked(body)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout, "")
        self.assertIn("requirements-mcp.txt", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
