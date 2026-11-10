#!/usr/bin/env python3
"""Duplicate and cost view of the test suite (L-spec-2406, charter L-charter-0094 R5).

  fetch_map(project, *, now=None) -> Path     latest `coverage-map` artifact, cached by head_sha
  duplicates(cover) -> list[dict]             tests whose covered set is inside another test's
  slowest(durations, history) -> list[dict]   files carrying 80% of measured time, with history
  read_durations(project) -> (dict, str)      shard_durations.json at origin/master + its sha
  build(project, *, now=None) -> dict         writes duplicates.json, slowest.json, overlap-source.json

Evidence only: every output lives under `$DOIT_ROOT/test-evidence/<project>/`.
Undetermined is never clean: any unreadable input raises ValueError and no
output pair is touched. Run: python3 overlap_view.py <project>
"""
import datetime
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile
from fractions import Fraction

import base_red
import fold
import ghlimit

MAX_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 120
SHA40 = re.compile(r"[0-9a-f]{40}")
MAP_NAME = "coverage-map.json"

# Read at call time so a test can swap the filesystem rename.
_replace = os.replace


def _evidence_dir(project):
    base_red._repo(project)  # validates the project name
    return fold.ROOT / "test-evidence" / project


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _iso(value):
    if not isinstance(value, str):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_map(obj):
    """SD3 shape; raises ValueError on anything else."""
    if not isinstance(obj, dict):
        raise ValueError("coverage map is not an object")
    if obj.get("schema") != 1 or isinstance(obj.get("schema"), bool):
        raise ValueError("coverage map schema is not 1")
    if not isinstance(obj.get("sha"), str) or not SHA40.fullmatch(obj["sha"]):
        raise ValueError("coverage map sha is not 40 hex")
    if not _iso(obj.get("generated_at")):
        raise ValueError("coverage map generated_at is not ISO-8601")
    tests = obj.get("tests")
    if not isinstance(tests, dict) or any(
            not isinstance(v, list) or any(not isinstance(s, str) for s in v) for v in tests.values()):
        raise ValueError("coverage map tests is not an object of string lists")
    unmapped = obj.get("unmapped")
    if not isinstance(unmapped, list) or any(not isinstance(s, str) for s in unmapped):
        raise ValueError("coverage map unmapped is not a list of strings")
    return obj


def fetch_map(project, *, now=None):
    evid = _evidence_dir(project)
    owner = base_red._owner(base_red._repo(project))
    rc, out, _ = ghlimit.run(["gh", "api", f"repos/{owner}/actions/workflows/coverage-map.yml/runs"
                              "?branch=master&status=success&per_page=1"])
    if rc:
        raise ValueError("coverage-map run lookup failed")
    try:
        runs = json.loads(out)["workflow_runs"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("coverage-map run list unreadable")
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        raise ValueError("no successful coverage-map run on master")
    run = runs[0]
    head, rid = run.get("head_sha"), str(run.get("id", ""))
    if not isinstance(head, str) or not SHA40.fullmatch(head) or not rid.isdigit():
        raise ValueError("coverage-map run has no usable head_sha or id")
    dest = evid / "coverage-map" / f"{head}.json"
    if dest.exists():
        return dest
    now = now or datetime.datetime.now(datetime.timezone.utc)
    artifacts = base_red._api(f"repos/{owner}/actions/runs/{rid}/artifacts?per_page=100", "artifacts")
    found = [a for a in artifacts if a.get("name") == "coverage-map" and not a.get("expired")
             and not _past(a.get("expires_at"), now)]
    if len(found) != 1 or not str(found[0].get("id", "")).isdigit():
        raise ValueError("coverage-map artifact missing, expired or ambiguous")
    size = found[0].get("size_in_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("coverage-map artifact size unknown")
    if size > MAX_BYTES:
        raise ValueError("coverage-map artifact over 64 MiB")
    if not ghlimit.gate(wait=False)["ok"]:
        raise ValueError("coverage-map artifact quota unavailable")
    try:
        proc = subprocess.run([ghlimit.real_gh(), "api", f"repos/{owner}/actions/artifacts/{found[0]['id']}/zip"],
                              capture_output=True, timeout=DOWNLOAD_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("coverage-map download failed")
    if proc.returncode or not proc.stdout:
        raise ValueError("coverage-map download failed")
    if len(proc.stdout) > MAX_BYTES:
        raise ValueError("coverage-map download over 64 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(proc.stdout)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != MAP_NAME or members[0].file_size > MAX_BYTES:
                raise ValueError("coverage-map zip has invalid members")
            raw = archive.read(members[0])
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError):
        raise ValueError("coverage-map zip unreadable")
    if len(raw) > MAX_BYTES:
        raise ValueError("coverage-map member over 64 MiB")
    try:
        obj = _validate_map(json.loads(raw))
    except UnicodeDecodeError:
        raise ValueError("coverage map is not text")
    except ValueError as e:
        raise ValueError(str(e) or "coverage map is not JSON")
    if obj["sha"] != head:
        raise ValueError("coverage map sha differs from the run head_sha")
    _atomic_write(dest, raw)
    return dest


def _past(value, now):
    if value is None:
        return False
    if not _iso(value):
        return True  # an unreadable expiry is undetermined, so not usable
    when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when <= now


def duplicates(cover):
    unmapped = set(cover.get("unmapped") or [])
    sets = {t: frozenset(v) for t, v in (cover.get("tests") or {}).items()
            if t not in unmapped and v}
    index = {}
    for t, s in sets.items():
        for src in s:
            index.setdefault(src, set()).add(t)
    rows = []
    for t in sorted(sets):
        mine = sets[t]
        holders = [index[src] for src in mine]
        holders.sort(key=len)
        sup = set(holders[0])
        for h in holders[1:]:
            sup &= h
        sup.discard(t)
        if not sup:
            continue
        equal = sorted(u for u in sup if len(sets[u]) == len(mine))
        group_min = min([t] + equal)
        if equal and t != group_min:
            rows.append({"test": t, "subset_of": group_min, "covered": len(mine)})
            continue
        strict = [u for u in sup if len(sets[u]) > len(mine)]
        if strict:
            best = min(strict, key=lambda u: (len(sets[u]), u))
            rows.append({"test": t, "subset_of": best, "covered": len(mine)})
    return rows


def slowest(durations, history):
    if not isinstance(durations, dict):
        raise ValueError("durations is not an object")
    for k, v in durations.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v != v or v in (float("inf"),):
            raise ValueError("durations holds a non-numeric or negative value")
    rows = history.get("tests") if isinstance(history, dict) and isinstance(history.get("tests"), dict) \
        else history
    if not isinstance(rows, dict):
        raise ValueError("history has no tests object")
    ordered = sorted(durations.items(), key=lambda kv: (-kv[1], kv[0]))
    target = Fraction(sum(Fraction(v) for _, v in ordered)) * 4
    out, cum = [], Fraction(0)
    if target == 0:
        return out
    for test, secs in ordered:
        out.append({"test": test, "seconds": secs, "history": rows.get(test)})
        cum += Fraction(secs)
        if cum * 5 >= target:
            break
    return out


def _git(checkout, *args):
    try:
        r = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("git unavailable")
    if r.returncode:
        raise ValueError("git " + args[0] + " failed")
    return r.stdout


def read_durations(project):
    checkout = base_red._repo(project)
    sha = _git(checkout, "rev-parse", "origin/master").strip()
    if not SHA40.fullmatch(sha):
        raise ValueError("origin/master did not resolve")
    raw = _git(checkout, "show", f"{sha}:scripts/ci/shard_durations.json")
    try:
        files = json.loads(raw)["files"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("shard_durations.json unreadable")
    if not isinstance(files, dict) or not files:
        raise ValueError("shard_durations.json has no files")
    for v in files.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError("shard_durations.json holds a bad duration")
    return files, sha


def _read_history(evid):
    try:
        obj = json.loads((evid / "history.json").read_text())
    except (OSError, ValueError):
        raise ValueError("history.json missing or unreadable")
    if not isinstance(obj, dict) or not isinstance(obj.get("tests"), dict):
        raise ValueError("history.json has no tests object")
    return obj


def _publish(pairs):
    """Replace every (path, bytes) or none: stage all temps, then rename, and on
    any failure put every already-renamed file back as it was."""
    staged, backups, done = [], {}, []
    try:
        for path, data in pairs:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
            staged.append((path, tmp))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            backups[path] = path.read_bytes() if path.exists() else None
        for path, tmp in staged:
            _replace(tmp, path)
            done.append(path)
    except BaseException as e:
        for path in done:
            if backups.get(path) is None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                _atomic_write(path, backups[path])
        for _, tmp in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if isinstance(e, (OSError, ValueError)):
            raise ValueError("could not write the overlap view") from e
        raise


def build(project, *, now=None):
    evid = _evidence_dir(project)
    mp = fetch_map(project, now=now)
    try:
        cover = _validate_map(json.loads(mp.read_text()))
    except (OSError, ValueError):
        raise ValueError("stored coverage map unreadable")
    history = _read_history(evid)
    durations, dsha = read_durations(project)
    dup = json.dumps(duplicates(cover), indent=1).encode() + b"\n"
    slow = json.dumps(slowest(durations, history), indent=1).encode() + b"\n"
    src = json.dumps({"map_sha": cover["sha"], "durations_sha": dsha}, indent=1).encode() + b"\n"
    paths = {"duplicates": evid / "duplicates.json", "slowest": evid / "slowest.json"}
    _publish([(paths["duplicates"], dup), (paths["slowest"], slow), (evid / "overlap-source.json", src)])
    return paths


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: overlap_view.py <project>\n")
        return 1
    try:
        paths = build(argv[1])
    except ValueError as e:
        sys.stderr.write(f"overlap_view: {e}\n")
        return 1
    print(paths["duplicates"])
    print(paths["slowest"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
