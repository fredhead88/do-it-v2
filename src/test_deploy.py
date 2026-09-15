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
    check("deployed 8714276 to prod" in p.stdout,
          "…and it renders as §4.11's line, with the 7-char sha (S34)")

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
    check(SHA[:7] in p.stderr, "the failure names the sha (7-char, S34) in the advice")
    check("git revert" not in p.stderr,
          "★ v: a target failure no longer advises git revert — that line is gone")
    check("no --rollback given" in p.stderr,
          "…it names that no rollback was given, since none was passed")
    check(events(d)[1]["rollback"] == "absent", "…and the event says the same: rollback=absent")

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

# ── a-4·i: live() matches sha[:7], not sha[:12] (S34) ──────────────────────────
with tempfile.TemporaryDirectory() as d:
    nine = SHA[:9]                            # the real target's check prints exactly this shape
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", f"echo build {nine} is live")
    check(p.returncode == 0,
          f"★ i: a check that prints only a 9-char sha (the real target's shape) must land, "
          f"not false-fail on a 12-char match: {p.stderr}")
    check(types(d) == ["deploy-started", "deploy-landed", "worked"],
          "…12 minutes of false deploy-failed is exactly this bug")

# ── a-4·ii: output streams to the log AS PRODUCED, and the event carries it ────
with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d); (root / "events").mkdir(parents=True)
    proc = subprocess.Popen([sys.executable, str(HERE / "deploy.py"), "L-spec-0001", "--sha", SHA,
                             "--target", "prod", "--cmd",
                             "echo partial-output-marker; sleep 5", "--check", f"echo {SHA}"],
                            env=dict(os.environ, DOIT_ROOT=d, DOIT_DEPLOY_TIMEOUT="30",
                                    DOIT_DEPLOY_POLL="0.01"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logdir = root / "logs"
    seen = False
    for _ in range(500):                      # wait for the marker to land, not for a clock
        if logdir.exists():
            logs = list(logdir.glob("*.log"))
            if logs and "partial-output-marker" in logs[0].read_text():
                seen = True
                break
        __import__("time").sleep(0.02)
    check(seen, "★ ii: the log carries output WHILE the command is still running, not only at exit")
    logfile = list(logdir.glob("*.log"))[0]
    check(logfile.name.startswith(f"deploy-L-spec-0001-{SHA[:7]}-") and logfile.name.endswith(".log"),
          f"log filename matches deploy-<spec>-<sha7>-<n>.log: {logfile.name}")
    proc.kill()
    proc.wait(timeout=10)
    check("partial-output-marker" in logfile.read_text(),
          "…and a KILLED run leaves the log so far, not nothing")

with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo x", "--check", f"echo {SHA}")
    ev = events(d)
    check(all("log" in e for e in ev if e["type"] in ("deploy-started", "deploy-landed")),
          "deploy-started and deploy-landed both carry log=<path>")
    check(pathlib.Path(ev[0]["log"]).exists(), "…and the path is real, not decoration")
    check("log_tail" not in ev[1], "log_tail is a failure field — landed never carries one")

# ── a-4·iii: a dropped second launch opens NOTHING, log included ───────────────
with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d); (root / "events").mkdir(parents=True)
    slow = subprocess.Popen([sys.executable, str(HERE / "deploy.py"), "L-spec-0001", "--sha", SHA,
                             "--target", "prod", "--cmd", "sleep 2", "--check", f"echo {SHA}"],
                            env=dict(os.environ, DOIT_ROOT=d, DOIT_DEPLOY_TIMEOUT="10"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(400):
        if (root / "deploy.lock").exists() and types(d):
            break
        __import__("time").sleep(0.01)
    p = run(d, "L-spec-0002", "--sha", SHA, "--target", "prod", "--cmd", "echo x", "--check", f"echo {SHA}")
    check(p.returncode == 3, f"a second concurrent deploy is still dropped: {p.returncode}")
    logs = list((root / "logs").glob("*")) if (root / "logs").exists() else []
    check(len(logs) == 1,
          f"★ iii: the dropped request opened no log of its own — only the first's exists: {logs}")
    slow.wait(timeout=30)

# ── a-4·iv: a gate refusal is exit 2 deploy-refused, --cmd never runs ──────────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--gate", "echo gate-checked; exit 9",
            "--cmd", "echo CMD-RAN-MARKER", "--check", f"echo {SHA}")
    check(p.returncode == 2, f"★ iv: a gate refusal exits 2, not 1: {p.returncode}")
    check(types(d) == ["deploy-started", "deploy-refused"],
          f"…and it is deploy-refused, never deploy-failed/blocked-external: {types(d)}")
    check("blocked-external" not in types(d),
          "deploy-refused is NOT blocked-external — nothing external was touched (S8/S17)")
    logtext = pathlib.Path(events(d)[1]["log"]).read_text()
    check("gate-checked" in logtext, "the gate ran")
    check("CMD-RAN-MARKER" not in logtext,
          "★ iv: --cmd never ran — refused before the target was touched")

with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--gate", "exit 0", "--cmd", "exit 5", "--check", f"echo {SHA}")
    check(p.returncode == 1,
          "a gate that PASSES still lets a failing --cmd read as deploy-failed (exit 1), the "
          "target may have changed")
    check(types(d) == ["deploy-started", "deploy-failed", "blocked-external"], types(d))

# ── a-4·v: --rollback is the target's own command, run on a failed check ───────
with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", "echo the service is up",   # never names the sha -> check fails
            "--rollback", "echo rollback-ran-marker")
    check(p.returncode == 1, "still a failed deploy")
    failed = [e for e in events(d) if e["type"] == "deploy-failed"][0]
    check(failed["rollback"] == "ran",
          f"★ v: rollback=ran when --cmd succeeded but the check never confirmed: {failed}")
    check("rollback-ran-marker" in pathlib.Path(failed["log"]).read_text(),
          "…and its output joins the SAME log")
    check("rollback ran" in p.stderr, "…and stderr names it, not git revert")

with tempfile.TemporaryDirectory() as d:
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "echo ok", "--check", "echo the service is up",
            "--rollback", "exit 3")
    failed = [e for e in events(d) if e["type"] == "deploy-failed"][0]
    check(failed["rollback"] == "failed", "rollback=failed when the rollback command itself exits non-zero")
    check("rollback FAILED" in p.stderr, "…and stderr says so, loudly")

with tempfile.TemporaryDirectory() as d:
    # --cmd itself failing is a different branch — rollback does not run there,
    # because --cmd's own exit already said what happened (see deploy.py docstring).
    p = run(d, "L-spec-0001", "--sha", SHA, "--target", "prod",
            "--cmd", "exit 5", "--check", f"echo {SHA}", "--rollback", "echo should-not-run")
    failed = [e for e in events(d) if e["type"] == "deploy-failed"][0]
    check(failed["rollback"] == "absent",
          "★ v: a failed --cmd does not trigger --rollback — only a failed check does")
    check("should-not-run" not in pathlib.Path(failed["log"]).read_text(),
          "…and the rollback command never actually ran")

# ── a-4·vi is fold's, not deploy.py's — see test_fold.py ───────────────────────

print(f"deploy: {N} checks pass")
