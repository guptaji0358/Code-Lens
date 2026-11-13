"""
cli.py - command-line front-end.

    python -m codelens_agent [--root DIR]... [--verbose] <command> [args]
    python -m codelens_agent [--root DIR]... --request -     (JSON on stdin)

stdout carries exactly one JSON envelope (see protocol.py). Diagnostics go
to stderr. Exit codes are defined in protocol.py.
"""

import argparse
import json
import sys
import traceback

from . import ops
from .protocol import (AgentError, EXIT_INTERNAL, EXIT_OK, diag, emit,
                       error_envelope, success_envelope)
from .sandbox import Sandbox

MAX_REQUEST_BYTES = 1024 * 1024


class _Parser(argparse.ArgumentParser):
    """argparse that raises AgentError instead of printing usage to
    stdout/stderr and calling sys.exit(2)."""

    def error(self, message):
        raise AgentError("invalid_argument", message,
                         hint="Run `python -m codelens_agent capabilities` "
                              "for the command catalogue.")


_EPILOG = """global options (accepted anywhere on the command line):
  --root DIR       permitted root directory; repeatable. Default: current directory.
                   Relative paths resolve against the first root.
  --request SRC    run a JSON request from SRC ('-' = stdin) instead of a command:
                   {"operation":"read","args":{"file":"a.py","lines":["1-20"]}}
  --verbose        diagnostics on stderr

start with:  python -m codelens_agent capabilities
exit codes:  0 ok, 1 internal, 2 usage, 3 path, 4 limit, 5 stale cursor
(this --help text is the only non-JSON output the tool produces)
"""


def _build_parser():
    p = _Parser(prog="codelens_agent", allow_abbrev=False,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                description="Read-only, machine-readable CodeLens interface. Every "
                            "command prints exactly one JSON object on stdout.",
                epilog=_EPILOG)
    sub = p.add_subparsers(dest="command", parser_class=_Parser)
    sub.add_parser("capabilities", allow_abbrev=False,
                   help="Describe versions, commands, limits.")

    pi = sub.add_parser("info", allow_abbrev=False, help="File metadata.")
    pi.add_argument("file")

    pr = sub.add_parser("read", allow_abbrev=False, help="Read line ranges.")
    pr.add_argument("file")
    pr.add_argument("--lines", action="append", metavar="N|START-END")
    pr.add_argument("--around", type=int)
    pr.add_argument("--context", type=int)
    ps = sub.add_parser("search", allow_abbrev=False, help="Search text files.")
    ps.add_argument("query")
    ps.add_argument("paths", nargs="*")
    ps.add_argument("--mode", choices=["flexible", "literal", "word"])
    ps.add_argument("--multiline", action="store_true", default=None)
    ps.add_argument("--case-sensitive", dest="case_sensitive", action="store_true", default=None)
    ps.add_argument("--ext", action="append")
    ps.add_argument("--include", action="append")
    ps.add_argument("--exclude", action="append")
    ps.add_argument("--context", type=int)
    ps.add_argument("--max-results", dest="max_results", type=int)
    ps.add_argument("--page-size", dest="page_size", type=int)
    ps.add_argument("--cursor")
    ps.add_argument("--count-only", dest="count_only", action="store_true", default=None)
    ps.add_argument("--files-only", dest="files_only", action="store_true", default=None)
    ps.add_argument("--no-comment-fallback", dest="comment_fallback",
                    action="store_false", default=None)
    return p


def _extract_globals(argv):
    """Pulls --root/--verbose/--request out of argv wherever they appear."""
    roots, verbose, request, rest = [], False, None, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--verbose":
            verbose = True
        elif a in ("--root", "--request"):
            if i + 1 >= len(argv):
                raise AgentError("invalid_argument", f"{a} requires a value.")
            if a == "--root":
                roots.append(argv[i + 1])
            else:
                request = argv[i + 1]
            i += 1
        elif a.startswith("--root="):
            roots.append(a[len("--root="):])
        elif a.startswith("--request="):
            request = a[len("--request="):]
        else:
            rest.append(a)
        i += 1
    return roots, verbose, request, rest


def _read_request(source):
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        else:
            with open(source, "rb") as fh:
                raw = fh.read(MAX_REQUEST_BYTES + 1)
    except OSError as e:
        raise AgentError("invalid_request", f"Cannot read request: {e.strerror or e}")
    if len(raw) > MAX_REQUEST_BYTES:
        raise AgentError("limit_exceeded", "Request exceeds 1 MiB.")
    try:
        req = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise AgentError("invalid_request", f"Request is not valid UTF-8 JSON: {e}")
    if not isinstance(req, dict) or not isinstance(req.get("operation"), str):
        raise AgentError("invalid_request",
                         "Request must be an object with a string 'operation'.",
                         hint='Example: {"operation":"read","args":{"file":"a.py","lines":["1-20"]}}')
    args = req.get("args", {})
    if not isinstance(args, dict):
        raise AgentError("invalid_request", "'args' must be an object.")
    roots = req.get("roots")
    if roots is not None and (not isinstance(roots, list)
                              or not all(isinstance(r, str) for r in roots)):
        raise AgentError("invalid_request", "'roots' must be a list of strings.")
    return req["operation"], args, roots


def _args_from_flags(rest):
    if rest and not rest[0].startswith("-") and rest[0] not in ops.OPERATIONS:
        raise AgentError("unknown_command", f"Unknown command {rest[0]!r}.",
                         hint="Available: " + ", ".join(sorted(ops.OPERATIONS)))
    ns = _build_parser().parse_args(rest)
    if not ns.command:
        raise AgentError("invalid_argument", "A command is required.",
                         hint="Available: " + ", ".join(sorted(ops.OPERATIONS)))
    args = {k: v for k, v in vars(ns).items()
            if k != "command" and v is not None and v != []}
    return ns.command, args


def main(argv=None):
    # Capture the real stdout buffer before anything (colorama inside the
    # core, for example) can wrap or replace sys.stdout.
    out = sys.stdout.buffer
    argv = list(sys.argv[1:] if argv is None else argv)
    operation, verbose = None, False
    try:
        roots, verbose, request, rest = _extract_globals(argv)
        if request is not None:
            if rest:
                raise AgentError("invalid_argument",
                                 "--request cannot be combined with a command.")
            operation, args, req_roots = _read_request(request)
            if not roots and req_roots:
                roots = req_roots
        else:
            operation, args = _args_from_flags(rest)

        if operation not in ops.OPERATIONS:
            raise AgentError("unknown_command", f"Unknown operation {operation!r}.",
                             hint="Available: " + ", ".join(sorted(ops.OPERATIONS)))
        if verbose:
            diag(f"codelens-agent: operation={operation} roots={roots or '[cwd]'}")
        data, warnings = ops.OPERATIONS[operation](Sandbox(roots or None), args)
        emit(success_envelope(operation, data, warnings), out)
        return EXIT_OK

    except AgentError as e:
        emit(error_envelope(operation, [e.to_dict()]), out)
        if verbose:
            diag(f"codelens-agent: {e.code}: {e.message}")
        return e.exit_code
    except SystemExit as e:  # argparse -h
        return e.code if isinstance(e.code, int) else EXIT_OK
    except Exception as e:  # last resort: still machine-readable
        err = AgentError("internal_error",
                         f"Unexpected {type(e).__name__}.",
                         hint="Re-run with --verbose and inspect stderr.")
        emit(error_envelope(operation, [err.to_dict()]), out)
        diag("codelens-agent: internal error: " + (
            traceback.format_exc() if verbose else f"{type(e).__name__}: {e}"))
        return EXIT_INTERNAL
