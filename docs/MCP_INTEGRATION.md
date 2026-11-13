# Code-Lens MCP Server (optional)

An optional [Model Context Protocol](https://modelcontextprotocol.io) server that exposes the
read-only Code-Lens agent interface to any MCP-compatible client. It is an **additional
transport**: the CLI + JSON interface ([AI_AGENT_INTEGRATION.md](AI_AGENT_INTEGRATION.md)) is
unchanged and does not need MCP.

- Transport: **stdio** only (no HTTP, no authentication)
- MCP SDK: official `mcp` Python SDK **2.3.x** (`mcp>=2.3,<3`); protocol versions served: the
  SDK's current version (2026-07-28) and the earlier handshake versions (2024-11-05 through
  2025-11-25), negotiated per client
- Code-Lens tool catalogue version: **1** · interface version: **1** · schema version: **1.0**
- Read-only. No tool writes, executes code, or accepts a root/path outside the permitted roots.

```
AI agent -> MCP client -> python -m codelens_agent.mcp_server -> ops -> search engine -> your files
                          (stdio, JSON-RPC)                       (the same code the CLI runs)
```

The adapter lives in `scripts/codelens_agent/mcp_server/`. It calls the existing
`codelens_agent.ops` operations with the existing `Sandbox`; there is no second search engine,
file reader, cursor implementation or sandbox.

## 1. Installation (optional dependency)

The CLI, the GUI and the interactive CLI never import the MCP SDK. Install it only if you want
the MCP server (Python 3.12+, as for the rest of the agent interface):

```bash
python -m pip install -r requirements-mcp.txt
```

(from the Code-Lens checkout; equivalent to `python -m pip install "mcp>=2.3,<3"`). Use a
virtual environment if you prefer to keep it separate. Without the SDK the server exits with
code 3 and an install hint on stderr; `python -m codelens_agent ...` is unaffected.

## 2. Launching the server

```bash
python -m codelens_agent.mcp_server --root /path/to/project [--root /another/dir] [--verbose]
```

`PYTHONPATH` must include the checkout's `scripts/` directory (or run from `scripts/`).
MCP clients start the server themselves; you normally never run it by hand.

| Option | Meaning |
|---|---|
| `--root DIR` | Permitted root; repeatable. **Default: the server's current directory** (a notice is printed on stderr). Relative tool paths resolve against the first root. |
| `--verbose` | Extra diagnostics on stderr. |
| `--check` | Validate roots and the SDK, report on stderr, exit 0. Does not start the server. |

stdout carries MCP messages only; everything else (including `--help` and `--check`) goes to
stderr. Shutdown: the client closes stdin and the server exits 0.

## 3. Configuring a client

The command to run is always: executable `python` (use the full path of the interpreter that has
the SDK installed), arguments `-m codelens_agent.mcp_server --root <dir>`, and environment
`PYTHONPATH=<checkout>/scripts`. Follow your client's own documentation for where to put it;
only the Claude Code format below has been checked against official documentation here.

**Claude Code** ([docs](https://code.claude.com/docs/en/mcp)) — `claude mcp add <name> -- <command> [args...]`:

```bash
claude mcp add --env PYTHONPATH=C:/path/to/Code-Lens/scripts --transport stdio codelens -- C:/path/to/python.exe -m codelens_agent.mcp_server --root C:/path/to/project
```

or a project `.mcp.json` (stdio servers use `command` and `args`):

```json
{
  "mcpServers": {
    "codelens": {
      "type": "stdio",
      "command": "C:/path/to/python.exe",
      "args": ["-m", "codelens_agent.mcp_server", "--root", "C:/path/to/project"],
      "env": {"PYTHONPATH": "C:/path/to/Code-Lens/scripts"}
    }
  }
}
```

**Codex, Gemini CLI and other clients:** each documents its own MCP server configuration
(a command, arguments and environment for a stdio server). Use the command above in the format
that client documents; this guide does not reproduce formats it has not verified.

### Permitted roots

Roots come **only** from `--root` on the server command line. Roots, paths or filesystem
capabilities offered by the client are not used, and the server never asks the client for roots.
Pass one `--root` per directory the agent may read, and nothing broader than necessary.

### Verifying the connection

1. `python -m codelens_agent.mcp_server --root <dir> --check` prints
   `codelens-mcp: OK. mcp SDK <version>; roots: ...` on stderr.
2. In Claude Code run `claude mcp list` or `/mcp` inside a session; `codelens` should show as
   connected with four tools.
3. Ask the agent to call `get_capabilities`; `data.roots` lists exactly the permitted roots.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Client says the server failed to start / connection closed | Run the exact command in a terminal. Exit **3**: SDK missing (`pip install -r requirements-mcp.txt` into *that* interpreter). Exit **2**: bad `--root` (must be an existing directory) or unknown flag. `No module named codelens_agent`: `PYTHONPATH` must point at `scripts/`. |
| Tools are listed but every path is refused (`path_outside_root`) | The path is outside every `--root`. Add the root to the server command; symlinks/junctions are judged by their resolved target. |
| Searches see the wrong directory | No `--root` was given, so the client's working directory is the only root. Pass `--root`. |
| Client times out on first call | Large trees: a search is bounded by `limits.search_time_budget_seconds`. Narrow `paths`/`extensions`. |
| Garbled or missing responses | Something wrote to stdout. The server redirects stray `print()` to stderr, but a wrapper script that prints before starting Python will corrupt the stream. Use `--verbose` and read stderr (the client's MCP log). |

## 4. Tools

All tools are read-only (`readOnlyHint: true`, `destructiveHint: false`) and reject unknown
arguments. Argument values are validated by the same code as the CLI, with the same error codes.

### `get_capabilities`
No arguments. Returns the CLI `capabilities` payload (tool/interface/schema versions, commands,
search modes, limits, pagination, error codes, roots, limitations) plus a `data.mcp` block
(tool list, pagination instructions, MCP-specific limitations). The `mcp_server` entry is removed
from `unsupported` over MCP.

### `search_code`
| Argument | Type | Notes |
|---|---|---|
| `query` | string, required | Text, not a regex. ≤ 10000 chars. |
| `paths` | string[] | Files/directories, recursive. Default: first root. ≤ 100. |
| `mode` | `flexible` \| `literal` \| `word` | Default `flexible`. |
| `multiline` | boolean | Required if the query contains a newline. |
| `case_sensitive` | boolean | Default false. |
| `extensions` | string[] | e.g. `["py", ".js"]` (CLI flag: `--ext`). |
| `include`, `exclude` | string[] | Globs. |
| `context` | integer 0–20 | Context lines per side. |
| `max_results` / `page_size` | integer 1–1000 | Give only one. |
| `cursor` | string | `data.next_cursor` of the previous page of the same search. |
| `count_only` / `files_only` | boolean | Mutually exclusive output modes. |
| `comment_fallback` | boolean | Default true (flexible multiline only). |

### `read_file`
`file` (required), `lines` (string[] of `"N"` or `"START-END"`, ≤ 50, 1-based), `around`
(integer), `context` (0–200, requires `around`). `lines` and `around` are mutually exclusive.
Output is capped at 2000 lines; `ranges[*].next_start` says where to continue.

### `get_file_info`
`file` (required). Size, modified time, text/binary, encoding, line count, line endings.

### Pagination — no separate `get_next_page` tool
A cursor is bound to the full set of search arguments (query, mode, paths, filters, …), so a
`get_next_page(cursor)` tool would have to repeat all of them. Call `search_code` again with
**identical arguments plus `cursor`**; the existing cursor validation runs unchanged
(`invalid_cursor` for malformed/foreign cursors, `stale_cursor` if searched files changed).

## 5. Output format

Every result carries the unchanged v1 envelope ([schema](schema/codelens-agent-v1.schema.json)):

- `structuredContent` — the envelope object (tools declare a loose `outputSchema`).
- `content[0].text` — the same envelope as compact JSON, so clients that forward only `content`
  to the model lose nothing (warnings, `next_cursor`, `more_results`, `truncated`, `strategy`).
- `isError` — `true` **only** when `success` is `false`. Zero matches is `success: true`,
  `isError: false`.

Failures keep the Code-Lens error contract (`errors[0]`: `code`, `message`, `path`, `hint`,
`retryable`), e.g. `path_outside_root`, `file_too_large`, `limit_exceeded`, `invalid_cursor`,
`stale_cursor`. Malformed JSON-RPC and unknown methods get standard JSON-RPC errors; malformed
lines are ignored and the server keeps running.

Example — `search_code {"query":"foo","extensions":["py"],"page_size":1}` (abridged):

```json
{"success":true,"operation":"search","data":{"results":[{"file":"a.py","matches":[{"line":1,"column":5,"end_column":7,"content":"def foo():","strategy":"as_typed"}]}],"total_matches":1,"truncated":true,"truncated_reason":"max_results","more_results":"confirmed","complete":false,"next_cursor":"<opaque>"},"warnings":[],"errors":[]}
```

Example — zero matches (`isError: false`):

```json
{"success":true,"operation":"search","data":{"results":[],"total_matches":0,"more_results":"none","complete":true,"next_cursor":null},"warnings":[],"errors":[]}
```

Example — refused path (`isError: true`):

```json
{"success":false,"operation":"read","data":null,"warnings":[],"errors":[{"code":"path_outside_root","message":"Path is outside the permitted roots.","path":"../x","hint":"Pass --root <dir> to permit a directory; symlinks are checked by their resolved target.","retryable":false}]}
```

## 6. Security model

The MCP server gets **no filesystem permission the CLI lacks**; it runs the CLI's own code.

- **Roots:** fixed by the server operator via `--root`; a fresh `Sandbox` is built per call from
  that list. Client-supplied roots/paths/capabilities are never trusted or requested.
- **Paths:** every tool path is resolved to a real path and must lie inside a root (`..`,
  absolute paths elsewhere, other drives/UNC and symlinks pointing out are refused).
- **Junctions/symlinks:** directory links are not walked during search; explicit paths through
  them are checked by resolved target.
- **Limits:** file size (5 MiB), read lines/ranges, query/paths/context/results caps, per-search
  file/byte/time budgets — all enforced by the shared `ops`/`sandbox` code. At most 4 operations
  run concurrently; cancelling a request does not interrupt a running search, which ends within
  its time budget.
- **Cursors:** validated by the existing stateless cursor code; they carry no paths.
- **Read-only:** no write tools; nothing opens files for writing; no source is executed.
- **Stdout integrity:** the protocol uses the real stdout buffer; `sys.stdout` is pointed at
  stderr so stray prints cannot corrupt it. (A native extension writing directly to file
  descriptor 1 is out of scope; Code-Lens has none.)
- **Argument names:** the MCP schema forbids extra properties; `root`, `ext` and any unknown
  name are rejected as `invalid_argument`.

## 7. Known limitations

- stdio only; no HTTP transport, authentication, resources, prompts or tasks.
- Text is returned as JSON (no prose summary), prioritising losslessness over brevity; use
  `count_only`/`files_only`/`page_size` to bound output.
- Not packaged in the frozen executables; run from a source checkout.
- Developed and tested against MCP SDK 2.3.0 only (`mcp>=2.3,<3`); the 1.x line has a different
  server API and was not evaluated for use.
- The CLI's own `capabilities` command still lists `mcp_server` under `unsupported` (it describes
  the CLI process); `get_capabilities` over MCP corrects this.
- Tests: `tests/test_mcp_*.py`. SDK-dependent tests are skipped when `mcp` is not installed.
