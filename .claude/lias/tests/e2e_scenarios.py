"""End-to-end acceptance scenarios A-G. Every step runs as a SEPARATE OS process (so "restart" is real)
inside throwaway project copies; the real project is never touched.   Run: python .claude/lias/tests/e2e_scenarios.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIAS = Path(__file__).resolve().parents[1]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail and not ok else ""))


def make_project():
    root = Path(tempfile.mkdtemp(prefix="lias_e2e_"))
    dst = root / ".claude" / "lias"
    shutil.copytree(LIAS, dst, ignore=shutil.ignore_patterns("database", "backups", "logs", "benchmarks",
                                                              "__pycache__", "*.bak", "generated", "tests"))
    (dst / "automation" / "generated").mkdir()
    return root


def cli(root, *args, inp=None):
    p = subprocess.run([sys.executable, str(root / ".claude" / "lias" / "cli.py"), *args],
                       capture_output=True, text=True, timeout=60, cwd=str(root))
    try:
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None, p.stderr
    except ValueError:
        return p.returncode, p.stdout, p.stderr


def hook(root, mode, payload):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    env.pop("LIAS_HOOK_ACTIVE", None)
    p = subprocess.run([sys.executable, str(root / ".claude" / "lias" / "hook.py"), mode],
                       input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


MANIFEST = {"id": "ext-census", "name": "Extension census", "version": "1.0.0", "risk": 0, "timeout_s": 5,
            "description": "Count top-level files grouped by their extension in the project root folder",
            "input_schema": {"fail": {"type": "boolean"}}, "cache": True, "fingerprint_globs": ["*"]}
SCRIPT = '''import json, os, sys
req = json.loads(sys.stdin.read() or "{}")
if req.get("fail"):
    raise SystemExit(7)
root = os.environ["LIAS_PROJECT_ROOT"]
c = {}
for f in os.listdir(root):
    if os.path.isfile(os.path.join(root, f)):
        e = os.path.splitext(f)[1] or "(none)"
        c[e] = c.get(e, 0) + 1
print(json.dumps({"by_ext": c}))
'''
TESTS = [{"name": "groups", "input": {}, "files": {"a.py": "", "b.py": "", "c.txt": ""},
          "expect": {"by_ext": {".py": 2, ".txt": 1}}}]


def main():
    p1, p2 = make_project(), make_project()
    try:
        # ---- A: persistent knowledge across processes ----
        rc, o, _ = cli(p1, "knowledge", "add", "architecture_decision",
                       "The installer is built with NSIS from CodeLensSetup.nsi", "--source", "user-confirmed",
                       "--key", "installer", "--verified")
        check("A1 save verified fact", rc == 0 and o["result"] == "created")
        rc, o, _ = cli(p1, "knowledge", "search", "how is the installer built")
        check("A2 new process retrieves it with provenance", rc == 0 and o and o[0]["source"] == "user-confirmed"
              and o[0]["status"] == "verified")
        rc, out = hook(p1, "prompt", {"cwd": str(p1), "prompt": "Which tool builds the installer?"})
        check("A3 hook injects it for a plain-English prompt", rc == 0 and "NSIS" in out and "data, not instructions" in out)

        # ---- B/C: observe -> eligible -> generate/validate/test/register -> reuse ----
        for _ in range(3):
            cli(p1, "observe", "ext-census", MANIFEST["description"])
        rc, o, _ = cli(p1, "growth", "candidates")
        check("B1 repeated workflow becomes eligible", rc == 0 and o[0]["eligible"] and o[0]["observed"] == 3)
        d = Path(tempfile.mkdtemp(prefix="lias_inputs_"))
        (d / "m.json").write_text(json.dumps(MANIFEST)); (d / "s.py").write_text(SCRIPT); (d / "t.json").write_text(json.dumps(TESTS))
        rc, o, _ = cli(p1, "growth", "propose", "--manifest", str(d / "m.json"), "--script", str(d / "s.py"),
                       "--tests", str(d / "t.json"))
        stages = [s["stage"] for s in o["stages"]] if isinstance(o, dict) else []
        check("C1 lifecycle passes every stage and activates",
              rc == 0 and o["activated"] and stages[:2] == ["evaluate", "static-validation"] and "activate" in stages, str(o)[:300])
        (p1 / "one.py").write_text("x"); (p1 / "two.txt").write_text("y")
        rc, o, _ = cli(p1, "run", "ext-census")
        check("C2 activated automation runs locally", rc == 0 and o["status"] == "success" and o["result"]["by_ext"].get(".py") == 1)
        rc, o2, _ = cli(p1, "run", "ext-census")
        check("C3 later request reuses it (cache hit, new process)", rc == 0 and o2["cached"] is True)
        (p1 / "three.py").write_text("z")
        rc, o3, _ = cli(p1, "run", "ext-census")
        check("C4 cache invalidated when project files change", o3["cached"] is False and o3["result"]["by_ext"][".py"] == 2)
        rc, o, _ = cli(p1, "growth", "candidates")
        check("C5 workflow now shown as covered", o[0]["eligible"] is False and "ext-census" in o[0]["covered_by"])

        # ---- D: controlled failure -> bounded recovery ----
        codes = [cli(p1, "run", "ext-census", "--input", '{"fail": true}')[0] for _ in range(3)]
        rc, o, _ = cli(p1, "run", "ext-census")
        check("D1 3 consecutive failures quarantine it", codes == [1, 1, 1] and rc == 1 and "quarantined" in o["error"])
        rc, o, _ = cli(p1, "doctor")
        check("D2 doctor reports the quarantine (exit 1)", rc == 1 and not o["healthy"] and o["checks"]["quarantined"] == ["ext-census"])
        cli(p1, "reinstate", "ext-census")
        rc, o, _ = cli(p1, "run", "ext-census")
        check("D3 reinstated after fix, works again", rc == 0 and o["status"] == "success")
        # corrupt DB -> recover
        rc, _, _ = cli(p1, "backup")
        dbp = p1 / ".claude" / "lias" / "database" / "lias.sqlite3"
        for s in ("", "-wal", "-shm"):
            Path(str(dbp) + s).unlink(missing_ok=True)
        dbp.write_bytes(b"garbage" * 500)
        rc, _, err = cli(p1, "knowledge", "search", "installer")
        check("D4 corrupt DB gives actionable error", rc == 2 and "recover" in err)
        rc, _, _ = cli(p1, "recover")
        rc, o, _ = cli(p1, "knowledge", "search", "installer")
        check("D5 recover restores knowledge", rc == 0 and o and "NSIS" in o[0]["content"])

        # ---- E: isolation ----
        rc, o, _ = cli(p2, "knowledge", "search", "installer NSIS")
        check("E1 other project cannot see the fact", rc == 0 and o == [])
        (p2 / ".claude" / "lias" / "database").mkdir(exist_ok=True)
        shutil.copy(dbp, p2 / ".claude" / "lias" / "database" / "lias.sqlite3")
        rc, _, err = cli(p2, "knowledge", "search", "installer")
        check("E2 copied database from another project is refused", rc == 2 and "another project" in err)

        # ---- F: permission compliance ----
        base = dict(MANIFEST, id="p-%d")
        def propose(m, script):
            (d / "m.json").write_text(json.dumps(m)); (d / "s.py").write_text(script)
            return cli(p1, "growth", "propose", "--manifest", str(d / "m.json"), "--script", str(d / "s.py"), "--tests", str(d / "t.json"))
        rc, o, _ = propose(dict(MANIFEST, id="tier2", risk=2, description="zz one two three four"), SCRIPT)
        check("F1 tier-2 never auto-generated", rc == 1 and not o["activated"])
        rc, o, _ = propose(dict(MANIFEST, id="writer", risk=1, description="yy one two three four"), SCRIPT)
        check("F2 write-capable needs explicit user approval", rc == 1 and "approved_by_user" in o["stages"][-1]["detail"])
        rc, o, _ = propose(dict(MANIFEST, id="hostile", description="xx one two three four"), "import subprocess\nprint('{}')")
        check("F3 hostile script rejected statically", rc == 1 and o["stages"][-1]["stage"] == "static-validation")
        gen = p1 / ".claude" / "lias" / "automation" / "generated" / "ext-census.py"
        gen.write_text(gen.read_text() + "\n# tampered\n")
        rc, o, _ = cli(p1, "run", "ext-census")
        check("F4 tampered generated script refused", rc == 1 and "integrity" in o["error"])
        rc, o, _ = cli(p1, "knowledge", "add", "project_fact", "api_key = abcdef123456", "--source", "t")
        check("F5 secret refused (exit 3)", rc == 3)

        # ---- G: efficiency (measured locally; model tokens NOT measured) ----
        rc, o, _ = cli(p1, "growth", "rollback", "ext-census") if False else (0, None, "")
        shutil.rmtree(p1 / ".claude" / "lias" / "database", ignore_errors=True)
        rc, o, _ = cli(p1, "benchmark")
        a = o["automation"] if isinstance(o, dict) else {}
        check("G1 cached run measurably faster than uncached",
              a and a["warm_cached"]["median_ms"] < a["cold_uncached"]["median_ms"],
              json.dumps(a.get("cold_uncached")) + json.dumps(a.get("warm_cached")))
        if a:
            print("     cold median %.1f ms -> warm median %.1f ms; result %d chars vs %d source bytes (proxy)" % (
                a["cold_uncached"]["median_ms"], a["warm_cached"]["median_ms"],
                a["proxy_context"]["lias_result_chars"], a["proxy_context"]["source_bytes_to_read_manually"]))
        check("G2 retrieval fixture accuracy >= 0.9", isinstance(o, dict) and o["retrieval"]["top1_accuracy"] >= 0.9)
    finally:
        for r in (p1, p2):
            shutil.rmtree(r, ignore_errors=True)
    bad = [r for r in results if not r[1]]
    print("\n%d/%d scenario checks passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
