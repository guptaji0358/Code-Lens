"""LIAS common: project identity, paths, config. Stdlib only."""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path

LIAS_VERSION = "2.0.0"
SCHEMA_VERSION = 3

DEFAULT_CONFIG = {
    "config_version": 1,
    "retrieval": {"max_results": 5, "max_chars": 2000},
    "automation": {
        "default_timeout_s": 30,
        "max_timeout_s": 120,
        "max_output_bytes": 65536,
        "max_consecutive_failures": 3,
        "cache_ttl_s": 3600,
        "max_cache_entries": 200,
        "compact_chars": 1500,
    },
    "promotion": {"min_repeats": 3, "max_attempts_per_day": 10, "max_generated": 20,
                  "duplicate_jaccard": 0.8,
                  "est_tokens_per_use": 800, "build_cost_tokens": 2400, "test_timeout_s": 15},
    "learning": {
        "unverified_ttl_days": 30,
        "candidate_ttl_days": 30,
        "near_duplicate_jaccard": 0.85,
        "command_prefixes": ["python", "pytest", "pyinstaller", "makensis", "npm", "pip list",
                             "git status", "git log", "git diff"],
    },
    "logs": {"retention_days": 30},
}


class LiasError(Exception):
    pass


def norm_root(root):
    return os.path.normcase(os.path.realpath(str(root)))


def project_id(root):
    return hashlib.sha256(norm_root(root).encode("utf-8")).hexdigest()[:16]


def find_project_root(start=None):
    """Nearest ancestor holding .claude/lias; falls back to the one holding .claude."""
    p = Path(start or __file__).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".claude" / "lias").is_dir():
            return cand
    raise LiasError("no .claude/lias found above %s" % p)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_bounded(argv, stdin_bytes, cwd, env, timeout, cap):
    """Run a child with a wall-clock timeout and a hard cap on captured stdout/stderr bytes.

    Unlike subprocess.run(capture_output=True) this never buffers unbounded output: when stdout exceeds
    `cap` the child is killed. Returns dict(rc, out, err, status) with status ok|timeout|output_limit.
    """
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, env=env)
    bufs = {"out": bytearray(), "err": bytearray()}
    state = {"over": False}

    def pump(stream, key):
        total = 0
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                total += len(chunk)
                room = cap - len(bufs[key])
                if room > 0:
                    bufs[key].extend(chunk[:room])
                if total > cap:
                    state["over"] = True
                    p.kill()
                    return
        except (OSError, ValueError):
            return

    def feed():
        try:
            p.stdin.write(stdin_bytes)
            p.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for t in threads:
        t.start()
    status = "ok"
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        status = "timeout"
    p.wait()
    for t in threads:
        t.join(timeout=2)
    if state["over"] and status == "ok":
        status = "output_limit"
    return {"rc": p.returncode, "out": bytes(bufs["out"]), "err": bytes(bufs["err"]), "status": status}


def validate_config(cfg):
    """Return a list of problems (empty = ok) for numeric limits that must be positive."""
    problems = []
    checks = {
        ("automation", "default_timeout_s"): 1, ("automation", "max_timeout_s"): 1,
        ("automation", "max_output_bytes"): 1024, ("automation", "max_consecutive_failures"): 1,
        ("automation", "max_cache_entries"): 1, ("retrieval", "max_results"): 1,
        ("retrieval", "max_chars"): 100, ("promotion", "max_attempts_per_day"): 1,
        ("promotion", "test_timeout_s"): 1, ("logs", "retention_days"): 1,
    }
    for (sec, key), lo in checks.items():
        v = cfg.get(sec, {}).get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
            problems.append("%s.%s must be a number >= %s (got %r)" % (sec, key, lo, v))
    return problems


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Lias:
    """Project-bound context. Everything resolves under <root>/.claude/lias."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.dir = self.root / ".claude" / "lias"
        if not self.dir.is_dir():
            raise LiasError("LIAS not installed at %s" % self.dir)
        self.pid = project_id(self.root)
        self.config = self._load_config()
        self._db = None

    def _load_config(self):
        p = self.dir / "config" / "lias.json"
        base = copy.deepcopy(DEFAULT_CONFIG)  # never hand out the shared module-level dict
        if not p.exists():
            return base
        try:
            cfg = _merge(base, json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, AttributeError) as e:
            raise LiasError("invalid config %s: %s" % (p, e))
        bad = validate_config(cfg)
        if bad:
            raise LiasError("invalid config %s: %s" % (p, "; ".join(bad)))
        return cfg

    @property
    def db_path(self):
        return self.dir / "database" / "lias.sqlite3"

    def db(self):
        if self._db is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA foreign_keys=ON")
                from knowledge import migrate  # local import avoids cycle
                migrate(con, self.pid, str(self.root))
            except sqlite3.DatabaseError as e:
                con.close()
                raise LiasError("database unreadable or corrupt (%s). Restore with: "
                                "python .claude/lias/cli.py recover" % e)
            except BaseException:
                con.close()
                raise
            self._db = con
        return self._db

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
