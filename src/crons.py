#!/usr/bin/env python3
"""crons — the builder-box cron manifest (L-spec-0318, L-charter-0036 R4/R3).

  crons.py check      [--manifest PATH] [--crontab-file PATH] [--crond-dir PATH]
  crons.py print      [name] [--manifest PATH]
  crons.py install    <name> [--manifest PATH] [--crontab-file PATH]
  crons.py ensure-env [--crontab-file PATH]

`crons.toml` (repo root) declares every builder-box job — reaper, lessons
digest, backup watch, the R1 tick, and friends — the way `REQUIRED_ACTIVATIONS`
already declares the droplet's. `check()` measures the installed crontab (and,
for the one `where = "cron.d"` row, `/etc/cron.d`) against it and reports every
row not present-and-executable — the same activation-gate shape the droplet's
deploy already runs, so a job going uninstalled is a check failure, not a human
noticing later (the 0143 closeout reaper's own failure mode).

`install` only ever adds or replaces ITS OWN `# doit-cron:<name>`-tagged line
in the user crontab; it never touches an unrelated line, never removes a
pre-existing hand-installed one, and refuses outright on a `cron.d` row —
writing `/etc/cron.d` needs root, so that row is print-only here (SD6).
Auto-install is out of scope by design (SD20): detection is automatic,
installation is a named, human-invoked act.
"""
import argparse, os, pathlib, re, subprocess, sys
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fold, scratch  # noqa: E402 — fold.ROOT is read live, at call time, never cached (AC2)

MANIFEST = HERE.parent / "crons.toml"
FIELDS = ("name", "where", "schedule", "command", "sig", "path", "owner", "project")
CROND_DEFAULT = "/etc/cron.d"


class Undetermined(Exception):
    """A needed read — the live crontab, or a cron.d directory a row actually
    needs — failed outright. Raised, never swallowed into a false negative."""


# ── manifest + templating ─────────────────────────────────────────────────────

def _tokens():
    """{doit}/{repo}/{root}/{tick_min} — {root} and {tick_min} read live, at
    call time (never cached at import), so a test that monkeypatches
    `fold.ROOT` or sets `DOIT_TICK_MIN` before calling sees it here too —
    exactly as `up.cron_line()` / `backup.cron_line()` already behave."""
    doit = HERE.parent / "doit"
    return {
        "doit": str(doit),
        "repo": str(doit.parent),
        "root": str(fold.ROOT),
        "tick_min": os.environ.get("DOIT_TICK_MIN", "5"),
    }


def _substitute(s, tokens):
    for k, v in tokens.items():
        s = s.replace("{" + k + "}", v)
    return s


def rows(manifest=None):
    """The manifest's rows — `manifest=None` reads `crons.toml` next to this
    repo's `doit`; a path reads that file instead (the test seam). Every
    templated field (`schedule`, `command`, `path`) comes back already
    substituted; `sig`/`where`/`owner`/`project`/`name` are passed through.
    Each row also carries `max_runtime_s` (R14a) — an int when the TOML row
    sets it, else `None`; it is deliberately NOT one of `FIELDS` (which stays
    the 8 required-string columns) since it is optional and not a string."""
    p = pathlib.Path(manifest) if manifest else MANIFEST
    doc = tomllib.loads(p.read_text())
    tokens = _tokens()
    out = []
    for r in doc.get("row", []):
        row = {k: r[k] for k in FIELDS}
        for f in ("schedule", "command", "path"):
            row[f] = _substitute(row[f], tokens)
        row["max_runtime_s"] = r.get("max_runtime_s")
        out.append(row)
    return out


def _row(name, manifest=None):
    for row in rows(manifest):
        if row["name"] == name:
            return row
    raise ValueError(f"crons: no such row {name!r}")


# ── the live crontab, and the one call site of the real binary ──────────────

_RUN = subprocess.run  # the one seam a test replaces (SD11) — never
                       # `_live_crontab_text` itself (AC8).


def _live_crontab_text():
    """`""` for "no crontab for this user" (that is not a failure — it is an
    empty crontab); any other non-zero exit, or `_RUN` itself raising, is
    `Undetermined` — a failed read must never read as a silent "nothing
    installed"."""
    try:
        result = _RUN(["crontab", "-l"], capture_output=True, text=True)
    except Exception as e:
        raise Undetermined(f"crons: crontab -l failed to run: {type(e).__name__}: {e}") from e
    if result.returncode == 0:
        return result.stdout
    if "no crontab" in (result.stderr or "").lower():
        return ""
    raise Undetermined(f"crons: crontab -l exited {result.returncode}: {result.stderr}")


def _crond_text(crond_dir):
    """Every file directly under `crond_dir`, concatenated. A directory that
    cannot be listed (absent, unreadable) is `Undetermined` — called only when
    the manifest actually has a `where = "cron.d"` row (AC7)."""
    d = pathlib.Path(crond_dir)
    try:
        names = sorted(p for p in d.iterdir() if p.is_file())
    except OSError as e:
        raise Undetermined(f"crons: cannot read cron.d dir {d}: {e}") from e
    chunks = []
    for f in names:
        try:
            chunks.append(f.read_text())
        except OSError as e:
            raise Undetermined(f"crons: cannot read {f}: {e}") from e
    return "\n".join(chunks)


def _find_line(text, sig):
    """The first non-commented line matching `sig`, or `None` — a line whose
    first non-whitespace character is `#` never counts, matching or not."""
    pat = re.compile(sig)
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if pat.search(line):
            return line
    return None


def _matched(text, sig):
    return _find_line(text, sig) is not None


def _executable(path):
    try:
        return os.access(path, os.X_OK)
    except OSError:
        return False


# ── check() — the activation gate itself ─────────────────────────────────────

def check(manifest=None, crontab_text=None, crond_dir=None):
    """One `{name, owner, project, reason}` per row not satisfied. `reason` is
    `"absent"` (no matching, non-commented line) or `"not-executable"` (a
    matching line exists, but the row's `path` has no execute bit).
    `crond_dir` is read at most once, and only if some row needs it."""
    the_rows = rows(manifest)
    user_text = _live_crontab_text() if crontab_text is None else crontab_text
    crond_cache = {}
    out = []
    for row in the_rows:
        if row["where"] == "cron.d":
            if "text" not in crond_cache:
                crond_cache["text"] = _crond_text(crond_dir if crond_dir is not None else CROND_DEFAULT)
            hay = crond_cache["text"]
        else:
            hay = user_text
        if not _matched(hay, row["sig"]):
            out.append({"name": row["name"], "owner": row["owner"],
                        "project": row["project"], "reason": "absent"})
        elif not _executable(row["path"]):
            out.append({"name": row["name"], "owner": row["owner"],
                        "project": row["project"], "reason": "not-executable"})
    return out


def _parse_field(field, lo, hi):
    """A cron minute/hour field (`*`, `*/N`, `N`, or a comma list of those)
    expanded to its sorted list of int values within `[lo, hi]`, or `None` for
    anything else (a range, a step of 0, an out-of-bounds value, `@daily`, …)
    — R14a's `interval_s` is strict: an unsupported syntax is `None`, never a
    guess."""
    if field == "*":
        return list(range(lo, hi + 1))
    out = []
    for part in field.split(","):
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                return None
            if step <= 0:
                return None
            out.extend(range(lo, hi + 1, step))
        else:
            try:
                v = int(part)
            except ValueError:
                return None
            if not (lo <= v <= hi):
                return None
            out.append(v)
    return sorted(set(out))


def interval_s(schedule):
    """The smallest gap in seconds between consecutive fire times over a 24h
    day, wrapping midnight (one fire a day gives 86400). Minute/hour accept
    `*`, `*/N`, `N`, comma lists of those; day-of-month, month, day-of-week
    must be `*` — anything else (a range, `@daily`, a non-`*` dom/month/dow)
    returns `None` (R14c: an unreadable schedule, never a guess)."""
    parts = schedule.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*" or dow != "*":
        return None
    minutes, hours = _parse_field(minute, 0, 59), _parse_field(hour, 0, 23)
    if not minutes or not hours:
        return None
    fires = sorted({h * 60 + m for h in hours for m in minutes})
    if len(fires) == 1:
        return 86400
    gaps = [fires[i + 1] - fires[i] for i in range(len(fires) - 1)]
    gaps.append(fires[0] + 1440 - fires[-1])
    return min(gaps) * 60


def unwrapped(manifest=None, crontab_text=None, crond_dir=None):
    """Names of rows whose `sig` matches a non-commented installed line that
    does NOT also match `\\bcron-run\\s+<name>\\s+--\\s` on that SAME line
    (R14a/R14c). A row with no matching line at all is not reported here —
    that absence is `cron-missing`'s (`check()`'s) territory."""
    the_rows = rows(manifest)
    user_text = _live_crontab_text() if crontab_text is None else crontab_text
    crond_cache = {}
    out = []
    for row in the_rows:
        if row["where"] == "cron.d":
            if "text" not in crond_cache:
                crond_cache["text"] = _crond_text(crond_dir if crond_dir is not None else CROND_DEFAULT)
            hay = crond_cache["text"]
        else:
            hay = user_text
        line = _find_line(hay, row["sig"])
        if line is None:
            continue
        wrap_pat = r'\bcron-run\s+' + re.escape(row["name"]) + r'\s+--\s'
        if not re.search(wrap_pat, line):
            out.append(row["name"])
    return out


# ── print + install ───────────────────────────────────────────────────────────

def print_line(name, manifest=None):
    """`"{schedule} {command}  # doit-cron:{name}"` for the named row — pure,
    and the exact string `install()` writes."""
    row = _row(name, manifest)
    return f"{row['schedule']} {row['command']}  # doit-cron:{name}"


def _write_live_crontab(text):
    p = _RUN(["crontab", "-"], input=text, capture_output=True, text=True)
    if p.returncode != 0:
        raise Undetermined(f"crons: crontab - failed: {p.stderr}")


def install(name, manifest=None, crontab_path=None):
    """Refuses (`ValueError`) an unknown `name` or a `where = "cron.d"` row —
    that write needs root and is not this tool's to make (SD6). Otherwise
    dedups by the literal substring `# doit-cron:<name>` OR (R14a) a
    non-commented line matching the row's own `sig` with no tag yet — a rerun
    replaces the FIRST such line (tagged or sig-matched) with the one new
    line, at that line's position, drops every FURTHER tagged-or-sig-matched
    line, and leaves every other line byte-for-byte untouched — it never
    removes or reorders an unrelated, pre-existing line (SD8). The sig-match
    arm is what lets a legacy bare (never-tagged) line be replaced in place
    instead of leaving it stray alongside a newly appended wrapped one."""
    row = _row(name, manifest)
    if row["where"] == "cron.d":
        raise ValueError(f"crons: {name!r} is a cron.d row — needs root; "
                          f"'doit crons print {name}' shows the line to install by hand")
    line = print_line(name, manifest)
    tag = f"# doit-cron:{name}"
    sig_pat = re.compile(row["sig"])
    if crontab_path is None:
        text = _live_crontab_text()
    else:
        p = pathlib.Path(crontab_path)
        text = p.read_text() if p.exists() else ""
    previous_line, replaced, new_lines = None, False, []
    for l in text.splitlines():
        is_tagged = l.rstrip().endswith(tag)
        is_sig_matched = not is_tagged and not l.lstrip().startswith("#") and bool(sig_pat.search(l))
        if is_tagged or is_sig_matched:
            if not replaced:
                previous_line, replaced = l, True
                new_lines.append(line)
            continue  # drop any further duplicate/matching line
        new_lines.append(l)
    action = "replaced" if replaced else "appended"
    if not replaced:
        new_lines.append(line)
    new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
    if crontab_path is None:
        _write_live_crontab(new_text)
    else:
        pathlib.Path(crontab_path).write_text(new_text)
    return {"ok": True, "action": action, "previous_line": previous_line}


def ensure_env(crontab_path=None):
    """Installs/replaces the fixed, tagged `TMPDIR=` pair as the target's
    FIRST two lines (R9b). Mirrors `install()`'s own `crontab_path=None` seam:
    `None` reads/writes the live crontab via `_live_crontab_text()`/
    `_write_live_crontab()`; a path reads/writes that file instead (`""` when
    it does not yet exist).

    The pair is always exactly two lines — a tag-comment line
    `"# doit-cron:tmpdir-env"`, then a BARE value line `f"TMPDIR={scratch.root()}"`
    with nothing else on it. This split is load-bearing
    (`problem:cron-env-trailing-comment`): Ubuntu cron 3.0pl1 does not treat a
    same-line `#` as a comment on an env-assignment line, so a trailing tag on
    the `TMPDIR=` line itself would make the crontab's actual value the
    literal string `<root>  # doit-cron:tmpdir-env` — a nonexistent directory.

    When a line exactly equal to the tag already exists, that line AND the
    line immediately after it (whatever it holds, dropped without inspection)
    are removed before the fresh pair is written at the top — never a second
    copy of the tag, or an orphaned old value line, anywhere else in the text.
    Every other line — job rows, comments, blanks — survives, in the same
    relative order, merely shifted down by exactly two lines.

    Returns `{"ok": True, "action": "installed" | "replaced", "line": <the
    value line just written>}` — `"replaced"` when a tagged pair was found and
    removed, `"installed"` otherwise."""
    tag = "# doit-cron:tmpdir-env"
    value_line = f"TMPDIR={scratch.root()}"
    if crontab_path is None:
        text = _live_crontab_text()
    else:
        p = pathlib.Path(crontab_path)
        text = p.read_text() if p.exists() else ""
    lines = text.splitlines()
    replaced = False
    kept = []
    i = 0
    while i < len(lines):
        if lines[i] == tag:
            replaced = True
            i += 2 if i + 1 < len(lines) else 1  # drop the tag + the line right after it
            continue
        kept.append(lines[i])
        i += 1
    new_lines = [tag, value_line] + kept
    new_text = "\n".join(new_lines) + "\n"
    if crontab_path is None:
        _write_live_crontab(new_text)
    else:
        pathlib.Path(crontab_path).write_text(new_text)
    return {"ok": True, "action": "replaced" if replaced else "installed", "line": value_line}


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="crons")
    sub = ap.add_subparsers(dest="cmd")

    p_check = sub.add_parser("check")
    p_check.add_argument("--manifest")
    p_check.add_argument("--crontab-file")
    p_check.add_argument("--crond-dir")

    p_print = sub.add_parser("print")
    p_print.add_argument("name", nargs="?")
    p_print.add_argument("--manifest")

    p_install = sub.add_parser("install")
    p_install.add_argument("name")
    p_install.add_argument("--manifest")
    p_install.add_argument("--crontab-file")

    p_ensure_env = sub.add_parser("ensure-env")
    p_ensure_env.add_argument("--crontab-file")

    a = ap.parse_args(argv)

    if a.cmd == "check":
        crontab_text = pathlib.Path(a.crontab_file).read_text() if a.crontab_file else None
        try:
            missing = check(manifest=a.manifest, crontab_text=crontab_text, crond_dir=a.crond_dir)
        except Undetermined as e:
            print(str(e), file=sys.stderr)
            return 2
        if not missing:
            return 0
        for m in missing:
            print(f"{m['name']}: {m['reason']}")
        return 1

    if a.cmd == "print":
        names = [a.name] if a.name else [row["name"] for row in rows(a.manifest)]
        for name in names:
            row = _row(name, a.manifest)
            if row["where"] == "cron.d":
                print(f"# {name}: cron.d — needs root; never installed by this tool")
            print(print_line(name, a.manifest))
        return 0

    if a.cmd == "install":
        try:
            res = install(a.name, manifest=a.manifest, crontab_path=a.crontab_file)
        except (ValueError, Undetermined) as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"action: {res['action']}")
        if res["action"] == "replaced":
            print(f"previous: {res['previous_line']}")
        return 0

    if a.cmd == "ensure-env":
        res = ensure_env(crontab_path=a.crontab_file)
        print(f"action: {res['action']}")
        return 0

    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
