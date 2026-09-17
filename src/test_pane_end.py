#!/usr/bin/env python3
"""pane_end's four preconditions, against the failures it exists to prevent:
ending a pane on a claim the fold would ignore, ending over a pending seat
packet, ending before the handover is on disk, and ending with nothing able to
replace the pane. Run: env -u DOIT_PROJECT python3 test_pane_end.py

No check here sends a real signal or touches a real ledger: `kill`,
`find_ancestor`, `pending_packets` and `child_env` all arrive injected, and the
one check that must prove the REAL import path (the relay seam) imports a stub
this file writes under a tmp dir.

★ The environment is pinned BEFORE `import fold`. `fold.PROJECT` is bound at
import from DOIT_PROJECT and filters every row `read_events()` returns, so a
fixture row written with no `project` field is invisible under the project every
pane on this box actually runs with — the module would refuse for a reason that
has nothing to do with its code. `DOIT_LEDGER_FILE` is pinned for the same class
of reason: `fold.append` defaults to `L-operator-local.jsonl`, so an unpinned
fixture would prove a row whose actor is `operator` while production (up.py's
`DOIT_LEDGER_FILE=L-planner-NNNN.jsonl`) writes one whose actor is `planner` —
and only the second is the row R8 asks for.
"""
import json, os, pathlib, shutil, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
A, B = TMP / "rootA", TMP / "rootB"
os.environ["DOIT_ROOT"] = str(A)
os.environ.pop("DOIT_PROJECT", None)
os.environ["DOIT_LEDGER_FILE"] = "L-planner-0001.jsonl"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold, pane_end  # noqa: E402

N = 0
CHARTER, OTHER = "L-charter-0020", "L-charter-0099"
TS = "2026-09-17T00:00:00+00:00"


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


class Spy:
    """Records its calls, and (for the kill spy) what was on disk when it fired —
    the only way to prove content-before-signal without reading the clock."""

    def __init__(self, ret=None, watch=None):
        self.calls, self.ret, self.watch, self.disk = [], ret, watch, []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        self.disk.append(self.watch.read_text() if self.watch and self.watch.exists() else "")
        return self.ret


def ledger(root, **files):
    shutil.rmtree(root, ignore_errors=True)
    (root / "events").mkdir(parents=True)
    for name, evs in files.items():
        (root / "events" / name).write_text(
            "".join(json.dumps({"v": 1, "ts": TS, **e}) + "\n" for e in evs))
    return root


def lines(root):
    return {p.name: len(p.read_text().splitlines())
            for p in sorted((root / "events").glob("*.jsonl"))}


def l1(charter=CHARTER, **extra):
    return [{"type": "l1-complete", "subject": charter, **extra}]


def handover(name="handover.md", text="charter · units · waves · ids written\n"):
    p = TMP / name
    p.write_text(text)
    return p


def proc_tree(name, chain):
    """A synthetic `/proc`: {pid: (Name, PPid)} written as real status files."""
    d = TMP / name
    for pid, (nm, ppid) in chain.items():
        (d / str(pid)).mkdir(parents=True, exist_ok=True)
        (d / str(pid) / "status").write_text(f"Name:\t{nm}\nState:\tS (sleeping)\nPPid:\t{ppid}\n")
    return d


def refuses(why, root, **kw):
    """Run the module and assert NO effect of any kind: no end, no signal, no
    line anywhere on the ledger."""
    before = lines(root)
    kill = Spy()
    ended, reason = pane_end.check_and_end(
        CHARTER, kw.pop("handover_path", HANDOVER), root=root, kill=kill,
        find_ancestor=kw.pop("find_ancestor", lambda: 4242),
        child_env=kw.pop("child_env", {"DOIT_SUPERVISED": "1"}),
        **kw)
    check(ended is False, f"{why}: must refuse, got ended={ended} ({reason})")
    check(kill.calls == [], f"{why}: refused but signalled {kill.calls}")
    check(lines(root) == before, f"{why}: refused but the ledger grew — {before} -> {lines(root)}")
    return reason


HANDOVER = handover()
EMPTY = handover("empty.md", "")
CLEAR = lambda events, root: []          # noqa: E731 — an injected pending_packets

# ─────────────────────────── AC6 · the ancestor walk ───────────────────────────
# The pane's chain is claude → shell → python: a one-hop answer signals the shell.
deep = proc_tree("proc_deep", {10: ("python3", 9), 9: ("bash", 8), 8: ("claude", 1), 1: ("init", 0)})
check(pane_end.ancestor_claude_pid(10, deep) == 8,
      "ancestor walk: returns the GRANDPARENT claude, not the immediate parent")
none = proc_tree("proc_none", {20: ("python3", 19), 19: ("bash", 1), 1: ("init", 0)})
check(pane_end.ancestor_claude_pid(20, none) is None,
      "ancestor walk: no claude before pid 1 -> None, never a guess")
check(pane_end.ancestor_claude_pid(999, deep) is None,
      "ancestor walk: an unreadable /proc entry refuses, it does not raise")

# ───────────────────── AC1 · an l1-complete the FOLD would count ─────────────────────
r = refuses("no l1-complete at all", ledger(B, **{"L-planner-0001.jsonl": []}), pending_packets=CLEAR)
check("l1-complete" in r, f"reason must name l1-complete: {r}")
r = refuses("l1-complete for a DIFFERENT charter",
            ledger(B, **{"L-planner-0001.jsonl": l1(OTHER)}), pending_packets=CLEAR)
check("l1-complete" in r, f"reason must name l1-complete: {r}")
# ★ the case a bare type+subject filter gets wrong: `builder` is outside
# fold.EMITS["l1-complete"], so fold.fold() drops this row into `ignored` and the
# board never counts it. A pane that ended on it would end on a claim nothing holds.
r = refuses("l1-complete from an UNAUTHORIZED actor",
            ledger(B, **{"L-builder-0001.jsonl": l1()}), pending_packets=CLEAR)
check("l1-complete" in r, f"reason must name l1-complete: {r}")
check(fold.EMITS["l1-complete"] == {"planner", "operator"},
      "the authorized set is read from fold.EMITS, not copied here")
# ★ read THROUGH fold.PROJECT's filter, not around it.
fold.PROJECT = "p"
try:
    r = refuses("l1-complete filtered out by fold.PROJECT",
                ledger(B, **{"L-planner-0001.jsonl": l1(project="other")}), pending_packets=CLEAR)
    check("l1-complete" in r, f"reason must name l1-complete: {r}")
finally:
    fold.PROJECT = None

# ───────────────────────── AC2 · the seat relay (R9) ─────────────────────────
ledger(B, **{"L-planner-0001.jsonl": l1()})
r = refuses("a pending seat packet is outstanding", B,
            pending_packets=lambda e, root: [{"spawn": "L-builder-0047", "age_min": 3.0}])
check("pending" in r, f"reason must name the pending packet: {r}")


def boom(events, root):
    raise RuntimeError("relay is unhappy")


r = refuses("pending_packets raised", B, pending_packets=boom)
check("pending" in r.lower() or "unresolved" in r, f"reason must name the failed lookup: {r}")
# ★ Run BEFORE the stub below exists: `src/relay.py` is not on this tree, so the
# real import fails, and an unresolvable check must read exactly like a non-empty one.
check(pane_end.RELAY_MODULE not in sys.modules, "the relay stub must not exist yet")
r = refuses("no injection and no importable relay module", B)
check(pane_end.RELAY_MODULE in r, f"reason must name the module it could not resolve: {r}")

# ──────────────────── AC3 · content before event (§9.2 rule 3) ────────────────────
r = refuses("handover file does not exist", B, pending_packets=CLEAR,
            handover_path=TMP / "nope.md")
check("handover" in r, f"reason must name the handover: {r}")
r = refuses("handover file is empty", B, pending_packets=CLEAR, handover_path=EMPTY)
check("handover" in r, f"reason must name the handover: {r}")

# ──────────────────────── AC4a · the supervised interlock ────────────────────────
r = refuses("supervised marker absent", B, pending_packets=CLEAR, child_env={})
check(pane_end.SUPERVISED_ENV in r, f"reason must name the marker: {r}")
r = refuses("supervised marker present but EMPTY", B, pending_packets=CLEAR,
            child_env={"DOIT_SUPERVISED": ""})
check(pane_end.SUPERVISED_ENV in r, f"reason must name the marker: {r}")
check(pane_end.supervised({}) is False and pane_end.supervised({"DOIT_SUPERVISED": "1"}) is True,
      "supervised() is presence-and-non-empty, never a default True")

# ───────────── AC5 · append into the INJECTED root, then signal, in that order ─────────────
ledger(A)                                        # the root DOIT_ROOT points at: stays untouched
ledger(B, **{"L-planner-0001.jsonl": l1()})
LEDGER_B = B / "events" / "L-planner-0001.jsonl"
kill, walk = Spy(watch=LEDGER_B), Spy(ret=4242)
ended, reason = pane_end.check_and_end(CHARTER, HANDOVER, root=B, pending_packets=CLEAR,
                                       child_env={"DOIT_SUPERVISED": "1"},
                                       kill=kill, find_ancestor=walk)
check(ended is True, f"all four preconditions hold -> the pane ends: {reason}")
rows = [json.loads(x) for x in LEDGER_B.read_text().splitlines()]
appended = [e for e in rows if e["type"] == "planner-ended"]
check(len(appended) == 1, f"exactly one planner-ended, got {len(appended)}")
check(appended[0]["subject"] == CHARTER, f"subject is the charter: {appended[0]}")
check(appended[0]["handover"] == str(HANDOVER), f"handover path is cited: {appended[0]}")
# ★ the injected root is BOUND, not merely passed: fold.ROOT/fold.EVENTS are
# module globals bound at import, so a `root` that is not bound is decorative.
check(lines(A) == {}, f"root A (DOIT_ROOT) gained a line it must not have: {lines(A)}")
check(fold.EVENTS == B / "events" and fold.ROOT == B,
      f"the module BOUND the injected root onto fold, it did not merely pass it: {fold.EVENTS}")
check(pathlib.Path.home() / ".do-it" not in LEDGER_B.parents,
      f"nothing was written under the real ~/.do-it: {LEDGER_B}")
check("L-operator-local.jsonl" not in lines(B),
      f"it must not land in fold.append's default file: {lines(B)}")
check(len(kill.calls) == 1, f"kill fires exactly once, got {len(kill.calls)}")
import signal as _signal  # noqa: E402
check(kill.calls[0][0] == (4242, _signal.SIGTERM),
      f"SIGTERM to the pid find_ancestor returned, got {kill.calls[0][0]}")
# ★ content before event before signal: the line was ALREADY on disk when kill fired.
check("planner-ended" in kill.disk[0],
      "the append must be confirmed on disk BEFORE the signal, never after")

# ──────────────── AC7 · findable from the ledger alone, as actor `planner` ────────────────
fold.EVENTS = B / "events"
found = [e for e in fold.read_events()
         if e.get("type") == "planner-ended" and e.get("subject") == CHARTER]
check(len(found) == 1, f"exactly one planner-ended is derivable from the ledger: {len(found)}")
check(found[0]["handover"] == str(HANDOVER), f"its handover is the real path: {found[0]}")
check(found[0]["actor"] == "planner",
      f"D90: the actor is the FILENAME — production writes `planner`, got {found[0]['actor']}")

# ───────── AC4b · the green path through the REAL import named by the constants ─────────
# A wrong module name is indistinguishable from an unmerged sibling at runtime —
# every refusal above would look identical. This is the one check where the
# difference is visible, so the stub is imported by RELAY_MODULE/RELAY_PENDING and
# NO pending_packets argument is passed.
stub = TMP / "stub"
stub.mkdir(exist_ok=True)
(stub / f"{pane_end.RELAY_MODULE}.py").write_text(
    f"def {pane_end.RELAY_PENDING}(events, root):\n    return []\n")
sys.path.insert(0, str(stub))
ledger(B, **{"L-planner-0001.jsonl": l1()})
kill2, walk2 = Spy(), Spy(ret=777)
ended, reason = pane_end.check_and_end(CHARTER, HANDOVER, root=B,
                                       child_env={"DOIT_SUPERVISED": "1"},
                                       kill=kill2, find_ancestor=walk2)
check(ended is True, f"the real import of {pane_end.RELAY_MODULE} reaches the green path: {reason}")
check(len(kill2.calls) == 1 and kill2.calls[0][0] == (777, _signal.SIGTERM),
      f"one SIGTERM to the resolved pid, got {kill2.calls}")

# ───── a resolved pid is a precondition too: no pid, no event and no signal ─────
ledger(B, **{"L-planner-0001.jsonl": l1()})
r = refuses("the ancestor walk found no claude", B, pending_packets=CLEAR,
            find_ancestor=lambda: None)
check("claude" in r, f"reason must name what it could not find: {r}")

shutil.rmtree(TMP, ignore_errors=True)
print(f"pane_end: {N} checks pass")
