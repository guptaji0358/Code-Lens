# AI-agent interface

Verified against `scripts/codelens_agent/` on 2026-11-17. Public docs:
`docs/AI_AGENT_INTEGRATION.md`, `docs/MCP_INTEGRATION.md`. Schema:
`docs/schema/codelens-agent-v1.schema.json`.

## Purpose and contract

- Read-only, provider-independent JSON CLI over the shared core. Stdlib only.
- Entry: `python scripts/codelens_agent <command>` (run from any directory), or
  `python -m codelens_agent` with `scripts/` or `PYTHONPATH=scripts`.
- **Interface version 1 is frozen.** Only additive changes are allowed.
- `tests/test_agent_schema.py` fails on any output field not documented in the schema. When you
  change output fields, update the schema first.

## Modules

| Module | Role |
|--------|------|
| `cli.py` | argv parsing and `--request` JSON handling. |
| `ops.py` | Validation helpers, the command catalogue `COMMANDS` (`capabilities`, `info`, `read`, `search`), and operations `op_*`. |
| `search.py` | Search operation logic over the core. |
| `sandbox.py` | `Sandbox` class: allowed roots and limits (`limits()`). |
| `cursor.py` | Stateless pagination cursors. |
| `protocol.py` | Envelope shape, error types (`AgentError`), and exit codes. |
| `engine.py` | Adapter to `CodeLensCLI.py`: `core()`, `load_text`, `stat_file`, `split_lines`, `detect_line_ending`. Uses `core.is_text_file`. |
| `reference.py` | Builds the GUI "AI Command Reference" catalogue by introspecting the argparse parser in `cli.py`, `mcp_server/tools.py` `TOOLS`, and `ops.COMMANDS`. |
| `mcp_server/` | Optional stdio MCP front-end (see below). |

Do not hand-write command info for the GUI reference. Only `EXAMPLE_ARGS` values are authored
by hand.

## Commands

- `capabilities`, `info`, `read`, `search`. `op_capabilities`, `op_info`, `op_read`, and
  `op_search` are in `ops.py`.
- `ops.py` is the sole validator. The MCP layer must not bypass it.

## MCP server (optional)

- `mcp_server/tools.py`: tool catalogue `TOOLS`, with no SDK import. Current tools are
  `get_capabilities`, `search_code`, `read_file`, `get_file_info`.
- `mcp_server/server.py`: the only module that imports the `mcp` SDK. It uses the low-level
  `Server` class. `build_server(roots)` and `run_stdio(roots, verbose)`.
- The SDK is optional: `pip install -r requirements-mcp.txt` (`mcp>=2.3,<3`).
- The CLI must never import the SDK.
- Stdio transport only, no auth. Three exit-code namespaces are documented in `MCP_INTEGRATION.md`.
  Keep them separate.
- Protocol-version coverage: `initialize` negotiation is tested. The 2026-07-28 / `server/discover`
  path is manually verified only, per `MCP_INTEGRATION.md`.

## Tests covering this area

- `tests/test_agent_*.py` (schema, search, read, pagination, cursor security, search safety,
  performance, docs, usability, capabilities info).
- `tests/test_mcp_*.py` (server, stdio, tools, docs). SDK-dependent tests skip without `mcp`.
- `tests/test_command_reference.py` (GUI part skips without PySide6).
- `tests/test_agent_docs.py` executes the `bash verify` blocks in the agent guide.

## Working rules

- Read `docs/AI_AGENT_INTEGRATION.md` before changing any command or output shape.
- Keep agent output consistent with the core: any search semantics change happens in
  `CodeLensCLI.py` and is picked up here through `engine.py`.
