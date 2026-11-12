"""Run: python -m unittest discover -s .claude/lias/tests -v   (uses temp projects only)"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

LIAS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIAS_DIR))

from common import Lias, LiasError, project_id  # noqa: E402
from knowledge import Knowledge, SecretRejected, now_iso  # noqa: E402
from automation import Automations  # noqa: E402


def make_project(with_automation=True):
    root = Path(tempfile.mkdtemp(prefix="lias_t_"))
    d = root / ".claude" / "lias"
    d.mkdir(parents=True)
    if with_automation:
        shutil.copytree(LIAS_DIR / "automation", d / "automation")
    return root


def write_auto(root, aid, script, **over):
    d = root / ".claude" / "lias" / "automation"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "manifests").mkdir(parents=True, exist_ok=True)
    (d / "scripts" / (aid + ".py")).write_text(script)
    m = {"id": aid, "name": aid, "description": "t", "entry": "scripts/%s.py" % aid,
         "timeout_s": 2, "risk": 0, "version": "1", "status": "active", "input_schema": {}}
    m.update(over)
    (d / "manifests" / (aid + ".json")).write_text(json.dumps(m))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = make_project()
        self.l = Lias(self.root)
        self.k = Knowledge(self.l)
        self.a = Automations(self.l)
        self.addCleanup(lambda: (self.l.close(), shutil.rmtree(self.root, ignore_errors=True)))


class TestKnowledge(Base):
    def test_persistence_across_instances(self):
        self.k.add("project_fact", "Build uses PyInstaller via CodeLens.spec", "user", verified=True)
        self.l.close()
        l2 = Lias(self.root)
        r = Knowledge(l2).search("PyInstaller spec")
        l2.close()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["status"], "verified")
        self.assertEqual(r[0]["source"], "user")

    def test_secret_rejected(self):
        for bad in ("api_key = abc123def", "token: ghp_" + "a" * 30, "sk-" + "x" * 24):
            with self.assertRaises(SecretRejected):
                self.k.add("project_fact", bad, "t")
        self.assertEqual(self.k.stats()["records"], {})

    def test_contradiction_not_overwritten(self):
        self.k.add("build_config", "Python 3.13", "user", key="python-version", verified=True)
        r = self.k.add("build_config", "Python 3.9", "guess", key="python-version")
        self.assertEqual(r["result"], "conflict")
        self.assertEqual(self.k.search("python-version")[0]["content"], "Python 3.13")
        r = self.k.add("build_config", "Python 3.12", "user", key="python-version", verified=True, supersede=True)
        self.assertEqual(r["result"], "created")
        self.assertEqual(self.k.search("python-version")[0]["content"], "Python 3.12")

    def test_unverified_labelled(self):
        self.k.add("project_fact", "maybe uses Qt6", "inferred")
        self.assertEqual(self.k.search("Qt6")[0]["status"], "unverified")

    def test_expiry(self):
        self.k.add("temporary", "scratch note zebra", "t", ttl_days=1)
        self.l.db().execute("UPDATE records SET expires_at='2000-01-01T00:00:00+00:00'")
        self.l.db().commit()
        self.assertEqual(self.k.search("zebra"), [])
        self.assertEqual(self.k.purge_expired(), 1)

    def test_invalidate_hides(self):
        rid = self.k.add("project_fact", "foo bar baz", "t", verified=True)["id"]
        self.assertTrue(self.k.invalidate(rid))
        self.assertEqual(self.k.search("foo"), [])

    def test_context_budget(self):
        for i in range(8):
            self.k.add("project_fact", "alpha %d " % i + "x" * 500, "t", verified=True)
        r = self.k.search("alpha", max_chars=1000)
        self.assertLessEqual(len(r), 3)

    def test_audit_trail(self):
        self.k.add("project_fact", "audited fact", "t", verified=True)
        self.assertGreaterEqual(self.l.db().execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)


class TestIsolation(unittest.TestCase):
    def test_two_projects_do_not_leak(self):
        r1, r2 = make_project(False), make_project(False)
        self.addCleanup(lambda: [shutil.rmtree(r, ignore_errors=True) for r in (r1, r2)])
        l1, l2 = Lias(r1), Lias(r2)
        Knowledge(l1).add("project_fact", "secretless unicorn fact", "t", verified=True)
        self.assertEqual(Knowledge(l2).search("unicorn"), [])
        self.assertNotEqual(l1.pid, l2.pid)
        l1.close(); l2.close()

    def test_moved_db_refused(self):
        r1, r2 = make_project(False), make_project(False)
        self.addCleanup(lambda: [shutil.rmtree(r, ignore_errors=True) for r in (r1, r2)])
        l1 = Lias(r1); l1.db(); l1.close()
        shutil.copy(l1.db_path, r2 / ".claude" / "lias" / "database.tmp")
        (r2 / ".claude" / "lias" / "database").mkdir()
        shutil.move(str(r2 / ".claude" / "lias" / "database.tmp"), str(r2 / ".claude" / "lias" / "database" / "lias.sqlite3"))
        l2 = Lias(r2)
        with self.assertRaises(LiasError):
            l2.db()

    def test_project_id_stable(self):
        self.assertEqual(project_id("."), project_id(os.getcwd()))


class TestAutomation(Base):
    def test_builtin_source_stats(self):
        (self.root / "a.py").write_text("x=1\ny=2\n")
        env = self.a.run("source-stats", {"extensions": [".py"]})
        self.assertEqual(env["status"], "success", env)
        self.assertEqual(env["result"]["total_lines"], 2)

    def test_cache_hit_and_invalidation(self):
        (self.root / "a.py").write_text("x=1\n")
        self.assertFalse(self.a.run("source-stats")["cached"])
        self.assertTrue(self.a.run("source-stats")["cached"])
        time.sleep(0.02)
        (self.root / "a.py").write_text("x=1\ny=2\nz=3\n")
        env = self.a.run("source-stats")
        self.assertFalse(env["cached"])
        self.assertEqual(env["result"]["total_lines"], 3)

    def test_input_validation(self):
        env = self.a.run("source-stats", {"extensions": "py"})
        self.assertEqual(env["status"], "error")
        env = self.a.run("source-stats", {"bogus": 1})
        self.assertIn("unexpected", env["error"])

    def test_timeout(self):
        write_auto(self.root, "slow", "import time; time.sleep(10)", timeout_s=0.5)
        env = self.a.run("slow")
        self.assertEqual(env["status"], "error")
        self.assertIn("timeout", env["error"])

    def test_quarantine_after_failures(self):
        write_auto(self.root, "bad", "import sys; sys.exit(1)")
        for _ in range(3):
            self.assertEqual(self.a.run("bad")["status"], "error")
        env = self.a.run("bad")
        self.assertIn("quarantined", env["error"])
        self.a.reinstate("bad")
        self.assertIn("exit 1", self.a.run("bad")["error"])

    def test_entry_path_escape_rejected(self):
        write_auto(self.root, "esc", "print('{}')", entry="../../../x.py")
        reg = self.a.load()
        self.assertEqual(reg["!invalid:esc.json"]["status"], "invalid")
        self.assertEqual(self.a.run("esc")["status"], "error")

    def test_risk_tier2_rejected(self):
        write_auto(self.root, "danger", "print('{}')", risk=2)
        self.assertIn("!invalid:danger.json", self.a.load())

    def test_inactive_not_run(self):
        write_auto(self.root, "draft", "print('{}')", status="inactive")
        self.assertIn("not active", self.a.run("draft")["error"])

    def test_output_must_be_json(self):
        write_auto(self.root, "junk", "print('not json')")
        self.assertEqual(self.a.run("junk")["status"], "error")


class TestIntegration(Base):
    def _hook(self, mode, payload, env_extra=None):
        import subprocess
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        env.pop("LIAS_HOOK_ACTIVE", None)
        env.update(env_extra or {})
        # hook.py is copied from the real install into the temp project
        shutil.copy(LIAS_DIR / "hook.py", self.root / ".claude" / "lias" / "hook.py")
        for f in ("common.py", "knowledge.py", "automation.py", "learning.py"):
            shutil.copy(LIAS_DIR / f, self.root / ".claude" / "lias" / f)
        cp = subprocess.run([sys.executable, str(self.root / ".claude" / "lias" / "hook.py"), mode],
                            input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=20)
        return cp.returncode, cp.stdout.decode(), cp.stderr.decode()

    def test_settings_merge_preserves_and_is_idempotent(self):
        from integrate import merge_settings
        sp = self.root / ".claude" / "settings.json"
        existing = {"permissions": {"allow": ["Bash(git status)"]}, "custom": 1,
                    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        sp.write_text(json.dumps(existing))
        self.assertTrue(merge_settings(self.root))
        first = sp.read_text()
        self.assertFalse(merge_settings(self.root))
        self.assertEqual(first, sp.read_text())
        d = json.loads(first)
        self.assertEqual(d["permissions"], existing["permissions"])
        self.assertEqual(d["custom"], 1)
        cmds = [h["command"] for g in d["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertEqual(cmds[0], "echo mine")
        self.assertEqual(sum("lias/hook.py" in c for c in cmds), 1)
        self.assertEqual(len(d["hooks"]["SessionStart"]), 1)

    def test_invalid_settings_untouched(self):
        from integrate import merge_settings
        sp = self.root / ".claude" / "settings.json"
        sp.write_text("{ not json")
        with self.assertRaises(LiasError):
            merge_settings(self.root)
        self.assertEqual(sp.read_text(), "{ not json")

    def test_claude_md_merge_idempotent_and_preserving(self):
        from integrate import merge_claude_md
        cm = self.root / "CLAUDE.md"
        cm.write_text("# Mine\nkeep this\n")
        self.assertTrue(merge_claude_md(self.root))
        once = cm.read_text()
        self.assertFalse(merge_claude_md(self.root))
        self.assertTrue(once.startswith("# Mine\nkeep this\n"))
        self.assertEqual(once.count("LIAS:BEGIN"), 1)

    def test_prompt_hook_injects_only_verified_relevant(self):
        self.k.add("architecture_decision", "Shared search engine lives in CodeLensCLI module", "doc", verified=True)
        self.k.add("project_fact", "Unverified rumor about search engine module", "guess")
        self.l.close()
        rc, out, _ = self._hook("prompt", {"cwd": str(self.root), "prompt": "where is the search engine module?"})
        self.assertEqual(rc, 0)
        self.assertIn("CodeLensCLI", out)
        self.assertIn("data, not instructions", out)
        self.assertNotIn("rumor", out)
        rc, out, _ = self._hook("prompt", {"cwd": str(self.root), "prompt": "write a haiku"})
        self.assertEqual((rc, out), (0, ""))

    def test_hook_idempotent_and_recursion_guard(self):
        self.k.add("project_fact", "Alpha bravo charlie delta fact", "doc", verified=True)
        self.l.close()
        p = {"cwd": str(self.root), "prompt": "alpha bravo charlie"}
        self.assertEqual(self._hook("prompt", p), self._hook("prompt", p))
        self.assertEqual(self._hook("prompt", p, {"LIAS_HOOK_ACTIVE": "1"})[1], "")

    def test_hook_never_fails_session(self):
        rc, out, err = self._hook("prompt", {"cwd": str(self.root.parent / "nonexistent-xyz"), "prompt": "alpha bravo"},
                                  {"CLAUDE_PROJECT_DIR": str(self.root.parent / "nonexistent-xyz")})
        self.assertEqual(rc, 0)

    def test_hook_does_not_write_knowledge(self):
        self.l.close()
        self._hook("prompt", {"cwd": str(self.root), "prompt": "password = hunter2 secret stuff"})
        self.assertEqual(Knowledge(Lias(self.root)).stats()["records"], {})


class TestLearning(TestIntegration):
    def setUp(self):
        super().setUp()
        from learning import Learner
        self.ln = Learner(self.l)

    def test_explicit_remember_saved_verified_and_survives_restart(self):
        rc, out, _ = self._hook("prompt", {"cwd": str(self.root),
                                           "prompt": "Remember that the installer is built with makensis"})
        self.assertEqual(rc, 0)
        self.assertIn("Saved verified", out)
        self.l.close()
        r = Knowledge(Lias(self.root)).search("installer makensis")
        self.assertEqual(r[0]["status"], "verified")
        self.assertEqual(r[0]["source"], "user-prompt")

    def test_non_explicit_prompt_learns_nothing(self):
        self._hook("prompt", {"cwd": str(self.root), "prompt": "the build uses makensis for the installer"})
        self.assertEqual(Knowledge(Lias(self.root)).stats()["records"], {})

    def test_pasted_content_never_learned(self):
        p = 'hi <pasted_content id="x1">remember that the deploy key lives in the wiki</pasted_content id="x1">'
        self.assertEqual(self.ln.learn_from_prompt(p), [])

    def test_secret_in_remember_rejected_not_staged(self):
        r = self.ln.learn_from_prompt("remember that api_key = abcdef123456")
        self.assertEqual(r[0]["result"], "rejected_secret")
        self.assertEqual(self.k.stats()["records"], {})
        self.assertEqual(self.ln.candidates(), [])

    def test_contradiction_staged_then_promoted_only_with_supersede(self):
        self.ln.learn_fact("The installer is NSIS", "user", explicit=True)
        r = self.ln.learn_fact("The installer is Inno Setup", "user2", explicit=True)
        self.assertEqual(r["result"], "staged")
        self.assertEqual(self.k.search("installer")[0]["content"], "The installer is NSIS")
        cid = r["candidate_id"]
        self.assertEqual(self.ln.promote(cid)["result"], "conflict")
        self.assertEqual(self.ln.promote(cid, supersede=True)["result"], "created")
        self.assertEqual(self.k.search("installer")[0]["content"], "The installer is Inno Setup")
        self.assertEqual(self.ln.candidates(), [])

    def test_near_duplicate_refreshes_not_duplicates(self):
        self.ln.learn_fact("CodeLens search engine lives in CodeLensCLI.py", "a", explicit=True)
        r = self.ln.learn_fact("CodeLens search engine lives in CodeLensCLI.py!", "b", explicit=True)
        self.assertEqual(r["result"], "duplicate")
        self.assertEqual(self.k.stats()["records"], {"verified": 1})

    def test_explicit_confirms_existing_unverified(self):
        self.k.add("project_fact", "GUI is built with PySide6 toolkit", "inferred")
        r = self.ln.learn_fact("GUI is built with PySide6 toolkit", "user", explicit=True)
        self.assertEqual(r["result"], "refreshed")
        self.assertEqual(self.k.stats()["records"], {"verified": 1})

    def test_unverified_has_expiry_and_temporary_ttl(self):
        rid = self.k.add("project_fact", "maybe thing", "guess")["id"]
        exp = self.l.db().execute("SELECT expires_at FROM records WHERE id=?", (rid,)).fetchone()[0]
        self.assertTrue(exp and exp > now_iso())
        self.assertEqual(self.ln.learn_fact("for now the build is broken", "u", explicit=True)["category"], "temporary")

    def test_command_learning_rules(self):
        self.assertEqual(self.ln.learn_command("python -m py_compile scripts/app.py", 0)["result"], "created")
        self.assertEqual(self.ln.learn_command("python -m py_compile scripts/app.py", 0)["result"], "refreshed")
        self.assertEqual(self.ln.learn_command("python x.py", 1)["result"], "ignored")
        self.assertEqual(self.ln.learn_command("rm -rf /", 0)["result"], "ignored")
        self.assertEqual(self.ln.learn_command("python x.py --token=abc123", 0)["result"], "rejected_secret")

    def test_candidate_expiry(self):
        self.ln.learn_fact("Thing one is X", "u", explicit=True)
        cid = self.ln.learn_fact("Thing one is Y", "u", explicit=True)["candidate_id"]
        self.l.db().execute("UPDATE candidates SET expires_at='2000-01-01T00:00:00+00:00'")
        self.l.db().commit()
        self.assertEqual(self.ln.candidates(), [])
        with self.assertRaises(LiasError):
            self.ln.promote(cid)


GOOD = '''import json, os, sys
req = json.loads(sys.stdin.read() or "{}")
root = os.environ["LIAS_PROJECT_ROOT"]
n = sum(1 for f in os.listdir(root) if f.endswith(req.get("ext", ".txt")))
print(json.dumps({"count": n}))
'''
GOOD_M = {"id": "count-ext", "name": "Count by extension", "description": "Count top-level files with a given extension",
          "version": "1.0.0", "risk": 0, "timeout_s": 5, "input_schema": {"ext": {"type": "string"}}}
GOOD_T = [{"name": "two", "input": {"ext": ".txt"}, "files": {"a.txt": "", "b.txt": "", "c.py": ""}, "expect": {"count": 2}}]


class TestGrowth(Base):
    def setUp(self):
        super().setUp()
        from growth import Growth
        self.g = Growth(self.l)

    def test_observe_and_candidate_threshold(self):
        for _ in range(2):
            self.g.observe("Count Files", "count files by extension in the project tree")
        self.assertEqual(self.g.candidates(), [])
        self.g.observe("Count Files", "count files by extension in the project tree")
        c = self.g.candidates()
        self.assertEqual(c[0]["observed"], 3)
        self.assertTrue(c[0]["eligible"])

    def test_covered_workflow_not_eligible(self):
        for _ in range(3):
            self.g.observe("src-stats", "Count files and lines per extension in the project")
        self.assertFalse(self.g.candidates()[0]["eligible"])

    def test_full_lifecycle_then_reuse(self):
        rep = self.g.propose(dict(GOOD_M), GOOD, GOOD_T)
        self.assertTrue(rep["activated"], rep)
        self.assertEqual([s["stage"] for s in rep["stages"]][:3], ["evaluate", "static-validation", "isolated-test:two"])
        (self.root / "x.txt").write_text("")
        env = self.a.run("count-ext", {"ext": ".txt"})
        self.assertEqual(env["status"], "success", env)
        self.assertEqual(env["result"]["count"], 1)

    def test_static_validation_rejections(self):
        bad = {
            "subprocess": "import subprocess\nprint('{}')",
            "network": "import socket\nprint('{}')",
            "eval": "print(eval('1'))",
            "getattr": "import os\ngetattr(os, 'system')('x')",
            "dunder": "print(().__class__.__bases__)",
            "write-open": "open('x.txt','w').write('a')\nprint('{}')",
            "os.remove": "import os\nos.remove('x')\nprint('{}')",
            "write-text": "from pathlib import Path\nPath('a').write_text('x')\nprint('{}')",
            "dyn-mode": "m='w'\nopen('x',m)\nprint('{}')",
            "relative": "from . import x\nprint('{}')",
        }
        for name, src in bad.items():
            m = dict(GOOD_M, id="bad-" + name.replace(".", "-"), description="unique %s probe workflow" % name)
            rep = self.g.propose(m, src, GOOD_T)
            self.assertFalse(rep["activated"], name)
            self.assertEqual(rep["stages"][-1]["stage"], "static-validation", name)
        self.assertFalse(list((self.l.dir / "automation" / "generated").glob("*.py")) if
                         (self.l.dir / "automation" / "generated").exists() else [])

    def test_failing_test_blocks_activation(self):
        t = [dict(GOOD_T[0], expect={"count": 99})]
        rep = self.g.propose(dict(GOOD_M), GOOD, t)
        self.assertFalse(rep["activated"])
        self.assertIn("mismatch", rep["stages"][-1]["detail"])
        self.assertNotIn("count-ext", self.a.load())

    def test_nonjson_crash_and_timeout_blocked(self):
        for src, tag in (("print('hi')", "json"), ("raise SystemExit(3)", "exit"), ("while True: pass", "timeout")):
            self.g.cfg["test_timeout_s"] = 2
            m = dict(GOOD_M, id="x-" + tag, description="distinct %s case words" % tag)
            self.assertFalse(self.g.propose(m, src, GOOD_T)["activated"], tag)

    def test_raw_os_write_banned_statically(self):
        from growth import static_validate
        src = "import os\nfd = os.open('n.txt', os.O_CREAT | os.O_WRONLY)\nprint('{}')"
        self.assertTrue(any("os.open" in v for v in static_validate(src, 0)))

    def test_runtime_snapshot_backstop_catches_writer(self):
        # Bypass static validation on purpose to prove the isolated-test backstop works on its own.
        d = tempfile.mkdtemp(prefix="lias_bs_")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        sp = os.path.join(d, "w.py")
        Path(sp).write_text("import json\nopen('new.txt', 'w').write('x')\nprint(json.dumps({}))")
        err = self.g._isolated_test(sp, {"input": {}, "files": {}, "expect": {}}, 0)
        self.assertIn("modified", err)
        self.assertIsNone(self.g._isolated_test(sp, {"input": {}, "files": {}, "expect": {}}, 1))

    def test_risk_tiers(self):
        self.assertIn("never", self.g.propose(dict(GOOD_M, risk=2), GOOD, GOOD_T)["stages"][-1]["detail"])
        self.assertIn("approved_by_user", self.g.propose(dict(GOOD_M, risk=1), GOOD, GOOD_T)["stages"][-1]["detail"])

    def test_no_tests_rejected(self):
        self.assertFalse(self.g.propose(dict(GOOD_M), GOOD, [])["activated"])

    def test_near_duplicate_rejected(self):
        m = dict(GOOD_M, id="other-stats", description="Count files and lines per extension in the project (skips .git)")
        rep = self.g.propose(m, GOOD, GOOD_T)
        self.assertFalse(rep["activated"])
        self.assertIn("near-duplicate", rep["stages"][-1]["detail"])

    def test_version_bump_and_rollback(self):
        self.assertTrue(self.g.propose(dict(GOOD_M), GOOD, GOOD_T)["activated"])
        self.assertFalse(self.g.propose(dict(GOOD_M), GOOD, GOOD_T)["activated"])  # same version
        v2 = GOOD.replace('"count": n', '"count": n, "v": 2')
        t2 = [dict(GOOD_T[0], expect={"count": 2, "v": 2})]
        self.assertTrue(self.g.propose(dict(GOOD_M, version="2.0.0"), v2, t2)["activated"])
        self.assertEqual(self.a.load()["count-ext"]["version"], "2.0.0")
        self.g.rollback("count-ext")
        self.assertEqual(self.a.load()["count-ext"]["version"], "1.0.0")
        self.assertNotIn('"v": 2', (self.l.dir / "automation" / "generated" / "count-ext.py").read_text())

    def test_attempt_budget(self):
        self.g.cfg["max_attempts_per_day"] = 2
        self.g.propose(dict(GOOD_M, id="b1", description="aaa bbb"), "print(", GOOD_T)
        self.g.propose(dict(GOOD_M, id="b2", description="ccc ddd"), "print(", GOOD_T)
        with self.assertRaises(LiasError):
            self.g.propose(dict(GOOD_M, id="b3", description="eee fff"), "print(", GOOD_T)

    def test_generated_failures_quarantine(self):
        src = GOOD.replace("print(json.dumps({\"count\": n}))",
                           "if req.get('boom'):\n    raise SystemExit(1)\nprint(json.dumps({'count': n}))")
        m = dict(GOOD_M, id="flaky", description="flaky distinct words here", input_schema={"boom": {"type": "boolean"}})
        t = [{"name": "ok", "input": {}, "files": {}, "expect": {"count": 0}}]
        self.assertTrue(self.g.propose(m, src, t)["activated"])
        for _ in range(3):
            self.a.run("flaky", {"boom": True})
        self.assertIn("quarantined", self.a.run("flaky", {})["error"])


class TestOptimization(Base):
    def test_cache_bound_enforced(self):
        self.l.config["automation"]["max_cache_entries"] = 3
        for i in range(6):
            (self.root / ("f%d.py" % i)).write_text("x\n" * (i + 1))
            self.a.run("source-stats", {"extensions": [".py"]})
        self.assertLessEqual(self.a.cache_stats()["entries"], 3)

    def test_cache_stats_hit_rate(self):
        for _ in range(4):
            self.a.run("source-stats")
        s = self.a.cache_stats()
        self.assertEqual((s["successful_runs"], s["cache_hits"]), (4, 3))

    def test_compaction_spills_full_output(self):
        env = self.a._env("x", "success", time.monotonic(), result={"files": list(range(5000)), "n": 5000, "name": "ok"})
        small = self.a.compact(env, budget=500)
        self.assertTrue(small["truncated"])
        self.assertEqual(small["result"]["n"], 5000)
        self.assertEqual(small["result"]["files"], "<list, 5000 items>")
        full = json.loads((self.root / small["full_output"]).read_text())
        self.assertEqual(len(full["result"]["files"]), 5000)
        self.assertLess(len(json.dumps(small)), 500)
        tiny = self.a._env("y", "success", time.monotonic(), result={"a": 1})
        self.assertIs(self.a.compact(tiny), tiny)  # small output passes through untouched

    def test_log_retention(self):
        logs = self.l.dir / "logs"
        logs.mkdir(exist_ok=True)
        old = logs / "run-x-1.json"
        old.write_text("{}")
        os.utime(old, (1, 1))
        new = logs / "run-x-2.json"
        new.write_text("{}")
        self.assertEqual(self.a.purge_logs(), 1)
        self.assertTrue(new.exists() and not old.exists())

    def test_cost_aware_candidates(self):
        from growth import Growth
        g = Growth(self.l)
        g.cfg.update(min_repeats=1, est_tokens_per_use=100, build_cost_tokens=500)
        for _ in range(4):
            g.observe("tiny-task", "totally unique tiny chore words alpha")
        c = g.candidates()[0]
        self.assertEqual(c["est_net_tokens"], -100)
        self.assertFalse(c["eligible"])
        g.observe("tiny-task", "totally unique tiny chore words alpha")
        self.assertTrue(g.candidates()[0]["eligible"])

    def test_benchmark_runs_and_reports_no_token_claim(self):
        import benchmark
        res = benchmark.run(self.root)
        self.assertGreaterEqual(res["retrieval"]["top1_accuracy"], 0.9)  # regression floor on the fixture
        self.assertIn("NOT MEASURED", res["model_tokens"])
        self.assertTrue(list((self.l.dir / "benchmarks").glob("bench-*.json")))


class TestHardening(Base):
    def setUp(self):
        super().setUp()
        import maintenance
        from growth import Growth
        self.m, self.g = maintenance, Growth(self.l)

    # ---- resource limits ----
    def test_output_flood_is_killed(self):
        write_auto(self.root, "flood", "import sys\nwhile True:\n    sys.stdout.write('x' * 100000)\n", timeout_s=20)
        self.l.config["automation"]["max_output_bytes"] = 4096
        t = time.monotonic()
        env = self.a.run("flood")
        self.assertEqual(env["status"], "error")
        self.assertIn("output exceeded", env["error"])
        self.assertLess(time.monotonic() - t, 10)

    def test_flood_blocked_in_isolated_test_too(self):
        src = "import sys\nwhile True:\n    sys.stdout.write('y' * 100000)\n"
        self.l.config["automation"]["max_output_bytes"] = 4096
        rep = self.g.propose(dict(GOOD_M, id="flood2", description="flood probe words zeta"), src, GOOD_T)
        self.assertFalse(rep["activated"])
        self.assertIn("output limit", rep["stages"][-1]["detail"])

    # ---- tamper / permission ----
    def _install_good(self):
        self.assertTrue(self.g.propose(dict(GOOD_M), GOOD, GOOD_T)["activated"])
        return self.l.dir / "automation" / "generated" / "count-ext.py"

    def test_tampered_generated_script_refused_and_flagged(self):
        p = self._install_good()
        self.assertEqual(self.a.run("count-ext", {"ext": ".txt"})["status"], "success")
        p.write_text(p.read_text() + "\n# edited after activation\n")
        env = self.a.run("count-ext", {"ext": ".txt"})
        self.assertEqual(env["status"], "error")
        self.assertIn("integrity", env["error"])
        self.assertIn("count-ext", self.m.health(self.l)["checks"]["tampered"])

    def test_tamper_not_masked_by_cache(self):
        m = dict(GOOD_M, cache=True, fingerprint_globs=["*.txt"])
        self.assertTrue(self.g.propose(m, GOOD, GOOD_T)["activated"])
        self.assertFalse(self.a.run("count-ext", {"ext": ".txt"})["cached"])
        self.assertTrue(self.a.run("count-ext", {"ext": ".txt"})["cached"])
        p = self.l.dir / "automation" / "generated" / "count-ext.py"
        p.write_text(p.read_text() + "\n# tampered\n")
        self.assertIn("integrity", self.a.run("count-ext", {"ext": ".txt"})["error"])

    def test_generated_manifest_without_hash_refused(self):
        write_auto(self.root, "handmade", "print('{}')", generated=True)
        self.assertIn("integrity", self.a.run("handmade")["error"])

    # ---- backup / restore / recovery ----
    def test_backup_restore_roundtrip(self):
        rid = self.k.add("project_fact", "kept across restore", "t", verified=True)["id"]
        self.m.backup(self.l)
        self.k.invalidate(rid)
        self.k.add("project_fact", "added after backup zebra", "t", verified=True)
        res = self.m.restore(self.l)
        self.assertIn("restored_from", res)
        k2 = Knowledge(self.l)
        self.assertEqual(len(k2.search("restore")), 1)
        self.assertEqual(k2.search("zebra"), [])
        self.assertTrue(list((self.l.dir / "backups").glob("pre-restore-*.sqlite3")))

    def test_backup_retention(self):
        self.k.add("project_fact", "x y z fact", "t", verified=True)
        for _ in range(8):
            self.m.backup(self.l)
        self.assertEqual(len(self.m.list_backups(self.l)), 5)

    def test_corrupt_db_clear_error_then_recover(self):
        self.k.add("project_fact", "survivor fact quartz", "t", verified=True)
        self.m.backup(self.l)
        self.l.close()
        self.l.db_path.write_bytes(b"this is not a database" * 100)
        for s in ("-wal", "-shm"):
            Path(str(self.l.db_path) + s).unlink(missing_ok=True)
        with self.assertRaises(LiasError) as cm:
            Lias(self.root).db()
        self.assertIn("recover", str(cm.exception))
        self.m.restore(self.l)
        l2 = Lias(self.root)
        self.assertEqual(len(Knowledge(l2).search("quartz")), 1)
        l2.close()

    def test_restore_rejects_foreign_and_invalid_backups(self):
        other = make_project(False)
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        lo = Lias(other)
        Knowledge(lo).add("project_fact", "foreign data", "t", verified=True)
        foreign = self.m.backup(lo)["backup"]
        fpath = other / foreign
        lo.close()
        with self.assertRaises(LiasError):
            self.m.restore(self.l, fpath)
        junk = self.root / "junk.sqlite3"
        junk.write_bytes(b"nope")
        with self.assertRaises(LiasError):
            self.m.restore(self.l, junk)

    # ---- repair ----
    def test_fts_drift_detected_and_repaired(self):
        self.k.add("project_fact", "searchable pelican fact", "t", verified=True)
        self.l.db().execute("DELETE FROM kfts")
        self.l.db().commit()
        self.assertEqual(self.k.search("pelican"), [])
        self.assertTrue(any("index out of sync" in i for i in self.m.health(self.l)["issues"]))
        self.m.fix(self.l)
        self.assertEqual(len(self.k.search("pelican")), 1)
        self.assertFalse(any("index out of sync" in i for i in self.m.health(self.l)["issues"]))

    def test_interrupted_write_keeps_original_and_cleans_tmp(self):
        import common
        target = self.root / "cfg.json"
        target.write_text("ORIGINAL")
        real = os.replace
        common.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                common.atomic_write(target, "NEW")
        finally:
            common.os.replace = real
        self.assertEqual(target.read_text(), "ORIGINAL")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_stale_tmp_flagged_and_removed(self):
        t = self.l.dir / "config" / "half.tmp"
        t.parent.mkdir(exist_ok=True)
        t.write_text("partial")
        os.utime(t, (1, 1))
        self.assertTrue(any("stale temp" in i for i in self.m.health(self.l)["issues"]))
        self.assertEqual(self.m.fix(self.l)["stale_tmp_removed"], 1)

    # ---- config validation ----
    def test_bad_config_rejected(self):
        cfgdir = self.l.dir / "config"
        cfgdir.mkdir(exist_ok=True)
        for bad in ('{"automation": {"max_timeout_s": -5}}', '{"retrieval": {"max_results": "lots"}}', "{ nope"):
            (cfgdir / "lias.json").write_text(bad)
            with self.assertRaises(LiasError):
                Lias(self.root)
        (cfgdir / "lias.json").write_text('{"custom_unknown": 1, "retrieval": {"max_results": 7}}')
        self.assertEqual(Lias(self.root).config["retrieval"]["max_results"], 7)

    # ---- filesystem escape ----
    @unittest.skipUnless(os.name == "nt", "junctions are a Windows feature")
    def test_junction_does_not_escape_project(self):
        import subprocess
        outside = Path(tempfile.mkdtemp(prefix="lias_out_"))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        (outside / "secret.py").write_text("a=1\nb=2\nc=3\n")
        (self.root / "real.py").write_text("x=1\n")
        link = self.root / "linked"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        try:
            env = self.a.run("source-stats", {"extensions": [".py"]}, use_cache=False)
        finally:
            os.rmdir(link)  # removes the junction only, never the target
        self.assertEqual(env["result"]["files_scanned"], 1)
        self.assertEqual(env["result"]["total_lines"], 1)

    def test_fingerprint_glob_traversal_rejected(self):
        write_auto(self.root, "fp", "print('{}')", cache=True, fingerprint_globs=["../*"])
        env = self.a.run("fp")
        self.assertEqual(env["status"], "error")


class TestMigration(unittest.TestCase):
    def test_v1_to_v2_with_backup_and_data_kept(self):
        import sqlite3
        import knowledge
        root = make_project(False)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        l = Lias(root)
        l.db_path.parent.mkdir(parents=True)
        con = sqlite3.connect(str(l.db_path))
        con.executescript(knowledge._SCHEMA.split("CREATE TABLE IF NOT EXISTS candidates")[0])
        con.executemany("INSERT INTO meta VALUES(?,?)", [("schema_version", "1"), ("project_id", l.pid), ("root", str(root))])
        con.execute("INSERT INTO records VALUES('r1','k','project_fact','kept me','t','2026-01-01',NULL,'verified','high',NULL,?,'[]',1)", (l.pid,))
        con.commit(); con.close()
        d = l.db()
        self.assertEqual(d.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "3")
        self.assertEqual([r[0] for r in d.execute("SELECT id FROM kfts WHERE kfts MATCH 'kept'")], ["r1"])
        self.assertEqual(d.execute("SELECT content FROM records WHERE id='r1'").fetchone()[0], "kept me")
        d.execute("SELECT * FROM candidates")
        self.assertTrue(list(l.db_path.parent.glob("*.bak")))
        l.close()


if __name__ == "__main__":
    unittest.main()
