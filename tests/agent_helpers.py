"""Shared helpers: run `python -m codelens_agent` as a real external process."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

ENVELOPE_KEYS = {"schema_version", "interface_version", "tool", "success",
                 "operation", "data", "warnings", "errors"}
ERROR_KEYS = {"code", "message", "path", "hint", "retryable"}


class Result:
    def __init__(self, proc):
        self.code = proc.returncode
        self.raw_out = proc.stdout
        self.err = proc.stderr.decode("utf-8", errors="replace")
        text = proc.stdout.decode("utf-8")
        self.json = json.loads(text)  # stdout MUST be exactly one JSON doc
        self.text = text

    @property
    def data(self):
        return self.json["data"]

    @property
    def error(self):
        return self.json["errors"][0]


def run_agent(*argv, stdin=None, cwd=None):
    env = dict(os.environ, PYTHONPATH=SCRIPTS, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "codelens_agent", *argv],
        input=stdin, capture_output=True, cwd=cwd or SCRIPTS, env=env, timeout=60)
    return Result(proc)


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)

    def write(self, name, content, mode="w", encoding="utf-8", newline=""):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if "b" in mode:
            with open(path, mode) as fh:
                fh.write(content)
        else:
            with open(path, mode, encoding=encoding, newline=newline) as fh:
                fh.write(content)
        return path

    def agent(self, *argv, **kw):
        return run_agent("--root", self.root, *argv, cwd=self.root, **kw)

    def assert_envelope(self, res, success, operation):
        self.assertEqual(set(res.json), ENVELOPE_KEYS)
        self.assertEqual(res.json["success"], success)
        self.assertEqual(res.json["operation"], operation)
        self.assertEqual(res.json["schema_version"], "1.0")
        self.assertEqual(res.json["interface_version"], "1")
        self.assertEqual(res.text.count("\n"), 1, "stdout must be one JSON line")
        if success:
            self.assertEqual(res.json["errors"], [])
            self.assertEqual(res.code, 0)
        else:
            self.assertIsNone(res.json["data"])
            self.assertEqual(set(res.error), ERROR_KEYS)
