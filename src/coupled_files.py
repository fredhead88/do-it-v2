"""Non-test files a merge gate pins, granted or asked about before the build starts."""
from pathlib import Path

import merge_gate

SCRATCH = "scripts/ci/pg_scratch_files.txt"
FINGERPRINT = "api/alembic_supabase/genesis_schema_fingerprint.txt"
ISOLATION = "api/alembic_supabase/branch_isolation.json"
ALLOWLIST = "api/scripts/check_auth_coverage.py"
STAGED = "_coupled_files"

RULES = [
    dict(id="pg-scratch-inventory", project="albert-scott", trigger="scratch-test", file=SCRATCH, mode="grant"),
    dict(id="migration-fingerprint", project="albert-scott", trigger="migration", file=FINGERPRINT, mode="grant"),
    dict(id="migration-branch-isolation", project="albert-scott", trigger="migration", file=ISOLATION, mode="ask"),
    dict(id="router-auth-allowlist", project="albert-scott", trigger="new-router", file=ALLOWLIST, mode="ask"),
]


def _fires(trigger, text, writes, checkout):
    if trigger == "scratch-test":
        return "pg_scratch" in text and any(
            w.startswith(("tests/", "api/tests/")) and Path(w).name.startswith("test_") for w in writes)
    if trigger == "migration":
        return any(w.startswith("api/alembic_supabase/versions/") for w in writes)
    if trigger == "new-router":
        routers = [w for w in writes if w.startswith("api/app/routers/") and w.endswith(".py")]
        return any(checkout is None or not (Path(checkout) / w).exists() for w in routers)
    raise ValueError("unknown trigger " + trigger)


def _reason(rule, mode):
    if mode == "grant":
        return f"{rule['id']}: the merge gate pins {rule['file']}; granted so the builder may add its line"
    return (f"{rule['id']}: add {rule['file']} to Writes (a human decision; "
            "the Security Contract forbids silent edits)")


def scan(text, project, checkout):
    """One row per rule whose trigger fires and whose file the spec does not already grant."""
    rules = [r for r in RULES if r["project"] == project]
    if not rules:
        return []
    writes = merge_gate.grant_from_text(text)
    rows = []
    for rule in rules:
        if merge_gate.granted(rule["file"], writes) or not _fires(rule["trigger"], text, writes, checkout):
            continue
        if checkout is None or not (Path(checkout) / rule["file"]).is_file():
            rows.append(dict(rule=rule["id"], file=rule["file"], mode="undetermined",
                             reason=f"{rule['id']}: {rule['file']} not found on the checkout"))
        else:
            rows.append(dict(rule=rule["id"], file=rule["file"], mode=rule["mode"],
                             reason=_reason(rule, rule["mode"])))
    return rows


def hook(spec_id, body, written_event, checkout, text=None):
    """Stage grant rows for tick to append once; ask rows hold the spec through body['refused']."""
    if text is None:
        text = Path(written_event["path"]).read_text()
    rows = scan(text, written_event.get("project") or "", checkout)
    grants = [dict(file=r["file"], rule=r["rule"], reason=r["reason"]) for r in rows if r["mode"] == "grant"]
    refused = [dict(check="coupled-files", id=r["file"], reason=r["reason"], seconds=0.0)
               for r in rows if r["mode"] == "ask"]
    undetermined = ["coupled-files: " + r["reason"] for r in rows if r["mode"] == "undetermined"]
    if grants:
        body[STAGED] = grants
    if refused:
        body.setdefault("refused", []).extend(refused)
    body["undetermined"].extend(undetermined)


def held_reason(events, spec_id):
    """The hold text while the newest preflight of the newest spec-written revision refused; else None."""
    mine = [e for e in events if e.get("subject") == spec_id]
    last = max((i for i, e in enumerate(mine) if e.get("type") == "spec-written"), default=None)
    if last is None:
        return None
    later = [e for e in mine[last + 1:] if e.get("type") == "spec-preflight"]
    if not later:
        return None
    files = [r.get("id") for r in later[-1].get("refused") or [] if r.get("check") == "coupled-files"]
    if not files:
        return None
    return "held: spec-preflight refused (" + "; ".join("coupled-files: " + f for f in files) + ")"
