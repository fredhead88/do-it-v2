#!/usr/bin/env python3
"""Read-only proofs that registered mechanisms have disappeared."""
import argparse
from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
import subprocess
import tomllib

CHECKOUT = Path(__file__).resolve().parent.parent


class RetiredError(ValueError):
    """A registry cannot be loaded safely."""


@dataclass(frozen=True)
class Retired:
    name: str
    kind: str
    pattern: str
    paths: list[str]
    repo: str | None = None
    regex: bool = False
    provisional: bool = False
    armed_when_path: str | None = None
    armed_when_contains: str | None = None


@dataclass(frozen=True)
class Presence:
    name: str
    kind: str
    repo: str
    where: str
    reason: str


def _relative(path):
    return isinstance(path, str) and bool(path) and not Path(path).is_absolute() and ".." not in Path(path).parts


def load(root: str = "retired.d") -> list[Retired]:
    root = Path(root)
    if not root.is_absolute():
        root = CHECKOUT / root
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise RetiredError(f"{root}: registry missing or unreadable")
    rows = []
    try:
        files = sorted(root.glob("*.toml"))
    except OSError as exc:
        raise RetiredError(f"{root}: {exc}") from exc
    if not files:
        raise RetiredError(f"{root}: registry has no *.toml files")
    for file in files:
        try:
            data = tomllib.loads(file.read_text())
        except (OSError, ValueError) as exc:
            raise RetiredError(f"{file}: {exc}") from exc
        entries = data.get("retired", [])
        if not isinstance(entries, list):
            raise RetiredError(f"{file}: row 1: retired must be an array of tables")
        for index, entry in enumerate(entries, 1):
            try:
                if not isinstance(entry, dict):
                    raise ValueError("expected a table")
                for field in ("name", "kind", "pattern"):
                    if not isinstance(entry.get(field), str) or not entry[field].strip():
                        raise ValueError(f"missing or empty {field}")
                if entry["kind"] not in {"code", "cron", "doc"}:
                    raise ValueError("unknown kind")
                paths = entry.get("paths")
                if not isinstance(paths, list) or (entry["kind"] == "code" and not paths):
                    raise ValueError("paths must be a list, nonempty for code")
                if not all(_relative(p) for p in paths):
                    raise ValueError("paths must be relative globs without ..")
                if "repo" in entry and (not isinstance(entry["repo"], str) or not Path(entry["repo"]).is_absolute()):
                    raise ValueError("repo must be an absolute checkout path")
                for field in ("regex", "provisional"):
                    if field in entry and not isinstance(entry[field], bool):
                        raise ValueError(f"{field} must be a boolean")
                if "armed_when_path" in entry or "armed_when_contains" in entry:
                    if not _relative(entry.get("armed_when_path")):
                        raise ValueError("armed_when_path must be relative without ..")
                    if not isinstance(entry.get("armed_when_contains"), str) or not entry["armed_when_contains"]:
                        raise ValueError("armed_when_contains must be a nonempty string")
                rows.append(Retired(**entry))
            except (ValueError, TypeError) as exc:
                raise RetiredError(f"{file}: row {index}: {exc}") from exc
    if not rows:
        raise RetiredError(f"{root}: registry has no retired rows")
    return rows


def _excluded(path, repo, kind):
    for candidate in (path, path.resolve()):
        relative = candidate.relative_to(repo)
        if (relative.parts and relative.parts[0] == "retired.d") or relative.as_posix() in {"src/retired.py", "src/test_retired.py"}:
            return True
        if kind == "doc" and relative.parts[:2] == ("docs", "handoffs"):
            return True
    return False


def _matches(parts, pattern):
    """Match tracked path components, including zero directories for **/."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return (_matches(parts, pattern[1:]) or
                bool(parts) and _matches(parts[1:], pattern))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and _matches(parts[1:], pattern[1:])


def check(rows: list[Retired], *, repo: str, crontab_text: str | None) -> list[Presence]:
    result = []
    for row in rows:
        checkout = Path(row.repo or repo).absolute()

        def emit(where, reason):
            result.append(Presence(row.name, row.kind, str(checkout), str(where), reason))

        if not checkout.is_dir() or not os.access(checkout, os.R_OK | os.X_OK):
            emit(checkout, "undetermined")
            continue
        checkout = checkout.resolve()
        if row.armed_when_path is not None:
            marker = checkout / row.armed_when_path
            try:
                marker.resolve().relative_to(checkout)
                armed = row.armed_when_contains in marker.read_text()
            except FileNotFoundError:
                armed = False
            except (OSError, ValueError):
                emit(marker, "undetermined")
                continue
            if not armed:
                emit(marker, "unarmed")
                continue
        try:
            matcher = re.compile(row.pattern).search if row.regex else lambda line: row.pattern in line
        except re.error:
            emit(checkout, "undetermined")
            continue
        if row.kind == "cron":
            if crontab_text is None:
                emit("crontab", "undetermined")
            elif any(matcher(line) for line in crontab_text.splitlines() if not line.lstrip().startswith("#")):
                emit("crontab", "present")
            patterns = ["crons.toml"]
        elif row.kind == "doc":
            patterns = row.paths or ["docs/**/*", "agents/**/*", "README.md"]
        else:
            patterns = row.paths
        try:
            tracked = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z"],
                                     capture_output=True, text=True)
            if tracked.returncode:
                emit(checkout, "undetermined")
                continue
        except (OSError, ValueError):
            emit(checkout, "undetermined")
            continue
        globs = [Path(pattern).parts for pattern in patterns]
        for name in sorted(set(tracked.stdout.split("\0")) - {""}):
            if not any(_matches(Path(name).parts, pattern) for pattern in globs):
                continue
            path = checkout / name
            try:
                if _excluded(path, checkout, row.kind):
                    continue
                with path.open() as stream:
                    present = False
                    for line in stream:
                        if row.kind == "cron" and line.lstrip().startswith("#"):
                            continue
                        present = bool(matcher(line)) or present
                if present:
                    emit(path, "present")
            except (OSError, ValueError):
                emit(path, "undetermined")
    return result


def _live_crontab_text():
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if proc.returncode == 0:
        return proc.stdout
    if re.search(r"no crontab for\b", proc.stderr.lower()):
        return ""
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--root")
    parser.add_argument("--repo")
    parser.add_argument("--crontab-file")
    args = parser.parse_args(argv)
    root = args.root or "retired.d"
    repo = args.repo or str(CHECKOUT)
    try:
        rows = load(str(root))
    except RetiredError as exc:
        print(f"undetermined: {exc}")
        print("0 present, 1 undetermined, 0 unarmed, 0 provisional")
        return 2
    if args.crontab_file is None:
        crontab_text = _live_crontab_text()
    else:
        try:
            crontab_text = Path(args.crontab_file).read_text()
        except (OSError, ValueError):
            crontab_text = None
    hits = check(rows, repo=repo, crontab_text=crontab_text)
    for row in rows:
        if row.provisional:
            print(f"provisional: {row.name} ({row.kind}) repo={row.repo or repo}")
    for hit in hits:
        print(f"{hit.reason}: {hit.name} ({hit.kind}) repo={hit.repo} where={hit.where}")
    counts = {reason: sum(hit.reason == reason for hit in hits) for reason in ["present", "undetermined", "unarmed"]}
    provisional = sum(row.provisional for row in rows)
    print(f"{counts['present']} present, {counts['undetermined']} undetermined, {counts['unarmed']} unarmed, {provisional} provisional")
    return 2 if counts["undetermined"] else 1 if counts["present"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
