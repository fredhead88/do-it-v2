"""Recorded resource lifecycles. Unrecorded paths are never candidates.

Creation sites call record(); callers inspect live() or reap() (dry by default).
The legacy census remains responsible for resources predating these records.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import contextvars
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

import fold
import reap_tmp
import tree_cleanup
import scratch
import disk_reaper

KINDS = frozenset({"worktree", "scratch-db", "venv", "upload-copy", "tmpdir"})
PROC_ROOT = Path("/proc")
_CREATED = {}  # Identity and pid prove this process owns the returned row.
_REAP_MEMO = contextvars.ContextVar('resources_reap_memo', default=None)
_REAP_REPORT = contextvars.ContextVar('resources_reap_report', default=None)


def _memo(events, key, compute):
    memo = _REAP_MEMO.get()
    if memo is None or memo['events'] is not events:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def _project_map(events):
    result = {}
    for event in events:
        if event.get('project'):
            result.setdefault(event.get('subject'), event['project'])
    return result


def _project_fields(subject, projects):
    if fold.PROJECT:
        return {}
    if subject in projects:
        return {'project': projects[subject]}
    report = _REAP_REPORT.get()
    if report is not None:
        report['project_fallback'] = report.get('project_fallback', 0) + 1
    return {}


def _live_builders(events):
    ended = _memo(events, 'ended', lambda: fold.terminal_spawns(events))
    return {e.get('subject') for e in events if e.get('spawn') and e['spawn'] not in ended
            and (e.get('type') == 'build-started' or
                 (e.get('type') == 'spawn-started' and
                  (e.get('role') == 'builder' or '-builder-' in e['spawn'])))}
_TAIL_WINDOW = 1024 * 1024
_READ_CHUNK = 1024 * 1024
# UTF-8 spellings of str.splitlines separators, including universal newlines.
# Keep two bytes between chunks so CRLF and three-byte separators stay whole.
_LINE_BREAKS = re.compile(rb"\r\n|[\n\r\v\f\x1c-\x1e]|\xc2\x85|\xe2\x80[\xa8\xa9]")
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
    created_ts: str | None = None


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


class RecordReadbackError(RuntimeError):
    """The appended resource event could not be confirmed in its ledger file."""


def _tail_lines(stream, size, window):
    """Count the prefix without parsing it; yield bounded, complete tail lines.

    A line crossing the window boundary is included. An oversized foreign line
    is counted but never buffered whole: our own encoded line fits twice in
    the window. Snapshot the size so concurrent appends cannot extend the scan.
    """
    remaining, offset, number = size, 0, 1
    pending, line, oversized = b"", bytearray(), False
    while True:
        chunk = stream.read(min(_READ_CHUNK, remaining)) if remaining else b""
        remaining -= len(chunk)
        final = not chunk or not remaining
        data = pending + chunk
        base = offset - len(pending)
        offset += len(chunk)
        safe = len(data) if final else max(0, len(data) - 2)
        cursor = 0
        for separator in _LINE_BREAKS.finditer(data):
            if separator.start() >= safe:
                break
            if not oversized:
                if len(line) + separator.start() - cursor <= window:
                    line.extend(data[cursor:separator.start()])
                else:
                    oversized = True
            if base + separator.end() > size - window and not oversized:
                yield number, bytes(line)
            number += 1
            line.clear()
            oversized = False
            cursor = separator.end()
        consumed = max(cursor, safe)
        if not oversized:
            if len(line) + consumed - cursor <= window:
                line.extend(data[cursor:consumed])
            else:
                line.clear()
                oversized = True
        pending = data[consumed:]
        if final:
            if line and not oversized and offset > size - window:
                yield number, bytes(line)
            return


def _readback(path, written):
    window = max(_TAIL_WINDOW, 2 * (len(json.dumps(written, sort_keys=True).encode("utf-8")) + 1))
    message = f"readback failed for {path}: {written['type']} subject={written['subject']}"
    result = None
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(0)
            for number, raw in _tail_lines(stream, size, window):
                try:
                    event = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue  # Like fold, count blank and torn lines, then skip.
                if isinstance(event, dict) and all(event.get(k) == v for k, v in written.items()):
                    event["actor"] = fold.actor_of(path.name)
                    event["_src"] = f"{path.name}:{number}"
                    result = event
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecordReadbackError(message) from exc
    if result is None:
        raise RecordReadbackError(message)
    return result


def record(kind: str, ref: str, *, lane: str, subject: str) -> dict:
    row = dict(kind=kind, ref=ref, lane=lane, subject=subject)
    if why := _field_reason(row):
        raise ValueError(why)
    written = _append("resource-created", subject, kind=kind, ref=ref, lane=lane)
    result = _readback(fold.ledger_path(), written)
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
                    undetermined=_field_reason(row), created_ts=row.get('ts'))


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
            resource = active.get(ref)
            if 'created_ts' not in event or (resource is not None and
                    resource.created_ts == event['created_ts'] and resource.subject == event.get('subject')):
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
    if resource.subject in _memo(events, 'ended', lambda: fold.terminal_spawns(events)):
        return None
    specs = _memo(events, 'folded', lambda: fold.fold(events))[0]
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
    if resource.subject in _memo(events, 'builders', lambda: _live_builders(events)):
        return repo, matching[0], False, "builder spawn live"
    spec_events = _memo(events, 'folded', lambda: fold.fold(events))[3].get(resource.subject, [])
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
    if (any(reap_tmp._under(ref, resource.ref) for ref in disk_reaper.process_refs(proc_root, stats={}))
            or reap_tmp._refs_d(proc_root, resource.ref)):
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
         exclude: frozenset[str] = frozenset(), now=None, live_rows=None) -> list[ReapResult]:
    token = _REAP_MEMO.set({'events': events})
    try:
        return _reap(events, dry_run=dry_run, safety=safety, exclude=exclude,
                     now=now, live_rows=live_rows)
    finally:
        _REAP_MEMO.reset(token)


def _gone(resource, now, grace):
    if resource.undetermined is not None or resource.source != 'ledger':
        return False
    if not os.path.isabs(resource.ref) or os.path.normpath(resource.ref) != resource.ref:
        return False
    try:
        created = datetime.fromisoformat(resource.created_ts)
        if (now - created).total_seconds() < grace * 3600:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return _absent(resource.ref)


def _absent(ref):
    try:
        os.lstat(ref)
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def _reap(events, *, dry_run, safety, exclude, now, live_rows):
    safety = _default_safety if safety is None else safety
    now = datetime.now(timezone.utc) if now is None else now
    settings = disk_reaper._settings()
    removed, retired = 0, 0
    results = []
    filename = os.environ.get("DOIT_LEDGER_FILE", "L-operator-local.jsonl")
    denied = not dry_run and fold.admits({"type": "resource-reaped"}, fold.actor_of(filename)) is not None
    for resource in live(events) if live_rows is None else live_rows:
        if resource.ref in exclude or not reapable(resource):
            continue
        try:
            reason = ("actor-not-admitted" if denied else None)
            if not reason and _gone(resource, now, settings['reap_gone_grace_h']):
                if settings['hold'] and not dry_run:
                    results.append(ReapResult(resource.ref, resource.kind, 'held', 'held: reap hold set'))
                    continue
                if retired >= settings['reap_retire_max_per_pass']:
                    results.append(ReapResult(resource.ref, resource.kind, 'deferred',
                        f"deferred: retire cap {settings['reap_retire_max_per_pass']} reached"))
                    continue
                if _absent(resource.ref):
                    reason = 'gone: path absent; record retired, nothing removed'
                    if not dry_run:
                        _append('resource-reaped', resource.subject, ref=resource.ref, reason=reason,
                            created_ts=resource.created_ts, ts=now.isoformat(timespec='seconds'),
                            **_project_fields(resource.subject, _memo(events, 'projects', lambda: _project_map(events))))
                    results.append(ReapResult(resource.ref, resource.kind,
                                              'would-retire' if dry_run else 'retired', reason))
                    retired += 1
                    continue
            reason = reason or ("undetermined: " + resource.undetermined if resource.undetermined else
                      _path_reason(resource) or _owner_reason(resource, events))
            action = 'refused'
            if reason is None:
                if settings['hold'] and not dry_run:
                    action, reason = 'held', 'held: reap hold set'
                elif removed >= settings['reap_max_entries_per_pass']:
                    action, reason = 'deferred', f"deferred: pass cap {settings['reap_max_entries_per_pass']} reached"
                else:
                    reason = safety(resource, events)
            if reason is not None and (not isinstance(reason, str) or not reason):
                raise ValueError("safety must return None or a non-empty reason")
            if reason:
                pass
            elif dry_run:
                action, reason = "would-reap", "owner terminal; safety passed"
                removed += 1
            else:
                _remove(resource, events)
                removed += 1
                reason = "owner terminal; safety passed; removal confirmed"
                _append("resource-reaped", resource.subject, ref=resource.ref, reason=reason,
                        **_project_fields(resource.subject, _memo(events, 'projects', lambda: _project_map(events))))
                action = "reaped"
        except Exception as exc:
            action, reason = "refused", f"undetermined: {exc}"
        results.append(ReapResult(resource.ref, resource.kind, action, reason))
    return results
