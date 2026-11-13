"""
cursor.py - stateless pagination cursors for `search`.

A cursor is base64url(JSON) with exactly these keys:
    v  cursor format version (1)
    k  kind: "m" (match pages) or "f" (files-only pages)
    p  request fingerprint: sha256 over every parameter that shapes results
       (query, mode, case, multiline, comment fallback, resolved roots,
       resolved paths, ext/include/exclude, context, output kind)
    s  snapshot fingerprint: sha256 over the sorted list of discovered
       files as (display path, size, mtime_ns)
    f  index into the deterministic sorted file list
    o  ordinal: match number inside file f (kind m) or index into the
       list of matching files (kind f)

Security: the cursor never carries a filesystem path - only integer
positions into a list the server re-derives, inside the sandbox, on every
request - so a forged cursor cannot introduce a path. Hashes are
truncated to 32 hex chars; roots enter only through the hash, so absolute
paths are not exposed. The cursor is NOT signed: tampering with f/o can
only move the read position inside a result set the caller is already
allowed to read (changing p or s is detected and rejected).

Limitation: the snapshot fingerprint is metadata-based (size + mtime_ns,
no content hashing). A same-size edit that preserves mtime (or a
coarse-mtime filesystem edited twice within one timestamp tick) is not
detected. Any add/delete/modify among the *searched* files, including
files before the current position, invalidates the cursor.
"""

import base64
import binascii
import hashlib
import json
import os

from .protocol import AgentError

CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 512
_KEYS = {"v", "k", "p", "s", "f", "o"}
_KINDS = ("m", "f")
_MAX_INDEX = 10 ** 9
_B64 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def _h(obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def request_fingerprint(sandbox, args, kind):
    roots = sorted(os.path.normcase(r) for r in sandbox.roots)
    paths = sorted(os.path.normcase(sandbox.resolve(p)) for p in args["paths"])
    return _h({
        "kind": kind, "query": args["query"], "mode": args["mode"],
        "cs": args["case_sensitive"], "ml": args["multiline"],
        "cf": args["comment_fallback"], "roots": roots, "paths": paths,
        "ext": sorted(args["ext"]), "inc": sorted(args["include"]),
        "exc": sorted(args["exclude"]), "ctx": args["context"],
    })


def snapshot_fingerprint(files):
    """files: sorted [(shown, real)]. Stats each file once."""
    h = hashlib.sha256()
    for shown, real in files:
        try:
            st = os.stat(real)
            sig = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            sig = "missing"
        h.update(f"{shown}\0{sig}\n".encode("utf-8", errors="replace"))
    h.update(f"n={len(files)}".encode())
    return h.hexdigest()[:32]


def encode(kind, p, s, f, o):
    raw = json.dumps({"v": CURSOR_VERSION, "k": kind, "p": p, "s": s, "f": f, "o": o},
                     sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _bad(msg):
    return AgentError("invalid_cursor", msg,
                      hint="Do not construct or edit cursors; restart the search "
                           "without --cursor.")


def decode(text):
    """Validates structure only -> dict. Raises invalid_cursor."""
    if not isinstance(text, str) or not text or len(text) > MAX_CURSOR_CHARS:
        raise _bad("Cursor is empty or too long.")
    if not set(text) <= _B64:
        raise _bad("Cursor has invalid characters.")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        obj = json.loads(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise _bad("Cursor is malformed.")
    if not isinstance(obj, dict) or set(obj) != _KEYS or obj["v"] != CURSOR_VERSION:
        raise _bad("Cursor structure or version is not supported.")
    if obj["k"] not in _KINDS:
        raise _bad("Cursor kind is invalid.")
    for key in ("p", "s"):
        v = obj[key]
        if not (isinstance(v, str) and len(v) == 32
                and all(c in "0123456789abcdef" for c in v)):
            raise _bad("Cursor fingerprint is malformed.")
    for key in ("f", "o"):
        v = obj[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= _MAX_INDEX:
            raise _bad("Cursor position is invalid.")
    return obj
