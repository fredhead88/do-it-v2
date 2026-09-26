#!/usr/bin/env python3
"""sweep_wrappers — pure gate logic behind the sweeper role's only two paths into
production (L-spec-0386, charter L-charter-0038 R1). No subprocess, no file
write, no network anywhere in this module: every check here is a pure function
over strings/paths, so it can be exercised by direct import with no live
credential and no live host.

`scripts/sweep/ro-sql` and `scripts/sweep/droplet-read` are the two thin
executables that call these functions and then, and only then, run the one
subprocess each is allowed: `psql` (read-only DSN, forced
`default_transaction_read_only=on`) or `ssh` (one allowlisted remote command,
built by `quote_remote_command` and never shell-interpolated). Refusal always
happens before either subprocess starts.
"""
import os
import pathlib
import re
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch  # noqa: E402 — readonly_dsn, _parse_env (existing, src/dispatch.py)

# The full allowlist (Plan SD6), by base command name. Order-sensitive — AC18
# checks exact tuple equality, not membership.
ALLOWED_DROPLET_COMMANDS = ("cat", "head", "tail", "ls", "stat", "grep", "find",
                            "journalctl", "systemctl", "crontab", "curl")

# The seven shell metacharacters/sequences that make any droplet-read argument
# unsafe to hand to a remote shell, whatever command it is attached to.
_METACHARS = (";", "&", "|", "`", "$", "<", ">")

# Fix 3: six plain, case-sensitive substrings that make a cat/head/tail/grep
# argument a likely secret path. A substring list, not a content scanner —
# Assumptions §4e/security_path: a renamed/symlinked secret, or a secret
# inside an ordinary log line matched by grep, is NOT caught by this.
_SECRET_SUBSTRINGS = (".env", "/.ssh/", "shadow", "credentials", ".pem", ".key")

# Fix 1: tokens leaked past round 1's exact-membership checks — these are
# checked by `str.startswith`, since the leaked forms are new prefixes, not
# variants of a token already checked.
_FIND_BAD_PREFIXES = ("-exec", "-ok", "-fprint", "-fls")
_JOURNALCTL_BAD_PREFIXES = ("--vacuum", "--rotate", "--flush", "--relinquish",
                            "--smart-relinquish", "--setup-keys")

# curl's positive walk (fix 2, fix 6): a closed flag set, no residual
# "everything else is fine" branch.
_CURL_NO_VALUE_FLAGS = ("-s", "-S", "-f", "-i", "-I")
_CURL_VALUE_FLAGS = ("--max-time", "-H")
_CURL_HOST_RE = re.compile(r"^(https?://)?127\.0\.0\.1(:\d+)?(/.*)?$")


def resolve_ro_dsn(project_checkout):
    """`(dsn, refusal)`. `None` from `dispatch.readonly_dsn` -> `(None, "no
    read-only DSN configured for this project")`. Else re-applies
    `dispatch.py:802`'s collision test over the same checkout's `.env`: a hit
    -> `(dsn, "the read-only DSN matches another credential in this
    project's .env; refusing")`; no hit -> `(dsn, None)`."""
    project_checkout = pathlib.Path(project_checkout)
    dsn = dispatch.readonly_dsn(project_checkout)
    if dsn is None:
        return None, "no read-only DSN configured for this project"
    env = dispatch._parse_env(project_checkout / ".env")
    if any(k != "SUPABASE_DB_URL_RO" and "DB_URL" in k and v == dsn for k, v in env.items()):
        return dsn, "the read-only DSN matches another credential in this project's .env; refusing"
    return dsn, None


def sql_statement_refusal(sql):
    """`None` when `sql` is admitted, else a non-empty refusal string. The
    first non-comment (`--`-prefixed lines skipped), non-blank line's first
    word must case-insensitively be SELECT/WITH/EXPLAIN/SHOW; EXPLAIN
    additionally refuses when its very next word is ANALYZE; and, across the
    whole statement, one optional trailing `;` is stripped before checking
    that no `;` remains — a further `;` anywhere else refuses."""
    keyword_line = None
    for raw_line in sql.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        keyword_line = line
        break
    if keyword_line is None:
        return "no SQL statement found"
    words = keyword_line.split()
    keyword = words[0].upper()
    if keyword not in ("SELECT", "WITH", "EXPLAIN", "SHOW"):
        return f"statement type {words[0]!r} is not permitted (SELECT/WITH/EXPLAIN/SHOW only)"
    if keyword == "EXPLAIN" and len(words) > 1 and words[1].upper() == "ANALYZE":
        return "EXPLAIN ANALYZE is not permitted"
    stripped = sql.rstrip()
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        return "only one optional trailing semicolon is permitted"
    return None


def _curl_refusal(args):
    if args.count("-s") != 1:
        return "curl: -s is required"
    positionals = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _CURL_NO_VALUE_FLAGS:
            i += 1
            continue
        if a in _CURL_VALUE_FLAGS:
            if i + 1 >= len(args):
                return f"curl: {a!r} requires a value"
            i += 2
            continue
        if a.startswith("-"):
            return f"curl: unrecognized flag {a!r}"
        positionals.append(a)
        i += 1
    if len(positionals) != 1:
        return f"curl: exactly one URL is required, got {len(positionals)}"
    if not _CURL_HOST_RE.match(positionals[0]):
        return f"curl: host not allowed: {positionals[0]!r}"
    return None


def droplet_command_refusal(cmd, args):
    """`None` when `(cmd, args)` is admitted to run over ssh, else a
    non-empty refusal string. `cmd` must be in `ALLOWED_DROPLET_COMMANDS`;
    every argument must be free of shell metacharacters/newlines; then a
    handful of per-command rules narrow further (secret paths, follow-mode,
    `find`/`journalctl` destructive flags, `systemctl`/`crontab`'s narrow
    subcommand, `curl`'s closed positive walk)."""
    if cmd not in ALLOWED_DROPLET_COMMANDS:
        return f"command {cmd!r} is not allowlisted"
    for a in args:
        if any(mc in a for mc in _METACHARS) or "\n" in a:
            return f"argument {a!r} contains a disallowed shell metacharacter"
    if cmd in ("cat", "head", "tail", "grep"):
        for a in args:
            if any(sub in a for sub in _SECRET_SUBSTRINGS):
                return f"argument {a!r} matches a disallowed secret-path pattern"
    if cmd in ("tail", "journalctl"):
        for a in args:
            if a in ("-f", "-F", "--follow"):
                return f"{cmd}: {a!r} (follow mode) is not permitted"
    if cmd == "find":
        for a in args:
            if a == "-delete" or a.startswith(_FIND_BAD_PREFIXES):
                return f"find: argument {a!r} is not permitted"
    if cmd == "journalctl":
        for a in args:
            if a.startswith(_JOURNALCTL_BAD_PREFIXES):
                return f"journalctl: argument {a!r} is not permitted"
    if cmd == "systemctl":
        if not args or args[0] not in ("status", "show", "is-active", "list-timers"):
            return "systemctl: only status/show/is-active/list-timers are permitted"
    if cmd == "crontab":
        if args != ["-l"]:
            return "crontab: only -l is permitted"
    if cmd == "curl":
        return _curl_refusal(args)
    return None


def quote_remote_command(cmd, args):
    """The one string handed to `ssh`, which `ssh` hands to the remote
    shell — `shlex.join` makes it round-trip byte-for-byte back through
    `shlex.split` on the far side, whatever whitespace or quoting an
    argument carries."""
    return shlex.join([cmd, *args])


def timeout_seconds():
    """`SWEEP_SUBPROCESS_TIMEOUT_SECONDS` or `30`, read fresh every call —
    never cached, so a test can set it per-case with no process restart."""
    return int(os.environ.get("SWEEP_SUBPROCESS_TIMEOUT_SECONDS", "30"))
