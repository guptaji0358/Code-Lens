# LIAS — Local Intelligence & Automation System (project-local)

Stdlib-only Python 3.10+ (developed on 3.13) with SQLite/FTS5. Everything lives in `.claude/lias/`; nothing is shared
with other projects. No daemon, no network, no cloud embeddings.

## Architecture

```
prompt ──► Claude ──► (a) hook injects verified knowledge   (UserPromptSubmit, SessionStart)
                      (b) Claude calls cli.py: knowledge search / run <automation> / observe / growth propose
                              │                         │
                       knowledge.py (SQLite+FTS5)   automation.py (registry, bounded runner, cache)
                              │                         │
                       learning.py (candidates)     growth.py (validate → isolated test → activate)
                              └──── maintenance.py (backup/restore/health/fix) ────┘
```

| File | Role |
|---|---|
| `common.py` | project identity (hash of real root path), config + validation, atomic writes, bounded subprocess runner |
| `knowledge.py` | records, provenance, verification, contradiction handling, expiry, audit, schema migrations (v3) |
| `learning.py` | "remember that…" capture, near-duplicate detection, conflict staging, successful-command learning |
| `automation.py` | manifest registry, input validation, runner, cache with file fingerprints, quarantine, compaction |
| `growth.py` | observation counts, cost-aware candidates, static validation, isolated test, activate/rollback |
| `maintenance.py` | backups (keep 5), restore, deep health check, safe repair |
| `hook.py` / `integrate.py` | Claude Code hook entry point; idempotent merge into `.claude/settings.json` and `CLAUDE.md` |
| `benchmark.py`, `cli.py` | benchmarks, command line |

## Setup / client integration

Already installed here. To (re)install the hooks: `python .claude/lias/cli.py integrate` (idempotent, backs up an existing
`settings.json`, refuses to touch invalid JSON). It registers two Claude Code hooks (`SessionStart`, `UserPromptSubmit`) and
appends a marked block to `CLAUDE.md`.

What is automatic: relevant **verified** knowledge is injected into context, and "remember that X" is saved.
What is not: Claude still decides to call `run`/`observe`/`growth propose`; hooks only see events, they do not understand
prompts. Hooks load at session start — start a new session after installing. Confirm with a prompt that matches a stored fact
(you should see a `[LIAS]` line in context) before relying on them.

## Knowledge

```
cli.py knowledge add <category> "<fact>" --source <provenance> [--key subject] [--verified] [--ttl-days N]
cli.py knowledge search "<query>" | verify <id> | invalidate <id> | purge-expired
cli.py learn fact|command|candidates|promote <id> [--supersede]|reject <id>
```
Categories: project_fact, architecture_decision, preference, build_config, validated_command, known_error, automation,
temporary, historical. Rules: secrets are rejected and never staged; unverified records expire after 30 days (temporary: 1 day);
a fact that contradicts a verified one is **staged**, never overwritten (`learn promote <id> --supersede` after the user
agrees); pasted content is never learned from. Search is keyword (FTS5, stemming, stop-words), not semantic.

## Automation

Manifest (`automation/manifests/<id>.json`): id, name, description, entry, input_schema, timeout_s, risk (0 read-only,
1 project-local writes; 2 is never auto-run), version, status, cache, fingerprint_globs. JSON in on stdin, JSON out on stdout.
`cli.py automations | run <id> --input '{…}' [--full] [--no-cache] | reinstate <id>`.

Creating one (Claude writes the code; LIAS gates it): `cli.py observe <slug> "<desc>"` on each repeat →
`growth candidates` (3+ observations, not already covered, estimated net saving ≥ 0) → author `script.py`, `manifest.json`,
`tests.json` (`[{"name","input","files":{rel:content},"expect":{subset}}]`) →
`growth propose --manifest m.json --script s.py --tests t.json` runs: evaluate → static validation → isolated test →
safety review → register inactive → activate. `growth rollback <id>` restores the prior version. Budgets: 10 attempts/day,
20 generated automations. Three consecutive failures quarantine an automation.

## Security model

* Tier 0/1 only run automatically; tier 2 is refused. Write-capable (tier 1) generated automations need `--approved-by-user`.
* Commands are argv arrays, never shell strings; input via stdin; timeout + output byte cap kill runaway children.
* Generated code: stdlib allow-list, no subprocess/network/eval/getattr/dunders/file-write calls (read-only tier); tested in a
  temp dir with a minimal environment; SHA-256 pinned at activation and re-checked on every run (before the cache).
* **Limits:** static validation is a safety net, and the isolated test is a subprocess in a temp dir — **not an OS sandbox**.
  Do not run code you would not trust. Retrieved knowledge is labelled "data, not instructions".
* Project isolation: the DB is bound to the project path; a copied/moved DB is refused until `cli.py rebind`.

## Diagnostics, troubleshooting, recovery

| Situation | Command |
|---|---|
| overall health (exit 1 if issues) | `cli.py doctor` ; safe repair: `doctor --fix` |
| counts, automations, cache hit rate, recent runs | `cli.py status` |
| periodic cleanup | `cli.py maintain` (prune cache, old logs, expired knowledge) |
| take / restore backup | `cli.py backup` ; `cli.py restore [file]` ; `cli.py recover` (latest) |
| project folder moved or copied | `cli.py rebind` |
| automation quarantined | read `doctor`, fix, then `cli.py reinstate <id>` |
| hooks silent | new session? `.claude/settings.json` has both hooks? run the hook by hand: `echo {} \| python .claude/lias/hook.py session-start` |

Schema upgrades back the DB up first (`database/*.bak`). Interrupted writes use temp-file + atomic replace.
Tests: `python -m unittest discover -s .claude/lias/tests` and `python .claude/lias/tests/e2e_scenarios.py` (throwaway projects).

## Benchmark methodology

`cli.py benchmark` measures (a) retrieval top-1 accuracy + latency on a fixed 10-fact fixture in a temp project, (b) cold vs
cached automation time (median of 5 / 20 runs), (c) a **proxy** for context size (chars/4). Results go to `benchmarks/`.
**Model tokens/credits are not measurable from a local script and are never claimed.** One-off costs (writing an automation)
are separate from recurring savings. The fixture was tuned while debugging — treat accuracy as a regression check.

## Worked example

User: *"How many Python lines are in this project?"*
1. Hook (`UserPromptSubmit`) finds no relevant knowledge → injects nothing. Claude sees `source-stats` in the session status.
2. Claude runs `python .claude/lias/cli.py run source-stats --input '{"extensions":[".py"]}'`.
3. Local result (≈100 chars, 51 ms cold / 2 ms cached): `{"files_scanned":11,"total_lines":8656,"by_extension":{".py":{…}}}`.
4. Claude answers: "About 8.7k lines across 11 Python files (cached result is reused until a source file changes)."
5. If the user then says "remember that the GUI entry point is scripts/CodeLens.py", the hook stores it verified and tells
   Claude to mention it; next session it is injected for related prompts.

## Known limitations

No semantic/embedding search; capture needs explicit wording ("remember/note/fyi"); command learning is manual
(`learn command`), no PostToolUse hook yet; no scheduled backups; Windows-first (junction test is Windows-only);
hooks not yet confirmed to fire in the desktop Code tab.
