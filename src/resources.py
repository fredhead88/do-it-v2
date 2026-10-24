"""Recorded resource lifecycles. Unrecorded paths are never candidates.

Creation sites call record(); callers inspect live() or reap() (dry by default).
The legacy census remains responsible for resources predating these records.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tomllib

import fold
import reap_tmp
import tree_cleanup
import scratch

KINDS = frozenset({"worktree", "scratch-db", "venv", "upload-copy", "tmpdir"})
PROC_ROOT = Path("/proc")
_CREATED = {}  # Identity and pid prove this process owns the returned row.
# Resolve roots when reaping, including a caller's current fold.ROOT.
ROOTS = {
    "worktree": lambda: (fold.ROOT / "worktrees",),
    "tmpdir": lambda: (Path("/tmp"), Path("/dev/shm"), Path("/var/tmp"),
                       scratch.root(), fold.ROOT / "cache/node_modules"),
    "scratch-db": lambda: (Path("/tmp"), Path("/var/tmp"), scratch.root()),
    "venv": lambda: (fold.ROOT / "venvs", Path("/tmp")),
    "upload-copy": lambda: (),
}


@dataclass(frozen=True)
class Resource:
    kind: str
    ref: str
    lane: str
    subject: str
    source: str = "ledger"
    undetermined: str | None = None


@dataclass(frozen=True)
class ReapResult:
    ref: str
    kind: str
    action: str
    reason: str


def _field_reason(row):
    for key in ("kind", "ref", "lane", "subject"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            return f"missing or invalid {key}"
    if row["kind"] not in KINDS:
        return "invalid kind"
    return None


def _append(event_type, subject, **fields):
    # JSON-quote every value: paths and identifiers are strings even when they
    # resemble JSON. A positional '--inbox' must never reroute the ledger.
    return fold.append([event_type, subject if subject != "--inbox" else "",
                        "subject=" + json.dumps(subject)] +
                       [key + "=" + json.dumps(value) for key, value in fields.items()], strict=True)


def record(kind: str, ref: str, *, lane: str, subject: str) -> dict:
    row = dict(kind=kind, ref=ref, lane=lane, subject=subject)
    if why := _field_reason(row):
        raise ValueError(why)
    written = _append("resource-created", subject, kind=kind, ref=ref, lane=lane)
    filename = os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    result = next(e for e in reversed(fold.read_events(use_cache=False))
                if e.get("_src", "").startswith(filename + ":")
                and all(e.get(k) == v for k, v in written.items()))
    _CREATED[id(result)] = (result, os.getpid())
    return result


def release(row: dict, *, reason: str):
    """Release only this process's exact record() return value, after use ends."""
    proof = _CREATED.get(id(row))
    if proof is None or proof[0] is not row or proof[1] != os.getpid():
        raise ValueError("creator release requires this process's record row")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("release requires a reason")
    resource = _resource(row, "")
    if resource.undetermined:
        raise ValueError(resource.undetermined)
    filename = os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    if fold.admits({"type": "resource-reaped"}, fold.actor_of(filename)) is not None:
        raise ValueError("actor-not-admitted")
    why = _path_reason(resource)
    if why:
        raise ValueError(why)
    path = Path(resource.ref)
    if reason == "promoted-to-cache" and os.path.lexists(path):
        raise ValueError("promotion not confirmed")
    if os.path.lexists(path):
        if resource.kind == "worktree" and (path / ".git").is_file():
            tree_cleanup.git(path, "worktree", "remove", "--force", str(path))
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    if os.path.lexists(path):
        raise RuntimeError("removal not confirmed")
    _append("resource-reaped", resource.subject, ref=resource.ref, reason=reason)
    del _CREATED[id(row)]


def _resource(row, fallback):
    return Resource(kind=row.get("kind", ""),
                    ref=row.get("ref") if isinstance(row.get("ref"), str) and row["ref"] else fallback,
                    lane=row.get("lane", ""), subject=row.get("subject", ""),
                    undetermined=_field_reason(row))


def _upload_path():
    # Match the live root's project checkout, rather than another root's config.
    config = fold.ROOT / "repos/do-it-v2/look.toml"
    if not config.exists():
        return None
    configured = tomllib.loads(config.read_text()).get("resources", {}).get("upload_manifest")
    if configured is not None and (not isinstance(configured, str) or not Path(configured).is_absolute()):
        raise ValueError("upload manifest path must be absolute")
    return configured


def _upload_rows(path):
    rows = [(n, json.loads(line)) for n, line in enumerate(Path(path).read_text().splitlines()) if line.strip()]
    if any(not isinstance(row, dict) for _, row in rows):
        raise ValueError("invalid upload manifest row")
    return rows


def manifest_counts():
    """Count legacy and declared rows in an independent, all-or-error read."""
    counts = {"legacy": 0, "declared": 0}
    try:
        path = _upload_path()
        if path is None:
            return counts
        for _, row in _upload_rows(path):
            counts["declared" if any(k in row for k in ("kind", "ref", "lane", "subject")) else "legacy"] += 1
        return counts
    except (OSError, ValueError, TypeError, AttributeError):
        return {"legacy": 0, "declared": 0, "unreadable": True}


def _uploads():
    ref = "upload-manifest:unknown"
    try:
        configured = _upload_path()
        if configured is None:
            return []
        ref = configured
        result = []
        for n, row in _upload_rows(configured):
            if not any(k in row for k in ("kind", "ref", "lane", "subject")):
                continue
            why = _field_reason(row)
            if why is None and row.get("kind") != "upload-copy":
                why = "invalid upload manifest kind"
            ref_value = row.get("ref")
            result.append(Resource("upload-copy", ref_value if isinstance(ref_value, str) and ref_value else f"upload-manifest:{n}",
                                   row.get("lane", ""), row.get("subject", ""),
                                   source="upload-manifest", undetermined=why))
        return result
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [Resource("upload-copy", ref, "upload-archive", "upload-manifest",
                         source="upload-manifest", undetermined=f"upload manifest: {type(exc).__name__}")]


def live(events: list[dict]) -> list[Resource]:
    active = {}
    for n, event in enumerate(events):
        kind, ref = event.get("type"), event.get("ref")
        if kind == "resource-reaped" and isinstance(ref, str):
            if fold.admits(event, event.get("actor")) is not None:
                continue
            active.pop(ref, None)
        elif kind == "resource-created":
            resource = _resource(event, f"ledger:{n}")
            active[resource.ref] = resource
    for resource in _uploads():
        active.setdefault(resource.ref, resource)
    return list(active.values())


def reapable(resource) -> bool:
    """Keep uncertain resources visible, including failures to resolve roots."""
    if resource.undetermined is not None:
        return True
    try:
        return bool(ROOTS.get(resource.kind, lambda: ())())
    except Exception:
        return True


def not_reapable_counts(events):
    counts = {}
    for resource in live(events):
        if not reapable(resource):
            counts[resource.kind] = counts.get(resource.kind, 0) + 1
    return counts


def _owner_reason(resource, events):
    if resource.subject in fold.terminal_spawns(events):
        return None
    specs = fold.fold(events)[0]
    owner = specs.get(resource.subject)
    if not owner or owner["state"] == "unknown":
        return "undetermined: owner"
    if owner["state"] not in tree_cleanup.TERMINAL_SPEC_STATES:
        return "owner not terminal: " + owner["state"]
    return None


def _path_reason(resource):
    roots = ROOTS[resource.kind]()
    if not roots:
        return "no-allowed-root"
    path = Path(os.path.realpath(resource.ref))
    allowed = [Path(os.path.realpath(root)) for root in roots]
    if not any(path != root and path.is_relative_to(root) for root in allowed):
        return "no-allowed-root"
    if not Path(resource.ref).is_absolute() or path != Path(resource.ref):
        return "undetermined: path is relative or symlinked"
    # Protect roots and their containers, including the caller's own checkout.
    protected = (Path("/"), Path.home().resolve(), fold.ROOT.resolve(), Path.cwd().resolve())
    if any(path == root or path in root.parents for root in protected):
        return "protected root"
    if path.exists() and path.lstat().st_uid != os.getuid():
        return "path owned by another uid"
    if (path / ".git").is_dir() or ((path / ".git").exists() and resource.kind != "worktree"):
        return "repository root"
    if (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir():
        return "repository root"
    return None


def _worktree(resource, events):
    """Establish the primary checkout and judge exactly the recorded worktree."""
    common = tree_cleanup.git(resource.ref, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if Path(common).name != ".git":
        raise tree_cleanup.Undetermined("primary repository unresolved")
    repo = Path(common).parent
    main = tree_cleanup.git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", ok=True).stdout.strip()
    if not main:
        main = tree_cleanup.git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if main not in ("main", "master"):
            raise tree_cleanup.Undetermined("main branch unresolved")
    wts = tree_cleanup.worktrees(repo)
    matching = [b for b, wt in wts.items() if Path(wt) == Path(resource.ref)]
    if len(matching) != 1 or matching[0] == main:
        raise tree_cleanup.Undetermined("recorded path is not a secondary branch worktree")
    ended = fold.terminal_spawns(events)
    if any(e.get("subject") == resource.subject and e.get("spawn") and e["spawn"] not in ended
           and (e.get("type") == "build-started" or
                (e.get("type") == "spawn-started" and
                 (e.get("role") == "builder" or "-builder-" in e["spawn"]))) for e in events):
        return repo, matching[0], False, "builder spawn live"
    spec_events = fold.fold(events)[3].get(resource.subject, [])
    if fold.standing_rejects(spec_events):
        return repo, matching[0], False, "standing rejection"
    branch, wt, dead, why = tree_cleanup.judge(repo, resource.subject, spec_events, main, wts, None)
    if branch != matching[0] or wt != resource.ref:
        raise tree_cleanup.Undetermined("owner branch does not match recorded path")
    return repo, branch, dead, why


def _default_safety(resource, events, *, proc_root=None):
    proc_root = PROC_ROOT if proc_root is None else proc_root
    if resource.kind == "worktree":
        _, _, dead, why = _worktree(resource, events)
        if not dead:
            # judge retains its existing branch/dirty reasons. Unknown proof is
            # still explicit, never presented as successful evidence of safety.
            if why and ("holds work" in why or "clean" in why or
                        why in ("builder spawn live", "standing rejection")):
                return why
            return "undetermined: " + (why or "worktree proof")
    if reap_tmp._refs_c(proc_root, resource.ref) or reap_tmp._refs_d(proc_root, resource.ref):
        return "live process, socket or pidfile references path"
    return None


def _remove(resource, events):
    path = Path(resource.ref)
    if resource.kind == "worktree":
        repo, branch, dead, why = _worktree(resource, events)
        if not dead:
            raise tree_cleanup.Undetermined(why or "worktree proof changed")
        tree_cleanup.reap(repo, branch, resource.ref, patch_id_proof=True)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    if os.path.lexists(path):
        raise RuntimeError("removal not confirmed")


def reap(events: list[dict], *, dry_run: bool = True, safety=None,
         exclude: frozenset[str] = frozenset()) -> list[ReapResult]:
    safety = _default_safety if safety is None else safety
    results = []
    filename = os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    denied = not dry_run and fold.admits({"type": "resource-reaped"}, fold.actor_of(filename)) is not None
    for resource in live(events):
        if resource.ref in exclude or not reapable(resource):
            continue
        try:
            reason = ("actor-not-admitted" if denied else None)
            reason = reason or ("undetermined: " + resource.undetermined if resource.undetermined else
                      _path_reason(resource) or _owner_reason(resource, events) or
                      safety(resource, events))
            if reason is not None and (not isinstance(reason, str) or not reason):
                raise ValueError("safety must return None or a non-empty reason")
            if reason:
                action = "refused"
            elif dry_run:
                action, reason = "would-reap", "owner terminal; safety passed"
            else:
                _remove(resource, events)
                reason = "owner terminal; safety passed; removal confirmed"
                _append("resource-reaped", resource.subject, ref=resource.ref, reason=reason)
                action = "reaped"
        except Exception as exc:
            action, reason = "refused", f"undetermined: {exc}"
        results.append(ReapResult(resource.ref, resource.kind, action, reason))
    return results
