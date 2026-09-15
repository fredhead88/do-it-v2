#!/usr/bin/env python3
"""models — the model map: contract → (backend, model), decided once per ledger root.

  $DOIT_ROOT/models.toml   (template: models.example.toml at the repo root)

The contract file's `model:` line is a PREFERENCE; this file is the ruling for one
root, and the backend it names is the only one that runs there. Pilot S5: three
contracts said Fable and ran on Opus for a day because the Agent tool substitutes
Sonnet silently and nothing recorded the swap. Pilot S32: the backend ruling lived
in a per-shell environment variable, one wrapper ran without it, and its poke
spawned a metered Executor twice. Both are the same defect — a decision that was
not written where every process reads it.
"""
import os, pathlib, tomllib

ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
PATH = ROOT / "models.toml"
BACKENDS = ("pane", "seat", "claude-p", "codex")
PANE_ONLY = ("thinker", "planner")
EXECUTOR_BACKENDS = ("pane", "claude-p")


def load(path=None):
    """The map, or None when the root has none (the pre-map behaviour then applies).
    A map that cannot be trusted raises — a wrong map is worse than no map."""
    p = pathlib.Path(path or PATH)
    if not p.is_file():
        return None
    d = tomllib.loads(p.read_text())
    default = (d.get("defaults") or {}).get("backend", "claude-p")
    if default not in BACKENDS:
        raise ValueError(f"{p}: defaults.backend {default!r} is not one of {BACKENDS}")
    out = {"_default": default}
    for name, c in (d.get("contracts") or {}).items():
        b, m = c.get("backend", default), c.get("model")
        if b not in BACKENDS:
            raise ValueError(f"{p}: contracts.{name}.backend {b!r} is not one of {BACKENDS}")
        if not m:
            raise ValueError(f"{p}: contracts.{name} names no model")
        if m.startswith("claude-fable") and b != "pane":
            raise ValueError(f"{p}: contracts.{name}: {m} on backend {b!r} — Fable is pane-only "
                             "(the Agent tool substitutes Sonnet silently; `-p` cannot select it)")
        if name in PANE_ONLY and b != "pane":
            raise ValueError(f"{p}: contracts.{name} is a pane role; backend {b!r} is not pane")
        if name == "executor" and b not in EXECUTOR_BACKENDS:
            raise ValueError(f"{p}: contracts.executor.backend {b!r} — the Executor is a pane or "
                             f"the tick's claude-p, nothing else is implemented")
        out[name] = {"backend": b, "model": m}
    return out


def backend_of(role, mp):
    """The backend the map rules for a role — the contract's own line, else the default."""
    return (mp.get(role) or {}).get("backend", mp["_default"])


def resolve(role, contract_model=None, mp=None, path=None):
    """{backend, model, source}. With no map: backend None (the caller's flags decide,
    as before) and the contract's own model, marked so the event says the map was absent."""
    mp = load(path) if mp is None else mp
    if mp is None:
        return {"backend": None, "model": contract_model, "source": "contract-default"}
    c = mp.get(role)
    if c is None:
        return {"backend": mp["_default"], "model": contract_model, "source": "models.toml-default"}
    return {"backend": c["backend"], "model": c["model"], "source": "models.toml"}


def first_on_model(events, contract_sha256, model):
    """D120 keyed on (contract, model actually used): True when no spawn-done on this
    ledger has run this exact contract on this model before — the trust run."""
    return not any(e.get("type") == "spawn-done" and e.get("contract_sha256") == contract_sha256
                   and (e.get("model_used") or e.get("model")) == model for e in events)
