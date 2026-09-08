#!/usr/bin/env python3
"""§10.4's wrapper, against the failure it was written for: a silent quota drain.

`probe` is the one contract that spends before anything lands on head, so no
pre-dispatch gate is upstream of it. The cap here is the only thing between a
planning-time run and an overnight bill.
"""
import json, os, pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paid_call  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def run(*argv, **env):
    e = dict(os.environ, **{k: str(v) for k, v in env.items()})
    for k, v in env.items():
        if v is None:
            e.pop(k, None)
    p = subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "paid_call.py"), *argv],
                       capture_output=True, text=True, env=e)
    return p


def lines(d):
    f = pathlib.Path(d) / "spend.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


with tempfile.TemporaryDirectory() as d:
    # ── no cap is not a mode ──────────────────────────────────────────────────
    p = run("hello", "--run-dir", d, "--", "echo", "hi", DOIT_SPEND_CAP_USD=None)
    check(p.returncode != 0, "a paid call with no declared cap must be refused (§4.4 correction #9)")
    check("no spend cap" in p.stderr, "…and say which flag or variable supplies one")
    check(not lines(d), "…and spend nothing")

    # ── the ordinary call: it runs, its output passes through, it is logged ───
    p = run("first", "--run-dir", d, "--cap", "1.00", "--", "echo", "hello-from-the-external")
    check(p.returncode == 0, "the wrapped command's exit code is the wrapper's")
    check("hello-from-the-external" in p.stdout, "the wrapper is transparent on stdout")
    e = lines(d)
    check(len(e) == 1, "one call, one line")
    check(e[0]["label"] == "first" and e[0]["argv"] == ["echo", "hello-from-the-external"],
          "attribution is the label AND the argv — 'which call' is half of §10.4")
    check(e[0]["cost_from"] == "unpriced" and e[0]["cost_usd"] == 0,
          "★ a call that reports no price is logged as `unpriced`, never as silently free")

    # ── the price the command itself reports is the price ────────────────────
    run("priced", "--run-dir", d, "--cap", "1.00", "--", "printf",
        '{"total_cost_usd": 0.25, "x": 1}')
    e = lines(d)
    check(e[-1]["cost_usd"] == 0.25 and e[-1]["cost_from"] == "total_cost_usd",
          "the CLI's own JSON is where the cost comes from")
    check(e[-1]["cumulative_usd"] == 0.25, "…and it accumulates")
    run("env-priced", "--run-dir", d, "--cap", "1.00", "--", "echo", "no json here",
        DOIT_CALL_USD="0.10")
    e = lines(d)
    check(e[-1]["cost_usd"] == 0.10 and e[-1]["cost_from"] == "DOIT_CALL_USD",
          "a command that cannot report its price takes the declared per-call price")
    check(abs(e[-1]["cumulative_usd"] - 0.35) < 1e-9, "cumulative is cumulative")

with tempfile.TemporaryDirectory() as d:
    # ── the cap ──────────────────────────────────────────────────────────────
    run("a", "--run-dir", d, "--cap", "0.30", "--", "printf", '{"cost_usd": 0.20}')
    p = run("b", "--run-dir", d, "--cap", "0.30", "--", "printf", '{"cost_usd": 0.20}')
    check(p.returncode == 0, "0.20 of a 0.30 cap still leaves room — the check is before, not halfway")
    check(lines(d)[-1]["cumulative_usd"] == 0.4, "…and the call that crossed it is logged, not hidden")
    p = run("c", "--run-dir", d, "--cap", "0.30", "--", "printf", '{"cost_usd": 0.20}')
    check(p.returncode == 3, "★ the call AFTER the cap is crossed does not run")
    check("REFUSED" in p.stderr and "Nothing ran" in p.stderr, "…and says so")
    check("complete: false" in p.stderr, "…and tells the probe what to return (§4.6·10 Budget)")
    check(len(lines(d)) == 2, "a refused call writes no spend line — it spent nothing")

with tempfile.TemporaryDirectory() as d:
    p = run("env-cap", "--run-dir", d, "--", "echo", "x", DOIT_SPEND_CAP_USD="0.05")
    check(p.returncode == 0, "the cap may come from the environment the wrapper is spawned with")
    check(lines(d)[0]["cap_usd"] == 0.05, "…and the cap in force is on the line, so the log is self-explaining")
    p = run("nothing", "--run-dir", d, "--cap", "1.0")
    check(p.returncode != 0, "no command is an error, not a zero-cost success")

with tempfile.TemporaryDirectory() as d:
    p = run("failing", "--run-dir", d, "--cap", "1.0", "--", "false")
    check(p.returncode == 1, "a call that failed still returns its own code")
    check(lines(d)[0]["exit"] == 1, "★ and is still logged — a failed paid call is a paid call")

check(paid_call.price('{"total_cost_usd": 1.5}') == (1.5, "total_cost_usd"), "price reads the object")
check(paid_call.price('noise\n{"cost_usd": 2}\n') == (2.0, "cost_usd"), "…on the last JSON line, past noise")
check(paid_call.price("not json at all") is None, "…and reports nothing rather than guessing")
check(paid_call.price('{"total_cost_usd": "1.5"}') is None, "a string price is not a price")

print(f"paid-call: {N} checks pass")
