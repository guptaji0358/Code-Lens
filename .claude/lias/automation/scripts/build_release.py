"""Project-local write (risk 1): start scripts/build_release.py DETACHED and return at once.

A full PyInstaller build outlasts LIAS's 120 s run cap, so this launcher spawns the real build
(scripts/build_release.py - the single source of build logic) in the background and reports where
its log is. Call again with {"action": "status"} to read the log tail. JSON stdin -> JSON stdout.
"""
import json
import os
import subprocess
import sys

ROOT = os.environ.get("LIAS_PROJECT_ROOT", os.getcwd())
LOG = os.path.join(ROOT, "build", "build_release.log")
SCRIPT = os.path.join(ROOT, "scripts", "build_release.py")


def main():
    req = json.loads(sys.stdin.read() or "{}")
    if req.get("action") == "status":
        tail = ""
        if os.path.isfile(LOG):
            with open(LOG, "rb") as fh:
                tail = fh.read()[-3000:].decode("utf-8", "replace")
        print(json.dumps({"log": LOG, "tail": tail}))
        return
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    argv = [sys.executable, SCRIPT]
    for flag in ("force", "no_launch", "no_installer"):
        if req.get(flag):
            argv.append("--" + flag.replace("_", "-"))
    log = open(LOG, "wb")
    p = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    print(json.dumps({"started": True, "pid": p.pid, "log": LOG, "args": argv[2:]}))


main()
