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
WEIGHT_FIELDS = ("input", "output", "cache_read", "cache_creation")


def _weights(d, p):
    """[weights]: model -> {input, output, cache_read, cache_creation} price
    ratios, relative to input=1.0 (retro step 9: fold.py's SPEND block and
    `doit spend` use these to render an input-equivalent weighted total —
    TOKENS, never dollars, §4.2). Optional, per model: a map with no [weights]
    table, or a model missing from one, renders unweighted — never a fabricated
    ratio. A model that IS named must carry all four keys, or the map is wrong,
    not partially wrong."""
    out = {}
    for model, w in (d.get("weights") or {}).items():
        missing = [f for f in WEIGHT_FIELDS if f not in w]
        if missing:
            raise ValueError(f"{p}: weights.{model} missing {missing}")
        try:
            out[model] = {f: float(w[f]) for f in WEIGHT_FIELDS}
        except (TypeError, ValueError):
            raise ValueError(f"{p}: weights.{model} has a non-numeric ratio")
    return out


def load_weights(path=None):
    """The [weights] table alone — for a caller (fold.py's SPEND block, `doit
    spend`) that wants price ratios without the full contract map. {} when the
    root has no models.toml, or the map has no [weights] table."""
    p = pathlib.Path(path or PATH)
    if not p.is_file():
        return {}
    return _weights(tomllib.loads(p.read_text()), p)


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
    out = {"_default": default, "_weights": _weights(d, p)}
    for name, c in (d.get("contracts") or {}).items():
        b, m = c.get("backend", default), c.get("model")
        _check(p, name, b, m)
        if name in PANE_ONLY and b != "pane":
            raise ValueError(f"{p}: contracts.{name} is a pane role; backend {b!r} is not pane")
        if name == "executor" and b not in EXECUTOR_BACKENDS:
            raise ValueError(f"{p}: contracts.executor.backend {b!r} — the Executor is a pane or "
                             f"the tick's claude-p, nothing else is implemented")
        fb = c.get("fallback")
        if fb is not None:
            # What the wrapper re-dispatches on when the PRIMARY backend refuses for its
            # own reason (a Codex limit) — never a pane, never the same backend.
            _check(p, f"{name}.fallback", fb.get("backend"), fb.get("model"))
            if fb["backend"] in ("pane", b):
                raise ValueError(f"{p}: contracts.{name}.fallback.backend {fb['backend']!r} — "
                                 f"a fallback is a different, dispatchable backend")
            fb = {"backend": fb["backend"], "model": fb["model"]}
        out[name] = {"backend": b, "model": m, "fallback": fb}
    return out


def _check(p, name, b, m):
    if b not in BACKENDS:
        raise ValueError(f"{p}: contracts.{name}.backend {b!r} is not one of {BACKENDS}")
    if not m:
        raise ValueError(f"{p}: contracts.{name} names no model")
    if m.startswith("claude-fable") and b != "pane":
        raise ValueError(f"{p}: contracts.{name}: {m} on backend {b!r} — Fable is pane-only "
                         "(the Agent tool substitutes Sonnet silently; `-p` cannot select it)")


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
        return {"backend": mp["_default"], "model": contract_model, "source": "models.toml-default", "fallback": None}
    return {"backend": c["backend"], "model": c["model"], "source": "models.toml", "fallback": c.get("fallback")}


PROFILES = {"mixed": "models.example.toml", "claude-only": "models.claude-only.toml"}


def main(argv):
    """doit models show | use <mixed|claude-only>. `use` copies the repo's template over
    the root's map — validated first — and records `models-changed` on the ledger, so a
    change of ruling is an event and not a mystery the next fold cannot explain."""
    import hashlib, shutil, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    if argv[:1] == ["show"]:
        mp = load()
        if mp is None:
            print(f"no map at {PATH} — every dispatch runs the flag/env route (pre-map behaviour)")
            return 1
        print(f"{PATH} · default backend {mp['_default']}")
        for name, c in sorted((k, v) for k, v in mp.items() if not k.startswith("_")):
            fb = c.get("fallback")
            print(f"  {name:17} {c['backend']:9} {c['model']}" + (f"  (fallback {fb['backend']} {fb['model']})" if fb else ""))
        return 0
    if argv[:1] == ["use"] and len(argv) == 2 and argv[1] in PROFILES:
        src = pathlib.Path(__file__).resolve().parent.parent / PROFILES[argv[1]]
        load(src)                                           # a template that lies is never installed
        PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, PATH)
        digest = hashlib.sha256(PATH.read_bytes()).hexdigest()[:16]
        import fold
        fold.append(["models-changed", "models", f"profile={argv[1]}", f"sha256={digest}", f"path={PATH}"])
        print(f"models: {PATH} <- {src.name} (profile {argv[1]}, sha256 {digest})")
        return 0
    sys.exit(f"usage: doit models show | use <{'|'.join(PROFILES)}>")


def first_on_model(events, contract_sha256, model):
    """D120 keyed on (contract, model actually used): True when no spawn-done on this
    ledger has run this exact contract on this model before — the trust run."""
    return not any(e.get("type") == "spawn-done" and e.get("contract_sha256") == contract_sha256
                   and (e.get("model_used") or e.get("model")) == model for e in events)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
