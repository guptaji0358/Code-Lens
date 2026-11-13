"""
sandbox.py - root allowlist, path resolution and resource limits.

Security model:
  * Every path an agent supplies is resolved to a real, absolute path
    (symlinks followed) and must lie inside one of the explicitly
    permitted roots. Roots come from --root (repeatable); with none given
    the single root is the current working directory.
  * Because the *resolved* path is checked, a symlink inside a root that
    points outside it is rejected (path_outside_root), as are `..`
    traversal, absolute paths elsewhere, and other drives/UNC shares.
  * Relative paths are resolved against the first root.
  * Nothing here (or anywhere in codelens_agent) opens files for writing.
"""

import os

from .protocol import AgentError

# ---- resource limits -------------------------------------------------------
MAX_FILE_BYTES = 5 * 1024 * 1024    # files larger than this are not read
MAX_READ_LINES = 2000               # lines returned per `read` response
MAX_READ_RANGES = 50                # ranges per `read` request
MAX_LINE_CHARS = 4000               # longer lines are truncated in output
MAX_SEARCH_FILES = 20000            # files considered per search (core MAX_FOLDER_FILES)
MAX_SEARCH_RESULTS = 1000           # hard cap for max_results
DEFAULT_SEARCH_RESULTS = 100
MAX_CONTEXT_LINES = 20              # context lines per side of a match
MAX_QUERY_CHARS = 10000
MAX_SEARCH_PATHS = 100
MAX_SEARCH_TOTAL_BYTES = 256 * 1024 * 1024  # total bytes read per search
SEARCH_TIME_BUDGET_SECONDS = 30.0   # checked between files


def limits():
    return {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_read_lines": MAX_READ_LINES,
        "max_read_ranges": MAX_READ_RANGES,
        "max_line_chars": MAX_LINE_CHARS,
        "max_search_files": MAX_SEARCH_FILES,
        "max_search_results": MAX_SEARCH_RESULTS,
        "default_search_results": DEFAULT_SEARCH_RESULTS,
        "max_context_lines": MAX_CONTEXT_LINES,
        "max_query_chars": MAX_QUERY_CHARS,
        "max_search_paths": MAX_SEARCH_PATHS,
        "max_search_total_bytes": MAX_SEARCH_TOTAL_BYTES,
        "search_time_budget_seconds": SEARCH_TIME_BUDGET_SECONDS,
    }


def _key(path):
    """Comparison form: case-folded on Windows, separators normalized."""
    return os.path.normcase(os.path.normpath(path))


def _is_within(path, root):
    p, r = _key(path), _key(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives, or absolute vs relative
        return False


class Sandbox:
    def __init__(self, roots=None):
        raw = list(roots) if roots else [os.getcwd()]
        self.roots = []
        for r in raw:
            if not isinstance(r, str) or not r or "\x00" in r:
                raise AgentError("invalid_argument", f"Invalid root: {r!r}")
            real = os.path.realpath(os.path.abspath(r))
            if not os.path.isdir(real):
                raise AgentError("path_not_found",
                                 f"Root is not an existing directory: {r}",
                                 path=r)
            if real not in self.roots:
                self.roots.append(real)

    def resolve(self, user_path):
        """Returns the real absolute path for `user_path`, or raises
        AgentError. Does not require the path to exist."""
        if not isinstance(user_path, str) or not user_path.strip():
            raise AgentError("invalid_argument", "Path must be a non-empty string.")
        if "\x00" in user_path:
            raise AgentError("invalid_argument", "Path contains a NUL byte.")

        candidate = user_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.roots[0], candidate)
        real = os.path.realpath(os.path.abspath(candidate))

        if not any(_is_within(real, root) for root in self.roots):
            raise AgentError(
                "path_outside_root",
                "Path is outside the permitted roots.",
                path=user_path,
                hint="Pass --root <dir> to permit a directory; symlinks are "
                     "checked by their resolved target.")
        return real

    def display(self, real_path):
        """Path shown to agents: relative to the containing root with
        forward slashes when possible, else absolute."""
        for root in self.roots:
            if _is_within(real_path, root):
                rel = os.path.relpath(real_path, root)
                return rel.replace(os.sep, "/") if rel != "." else "."
        return real_path.replace(os.sep, "/")
