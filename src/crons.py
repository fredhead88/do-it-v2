#!/usr/bin/env python3
"""crons — the builder-box cron manifest (L-spec-0318, L-charter-0036 R4/R3).

  crons.py check  [--manifest PATH] [--crontab-file PATH] [--crond-dir PATH]
  crons.py print  [name] [--manifest PATH]
  crons.py install <name> [--manifest PATH] [--crontab-file PATH]

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
import fold  # noqa: E402 — fold.ROOT is read live, at call time, never cached (AC2)

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
    substituted; `sig`/`where`/`owner`/`project`/`name` are passed through."""
    p = pathlib.Path(manifest) if manifest else MANIFEST
    doc = tomllib.loads(p.read_text())
    tokens = _tokens()
    out = []
    for r in doc.get("row", []):
        row = {k: r[k] for k in FIELDS}
        for f in ("schedule", "command", "path"):
            row[f] = _substitute(row[f], tokens)
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


def _matched(text, sig):
    """`sig` against non-commented lines only — a line whose first
    non-whitespace character is `#` never counts, matching or not."""
    pat = re.compile(sig)
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if pat.search(line):
            return True
    return False


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
    dedups by the literal substring `# doit-cron:<name>`: a rerun replaces
    every line already carrying that tag with the one new line, at the FIRST
    such line's position, and leaves every other line byte-for-byte untouched
    — it never removes or reorders an unrelated, pre-existing line (SD8)."""
    row = _row(name, manifest)
    if row["where"] == "cron.d":
        raise ValueError(f"crons: {name!r} is a cron.d row — needs root; "
                          f"'doit crons print {name}' shows the line to install by hand")
    line = print_line(name, manifest)
    tag = f"# doit-cron:{name}"
    if crontab_path is None:
        text = _live_crontab_text()
    else:
        p = pathlib.Path(crontab_path)
        text = p.read_text() if p.exists() else ""
    previous_line, replaced, new_lines = None, False, []
    for l in text.splitlines():
        if l.rstrip().endswith(tag):
            if not replaced:
                previous_line, replaced = l, True
                new_lines.append(line)
            continue  # drop any further duplicate-tagged line
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

    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
