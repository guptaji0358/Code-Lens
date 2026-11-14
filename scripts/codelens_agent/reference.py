"""
reference.py - the AI Command Reference catalogue (data only, no GUI).

Everything here is DERIVED from the live implementations so it cannot drift:
  * CLI entries   <- the argparse parser in cli.py (flags, positionals, types,
                     choices, repeatability) + ops.COMMANDS (summary, defaults,
                     argument descriptions).
  * MCP entries   <- mcp_server.tools.TOOLS (descriptions + JSON input schemas,
                     argument renames) + ops.COMMANDS (defaults).
  * Output shapes <- a REAL run of each operation against a tiny sample file
                     (shape = key names and value types), not hand-written text.
  * Permission    <- ops.op_capabilities (`read_only`) and ops.UNSUPPORTED.
The only authored data is EXAMPLE_ARGS (argument VALUES for one realistic call
per operation); tests execute every example through the real operations.

Pure stdlib; imports neither the MCP SDK nor any GUI code.
"""

import argparse
import json
import os
import subprocess
import tempfile

from . import cli, ops
from .mcp_server import tools as mcp_tools
from .sandbox import Sandbox

CLI = "cli"
MCP = "mcp"
CATEGORY_TITLES = {MCP: "MCP Tools", CLI: "CLI Commands"}

CLI_PREFIX = "python -m codelens_agent"
MCP_SERVER_COMMAND = "python -m codelens_agent.mcp_server"

# One realistic call per operation, as agent-operation argument values (CLI
# names). Positional/flag/JSON renderings are derived from these.
EXAMPLE_ARGS = {
    "capabilities": {},
    "info": {"file": "scripts/CodeLensCLI.py"},
    "read": {"file": "scripts/CodeLensCLI.py", "around": 120, "context": 3},
    "search": {"query": "def read_file_smart", "paths": ["scripts"], "ext": ["py"],
               "mode": "literal", "context": 1, "max_results": 20},
}

# Sample used to observe real output shapes.
_SAMPLE_NAME = "sample.txt"
_SAMPLE_TEXT = "alpha\ndef main():\n    return 1\n"
_SAMPLE_ARGS = {
    "capabilities": {},
    "info": {"file": _SAMPLE_NAME},
    "read": {"file": _SAMPLE_NAME, "lines": ["1-2"]},
    "search": {"query": "main", "paths": [_SAMPLE_NAME], "context": 1},
}
_CAPS_DEPTH = 1   # capabilities is huge: show top-level keys only


# ---- helpers ------------------------------------------------------------------

def _op_arg_meta(operation):
    return {a["name"]: a for a in ops.COMMANDS[operation]["args"]}


def _title(identifier):
    return identifier.replace("_", " ").replace("-", " ").title()


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _shape(value, depth):
    """JSON-like structure with values replaced by their type names."""
    if isinstance(value, dict):
        if depth <= 0:
            return "object"
        return {k: _shape(v, depth - 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape(value[0], depth - 1)] if depth > 0 else "array"
    return _type_name(value)


def permission_for(operation):
    """('read-only' | ..., explanation) from the implementation's own flags."""
    with tempfile.TemporaryDirectory() as tmp:
        data, _ = ops.OPERATIONS["capabilities"](Sandbox([tmp]), {})
    if data.get("read_only") is True and "write" in data.get("unsupported", ()):
        return ("Read-only",
                "Never modifies files; paths are limited to the permitted roots "
                "(--root).")
    return ("Unspecified", "")


_shape_cache = {}


def output_shape(operation):
    """Observed response envelope shape for `operation` (cached)."""
    if operation in _shape_cache:
        return _shape_cache[operation]
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.realpath(tmp)
        with open(os.path.join(root, _SAMPLE_NAME), "w", encoding="utf-8", newline="") as fh:
            fh.write(_SAMPLE_TEXT)
        sandbox = Sandbox([root])
        data, warnings = ops.OPERATIONS[operation](sandbox, dict(_SAMPLE_ARGS[operation]))
    from .protocol import success_envelope
    env = success_envelope(operation, data, warnings)
    depth = _CAPS_DEPTH if operation == "capabilities" else 6
    shaped = {k: (_shape(v, depth) if k == "data" else _shape(v, 1))
              for k, v in env.items()}
    _shape_cache[operation] = shaped
    return shaped


def _output_text(operation):
    body = json.dumps(output_shape(operation), indent=2)
    note = ("Shape observed from a real run (values replaced by their types). "
            "'null' means the field was unset in that run and may hold a value. "
            "Failures return success=false, data=null and an errors list.")
    return body + "\n\n" + note


# ---- CLI entries (argparse introspection) ----------------------------------------

def _subparsers():
    top = cli._build_parser()
    for action in top._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise RuntimeError("codelens_agent CLI has no subcommands")


def _cli_arg_from_action(action, meta):
    positional = not action.option_strings
    store_bool = isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction))
    if store_bool:
        type_ = "boolean"
    elif action.type is int:
        type_ = "integer"
    else:
        type_ = "string"
    repeatable = (isinstance(action, argparse._AppendAction)
                  or action.nargs in ("*", "+"))
    required = bool(action.required) if not positional else action.nargs in (None, "+")
    choices = list(action.choices) if action.choices else None
    name = action.dest
    if positional:
        form = name.upper()
        form = f"[{form}...]" if action.nargs == "*" else form
    else:
        flag = action.option_strings[0]
        if store_bool:
            form = flag
        else:
            value = ("{" + "|".join(choices) + "}") if choices else \
                    (action.metavar or name.upper())
            form = f"{flag} {value}"
        if not required:
            form = f"[{form}]"
        if repeatable:
            form += "..."
    return {
        "name": name, "flag": None if positional else action.option_strings[0],
        "positional": positional, "form": form, "type": type_,
        "required": required, "repeatable": repeatable, "choices": choices,
        "default": meta.get(name, {}).get("default"),
        "description": meta.get(name, {}).get("description", ""),
        "store_false": isinstance(action, argparse._StoreFalseAction),
        "action": action,
    }


def _cli_example(operation, args_meta, example_args):
    parts = [CLI_PREFIX, operation]
    positionals = [a for a in args_meta if a["positional"]]
    flags = [a for a in args_meta if not a["positional"]]
    for a in positionals:
        v = example_args.get(a["name"])
        if v is None:
            continue
        parts.extend(v if isinstance(v, list) else [v])
    for a in flags:
        if a["name"] not in example_args:
            continue
        v = example_args[a["name"]]
        if a["type"] == "boolean":
            if bool(v) != a["store_false"]:
                parts.append(a["flag"])
            continue
        for item in (v if isinstance(v, list) else [v]):
            parts.extend([a["flag"], str(item)])
    head, rest = parts[:2], parts[2:]
    return " ".join(head) + (" " + subprocess.list2cmdline([str(p) for p in rest])
                             if rest else "")


def cli_entries():
    entries = []
    for name, sub in _subparsers().items():
        meta = _op_arg_meta(name)
        args = [_cli_arg_from_action(a, meta) for a in sub._actions
                if not isinstance(a, argparse._HelpAction)]
        args.sort(key=lambda a: (not a["positional"],))   # stable: positionals first
        syntax = " ".join([CLI_PREFIX + " [--root DIR] [--verbose]", name]
                          + [a["form"] for a in args])
        perm, perm_note = permission_for(name)
        entries.append({
            "id": f"{CLI}:{name}", "kind": CLI, "label": _title(name),
            "identifier": name, "operation": name, "syntax": syntax,
            "summary": ops.COMMANDS[name]["summary"],
            "args": [{k: v for k, v in a.items() if k != "action"} for a in args],
            "example": _cli_example(name, args, EXAMPLE_ARGS[name]),
            "output": _output_text(name),
            "permission": perm, "permission_note": perm_note,
            "notes": ["Global options (any position): --root DIR (repeatable, default "
                      "current directory), --verbose, --request SRC (JSON request; "
                      "cannot be combined with a command)."],
        })
    return entries


# ---- MCP entries (tool schemas) -----------------------------------------------

def _mcp_arg(name, schema, required, op_meta, rename):
    op_name = rename.get(name, name)
    t = schema.get("type")
    if t == "array":
        type_, repeatable = "array of " + schema.get("items", {}).get("type", "string"), True
    else:
        type_, repeatable = t or "string", False
    bounds = {k: schema[k] for k in ("minimum", "maximum", "minLength", "maxLength",
                                     "maxItems") if k in schema}
    return {
        "name": name, "operation_arg": op_name, "type": type_, "required": required,
        "repeatable": repeatable, "choices": schema.get("enum"), "bounds": bounds,
        "default": op_meta.get(op_name, {}).get("default"),
        "description": schema.get("description", ""),
    }


def _mcp_example(tool_name, operation, tool):
    rename = tool["rename"]
    back = {v: k for k, v in rename.items()}
    args = {back.get(k, k): v for k, v in EXAMPLE_ARGS[operation].items()}
    return json.dumps({"name": tool_name, "arguments": args}, indent=2)


def mcp_entries():
    entries = []
    for name, tool in mcp_tools.TOOLS.items():
        operation = tool["operation"]
        schema = tool["input"]
        meta = _op_arg_meta(operation)
        req = set(schema["required"])
        args = [_mcp_arg(n, s, n in req, meta, tool["rename"])
                for n, s in schema["properties"].items()]
        args.sort(key=lambda a: not a["required"])
        if args:
            fields = ", ".join(('"%s": <%s>' % (a["name"], a["type"])) if a["required"]
                               else ('"%s"?: <%s>' % (a["name"], a["type"])) for a in args)
            syntax = "%s { %s }" % (name, fields)
        else:
            syntax = "%s {}" % name
        perm, perm_note = permission_for(operation)
        entries.append({
            "id": f"{MCP}:{name}", "kind": MCP, "label": _title(name),
            "identifier": name, "operation": operation, "syntax": syntax,
            "summary": tool["description"], "args": args,
            "example": _mcp_example(name, operation, tool),
            "output": _output_text(operation),
            "permission": perm, "permission_note": perm_note,
            "notes": ["Invoked by an MCP client over stdio against a server started "
                      f"with: {MCP_SERVER_COMMAND} [--root DIR]...",
                      "Roots come from the server command line; the client cannot "
                      "change them. The result is the same envelope the CLI prints."]
                     + (["MCP argument names that differ from the CLI: "
                         + ", ".join(f"{k} -> {v}" for k, v in tool["rename"].items())]
                        if tool["rename"] else []),
        })
    return entries


# ---- catalogue / filtering / copy ------------------------------------------------

def _search_text(e):
    bits = [e["label"], e["identifier"], e["summary"], e["syntax"], e["permission"]]
    bits += [a["name"] + " " + a["description"] for a in e["args"]]
    return " ".join(bits).lower()


def build_catalogue():
    """All entries, MCP first. Computed from the live implementation."""
    entries = mcp_entries() + cli_entries()
    for e in entries:
        e["search_text"] = _search_text(e)
    return entries


def filter_entries(entries, query="", kind=None):
    """Case-insensitive AND-match of every whitespace-separated term."""
    terms = (query or "").lower().split()
    return [e for e in entries
            if (kind is None or e["kind"] == kind)
            and all(t in e["search_text"] for t in terms)]


def copy_text(entry, what="syntax"):
    """Text the GUI puts on the clipboard: 'syntax' or 'example'."""
    if what not in ("syntax", "example"):
        raise ValueError("what must be 'syntax' or 'example'")
    return entry[what]
