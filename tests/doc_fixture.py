"""The small project that docs/AI_AGENT_INTEGRATION.md uses in its examples.

Both the documentation-example tests and the schema tests build it, so every
documented command can be re-run for real.
"""

import os

FILES = {
    "src/app.py": (
        "import json\n"
        "\n"
        "\n"
        "def parse_config(path):\n"
        "    with open(path, encoding=\"utf-8\") as fh:\n"
        "        return json.load(fh)\n"
        "\n"
        "\n"
        "def main():\n"
        "    config = parse_config(\"config.json\")\n"
        "    print(config[\"name\"])\n"
    ),
    "src/utils.py": (
        "from app import parse_config\n"
        "\n"
        "\n"
        "def load_defaults():\n"
        "    return parse_config(\"defaults.json\")\n"
        "\n"
        "\n"
        "def load_user():\n"
        "    return parse_config(\"user.json\")\n"
    ),
    "tests/test_app.py": (
        "from app import parse_config\n"
        "\n"
        "\n"
        "def test_parse_config():\n"
        "    assert parse_config(\"fixture.json\")\n"
    ),
    "README.md": "# Demo\n\nCall parse_config() to load settings.\n",
}


def build(root):
    """Writes the fixture project into `root` (an existing directory)."""
    for rel, text in FILES.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    with open(os.path.join(root, "logo.bin"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + bytes(range(256)))
    return root
