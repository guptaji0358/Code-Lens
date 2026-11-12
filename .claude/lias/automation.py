"""LIAS automation registry + runner.

Manifests live in automation/manifests/*.json. Entry scripts must live under
automation/scripts/ (or automation/generated/ once activated). Commands are argv
arrays (never shell strings); JSON goes in via stdin and comes out via stdout.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from common import LiasError, run_bounded
from knowledge import now_iso

REQUIRED = ("id", "name", "description", "entry", "timeout_s", "risk", "version", "status")
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "number": (int, float)}


class Automations:
    def __init__(self, lias):
        self.l = lias
        self.mdir = lias.dir / "automation" / "manifests"
        self.allowed = [(lias.dir / "automation" / "scripts").resolve(),
                        (lias.dir / "automation" / "generated").resolve()]

    # ---- registry ----
    def load(self):
        out = {}
        if self.mdir.is_dir():
            for p in sorted(self.mdir.glob("*.json")):
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_manifest(m)
                    out[m["id"]] = m
                except (ValueError, LiasError) as e:
                    out["!invalid:" + p.name] = {"id": p.name, "status": "invalid", "error": str(e)}
        return out

    def validate_manifest(self, m):
        miss = [k for k in REQUIRED if k not in m]
        if miss:
            raise LiasError("manifest missing %s" % miss)
        if m["risk"] not in (0, 1):
            raise LiasError("risk must be 0 (read-only) or 1 (project-local write); tier 2 is never auto-run")
        if not isinstance(m["timeout_s"], (int, float)) or m["timeout_s"] <= 0:
            raise LiasError("bad timeout_s")
        self._entry_path(m)

    def _entry_path(self, m):
        p = (self.l.dir / "automation" / m["entry"]).resolve()
        if not any(a == p or a in p.parents for a in self.allowed) or not p.is_file():
            raise LiasError("entry %r must be an existing file under automation/scripts or generated" % m["entry"])
        return p

    def state(self, aid):
        r = self.l.db().execute("SELECT * FROM automation_state WHERE automation_id=?", (aid,)).fetchone()
        return dict(r) if r else {"status": None, "consecutive_failures": 0}

    def effective_status(self, m):
        return self.state(m["id"])["status"] or m["status"]

    # ---- validation of inputs ----
    @staticmethod
    def check_input(m, data):
        schema = m.get("input_schema", {})
        if not isinstance(data, dict):
            raise LiasError("input must be a JSON object")
        extra = set(data) - set(schema)
        if extra:
            raise LiasError("unexpected input fields: %s" % sorted(extra))
        for k, spec in schema.items():
            if k not in data:
                if spec.get("required"):
                    raise LiasError("missing required input %r" % k)
                continue
            if not isinstance(data[k], TYPES[spec["type"]]) or (spec["type"] == "integer" and isinstance(data[k], bool)):
                raise LiasError("input %r must be %s" % (k, spec["type"]))
        return data

    # ---- cache ----
    def _fingerprint(self, m):
        h = hashlib.sha256()
        for pat in m.get("fingerprint_globs", []):
            if ".." in Path(pat).parts or Path(pat).is_absolute():
                raise LiasError("bad fingerprint glob")
            for p in sorted(self.l.root.glob(pat)):
                if p.is_file() and ".claude" not in p.relative_to(self.l.root).parts:
                    st = p.stat()
                    h.update(("%s|%d|%d\n" % (p.relative_to(self.l.root), st.st_mtime_ns, st.st_size)).encode())
        return h.hexdigest()

    def _ckey(self, m, data):
        raw = json.dumps([m["id"], m["version"], data, self._fingerprint(m)], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ---- execution ----
    def run(self, aid, data=None, use_cache=True):
        cfg = self.l.config["automation"]
        data = data or {}
        reg = self.load()
        m = reg.get(aid)
        t0 = time.monotonic()
        if m is None or m.get("status") == "invalid":
            return self._env(aid, "error", t0, error="unknown or invalid automation: %s" % aid)
        status = self.effective_status(m)
        if status != "active":
            return self._env(aid, "error", t0, error="automation is %s, not active" % status)
        try:
            self.check_input(m, data)
        except LiasError as e:
            return self._env(aid, "error", t0, error="input validation: %s" % e)
        if m.get("generated"):  # verify BEFORE the cache: a tampered script must never be vouched for by old results
            want = m.get("sha256")
            have = hashlib.sha256(self._entry_path(m).read_bytes()).hexdigest()
            if not want or have != want:
                err = "integrity check failed: generated script differs from the version that was tested"
                env = self._env(aid, "error", t0, error=err)
                self._record(aid, False, env["duration_ms"], err)
                return env
        cacheable = use_cache and m.get("cache", False)
        ckey = None
        con = self.l.db()
        if cacheable:
            try:
                ckey = self._ckey(m, data)
            except LiasError as e:
                return self._env(aid, "error", t0, error="cache fingerprint: %s" % e)
            row = con.execute("SELECT result FROM cache WHERE ckey=? AND expires>?", (ckey, time.time())).fetchone()
            if row:
                env = json.loads(row[0])
                env["cached"] = True
                self._log(aid, "success", 0, cached=True)
                return env
        timeout = min(float(m["timeout_s"]), cfg["max_timeout_s"])
        cap = cfg["max_output_bytes"]
        argv = [sys.executable, "-I", str(self._entry_path(m))]
        try:
            r = run_bounded(argv, json.dumps(data).encode(), str(self.l.root),
                            {**os.environ, "LIAS_PROJECT_ROOT": str(self.l.root)}, timeout, cap)
            if r["status"] == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if r["status"] == "output_limit":
                raise RuntimeError("output exceeded %d bytes; process killed" % cap)
            if r["rc"] != 0:
                raise RuntimeError("exit %d: %s" % (r["rc"], r["err"][:500].decode("utf-8", "replace")))
            result = json.loads(r["out"].decode("utf-8", "replace"))
            env = self._env(aid, "success", t0, result=result)
            if cacheable:
                con.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?)",
                            (ckey, aid, time.time(), time.time() + cfg["cache_ttl_s"], json.dumps(env)))
                con.commit()
                self.prune_cache()
            self._record(aid, True, env["duration_ms"])
            return env
        except subprocess.TimeoutExpired:
            err = "timeout after %ss" % timeout
        except (RuntimeError, ValueError, OSError) as e:
            err = "%s: %s" % (type(e).__name__, e)
        env = self._env(aid, "error", t0, error=err)
        self._record(aid, False, env["duration_ms"], err)
        return env

    @staticmethod
    def _env(aid, status, t0, result=None, error=None):
        return {"automation_id": aid, "status": status,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "result": result, "warnings": [], "error": error, "cached": False}

    def _log(self, aid, status, ms, cached=False, error=None):
        con = self.l.db()
        con.execute("INSERT INTO runs(ts,automation_id,status,duration_ms,cached,error) VALUES(?,?,?,?,?,?)",
                    (now_iso(), aid, status, ms, int(cached), error))
        con.commit()

    def _record(self, aid, ok, ms, error=None):
        """Log run; quarantine after N consecutive failures."""
        self._log(aid, "success" if ok else "error", ms, error=error)
        con = self.l.db()
        st = self.state(aid)
        fails = 0 if ok else st["consecutive_failures"] + 1
        status = st["status"]
        if fails >= self.l.config["automation"]["max_consecutive_failures"]:
            status = "quarantined"
        con.execute("INSERT OR REPLACE INTO automation_state VALUES(?,?,?,?)",
                    (aid, status, fails, error if not ok else None))
        con.commit()

    # ---- cache maintenance ----
    def prune_cache(self):
        """Drop expired entries, then oldest beyond max_cache_entries. Returns rows removed."""
        con = self.l.db()
        n = con.execute("DELETE FROM cache WHERE expires<=?", (time.time(),)).rowcount
        cap = self.l.config["automation"]["max_cache_entries"]
        n += con.execute("DELETE FROM cache WHERE ckey IN (SELECT ckey FROM cache ORDER BY created DESC "
                         "LIMIT -1 OFFSET ?)", (cap,)).rowcount
        con.commit()
        return n

    def cache_stats(self):
        con = self.l.db()
        runs = con.execute("SELECT COUNT(*), COALESCE(SUM(cached),0) FROM runs WHERE status='success'").fetchone()
        return {"entries": con.execute("SELECT COUNT(*) FROM cache").fetchone()[0],
                "successful_runs": runs[0], "cache_hits": runs[1],
                "hit_rate": round(runs[1] / runs[0], 3) if runs[0] else None}

    # ---- output compaction ----
    def compact(self, env, budget=None):
        """Return env unchanged if small; else keep scalars + shapes and spill the full result to logs/."""
        budget = budget or self.l.config["automation"]["compact_chars"]
        full = json.dumps(env, ensure_ascii=False)
        if len(full) <= budget:
            return env
        logs = self.l.dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / ("run-%s-%d.json" % (env["automation_id"], int(time.time() * 1000)))
        path.write_text(full, encoding="utf-8")

        def shrink(v, depth=0):
            if isinstance(v, dict):
                if depth >= 2:
                    return "<object, %d keys>" % len(v)
                return {k: shrink(x, depth + 1) for k, x in list(v.items())[:20]}
            if isinstance(v, list):
                return "<list, %d items>" % len(v)
            if isinstance(v, str) and len(v) > 200:
                return v[:200] + "…"
            return v
        small = dict(env, result=shrink(env.get("result")), truncated=True,
                     full_output=str(path.relative_to(self.l.root)), full_chars=len(full))
        return small

    def purge_logs(self):
        days = self.l.config["logs"]["retention_days"]
        cutoff = time.time() - days * 86400
        n = 0
        logs = self.l.dir / "logs"
        if logs.is_dir():
            for p in logs.glob("run-*.json"):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
        return n

    def reinstate(self, aid):
        con = self.l.db()
        con.execute("DELETE FROM automation_state WHERE automation_id=?", (aid,))
        con.commit()
