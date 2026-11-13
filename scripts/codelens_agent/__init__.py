"""
codelens_agent - provider-independent, machine-readable command interface
to the CodeLens search engine, for AI agents and other programs.

Read-only by design. Run with:  python -m codelens_agent <command> [args]
"""

# Bumped only on breaking changes to command names/semantics. Additive
# changes (new commands, new optional args, new response fields) keep it.
INTERFACE_VERSION = "1"

# Version of the JSON response envelope / data shapes.
SCHEMA_VERSION = "1.0"

TOOL_NAME = "codelens-agent"

# CodeLens release this interface ships with (mirrors scripts/app.py's
# APP_VERSION; not imported from there because app.py needs PySide6).
TOOL_VERSION = "2.0.0"
