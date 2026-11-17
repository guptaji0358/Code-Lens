# Build, run, and test workflow

Verified on 2026-11-17 against the repo files named below.

## Run from source

```bash
python scripts/CodeLensCLI.py            # terminal edition, stdlib (colorama optional)
pip install PySide6 && python scripts/CodeLens.py   # desktop GUI
python scripts/installer_app.py          # install wizard (PySide6)
python scripts/installer_app.py --uninstall
python scripts/codelens_agent capabilities   # agent JSON interface, stdlib only
```

## Build pipeline (verified)

**Default path for builds:** the LIAS automation `build-release` or `build_and_run.bat`. Do not
hand-drive PyInstaller or makensis. Details below.

- `build_and_run.bat` (repo root) runs `python scripts\build_release.py %*`. Flags:
  `--force`, `--no-installer`, `--no-launch`.
- `scripts/build_release.py` reuses the existing `specs/*.spec` files and `CodeLensSetup.nsi`.
  It defines no new build steps. Outputs:
  - `out/CodeLens/`, `out/CodeLensCLI/`, `out/CodeLens_Uninstall.exe`, the three app builds.
  - `dist/CodeLens Setup/`, the installer wizard (onedir).
  - `dist/CodeLens_Setup.exe`, the NSIS bootstrap, only if `makensis` is found.
- It is self-skipping. A source/spec fingerprint is stored at `build/.fingerprint`.
- Spec files: `specs/CodeLens.spec`, `specs/CodeLensCLI.spec`, `specs/CodeLens Setup.spec`,
  `specs/CodeLens_Uninstall.spec`. The Setup spec bundles the `out/` builds as its payload, so
  order matters.
- `build/`, `dist/`, `dist_tmp/`, `build_tmp/`, and `out/` are git-ignored.

**Discrepancy:** `.claude/CLAUDE.md` says PyInstaller packaging is an "external" step outside the
repo. The repo does ship `build_release.py` and `build_and_run.bat`. Trust the scripts. Confirm
with the user before assuming either is stale.

## Tests

```bash
pip install -r tests/requirements-test.txt     # jsonschema; run FAILS without it, does not skip
python -m unittest discover -s tests            # ~110 s, includes performance tests
python -m unittest discover -s tests -p "test_agent_schema.py"   # one file
python -m py_compile scripts/<file>.py          # syntax check after editing (no lint config)
```

- About 20 test modules in `tests/` (`test_agent_*`, `test_mcp_*`, `test_command_reference.py`,
  `test_gui_layout.py`, `test_gui_auto_select.py`). Helpers: `agent_helpers.py`, `mcp_helpers.py`,
  `doc_fixture.py`.
- GUI behaviour has only partial automated coverage. CLAUDE.md says the GUI and interactive CLI
  have no automated tests. Some `test_gui_*` files exist, so that statement is stale or partial.
- The optional `mcp` SDK is needed for SDK-dependent MCP tests (`requirements-mcp.txt`). They skip
  without it.

## Persisted runtime files

- `shortcuts.json` and `recent_folders.json` live next to the running script or exe (`BASE_DIR` in
  `theme.py`). `recent_folders.json` stores folder paths only.
- Crash logs go to `%LOCALAPPDATA%\CodeLens_crash.log`.

## LIAS (project-local automation)

- Hooks in `.claude/settings.json`: `SessionStart` runs `.claude/lias/hook.py session-start`;
  `UserPromptSubmit` runs `hook.py prompt`. Both are configured by the project.
- Runtime: `.claude/lias/` (stdlib Python, SQLite). Tests: `python -m unittest discover -s .claude/lias/tests`.
- Automations are under `.claude/lias/automation/`. The `build-release` automation maps to the
  build script above.
- Persistent project knowledge is held in LIAS (`cli.py knowledge search`), separate from this memory.

## Git state at initialization

- Branch `feat`, main branch `main`. Uncommitted edits exist in `scripts/dialogs.py`,
  `main_window.py`, `persistence.py`, `widgets.py`, plus untracked `.claude/Skills/`,
  `screenshots_auto_select/`, and `tests/test_gui_auto_select.py`. This memory describes committed
  structure and does not record in-progress work.
