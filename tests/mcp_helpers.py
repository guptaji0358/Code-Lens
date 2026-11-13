"""Shared helpers for the MCP tests.

Two ways of talking to a REAL server process (`python -m codelens_agent.mcp_server`):
  * `sdk_session()` - the official SDK client over stdio (protocol-correct client).
  * `RawServer`     - raw newline-delimited JSON-RPC over pipes, for checks an
                      SDK client would hide (stdout bytes, malformed input,
                      older protocol versions, shutdown).

MCP SDK tests are skipped (loudly, with the reason) when the optional SDK is
not installed; tests that need no SDK always run.
"""

import asyncio
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

from agent_helpers import AgentTestCase, SCRIPTS

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

requires_mcp = unittest.skipUnless(
    HAVE_MCP, "optional MCP SDK not installed (pip install -r requirements-mcp.txt)")


def server_env():
    return dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")


def server_argv(*roots, extra=()):
    argv = [sys.executable, "-m", "codelens_agent.mcp_server"]
    for r in roots:
        argv += ["--root", r]
    return argv + list(extra)


@contextlib.asynccontextmanager
async def sdk_session(*roots, extra=()):
    """Connected SDK Client for a server confined to `roots`."""
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codelens_agent.mcp_server", *[a for r in roots for a in ("--root", r)],
              *extra],
        env={"PYTHONPATH": SCRIPTS, "PYTHONIOENCODING": "utf-8"},
        cwd=roots[0] if roots else None)
    async with Client(params) as client:
        yield client


def run_async(coro_fn):
    """Runs `coro_fn()` to completion in a fresh event loop."""
    return asyncio.run(coro_fn())


class RawServer:
    """A server process driven with raw JSON-RPC lines."""

    def __init__(self, *roots, extra=(), cwd=None, argv=None):
        self.proc = subprocess.Popen(
            argv or server_argv(*roots, extra=extra), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=server_env(),
            cwd=cwd or (roots[0] if roots else SCRIPTS))
        self.out_lines = queue.Queue()
        self.all_out = []
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()
        self._id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.all_out.append(line)
            self.out_lines.put(line)

    def send_raw(self, text):
        self.proc.stdin.write(text.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.send_raw(json.dumps(msg))
        return None if notify else self._id

    def read(self, timeout=15):
        try:
            return json.loads(self.out_lines.get(timeout=timeout))
        except queue.Empty:
            return None

    def request(self, method, params=None, timeout=15):
        rid = self.send(method, params)
        while True:
            msg = self.read(timeout)
            if msg is None:
                raise AssertionError(f"no response to {method}")
            if msg.get("id") == rid:
                return msg

    def handshake(self, version="2025-06-18", capabilities=None):
        r = self.request("initialize", {
            "protocolVersion": version, "capabilities": capabilities or {},
            "clientInfo": {"name": "codelens-tests", "version": "0"}})
        self.send("notifications/initialized", notify=True)
        return r

    def call(self, name, arguments=None):
        r = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return r["result"]

    def close(self, timeout=15):
        """Closes stdin (the MCP stdio shutdown signal) and waits. -> exit code."""
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError("server did not exit after stdin closed")
        self._t.join(5)
        self.stderr = self.proc.stderr.read().decode("utf-8", errors="replace")
        return rc

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                pipe.close()


class McpTestCase(AgentTestCase):
    """AgentTestCase (temp root + file helpers) plus MCP conveniences."""

    def raw(self, *roots, **kw):
        srv = RawServer(*(roots or (self.root,)), **kw)
        self.addCleanup(srv.kill)
        return srv

    def with_client(self, fn, *roots, extra=()):
        """Runs `async fn(client)` against a fresh real server. -> its result."""
        async def go():
            async with sdk_session(*(roots or (self.root,)), extra=extra) as c:
                return await fn(c)
        return run_async(go)

    @staticmethod
    def env_of(result):
        """The Code-Lens envelope from a CallToolResult (structured content)."""
        return result.structured_content
