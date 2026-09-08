#!/usr/bin/env python3
"""doit paid-call <label> -- <command...>  — §10.4's logging wrapper.

*Any call that costs money is logged and attributed. No silent quota drains.*

`probe` (§4.6·10) is the one contract that spends **before** anything lands on
head, so the shared pre-dispatch gate reaches it never: its spend goes through
this wrapper by its `Tools` field instead, and the wrapper — not the model —
enforces the declared spend cap (D96).

- the log is `spend.jsonl` in the run directory (cwd by default), one line per
  call: label, argv, cost, cumulative, and where the cost came from;
- **the cap is checked before the call and again after it.** A call that would
  cross the cap does not run: exit 3, nothing spent. A call that crossed it on
  its own return is logged and the *next* one is refused — an after-the-fact cap
  is the honest ceiling when the price is only known on return;
- cost is read from the command's own JSON stdout (`total_cost_usd`, else
  `cost_usd`), else from `DOIT_CALL_USD`, else 0 **recorded as `unpriced`** —
  an unpriced call is visible, never silently free.
"""
import argparse, json, os, pathlib, subprocess, sys


def read(log):
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


def price(stdout):
    """The command's own report of what it cost, or None."""
    for line in (stdout or "").splitlines()[::-1] + [stdout or ""]:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(d, dict):
            for k in ("total_cost_usd", "cost_usd"):
                if isinstance(d.get(k), (int, float)):
                    return float(d[k]), k
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit paid-call", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", help="what this call is — the attribution half of §10.4")
    ap.add_argument("--run-dir", default=None, help="where spend.jsonl lives (default: cwd)")
    ap.add_argument("--cap", type=float, default=None,
                    help="declared spend cap in USD (default: $DOIT_SPEND_CAP_USD, else uncapped is REFUSED)")
    ap.add_argument("cmd", nargs="*", help="-- then the command that spends")
    # argparse's REMAINDER swallows the flags too, so `--` is split off by hand.
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    a = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if not cmd:
        sys.exit("paid-call: nothing to run — usage: doit paid-call <label> -- <command...>")

    cap = a.cap if a.cap is not None else (
        float(os.environ["DOIT_SPEND_CAP_USD"]) if os.environ.get("DOIT_SPEND_CAP_USD") else None)
    if cap is None:
        # §4.4 correction #9: budgets are never uncapped. The one contract that
        # spends at planning time is the last place to make an exception.
        sys.exit("paid-call: no spend cap — pass --cap or set DOIT_SPEND_CAP_USD. "
                 "A paid call with no declared cap is refused (§10.4, D96).")

    run_dir = pathlib.Path(a.run_dir or os.getcwd())
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "spend.jsonl"
    spent = sum(e.get("cost_usd", 0.0) for e in read(log))
    if spent >= cap:
        sys.stderr.write(f"paid-call: REFUSED {a.label} — ${spent:.4f} already spent of a ${cap:.4f} cap. "
                         f"Nothing ran. Stop, write the run record, return complete: false.\n")
        return 3

    p = subprocess.run(cmd, capture_output=True, text=True)
    got = price(p.stdout)
    cost, src = got if got else (float(os.environ.get("DOIT_CALL_USD", 0) or 0),
                                 "DOIT_CALL_USD" if os.environ.get("DOIT_CALL_USD") else "unpriced")
    spent += cost
    with open(log, "a") as fh:
        fh.write(json.dumps({"label": a.label, "argv": cmd, "cost_usd": cost, "cost_from": src,
                             "cumulative_usd": round(spent, 6), "cap_usd": cap,
                             "exit": p.returncode}, sort_keys=True) + "\n")
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    sys.stderr.write(f"paid-call: {a.label} ${cost:.4f} ({src}) · ${spent:.4f} of ${cap:.4f}\n")
    if spent >= cap:
        sys.stderr.write("paid-call: the cap is now reached — the next call is refused.\n")
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
