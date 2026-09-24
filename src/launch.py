#!/usr/bin/env python3
"""launch — every standing role starts from a versioned, hygienic launcher,
never an inherited token (L-charter-0036 R2).

  doit launch codex

`launch.toml` (repo root, alongside `models.example.toml`) names one
`[roles.<role>]` table per standing role: which ambient credential NAMES to
strip before the child ever sees them, which rules file is its standing
contract, and — never by default (SD23) — a `model` an operator pins by hand
later. Planner/relay/Executor keep their OWN launchers in `up.py` (`doit up`,
`doit relay`, the Executor loop); this module is what those three call into
for the stripping, the model lookup, and the ledger record. Codex has no
launcher today; `doit launch codex` is the one launch-and-wait this spec
adds for it — not a fourth supervising loop (Boundaries).

Every launch is recorded as one `role-launched` event, and every end as one
`role-ended`, both on `<events_dir or fold.EVENTS>/L-launch-local.jsonl` — one
file, so the derived actor is always `"launch"` (D90), regardless of which
process ran this code.
"""
import hashlib, json, os, pathlib, socket, string, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch, fold, panes  # noqa: E402

# `account("planner"|"relay"|"executor")`'s source of the live oauth account —
# a plain module attribute so a test overrides it exactly as `dispatch.AGENTS`
# already is (Assumptions).
CLAUDE_CONFIG = pathlib.Path.home() / ".claude.json"


def _toml_path():
    """`launch.toml`'s path — `DOIT_LAUNCH_TOML` if set, else the repo-root
    default — recomputed on every call so a subprocess's env var is visible
    with nothing to invalidate (SD23; mirrors `models.PATH`'s env-driven
    pattern, keyed to its own var). A test that assigns `launch.PATH = ...`
    directly (mirroring `dispatch.AGENTS`) lands in this SAME module's
    globals and wins over the env var, because this function's first line
    always sees it before falling back to the live computation."""
    if "PATH" in globals():
        return globals()["PATH"]
    return pathlib.Path(os.environ.get("DOIT_LAUNCH_TOML") or (HERE.parent / "launch.toml"))


def __getattr__(name):
    """`launch.PATH` from OUTSIDE the module — read fresh every access, same
    rule `_toml_path()` applies inside it (PEP 562)."""
    if name == "PATH":
        return _toml_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _roles():
    """Every `[roles.<role>]` table in `launch.toml`, read fresh. `{}` when
    the file is absent — `child_env`'s `ValueError` on an unknown role is what
    actually refuses; this function itself never raises on a missing file."""
    p = _toml_path()
    if not p.is_file():
        return {}
    import tomllib
    return tomllib.loads(p.read_text()).get("roles") or {}


def child_env(role, base=None):
    """A NEW dict: `base`'s own keys (default `os.environ`), minus every
    `strip` name for `role` actually present, plus `set` pairs expanded
    against `base` — never real `os.environ`. `base` is never mutated. An
    unknown role raises `ValueError` naming it (AC3)."""
    roles = _roles()
    if role not in roles:
        raise ValueError(f"launch: no [roles.{role}] in {_toml_path()}")
    cfg = roles[role]
    base = dict(os.environ) if base is None else dict(base)
    strip = set(cfg.get("strip") or [])
    out = {k: v for k, v in base.items() if k not in strip}
    for k, v in (cfg.get("set") or {}).items():
        out[k] = string.Template(v).safe_substitute(base)
    return out


def model_for(role):
    """`role`'s own `model` key in `launch.toml`, or `None`. NEVER calls
    `models.resolve`/`models.load` — SD23 amends SD8: a launcher passes
    `--model` only when `launch.toml` names one for that role explicitly."""
    return (_roles().get(role) or {}).get("model")


def run_codex_status():
    """The default probe for `account("codex")`: `codex login status`,
    stdout and stderr captured SEPARATELY — `(returncode, stdout, stderr)`.
    Injectable: a test replaces `launch.run_codex_status` with a stub
    returning the exact tuple (or raising) it wants."""
    p = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout, p.stderr


def account(role):
    """A plain identifier, never a token, and never raises.

    planner/relay/executor: `oauthAccount.emailAddress` from `CLAUDE_CONFIG`
    (module attribute, injectable); `"undetermined"` on anything short of a
    clean read (absent, unreadable, malformed, missing the key).

    codex: `run_codex_status()`, stdout and stderr read TOGETHER (SD23) —
    concatenated stdout first — first non-blank line, stripped, on exit 0;
    `"undetermined"` on any other exit code, an all-blank combined stream,
    `FileNotFoundError`, or a timeout."""
    if role == "codex":
        try:
            rc, out, err = run_codex_status()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "undetermined"
        if rc != 0:
            return "undetermined"
        for line in ((out or "") + (err or "")).splitlines():
            if line.strip():
                return line.strip()
        return "undetermined"
    try:
        data = json.loads(CLAUDE_CONFIG.read_text())
        email = (data.get("oauthAccount") or {}).get("emailAddress")
        return email or "undetermined"
    except Exception:
        return "undetermined"


def _write(events_dir, type_, **kv):
    d = pathlib.Path(events_dir) if events_dir is not None else fold.EVENTS
    d.mkdir(parents=True, exist_ok=True)
    dispatch.emit(d / "L-launch-local.jsonl", {}, type_, **kv)


def record(role, pane, pid, model, rules, tmux_pane=None, events_dir=None, model_configured=None):
    """Appends one `role-launched` event (Target). Returns the same dict it
    appended (v/ts/type aside) — the eleven base fields, PLUS
    `model_configured` only when the caller passed one; `record` never
    derives it itself, so it can never disagree with the caller about which
    root's map it means (Assumptions)."""
    strip = (_roles().get(role) or {}).get("strip") or []
    entry = {
        "role": role, "pane": pane, "pid": pid, "host": socket.gethostname(),
        "proc_start": panes.proc_start(pid), "model": model, "account": account(role),
        "stripped": [n for n in strip if n in os.environ],
        "rules": str(rules), "rules_sha256": hashlib.sha256(pathlib.Path(rules).read_bytes()).hexdigest(),
        "tmux_pane": tmux_pane or os.environ.get("TMUX_PANE"),
    }
    if model_configured is not None:
        entry["model_configured"] = model_configured
    _write(events_dir, "role-launched", **entry)
    return entry


def ended(pane, rc, events_dir=None):
    """Appends one `role-ended` event `{pane, rc}` to the same file. Returns it."""
    entry = {"pane": pane, "rc": rc}
    _write(events_dir, "role-ended", **entry)
    return entry


def main(argv):
    """`doit launch codex` — a single launch-and-wait (SD8), never a fourth
    supervising loop. A missing `rules` file refuses before any subprocess or
    ledger write (AC9). Returns the exit code the caller should use."""
    if list(argv) != ["codex"]:
        print("usage: doit launch codex", file=sys.stderr)
        return 2
    cfg = _roles().get("codex")
    if cfg is None:
        print(f"launch: no [roles.codex] in {_toml_path()}", file=sys.stderr)
        return 1
    rules = cfg.get("rules")
    if not rules or not pathlib.Path(rules).is_file():
        print(f"launch: codex's rules file is missing: {rules}", file=sys.stderr)
        return 1
    env = child_env("codex")
    model = model_for("codex")
    cmd = ["codex"] + (["--model", model] if model else [])
    proc = subprocess.Popen(cmd, env=env, cwd=cfg.get("cwd"))
    pane = f"codex-{os.getpid()}"
    record("codex", pane, proc.pid, model or "unpinned", rules)
    rc = proc.wait()
    ended(pane, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
