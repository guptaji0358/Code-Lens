"""
codelens_agent.mcp_server - optional MCP (Model Context Protocol) front-end
for the read-only CodeLens agent interface.

Layout:
  tools.py   tool catalogue + call handling. Pure Python: it never imports the
             MCP SDK, so it loads (and is testable) without the optional
             dependency.
  server.py  the only module that imports the MCP SDK (lazily).
  __main__.py  `python -m codelens_agent.mcp_server [--root DIR]...`

Importing this package must stay free of the MCP SDK and of any side effects:
the ordinary CLI (`python -m codelens_agent`) never imports it.
"""

# Version of the MCP tool catalogue (names, argument names, result shape).
# Independent of codelens_agent.INTERFACE_VERSION, which it does not change.
MCP_TOOLS_VERSION = "1"
