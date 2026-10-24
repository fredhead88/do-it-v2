#!/usr/bin/env python3
"""Output validation retained for the legacy pane-end --grader consumer."""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def valid_output(path, schema_path=None):
    """True iff `path` is a non-empty, well-formed JSON file that validates
    against `schema_path` (default `agents/grader.schema.json`) — the ONE
    schema-validity check retained for `pane_end.check_and_end_grader`. Any
    failure — missing file, malformed JSON, a schema miss, or `jsonschema`
    itself unimportable — reads False, never raised."""
    path = pathlib.Path(path)
    schema_path = pathlib.Path(schema_path) if schema_path else (HERE.parent / "agents" / "grader.schema.json")
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    try:
        import jsonschema
        jsonschema.validate(obj, json.loads(schema_path.read_text()))
    except Exception:
        return False
    return True
