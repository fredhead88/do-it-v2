#!/usr/bin/env python3
"""§4.11's deploy script, against the failure its budget line names: reporting a
deploy as landed when it did not.

Every check runs the real script as a subprocess against a real bash command in a
scratch DOIT_ROOT — the whole mechanism is what a handed-in command's exit code
and output make the script write, so a mocked subprocess would test nothing.
"""
import json, os, pathlib, subprocess, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

N = 0
SHA = "8714276abc12def3456789012345678901234567"
HERE = pathlib.Path(__file__).resolve().parent


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def run(root, *argv, **env):
    e = dict(os.environ, DOIT_ROOT=str(root), DOIT_DEPLOY_POLL="0.01", DOIT_DEPLOY_TIMEOUT="5")
    e.update({k: str(v) for k, v in env.items()})
    e.pop("DOIT_PROJECT", None)
    return subprocess.run([sys.executable, str(HERE / "deploy.py"), *argv],
                          capture_output=True, text=True, env=e)


def events(root):
    out = []
    for f in sorted((pathlib.Path(root) / "events").glob("*.jsonl")):
        out += [{**json.loads(l), "actor": f.stem} for l in f.read_text().splitlines() if l.strip()]
    return out


def types(root):
    return [e["type"] for e in events(root)]


# ── the honest landing ────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo shipping", "--check", f"echo running {SHA}")
    check(p.returncode == 0, f"a deploy whose check names the sha lands: {p.stderr}")
    check(types(d) == ["deploy-started", "deploy-landed", "worked"],
          f"§4.11's three events, in order, and nothing else: {types(d)}")
    e = events(d)[1]
    check(e["sha"] == SHA and e["target"] == "prod", "deploy-landed carries the sha and the target")
    check(events(d)[0]["type"] == "deploy-started",
          "★ started is written BEFORE the command runs — a deploy that hangs must be visible")
    check("deployed 8714276abc12 to prod" in p.stdout, "…and it renders as §4.11's line")

# ── the failure the script exists to prevent ──────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", "echo the service is up")
    check(p.returncode == 1,
          "★ a check that exits 0 without naming the sha proves the SERVICE answers, "
          "not that THIS BUILD does — that is not landed")
    check(types(d) == ["deploy-started", "deploy-failed", "blocked-external"],
          f"…and it is a failure with an external cause: {types(d)}")
    check("never reported" in events(d)[1]["why"], "the why says what was not established")
    check(SHA[:12] in p.stderr and "git revert --no-edit -m 1" in p.stderr,
          "★ rollback first (§5.8): the failure prints the revert line with the sha in it")

with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo boom >&2; exit 7", "--check", f"echo {SHA}")
    check(p.returncode == 1, "a deploy command that fails is a failed deploy")
    check("exited 7" in events(d)[1]["why"], "…and the exit code is on the event")
    check("boom" in events(d)[1]["log_tail"], "log_tail carries the output, and only on failure")
    check(types(d).count("deploy-landed") == 0,
          "★ the check is never consulted after the command failed — no landed event")

with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", "exit 1", DOIT_DEPLOY_TIMEOUT="0.3")
    check(p.returncode == 1, "a check that never passes is a failed deploy, not a hung one")
    check("deploy-failed" in types(d), "…recorded, with the cap in the why")
    check(len([t for t in types(d) if t == "deploy-started"]) == 1, "one attempt, one started event")

with tempfile.TemporaryDirectory() as d:
    # the check runs at least once even with no time left: a deploy that finished
    # exactly at the cap must not be failed for a poll that never happened.
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", f"echo {SHA}", DOIT_DEPLOY_TIMEOUT="0.001")
    check(p.returncode == 0, "★ the check runs at least once — the cap does not skip the verdict")

# ── what is refused before anything runs ──────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", "abc", "--target", "prod", "--cmd", "true", "--check", "echo abc")
    check(p.returncode != 0, "a sha too short to identify a build is refused")
    check(types(d) == [], "…before any event — nothing was attempted")
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod", "--cmd", "echo ok")
    check(p.returncode != 0, "★ no --check is refused: a deploy with no proof is not a deploy")
    check(types(d) == [], "…and again nothing was written")

# ── serial, always ────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    (pathlib.Path(d) / "events").mkdir(parents=True)
    slow = subprocess.Popen([sys.executable, str(HERE / "deploy.py"), "L-spec-0001", "--sha", SHA,
                             "--target", "prod", "--cmd", "sleep 2", "--check", f"echo {SHA}"],
                            env=dict(os.environ, DOIT_ROOT=d, DOIT_DEPLOY_TIMEOUT="10"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(400):                     # wait for the lock to be taken, not for a clock
        if (pathlib.Path(d) / "deploy.lock").exists() and types(d):
            break
        __import__("time").sleep(0.01)
    p = run(d, "L-spec-0002", "--sha", SHA, "--target", "prod", "--cmd", "echo x", "--check", f"echo {SHA}")
    check(p.returncode == 3, f"★ a second concurrent deploy is dropped, never queued: {p.returncode} {p.stdout}")
    check([e for e in events(d) if e["subject"] == "L-spec-0002"] == [],
          "…with no event at all: nothing was attempted, so nothing external is blocked")
    slow.wait(timeout=30)

# ── the environment the handed-in commands get ────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "staging",
            "--cmd", "echo deploying $DOIT_TARGET", "--check", 'echo "$DOIT_SHA"')
    check(p.returncode == 0, "the commands get DOIT_SHA and DOIT_TARGET, so no string splicing")

# ── the fold's rule, not the script's ─────────────────────────────────────────
import fold  # noqa: E402
check(fold.EMITS["deploy-landed"] == {"executor", "operator"},
      "★ deploy-landed is the clearing direction — only the seat that ran the deploy may claim it")
check("deploy-failed" not in fold.EMITS and "deploy-started" not in fold.EMITS,
      "…while reporting a failure stays open to any actor (the safe direction)")

with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d); (root / "events").mkdir(parents=True)
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod", "--cmd", "true",
            "--check", f"echo {SHA}", DOIT_DEPLOY_LEDGER_FILE="L-builder-0001.jsonl")
    check(p.returncode == 0, "the script does not police who runs it")
    ev = subprocess.run([sys.executable, str(HERE / "fold.py"), "states"],
                        capture_output=True, text=True, env=dict(os.environ, DOIT_ROOT=d))
    board = subprocess.run([sys.executable, str(HERE / "fold.py")], capture_output=True, text=True,
                           env=dict(os.environ, DOIT_ROOT=d)).stdout
    check("unauthorized events recorded and ignored: 1" in board,
          f"★ …the FOLD does: a builder's deploy-landed is recorded and ignored:\n{board[-400:]}")

print(f"deploy: {N} checks pass")
