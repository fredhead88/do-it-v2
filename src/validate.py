#!/usr/bin/env python3
"""validate — check one role's Output object against its schema, before it is returned.

  validate.py <role> <file>      exit 0 and VALID, or exit 1 with the first violation

On the `claude -p` route the CLI's StructuredOutput tool enforces `--json-schema` and
the model retries until it complies. On the seat route there is no such tool, and a
model cannot count characters: the first seat spawn needed four rounds to land a
`maxLength: 400` finding. This is the seat route's StructuredOutput — a contract
running as a sub-agent writes its object to `$R/seat/<spawn>.output.json` and runs
this until it prints VALID.
"""
import json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HERE.parent / "agents"


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: doit validate <role> <file>")
    role, path = argv
    schema = AGENTS / f"{role}.schema.json"
    if not schema.is_file():
        sys.exit(f"validate: no schema for {role} at {schema}")
    try:
        import jsonschema
    except ImportError:
        sys.exit("validate: jsonschema is not importable — unchecked is never clean")
    try:
        obj = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"validate: {path}: {e}")
    try:
        jsonschema.validate(obj, json.loads(schema.read_text()))
    except jsonschema.ValidationError as e:
        where = "/".join(str(x) for x in e.absolute_path) or "<root>"
        sys.exit(f"INVALID at {where}: {e.message[:300]}")
    lens = {k: len(v) for k, v in obj.items() if isinstance(v, str)}
    print(f"VALID {role} {path}" + (f" · string lengths {lens}" if lens else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
