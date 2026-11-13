"""
engine.py - adapter between the agent interface and the CodeLens core
(scripts/CodeLensCLI.py). The core is reused unmodified:

  * text/binary detection  -> core.is_text_file
  * encoding detection     -> core.read_file_smart

Two things the adapter handles itself so the core stays untouched:

  1. stdout isolation. Importing CodeLensCLI runs colorama.init(), which
     replaces sys.stdout/sys.stderr with wrappers. The import happens with
     both streams pointed at a throwaway buffer and the originals are
     restored afterwards, so the core can never write to or wrap the real
     stdout.
  2. editor-compatible line numbers. The core uses str.splitlines(), which
     also breaks on form feed, \\v, \\x1c-\\x1e, \\x85 and U+2028/2029, so its
     line numbers can disagree with editors. Here lines are split only on
     \\r\\n, \\n and \\r.
"""

import io
import os
import re
import sys

from .protocol import AgentError

_core = None
_LINE_SPLIT_RE = re.compile(r"\r\n|\n|\r")


def core():
    """Imports (once) and returns the CodeLensCLI module, silently."""
    global _core
    if _core is None:
        scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        saved = (sys.stdout, sys.stderr)
        sink = io.StringIO()
        sys.stdout = sys.stderr = sink
        try:
            import CodeLensCLI
        finally:
            sys.stdout, sys.stderr = saved
        _core = CodeLensCLI
    return _core


def split_lines(text):
    """Editor-compatible split: only \\r\\n, \\n, \\r are line breaks, and a
    single trailing line break does not create an extra empty line."""
    if text == "":
        return []
    lines = _LINE_SPLIT_RE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def detect_line_ending(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [name for name, n in (("crlf", crlf), ("lf", lf), ("cr", cr)) if n]
    if not kinds:
        return "none"
    return kinds[0] if len(kinds) == 1 else "mixed"


def stat_file(real_path, display_path):
    """Returns os.stat_result for a regular file or raises AgentError."""
    try:
        st = os.stat(real_path)
    except FileNotFoundError:
        raise AgentError("path_not_found", "File does not exist.", path=display_path)
    except PermissionError:
        raise AgentError("permission_denied", "Permission denied.", path=display_path)
    except OSError as e:
        raise AgentError("path_not_found", f"Cannot stat path: {e.strerror or e}",
                         path=display_path)
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise AgentError("not_a_file", "Path is not a regular file.",
                         path=display_path)
    return st


def _has_utf16_bom(real_path):
    with open(real_path, "rb") as fh:
        head = fh.read(2)
    return head in (b"\xff\xfe", b"\xfe\xff")


def load_text(real_path, display_path, max_bytes):
    """Loads a text file via the core's detection/decoding and returns
    (lines, encoding, raw_text). Raises AgentError for missing, denied,
    oversized, or non-text files."""
    st = stat_file(real_path, display_path)
    if st.st_size > max_bytes:
        raise AgentError(
            "file_too_large",
            f"File is {st.st_size} bytes; the limit is {max_bytes}.",
            path=display_path,
            hint="Use `info` for metadata; this interface does not read "
                 "files above the limit.")
    c = core()
    try:
        if not c.is_text_file(real_path):
            raise AgentError("not_text_file",
                             "File looks binary; it is not read as text.",
                             path=display_path)
        text, encoding = c.read_file_smart(real_path)
        if encoding.startswith("utf-16") and not _has_utf16_bom(real_path):
            # The core's fallback chain tries utf-16 before cp1252, so any
            # even-length legacy-encoded file "decodes" as CJK garbage.
            # Genuine UTF-16 text carries a BOM; without one, re-decode as
            # a single-byte encoding instead (adapter-level guard; core
            # untouched).
            with open(real_path, "rb") as fh:
                raw = fh.read()
            try:
                text, encoding = raw.decode("cp1252"), "cp1252"
            except UnicodeDecodeError:
                text, encoding = raw.decode("latin-1"), "latin-1"
    except AgentError:
        raise
    except FileNotFoundError:
        raise AgentError("path_not_found", "File disappeared while reading.",
                         path=display_path, retryable=True)
    except PermissionError:
        raise AgentError("permission_denied", "Permission denied.", path=display_path)
    except OSError as e:
        raise AgentError("permission_denied",
                         f"Cannot read file: {e.strerror or e}", path=display_path)
    return split_lines(text), encoding, text
