"""
tools.py - the MCP tool catalogue and call handling. No MCP SDK import.

Every tool is a thin adapter over an existing agent operation
(`codelens_agent.ops.OPERATIONS`). Nothing here searches, reads files,
resolves paths or enforces limits: that is all done by the same Sandbox/ops
code the CLI uses, called with a Sandbox built from the server's own
--root list (never from anything the MCP client sends).

A tool result is always the existing v1 response envelope
(protocol.success_envelope / error_envelope), unchanged. A successful call
with zero matches is `success: true` (isError false); only an envelope with
`success: false` is reported as an MCP tool execution error.
"""

import json
import traceback

from .. import INTERFACE_VERSION, SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION
from .. import ops
from .. import sandbox as sb
from ..protocol import (AgentError, diag, error_envelope, success_envelope)
from ..sandbox import Sandbox
from . import MCP_TOOLS_VERSION

# ---- JSON Schemas -------------------------------------------------------------

_STR = {"type": "string"}


def _bool(desc):
    return {"type": "boolean", "description": desc}


def _int(desc, lo, hi):
    return {"type": "integer", "minimum": lo, "maximum": hi, "description": desc}


def _str_list(desc, max_items=None):
    s = {"type": "array", "items": _STR, "description": desc}
    if max_items:
        s["maxItems"] = max_items
    return s


def _object(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


# Loose on purpose: it pins the envelope shape clients can rely on without
# duplicating docs/schema/codelens-agent-v1.schema.json (the full contract).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": _STR,
        "interface_version": _STR,
        "tool": {"type": "object"},
        "success": {"type": "boolean"},
        "operation": {"type": ["string", "null"]},
        "data": {"type": ["object", "null"]},
        "warnings": {"type": "array", "items": {"type": "object"}},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["schema_version", "interface_version", "tool", "success",
                 "operation", "data", "warnings", "errors"],
}

_FILE_DESC = ("File path: relative to the first permitted root, or absolute inside "
              "a permitted root. Paths outside the roots are refused.")

SEARCH_INPUT = _object({
    "query": {"type": "string", "minLength": 1, "maxLength": sb.MAX_QUERY_CHARS,
              "description": "Text to find (not a regular expression)."},
    "paths": _str_list("Files or directories to search (recursive). Default: the first "
                       "permitted root.", sb.MAX_SEARCH_PATHS),
    "mode": {"type": "string", "enum": ["flexible", "literal", "word"],
             "description": "flexible (default: whitespace-insensitive), literal (exact "
                            "text) or word (exact text on word boundaries; single token)."},
    "multiline": _bool("Match across line breaks. Required if the query contains a newline."),
    "case_sensitive": _bool("Case-sensitive matching (default false)."),
    "extensions": _str_list("Only files with these extensions, e.g. ['py', '.js']."),
    "include": _str_list("Glob(s) the relative path or name must match."),
    "exclude": _str_list("Glob(s) excluding files or directories."),
    "context": _int("Context lines before/after each match.", 0, sb.MAX_CONTEXT_LINES),
    "max_results": _int("Maximum matches returned. Give either this or page_size.",
                        1, sb.MAX_SEARCH_RESULTS),
    "page_size": _int("Matches (or files, with files_only) per page. Alternative to "
                      "max_results.", 1, sb.MAX_SEARCH_RESULTS),
    "cursor": {"type": "string", "maxLength": 512,
               "description": "data.next_cursor from a previous page of the SAME search "
                              "(repeat every other argument unchanged). Opaque."},
    "count_only": _bool("Return only counts, no snippets. Not paginated."),
    "files_only": _bool("Return matching files with per-file match counts only."),
    "comment_fallback": _bool("Flexible multiline only: allow comment-stripping fallbacks "
                              "(always reported per match). Default true."),
}, required=["query"])

READ_INPUT = _object({
    "file": {"type": "string", "minLength": 1, "description": _FILE_DESC},
    "lines": _str_list("Line ranges 'N' or 'START-END' (1-based, inclusive). Mutually "
                       "exclusive with 'around'. Omit both to read the whole file "
                       f"(output is capped at {sb.MAX_READ_LINES} lines).",
                       sb.MAX_READ_RANGES),
    "around": {"type": "integer", "minimum": 1,
               "description": "Return lines around this line number."},
    "context": _int("Lines before/after 'around' (default 5). Requires 'around'.", 0, 200),
}, required=["file"])

INFO_INPUT = _object({"file": {"type": "string", "minLength": 1, "description": _FILE_DESC}},
                     required=["file"])

NO_INPUT = _object({})

# ---- catalogue ------------------------------------------------------------------
# name -> definition. `operation` is the key in ops.OPERATIONS; `rename` maps
# MCP argument names to the agent operation's argument names.

TOOLS = {
    "get_capabilities": {
        "operation": "capabilities",
        "description": "Describe the Code-Lens interface: versions, operations, search "
                       "modes, resource limits, pagination, permitted roots and known "
                       "limitations. Call this first.",
        "input": NO_INPUT, "rename": {},
    },
    "search_code": {
        "operation": "search",
        "description": "Search text files in the permitted roots with the Code-Lens engine. "
                       "Returns file, line/column spans, the match strategy used, and "
                       "completeness/pagination fields (more_results, next_cursor, "
                       "truncated). Zero matches is a successful result. To get the next "
                       "page, call search_code again with the same arguments plus "
                       "cursor=data.next_cursor.",
        "input": SEARCH_INPUT, "rename": {"extensions": "ext"},
    },
    "read_file": {
        "operation": "read",
        "description": "Read exact 1-based line ranges (or lines around a line number) "
                       "from a text file inside the permitted roots. Read-only.",
        "input": READ_INPUT, "rename": {},
    },
    "get_file_info": {
        "operation": "info",
        "description": "File metadata: size, modified time, text/binary, encoding, line "
                       "count, line endings.",
        "input": INFO_INPUT, "rename": {},
    },
}

TOOL_NAMES = tuple(TOOLS)


def tool_list():
    """Plain-dict tool descriptors (name, description, inputSchema, ...)."""
    return [{"name": n, "description": t["description"],
             "input_schema": t["input"], "output_schema": OUTPUT_SCHEMA}
            for n, t in TOOLS.items()]


# ---- calling --------------------------------------------------------------------

def _translate(name, arguments):
    """Validates the argument *names* against the MCP schema, drops nulls, and
    renames MCP names to the agent operation's names. Value validation is
    left entirely to ops (one validator, same error codes as the CLI)."""
    tool = TOOLS[name]
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise AgentError("invalid_argument", "Tool arguments must be a JSON object.")
    allowed = set(tool["input"]["properties"])
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise AgentError("invalid_argument",
                         f"Unknown argument(s): {', '.join(map(str, extra))}",
                         hint=f"Allowed: {', '.join(sorted(allowed)) or '(none)'}")
    out = {}
    for key, value in arguments.items():
        if value is None:
            continue
        out[tool["rename"].get(key, key)] = value
    return out


def _mcp_capabilities(data, roots):
    """Adds the MCP-specific facts to the unchanged capabilities payload."""
    data = dict(data)
    # The CLI's list says "mcp_server" is unsupported (true for the CLI
    # process itself); over MCP that entry would be wrong.
    data["unsupported"] = [u for u in data["unsupported"] if u != "mcp_server"]
    data["mcp"] = {
        "tools_version": MCP_TOOLS_VERSION,
        "transport": "stdio",
        "tools": [{"name": n, "operation": t["operation"],
                   "arguments": sorted(t["input"]["properties"]),
                   "required": t["input"]["required"]}
                  for n, t in TOOLS.items()],
        "pagination": {
            "next_page_tool": None,
            "how": "Call search_code again with identical arguments plus "
                   "cursor=data.next_cursor.",
        },
        "roots_source": "server command line (--root); client-provided roots are ignored",
        "argument_renames": {"search_code.extensions": "ext (CLI flag --ext)"},
        "result": "content[0].text is the JSON envelope; structuredContent is the same "
                  "envelope; isError is true only when success is false.",
        "limitations": [
            "stdio transport only (no HTTP, no authentication).",
            "Cancelling a request does not interrupt a running search; it ends within "
            "limits.search_time_budget_seconds.",
            "Searches run in worker threads; at most %d run concurrently." % MAX_CONCURRENT,
        ],
    }
    return data


# Concurrent operations the server runs at once (see server.py).
MAX_CONCURRENT = 4


def call_tool(name, arguments, roots):
    """Runs one tool call. Returns (envelope_dict, is_error). Never raises.

    `roots`: list of root directories from the server's own configuration
    (None/empty -> current directory, same as the CLI). A fresh Sandbox is
    built per call, exactly like one CLI invocation, so root symlink/junction
    changes are never cached.
    """
    operation = None
    try:
        if name not in TOOLS:
            raise AgentError("unknown_command", f"Unknown tool {name!r}.",
                             hint="Available: " + ", ".join(TOOL_NAMES))
        operation = TOOLS[name]["operation"]
        args = _translate(name, arguments)
        sandbox = Sandbox(roots or None)
        data, warnings = ops.OPERATIONS[operation](sandbox, args)
        if name == "get_capabilities":
            data = _mcp_capabilities(data, sandbox.roots)
        return success_envelope(operation, data, warnings), False
    except AgentError as e:
        return error_envelope(operation, [e.to_dict()]), True
    except Exception as e:  # last resort: still a well-formed envelope
        err = AgentError("internal_error", f"Unexpected {type(e).__name__}.",
                         hint="Run the server with --verbose and inspect stderr.")
        diag("codelens-mcp: internal error in tool %s: %s" % (
            name, traceback.format_exc().strip()))
        return error_envelope(operation, [err.to_dict()]), True


def envelope_text(envelope):
    """Text representation: the envelope as compact JSON, identical to what the
    CLI prints, so nothing (warnings, cursors, truncation, strategy) is lost
    for clients that only forward `content` to the model."""
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


SERVER_INSTRUCTIONS = (
    "Read-only code search over explicitly permitted directories. Start with "
    "get_capabilities. search_code finds text (not regex) and returns positions; "
    "read_file returns exact line ranges; get_file_info gives metadata. Every result "
    "is a Code-Lens envelope: check data.more_results / data.next_cursor / warnings "
    "before treating results as complete. Zero matches is success, not an error."
)

__all__ = ["TOOLS", "TOOL_NAMES", "OUTPUT_SCHEMA", "tool_list", "call_tool",
           "envelope_text", "SERVER_INSTRUCTIONS", "MAX_CONCURRENT",
           "INTERFACE_VERSION", "SCHEMA_VERSION", "TOOL_NAME", "TOOL_VERSION"]
