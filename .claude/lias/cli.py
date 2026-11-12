"""LIAS CLI. Usage: python .claude/lias/cli.py <command> ...   (all output is compact JSON)"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import LIAS_VERSION, Lias, LiasError, find_project_root  # noqa: E402
from knowledge import Knowledge, SecretRejected, rebind  # noqa: E402
from automation import Automations  # noqa: E402


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def doctor(l):
    k, a = Knowledge(l), Automations(l)
    checks = {}
    checks["db_integrity"] = l.db().execute("PRAGMA integrity_check").fetchone()[0]
    checks["fts5"] = True
    checks["python"] = sys.version.split()[0]
    reg = a.load()
    checks["invalid_manifests"] = [k_ for k_, v in reg.items() if v.get("status") == "invalid"]
    ok = checks["db_integrity"] == "ok" and not checks["invalid_manifests"]
    return {"lias_version": LIAS_VERSION, "project_root": str(l.root), "project_id": l.pid,
            "healthy": ok, "checks": checks}


def status(l):
    k, a = Knowledge(l), Automations(l)
    autos = {i: {"status": a.effective_status(m), "version": m["version"], "risk": m["risk"]}
             for i, m in a.load().items() if m.get("status") != "invalid"}
    runs = [dict(r) for r in l.db().execute(
        "SELECT ts,automation_id,status,duration_ms,cached FROM runs ORDER BY id DESC LIMIT 5")]
    return {"lias_version": LIAS_VERSION, "knowledge": k.stats(), "automations": autos,
            "cache": a.cache_stats(), "recent_runs": runs}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lias")
    ap.add_argument("--root", help="project root (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dp = sub.add_parser("doctor"); dp.add_argument("--fix", action="store_true", help="safe repairs only")
    sub.add_parser("backup")
    rs = sub.add_parser("restore"); rs.add_argument("path", nargs="?"); rs.add_argument("--force", action="store_true")
    sub.add_parser("recover")
    sub.add_parser("status")
    sub.add_parser("rebind")
    sub.add_parser("integrate").add_argument("--dry-run", action="store_true")
    kp = sub.add_parser("knowledge").add_subparsers(dest="kcmd", required=True)
    s = kp.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int)
    a = kp.add_parser("add")
    a.add_argument("category"); a.add_argument("content")
    a.add_argument("--source", required=True); a.add_argument("--key")
    a.add_argument("--verified", action="store_true"); a.add_argument("--ttl-days", type=int)
    a.add_argument("--supersede", action="store_true")
    i = kp.add_parser("invalidate"); i.add_argument("id"); i.add_argument("--reason", default="")
    kp.add_parser("purge-expired")
    lp = sub.add_parser("learn").add_subparsers(dest="lcmd", required=True)
    lf = lp.add_parser("fact"); lf.add_argument("content"); lf.add_argument("--source", required=True)
    lf.add_argument("--confirmed", action="store_true", help="user explicitly confirmed this fact")
    lc = lp.add_parser("command"); lc.add_argument("command"); lc.add_argument("--exit-code", type=int, required=True)
    lp.add_parser("candidates")
    pr = lp.add_parser("promote"); pr.add_argument("id"); pr.add_argument("--supersede", action="store_true")
    rj = lp.add_parser("reject"); rj.add_argument("id")
    kv = kp.add_parser("verify"); kv.add_argument("id")
    ob = sub.add_parser("observe"); ob.add_argument("signature"); ob.add_argument("description")
    gp = sub.add_parser("growth").add_subparsers(dest="gcmd", required=True)
    gp.add_parser("candidates"); gp.add_parser("history")
    pp = gp.add_parser("propose")
    pp.add_argument("--manifest", required=True); pp.add_argument("--script", required=True)
    pp.add_argument("--tests", required=True); pp.add_argument("--approved-by-user", action="store_true")
    rb = gp.add_parser("rollback"); rb.add_argument("id")
    sub.add_parser("benchmark")
    sub.add_parser("maintain")
    r = sub.add_parser("run"); r.add_argument("id"); r.add_argument("--input", default="{}")
    r.add_argument("--full", action="store_true", help="do not compact large output")
    r.add_argument("--no-cache", action="store_true")
    sub.add_parser("automations")
    re_ = sub.add_parser("reinstate"); re_.add_argument("id")
    args = ap.parse_args(argv)
    try:
        l = Lias(args.root or find_project_root())
        k, au = Knowledge(l), Automations(l)
        if args.cmd == "doctor":
            import maintenance
            if args.fix:
                out(maintenance.fix(l))
            rep = maintenance.health(l)
            out({"lias_version": LIAS_VERSION, "project_root": str(l.root), "project_id": l.pid,
                 "python": sys.version.split()[0], "healthy": not rep["issues"], **rep})
            return 0 if not rep["issues"] else 1
        elif args.cmd == "backup":
            import maintenance
            out(maintenance.backup(l))
        elif args.cmd in ("restore", "recover"):
            import maintenance
            out(maintenance.restore(l, getattr(args, "path", None), getattr(args, "force", False)))
        elif args.cmd == "status": out(status(l))
        elif args.cmd == "rebind": rebind(l); out({"rebound": l.pid})
        elif args.cmd == "learn":
            from learning import Learner
            ln = Learner(l)
            if args.lcmd == "fact": out(ln.learn_fact(args.content, args.source, explicit=args.confirmed))
            elif args.lcmd == "command": out(ln.learn_command(args.command, args.exit_code))
            elif args.lcmd == "candidates": out(ln.candidates())
            elif args.lcmd == "promote": out(ln.promote(args.id, args.supersede))
            elif args.lcmd == "reject": out({"rejected": ln.reject(args.id)})
        elif args.cmd == "knowledge" and args.kcmd == "verify":
            out({"verified": k.verify(args.id)})
        elif args.cmd in ("observe", "growth"):
            from growth import Growth
            g = Growth(l)
            if args.cmd == "observe": out(g.observe(args.signature, args.description))
            elif args.gcmd == "candidates": out(g.candidates())
            elif args.gcmd == "history": out(g.history())
            elif args.gcmd == "rollback": out(g.rollback(args.id))
            else:
                rd = lambda p: open(p, encoding="utf-8-sig").read()
                rep = g.propose(json.loads(rd(args.manifest)), rd(args.script), json.loads(rd(args.tests)),
                                approved_by_user=args.approved_by_user)
                out(rep)
                return 0 if rep["activated"] else 1
        elif args.cmd == "integrate":
            from integrate import merge_settings, merge_claude_md
            out({"settings_changed": merge_settings(l.root, args.dry_run),
                 "claude_md_changed": merge_claude_md(l.root, args.dry_run), "dry_run": args.dry_run})
        elif args.cmd == "benchmark":
            import benchmark
            out(benchmark.run(l.root))
        elif args.cmd == "maintain":
            out({"cache_pruned": au.prune_cache(), "logs_purged": au.purge_logs(),
                 "knowledge_expired": k.purge_expired()})
        elif args.cmd == "automations": out(au.load())
        elif args.cmd == "reinstate": au.reinstate(args.id); out({"reinstated": args.id})
        elif args.cmd == "run":
            env = au.run(args.id, json.loads(args.input), use_cache=not args.no_cache)
            out(env if args.full else au.compact(env))
            return 0 if env["status"] == "success" else 1
        elif args.kcmd == "search": out(k.search(args.query, args.limit))
        elif args.kcmd == "add":
            out(k.add(args.category, args.content, args.source, key=args.key, verified=args.verified,
                      ttl_days=args.ttl_days, supersede=args.supersede))
        elif args.kcmd == "invalidate": out({"invalidated": k.invalidate(args.id, args.reason)})
        elif args.kcmd == "purge-expired": out({"expired": k.purge_expired()})
        return 0
    except SecretRejected as e:
        print("REJECTED: %s" % e, file=sys.stderr); return 3
    except (LiasError, ValueError) as e:
        print("ERROR: %s" % e, file=sys.stderr); return 2
    finally:
        pass


if __name__ == "__main__":
    sys.exit(main())
