"""M5: docs/MCP_INTEGRATION.md stays in sync with the implementation."""

import os
import sys
import unittest

from agent_helpers import REPO, SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from codelens_agent.mcp_server import tools  # noqa: E402


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


class McpDocTests(unittest.TestCase):
    def test_every_tool_and_argument_is_documented(self):
        doc = _read("docs", "MCP_INTEGRATION.md")
        for name, t in tools.TOOLS.items():
            self.assertIn(f"`{name}`", doc, name)
            for arg in t["input"]["properties"]:
                self.assertIn(f"`{arg}`", doc, f"{name}.{arg}")

    def test_guide_links_to_mcp_doc(self):
        self.assertIn("MCP_INTEGRATION.md", _read("docs", "AI_AGENT_INTEGRATION.md"))

    def test_optional_requirements_file(self):
        req = _read("requirements-mcp.txt")
        self.assertIn("mcp", req)
        self.assertIn("requirements-mcp.txt", _read("docs", "MCP_INTEGRATION.md"))
        # the ordinary test requirements must stay MCP-free
        self.assertNotIn("mcp", _read("tests", "requirements-test.txt"))


if __name__ == "__main__":
    unittest.main()
