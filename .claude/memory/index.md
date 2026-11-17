# CodeLens project memory — index

Navigation map for future sessions. Current source and config are authoritative; if a memory
file disagrees with the code, trust the code and fix the memory.

## Files

| File | Covers | Read when |
|------|--------|-----------|
| [architecture.md](architecture.md) | Core vs GUI split, state model, async worker pattern, live FS sync, file-to-file responsibilities | Touching loading, search, reload, the GUI, or adding a widget/dialog |
| [agent-interface.md](agent-interface.md) | `scripts/codelens_agent/` JSON CLI, sandbox, cursors, optional MCP server, frozen schema | Changing agent output, commands, MCP tools, or docs |
| [build-test-workflow.md](build-test-workflow.md) | Run commands, PyInstaller/NSIS build pipeline, test suite and its dependencies, LIAS hooks | Running, building, testing, or releasing |

## Common task routes

- **Change search/loading/reload semantics** → `architecture.md` (edit `scripts/CodeLensCLI.py` only),
  then `agent-interface.md` if agent output changes, then `build-test-workflow.md` for tests.
- **Change GUI behaviour** → `architecture.md` (GUI section) → the matching file under `scripts/`
  (`main_window.py` for `FinderWindow` methods; `widgets.py` / `dialogs.py` / `theme.py` otherwise).
- **Change agent CLI / MCP output fields** → `agent-interface.md`; update
  `docs/schema/codelens-agent-v1.schema.json` (the schema test fails on undocumented fields).
- **Build or release** → `build-test-workflow.md`.
- **Install-time / Explorer integration** → `architecture.md` (Windows integration section).

## Relationships

- `architecture.md` describes the GUI and core that `agent-interface.md` reuses unmodified via
  `codelens_agent/engine.py`, so core changes can affect both.
- `build-test-workflow.md` lists the tests that cover the agent interface; GUI tests are partial.

## Maintenance

After a change that alters modules, responsibilities, commands, build steps, or conventions,
update the smallest affected file and this index. Keep each file ≤ 200 lines.
