#!/usr/bin/env python3
"""Hermetic balance regression checks; run directly or collect with pytest."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO = Path(__file__).resolve().parent.parent


def expected(c, s, state):
    # Independent transition table: ordered predicates for each source state.
    rows = {
        "default": [(s is not None and s >= 70, "claude-pressed"),
                    (s is not None and s < 50 and c is not None and c >= 70, "codex-pressed")],
        "claude-pressed": [(s is None or s >= 60, "claude-pressed"),
                           (s is not None and s < 50 and c is None, "claude-pressed"),
                           (s is not None and s < 50 and c is not None and c >= 70, "codex-pressed")],
        "codex-pressed": [(s is not None and s >= 70, "claude-pressed"),
                          (c is None or c >= 50, "codex-pressed"),
],
    }
    return next((target for pred, target in rows[state] if pred), "default")


def response(criterion, body):
    encoded = json.dumps(body, sort_keys=True).encode()
    print(json.dumps({"criterion": criterion, "url": f"cli://balance/{criterion}", "status": 0,
                      "body_sha256": hashlib.sha256(encoded).hexdigest(), "body_excerpt": body}))


def test_transitions():
    import balance
    states = ("default", "claude-pressed", "codex-pressed")
    n = 0
    for state in states:
        for c in range(0, 101, 5):
            for s in range(0, 101, 5):
                assert balance.choose(c, s, state) == expected(c, s, state), (c, s, state)
                n += 1
    print(f"1130-AC1 {n} cells")
    response("AC1", {"cells": n})
    n = 0
    for state in states:
        for known in range(0, 101, 5):
            for c, s in ((None, known), (known, None)):
                assert balance.choose(c, s, state) == expected(c, s, state), (c, s, state)
                n += 1
        assert balance.choose(None, None, state) == state
        for invalid in (-1, 101, "garbage", float("nan"), float("inf"), True, {}, []):
            assert balance.choose(invalid, 40, state) == balance.choose(None, 40, state)
            assert balance.choose(80, invalid, state) == balance.choose(80, None, state)
    print(f"1130-AC2 {n} cells")
    response("AC2", {"cells": n, "invalid_readings": "unknown"})


def fixture():
    # Keep the shipped profile's comments, weights, and inline fallback syntax.
    text = (REPO / "models.example.toml").read_text()
    return text.replace('[contracts.builder]\nbackend = "codex"',
                        '[contracts.builder]\nbackend = "codex"  # managed backend comment').replace('[contracts.reviewer]\nbackend = "codex"',
                        '[contracts.reviewer]\nbackend = "seat"').replace(
                            '[contracts.reviewer]\nbackend = "seat"           # provisional — see header\nmodel = "gpt-6-astra"\nfallback = { backend = "seat", model = "claude-sonnet-5" }   # when Codex refuses on its limit',
                            '[contracts.reviewer]\nbackend = "seat"           # provisional — see header\nmodel = "claude-sonnet-5-5"')


def test_integration():
    with tempfile.TemporaryDirectory(prefix="balance-test-") as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"HOME": str(root / "home"), "DOIT_ROOT": str(root),
                                     "DOIT_LEDGER_FILE": "L-operator-wrong.jsonl"}):
            import balance
            import dispatch
            import fold
            import models
            # Modules may already be loaded by a collector; pin all inherited paths.
            with contextlib.ExitStack() as stack:
                for module, attrs in ((fold, {"ROOT": root, "EVENTS": root / "events", "PROJECT": None}),
                                      (models, {"ROOT": root, "PATH": root / "models.toml"})):
                    for name, value in attrs.items():
                        stack.enter_context(patch.object(module, name, value))
                run_checks(root, balance, dispatch, fold, models)


def run_checks(root, balance, dispatch, fold, models):
    now = 1800000000
    rollout = root / "home/.codex/sessions/2027/01/01/rollout-test.jsonl"
    rollout.parent.mkdir(parents=True)

    def quota(pct=80, resets=None):
        rollout.write_text(json.dumps({"rate_limits": {"primary": {
            "used_percent": pct, "resets_at": resets or now + 3600}}}) + "\n")

    def seat(observed, pct="72"):
        return {"type": "seat-usage", "observed_at": observed, "seven_day_pct": pct}

    quota()
    cases = [("fresh", now, 72), ("29 min", now - 1740, 72),
             ("31 min", now - 1860, None), ("30 min", now - 1800, None),
             ("future", now + 301, None), ("clock skew", now + 299, 72),
             ("garbage", "bad", None)]
    for label, observed, want in cases:
        assert balance.read_inputs([seat(observed)], now, dispatch.codex_quota) == (80, want)
        print(f"1130-AC3 {label}")
    iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    assert balance.read_inputs([seat(iso), seat(now - 100, "5")], now, dispatch.codex_quota) == (80, 72)
    assert balance.read_inputs([seat(now), seat("bad")], now, dispatch.codex_quota) == (80, None)
    assert balance.read_inputs([seat(now, "bad")], now, dispatch.codex_quota) == (80, None)
    assert balance.read_inputs([], now, dispatch.codex_quota) == (80, None)
    print("1130-AC3 no-event")
    quota(None)
    assert balance.read_inputs([], now, dispatch.codex_quota) == (None, None)
    quota(80, now - 1)
    assert balance.read_inputs([], now, dispatch.codex_quota) == (0, None)
    rollout.unlink()
    assert balance.read_inputs([], now, dispatch.codex_quota) == (None, None)
    print("1130-AC3 no-rollout")
    response("AC3", {"freshness_cases": len(cases), "real_quota": True, "no_rollout": None})

    text = fixture()
    targets = {
        "default": [("codex", "gpt-6.1-sol", "high"), ("codex", "gpt-6.1-sol", "medium"),
                    ("codex", "gpt-6.1-sol", "medium"), ("codex", "gpt-6.1-sol", "medium"),
                    ("seat", "claude-sonnet-5-5", None), ("seat", "claude-opus-5-5", None)],
        "claude-pressed": [("codex", "gpt-6.1-sol", "high"), ("codex", "gpt-6.1-sol", "medium"),
                           ("codex", "gpt-6.1-sol", "medium"), ("codex", "gpt-6.1-sol", "medium"),
                           ("codex", "gpt-6.1-sol", "medium"), ("seat", "claude-sonnet-5-5", None)],
        "codex-pressed": [("seat", "claude-sonnet-5-5", None)] * 3 +
                         [("seat", "claude-haiku-4-5-20251001", None),
                          ("seat", "claude-sonnet-5-5", None), ("seat", "claude-opus-5-5", None)],
    }
    roles = ("builder", "spec-writer", "probe", "research", "reuse-scout", "charter-reviewer")

    removed_comments = set()
    for line in text.splitlines():
        if re.match(r"\s*(effort|fallback)\s*=", line) and '#' in line:
            removed_comments.add(line[line.index('#'):])

    def unmanaged_lines(source):
        role = None
        out = []
        for line in source.splitlines(keepends=True):
            if line.strip() in removed_comments:
                continue
            match = re.match(r"\[contracts\.([^]]+)\]", line)
            if line.lstrip().startswith("["):
                role = match[1] if match else None
            if role not in roles or not re.match(r"\s*(backend|model|effort|fallback)\s*=", line):
                out.append(line)
        return out

    for state, rows in targets.items():
        rewritten = balance.rewrite(text, state)
        assert balance.rewrite(rewritten, state) == rewritten
        assert unmanaged_lines(rewritten) == unmanaged_lines(text)
        assert '# when Codex refuses on its limit' in rewritten
        assert '  # managed backend comment' in rewritten
        models.PATH.write_text(rewritten)
        mp = models.load()
        for role, (backend, model, effort) in zip(roles, rows):
            resolved = models.resolve(role, mp=mp)
            assert (resolved["backend"], resolved["model"], resolved["effort"]) == (backend, model, effort)
            if backend == "seat":
                assert resolved["fallback"] is None
            else:
                fallback_model = "claude-haiku-4-5-20251001" if role == "research" else "claude-sonnet-5-5"
                assert resolved["fallback"] == {"backend": "seat", "model": fallback_model}
            print(f"1130-AC4 {state} {role} ok")
        print(f"1130-AC4 unchanged lines: {len(unmanaged_lines(text))}")
        assert all(mp[r]["backend"] != "codex" for r in
                   ("reviewer", "grader", "spec-auditor", "plan-auditor", "charter-reviewer"))
        print(f"1130-AC5 {state} judges on seat")
    missing = re.sub(r'\[contracts.reuse-scout\]\n(?:[^\[]|\n)*?(?=\[)', '', text, count=1)
    assert '[contracts.reuse-scout]' not in missing
    appended = balance.rewrite(missing, "default")
    assert appended.rfind('[contracts.reuse-scout]') > appended.rfind('[seats]')
    models.PATH.write_text(appended)
    assert models.load()["reuse-scout"]["backend"] == "seat"

    response("AC4", {"resolved_pairs": 18, "unchanged_lines": len(unmanaged_lines(text)), "missing_table_appended": True})

    state_path = root / "state/balance.json"

    def snapshot():
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
                and p.relative_to(root).parts[0] != "cache"}

    def run(args=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = balance.main(list(args))
        return rc, buf.getvalue()

    for state in targets:
        models.PATH.write_text((REPO / "models.example.toml").read_text())
        before = snapshot()
        try:
            balance.rewrite(models.PATH.read_text(), state)
        except ValueError as exc:
            assert 'reviewer' in str(exc)
        else:
            raise AssertionError("mixed profile accepted")
        rc, out = run()
        assert rc != 0 and "reviewer" in out and snapshot() == before
    models.PATH.write_text(text)
    with patch.dict(balance.TARGETS["default"]["charter-reviewer"], {"backend": "codex"}):
        try:
            balance.rewrite(text, "default")
        except ValueError:
            pass
        else:
            raise AssertionError("Codex judge accepted")
    for bad in (None, "invalid = ["):
        if bad is None:
            models.PATH.unlink()
        else:
            models.PATH.write_text(bad)
        before = snapshot()
        assert run()[0] != 0 and snapshot() == before
    models.PATH.write_text(text)
    with patch.dict(balance.TARGETS["default"]["builder"]["fallback"], {"backend": "claude-p"}):
        before = snapshot()
        assert run()[0] != 0 and snapshot() == before
    print("1130-AC5 invalid map, missing map, validation failure: unchanged")
    response("AC5", {"judge_states": 3, "invalid_writes": "refused without changes"})

    # Operator ordering uses fold.read_events order, including equal timestamps.
    fold.EVENTS.mkdir(exist_ok=True)
    operator_file = fold.EVENTS / "L-operator-local.jsonl"

    def operator(ts):
        operator_file.write_text(json.dumps({"v": 1, "type": "models-changed", "subject": "models", "ts": ts}) + "\n")

    operator("2030-01-01T00:00:00+00:00")
    before = snapshot()
    assert run() == (0, "held: operator profile 2030-01-01T00:00:00+00:00\n")
    assert snapshot() == before and not state_path.exists()
    operator_file.unlink()
    assert run(["hold"])[0] == 0
    before = snapshot()
    assert 'held: manual ' in run()[1] and snapshot() == before
    assert run(["resume"])[0] == 0
    data = json.loads(state_path.read_text())
    assert data["state"] == "default" and not data["held"] and data["resumed_at"]
    state_path.unlink()
    before_models = models.PATH.read_bytes()
    assert run(["resume"])[0] == 0 and models.PATH.read_bytes() == before_models
    assert not list(fold.EVENTS.glob("*balance*"))
    operator("2000-01-01T00:00:00+00:00")
    assert 'held:' not in run()[1]
    operator("2030-01-01T00:00:00+00:00")
    before = snapshot()
    assert 'held: operator profile' in run()[1] and snapshot() == before
    operator_file.unlink()
    state_path.write_text("corrupt")
    before = snapshot()
    assert run() == (0, "held: state unreadable\n") and snapshot() == before
    state_path.unlink()
    assert run(["resume"])[0] == 0
    # Equal timestamps from a different process: pin a balance event then an operator event.
    script = """import fold,json,os,sys
from datetime import datetime
fold.NOW = datetime.fromisoformat(sys.argv[1])
fold.append(['balance','models','state_from=default','state_to=default','codex_pct=0'], strict=True)
ts=fold.NOW.isoformat(timespec='seconds')
print(ts)
"""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), DOIT_LEDGER_FILE="L-balance-local.jsonl")
    stamp = subprocess.check_output(["/usr/bin/python3", "-c", script, fold.NOW.isoformat()], env=env, text=True).strip()
    operator(stamp)
    # resume predates this tie; only ledger order decides the two events' precedence.
    data = json.loads(state_path.read_text())
    data["resumed_at"] = "2000-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(data))
    events = fold.read_events()
    assert events[-1]["type"] == "models-changed", events
    before = snapshot()
    assert 'held: operator profile' in run()[1] and snapshot() == before
    operator("1999-01-01T00:00:00+00:00")
    assert 'held:' not in run()[1]
    operator_file.unlink()
    print("1130-AC6 operator later, earlier, equal; hold; resume; absent; unreadable ok")
    response("AC6", {"operator_order": ["earlier", "equal", "later"], "hold_resume": True, "corrupt_state": "held"})

    # Real local rollout and seat event, strict append, then read from another process.
    quota(80)
    seat_file = fold.EVENTS / "L-seat-usage-local.jsonl"
    seat_file.write_text(json.dumps(dict(seat(datetime.now(timezone.utc).isoformat(), "20"),
                                         ts="2026-01-01T00:00:00+00:00", subject="usage")) + "\n")
    # Real quota reset timestamp needs to be in the future relative to the real clock.
    quota(80, 4102444800)
    before = snapshot()
    rc, out = run(["--dry-run"])
    assert rc == 0 and "codex-pressed" in out and snapshot() == before
    print("1130-AC7 dry-run clean")
    ledger = fold.EVENTS / "L-balance-local.jsonl"
    n = len(ledger.read_text().splitlines())
    assert run()[0] == 0
    assert len(ledger.read_text().splitlines()) == n + 1
    assert os.environ["DOIT_LEDGER_FILE"] == "L-operator-wrong.jsonl"
    check_script = """import fold,models,json,hashlib
es=fold.read_events(); e=[e for e in es if e['type']=='balance'][-1]
assert e['actor']=='balance'
assert not any(x is e or x == e for x in fold.fold(es)[2])
mp=models.load()
for c in e['changes']:
 assert {k:mp[c['role']][k] for k in ('backend','model')}==c['to']
assert e['models_sha256']==hashlib.sha256(models.PATH.read_bytes()).hexdigest()
raw=json.loads((fold.EVENTS/'L-balance-local.jsonl').read_text().splitlines()[-1])
assert 'actor' not in raw
print('1130-AC7 kept')
"""
    print(subprocess.check_output(["/usr/bin/python3", "-c", check_script], env=env, text=True).strip())
    before = snapshot()
    assert run()[0] == 0 and snapshot() == before
    quota(90, 1)
    assert run()[0] == 0
    last = json.loads(ledger.read_text().splitlines()[-1])
    assert last["codex_pct"] == "0" and last["state_to"] == "default"
    zero = last["codex_pct"]
    print(subprocess.check_output(["/usr/bin/python3", "-c", check_script], env=env, text=True).strip())
    # Unknown percentages are explicit strings, never zero or missing.
    rollout.unlink()
    seat_file.unlink()
    models.PATH.write_text(text)
    assert run()[0] == 0
    last = json.loads(ledger.read_text().splitlines()[-1])
    assert last["codex_pct"] == last["claude_7d_pct"] == "unknown"
    print("1130-AC7 zero reset and unknown kept")
    response("AC7", {"separate_process": "kept", "zero": zero, "unknown": last["codex_pct"], "dry_run": "unchanged"})

    result = subprocess.run([str(REPO / "doit"), "balance", "--dry-run"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    import tomllib
    rows = tomllib.loads((REPO / "crons.toml").read_text())["row"]
    row, = [r for r in rows if r["name"] == "balance"]
    assert row == {"name": "balance", "where": "user", "schedule": "*/10 * * * *",
                   "command": "{doit} cron-run balance -- {doit} balance >> {root}/logs/balance.log 2>&1",
                   "path": "{doit}", "sig": r"\bdoit balance\b", "owner": "thinker", "project": "do-it-v2"}
    assert [r["name"] for r in rows if re.search(row["sig"], r["command"].replace("{doit}", "/repo/doit"))] == ["balance"]
    command = row["command"].replace("{doit}", "/repo/doit")
    assert [r["name"] for r in rows if re.search(r["sig"], command)] == ["balance"]
    help_text = subprocess.check_output([str(REPO / "doit"), "--help"], text=True)
    assert "doit balance [--dry-run] | hold | resume" in help_text
    print("1130-AC8 ok")
    response("AC8", {"cli_status": result.returncode, "cron_rows": len(rows), "signature": "unique"})


if __name__ == "__main__":
    test_transitions()
    test_integration()
