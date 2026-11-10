"""Fixed grants for an owed batch; manifests describe access, never create it."""
import re

NEEDS = ("ro-dsn", "repo", "droplet", "environment")
_KINDS = dict(zip(NEEDS, ("plain", "repo", "plain", "repo")))
_GRANTS = {
    "ro-dsn": "3. Grant: this batch's own read-only DB credential (capability=ro-dsn).",
    "repo": "3. Grant: repository clone only; production DSNs stay unset.",
    "droplet": "3. Grant: `scripts/sweep/droplet-read`, read-only; no database credential.",
    "environment": "3. Grant: repository environment checks only; production DSNs stay unset.",
}


def manifest_kind(need):
    return _KINDS[need]


def grant_line(need):
    return _GRANTS[need]


def wants_dsn(kind, need):
    return kind != "repo" and (need is None or need == "ro-dsn")


def parse_need(raw, path, die):
    match = re.search(r"^need:[ \t]*(.*)$", raw, re.M)
    need = match.group(1).strip() if match else None
    if need is not None and need not in NEEDS:
        die(f"{path}: unsupported sweep need {need!r}")
    return need


def sweep_dsn_role(manifest_path, project, provision):
    """Only a declared ro-dsn or legacy non-repo sweep gets a private RO DSN; repo checks use the per-check scratch endpoint."""
    import packet  # lazy: packet imports this module
    m = packet.parse_sweep_manifest(manifest_path)
    if not wants_dsn(m["kind"], m["need"]):
        return "none"
    return provision() if project else "absent"
