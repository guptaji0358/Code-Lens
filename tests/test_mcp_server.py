"""M5: the MCP server driven by the official SDK client over real stdio."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from agent_helpers import SCRIPTS
from mcp_helpers import McpTestCase, requires_mcp

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from codelens_agent.mcp_server import tools  # noqa: E402


@requires_mcp
class ServerTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "def foo():\n    return 1\n# foo again\n")
        self.write("sub/b.txt", "nothing\nfoo\n")

    def test_startup_negotiation_and_discovery(self):
        async def fn(c):
            self.assertTrue(c.protocol_version)
            self.assertEqual(c.server_info.name, "codelens")
            lt = await c.list_tools()
            return {t.name: t for t in lt.tools}
        got = self.with_client(fn)
        self.assertEqual(set(got), set(tools.TOOL_NAMES))
        for name, t in got.items():
            self.assertTrue(t.description)
            self.assertTrue(t.annotations.read_only_hint)
            self.assertFalse(t.annotations.destructive_hint)
            self.assertEqual(t.input_schema["additionalProperties"], False)
            self.assertIsNotNone(t.output_schema)

    def test_every_tool_structured_and_text(self):
        calls = [("get_capabilities", {}), ("search_code", {"query": "foo"}),
                 ("read_file", {"file": "a.py", "lines": ["1-2"]}),
                 ("get_file_info", {"file": "a.py"})]

        async def fn(c):
            return [await c.call_tool(n, a) for n, a in calls]
        for (name, _), r in zip(calls, self.with_client(fn)):
            self.assertFalse(r.is_error, name)
            env = self.env_of(r)
            self.assertTrue(env["success"])
            self.assertEqual(r.content[0].text, tools.envelope_text(env))

    def test_zero_matches_not_error(self):
        async def fn(c):
            return await c.call_tool("search_code", {"query": "zzzqqq"})
        r = self.with_client(fn)
        self.assertFalse(r.is_error)
        self.assertEqual(self.env_of(r)["data"]["total_matches"], 0)

    def test_errors_are_tool_errors_with_envelope(self):
        async def fn(c):
            return [await c.call_tool("read_file", {"file": "nope.py"}),
                    await c.call_tool("search_code", {"query": "x", "bogus": 1}),
                    await c.call_tool("search_code", {"query": "x", "page_size": 99999}),
                    await c.call_tool("nope", {})]
        codes = ["path_not_found", "invalid_argument", "invalid_argument", "unknown_command"]
        for r, code in zip(self.with_client(fn), codes):
            self.assertTrue(r.is_error)
            self.assertEqual(self.env_of(r)["errors"][0]["code"], code)

    def test_pagination_partial_and_cursor_errors(self):
        for i in range(4):
            self.write(f"p/f{i}.txt", "needle\nneedle\nneedle\n")

        async def fn(c):
            args = {"query": "needle", "page_size": 5}
            pages, cur = [], None
            while True:
                a = dict(args, **({"cursor": cur} if cur else {}))
                r = await c.call_tool("search_code", a)
                self.assertFalse(r.is_error)
                pages.append(self.env_of(r)["data"])
                cur = pages[-1]["next_cursor"]
                if not cur:
                    break
            bad = await c.call_tool("search_code", dict(args, cursor="!!!"))
            return pages, bad
        pages, bad = self.with_client(fn)
        self.assertEqual(pages[0]["more_results"], "confirmed")
        self.assertFalse(pages[0]["complete"])
        self.assertEqual(sum(p["total_matches"] for p in pages), 12)
        self.assertEqual(pages[-1]["more_results"], "none")
        self.assertEqual(self.env_of(bad)["errors"][0]["code"], "invalid_cursor")

    def test_stale_cursor(self):
        for i in range(3):
            self.write(f"p/f{i}.txt", "needle\nneedle\n")

        async def fn(c):
            args = {"query": "needle", "page_size": 2}
            first = self.env_of(await c.call_tool("search_code", args))["data"]
            self.write("p/new.txt", "needle\n")
            return await c.call_tool("search_code", dict(args, cursor=first["next_cursor"]))
        r = self.with_client(fn)
        self.assertTrue(r.is_error)
        e = self.env_of(r)["errors"][0]
        self.assertEqual(e["code"], "stale_cursor")

    def test_sandbox_escapes(self):
        outside = tempfile.mkdtemp(prefix="cl_mcp_out_")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "s.txt"), "w") as fh:
            fh.write("TOPSECRET needle\n")

        async def fn(c):
            out = []
            for p in ("../" + os.path.basename(outside) + "/s.txt",
                      os.path.join(outside, "s.txt"), "..\\..\\x"):
                out.append(await c.call_tool("read_file", {"file": p}))
                out.append(await c.call_tool("get_file_info", {"file": p}))
                out.append(await c.call_tool("search_code", {"query": "needle", "paths": [p]}))
            return out
        for r in self.with_client(fn):
            self.assertTrue(r.is_error)
            self.assertIn(self.env_of(r)["errors"][0]["code"], ("path_outside_root",))
            self.assertNotIn("TOPSECRET", r.content[0].text)

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_not_followed(self):
        outside = tempfile.mkdtemp(prefix="cl_mcp_out_")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "s.txt"), "w") as fh:
            fh.write("TOPSECRET needle\n")
        link = os.path.join(self.root, "jn")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
        if r.returncode != 0:
            self.skipTest("cannot create junction")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))

        async def fn(c):
            return [await c.call_tool("search_code", {"query": "needle"}),
                    await c.call_tool("read_file", {"file": "jn/s.txt"})]
        s, rd = self.with_client(fn)
        self.assertNotIn("TOPSECRET", s.content[0].text)
        self.assertTrue(rd.is_error)
        self.assertEqual(self.env_of(rd)["errors"][0]["code"], "path_outside_root")

    def test_resource_limits(self):
        self.write("big.txt", "x" * (5 * 1024 * 1024 + 10))

        async def fn(c):
            return [await c.call_tool("read_file", {"file": "big.txt"}),
                    await c.call_tool("read_file", {"file": "a.py",
                                                    "lines": ["1"] * 51}),
                    await c.call_tool("search_code", {"query": "x" * 10001}),
                    await c.call_tool("search_code", {"query": "x", "context": 21})]
        codes = ["file_too_large", "limit_exceeded", "limit_exceeded", "invalid_argument"]
        for r, code in zip(self.with_client(fn), codes):
            self.assertTrue(r.is_error)
            self.assertEqual(self.env_of(r)["errors"][0]["code"], code)

    def test_client_roots_are_ignored(self):
        srv = self.raw()
        srv.handshake(capabilities={"roots": {"listChanged": True}})
        r = srv.call("get_file_info", {"file": os.path.join(tempfile.gettempdir(), "x")})
        self.assertTrue(r["isError"])
        srv.close()
        # the server never asked the client for its roots
        for line in srv.all_out:
            self.assertNotIn(b"roots/list", line)


if __name__ == "__main__":
    unittest.main()
