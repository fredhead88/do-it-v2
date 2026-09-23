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
import json, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HERE.parent / "agents"


def spec_shape(spec_text):
    """L-spec-0195/R3, AC1: a spec the tools cannot read is bounced BEFORE a builder
    is ever dispatched against it, never discovered only at dispatch time
    (`fold.py`'s void state). Pure, text-only — composes the same pure checks
    `packet.verify_script`/`merge_gate.grant_from_text` already perform, in three
    independently labelled buckets, and NEVER `packet.verify_script(c)` itself
    (which needs a packet `Ctx`, side-effects, and would `die()` reachable through a
    `git merge-base` call this function must never make).

    Returns `[]` when all three buckets pass ("packetable"); otherwise one non-empty
    finding per FAILED bucket, each naming which bucket it is."""
    sys.path.insert(0, str(HERE))
    import merge_gate, packet  # noqa: E402 — local, so a CLI-only caller of this
                                # module never pays for packet.py's own imports

    findings = []

    # Bucket 1 — Verification: a `## Verification` section holding a non-empty
    # fenced command block that is one gated `&&` chain, naming no bare
    # interpreter — the same three lint rules `packet.verify_script` applies,
    # composed here directly over the pure functions it delegates to.
    sec = packet.section(spec_text, "Verification")
    if sec is None:
        findings.append("Verification: no `## Verification` section in the spec")
    else:
        blk = re.search(r"```[a-z]*\n(.*?)```", sec, re.S)
        if not blk or not blk.group(1).strip():
            findings.append("Verification: the section carries no non-empty fenced command block")
        else:
            block = blk.group(1)
            segs, bad = packet._quote_aware_scan(block)
            bad = list(bad) + [f"a newline-separated statement — `{l}` does not end in `&&`"
                                for l in packet._newline_violations(block)]
            bad += packet._interp_violations(segs)
            if bad:
                findings.append("Verification: not one gated `&&` chain — " + "; ".join(bad[:3]))

    # Bucket 2 — acceptance criteria: `packet.criteria` already encodes the section-
    # or-fallback-scan rule and `die()`s (raising `SystemExit`) when neither finds a
    # single `AC\d+ [` line anywhere.
    try:
        packet.criteria(spec_text)
    except SystemExit as e:
        findings.append(f"Acceptance criteria: {e.code}")

    # Bucket 3 — writes grant: `merge_gate.grant_from_text` raises `Undetermined` on
    # anything that is not a usable path/glob list — prose included.
    try:
        merge_gate.grant_from_text(spec_text)
    except merge_gate.Undetermined as e:
        findings.append(f"Writes grant: {e}")

    return findings


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
