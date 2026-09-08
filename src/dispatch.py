#!/usr/bin/env python3
"""dispatch — spawn one contract on the D116 line, check after the fact, append events.

  dispatch.py <role> <subject> [--packet FILE|-] [--path P] [--cwd DIR] [--charter ID]
              [--project NAME] [--mcp-config FILE] [--timeout MIN] [--max-usd N]

Every check here converts a silent failure into a loud one, and each was observed
before it was written (D116, D120): a tools: line without StructuredOutput comes
back is_error:false with structured_output:null; a denied Write came back as
`answered: yes` with no file on disk; a refused spawn is is_error:true with cost
above zero; an unreachable seat is api_error with zero tokens. The contract
appends nothing — every event below is derived from the Output object into this
spawn's own file, and the actor is that filename (D90).
"""
import argparse, atexit, hashlib, json, os, pathlib, subprocess, sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
AGENTS = HERE.parent / "agents"
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


def alloc(d, prefix, suffix):
    """max+1 of a kind (§2.8), claimed with O_EXCL so two concurrent spawns never share a file."""
    n = max([int(p.stem.rsplit("-", 1)[1]) for p in d.glob(f"{prefix}[0-9]*{suffix}")] or [0])
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


def porcelain(cwd):
    r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None      # None = undetermined, never clean


def run_claude(cmd, packet, cwd, timeout):
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}   # no meter to fall through to
    return subprocess.run(cmd, input=packet, capture_output=True, text=True, cwd=cwd,
                          env=env, timeout=timeout)


def poke():
    """D117: every wrapper ends by running one tick, so latency is a poke, not an interval."""
    if not os.environ.get("DOIT_NO_POKE"):
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
        ev.append(("verdict", dict(confirmed=all(v["verdict"] == "met" for v in vs)
                                   and out["matches_intent"] == "yes" and out["card_ok"] == "yes",
                                   n=len(vs), matches_intent=out["matches_intent"], card_ok=out["card_ok"])))
        ev += [("rejected-criterion", dict(criterion=v["ac"], why=v["reason"])) for v in vs if v["verdict"] == "unmet"]
        if out["could_not_run"] and not any(t == "gate-infra" for t, _ in decl):
            ev.append(("gate-infra", dict(line="could_not_run")))
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
    packet = sys.stdin.read() if a.packet == "-" else pathlib.Path(a.packet).read_text()
    EVENTS.mkdir(parents=True, exist_ok=True), CONTENT.mkdir(parents=True, exist_ok=True)
    ledger = alloc(EVENTS, f"L-{a.role}-", ".jsonl")
    spawn, cwd, builder = ledger.stem, a.cwd or os.getcwd(), a.role == "builder"
    base = {"subject": a.subject, "project": a.project or pathlib.Path(cwd).name, "spawn": spawn}
    if a.charter:
        base["charter"] = a.charter
    meta = dict(model=fm.get("model"), packet_sha256=sha(packet), contract_sha256=sha(
        (AGENTS / f"{a.role}.md").read_bytes() + schema_path.read_bytes()), cli=CLI)

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
    before = None if builder else porcelain(cwd)
    if not builder and before is None:
        fail(f"repo status undetermined in {cwd} before spawn — not spent")
    if builder:
        emit(ledger, base, "build-started", worktree=cwd)
    packet += f"\n\nspawn_id: {spawn}\n"
    tools = [t.strip() for t in fm["tools"].split(",") if t.strip() != "StructuredOutput"]
    mcp = json.load(open(a.mcp_config)) if a.mcp_config else {}
    tools += [f"mcp__{s}" for s in mcp.get("mcpServers", {})]
    cmd = ["claude", "-p", "--agent", a.role, "--strict-mcp-config", "--permission-mode", "dontAsk",
           "--disable-slash-commands", "--output-format", "json", "--json-schema", schema_path.read_text(),
           "--max-budget-usd", str(a.max_usd or usd), "--allowedTools", ",".join(tools),
           "--add-dir", str(CONTENT)]
    if builder:
        cmd += ["--disallowedTools", ",".join(BUILDER_DENY)]
    if a.mcp_config:
        cmd += ["--mcp-config", a.mcp_config]
    try:
        r = run_claude(cmd, packet, cwd, (a.timeout or tmin) * 60)
    except subprocess.TimeoutExpired:
        fail(f"timeout after {a.timeout or tmin} min")
    try:
        res = json.loads(r.stdout)
    except ValueError:
        fail(f"exit {r.returncode}, no JSON on stdout: {r.stderr[-300:]!r}")
    u = res.get("usage") or {}
    meta.update(model=next(iter(res.get("modelUsage") or {}), fm.get("model")), cost_usd=res.get("total_cost_usd"),
                input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                cache_read=u.get("cache_read_input_tokens"), cache_creation=u.get("cache_creation_input_tokens"),
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
    if out.get("contamination"):
        fail("contamination: the packet carried what Blindness strips; the run is void")
    if kind and not (a.role == "spec-writer" and out["status"] != "written"):
        p = pathlib.Path(a.path) if a.path else fail("a writing role needs --path")
        if out.get("path") and not out["path"].endswith(p.name):
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


CLI = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.split()[0:1] or [None]
CLI = CLI[0]

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
    main(ap.parse_args())
