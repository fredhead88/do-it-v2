"""Pure, revision-bound write authority over an admitted ledger snapshot."""
from copy import deepcopy
from pathlib import PurePosixPath

import merge_gate


def granted_tests(spec_id, events):
    """Copy metadata from only the newest grant for the newest written revision."""
    written = next((e for e in reversed(events) if e.get("type") == "spec-written"
                    and e.get("subject") == spec_id), {})
    revision = written.get("_src")
    if not revision:
        return []
    grant = next((e for e in reversed(events) if e.get("type") == "writes-granted"
                  and e.get("subject") == spec_id
                  and e.get("spec_written_src") == revision), None)
    if grant is None:
        return []
    rows = grant.get("tests")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or "test" not in row
                                         for row in rows):
        raise merge_gate.Undetermined("malformed recorded tests for " + spec_id)
    return deepcopy(rows)


def effective_grant(spec_id, text, events):
    """Text paths first, followed by validated literal recorded files, once each."""
    paths = merge_gate.grant_from_text(text)
    recorded = [row["test"] for row in granted_tests(spec_id, events)]
    for path in recorded:
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                or ".." in path.split("/") or any(c in path for c in "*?[]")
                or path.endswith("/") or PurePosixPath(path).name in ("", ".", "..")):
            raise merge_gate.Undetermined("recorded tests for " + spec_id
                                          + ": expected a literal repository-relative file")
    if recorded:
        merge_gate.check_grant(recorded, "recorded tests for " + spec_id)
    return list(dict.fromkeys([*paths, *recorded]))
