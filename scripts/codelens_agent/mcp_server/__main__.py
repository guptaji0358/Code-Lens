"""
python -m codelens_agent.mcp_server [--root DIR]... [--verbose] [--check]

Starts the optional Code-Lens MCP server on stdio. stdout is reserved for
MCP protocol messages; every diagnostic (including --help errors and
--check output) goes to stderr.
"""

import argparse
import os
import sys

if __package__ in (None, ""):  # pragma: no cover - run as a directory/file
    sys.exit("Run as: python -m codelens_agent.mcp_server")

from ..protocol import AgentError, diag
from ..sandbox import Sandbox

EXIT_OK, EXIT_SDK_MISSING, EXIT_USAGE = 0, 3, 2

SDK_HELP = ("The MCP SDK is not installed. Install the optional dependency with:\n"
            "    python -m pip install -r requirements-mcp.txt\n"
            "(from the Code-Lens checkout), or: python -m pip install \"mcp>=2.3,<3\"")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        diag("codelens-mcp: usage error: " + message)
        raise SystemExit(EXIT_USAGE)

    def _print_message(self, message, file=None):
        if message:
            sys.stderr.write(message)


def _parser():
    p = _Parser(prog="python -m codelens_agent.mcp_server", allow_abbrev=False,
                description="Code-Lens MCP server (stdio, read-only).")
    p.add_argument("--root", action="append", default=[], metavar="DIR",
                   help="permitted root directory; repeatable. Default: current directory.")
    p.add_argument("--verbose", action="store_true", help="diagnostics on stderr")
    p.add_argument("--check", action="store_true",
                   help="validate roots and the MCP SDK, report on stderr, and exit")
    return p


def main(argv=None):
    ns = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        roots = Sandbox(ns.root or None).roots  # validates; fail fast
    except AgentError as e:
        diag(f"codelens-mcp: invalid root: {e.message}")
        return EXIT_USAGE
    if not ns.root:
        diag("codelens-mcp: no --root given; the only permitted root is the current "
             f"directory: {roots[0]}")

    try:
        import mcp  # noqa: F401
        from . import server
    except ImportError as e:
        diag("codelens-mcp: " + SDK_HELP)
        if ns.verbose:
            diag(f"codelens-mcp: import error: {e}")
        return EXIT_SDK_MISSING

    if ns.check:
        from importlib import metadata
        try:
            ver = metadata.version("mcp")
        except metadata.PackageNotFoundError:
            ver = "unknown"
        diag(f"codelens-mcp: OK. mcp SDK {ver}; roots: {', '.join(roots)}")
        return EXIT_OK
    return server.run_stdio(ns.root or None, verbose=ns.verbose)


if __name__ == "__main__":
    sys.exit(main())
