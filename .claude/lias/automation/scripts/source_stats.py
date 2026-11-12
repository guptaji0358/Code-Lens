"""Read-only: count files/lines per extension under the project root. JSON stdin -> JSON stdout."""
import json
import os
import sys

SKIP = {".git", "__pycache__", "dist", "build", "dist_tmp", "build_tmp", "out", ".claude", "node_modules"}


def main():
    req = json.loads(sys.stdin.read() or "{}")
    exts = tuple(e.lower() for e in req.get("extensions", [".py"]))
    root = os.environ.get("LIAS_PROJECT_ROOT", os.getcwd())
    per = {}
    files = 0
    for dp, dn, fn in os.walk(root, followlinks=False):
        # skip junctions/symlinked dirs so the scan can never leave the project tree
        dn[:] = [d for d in dn if d not in SKIP and not os.path.islink(os.path.join(dp, d))
                 and not (hasattr(os.path, "isjunction") and os.path.isjunction(os.path.join(dp, d)))]
        for f in fn:
            ext = os.path.splitext(f)[1].lower()
            if ext not in exts:
                continue
            try:
                with open(os.path.join(dp, f), "rb") as fh:
                    n = sum(1 for _ in fh)
            except OSError:
                continue
            files += 1
            e = per.setdefault(ext, {"files": 0, "lines": 0})
            e["files"] += 1
            e["lines"] += n
    print(json.dumps({"files_scanned": files, "total_lines": sum(v["lines"] for v in per.values()),
                      "by_extension": per}))


main()
