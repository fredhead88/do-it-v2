#!/usr/bin/env python3
"""deploy — serial, it waits, and it verifies the sha is live (§4.11, D21/D88).

  deploy.py <spec> --sha SHA --target NAME --cmd '<deploy>' --check '<proves the sha is live>'
            [--gate '<precondition>'] [--rollback '<target's own rollback>']
            [--timeout SEC] [--poll SEC]

*Not inline in the Executor, and not an agent.* Inline ties up the Executor's
context with deploy logs — the one thing §3.9 says it must never read — and gives
the deploy no independent budget and no independent failure record. Every input
is handed in and the answer is whatever the handed-in check returns, so nothing
here judges: it is a script for §4.11's standing reason, that **a script cannot
be reasoned out of a check and a model can.**

★ THE CHECK IS THE VERDICT, AND IT MUST NAME THE SHA. `--check` exiting 0 proves
something is answering; it does not prove *this build* is answering. `landed`
needs both: exit 0 **and** the sha in what the check printed. A deploy with no
`--check` is refused before anything runs — a script that cannot prove a deploy
landed must not be the thing that reports it landed (§4.11's own budget line).

★ THE MATCH IS `sha[:7]`, NOT `sha[:12]` (S34). The real target's check prints a
9-character short sha; a 12-character match against that is not stricter, it is
wrong — every real deploy false-failed and one sat 12 minutes under a false
`deploy-failed` before anyone looked. `--sha` itself is still refused below 7
characters, so a short match is never a match against noise.

★ SERIAL, ALWAYS. One deploy at a time per ledger root, held by `flock`. A second
concurrent request is dropped (exit 3) with no event and no log file of its own
(S30) — the flock is taken BEFORE anything is opened that the first run holds,
including the log, so a dropped request has touched nothing.

★ THE LOG STREAMS, IT IS NOT CAPTURED AND DUMPED AT THE END (S30/S34). Every byte
either command produces is written to `$R/logs/deploy-<spec>-<sha7>-<n>.log` as it
is produced — `n` the next free integer for that spec+sha7 pair — so a wrapper
that gets killed mid-deploy leaves the log so far, not silence. `deploy-started`,
`deploy-landed`, `deploy-failed` and `deploy-refused` all carry `log=<path>`;
`log_tail` (the last few KB) still rides on the failure/refusal events for a
first glance, but it is never the only record any more.

★ REFUSED IS NOT FAILED (S8/S17). A gate that says no before the target is
touched, and a deploy that touched the target and did not land, are different
claims and get different exit codes:
  - `--gate '<cmd>'` is an OPTIONAL precondition, run before `--cmd`, under the
    same lock and log. A non-zero exit REFUSES the deploy: `--cmd` never runs,
    the script exits **2**, and the ledger gets `deploy-refused{why}` — never
    `blocked-external`, because nothing external was touched.
  - Any other failure — `--cmd` itself exiting non-zero, or `--check` never
    reporting the sha live — means the target may have changed. The script
    exits **1** with `deploy-failed{why}` and (§4.11 as before) `blocked-external`.
  This repo chose the explicit `--gate` design over a timed window on `--cmd`
  (e.g. "non-zero within the first N seconds") on purpose: a window is a guess
  about what a fast failure means, and a gate is a fact the caller states
  outright — "this command's failure means nothing happened yet". A caller with
  no precondition worth separating just omits `--gate`, and every `--cmd`
  failure reads as `deploy-failed`, exactly as before this change.

★ ROLLBACK IS THE TARGET'S OWN COMMAND, RUN ON A FAILED CHECK (S30). `--rollback
'<cmd>'` (e.g. `./deploy.sh --rollback`) is optional. It runs when `--cmd`
succeeded but `--check` never reported the sha live within the timeout — the one
case where the target believably changed and nothing else is going to undo it.
It does NOT run when `--cmd` itself failed (that command's own exit already says
what happened, and §5.8's rollback-first is the caller's next line, not a second
guess layered on top) and it does NOT run on a gate refusal (nothing to undo).
Its output joins the same log; `deploy-failed` carries `rollback=ran|absent|failed`.
The stderr advice on a target failure never says `git revert` any more — it
names the rollback that ran, that it failed, or that none was given.

★ UNDETERMINED IS NEVER CLEAN. The command failing, the check never naming the
sha, and the wall-clock cap expiring are one status: `failed`. Rollback (above)
is the script's own next line where it applies; §5.8 is the caller's for
anything wider.

★ A KILLED WRAPPER IS VISIBLE (S34). `fold.render` puts a `deploy-started` with
no `deploy-landed`/`deploy-failed`/`deploy-refused` after it under IN FLIGHT as
`deploy in flight · <spec> · <sha7> · <age>` — so a deploy whose process died
between `deploy-started` and its terminal event does not read as "nothing is
happening" on the board.
"""
import argparse, fcntl, os, pathlib, select, subprocess, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402

TAIL = 2000          # log_tail is for the operator's first glance, not for the ledger to store a build log


def stream(cmd, timeout, env, log_fh):
    """Run cmd, writing its combined stdout+stderr to log_fh AS PRODUCED — not
    captured and dumped at the end (S30/S34): a killed run leaves the log file
    with everything up to the kill, not nothing. Returns (returncode, full_output)
    exactly like the old capture-then-dump helper, so `live()`, the `why`
    messages and `log_tail` are unchanged.

    Mirrors the old helper's floor: the command always gets at least 1 second,
    even when the caller hands in less — a cap that could starve the very
    command it is timing would make "the check runs at least once" false."""
    try:
        p = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    except OSError as e:
        return 127, str(e)
    out = []
    deadline = time.monotonic() + max(timeout, 1)
    fd = p.stdout.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if p.poll() is None:
                p.kill()
            break
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if ready:
            line = p.stdout.readline()
            if line == "":                    # EOF: the process closed stdout
                break
            out.append(line)
            log_fh.write(line)
            log_fh.flush()
        elif p.poll() is not None:
            break
    try:
        p.wait(timeout=1)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    code = p.returncode if p.returncode is not None else 124
    return code, "".join(out)


def live(sha, out, code):
    """Exit 0 AND the sha named, matched on the first 7 characters (S34) — the
    real target's check prints a 9-character short sha, and matching more than
    it ever prints can never pass. Either exit-0-alone or sha-alone is a
    different claim than 'this sha is live' — exit 0 alone says the service
    answers, and the sha alone says the check printed a string."""
    return code == 0 and sha[:7] in out


def next_log_path(spec, sha7):
    """`$R/logs/deploy-<spec>-<sha7>-<n>.log`, n the next free integer for this
    spec+sha7 pair, so a re-run against the same build does not clobber the
    prior attempt's log. Caller must hold the deploy lock before calling this —
    it lists the directory and a second concurrent lister would race it."""
    logs_dir = fold.ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"deploy-{spec}-{sha7}-"
    used = [int(tail) for p in logs_dir.glob(f"{prefix}*.log")
            if (tail := p.stem[len(prefix):]).isdigit()]
    n = (max(used) + 1) if used else 1
    return logs_dir / f"{prefix}{n}.log"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit deploy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="the subject the deploy is recorded against")
    ap.add_argument("--sha", required=True, help="the merged sha this deploy must put live")
    ap.add_argument("--target", required=True, help="which surface — the deploy is recorded per target")
    ap.add_argument("--cmd", required=True, help="the deploy command for the target")
    ap.add_argument("--check", required=True, help="the command that PROVES the sha is live; must print it")
    ap.add_argument("--gate", default=None,
                    help="optional precondition, run BEFORE --cmd under the same lock and log. A "
                         "non-zero exit REFUSES the deploy (exit 2, deploy-refused) and --cmd never "
                         "runs — the target is provably untouched. Chosen over a timed window on "
                         "--cmd itself: a window is a guess about what a fast failure means, a gate "
                         "is the caller stating the fact outright.")
    ap.add_argument("--rollback", default=None,
                    help="the target's own rollback command (e.g. './deploy.sh --rollback'), run "
                         "when --cmd succeeded but --check never reported the sha live. Output joins "
                         "the deploy log; deploy-failed records rollback=ran|absent|failed.")
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("DOIT_DEPLOY_TIMEOUT", 600)),
                    help="wall-clock cap for command + wait (default $DOIT_DEPLOY_TIMEOUT or 600)")
    ap.add_argument("--poll", type=float, default=float(os.environ.get("DOIT_DEPLOY_POLL", 10)),
                    help="seconds between checks while waiting")
    a = ap.parse_args(argv)

    if len(a.sha) < 7:
        # A 4-character "sha" matches half the strings a check could print, so
        # `live()` would pass on noise. Refuse rather than verify against nothing.
        sys.exit(f"deploy: {a.sha!r} is too short to identify a build — pass the full merge sha")
    sha7 = a.sha[:7]

    # The Executor's own file when it calls this (D90: the filename is the actor).
    os.environ["DOIT_LEDGER_FILE"] = os.environ.get(
        "DOIT_DEPLOY_LEDGER_FILE", os.environ.get("DOIT_LEDGER_FILE", "L-executor-0001.jsonl"))
    fold.EVENTS.mkdir(parents=True, exist_ok=True)
    lock = open(fold.ROOT / "deploy.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("deploy: one is already running; this one is dropped (§4.11: serial, always)")
        return 3

    # ★ The flock above is taken BEFORE anything is opened that the first run
    # holds — including the log (S30). A dropped request returned already, above,
    # and opened nothing: no logs/ listing, no log file, no counter read.
    log_path = next_log_path(a.spec, sha7)
    log_fh = open(log_path, "w", buffering=1)

    env = dict(os.environ, DOIT_SHA=a.sha, DOIT_TARGET=a.target)
    fold.append(["deploy-started", a.spec, f"sha={a.sha}", f"target={a.target}", f"log={log_path}"])
    t0 = time.monotonic()

    if a.gate:
        gcode, gout = stream(a.gate, a.timeout, env, log_fh)
        if gcode != 0:
            elapsed = round(time.monotonic() - t0, 1)
            why = f"gate exited {gcode} — refused before the target was touched"
            fold.append(["deploy-refused", a.spec, f"sha={a.sha}", f"target={a.target}",
                        f"why={why}", f"elapsed_s:={elapsed}", f"log={log_path}",
                        f"log_tail={gout[-TAIL:]}"])
            log_fh.close()
            sys.stderr.write(f"deploy: REFUSED {sha7} -> {a.target} — {why}\n{gout[-TAIL:]}\n"
                             f"deploy: --cmd never ran; nothing to roll back\n")
            return 2

    code, out = stream(a.cmd, a.timeout, env, log_fh)
    why = None if code == 0 else f"deploy command exited {code}"

    while why is None:
        ccode, cout = stream(a.check, max(a.timeout - (time.monotonic() - t0), 1), env, log_fh)
        out += cout
        if live(a.sha, cout, ccode):
            break
        if time.monotonic() - t0 >= a.timeout:
            why = (f"the check never reported {sha7} live within {a.timeout:g}s"
                   f" (last exit {ccode})")
            break
        time.sleep(a.poll)

    elapsed = round(time.monotonic() - t0, 1)
    if why is None:
        fold.append(["deploy-landed", a.spec, f"sha={a.sha}", f"target={a.target}",
                     f"elapsed_s:={elapsed}", f"log={log_path}"])
        fold.append(["worked", a.spec, "what=deploy", f"target={a.target}"])
        log_fh.close()
        print(f"deployed {sha7} to {a.target} at {fold.NOW.isoformat(timespec='seconds')} ({elapsed}s)")
        return 0

    # ★ Rollback runs ONLY on the check-never-live branch: --cmd succeeded, so
    # the target plausibly changed, and nothing else is going to undo it. A
    # --cmd that itself failed already said what happened via its own exit —
    # this script does not layer a second guess on top of that (§5.8 is the
    # caller's next line for that case, unchanged).
    rollback = "absent"
    if a.rollback and code == 0:
        rcode, rout = stream(a.rollback, a.timeout, env, log_fh)
        out += rout
        rollback = "ran" if rcode == 0 else "failed"

    fold.append(["deploy-failed", a.spec, f"sha={a.sha}", f"target={a.target}",
                f"why={why}", f"elapsed_s:={elapsed}", f"log_tail={out[-TAIL:]}",
                f"log={log_path}", f"rollback={rollback}"])
    # `blocked-external`: what failed is the target, not the build. §4.11 names
    # both events because the board reads them differently.
    fold.append(["blocked-external", a.spec, f"why={why}", f"target={a.target}"])
    log_fh.close()
    if rollback == "ran":
        advice = f"deploy: rollback ran — {a.rollback!r}"
    elif rollback == "failed":
        advice = f"deploy: rollback FAILED — {a.rollback!r} — target may still be broken, intervene manually"
    elif a.rollback:
        advice = ("deploy: rollback not run — --rollback only runs when the check never confirms "
                  "the sha live, not on a failed deploy command")
    else:
        advice = "deploy: no --rollback given — nothing was rolled back automatically"
    sys.stderr.write(f"deploy: FAILED {sha7} -> {a.target} — {why}\n{out[-TAIL:]}\n{advice}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
