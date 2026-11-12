"""LIAS autonomous automation growth.

Lifecycle: OBSERVE -> EVALUATE -> (script authored by Claude) -> STATIC VALIDATION -> ISOLATED TEST
           -> SAFETY REVIEW -> REGISTER (inactive) -> ACTIVATE -> MONITOR (runner quarantine).
LIAS validates, tests and registers; it does not author code. Static validation is a safety net, and the
isolated test is a temp-dir subprocess, NOT an OS sandbox.
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from common import LiasError, atomic_write, run_bounded
from knowledge import now_iso
from learning import jaccard, overlap


def similar(a, b, thr):
    return jaccard(a, b) >= thr or overlap(a, b) >= thr
from automation import Automations

ALLOWED_IMPORTS = {
    "json", "os", "sys", "re", "hashlib", "collections", "itertools", "pathlib", "datetime", "time",
    "math", "fnmatch", "statistics", "csv", "io", "typing", "functools", "string", "textwrap",
    "difflib", "glob", "zlib", "ast", "tokenize", "configparser", "base64", "operator", "enum",
}
BANNED_NAMES = {"eval", "exec", "compile", "__import__", "globals", "locals", "getattr", "setattr",
                "delattr", "vars", "breakpoint", "input", "memoryview"}
BANNED_ATTRS = {
    "system", "popen", "remove", "unlink", "rmdir", "rename", "renames", "replace", "removedirs", "kill",
    "startfile", "putenv", "unsetenv", "chmod", "chown", "truncate", "fork", "symlink", "link",
    "write_text", "write_bytes", "touch", "mkdir", "makedirs", "symlink_to", "hardlink_to", "rmtree",
    "environ_clear", "chdir", "walk_up",
}
WRITE_MODE = set("wax+")


def static_validate(source, risk):
    """Return list of violation strings (empty = pass)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ["syntax error: %s" % e]
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    bad.append("line %d: import %s not allowed" % (n.lineno, a.name))
        elif isinstance(n, ast.ImportFrom):
            if n.level or (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                bad.append("line %d: from-import %s not allowed" % (n.lineno, n.module))
        elif isinstance(n, ast.Name) and n.id in BANNED_NAMES:
            bad.append("line %d: use of %s is banned" % (n.lineno, n.id))
        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("__") and n.attr.endswith("__"):
                bad.append("line %d: dunder attribute %s is banned" % (n.lineno, n.attr))
            elif n.attr in BANNED_ATTRS and risk == 0:
                bad.append("line %d: .%s() is banned for read-only automations" % (n.lineno, n.attr))
            elif n.attr in ("open", "fdopen", "write", "writev", "pwrite", "sendfile", "copy_file_range") \
                    and isinstance(n.value, ast.Name) and n.value.id == "os":
                bad.append("line %d: os.%s banned (raw file access)" % (n.lineno, n.attr))
            elif n.attr.startswith(("spawn", "exec")) and isinstance(n.value, ast.Name) and n.value.id == "os":
                bad.append("line %d: os.%s banned" % (n.lineno, n.attr))
        if isinstance(n, ast.Call):
            f = n.func
            is_open = (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open")
            if is_open:
                mode = None
                if len(n.args) >= 2:
                    mode = n.args[1]
                for kw in n.keywords:
                    if kw.arg == "mode":
                        mode = kw.value
                if isinstance(f, ast.Attribute) and f.attr == "open" and len(n.args) >= 1 and mode is None:
                    mode = n.args[0]  # Path.open(mode)
                if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                    bad.append("line %d: open() mode must be a literal string" % n.lineno)
                elif mode is not None and (WRITE_MODE & set(mode.value)) and risk == 0:
                    bad.append("line %d: write mode %r banned for read-only automations" % (n.lineno, mode.value))
    return bad


def _snapshot(root):
    snap = {}
    for dp, _, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, root)] = hashlib.sha1(fh.read()).hexdigest()
    return snap


def _subset(expect, actual):
    if isinstance(expect, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expect.items())
    return expect == actual


class Growth:
    def __init__(self, lias):
        self.l = lias
        self.cfg = lias.config["promotion"]
        self.au = Automations(lias)
        self.gdir = lias.dir / "automation" / "generated"
        self.mdir = lias.dir / "automation" / "manifests"

    # ---- OBSERVE / EVALUATE ----
    def observe(self, signature, description):
        sig = "".join(c if c.isalnum() else "-" for c in signature.lower()).strip("-")[:60]
        if not sig:
            raise LiasError("empty signature")
        con = self.l.db()
        ts = now_iso()
        con.execute("INSERT INTO observations VALUES(?,?,1,?,?) ON CONFLICT(signature) DO UPDATE SET "
                    "count=count+1, last_seen=excluded.last_seen", (sig, description[:200], ts, ts))
        con.commit()
        return dict(con.execute("SELECT * FROM observations WHERE signature=?", (sig,)).fetchone())

    def candidates(self):
        """Repeated workflows worth automating (not already covered by a registered automation)."""
        reg = [m for m in self.au.load().values() if m.get("status") != "invalid"]
        out = []
        for r in self.l.db().execute("SELECT * FROM observations ORDER BY count DESC"):
            if r["count"] < self.cfg["min_repeats"]:
                continue
            covered = [m["id"] for m in reg if m["id"] == r["signature"]
                       or similar(m["description"], r["description"], self.cfg["duplicate_jaccard"])]
            net = r["count"] * self.cfg["est_tokens_per_use"] - self.cfg["build_cost_tokens"]
            out.append({"signature": r["signature"], "description": r["description"], "observed": r["count"],
                        "covered_by": covered, "est_net_tokens": net,
                        "eligible": not covered and net >= 0,
                        "note": "ASSUMPTION-BASED estimate (config est_tokens_per_use/build_cost_tokens), not measured"})
        return out

    # ---- pipeline ----
    def _budget_check(self):
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        n = self.l.db().execute("SELECT COUNT(*) FROM audit WHERE action='growth-attempt' AND ts>?", (since,)).fetchone()[0]
        if n >= self.cfg["max_attempts_per_day"]:
            raise LiasError("generation budget exhausted (%d attempts in 24h)" % n)
        if self.gdir.is_dir() and len(list(self.gdir.glob("*.py"))) >= self.cfg["max_generated"]:
            raise LiasError("generated automation limit reached (%d)" % self.cfg["max_generated"])

    def _audit(self, action, aid, detail=""):
        self.l.db().execute("INSERT INTO audit VALUES(?,?,?,?)", (now_iso(), action, aid, detail))
        self.l.db().commit()

    def _isolated_test(self, script_path, case, risk):
        tmp = tempfile.mkdtemp(prefix="lias_iso_")
        try:
            for rel, content in (case.get("files") or {}).items():
                p = os.path.abspath(os.path.join(tmp, rel))
                if not p.startswith(os.path.abspath(tmp) + os.sep):
                    return "fixture path escapes sandbox: %s" % rel
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="") as fh:
                    fh.write(content)
            before = _snapshot(tmp)
            env = {k: os.environ[k] for k in ("SystemRoot", "PATH", "TEMP", "TMP") if k in os.environ}
            env["LIAS_PROJECT_ROOT"] = tmp
            cp = run_bounded([sys.executable, "-I", str(script_path)], json.dumps(case.get("input", {})).encode(),
                             tmp, env, self.cfg["test_timeout_s"], self.l.config["automation"]["max_output_bytes"])
            if cp["status"] == "timeout":
                return "timeout"
            if cp["status"] == "output_limit":
                return "output limit exceeded"
            if cp["rc"] != 0:
                return "exit %d: %s" % (cp["rc"], cp["err"][:300].decode("utf-8", "replace"))
            try:
                result = json.loads(cp["out"].decode("utf-8"))
            except ValueError:
                return "stdout is not valid JSON"
            if risk == 0 and _snapshot(tmp) != before:
                return "read-only automation modified its working directory"
            if not _subset(case.get("expect", {}), result):
                return "output mismatch: expected subset %s, got %s" % (json.dumps(case.get("expect")), json.dumps(result)[:300])
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def propose(self, manifest, source, tests, approved_by_user=False):
        """Run the whole lifecycle. Returns a stage report; installs+activates only if every stage passes."""
        report = {"id": manifest.get("id"), "stages": [], "activated": False}

        def stage(name, ok, detail=""):
            report["stages"].append({"stage": name, "ok": ok, "detail": detail})
            return ok

        self._budget_check()
        aid = manifest.get("id", "")
        self._audit("growth-attempt", aid)
        if not aid.replace("-", "").replace("_", "").isalnum():
            stage("evaluate", False, "id must be alphanumeric/-/_"); return report
        risk = manifest.get("risk", 0)
        if risk not in (0, 1):
            stage("evaluate", False, "risk tier 2 is never auto-generated"); return report
        if risk == 1 and not approved_by_user:
            stage("evaluate", False, "risk 1 (writes) requires approved_by_user=True"); return report
        if not tests:
            stage("evaluate", False, "at least one test case is required"); return report
        existing = self.au.load()
        old = existing.get(aid)
        if old is None:
            dup = [m["id"] for m in existing.values() if m.get("status") != "invalid"
                   and similar(m.get("description", ""), manifest.get("description", ""), self.cfg["duplicate_jaccard"])]
            if dup:
                stage("evaluate", False, "near-duplicate of %s: extend that automation instead" % dup); return report
        elif old.get("version") == manifest.get("version"):
            stage("evaluate", False, "id exists at version %s; bump version" % old["version"]); return report
        stage("evaluate", True, "new" if old is None else "new version of %s" % old["version"])

        viol = static_validate(source, risk)
        if not stage("static-validation", not viol, "; ".join(viol[:5])):
            return report

        stagedir = tempfile.mkdtemp(prefix="lias_stage_")
        try:
            sp = os.path.join(stagedir, aid + ".py")
            with open(sp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(source)
            for i, case in enumerate(tests):
                err = self._isolated_test(sp, case, risk)
                if not stage("isolated-test:%s" % case.get("name", i), err is None, err or ""):
                    return report
        finally:
            shutil.rmtree(stagedir, ignore_errors=True)
        stage("safety-review", True, "tier %d, imports within allowlist, no banned calls" % risk)

        # REGISTER (inactive) -> ACTIVATE, with restore on failure
        self.gdir.mkdir(parents=True, exist_ok=True)
        spath, mpath = self.gdir / (aid + ".py"), self.mdir / (aid + ".json")
        backup = None
        if old is not None and spath.exists():
            vdir = self.gdir / "_versions" / aid / str(old["version"])
            vdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spath, vdir / (aid + ".py"))
            shutil.copy2(mpath, vdir / (aid + ".json"))
            backup = vdir
        m = {k: manifest[k] for k in ("id", "name", "description", "input_schema", "timeout_s", "risk",
                                       "version", "cache", "fingerprint_globs") if k in manifest}
        m.update(entry="generated/%s.py" % aid, status="inactive", generated=True, tests=len(tests))
        m.setdefault("input_schema", {}); m.setdefault("timeout_s", 30)
        try:
            atomic_write(spath, source)
            m["sha256"] = hashlib.sha256(spath.read_bytes()).hexdigest()  # tamper check at every run
            atomic_write(mpath, json.dumps(m, indent=2) + "\n")
            self.au.validate_manifest(m)
            stage("register", True, "inactive")
            if static_validate(spath.read_text(encoding="utf-8"), risk):
                raise LiasError("installed script differs from the validated one")
            m["status"] = "active"
            atomic_write(mpath, json.dumps(m, indent=2) + "\n")
            self.au.reinstate(aid)  # clear any quarantine state from a prior version
            stage("activate", True, "version %s" % m["version"])
            report["activated"] = True
            self._audit("growth-activate", aid, m["version"])
        except Exception as e:
            stage("install", False, "%s: %s" % (type(e).__name__, e))
            self._restore(aid, backup)
        return report

    def _restore(self, aid, backup):
        spath, mpath = self.gdir / (aid + ".py"), self.mdir / (aid + ".json")
        if backup is not None:
            shutil.copy2(backup / (aid + ".py"), spath)
            shutil.copy2(backup / (aid + ".json"), mpath)
        else:
            for p in (spath, mpath):
                if p.exists():
                    p.unlink()

    def rollback(self, aid):
        vroot = self.gdir / "_versions" / aid
        vers = sorted(vroot.iterdir(), key=lambda p: p.stat().st_mtime) if vroot.is_dir() else []
        if not vers:
            raise LiasError("no previous version of %s" % aid)
        self._restore(aid, vers[-1])
        self.au.reinstate(aid)
        self._audit("growth-rollback", aid, vers[-1].name)
        return {"rolled_back_to": vers[-1].name}

    def history(self, limit=20):
        return [dict(r) for r in self.l.db().execute(
            "SELECT * FROM audit WHERE action LIKE 'growth-%' ORDER BY ts DESC LIMIT ?", (limit,))]
