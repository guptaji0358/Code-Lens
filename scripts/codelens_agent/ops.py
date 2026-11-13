"""
ops.py - the operations. Each takes (sandbox, args: dict) and returns
(data: dict, warnings: list). All validation lives here so the argparse
front-end and the JSON-request front-end share one code path.
All operations are read-only.
"""

import datetime
import re

from . import INTERFACE_VERSION, SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION
from . import engine
from . import sandbox as sb
from . import search as search_mod
from .protocol import AgentError, ERROR_EXIT_CODES, warning

# ---- argument validation helpers --------------------------------------------

def _unknown_args(args, allowed):
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise AgentError("invalid_argument",
                         f"Unknown argument(s): {', '.join(extra)}",
                         hint=f"Allowed: {', '.join(sorted(allowed)) or '(none)'}")


def _req_str(args, name):
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise AgentError("invalid_argument", f"'{name}' is required (non-empty string).")
    return v


def _opt_int(args, name, lo, hi):
    v = args.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise AgentError("invalid_argument", f"'{name}' must be an integer.")
    if not lo <= v <= hi:
        raise AgentError("invalid_argument", f"'{name}' must be between {lo} and {hi}.")
    return v


def _opt_str_list(args, name):
    v = args.get(name)
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in v):
        raise AgentError("invalid_argument", f"'{name}' must be a list of strings.")
    return [str(x) for x in v]


# ---- command catalogue (served by `capabilities`) ---------------------------

def _arg(name, type_, required=False, repeatable=False, default=None, description=""):
    return {"name": name, "type": type_, "required": required,
            "repeatable": repeatable, "default": default, "description": description}


COMMANDS = {
    "capabilities": {
        "summary": "Describe this interface: versions, commands, limits, error and exit codes.",
        "args": [],
    },
    "info": {
        "summary": "File metadata: size, modified time, text/binary, encoding, line count.",
        "args": [_arg("file", "string", required=True,
                      description="File path (relative to the first root, or absolute inside a root).")],
    },
    "read": {
        "summary": "Read exact line ranges from a text file, with 1-based line numbers.",
        "args": [
            _arg("file", "string", required=True, description="File path."),
            _arg("lines", "string", repeatable=True,
                 description="Range 'N' or 'START-END' (1-based, inclusive). Repeat for non-contiguous ranges."),
            _arg("around", "integer",
                 description="Return context around this line number. Mutually exclusive with 'lines'."),
            _arg("context", "integer", default=5,
                 description="Lines before/after 'around' (0-200)."),
        ],
    },
}

COMMANDS["search"] = {
    "summary": "Search text files (files and/or directories, recursive) using the CodeLens "
               "engine. Every match reports file, line/column span and the strategy used.",
    "args": [
        _arg("query", "string", required=True, description="Text to find."),
        _arg("paths", "string", repeatable=True, default="<first root>",
             description="Files or directories (recursive). Positional, after the query."),
        _arg("mode", "string", default="flexible",
             description="flexible (CodeLens semantics: whitespace-insensitive), literal "
                         "(exact text), or word (exact text on word boundaries)."),
        _arg("multiline", "boolean", default=False,
             description="Match across line breaks (flexible mode adds the comment fallback). "
                         "Required if the query contains a newline."),
        _arg("case_sensitive", "boolean", default=False, description="Case-sensitive matching."),
        _arg("ext", "string", repeatable=True, description="Only these extensions, e.g. 'py'."),
        _arg("include", "string", repeatable=True,
             description="Glob(s) (fnmatch; '*' also matches '/') the relative path or name must match."),
        _arg("exclude", "string", repeatable=True,
             description="Glob(s) excluding files or directories."),
        _arg("context", "integer", default=0,
             description=f"Context lines before/after each match (0-{sb.MAX_CONTEXT_LINES})."),
        _arg("max_results", "integer", default=sb.DEFAULT_SEARCH_RESULTS,
             description=f"Maximum matches returned (1-{sb.MAX_SEARCH_RESULTS})."),
        _arg("comment_fallback", "boolean", default=True,
             description="Flexible multiline only: allow the core's comment-stripping fallbacks "
                         "(always reported per match). CLI: --no-comment-fallback."),
        _arg("page_size", "integer", default=None,
             description=f"Matches (or files, with files_only) per page (1-{sb.MAX_SEARCH_RESULTS}); "
                         "alternative to max_results (give only one)."),
        _arg("cursor", "string", default=None,
             description="next_cursor from a previous page of the SAME search. Opaque; "
                         "rejected as invalid_cursor if malformed/foreign, stale_cursor if "
                         "the searched files changed."),
        _arg("count_only", "boolean", default=False,
             description="Return only counts (no snippets). Exact when count_complete is true; "
                         "otherwise partial with partial_reason."),
        _arg("files_only", "boolean", default=False,
             description="Return matching file paths with per-file match_count, no match details. "
                         "files_with_matches counts files; total_matches counts matches "
                         "(null if the scan was cut short)."),
    ],
}

GLOBAL_ARGS = [
    _arg("root", "string", repeatable=True, default="<current directory>",
         description="Permitted root directory. Paths outside all roots are refused."),
    _arg("verbose", "boolean", default=False,
         description="Write diagnostics (and tracebacks for internal errors) to stderr."),
]


# ---- capabilities -----------------------------------------------------------
# Everything an agent needs to use the interface without reading any other
# document. Keep these in sync with the implementation (tests enforce the
# parts that can be checked mechanically).

UNSUPPORTED = [
    "write",                    # no command modifies any file
    "regex_search",             # queries are text, never regular expressions
    "symbol_index",             # no AST / definition / reference awareness
    "follow_directory_symlinks",  # directory symlinks/junctions are not walked
    "read_binary_files",
    "read_files_over_limit",    # see limits.max_file_bytes
    "signed_cursors",           # cursors are tamper-evident only for p/s fields
    "mcp_server",
]

SEARCH_MODES = [
    {"name": "flexible", "default": True,
     "description": "CodeLens semantics: whitespace-insensitive, tolerant of line breaks "
                    "between tokens; with multiline also tries comment-stripping fallbacks.",
     "whitespace_sensitive": False, "allows_whitespace_in_query": True,
     "supports_multiline": True},
    {"name": "literal", "default": False,
     "description": "Exact text match (case-insensitive unless case_sensitive).",
     "whitespace_sensitive": True, "allows_whitespace_in_query": True,
     "supports_multiline": True},
    {"name": "word", "default": False,
     "description": "Exact text bounded by non-word characters. Query must be a single "
                    "token without whitespace; multiline is not allowed.",
     "whitespace_sensitive": True, "allows_whitespace_in_query": False,
     "supports_multiline": False},
]

OUTPUT_MODES = [
    {"name": "matches", "flag": None, "description": "Match records with line/column spans, "
     "content and optional context. Paginated."},
    {"name": "count_only", "flag": "count_only", "description": "Counts only. Not paginated; "
     "exact when count_complete is true."},
    {"name": "files_only", "flag": "files_only", "description": "Matching files with "
     "match_count. Paginated by file."},
]

PAGINATION = {
    "supported": True,
    "commands": ["search"],
    "page_size_argument": "page_size",
    "alternative_argument": "max_results",
    "default_page_size": sb.DEFAULT_SEARCH_RESULTS,
    "max_page_size": sb.MAX_SEARCH_RESULTS,
    "cursor_argument": "cursor",
    "cursor_response_field": "data.next_cursor",
    "cursor_opaque": True,
    "cursor_max_chars": 512,
    "cursor_errors": ["invalid_cursor", "stale_cursor"],
    "paginated_outputs": ["matches", "files_only"],
    "unpaginated_outputs": ["count_only"],
    "completeness_fields": ["more_results", "complete", "limit_reached", "truncated",
                            "truncated_reason"],
    "more_results_values": {
        "none": "search space exhausted; results are complete",
        "confirmed": "at least one more result exists; next_cursor is set",
        "unknown": "search stopped early (time/byte/file budget); the rest was not examined",
    },
    "stateless": True,
    "consistency": "A cursor is rejected as stale_cursor if the size/mtime of any searched "
                   "file (or the set of files) changed. Content-identical rewrites that "
                   "preserve size and mtime are not detected.",
}

REQUEST_PROTOCOL = {
    "flag": "--request",
    "source": "'-' reads stdin; otherwise a file path. UTF-8 JSON, max 1048576 bytes.",
    "shape": {"operation": "string (required)", "args": "object (optional)",
              "roots": "array of strings (optional; ignored if --root is given)"},
    "args_use": "snake_case argument names exactly as listed under commands[*].args",
    "cannot_combine_with_command": True,
}

SEARCH_STRATEGIES = {
    "literal": "mode=literal exact text",
    "word": "mode=word exact text on word boundaries",
    "as_typed": "flexible; matched source equals the query (modulo case)",
    "whitespace_insensitive": "flexible; matched source differs from the query only in "
                              "whitespace/line breaks",
    "snippet_comments_removed": "flexible multiline fallback: comments in the QUERY removed",
    "file_comments_stripped": "flexible multiline fallback: comments in the FILE ignored "
                              "(content still shows the original source)",
}

WARNING_CODES = {
    "output_truncated": "read: a range hit the line budget; continue at ranges[*].next_start",
    "content_not_inspected": "info: file over the size limit; line count/encoding not computed",
    "results_truncated": "search: stopped early (page full, time or byte budget)",
    "file_limit_reached": "search: only the first max_search_files sorted files were considered",
    "file_skipped": "search: a file or directory was skipped (reason in message and "
                    "data.files_skipped)",
}

DOCUMENTATION = {
    "note": "Paths are relative to a CodeLens source checkout; they are not shipped in the "
            "frozen executables. This capabilities response is sufficient to use the "
            "interface.",
    "agent_guide": "docs/AI_AGENT_INTEGRATION.md",
    "json_schema": "docs/schema/codelens-agent-v1.schema.json",
}


def op_capabilities(sandbox, args):
    _unknown_args(args, ())
    c = engine.core()
    data = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "interface_version": INTERFACE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "commands": COMMANDS,
        "global_args": GLOBAL_ARGS,
        "planned_commands": [],
        "unsupported": UNSUPPORTED,
        "search_modes": SEARCH_MODES,
        "output_modes": OUTPUT_MODES,
        "pagination": PAGINATION,
        "request_protocol": REQUEST_PROTOCOL,
        "search_strategies": SEARCH_STRATEGIES,
        "warning_codes": WARNING_CODES,
        "documentation": DOCUMENTATION,
        "roots": list(sandbox.roots),
        "limits": sb.limits(),
        "line_numbering": {
            "base": 1,
            "line_breaks": ["\\r\\n", "\\n", "\\r"],
            "note": "Matches editors; differs from Python str.splitlines().",
        },
        "encodings_tried": list(c.ENCODING_ATTEMPTS),
        "exit_codes": {
            "0": "success",
            "1": "internal error",
            "2": "invalid arguments or request",
            "3": "path missing, outside roots, denied, or not a text file",
            "4": "resource limit exceeded",
            "5": "pagination cursor is stale (searched files changed)",
        },
        "error_codes": sorted(ERROR_EXIT_CODES),
        "deprecations": [],
    }
    return data, []


# ---- info -------------------------------------------------------------------

def _iso_utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def op_info(sandbox, args):
    _unknown_args(args, ("file",))
    user_path = _req_str(args, "file")
    real = sandbox.resolve(user_path)
    shown = sandbox.display(real)
    st = engine.stat_file(real, shown)
    c = engine.core()

    warnings = []
    data = {
        "file": shown,
        "absolute_path": real,
        "kind": "file",
        "size_bytes": st.st_size,
        "modified": _iso_utc(st.st_mtime),
        "modified_epoch": st.st_mtime,
        "is_text": None,
        "content_inspected": False,
        "encoding": None,
        "line_count": None,
        "line_ending": None,
        "ends_with_newline": None,
    }
    data["is_text"] = bool(c.is_text_file(real))
    if not data["is_text"]:
        return data, warnings
    if st.st_size > sb.MAX_FILE_BYTES:
        warnings.append(warning(
            "content_not_inspected",
            f"File exceeds {sb.MAX_FILE_BYTES} bytes; line count and encoding not computed.",
            path=shown))
        return data, warnings

    lines, encoding, text = engine.load_text(real, shown, sb.MAX_FILE_BYTES)
    data.update({
        "content_inspected": True,
        "encoding": encoding,
        "line_count": len(lines),
        "line_ending": engine.detect_line_ending(text),
        "ends_with_newline": text.endswith(("\n", "\r")),
    })
    return data, warnings


# ---- read -------------------------------------------------------------------

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def _parse_range(text):
    m = _RANGE_RE.match(text)
    if not m:
        raise AgentError("invalid_argument",
                         f"Invalid line range {text!r}.",
                         hint="Use 'N' or 'START-END', e.g. '120' or '40-80'.")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) is not None else start
    if start < 1:
        raise AgentError("invalid_argument", f"Line numbers start at 1 (got {text!r}).")
    if end < start:
        raise AgentError("invalid_argument",
                         f"Range end is before start in {text!r}.")
    return start, end


def op_read(sandbox, args):
    _unknown_args(args, ("file", "lines", "around", "context"))
    user_path = _req_str(args, "file")
    ranges_in = _opt_str_list(args, "lines")
    around = _opt_int(args, "around", 1, 2**31 - 1)
    context = _opt_int(args, "context", 0, 200)
    if ranges_in and around is not None:
        raise AgentError("invalid_argument",
                         "'lines' and 'around' are mutually exclusive.")
    if context is not None and around is None:
        raise AgentError("invalid_argument", "'context' requires 'around'.")
    if ranges_in is not None and len(ranges_in) > sb.MAX_READ_RANGES:
        raise AgentError("limit_exceeded",
                         f"At most {sb.MAX_READ_RANGES} ranges per request.")

    real = sandbox.resolve(user_path)
    shown = sandbox.display(real)
    lines, encoding, text = engine.load_text(real, shown, sb.MAX_FILE_BYTES)
    total = len(lines)

    if around is not None:
        if around > total:
            raise AgentError("invalid_argument",
                             f"Line {around} is beyond the end of the file ({total} lines).",
                             path=shown)
        ctx = 5 if context is None else context
        wanted = [(max(1, around - ctx), min(total, around + ctx), f"around:{around}")]
    elif ranges_in:
        wanted = []
        for r in ranges_in:
            s, e = _parse_range(r)
            if s > total:
                raise AgentError(
                    "invalid_argument",
                    f"Range {r!r} starts beyond the end of the file ({total} lines).",
                    path=shown)
            wanted.append((s, e, r.strip()))
    else:
        wanted = [(1, total, "all")] if total else []

    budget = sb.MAX_READ_LINES
    out_ranges, warnings, truncated_any = [], [], False
    for start, end, label in wanted:
        clamped = end > total
        end = min(end, total)
        take_end = min(end, start + budget - 1) if budget > 0 else start - 1
        truncated = take_end < end
        rows, long_lines = [], []
        for n in range(start, take_end + 1):
            t = lines[n - 1]
            if len(t) > sb.MAX_LINE_CHARS:
                t = t[:sb.MAX_LINE_CHARS]
                long_lines.append(n)
            rows.append({"line": n, "text": t})
        budget -= len(rows)
        truncated_any = truncated_any or truncated
        out_ranges.append({
            "requested": label,
            "start": start,
            "end": take_end if rows else end,
            "clamped_to_file_end": clamped,
            "truncated": truncated,
            "next_start": take_end + 1 if truncated else None,
            "truncated_long_lines": long_lines,
            "lines": rows,
        })
    if truncated_any:
        warnings.append(warning(
            "output_truncated",
            f"Output limited to {sb.MAX_READ_LINES} lines; use 'next_start' to continue.",
            path=shown))

    data = {
        "file": shown,
        "absolute_path": real,
        "total_lines": total,
        "encoding": encoding,
        "line_ending": engine.detect_line_ending(text),
        "ranges": out_ranges,
        "truncated": truncated_any,
    }
    return data, warnings


def _opt_bool(args, name, default):
    v = args.get(name)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise AgentError("invalid_argument", f"'{name}' must be a boolean.")
    return v


def op_search(sandbox, args):
    _unknown_args(args, ("query", "paths", "mode", "multiline", "case_sensitive", "ext",
                         "include", "exclude", "context", "max_results", "comment_fallback",
                         "page_size", "cursor", "count_only", "files_only"))
    query = _req_str(args, "query")
    if len(query) > sb.MAX_QUERY_CHARS:
        raise AgentError("limit_exceeded", f"Query exceeds {sb.MAX_QUERY_CHARS} characters.")
    if "\x00" in query:
        raise AgentError("invalid_argument", "Query contains a NUL byte.")
    mode = args.get("mode")
    if mode is None:
        mode = "flexible"
    if mode not in search_mod.MODES:
        raise AgentError("invalid_argument", f"Unknown mode {mode!r}.",
                         hint="Use one of: " + ", ".join(search_mod.MODES))
    multiline = _opt_bool(args, "multiline", False)
    query = query.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in query and not multiline:
        raise AgentError("invalid_argument",
                         "Query contains a line break but multiline is not enabled.",
                         hint="Set multiline=true (--multiline); line breaks are never ignored silently.")
    if mode == "word" and (multiline or any(ch.isspace() for ch in query)):
        raise AgentError("invalid_argument",
                         "word mode needs a single token without whitespace and no multiline.",
                         hint="Use mode=literal or flexible for phrases.")
    paths = _opt_str_list(args, "paths") or ["."]
    if len(paths) > sb.MAX_SEARCH_PATHS:
        raise AgentError("limit_exceeded", f"At most {sb.MAX_SEARCH_PATHS} paths per search.")
    count_only = _opt_bool(args, "count_only", False)
    files_only = _opt_bool(args, "files_only", False)
    if count_only and files_only:
        raise AgentError("invalid_argument", "count_only and files_only are mutually exclusive.")
    cursor_arg = args.get("cursor")
    if cursor_arg is not None and not isinstance(cursor_arg, str):
        raise AgentError("invalid_argument", "'cursor' must be a string.")
    if cursor_arg is not None and count_only:
        raise AgentError("invalid_argument", "count_only does not paginate; omit cursor.")
    page_size = _opt_int(args, "page_size", 1, sb.MAX_SEARCH_RESULTS)
    if page_size is not None and args.get("max_results") is not None:
        raise AgentError("invalid_argument", "Give either page_size or max_results, not both.")
    clean = {
        "output": "count" if count_only else "files" if files_only else "matches",
        "cursor": cursor_arg or None,
        "query": query, "mode": mode, "multiline": multiline,
        "case_sensitive": _opt_bool(args, "case_sensitive", False),
        "comment_fallback": _opt_bool(args, "comment_fallback", True),
        "paths": paths,
        "ext": _opt_str_list(args, "ext") or [],
        "include": _opt_str_list(args, "include") or [],
        "exclude": _opt_str_list(args, "exclude") or [],
        "context": _opt_int(args, "context", 0, sb.MAX_CONTEXT_LINES) or 0,
        "max_results": page_size or _opt_int(args, "max_results", 1, sb.MAX_SEARCH_RESULTS)
                       or sb.DEFAULT_SEARCH_RESULTS,
    }
    return search_mod.run(sandbox, clean)


OPERATIONS = {
    "capabilities": op_capabilities,
    "info": op_info,
    "read": op_read,
    "search": op_search,
}
