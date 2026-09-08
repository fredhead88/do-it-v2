#!/usr/bin/env python3
"""deploy — serial, it waits, and it verifies the sha is live (§4.11, D21/D88).

  deploy.py <spec> --sha SHA --target NAME --cmd '<deploy>' --check '<proves the sha is live>'
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

★ SERIAL, ALWAYS. One deploy at a time per ledger root, held by `flock`. A second
concurrent request is dropped (exit 3) with no event: nothing was attempted, so
nothing external is blocked, and the tick that poked it comes round again.

★ UNDETERMINED IS NEVER CLEAN. The command failing, the check never naming the
sha, and the wall-clock cap expiring are one status: `failed`. Rollback is the
caller's next line (§5.8) and `deploy-failed` carries the sha it needs.
"""
import argparse, fcntl, os, pathlib, subprocess, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402

TAIL = 2000          # log_tail is for the operator's first glance, not for the ledger to store a build log


def shell(cmd, timeout, env):
    """Whatever the caller handed in, run as it was handed in. A deploy command is
    the target's, never this script's to parse."""
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=max(timeout, 1), env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") + (e.stderr or b"")
        return 124, (out.decode(errors="replace") if isinstance(out, bytes) else str(out))


def live(sha, out, code):
    """Exit 0 AND the sha named. Either alone is a different claim than 'this sha
    is live' — exit 0 alone says the service answers, and the sha alone says the
    check printed a string."""
    return code == 0 and sha[:12] in out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit deploy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", help="the subject the deploy is recorded against")
    ap.add_argument("--sha", required=True, help="the merged sha this deploy must put live")
    ap.add_argument("--target", required=True, help="which surface — the deploy is recorded per target")
    ap.add_argument("--cmd", required=True, help="the deploy command for the target")
    ap.add_argument("--check", required=True, help="the command that PROVES the sha is live; must print it")
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

    env = dict(os.environ, DOIT_SHA=a.sha, DOIT_TARGET=a.target)
    fold.append(["deploy-started", a.spec, f"sha={a.sha}", f"target={a.target}"])
    t0 = time.monotonic()
    code, out = shell(a.cmd, a.timeout, env)
    why = None if code == 0 else f"deploy command exited {code}"

    while why is None:
        ccode, cout = shell(a.check, max(a.timeout - (time.monotonic() - t0), 1), env)
        out += cout
        if live(a.sha, cout, ccode):
            break
        if time.monotonic() - t0 >= a.timeout:
            why = (f"the check never reported {a.sha[:12]} live within {a.timeout:g}s"
                   f" (last exit {ccode})")
            break
        time.sleep(a.poll)

    elapsed = round(time.monotonic() - t0, 1)
    if why is None:
        fold.append(["deploy-landed", a.spec, f"sha={a.sha}", f"target={a.target}",
                     f"elapsed_s:={elapsed}"])
        fold.append(["worked", a.spec, "what=deploy", f"target={a.target}"])
        print(f"deployed {a.sha[:12]} to {a.target} at {fold.NOW.isoformat(timespec='seconds')} ({elapsed}s)")
        return 0
    fold.append(["deploy-failed", a.spec, f"sha={a.sha}", f"target={a.target}",
                 f"why={why}", f"elapsed_s:={elapsed}", f"log_tail={out[-TAIL:]}"])
    # `blocked-external`: what failed is the target, not the build. §4.11 names
    # both events because the board reads them differently.
    fold.append(["blocked-external", a.spec, f"why={why}", f"target={a.target}"])
    sys.stderr.write(f"deploy: FAILED {a.sha[:12]} -> {a.target} — {why}\n{out[-TAIL:]}\n"
                     f"deploy: rollback first (§5.8) — git revert --no-edit -m 1 {a.sha}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
