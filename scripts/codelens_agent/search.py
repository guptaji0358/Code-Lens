"""
search.py - the `search` operation.

All matching is delegated to the CodeLens core (scripts/CodeLensCLI.py):
  * flexible patterns      -> core.build_flexible_pattern
  * match computation      -> core._find_matches
  * comment fallback chain -> core._search_file_with_comment_fallback
  * comment helpers        -> core.COMMENT_MARKER_RE / has_comments
No matching algorithm is reimplemented here; this module only builds the
pattern for the two extra modes (literal, word), walks files safely,
and annotates every match with HOW it was obtained.

Strategies reported per match (never silently relaxed):
  literal                  exact text, no whitespace flexibility (mode=literal)
  word                     exact text bounded by non-word characters (mode=word)
  as_typed                 flexible mode; the matched source text equals the
                           query (modulo case when case-insensitive)
  whitespace_insensitive   flexible mode; matched source differs from the
                           query only in whitespace/line breaks
  snippet_comments_removed flexible fallback: comment(s) in the QUERY were
                           removed before matching
  file_comments_stripped   flexible fallback: comments in the FILE were
                           blanked before matching (matched_text/content show
                           the ORIGINAL source, comments included)

Columns are 1-based character offsets; end_column is INCLUSIVE and lies on
end_line. Lines use editor numbering (\\r\\n, \\n, \\r).
"""

import fnmatch
import os
import re
import stat
import time

from . import cursor as cur
from . import engine
from . import sandbox as sb
from .protocol import AgentError, warning

MODES = ("flexible", "literal", "word")

_MAX_SNIPPET_LINES = 100
_MAX_WARNINGS = 20


class _Doc:
    """Minimal stand-in for core.LoadedFile (the core's fallback helper only
    reads `.lines`)."""

    def __init__(self, lines):
        self.lines = lines


# ---- pattern construction ---------------------------------------------------

def _build_pattern(c, query, mode, case_sensitive):
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if mode == "flexible":
            return re.compile(c.build_flexible_pattern(query), flags)
        if mode == "literal":
            return re.compile(re.escape(query), flags)
        return re.compile(r"(?<!\w)" + re.escape(query) + r"(?!\w)", flags)
    except re.error as e:
        raise AgentError("invalid_argument", f"Cannot build search pattern: {e}")


# ---- file discovery ---------------------------------------------------------

def _norm_exts(exts):
    out = set()
    for e in exts or []:
        e = e.strip().lower()
        if not e or any(ch in e for ch in "/\\*?"):
            raise AgentError("invalid_argument", f"Invalid extension filter {e!r}.",
                             hint="Use e.g. 'py' or '.py'.")
        out.add(e if e.startswith(".") else "." + e)
    return out


def _glob_hit(globs, shown):
    base = shown.rsplit("/", 1)[-1]
    for g in globs:
        gl = g.replace("\\", "/")
        if fnmatch.fnmatchcase(shown.lower(), gl.lower()) or \
           fnmatch.fnmatchcase(base.lower(), gl.lower()):
            return True
    return False


class _Filter:
    def __init__(self, exts, include, exclude):
        self.exts, self.include, self.exclude = exts, include, exclude

    def allows(self, shown):
        if self.exts and os.path.splitext(shown)[1].lower() not in self.exts:
            return False
        if self.include and not _glob_hit(self.include, shown):
            return False
        if self.exclude and _glob_hit(self.exclude, shown):
            return False
        return True

    def dir_excluded(self, shown):
        return bool(self.exclude) and _glob_hit(self.exclude, shown)


class _Skips:
    def __init__(self):
        self.counts, self.warnings, self.suppressed = {}, [], 0

    def add(self, reason, message, path):
        self.counts[reason] = self.counts.get(reason, 0) + 1
        if len(self.warnings) < _MAX_WARNINGS:
            self.warnings.append(warning("file_skipped", f"{message} [{reason}]", path=path))
        else:
            self.suppressed += 1


def _is_junction(ent):
    """True for a Windows directory junction / mount point DirEntry.

    DirEntry.is_junction() exists from Python 3.12. On older versions the
    same fact is read from the entry's reparse-point tag (st_reparse_tag,
    Windows, Python 3.8+), so the check never depends on the interpreter
    version. Failure to inspect an entry is treated as "is a junction"
    (fail closed: the entry is then resolved and checked, never walked)."""
    native = getattr(ent, "is_junction", None)
    if native is not None:
        return native()
    if os.name != "nt":
        return False
    try:
        tag = ent.stat(follow_symlinks=False).st_reparse_tag
    except AttributeError:
        return False       # not a reparse point (no tag reported)
    except OSError:
        return True
    return tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def _discover(sandbox, paths, flt, skips, skip_dirs):
    """Returns (sorted list of (shown, real), file_limit_hit)."""
    found, seen, limit_hit = {}, set(), False

    def consider(real, shown):
        nonlocal limit_hit
        key = os.path.normcase(real)
        if key in seen:
            return True
        try:  # defense in depth: every file must resolve inside a root
            sandbox.resolve(real)
        except AgentError:
            skips.add("outside_root", "Path resolves outside the permitted roots.", shown)
            return True
        if not flt.allows(shown):
            skips.counts["filtered"] = skips.counts.get("filtered", 0) + 1
            return True
        if len(found) >= sb.MAX_SEARCH_FILES:
            limit_hit = True
            return False
        seen.add(key)
        found[shown] = real
        return True

    for user_path in paths:
        real = sandbox.resolve(user_path)
        shown = sandbox.display(real)
        if os.path.isdir(real):
            stack = [real]
            while stack and not limit_hit:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        entries = sorted(it, key=lambda e: e.name)
                except OSError as e:
                    skips.add("unreadable_dir", f"Cannot list directory: {e.strerror or e}",
                              sandbox.display(d))
                    continue
                subdirs = []
                for ent in entries:
                    try:
                        # Windows directory junctions are reparse points that
                        # DirEntry.is_symlink() reports False for; treat them
                        # as links so they are never walked blindly.
                        is_link = ent.is_symlink() or _is_junction(ent)
                        is_dir = ent.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_link:
                        try:
                            target = sandbox.resolve(ent.path)
                        except AgentError:
                            skips.add("outside_root", "Symlink points outside the permitted roots.",
                                      sandbox.display(os.path.abspath(ent.path)))
                            continue
                        if os.path.isdir(target):
                            skips.add("symlink_dir", "Directory symlinks are not followed.",
                                      sandbox.display(ent.path))
                            continue
                        ent_real = target
                    else:
                        ent_real = ent.path
                    ent_shown = sandbox.display(os.path.realpath(ent_real))
                    if is_dir and not is_link:
                        if ent.name in skip_dirs or flt.dir_excluded(ent_shown):
                            continue
                        subdirs.append(ent.path)
                    elif not consider(os.path.realpath(ent_real), ent_shown):
                        break
                stack.extend(reversed(subdirs))
        else:
            if not os.path.exists(real):
                raise AgentError("path_not_found", "Path does not exist.", path=user_path)
            consider(real, shown)
    return [(s, found[s]) for s in sorted(found)], limit_hit


# ---- match annotation -------------------------------------------------------

def _slice_text(lines, sl, sc, el, ec):
    """Source text from (line sl, 0-based col sc) to (line el, exclusive col ec)."""
    if sl == el:
        return lines[sl - 1][sc:ec]
    parts = [lines[sl - 1][sc:]]
    parts.extend(lines[sl:el - 1])
    parts.append(lines[el - 1][:ec])
    return "\n".join(parts)


def _clip(text):
    return text if len(text) <= sb.MAX_LINE_CHARS else text[:sb.MAX_LINE_CHARS]


def _expand(results, lines):
    """core match descriptors -> [(start_line, start_col0, end_line, end_col0_excl)]"""
    out = []
    for r in results:
        if r[0] == "single":
            _, ln, _text, spans = r
            for s, e in spans:
                out.append((ln, s, ln, e))
        else:
            _, sl, el, per_line = r
            first, last = per_line[0], per_line[-1]
            sc = first[2][0][0] if first[2] else len(first[1])
            ec = last[2][0][1] if last[2] else 0
            out.append((sl, sc, el, ec))
    out.sort()
    return out


def _context(lines, start, end, n):
    before = [{"line": i, "text": _clip(lines[i - 1])}
              for i in range(max(1, start - n), start)]
    after = [{"line": i, "text": _clip(lines[i - 1])}
             for i in range(end + 1, min(len(lines), end + n) + 1)]
    return before, after


def _strategy(mode, note, matched, query, case_sensitive):
    """-> (strategy, exact_text_match, fallback_used, comments_stripped_from)"""
    if mode == "literal":
        return "literal", True, False, None
    if mode == "word":
        return "word", True, False, None
    if note is None:
        a, b = (matched, query) if case_sensitive else (matched.lower(), query.lower())
        exact = a == b
        return ("as_typed" if exact else "whitespace_insensitive"), exact, False, None
    if "your search text" in note:
        return "snippet_comments_removed", False, True, "query"
    return "file_comments_stripped", False, True, "file"


def _match_file(c, doc, pattern, pattern_nocomment, args):
    """-> (list of (start_line,sc,end_line,ec), note)"""
    mode, multiline = args["mode"], args["multiline"]
    if mode == "flexible" and multiline and args["comment_fallback"]:
        results, note = c._search_file_with_comment_fallback(doc, pattern, pattern_nocomment)
        return _expand(results, doc.lines), note
    results = c._find_matches(doc.lines, pattern, multiline=multiline)
    return _expand(results, doc.lines), None


def _match_record(lines, sl, sc, el, ec, note, args):
    mode, query, cs = args["mode"], args["query"], args["case_sensitive"]
    ctx = args["context"]
    matched = _slice_text(lines, sl, sc, el, ec)
    strat, exact, fb, stripped_from = _strategy(mode, note, matched, query, cs)
    content_lines = lines[sl - 1:el]
    snippet_cut = len(content_lines) > _MAX_SNIPPET_LINES
    content = "\n".join(_clip(t) for t in content_lines[:_MAX_SNIPPET_LINES])
    before, after = _context(lines, sl, el, ctx)
    return {
        "line": sl, "end_line": el,
        "column": sc + 1, "end_column": max(ec, sc + 1) if sl == el else ec,
        "content": content,
        "matched_text": _clip(matched),
        "strategy": strat,
        "exact_text_match": exact,
        "case_sensitive": cs,
        "fallback_used": fb,
        "comments_stripped": fb,
        "comments_stripped_from": stripped_from,
        "content_truncated": snippet_cut or len(matched) > sb.MAX_LINE_CHARS,
        "context_before": before,
        "context_after": after,
    }


_STOP_MESSAGES = {
    "max_results": "Stopped after {n} {unit} (max_results/page_size) with more results "
                   "confirmed; pass next_cursor as --cursor to continue.",
    "time_budget_exceeded": "Search stopped: time budget exceeded; results are partial.",
    "byte_budget_exceeded": "Search stopped: total byte budget exceeded; results are partial.",
}


def run(sandbox, args):
    """args adds: output ("matches"|"count"|"files"), cursor (str|None).

    Completeness vocabulary (all additive fields):
      limit_reached  the page filled up to max_results/page_size
      more_results   "none"      search space exhausted (complete)
                     "confirmed" at least one more result exists
                     "unknown"   stopped early (time/byte/file budget); the
                                 rest was NOT examined
      truncated      legacy flag: True whenever the search stopped early
                     for any reason (more_results != "none")
    """
    c = engine.core()
    query, mode = args["query"], args["mode"]
    cs = args["case_sensitive"]
    output = args.get("output", "matches")
    pattern = _build_pattern(c, query, mode, cs)

    pattern_nocomment = None
    if mode == "flexible" and args["multiline"] and args["comment_fallback"] \
            and c.has_comments(query):
        stripped = c.COMMENT_MARKER_RE.sub("", query)
        if stripped.strip():
            pattern_nocomment = _build_pattern(c, stripped, mode, cs)

    flt = _Filter(_norm_exts(args["ext"]), args["include"], args["exclude"])
    skips = _Skips()
    files, file_limit_hit = _discover(sandbox, args["paths"], flt, skips,
                                      getattr(c, "_SKIP_DIR_NAMES", set()))

    kind = "f" if output == "files" else "m"
    req_fp = cur.request_fingerprint(sandbox, args, kind)
    snap_fp = cur.snapshot_fingerprint(files)
    start_fi = start_ord = 0
    if args.get("cursor"):
        cd = cur.decode(args["cursor"])
        if cd["k"] != kind or cd["p"] != req_fp:
            raise AgentError("invalid_cursor",
                             "Cursor does not belong to this search request.",
                             hint="A cursor only works with the exact same query, mode, "
                                  "case/multiline/comment settings, roots, paths, filters "
                                  "and output kind. Restart the search without --cursor.")
        if cd["s"] != snap_fp:
            raise AgentError("stale_cursor",
                             "Files changed since the cursor was issued; paging would be "
                             "inconsistent.",
                             hint="Restart the search without --cursor.")
        if cd["f"] > len(files):
            raise AgentError("invalid_cursor", "Cursor position is out of range.")
        start_fi, start_ord = cd["f"], cd["o"]

    deadline = time.monotonic() + sb.SEARCH_TIME_BUDGET_SECONDS
    budget_bytes = sb.MAX_SEARCH_TOTAL_BYTES
    max_results = args["max_results"]
    results_out, total, searched = [], 0, 0
    file_rows = []
    stop_reason, next_pos = None, None

    first_fi = 0 if output == "files" else start_fi
    for fi in range(first_fi, len(files)):
        shown, real = files[fi]
        if time.monotonic() > deadline:
            stop_reason, next_pos = "time_budget_exceeded", (fi, 0)
            break
        try:
            size = os.stat(real).st_size
        except OSError:
            skips.add("unreadable", "Cannot stat file.", shown)
            continue
        if size > sb.MAX_FILE_BYTES:
            skips.add("too_large", f"File is {size} bytes; limit is {sb.MAX_FILE_BYTES}.", shown)
            continue
        if budget_bytes - size < 0:
            stop_reason, next_pos = "byte_budget_exceeded", (fi, 0)
            break
        try:
            lines, encoding, _text = engine.load_text(real, shown, sb.MAX_FILE_BYTES)
        except AgentError as e:
            reason = {"not_text_file": "binary", "file_too_large": "too_large",
                      "permission_denied": "unreadable"}.get(e.code, e.code)
            skips.add(reason, e.message, shown)
            continue
        budget_bytes -= size
        searched += 1
        spans, note = _match_file(c, _Doc(lines), pattern, pattern_nocomment, args)
        if not spans:
            continue

        if output == "count":
            total += len(spans)
            file_rows.append(shown)
            continue
        if output == "files":
            total += len(spans)
            file_rows.append({"file": shown, "encoding": encoding,
                              "match_count": len(spans)})
            continue

        skip = start_ord if fi == start_fi else 0
        matches = []
        for ordinal in range(skip, len(spans)):
            if total >= max_results:
                stop_reason, next_pos = "max_results", (fi, ordinal)
                break
            sl, sc, el, ec = spans[ordinal]
            matches.append(_match_record(lines, sl, sc, el, ec, note, args))
            total += 1
        if matches:
            results_out.append({"file": shown, "encoding": encoding, "matches": matches})
        if stop_reason:
            break

    scan_complete = stop_reason is None and not file_limit_hit
    files_out = None
    if output == "files":
        files_out = file_rows[start_ord:start_ord + max_results]
        if scan_complete and start_ord + max_results < len(file_rows):
            stop_reason, next_pos = "max_results", (0, start_ord + max_results)
        total_out = total if scan_complete else None
        n_match_files = len(file_rows)
    elif output == "count":
        total_out, n_match_files = total, len(file_rows)
    else:
        total_out, n_match_files = total, len(results_out)

    if stop_reason is None and file_limit_hit:
        stop_reason = "file_limit_reached"
    if stop_reason == "max_results":
        more = "confirmed"
    elif stop_reason is None:
        more = "none"
    else:
        more = "unknown"

    # A cursor is issued whenever there is a well-defined resume point:
    # confirmed-more pages, and match scans cut by the time/byte budget.
    # Never for count-only, never when the file list itself was capped
    # (the unexamined files are not addressable), and never for a files-only
    # scan that did not finish (its page boundaries are undefined).
    next_cursor = None
    if next_pos is not None and output != "count" \
            and not (output == "files" and stop_reason != "max_results") \
            and stop_reason != "file_limit_reached":
        next_cursor = cur.encode(kind, req_fp, snap_fp, next_pos[0], next_pos[1])

    warnings = []
    if stop_reason in _STOP_MESSAGES:
        unit = "files" if output == "files" else "matches"
        warnings.append(warning("results_truncated",
                                _STOP_MESSAGES[stop_reason].format(n=max_results, unit=unit)))
    if file_limit_hit:
        warnings.append(warning("file_limit_reached",
                                f"Only the first {sb.MAX_SEARCH_FILES} files (sorted) were "
                                "considered; results/counts are partial."))
    warnings.extend(skips.warnings)
    if skips.suppressed:
        warnings.append(warning("file_skipped",
                                f"{skips.suppressed} more skipped files not listed."))

    if output == "matches":
        limit_reached = total >= max_results
    elif output == "files":
        limit_reached = len(files_out) >= max_results
    else:
        limit_reached = False

    data = {
        "query": query,
        "mode": mode,
        "multiline": args["multiline"],
        "case_sensitive": cs,
        "comment_fallback": args["comment_fallback"],
        "results": results_out,
        "total_matches": total_out,
        "files_with_matches": n_match_files,
        "files_searched": searched,
        "files_skipped": dict(sorted(skips.counts.items())),
        "truncated": stop_reason is not None,
        "truncated_reason": stop_reason,
        "positions": {"base": 1, "end_column": "inclusive", "unit": "characters"},
        # ---- M3 (additive) ----
        "output": {"matches": "matches", "count": "count_only",
                   "files": "files_only"}[output],
        "page_size": max_results,
        "limit_reached": limit_reached,
        "more_results": more,
        "complete": more == "none",
        "next_cursor": next_cursor,
    }
    if output == "count":
        data["count_complete"] = scan_complete
        data["partial_reason"] = None if scan_complete else stop_reason
    if output == "files":
        data["files"] = files_out
        data["total_matches_known"] = scan_complete
    return data, warnings
