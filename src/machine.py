#!/usr/bin/env python3
"""Compare the box's model map and whole crontab with the repository."""
import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

CHECKOUT = Path(__file__).resolve().parent.parent
NAMES = ("models.toml", "crontab.txt")


@dataclass(frozen=True)
class Drift:
    kind: str
    subject: str
    detail: str


def read_text(path):
    """Read UTF-8 without universal-newline conversion; failure stays unknown."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _sensitive(name):
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return bool(set(re.split(r"[_ .-]+", name.lower())) & {"key", "token", "secret", "password", "passwd"})


def secret_detail(text, subject):
    """Return only locations and names, never secret values."""
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(r"(?<![\w])(?:sk-|ghp_|xoxb-|xoxp-)\S*", line):
            return f"secret value prefix on line {number}"
    if subject == "models.toml":
        try:
            data = tomllib.loads(text)
        except ValueError:
            data = None

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if _sensitive(key) and item not in ("", None, [], {}):
                        return "secret-bearing TOML key"
                    found = visit(item)
                    if found: return found
            elif isinstance(value, list):
                for item in value:
                    found = visit(item)
                    if found: return found
            return None

        if data is not None:
            return visit(data)
    # Also fail closed on assignments in malformed TOML.
    for number, line in enumerate(text.splitlines(), 1):
        pattern = r'\s*([^#=]+?)\s*=\s*(.*?)\s*$' if subject == "models.toml" else r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$'
        match = re.match(pattern, line)
        if match and _sensitive(match[1].strip('"\'')):
            value = match[2].split("#", 1)[0].strip()
            if value not in ("", '""', "''"):
                return f"secret-bearing assignment on line {number}"
    return None


def input_problem(text, subject):
    if not isinstance(text, str) or not text.strip():
        return Drift("undetermined", subject, "input missing, unreadable or empty")
    detail = secret_detail(text, subject)
    return Drift("secret", subject, detail) if detail else None


def check(*, live_models: str, live_crontab: str, repo: str) -> list[Drift]:
    result = []
    for name, live in zip(NAMES, (live_models, live_crontab)):
        saved = read_text(Path(repo) / "machine" / name)
        problems = [input_problem(text, name) for text in (live, saved)]
        problem = next((p for p in problems if p is not None), None)
        if problem:
            result.append(problem)
        elif live != saved:
            a, b = Counter(live.splitlines(keepends=True)), Counter(saved.splitlines(keepends=True))
            result.append(Drift("differs", name,
                                f"live-only: {list((a - b).elements())!r}; repo-only: {list((b - a).elements())!r}"
                                + ("; line order differs" if a == b else "")))
    return result


def crontab_text(command="crontab -l"):
    try:
        proc = subprocess.run(shlex.split(command), capture_output=True, timeout=10)
        return proc.stdout.decode("utf-8") if proc.returncode == 0 else None
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None


def snapshot(*, live_models, live_crontab, repo):
    problems = [p for name, text in zip(NAMES, (live_models, live_crontab))
                if (p := input_problem(text, name))]
    if problems:
        return problems
    dest = Path(repo) / "machine"
    dest.mkdir(parents=True, exist_ok=True)
    for name, text in zip(NAMES, (live_models, live_crontab)):
        (dest / name).write_bytes(text.encode("utf-8"))
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "snapshot"))
    parser.add_argument("--models")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--crontab")
    group.add_argument("--crontab-cmd")
    parser.add_argument("--repo", default=str(CHECKOUT))
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if args.gate and args.command != "check":
        parser.error("--gate is only valid for check")
    if args.gate:
        import install_sync
        if not install_sync._canonical(args.repo):
            print("machine: skipped (not the canonical checkout)")
            return 0
    import models
    live_models = read_text(args.models or models.PATH)
    live_crontab = read_text(args.crontab) if args.crontab else crontab_text(args.crontab_cmd or "crontab -l")
    fn = check if args.command == "check" else snapshot
    drifts = fn(live_models=live_models, live_crontab=live_crontab, repo=args.repo)
    for drift in drifts:
        print(f"{drift.kind} {drift.subject} {drift.detail}")
    return 2 if any(d.kind in ("undetermined", "secret") for d in drifts) else int(bool(drifts))


if __name__ == "__main__":
    raise SystemExit(main())
