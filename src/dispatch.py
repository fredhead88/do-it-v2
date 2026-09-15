#!/usr/bin/env python3
"""dispatch — spawn one contract on the D116 line, check after the fact, append events.

  dispatch.py <role> <subject> [--packet FILE|-] [--path P] [--cwd DIR] [--charter ID]
              [--project NAME] [--mcp-config FILE] [--timeout MIN] [--max-usd N]

The BACKEND — claude-p, seat, or codex — and the MODEL are the ledger root's ruling,
`$DOIT_ROOT/models.toml` (models.py). `--seat` / DOIT_SEAT may only agree with it.
Every terminal event stamps `backend`, `model_requested` (the map) beside
`model_used` (what came back), `model_match`, and `first_on_model` — the D120
trust run of a (contract, model) pair is then a ledger fact, not a memory.
Also stamped: the four-way token split (`input_tokens`, `output_tokens`,
`cache_read`, `cache_creation`) and, on a seat spawn whose stamp.sh resolved
it, `subagent_tokens` — the route's OLDER blended figure, kept beside the
split rather than replaced by it (src/usage.py; retro step 9).

Every check here converts a silent failure into a loud one, and each was observed
before it was written (D116, D120): a tools: line without StructuredOutput comes
back is_error:false with structured_output:null; a denied Write came back as
`answered: yes` with no file on disk; a refused spawn is is_error:true with cost
above zero; an unreachable seat is api_error with zero tokens. The contract
appends nothing — every event below is derived from the Output object into this
spawn's own file, and the actor is that filename (D90).
"""
import argparse, atexit, hashlib, json, os, pathlib, re, subprocess, sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HERE.parent / "agents"
sys.path.insert(0, str(HERE))
import models  # noqa: E402
ROOT = pathlib.Path(os.environ.get("DOIT_ROOT", pathlib.Path.home() / ".do-it"))
EVENTS, CONTENT = ROOT / "events", ROOT / "content"

# (what must exist at --path afterwards, wall-clock minutes, dollar cap). Never
# uncapped (§4.4 correction 9) — a Budget the fold cannot compare is decoration.
ROLES = {"spec-writer": ("file", 30, 5), "spec-auditor": (None, 15, 3), "builder": (None, 90, 15),
         "grader": (None, 15, 3), "reviewer": (None, 30, 5), "plan-auditor": (None, 15, 3),
         "research": ("file", 5, 1), "reuse-scout": ("file", 15, 3),
         "charter-reviewer": (None, 30, 5), "probe": ("dir", 120, 10)}
# The builder's sandbox (§4.6·3). A pattern deny is the only per-command control
# this CLI has (D119 row U); "commit to main" is the branch layout's job, not a pattern's.
BUILDER_DENY = ["Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(*--no-verify*)",
                "Bash(git checkout main:*)", "Bash(git switch main:*)", "Bash(npm install:*)",
                "Bash(npm i :*)", "Bash(pnpm add:*)", "Bash(yarn add:*)", "Bash(pip install:*)",
                "Bash(pip3 install:*)", "Bash(brew :*)"]

sha = lambda s: hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()[:16]
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


def frontmatter(role):
    text = (AGENTS / f"{role}.md").read_text().split("---")[1]
    return {k.strip(): v.strip() for k, v in
            (l.split(":", 1) for l in text.strip().splitlines() if ":" in l)}


def subject_ids(prefix):
    """The ids of that kind the LEDGER owns. A spec id can exist as a subject with
    no content file — three did, from a charter whose specs were never written out —
    and §2.8's max+1 is over the ids that exist, not over the files that happen to.
    Allocating over files alone re-issues such an id, and the second `spec-written`
    lands on a subject the fold has already resolved to terminal: it never renders,
    the Executor never picks it up, and an append-only ledger cannot take it back."""
    out = []
    for f in EVENTS.glob("*.jsonl"):
        for line in f.read_text().splitlines():
            m = re.match(rf"{re.escape(prefix)}(\d+)$", (json.loads(line).get("subject") or "")
                         ) if line.strip().startswith("{") else None
            m and out.append(int(m.group(1)))
    return out


def alloc(d, prefix, suffix):
    """max+1 of a kind (§2.8), claimed with O_EXCL so two concurrent spawns never share a file."""
    n = max([int(p.stem.rsplit("-", 1)[1]) for p in d.glob(f"{prefix}[0-9]*{suffix}")]
            + subject_ids(prefix) or [0])
    while True:
        n += 1
        path = d / f"{prefix}{n:04d}{suffix}"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            pass


def emit(dst, base, type_, /, **kv):
    """Never trust the write — re-read (§9.2 rule 4). Never an actor field (D90)."""
    e = {**kv, **base, "v": 1, "ts": now(), "type": type_}
    e.pop("actor", None)
    line = json.dumps(e, sort_keys=True, default=str)
    with open(dst, "a") as fh:
        fh.write(line + "\n")
    assert line in dst.read_text().splitlines(), f"append to {dst} did not land"


# ★ `probe` runs with its cwd OUTSIDE every repo on purpose (§4.6·10, §9.5): its
# run directory is under the ledger root so there is no accidental path into the
# product. "Not a repository" is a definite answer, not an undetermined one, and
# reading it as undetermined refused the probe before it ever spent.
NOT_A_REPO = "<not a git repository>"


# Paths other processes write to while a spawn runs — a cron's health file, a
# session hook's handoff — declared by the operator, never inferred. The
# baseline's finding 2 (a $2.19 spawn voided by the driver's own handoff file)
# fired again on the Albert Scott pilot: a cron rewrote docs/sessions/process-
# health.md under a 15-minute audit, and the whole spawn was void. The check is
# still whole-tree by default; this only lets a root declare what it knows moves.
VOLATILE = [g for g in os.environ.get("DOIT_REPO_VOLATILE", "").split() if g]


def porcelain(cwd):
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=cwd, capture_output=True, text=True)
    if r.returncode == 0:
        if not VOLATILE:
            return r.stdout
        import fnmatch
        keep = [l for l in r.stdout.splitlines()
                if not any(fnmatch.fnmatch(l[3:].strip().strip('"'), g) for g in VOLATILE)]
        return "\n".join(keep) + ("\n" if keep else "")
    if "not a git repository" in r.stderr.lower():
        return NOT_A_REPO                               # definite: there is no repo to mutate
    return None                                         # None = undetermined, never clean


def run_claude(cmd, packet, cwd, timeout):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}   # no meter to fall through to
    return subprocess.run(cmd, input=packet, capture_output=True, text=True, cwd=cwd,
                          env=env, timeout=timeout)


# Roles whose contract writes a file: the Codex sandbox lets them write in the
# spawn's cwd and under content/; every judging role runs read-only. The
# builder's BUILDER_DENY patterns have no Codex spelling — the sandbox, the
# porcelain check and the merge gate are the enforcement on that backend.
CODEX_WRITES = ("builder", "spec-writer", "research", "reuse-scout", "probe", "reviewer", "charter-reviewer")


def run_codex_exec(cmd, packet, cwd, timeout):
    """The one subprocess the codex backend runs. Neither vendor's metered key is
    in the environment: the spawn draws the ChatGPT plan or it fails loudly."""
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    return subprocess.run(cmd, input=packet, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)


def run_codex(spawn, role, model, schema_path, packet, cwd, timeout):
    """The codex backend (pilot S1). `codex exec` reads the packet on stdin, enforces
    the contract's schema itself (`--output-schema`), writes the final object to
    `-o` (no `doit validate` loop, no transcription — pilot S18/S28), and streams
    events as JSONL, from which turns, tokens and the thread id are read. Codex
    does not echo the model it ran, so `model_used` is the flag it was given and
    the event says `model_observed: false`. Cost is unpriced on the plan (null)."""
    import time
    SEAT.mkdir(parents=True, exist_ok=True)
    (SEAT / f"{spawn}.packet.md").write_text(packet)
    out, log = SEAT / f"{spawn}.output.json", SEAT / f"{spawn}.codex.jsonl"
    sandbox = "workspace-write" if role in CODEX_WRITES else "read-only"
    cmd = ["codex", "exec", "-C", str(cwd), "-m", model, "--sandbox", sandbox, "--skip-git-repo-check",
           "--json", "--output-schema", str(schema_path), "-o", str(out), "--add-dir", str(CONTENT), "-"]
    (SEAT / f"{spawn}.cmd.json").write_text(json.dumps({"cmd": cmd, "cwd": str(cwd)}, indent=1))
    t0 = time.time()
    r = run_codex_exec(cmd, packet, str(cwd), timeout)
    log.write_text(r.stdout or "")
    thread, turns, usage, errors = None, 0, {}, []
    for line in (r.stdout or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "thread.started":
            thread = e.get("thread_id")
        elif t == "turn.completed":
            turns += 1
            for k, v in (e.get("usage") or {}).items():
                usage[k] = usage.get(k, 0) + (v or 0)
        elif t in ("error", "turn.failed"):
            errors.append(str(e.get("message") or e)[:200])
    try:
        structured = json.loads(out.read_text()) if out.is_file() else None
    except ValueError:
        structured = None
    bad = r.returncode != 0 or bool(errors)
    env = {"is_error": bad, "terminal_reason": "error" if bad else "completed", "structured_output": structured,
           "num_turns": turns or None, "duration_ms": int((time.time() - t0) * 1000),
           "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                     "cache_read_input_tokens": usage.get("cached_input_tokens"),
                     "cache_creation_input_tokens": usage.get("cache_write_input_tokens")},
           "total_cost_usd": None, "modelUsage": {model: {}}, "model_observed": False, "session_id": thread,
           "permission_denials": [], "result": "; ".join(errors) or (r.stderr or "")[-300:]}
    return SeatResult(json.dumps(env), r.returncode, r.stderr)


SEAT = ROOT / "seat"


class SeatResult:
    """The same three fields main() reads off a subprocess result."""
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def run_seat(spawn, cmd, packet, cwd, timeout):
    """The seat path (D116 by another route). Where `claude -p` is banned as metered
    — the Albert Scott rule, spec 572 — the spawn runs as an interactive session's
    seat-billed sub-agent instead. This function does not spawn: it writes the
    packet and the line that WOULD have run to `$R/seat/<spawn>.*`, prints where the
    result is expected, and waits for `<spawn>.result.json` — the same JSON envelope
    `claude -p --output-format json` prints (is_error, structured_output, usage,
    total_cost_usd, num_turns, session_id, modelUsage, permission_denials). Every
    after-the-fact check in main() then runs unchanged, and the ledger records
    `spawn_path: seat` so the two routes are distinguishable forever."""
    import time
    SEAT.mkdir(parents=True, exist_ok=True)
    (SEAT / f"{spawn}.packet.md").write_text(packet)
    (SEAT / f"{spawn}.cmd.json").write_text(json.dumps({"cmd": cmd, "cwd": cwd}, indent=1))
    want, bare = SEAT / f"{spawn}.result.json", SEAT / f"{spawn}.output.json"
    print(json.dumps({"seat": spawn, "packet": str(SEAT / f"{spawn}.packet.md"),
                      "result_expected_at": str(want), "or_output_at": str(bare),
                      "cwd": cwd, "timeout_s": timeout}), flush=True)
    # The completion signal is the PANE's stamp — `<spawn>.meta.json` (or a full
    # `result.json`) — never the Output file's existence: a contract iterating on its
    # Output with `doit validate` writes an invalid draft first, and the wrapper read
    # that draft on the first real spec-writer spawn and recorded it as failed while
    # the validated version landed a second later.
    meta_p = SEAT / f"{spawn}.meta.json"
    t0 = time.time()
    while not (want.is_file() or (meta_p.is_file() and bare.is_file())):
        if time.time() - t0 > timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        time.sleep(2)
    time.sleep(1)                       # a writer that is still writing
    if want.is_file():
        return SeatResult(want.read_text())
    # The bare Output the contract validated with `doit validate`, plus what the pane
    # recorded beside it (model, session, turns). The envelope is built here so no
    # hand ever writes one.
    m = json.loads(meta_p.read_text())
    # `model_observed` (usage.py's stamp) is the model the sub-agent's OWN
    # transcript actually ran on — real evidence, where `model` is only the pane
    # operator's typed claim. Prefer it as the modelUsage key so `model_used`
    # downstream reflects what was observed, not assumed, and stamp `model_observed`
    # true only when that evidence exists (a bare `model` with no transcript match
    # is exactly as unverified as codex's flag-only guess, and must not default true).
    observed = m.get("model_observed")
    model_for_usage = observed or m.get("model")
    env = {"is_error": False, "terminal_reason": "completed", "structured_output": json.loads(bare.read_text()),
           "num_turns": m.get("turns"), "duration_ms": m.get("duration_ms"), "usage": m.get("usage") or {},
           "total_cost_usd": None, "modelUsage": {model_for_usage: {}} if model_for_usage else {},
           "session_id": m.get("session"), "permission_denials": m.get("denied") or [],
           "model_observed": observed is not None}
    return SeatResult(json.dumps(env))


def poke():
    """D117: every wrapper ends by running one tick, so latency is a poke, not an interval.
    The ROOT's map rules first: a tick can only spawn a claude-p Executor, and a root
    whose Executor is a pane must never be poked into `claude -p` (pilot S32 — one
    wrapper without DOIT_NO_POKE in its shell cost $2.09 metered and three unserved
    30-minute spawns). The variable stays as the second guard, never the only one."""
    if os.environ.get("DOIT_NO_POKE"):
        return
    mp = models.load()
    if mp is not None and models.backend_of("executor", mp) != "claude-p":
        return
    subprocess.Popen([sys.executable, str(HERE / "tick.py")], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def file_adr(kind, title, body, base):
    """§2.7: every ADR carries the ids that caused it and a retirement condition."""
    p = alloc(CONTENT, "L-adr-", ".md")
    p.write_text(f"# {p.stem} · {kind}\n\n**Decision:** {title}\n\n{body}\n\n"
                 f"Caused by: {base['subject']} · {base['spawn']}\n"
                 f"Retire when: {base['subject']} is reverted or superseded.\n")
    return p.stem


def write_card(out, subject, spawn):
    """content/L-card-NNNN.md from the card object, fifteen lines or fewer (§5.10)."""
    i, v, acs, nb = out["identity"], out["verify"], out["acs"], out["not_built"]
    L = [f"# {subject.replace('-spec-', '-card-')} · {out['status']} · built by {spawn}",
         f"branch {i['branch']} · base {i['base_sha'][:7]} · ready {i['ready_sha'][:7]}",
         f"verify `{v['command']}` → exit {v['exit_code']} · {v['result']}"]
    room = 11 - min(len(nb), 4)
    L += [f"{a['id']} [{a['criterion_type']}] {a['disposition']} · {a['evidence_type']} · {a['check'][:80]}"
          for a in acs[:room - (len(acs) > room)]]
    if len(acs) > room:
        L.append(f"… {len(acs) - room + 1} more criteria")
    L += [f"not built: {n['item'][:90]} ({n['reason']})" for n in nb[:3 if len(nb) > 4 else 4]]
    if len(nb) > 4:
        L.append(f"… {len(nb) - 3} more not built")
    t = out["tests"]
    L.append(f"stubs {len(out['stubs'])} · deviations {len(out['deviations'])} · tests "
             f"{'added' if t['added'] else 'NONE: ' + t.get('none_reason', '')[:60]} · "
             f"unknowns {len(out['unknowns'])} · escalations {len(out['escalations'])}")
    assert len(L) <= 15, len(L)
    path = CONTENT / (subject.replace("-spec-", "-card-") + ".md")
    path.write_text("\n".join(L) + "\n")
    # The rendered card is the operator's fifteen lines (§5.10); the grader's packet
    # needs EVERY per-criterion row, and the first real chain lost rows 7+ to the cap
    # (baseline: "the card had rows for AC1–AC6 only"). The full card object is the
    # durable record — minus `deviations[].why`, which is the builder's reasoning and
    # the one thing the judge must never read.
    full = {k: out[k] for k in ("status", "identity", "acs", "verify", "stubs", "tests",
                                "not_built", "unknowns", "built_against")}
    full["deviations"] = [{"type": d["type"], "what": d["what"]} for d in out["deviations"]]
    path.with_suffix(".json").write_text(json.dumps(full, indent=1))
    return path


def coverage_changes(checkers):
    """A checker whose coverage note differs from the last recorded one marks its prior passes stale."""
    import fold
    last = {e.get("checker"): e.get("coverage_note") for e in fold.read_events()
            if e.get("type") == "checker-coverage-change"}
    return [("checker-coverage-change", dict(checker=c["id"], version=c["version"],
                                             coverage_note=c["coverage_note"]))
            for c in checkers if last.get(c["id"]) != c["coverage_note"]]


def events_for(role, out, a, base):
    """Output object → the events the contract names. Declarations become events typed by the term."""
    decl = [(d["term"], {k: v for k, v in d.items() if k != "term"}) for d in out.get("declarations", [])]
    qs = [("question", q) for q in out.get("escalations", [])]
    ev = []
    if role == "spec-writer":
        if out["status"] == "written":
            ev.append(("spec-written", dict(spec=out["spec_id"], path=a.path, ac_count=out["ac_count"],
                                            ac_types=out["ac_types"], requirement_ids=out["requirement_ids"],
                                            owed_ac_count=out["owed"], unknown_count=out["unknowns"],
                                            footprint=out["footprint"])))
        elif out["status"] == "killed":
            ev.append(("spec-killed", dict(check=out["killed_by_check"])))
    elif role == "spec-auditor":
        ev += [("audit-finding", dict(list="findings", **f)) for f in out["findings"]]
        ev += [("audit-finding", dict(list="rejected", **f)) for f in out["rejected"]]
    elif role == "plan-auditor":
        ev += [("audit-finding", dict(stage=out["stage"], **f)) for f in out["findings"]]
    elif role == "charter-reviewer":
        ev.append((f"charter-review-{out['verdict']}", dict(
            depth=out["walkthrough"]["depth"], uncovered_requirement_ids=out["uncovered_requirement_ids"])))
        ev += [("audit-finding", f) for f in out["findings"]]
    elif role == "grader":
        vs = out["verdicts"]
        # S15/S33 (a-6): `confirmed` here stays the LITERAL all-rows-met roll-up —
        # the model's own claim, computed before this spawn's dispatch could know
        # which criteria the spec-writer had declared owed. The fold (verdict_
        # confirmed) is where a `cannot-assess` row on an owed criterion stops
        # zeroing it; that needs the row-level data this event did not carry
        # before, so `cannot_assess` rides beside `confirmed` now.
        ev.append(("verdict", dict(confirmed=all(v["verdict"] == "met" for v in vs)
                                   and out["matches_intent"] == "yes" and out["card_ok"] == "yes",
                                   n=len(vs), matches_intent=out["matches_intent"], card_ok=out["card_ok"],
                                   cannot_assess=[v["ac"] for v in vs if v["verdict"] == "cannot-assess"])))
        ev += [("rejected-criterion", dict(criterion=v["ac"], why=v["reason"])) for v in vs if v["verdict"] == "unmet"]
        if out["could_not_run"] and not any(t == "gate-infra" for t, _ in decl):
            ev.append(("gate-infra", dict(line="could_not_run")))
        # A re-grade that finds a standing rejection met clears it — the grader may
        # clear (fold.EMITS), and without this a rework could never reach accepted.
        import fold
        specs, *_ = fold.fold(fold.read_events())
        standing = fold.standing_rejects(specs.get(a.subject, {"evs": []})["evs"])
        met = {v["ac"]: v["reason"] for v in vs if v["verdict"] == "met"}
        confirmed = ev[0][1]["confirmed"]
        # A confirmed verdict found everything met, the done-condition included — a
        # rejection the packet no longer names (round one's DONE-COND) cannot outlive
        # it, or the Executor reworks forever. Seen on the first real chain.
        ev += [("criterion-cleared", dict(criterion=c, evidence=met.get(c) or f"confirmed verdict {base['spawn']}"))
               for c in sorted(standing) if c in met or confirmed]
        ev += coverage_changes(out["checkers"])
    elif role == "reviewer":
        ev.append(("review", dict(depth=out["depth"], round=out["round"], n_blocking=len(out["blocking"]))))
        for b in out["blocking"]:
            ev += [("rejected-criterion", dict(criterion=b["ac"], why=b["finding"])),
                   ("must-fix", dict(criterion=b["ac"], reverify=b["reverify"]))]
        ev += [("criterion-cleared", dict(criterion=c["ac"], evidence=c["evidence"])) for c in out["cleared"]]
    elif role == "research":
        ev.append(("research-filed", dict(path=out["path"], answered=out["answered"])))
    elif role == "reuse-scout":
        ev.append(("reuse-scouted", dict(path=out["path"], n_candidates=len(out["candidates"]),
                                         nothing_cleared=out["nothing_cleared"])))
        for c in out["candidates"]:     # the rejections too — they are the corpus (§6.6)
            adr = file_adr("acquisition", f"{c['kind']} {c['name']} {c['installed_major']}",
                           json.dumps({k: c[k] for k in ("gates", "fit", "doc_url")}, indent=1)
                           + (f"\nPreviously rejected: {c['previously_rejected']}" if c.get("previously_rejected") else ""),
                           base)
            ev.append(("adr-filed", dict(adr=adr, kind="acquisition", candidate=c["name"])))
    elif role == "probe":
        ev.append(("probe-run", dict(path=out["path"], externals=[x["name"] for x in out["externals"]],
                                     n_inputs=out["n_inputs"], spend=out["spend_usd"], complete=out["complete"])))
    elif role == "builder":
        card, i = write_card(out, a.subject, base["spawn"]), out["identity"]
        ev.append(("build-done", dict(status=out["status"], card=str(card), branch=i["branch"],
                                      base_sha=i["base_sha"], ready_sha=i["ready_sha"],
                                      verify_exit=out["verify"]["exit_code"], tests_added=out["tests"]["added"])))
        for d in out["deviations"]:
            e = dict(deviation=d["type"], what=d["what"])   # `type` is the event's (found on the first real build)
            if d["type"] == "significant":      # owes an ADR; the wrapper files it from `why`
                e["adr"] = file_adr("architecture", d["what"], d["why"], base)
                ev.append(("adr-filed", dict(adr=e["adr"], kind="architecture")))
            ev.append(("build-deviation", e))
        ev += [("build-stub", s) for s in out["stubs"]]
        if out["status"] == "BLOCKED":
            ev.append(("build-blocked", dict(reason=next((q["asks"] for q in out["escalations"]), None)
                                             or next((d["line"] for d in out["declarations"]), "unstated"))))
    return ev + qs + decl


def main(a):
    kind, tmin, usd = ROLES[a.role]
    atexit.register(poke)
    fm, schema_path = frontmatter(a.role), AGENTS / f"{a.role}.schema.json"
    # An empty or non-file --packet is refused BEFORE a spawn id is allocated: a shell
    # that captured a failed `doit packet` into a variable hands "" here, which read `.`
    # as the packet and crashed after the fact (pilot "Smaller" list; hit again on charter 3).
    if a.packet != "-" and not (a.packet and pathlib.Path(a.packet).is_file()):
        sys.exit(f"dispatch: --packet {a.packet!r} is not a file — nothing allocated, nothing spent")
    packet = sys.stdin.read() if a.packet == "-" else pathlib.Path(a.packet).read_text()
    EVENTS.mkdir(parents=True, exist_ok=True), CONTENT.mkdir(parents=True, exist_ok=True)
    ledger = alloc(EVENTS, f"L-{a.role}-", ".jsonl")
    spawn, cwd, builder = ledger.stem, a.cwd or os.getcwd(), a.role == "builder"
    base = {"subject": a.subject, "project": a.project or pathlib.Path(cwd).name, "spawn": spawn}
    if a.charter:
        base["charter"] = a.charter
    meta = dict(model=fm.get("model"), packet_sha256=sha(packet), contract_sha256=sha(
        (AGENTS / f"{a.role}.md").read_bytes() + schema_path.read_bytes()), cli=CLI)
    mm = models.resolve(a.role, fm.get("model"))
    flag_seat = bool(getattr(a, "seat", False) or os.environ.get("DOIT_SEAT"))
    backend = mm["backend"] or ("seat" if flag_seat else "claude-p")
    meta.update(backend=backend, spawn_path=backend, model_requested=mm["model"], model_map=mm["source"])

    def fail(why):
        emit(ledger, base, "spawn-failed", why=why, **meta)
        print(f"FAILED {spawn}: {why}", file=sys.stderr)
        sys.exit(1)

    # D120: a refused spawn charged and is deterministic; an identical packet on an
    # identical contract is refused here, before it charges again. An api_error is
    # the one failure that IS retried — after the operator logs in.
    sys.path.insert(0, str(HERE))
    import fold
    prior = next((e for e in fold.read_events() if e.get("type") == "spawn-failed"
                  and e.get("packet_sha256") == meta["packet_sha256"]
                  and e.get("contract_sha256") == meta["contract_sha256"]
                  and str(e.get("why", "")).startswith(("is_error", "contamination"))), None)
    if prior:
        fail(f"identical packet and contract already failed as {prior.get('spawn')} "
             f"({str(prior.get('why'))[:60]}); not retried (D120) — not spent")
    # The map is decided once per root. A flag or variable that disagrees with it is
    # the per-shell ruling S32 measured the cost of; it is refused, not honoured.
    if mm["backend"] == "pane":
        fail(f"{a.role} is a pane role under {models.PATH} — it is opened, never dispatched — not spent")
    if mm["backend"] and flag_seat and mm["backend"] != "seat":
        fail(f"--seat/DOIT_SEAT contradicts {models.PATH} (backend={mm['backend']} for {a.role}); "
             f"the map is decided once per root — edit the file, not the flag — not spent")
    # §3.6: most of both fable audits is a script, and the script runs first. A
    # plan-auditor packet with no pre-pass block asks the model to re-derive six
    # mechanical checks — and "the script did not run" then reads exactly like
    # "the script found nothing". Refused before it spends.
    import audit
    if a.role == "plan-auditor" and audit.HEADER not in packet:
        fail(f"plan-auditor packet carries no script pre-pass ('{audit.HEADER}') — "
             f"run `doit audit <stage> {a.subject} --cut …` and put its block in the packet "
             f"(§3.6) — not spent")
    before = None if builder else porcelain(cwd)
    if not builder and before is None:
        fail(f"repo status undetermined in {cwd} before spawn — not spent")
    # A start event for every role: the tick reads a start with no terminal event as
    # in flight and keeps the subject off the lane (found on the first real chain).
    if builder:
        emit(ledger, base, "build-started", worktree=cwd)
    else:
        emit(ledger, base, "spawn-started", role=a.role)
    packet += f"\n\nspawn_id: {spawn}\n"
    tools = [t.strip() for t in fm["tools"].split(",") if t.strip() != "StructuredOutput"]
    mcp = json.load(open(a.mcp_config)) if a.mcp_config else {}
    tools += [f"mcp__{s}" for s in mcp.get("mcpServers", {})]
    cmd = ["claude", "-p", "--agent", a.role, "--model", mm["model"] or fm.get("model", ""),
           "--strict-mcp-config", "--permission-mode", "dontAsk",
           "--disable-slash-commands", "--output-format", "json", "--json-schema", schema_path.read_text(),
           "--max-budget-usd", str(a.max_usd or usd), "--allowedTools", ",".join(tools),
           "--add-dir", str(CONTENT)]
    if builder:
        cmd += ["--disallowedTools", ",".join(BUILDER_DENY)]
    if a.mcp_config:
        cmd += ["--mcp-config", a.mcp_config]
    seat = backend == "seat"
    try:
        if backend == "codex":
            r = run_codex(spawn, a.role, mm["model"], schema_path, packet, cwd, (a.timeout or tmin) * 60)
        elif seat:
            r = run_seat(spawn, cmd, packet, cwd, (a.timeout or tmin) * 60)
        else:
            r = run_claude(cmd, packet, cwd, (a.timeout or tmin) * 60)
    except subprocess.TimeoutExpired:
        fail(f"timeout after {a.timeout or tmin} min")
    try:
        res = json.loads(r.stdout)
    except ValueError:
        fail(f"exit {r.returncode}, no JSON on stdout: {r.stderr[-300:]!r}")
    # A codex refusal is the backend's, not the packet's (a weekly limit, a sandbox
    # denial, a dead CLI): with a `fallback` on the map the spawn runs ONCE more on
    # that backend, and the event says so. Never a third route, never silently.
    if backend == "codex" and res.get("is_error") and mm.get("fallback"):
        fb = mm["fallback"]
        emit(ledger, base, "backend-fallback", from_backend="codex", to_backend=fb["backend"],
             to_model=fb["model"], why=str(res.get("result"))[:200])
        backend, seat = fb["backend"], fb["backend"] == "seat"
        mm = {**mm, "model": fb["model"]}
        cmd[cmd.index("--model") + 1] = fb["model"]
        meta.update(backend=backend, spawn_path=backend, model_requested=fb["model"],
                    backend_fallback=True, fallback_from="codex")
        try:
            r = run_seat(spawn, cmd, packet, cwd, (a.timeout or tmin) * 60) if seat \
                else run_claude(cmd, packet, cwd, (a.timeout or tmin) * 60)
        except subprocess.TimeoutExpired:
            fail(f"timeout after {a.timeout or tmin} min (on the fallback backend {backend})")
        try:
            res = json.loads(r.stdout)
        except ValueError:
            fail(f"fallback {backend}: exit {r.returncode}, no JSON on stdout: {r.stderr[-300:]!r}")
    u = res.get("usage") or {}
    # What came back is the record; the contract's line is never written in its place
    # (pilot S5: the ledger said Fable for a day of Opus). Unobserved is null.
    used = next(iter(res.get("modelUsage") or {}), None)
    meta.update(model_used=used, model_observed=bool(used) and res.get("model_observed", True),
                model_match=(used == mm["model"]) if used and mm["model"] else None,
                first_on_model=models.first_on_model(fold.read_events(), meta["contract_sha256"], used) if used else None)
    meta.update(model=used, cost_usd=res.get("total_cost_usd"),
                input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                cache_read=u.get("cache_read_input_tokens"), cache_creation=u.get("cache_creation_input_tokens"),
                # The seat route's blended figure (stamp.sh's <subagent_tokens> arg,
                # pre-dating the four-way split): a single Task-tool token count that
                # is NOT the sum of the four fields above (caching makes them measure
                # different things — see src/usage.py). Kept on the event, alongside
                # the split, never in place of it, so a pre-split spawn's only number
                # is not lost and the SPEND block can render it honestly as unsplit.
                subagent_tokens=u.get("subagent_tokens"),
                turns=res.get("num_turns"), duration_ms=res.get("duration_ms"), session=res.get("session_id"),
                denied=[d.get("tool_name") for d in (res.get("permission_denials") or [])][:10])
    if res.get("is_error"):
        if res.get("terminal_reason") == "api_error" or res.get("api_error_status"):
            emit(ledger, base, "escalation-blocking",
                 why=f"seat unreachable ({res.get('api_error_status')}) — /login as the operator; {spawn} is not retried")
            fail(f"api_error {res.get('api_error_status')}: {str(res.get('result'))[:300]}")
        fail(f"is_error: {str(res.get('result'))[:300]}")
    out = res.get("structured_output")
    if out is None:
        fail("null structured_output — tools: line lacks StructuredOutput, or the model never called it")
    # The CLI enforces --json-schema on its own route; the seat route has no CLI, so
    # the schema is checked here for both — a 440-character finding against a
    # 400 cap came back from the first seat spawn and nothing refused it.
    try:
        import jsonschema
        jsonschema.validate(out, json.loads(schema_path.read_text()))
    except ImportError:
        if seat:
            fail("jsonschema is not importable and this is a seat spawn — the Output is unchecked, "
                 "and unchecked is never clean (pip install jsonschema)")
    except jsonschema.ValidationError as e:
        fail(f"structured_output violates {schema_path.name}: {e.message[:200]} at "
             f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}")
    if out.get("contamination"):
        fail("contamination: the packet carried what Blindness strips; the run is void")
    if kind and not (a.role == "spec-writer" and out["status"] != "written"):
        p = pathlib.Path(a.path) if a.path else fail("a writing role needs --path")
        # A run directory is named with a trailing slash by its own contract, and
        # `content/L-probe-0001/`.endswith("L-probe-0001") is false.
        if out.get("path") and not out["path"].rstrip("/").endswith(p.name):
            fail(f"path mismatch: packet named {p.name}, output says {out['path']}")
        exists = p.is_dir() and any(p.iterdir()) if kind == "dir" else p.is_file() and p.stat().st_size > 0
        if not exists:
            fail(f"nothing at {p} — the model reported {out.get('status') or out.get('answered')} "
                 "with no file on disk (D120 W3)")
    if not builder and porcelain(cwd) != before:
        fail(f"repo status changed across a non-builder spawn: {porcelain(cwd)!r}")
    for t, kv in events_for(a.role, out, a, base):
        emit(ledger, base, t, **kv)
    scalars = {k: v for k, v in out.items() if isinstance(v, (str, int, float, bool))}
    emit(ledger, base, "spawn-done", **{**scalars, **meta})
    print(json.dumps({"spawn": spawn, "ok": True, **meta}))


def cli_version():
    """Recorded on every terminal event (D120: re-trust on a CLI change). A version
    that cannot be read is recorded as null, never a crash — the CLI replaces its own
    binary on auto-update, and one exec during the swap took the whole suite down."""
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.split()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


CLI = cli_version()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("role", choices=sorted(ROLES))
    ap.add_argument("subject", help="L-spec-NNNN / L-charter-NNNN — the events' subject")
    ap.add_argument("--packet", default="-", help="the packet file; '-' reads stdin (default)")
    ap.add_argument("--path", help="the content path the packet named; must exist afterwards")
    ap.add_argument("--cwd", help="the spawn's cwd: the repo, or the builder's worktree")
    ap.add_argument("--charter"), ap.add_argument("--project")
    ap.add_argument("--mcp-config", help="the browser, for the two reviewer roles")
    ap.add_argument("--timeout", type=int, help="minutes; overrides the role default")
    ap.add_argument("--max-usd", type=float, help="overrides the role default")
    ap.add_argument("--detach", action="store_true", help="fork and return at once; needs --packet FILE")
    ap.add_argument("--seat", action="store_true",
                    help="do not run `claude -p`; write the packet under $R/seat/ and wait for "
                         "<spawn>.result.json from a seat-billed sub-agent (or DOIT_SEAT=1)")
    a = ap.parse_args()
    if a.detach:        # D117: sub-agents are detached subprocesses; the wrapper's terminal event and poke follow
        if a.packet == "-":
            sys.exit("--detach needs --packet FILE")
        (ROOT / "logs").mkdir(parents=True, exist_ok=True)
        log = open(ROOT / "logs" / f"dispatch-{a.role}-{now().replace(':', '')}.log", "ab")
        p = subprocess.Popen([sys.executable, __file__] + [x for x in sys.argv[1:] if x != "--detach"],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        print(json.dumps({"detached": p.pid, "role": a.role, "subject": a.subject, "log": log.name}))
        sys.exit(0)
    main(a)
