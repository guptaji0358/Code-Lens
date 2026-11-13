"""
server.py - MCP SDK glue (the only module that imports the MCP SDK).

Uses the SDK's low-level Server on purpose: tool arguments must reach the
existing ops validation untouched, so every failure keeps the Code-Lens
error envelope. (The high-level decorator API would validate and coerce
arguments with pydantic first and answer with SDK-formatted errors.) No
`get_tool_input_schema` is registered, so the SDK performs no argument
validation of its own.

Transport: stdio only. The SDK owns stdout for the protocol (it moves fd 1
aside so stray writes cannot corrupt the stream); all diagnostics go to
stderr. Roots come only from the server's own configuration.
"""

import functools
import io
import sys
from contextlib import suppress

import anyio

from .. import TOOL_VERSION
from ..protocol import diag
from . import tools

SERVER_NAME = "codelens"

class _UnownedTextWrapper(io.TextIOWrapper):
    """UTF-8 text layer over the real stdout buffer whose close() never
    closes that buffer. newline="\\n": no CRLF translation on Windows."""

    def close(self):
        with suppress(ValueError):
            self.detach()


def build_server(roots=None):
    """Returns a configured low-level MCP Server. Raises ImportError when
    the optional MCP SDK is not installed."""
    from mcp.server import Server
    from mcp.types import (CallToolResult, ListToolsResult, TextContent, Tool,
                           ToolAnnotations)

    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)
    tool_objs = [
        Tool(name=t["name"], description=t["description"],
             input_schema=t["input_schema"], output_schema=t["output_schema"],
             annotations=annotations)
        for t in tools.tool_list()
    ]
    # Bounds concurrent worker threads; each op is itself bounded by the
    # sandbox limits (file/byte/time budgets).
    limiter = anyio.CapacityLimiter(tools.MAX_CONCURRENT)
    roots = list(roots) if roots else None

    async def on_list_tools(ctx, params):
        return ListToolsResult(tools=tool_objs)

    async def on_call_tool(ctx, params):
        # Synchronous, blocking disk work: run off the event loop so the
        # server keeps answering pings / other requests.
        envelope, is_error = await anyio.to_thread.run_sync(
            functools.partial(tools.call_tool, params.name, params.arguments, roots),
            limiter=limiter)
        return CallToolResult(
            content=[TextContent(type="text", text=tools.envelope_text(envelope))],
            structured_content=envelope,
            is_error=is_error)

    return Server(SERVER_NAME, version=TOOL_VERSION,
                  title="Code-Lens (read-only code search)",
                  instructions=tools.SERVER_INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_stdio(roots=None):
    from mcp.server.stdio import stdio_server

    server = build_server(roots)
    # Belt and braces for stdout integrity: the protocol gets the real stdout
    # buffer explicitly, and sys.stdout is pointed at stderr so any stray
    # print()/sys.stdout.write() in this process lands on stderr instead of
    # corrupting the protocol stream. (The SDK's own fd diversion is
    # best-effort and was observed not to cover this case on Windows.)
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr
    out = anyio.wrap_file(_UnownedTextWrapper(protocol_out, encoding="utf-8",
                                              newline="\n"))
    async with stdio_server(stdout=out) as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def run_stdio(roots=None, verbose=False):
    """Blocks until the client closes stdin. Returns an exit code."""
    # Import the CodeLens core now, on the main thread, with stdout/stderr
    # isolated by engine.core(), before the protocol owns the streams.
    from .. import engine
    engine.core()
    if verbose:
        diag("codelens-mcp: serving on stdio; roots=%s" % (roots or ["<cwd>"]))
    try:
        anyio.run(serve_stdio, roots)
    except KeyboardInterrupt:
        pass
    if verbose:
        diag("codelens-mcp: stopped")
    return 0
