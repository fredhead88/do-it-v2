#!/usr/bin/env python3
"""audit — the deterministic pre-pass under both fable audits (§3.6).

  audit.py cut  <charter> --cut FILE [--charter FILE] [--repo DIR]
  audit.py plan <charter> --cut FILE --plan FILE [--charter FILE] [--repo DIR]

§3.6: *"Most of both audits is a script, and the script runs first."* Six checks,
every one of them mechanical, handed to the plan-auditor as ground truth it does
not re-derive — so its one pass is spent on the residue, which is semantic
coverage and nothing else.

**A check answers one of three ways, and `undetermined` is not one of the other
two.** No finding · a list of findings · `undetermined — why`. No `--repo` makes
the size heuristic undetermined, not clean; a unit with no `Wave:` makes the
overlap check undetermined, not clean. The wrapper refuses a plan-auditor packet
that carries no block from here, so "the script did not run" cannot look like
"the script found nothing".

The requirement-id diff is one function used twice (§3.6): units against their
charter here, the charter set against the goal in `think.land` — the same
`coverage()`, both directions, never a second implementation.
"""
import argparse, itertools, os, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fold  # noqa: E402

HEADER = "## Script pre-pass (ground truth — do not re-derive)"

# §4.3: hand back at ~35–40% of a 1M window. A unit whose footprint alone reaches
# that has no room to spare, and §3.6 calls this the cheapest bad-cut detector.
CEILING = int(os.environ.get("DOIT_UNIT_TOKENS", 350_000))
FLOOR = 8_000                                    # the spawn floor (D116)

HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
SIG = ("consumes", "produces")
LABEL = re.compile(r"^\s*[-*]?\s*\**(Goal|Delivers|Footprint|Consumes|Produces|Wave)\**\s*:\s*(.*)$", re.I)
# A stable requirement id, and the listed form of one. Both live here because the
# coverage diff is here; `think` imports them rather than keeping a second pair.
ID = re.compile(r"\b([A-Z][A-Z0-9]{0,7}-?\d+)\b")
LISTED = re.compile(r"^\s*[-*]?\s*\**([A-Z][A-Z0-9]{0,7}-?\d+)\**\s*[:.]", re.M)


def die(msg):
    sys.exit(f"audit: {msg}")


def section(text, name):
    """One section's body: from its heading to the next heading of any depth."""
    m = re.search(rf"^#+\s*[0-9.]*\s*{re.escape(name)}\b.*$", text, re.I | re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^#+\s", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def coverage(want, cited, out=()):
    """The requirement-id diff, and it runs BOTH directions (D98, §3.6): a
    requirement nothing cites is under-delivery; a citation of what was never asked
    for is scope creep — and citing an `Out of scope` id is the drift §7.9 says this
    same diff catches."""
    want, cited, out = set(want), set(cited), set(out)
    return {"undelivered": sorted(want - cited), "creep": sorted(cited - want - out),
            "out_of_scope": sorted(cited & out)}


def goal_coverage(goal_text, charters):
    """The same diff, one level up: the charter set against the goal (D98). This is
    the second argument §3.6 names, not a second script."""
    return coverage(LISTED.findall(section(goal_text, "Requirements") or ""),
                    {i for c in charters for i in c["covers"]},
                    LISTED.findall(section(goal_text, "Out of scope") or ""))


# ── the cut file, parsed ─────────────────────────────────────────────────────

def split(v, sig=False):
    """Footprints and ids are comma- or space-separated. **A signature is never split
    on whitespace** — `TxStatus enum` is one name and two words, and splitting it
    reported two undefined seams where there was one defined one."""
    v = v.strip()
    parts = ([v] if "(" in v else v.split(",")) if sig else re.split(r"[,\s]+", v)
    return [p.strip().strip("`*,") for p in parts if p.strip().strip("`*,")]


def fields(body):
    """The labelled lines of one unit block, each label taking its own value and any
    list that follows it."""
    f, cur = {}, None
    for line in body.splitlines():
        m = LABEL.match(line)
        if m:
            cur = m.group(1).lower()
            f[cur] = f.get(cur, []) + split(m.group(2), cur in SIG)
        elif cur and line.strip().startswith(("-", "*")):
            f[cur] += split(line.strip().lstrip("-* "), cur in SIG)
        elif line.strip():
            cur = None
    return f


def units(cut_text):
    """One block per unit (planner.md ①). A heading section is a unit when it declares
    a `Footprint:` — a preamble or a notes section is not a unit, and neither is a
    cut file with no units at all."""
    out, heads = [], list(HEAD.finditer(cut_text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(cut_text)
        f = fields(cut_text[m.end():end])
        if "footprint" not in f:
            continue
        wave = next((int(w) for w in f.get("wave", []) if w.isdigit()), None)
        out.append({"name": m.group(2).strip(), "wave": wave,
                    **{k: f.get(k, []) for k in ("goal", "delivers", "footprint", "consumes", "produces")}})
    return out


def sig_name(s):
    """A seam's name is what two units must agree on; the rest of the signature is
    what only the auditor can compare."""
    m = re.match(r"[\s`*]*([A-Za-z_][\w.\-/]*)", s)
    return m.group(1) if m else s.strip()


# ── the six checks ───────────────────────────────────────────────────────────

def overlap(us):
    """Within a wave, zero footprint overlap; across waves, unlimited (§3.7). So the
    wave is what makes this check mean anything, and a unit with no `Wave:` cannot be
    placed in one."""
    blind = [u["name"] for u in us if u["wave"] is None]
    if blind:
        return [], f"no `Wave:` on {', '.join(blind)} — the overlap rule is per wave (§3.7)"
    found = []
    for a, b in itertools.combinations(us, 2):
        both = sorted(set(a["footprint"]) & set(b["footprint"]))
        if a["wave"] == b["wave"] and both:
            found.append(f"wave {a['wave']}: {a['name']} ∩ {b['name']} = {' '.join(both)}")
    return found, None


def producers(us):
    p = {}
    for u in us:
        for s in u["produces"]:
            p.setdefault(sig_name(s), []).append(u)
    return p


def seams(us):
    """Every `Consumes:` has a matching `Produces:` (§3.6) — and the producer lands
    first, because a seam produced in a later wave is defined and still unbuildable."""
    prod, found = producers(us), []
    for u in us:
        for c in u["consumes"]:
            n = sig_name(c)
            ps = [p for p in prod.get(n, []) if p is not u]
            if not ps:
                found.append(f"{u['name']} consumes `{n}` — no unit produces it")
            elif u["wave"] is not None and all(p["wave"] is not None and p["wave"] > u["wave"] for p in ps):
                found.append(f"{u['name']} (wave {u['wave']}) consumes `{n}`, produced only in wave "
                             f"{min(p['wave'] for p in ps)}")
    return found, None


def shared(us, plan_text):
    """One name introduced by two units with no owner (§3.6). **An extract has exactly
    one producer** (§3.7): the contested thing becomes its own unit that runs first and
    every other unit *consumes* it. So two `Produces:` of one name is unowned however
    the waves fall — the first real plan-auditor run rejected the earlier
    earliest-wave-wins rule on that ground, and it was right: a later unit re-producing
    a wave-1 name re-touches what wave 1 just landed. At stage `plan` the Shared
    decisions section is the one place that can name an owner instead."""
    owned = set()
    if plan_text is not None:
        sec = section(plan_text, "Shared decisions") or ""
        owned = {sig_name(w) for w in re.findall(r"`([^`]+)`", sec)} | set(re.split(r"[\s,]+", sec))
    found = []
    for name, ps in sorted(producers(us).items()):
        if len(ps) < 2 or name in owned:
            continue
        found.append(f"`{name}` introduced by {', '.join(p['name'] for p in ps)} — no owner"
                     + (f" (waves {', '.join(str(p['wave']) for p in ps)}: a later unit re-touches "
                        f"what the earlier one landed)" if len({p["wave"] for p in ps}) > 1 else ""))
    return found, None


def sizes(us, repo):
    """The size heuristic against §4.3's ceiling: the footprint a builder must read,
    at ~4 bytes a token, plus the spawn floor. A footprint that resolves to nothing on
    disk is not a small unit — it is an unmeasured one."""
    if not repo:
        return [], "no --repo — the footprint cannot be measured, and an unmeasured unit is not a small one"
    root, found, blind = pathlib.Path(repo), [], []
    for u in us:
        b, unresolved = 0, []
        for f in u["footprint"]:
            hits = [h for h in (root.glob(f) if any(c in f for c in "*?[") else [root / f]) if h.is_file()]
            b += sum(h.stat().st_size for h in hits)
            if not hits:
                unresolved.append(f)
        est = FLOOR + b // 4
        if len(unresolved) == len(u["footprint"]):
            blind.append(f"{u['name']} (nothing on disk at {' '.join(unresolved)})")
        elif est > CEILING:
            found.append(f"{u['name']} ~{est // 1000}k tokens of footprint against the "
                         f"{CEILING // 1000}k ceiling (§4.3) — cut smaller")
    return found, (f"unmeasurable: {'; '.join(blind)}" if blind else None)


def dep_name(line):
    s = line.strip().lstrip("-*| ").strip()
    m = re.match(r"`([^`]+)`", s)
    return (m.group(1) if m else (s.split() or [""])[0]).strip("*_`,")


def acquisition(plan_text, events=None):
    """An acquisition decision for every declared dependency — the ADR-trail diff
    (§3.6, §6). The trail is `adr-filed{kind: acquisition}`; §2.7 gives it that field
    precisely so this query is cheap."""
    if plan_text is None:
        return [], "no Plan at this stage — Acquisition decisions are a Plan section (§3.5)"
    sec = section(plan_text, "Acquisition")
    if sec is None:
        return ["the Plan has no Acquisition decisions section — a missing section is a "
                "missing decision (§3.5)"], None
    deps = [d for d in (dep_name(l) for l in sec.splitlines() if l.strip().startswith(("-", "*")))
            if d and d.lower() not in ("none", "n/a", "nothing")]
    filed = {e.get("candidate") for e in (fold.read_events() if events is None else events)
             if e.get("type") == "adr-filed" and e.get("kind") == "acquisition"}
    return [f"`{d}` is declared in the Plan with no acquisition ADR on the trail" for d in deps
            if d not in filed], None


def units_vs_charter(us, charter_text):
    """The requirement-id diff at the cut's own level: a charter requirement no unit
    delivers, and a unit citing an id the charter never stated."""
    if charter_text is None:
        return [], "no --charter — the requirement-id diff has nothing to diff against"
    want = LISTED.findall(section(charter_text, "Requirements") or "")
    if not want:
        return [], "the charter's Requirements section carries no stable ids"
    d = coverage(want, {i for u in us for i in u["delivers"]})
    return ([f"no unit delivers {i}" for i in d["undelivered"]]
            + [f"a unit cites {i}, which the charter does not state" for i in d["creep"]], None)


# ── the block ────────────────────────────────────────────────────────────────

def render(stage, rows):
    """rows: (label, findings, undetermined). The plan-auditor reads this as ground
    truth, so an undetermined check must not be able to read as a clean one."""
    out = [HEADER, f"stage: {stage}"]
    for label, found, und in rows:
        if und:
            out.append(f"- {label}: undetermined — {und}"
                       + (f"; found anyway: {'; '.join(found)}" if found else ""))
        elif found:
            out += [f"- {label}:"] + [f"  - {f}" for f in found]
        else:
            out.append(f"- {label}: none")
    return "\n".join(out) + "\n"


def coverage_rows(diff, of="charter"):
    """The goal-level diff as check rows, so `think --land`'s packet carries the same
    block under the same header the wrapper looks for."""
    return [(f"goal requirements no {of} cites", diff["undelivered"], None),
            (f"{of} ids the goal does not ask for", diff["creep"], None),
            (f"{of} ids on the goal's Out of scope list", diff["out_of_scope"], None)]


def read(p):
    if p is None:
        return None
    q = pathlib.Path(p)
    if not q.is_file():
        die(f"nothing on disk at {q}")
    return q.read_text()


def prepass(stage, cut_text, charter_text=None, plan_text=None, repo=None, events=None):
    us = units(cut_text)
    if not us:
        die("the cut file declares no unit — a unit block carries a `Footprint:` line "
            "(planner.md ①). Undetermined is never clean")
    rows = [("same-wave footprint overlap", *overlap(us)),
            ("undefined seams (a Consumes with no Produces)", *seams(us)),
            ("shared names introduced twice with no owner", *shared(us, plan_text)),
            ("requirement coverage, units vs charter", *units_vs_charter(us, charter_text)),
            ("unit size vs the §4.3 ceiling", *sizes(us, repo)),
            ("acquisition ADR trail", *acquisition(plan_text, events))]
    return f"units: {len(us)} · waves: {sorted({u['wave'] for u in us if u['wave'] is not None})}\n" \
           + render(stage, rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="doit audit", description=__doc__.split("\n\n")[0])
    ap.add_argument("stage", choices=["cut", "plan"])
    ap.add_argument("subject", help="the charter id — what the audit's events are about")
    ap.add_argument("--cut", required=True, help="the cut file (§3.6 ①, D94)")
    ap.add_argument("--plan", help="the Plan; required at stage plan")
    ap.add_argument("--charter", help="the charter file, for the requirement-id diff")
    ap.add_argument("--repo", help="the repository, for the size heuristic")
    ap.add_argument("--out", help="write the block here as well as to stdout")
    a = ap.parse_args(argv)
    if a.stage == "plan" and not a.plan:
        die("stage plan needs --plan: the plan-audit reads the cut AND the Plan (§3.6 ④)")
    block = prepass(a.stage, read(a.cut), read(a.charter), read(a.plan), a.repo)
    if a.out:
        pathlib.Path(a.out).write_text(block)
    print(block, end="")
    return block


if __name__ == "__main__":
    main()
