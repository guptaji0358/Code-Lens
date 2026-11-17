"""Risk-1 (project-local write + push): add paths -> commit -> push -> verify log.

JSON stdin -> JSON stdout. Delegates to tools/git_auto.py with an argv list (no shell).
Input: {"paths": [...], "message": "...", "log": "short|long|none", "no_push": bool, "branch": "..."}
"""
import json
import os
import subprocess
import sys

LOG_MODES = ("short", "long", "none")


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(0)


def main():
    req = json.loads(sys.stdin.read() or "{}")
    paths = req.get("paths") or []
    message = (req.get("message") or "").strip()
    log = req.get("log", "short")
    if not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        fail("paths must be a non-empty list of strings")
    if any(p.startswith("-") or ".." in p.replace("\\", "/").split("/") for p in paths):
        fail("paths must be relative to the repo root and must not start with '-' or contain '..'")
    if not message:
        fail("message is required")
    if log not in LOG_MODES:
        fail("log must be one of: short, long, none")

    root = os.environ.get("LIAS_PROJECT_ROOT", os.getcwd())
    tool = os.path.join(root, "tools", "git_auto.py")
    argv = [sys.executable, "-I", tool, "--paths", *paths, "--message", message, "--log", log]
    if req.get("no_push"):
        argv.append("--no-push")
    if req.get("branch"):
        argv += ["--branch", str(req["branch"])]

    proc = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=110)
    print(json.dumps({"ok": proc.returncode == 0, "exit_code": proc.returncode,
                      "output": (proc.stdout + proc.stderr).strip()[-8000:]}))


main()
