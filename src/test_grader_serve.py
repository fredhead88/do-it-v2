#!/usr/bin/env python3
"""Regression coverage for the validator retained by pane-end --grader."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import grader_serve


class ValidOutputTests(unittest.TestCase):
    def test_validation_and_read_failures(self):
        with tempfile.TemporaryDirectory(prefix="grader-output-") as tmp:
            path = Path(tmp) / "output.json"
            schema = Path(tmp) / "schema.json"
            schema.write_text(json.dumps({"type": "object", "required": ["ok"],
                                          "properties": {"ok": {"const": True}}}))
            self.assertFalse(grader_serve.valid_output(path, schema))
            for value, valid in [("", False), ("{", False), ("{}", False), ('{"ok": true}', True)]:
                path.write_text(value)
                self.assertEqual(grader_serve.valid_output(path, schema), valid)
            self.assertFalse(grader_serve.valid_output(path, Path(tmp) / "missing"))
            with patch.dict("sys.modules", {"jsonschema": None}):
                self.assertFalse(grader_serve.valid_output(path, schema))
        self.assertEqual([n for n, v in vars(grader_serve).items() if callable(v)], ["valid_output"])


if __name__ == "__main__":
    unittest.main()
