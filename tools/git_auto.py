#!/usr/bin/env python3
"""Git automation: add selected paths -> commit with a message -> push -> show log.

Interactive (no args):   python tools/git_auto.py
Non-interactive:         python tools/git_auto.py --paths scripts/a.py tests --message "Fix X" --log short
Options:
  --paths    one or more paths relative to the repo root (required unless interactive)
  --message  commit message (required unless interactive)
  --log      short | long | none   verification log shown after push (default: short)
  --no-push  commit only, skip the push step
  --branch   push target branch (default: current branch)

Stdlib only. Never uses `git add .` / `-A`: only the paths you list are staged.
"""

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT_HINT = Path(__file__).resolve().parent.parent


def run_git(repo, *args, check=True):
    """Run git with an argument list (no shell). Returns CompletedProcess."""
    cmd = ["git", "-C", str(repo), *args]
    print(f"$ git {' '.join(args)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip())
    if check and result.returncode != 0:
        raise SystemExit(f"[FAILED] git {' '.join(args)} (exit {result.returncode})")
    return result


def find_repo_root():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT_HINT), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit("[FAILED] Not inside a git repository.")
    return Path(result.stdout.strip())


def split_paths(raw):
    """Accept comma, semicolon or space separated paths; strip quotes."""
    parts = raw.replace(";", ",").replace(",", " ").split()
    return [p.strip('"\'') for p in parts if p.strip('"\'')]


def interactive_input(args):
    if not args.paths:
        raw = input("Root paths to add (comma/space separated, relative to repo): ")
        args.paths = split_paths(raw)
    if not args.message:
        args.message = input("Commit message: ").strip()
    if args.log == "short" and not args.interactive_log_asked:
        choice = input("Verify log after push - short / long / none [short]: ").strip().lower()
        if choice in ("short", "long", "none"):
            args.log = choice
    return args


def validate(args, repo):
    if not args.paths:
        raise SystemExit("[FAILED] No paths given.")
    if not args.message:
        raise SystemExit("[FAILED] Empty commit message.")
    missing = [p for p in args.paths if not (repo / p).exists()]
    if missing:
        raise SystemExit(f"[FAILED] Path(s) not found under repo root: {', '.join(missing)}")


def show_log(repo, mode):
    if mode == "none":
        return
    print(f"\n=== Verification log ({mode}) ===")
    if mode == "short":
        run_git(repo, "log", "-1", "--oneline", "--decorate")
    else:
        run_git(repo, "log", "-1", "--format=fuller", "--stat")


def main():
    parser = argparse.ArgumentParser(description="Add paths, commit, push, verify log.")
    parser.add_argument("--paths", nargs="+", help="paths relative to repo root")
    parser.add_argument("--message", "-m", help="commit message")
    parser.add_argument("--log", choices=["short", "long", "none"], default="short")
    parser.add_argument("--no-push", action="store_true", help="commit only")
    parser.add_argument("--branch", help="push branch (default: current branch)")
    args = parser.parse_args()
    args.interactive_log_asked = "--log" in sys.argv

    repo = find_repo_root()
    print(f"Repo: {repo}")

    if args.paths is None or args.message is None:
        args = interactive_input(args)
    validate(args, repo)

    # 1. add (only the listed paths)
    run_git(repo, "add", "--", *args.paths)

    # Nothing staged? stop before committing.
    staged = run_git(repo, "diff", "--cached", "--name-only", check=False).stdout.strip()
    if not staged:
        raise SystemExit("[STOP] Nothing staged from those paths (no changes). Nothing committed.")
    print(f"\nStaged files:\n{staged}\n")

    # 2. commit
    run_git(repo, "commit", "-m", args.message)

    # 3. push
    if args.no_push:
        print("\n[SKIP] --no-push given; not pushing.")
    else:
        branch = args.branch or run_git(repo, "branch", "--show-current").stdout.strip()
        if not branch:
            raise SystemExit("[FAILED] Detached HEAD; pass --branch to push.")
        run_git(repo, "push", "-u", "origin", branch)

    # 4. verify
    show_log(repo, args.log)
    print("\n[DONE]")


if __name__ == "__main__":
    main()
