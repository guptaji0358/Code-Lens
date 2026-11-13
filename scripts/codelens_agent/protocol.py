"""
protocol.py - the response envelope, error contract and exit codes.

Every command returns exactly one JSON object on stdout, shaped:

    {
      "schema_version": "1.0",
      "interface_version": "1",
      "tool": {"name": "codelens-agent", "version": "2.0.0"},
      "success": true,
      "operation": "read",
      "data": {...} | null,
      "warnings": [{"code", "message", "path"?}],
      "errors":   [{"code", "message", "path", "hint", "retryable"}]
    }

Nothing else is ever written to stdout in JSON mode.
"""

import json
import sys

from . import INTERFACE_VERSION, SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION

# ---- exit codes ----------------------------------------------------------
EXIT_OK = 0
EXIT_INTERNAL = 1       # unexpected failure inside codelens-agent
EXIT_USAGE = 2          # invalid arguments / request / unknown command
EXIT_PATH = 3           # path missing, outside roots, denied, not a text file
EXIT_LIMIT = 4          # a resource limit was exceeded
EXIT_STALE = 5          # (M3, additive) pagination cursor no longer valid

# ---- error codes -> exit codes ------------------------------------------
ERROR_EXIT_CODES = {
    "invalid_argument": EXIT_USAGE,
    "invalid_request": EXIT_USAGE,
    "unknown_command": EXIT_USAGE,
    "path_not_found": EXIT_PATH,
    "path_outside_root": EXIT_PATH,
    "permission_denied": EXIT_PATH,
    "not_a_file": EXIT_PATH,
    "not_text_file": EXIT_PATH,
    "file_too_large": EXIT_LIMIT,
    "limit_exceeded": EXIT_LIMIT,
    "invalid_cursor": EXIT_USAGE,   # M3: malformed / foreign cursor
    "stale_cursor": EXIT_STALE,     # M3: files changed since the first page
    "internal_error": EXIT_INTERNAL,
}


class AgentError(Exception):
    """A failure with a stable machine-readable code."""

    def __init__(self, code, message, path=None, hint=None, retryable=False):
        super().__init__(message)
        if code not in ERROR_EXIT_CODES:
            raise ValueError(f"unregistered error code: {code}")
        self.code = code
        self.message = message
        self.path = path
        self.hint = hint
        self.retryable = retryable

    @property
    def exit_code(self):
        return ERROR_EXIT_CODES[self.code]

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "hint": self.hint,
            "retryable": self.retryable,
        }


def warning(code, message, path=None):
    return {"code": code, "message": message, "path": path}


def _envelope(operation, success, data, warnings, errors):
    return {
        "schema_version": SCHEMA_VERSION,
        "interface_version": INTERFACE_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "success": success,
        "operation": operation,
        "data": data,
        "warnings": list(warnings or []),
        "errors": list(errors or []),
    }


def success_envelope(operation, data, warnings=None):
    return _envelope(operation, True, data, warnings, [])


def error_envelope(operation, errors, warnings=None):
    return _envelope(operation, False, None, warnings, errors)


def emit(envelope, stream):
    """Writes the envelope as one UTF-8 JSON line to the raw binary
    `stream` (the process's real stdout buffer, captured before anything
    could wrap or replace sys.stdout)."""
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    stream.write(text.encode("utf-8", errors="replace") + b"\n")
    stream.flush()


def diag(message):
    """Diagnostics go to stderr only - never stdout."""
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass
