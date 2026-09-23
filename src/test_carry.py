#!/usr/bin/env python3
"""One runnable check on `doit carry`. Run: env -u DOIT_PROJECT python3 test_carry.py

Everything is a fixture. `DOIT_ROOT`, the v4 ledger, the inbox and the staging tree
all point under one temporary directory — the operator's real `~/.claude/ledger`,
`~/.claude/spec-inbox` and `~/.claude/spec-staging` are READ-ONLY from this seat,
and a test that wrote fixtures there would violate that even if it cleaned up.

`doit dispatch` and `doit append` are a recorded-argv stub, never a real spawn: a
live $5 spec-writer inside a test run is itself a failure, so the stub counts its
own calls and `dispatch.alloc` is counted separately to prove every refusal fires
before an allocation.

`DOIT_PROJECT` is PINNED here rather than inherited (A10), and deliberately set to a
label that matches neither the fixture `spec-carried` event's project nor the
`spec-written` one's: that is the only arrangement in which the idempotency guard's
neutralised read is distinguishable from `fold.read_events()`'s filtered one.
"""
import contextlib, io, json, os, pathlib, sys, tempfile, types

TMP = pathlib.Path(tempfile.mkdtemp(prefix="carry-test-"))
os.environ["DOIT_ROOT"] = str(TMP / "root")
os.environ["V4_LEDGER_DIR"] = str(TMP / "ledger")
os.environ["V4_INBOX_DIR"] = str(TMP / "inbox")
os.environ["V4_STAGING_DIR"] = str(TMP / "staging")
os.environ["DOIT_PROJECT"] = "pinned-by-the-suite"
# `ledger_actor()`'s literal "operator" default must not depend on whatever the
# invoking seat/builder environment happens to export (this spec's Assumptions).
os.environ.pop("DOIT_LEDGER_FILE", None)
for d in ("root/content", "root/events", "ledger", "inbox", "staging", "repo/.git"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
REPO = TMP / "repo"
HEAD = "4d1c0ffee4d1c0ffee4d1c0ffee4d1c0ffee1234"
(REPO / ".git" / "HEAD").write_text(HEAD + "\n")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold  # noqa: E402

N = 0
TICK = [0]


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def stamp():
    TICK[0] += 1
    return f"2026-09-17T10:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


def write_event(actor, e):
    p = pathlib.Path(os.environ["DOIT_ROOT"]) / "events" / f"{actor}.jsonl"
    with open(p, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": stamp(), **e}) + "\n")


def record(num, stem, intent="carry me", spec_file=None, at="'2026-09-15T12:52:02Z'",
           status="registered"):
    """A fixture v4 record in 1454's shape. `spec_file` defaults to a staging path
    that does NOT exist — the measured state of all fifteen real records."""
    sf = spec_file if spec_file is not None else str(TMP / "staging" / f"{stem}-spec.md")
    body = (f"spec_id: {num}-{stem}\ntitle: fixture {num}\nintent: '{intent}'\n"
            f"status: {status}\nhanded_over_at: {at}\nspec_file: {sf}\nsource_brief: null\n")
    p = TMP / "ledger" / f"{num}-{stem}.yml"
    p.write_text(body)
    return p


def inbox(num, stem, text):
    p = TMP / "inbox" / f"{num}-{stem}-spec.md"
    p.write_text(text)
    return p


# ── the stub: every subprocess `carry` makes, recorded and never run ───────────
CALLS = []


class Stub:
    """Stands in for `carry.subprocess`. It records argv and returns a canned result;
    it never runs anything, so no dispatch in this file can spend."""
    def __init__(self):
        self.dispatch = lambda argv: (0, "", "")
        self.append = self.record_append

    def record_append(self, argv):
        kv = dict(x.split("=", 1) for x in argv[4:])
        # what `doit append` would write — including a `project` label that differs
        # from DOIT_PROJECT, which is the whole point of AC3.
        write_event("L-operator-stub", {"type": argv[2], "subject": argv[3],
                                        "project": "a-completely-different-project", **kv})
        return 0, "", ""

    def run(self, cmd, capture_output=False, text=False, **kw):
        CALLS.append(list(cmd))
        rc, out, err = (self.dispatch if cmd[1] == "dispatch" else self.append)(list(cmd))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


STUB = Stub()
carry.subprocess = STUB

ALLOCS = [0]
_real_alloc = dispatch.alloc


def counting_alloc(d, prefix, suffix):
    ALLOCS[0] += 1
    return _real_alloc(d, prefix, suffix)


dispatch.alloc = counting_alloc

NET = [0]
import urllib.request  # noqa: E402


def no_network(*a, **k):
    NET[0] += 1
    raise AssertionError("carry must never open a network connection")


urllib.request.urlopen = no_network

# `fold.check_append` (this spec's own new seam, produced by a sibling in the same
# wave) is not present on `fold.py` at this spec's base_sha (Declarations) — every
# check in this file sets it directly rather than depending on merge order. Default:
# honour everything, so every check below that does not override it behaves exactly
# as `carry()` did before this spec existed.
fold.check_append = lambda event, actor: None


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = carry.main(argv)
    return code, out.getvalue(), err.getvalue()


# L-spec-0195: a well-formed spec fixture — a real `## Verification` `&&` chain,
# an `## Acceptance Criteria` section, a path-shaped `Writes:` line — so every
# happy-path `writer()` fixture keeps clearing `validate.spec_shape` unchanged
# (Assumptions).
WELL_FORMED_SPEC = ("# fixture spec\n## Verification\n```\ntrue\n```\n"
                    "## Acceptance Criteria\nAC1 [backend]: x.\n  review_path: y\n"
                    "Writes: a.py\n")


def writer(footprint, path_written=True, rc=0, stderr="", status=None, spec_text=WELL_FORMED_SPEC):
    """A `doit dispatch spec-writer` that behaves the way the real one would."""
    def go(argv):
        if rc == 0 and path_written:
            pathlib.Path(argv[argv.index("--path") + 1]).write_text(spec_text)
            write_event("L-spec-writer-0001", {"type": "spec-written", "subject": argv[3],
                                               "project": "another-project-again",
                                               "path": argv[argv.index("--path") + 1],
                                               "footprint": footprint})
        elif rc == 0 and status:
            write_event("L-spec-writer-0001", {"type": "spawn-done", "subject": argv[3],
                                               "project": "another-project-again", "status": status})
        return rc, "" if rc else json.dumps({"ok": True}), stderr
    return go


TRAILER_LITERAL = ("\n---\nCarry this spec over into v2 form — Writes: line, AC ids, "
                   "Verification block, under 400 lines. Change nothing substantive. "
                   "charter: null (free-standing, D15). Source: v4 spec 1454.\n")

# ══ AC4 · review_tier — the four predicates, the no-match case, and the order ══
for path, want in [("api/app/routers/billing.py", ("full", "money")),
                   ("api/alembic_supabase/versions/0099_x.py", ("full", "migration")),
                   ("deploy.sh", ("full", "production-config")),
                   ("pipelines/amazon/runner.py", ("full", "production-data")),
                   ("/etc/cron.d/albert-scott-amazon", ("full", "production-config")),
                   ("scripts/amazon/feed_probes.py", ("gates-only", "none")),
                   ("src/carry.py", ("gates-only", "none"))]:
    check(carry.review_tier([path]) == want, f"review_tier({path!r}) -> {carry.review_tier([path])}")
check(carry.review_tier(["api/app/routers/billing_cron.py"]) == ("full", "money"),
      "★ first hit wins: a path matching both `billing` and `cron` reports money, not config")
check(carry.review_tier(["src/carry.py", "deploy.sh"]) == ("full", "production-config"),
      "the predicate scans every path in the footprint, not only the first")

# ══ AC2 · refusal by name, BEFORE allocation ═══════════════════════════════════
# Ordered first, while no content/L-spec-*.md can possibly exist yet.
CONTENT = pathlib.Path(os.environ["DOIT_ROOT"]) / "content"
record(1454, "feed-health-probes-fail-per-client-on-an-absent-date-column")
INBOX_BYTES = b"# 1454 fixture spec\n\nThe staged material, verbatim.\n"
inbox(1454, "feed-health-probes-fail-per-client-on-an-absent-date-column",
      INBOX_BYTES.decode())

record(1470, "empty-intent", intent="")
record(1471, "no-staged-spec-anywhere")
record(1499, "ambiguous-a"), record(1499, "ambiguous-b")
record(1472, "stale-spec-file-but-inbox-present", spec_file="/nowhere/at/all/1472-spec.md")
inbox(1472, "stale-spec-file-but-inbox-present", "# 1472 staged\n")

BASE = ["--repo", str(REPO), "--project", "albert-scott"]
for argv, needle, why in [
    (["1470"] + BASE, "empty or missing", "an empty `intent` is refused by name"),
    (["1471"] + BASE, "no staged spec — inbox glob", "no inbox match and no readable spec_file"),
    (["9999"] + BASE, "no v4 record matching 9999 under", "zero ledger matches, named"),
    (["1499"] + BASE, "1499 is ambiguous: 2 matches", "★ two matches is a refusal, never the first"),
]:
    code, out, err = run(argv)
    check(code != 0 and needle in err, f"{why}: {err!r}")

os.environ["DOIT_PROJECT"] = ""
code, out, err = run(["1454", "--repo", str(REPO)])
check(code != 0 and "--project is unset and DOIT_PROJECT is empty" in err,
      f"★ an unset project refuses rather than carrying into the wrong repo: {err!r}")
os.environ["DOIT_PROJECT"] = "pinned-by-the-suite"

check(ALLOCS[0] == 0, f"★ every refusal fired BEFORE alloc — {ALLOCS[0]} allocation(s) happened")
check(CALLS == [], f"★ no refusal reached a dispatch: {CALLS}")
check(list(CONTENT.glob("L-spec-*.md")) == [], "no placeholder spec file was left behind")

# the sixth fixture: a stale `spec_file` with an inbox copy present CARRIES.
STUB.dispatch = writer(["src/x.py"])
code, out, err = run(["1472"] + BASE)
check(code == 0, f"★ a stale spec_file with an inbox copy carries, it does not refuse: {err!r}")
slot = pathlib.Path(json.loads(out)["packet"])
check(slot.read_bytes().startswith(b"# 1472 staged\n"), "the inbox copy is the staged material")

# ...and `spec_file` is still the fallback when the inbox has no match at all.
record(1473, "spec-file-only", spec_file=str(TMP / "staging" / "1473-real-spec.md"))
(TMP / "staging" / "1473-real-spec.md").write_text("# 1473 from spec_file\n")
code, out, err = run(["1473"] + BASE)
check(code == 0 and pathlib.Path(json.loads(out)["packet"]).read_bytes().startswith(
    b"# 1473 from spec_file\n"), f"a readable spec_file is the second resolution step: {err!r}")

# ══ AC1 · carried_slot builds the hand path's exact shape ══════════════════════
FREE_STANDING_CLAUSE = "charter: null (free-standing, D15)."
ALLOCS[0], CALLS[:] = 0, []
slot, spec_id, source_id, charter, charter_reason = carry.carried_slot("1454")
body = slot.read_bytes()
check(body.startswith(INBOX_BYTES), "the packet STARTS with the staged bytes")
check(body[len(INBOX_BYTES):] == TRAILER_LITERAL.encode(),
      f"★ and its remainder is the trailer, byte for byte: {body[len(INBOX_BYTES):]!r}")
check(b"spawn_id" not in body, "★ carry appends no spawn_id line — dispatch owns that")
check("—" in TRAILER_LITERAL
      and carry.TRAILER.format(charter_clause=FREE_STANDING_CLAUSE, source_id="1454") == TRAILER_LITERAL,
      "the trailer constant is the precedent's, em dash included")
check(__import__("re").fullmatch(r"L-spec-\d{4}", spec_id), f"freshly allocated id: {spec_id}")
check(source_id == "1454", f"source_id is the v4 id: {source_id}")
check(charter is None, "★ charter is None — a carried spec is free-standing (D15)")
check(charter_reason == "absent", "★ no author frontmatter at all on this fixture — untrusted "
      "never even applies: absent, not untrusted")
check(carry.carried_slot("1454")[1] != spec_id, "a second call allocates a second id, never reuses")

# the full argv, through main
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["api/app/routers/billing.py", "docs/notes.md"])
code, out, err = run(["1454"] + BASE)
check(code == 0, f"the happy path exits 0: {err!r}")
res = json.loads(out)
d = next(c for c in CALLS if c[1] == "dispatch")
check(d[:4] == [str(carry.doit_bin()), "dispatch", "spec-writer", res["spec"]],
      f"dispatch is invoked as an argv list for the allocated id: {d[:4]}")
check(d[d.index("--packet") + 1] == res["packet"], "--packet is the slot this tool wrote")
want_path = str(pathlib.Path(os.environ["DOIT_ROOT"]) / "content" / f"{res['spec']}.md")
check(d[d.index("--path") + 1] == want_path and os.path.isabs(want_path),
      f"★ --path is ABSOLUTE: dispatch resolves it against the invoking shell's cwd: {want_path}")
check(d[d.index("--cwd") + 1] == str(REPO), "--cwd is the --repo value, never os.getcwd()")
check(d[d.index("--project") + 1] == "albert-scott", "--project is explicit, never the dir basename")
check("--charter" not in d, "★ no --charter anywhere: the carried spec is charter: null")
check(all(c[0] == str(carry.doit_bin()) and c[1] in ("dispatch", "append") for c in CALLS)
      and len(CALLS) == 2, f"★ only the two stubbed doit calls ran — nothing spent: {CALLS}")
check(ALLOCS[0] == 1, f"exactly one allocation on the happy path: {ALLOCS[0]}")

# ══ AC5(a) · the spec-carried event, its tier, and its verbatim audited_at ═════
ap = next(c for c in CALLS if c[1] == "append")
check(ap[2:4] == ["spec-carried", res["spec"]], f"append spec-carried <the new spec id>: {ap}")
kv = dict(x.split("=", 1) for x in ap[4:])
check(kv["source"] == "1454", f"source is the v4 id: {kv}")
check(kv["tier"] == "full" and res["rule"] == "money",
      "★ the tier came from the spec-written event's footprint, not from any markdown")
check(kv["audited_at"] == "2026-09-15T12:52:02Z",
      f"★ audited_at is the record's handed_over_at VERBATIM, not re-serialized: {kv}")
record(1474, "unquoted-stamp", at="2026-09-15T12:52:02Z")
check(carry.load_record(TMP / "ledger" / "1474-unquoted-stamp.yml")["handed_over_at"]
      == "2026-09-15T12:52:02Z",
      "★ an UNQUOTED stamp survives too — safe_load would hand back a datetime")
check((CONTENT / f"{res['spec']}.md").is_file(), "the writer's spec is at the allocated path")

# ══ AC3 · the guard sees what the project filter hides ═════════════════════════
carried = carry.already_carried("1454")
check(carried and carried["subject"] == res["spec"], "the guard finds the spec-carried event")
check(carried["project"] != os.environ["DOIT_PROJECT"], "...whose project differs from DOIT_PROJECT")
check(fold.PROJECT == "pinned-by-the-suite", "fold read DOIT_PROJECT at import")
check(not [e for e in fold.read_events() if e.get("type") == "spec-carried"],
      "★ fold.read_events() CANNOT see that event — a naive guard would double-spend $5")
ALLOCS[0], CALLS[:] = 0, []
code, out, err = run(["1454"] + BASE)
check(code != 0 and f"already carried as {res['spec']}" in err and "--force" in err,
      f"★ the guard fires anyway, naming the existing spec: {err!r}")
check(ALLOCS[0] == 0 and CALLS == [], "the guard refuses before alloc and before any dispatch")
code, out, err = run(["1454", "--force"] + BASE)
check(code == 0 and json.loads(out)["spec"] != res["spec"], f"--force carries again: {err!r}")
check(ALLOCS[0] == 1, "…and allocates exactly once when forced")

# ══ AC5(b) · an empty footprint refuses to stamp ═══════════════════════════════
record(1480, "empty-footprint"), inbox(1480, "empty-footprint", "# 1480\n")
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer([])
code, out, err = run(["1480"] + BASE)
check(code != 0 and "written with an empty footprint — no tier stamped" in err,
      f"★ an empty footprint is a refusal to stamp, not a silent gates-only: {err!r}")
check(not [c for c in CALLS if c[1] == "append"], "…and nothing was appended")
check(carry.already_carried("1480") is None, "no spec-carried event exists for it")

# ══ AC5(c) · dispatch's fail() path: no stamp, and a retry is permitted ════════
record(1481, "dispatch-fails"), inbox(1481, "dispatch-fails", "# 1481\n")
STUB.dispatch = writer(None, rc=1, stderr="FAILED L-spec-writer-0002: is_error: budget\n")
code, out, err = run(["1481"] + BASE)
check(code != 0 and "FAILED L-spec-writer-0002: is_error: budget" in err,
      f"★ the reason dispatch gave is printed, not swallowed: {err!r}")
check(carry.already_carried("1481") is None, "a failed dispatch stamps nothing")
STUB.dispatch = writer(["src/ok.py"])
code, out, err = run(["1481"] + BASE)
check(code == 0, f"★ …so the same source retries with no --force: a failed spawn is not burned: {err!r}")
check(json.loads(out)["tier"] == "gates-only", "and a neutral footprint tiers gates-only")

# ══ AC5(d) · a `killed` spawn: zero exit, no spec-written ══════════════════════
record(1482, "killed-spawn"), inbox(1482, "killed-spawn", "# 1482\n")
STUB.dispatch = writer(None, path_written=False, status="killed")
code, out, err = run(["1482"] + BASE)
check(code != 0 and "spawn returned status killed — no spec written" in err,
      f"★ a killed spawn names its status and stamps nothing: {err!r}")
check(carry.already_carried("1482") is None, "no spec-carried for a killed spawn")
STUB.dispatch = writer(["src/ok.py"])
code, out, err = run(["1482"] + BASE)
check(code == 0, "a killed spawn is a retry by design, not a permanently burned source")

# ══ L-spec-0195/AC5 · a spec that fails validate.spec_shape never reaches
#    spec-carried — the refusal names spec-shape and the findings ═════════════
record(1483, "malformed-verification"), inbox(1483, "malformed-verification", "# 1483\n")
BAD_SHAPE_SPEC = "# 1483\nno verification, no acceptance criteria, no writes grant\n"
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ok.py"], spec_text=BAD_SHAPE_SPEC)
code, out, err = run(["1483"] + BASE)
check(code != 0 and "fails spec-shape validation" in err and "Verification:" in err,
      f"★ a spec the tools cannot read never reaches spec-carried: {err!r}")
check(not [c for c in CALLS if c[1] == "append"], "…and nothing was appended")
check(carry.already_carried("1483") is None, "no spec-carried event exists for it")
# ...and the source is not burned: a retry with a well-formed spec carries.
STUB.dispatch = writer(["src/ok.py"])
code, out, err = run(["1483"] + BASE)
check(code == 0, f"★ a spec-shape refusal is a retry, not a permanently burned source: {err!r}")

# ══ AC9 · the PR url: recognized, constructed from flags, never fetched ════════
URL = "https://github.com/o/r/pull/1"
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["deploy.sh"])
code, out, err = run([URL, "--title", "A Carried Title", "--body", "the intent, prose and all"]
                     + BASE)
check(code == 0, f"the PR path carries from flags: {err!r}")
pk = pathlib.Path(json.loads(out)["packet"]).read_text()
front, _, rest = pk.partition("---\n\n")
check(json.dumps("the intent, prose and all") in front, f"frontmatter intent is --body: {front!r}")
check("source_brief: null" in front and json.dumps(HEAD) in front,
      f"★ frontmatter carries source_brief: null and the --repo HEAD: {front!r}")
check(rest.startswith("# A Carried Title\n"), f"the body starts with # <title>: {rest[:40]!r}")
check(pk.endswith(carry.TRAILER.format(charter_clause=FREE_STANDING_CLAUSE, source_id=URL)),
      f"★ …and ends with the trailer carrying the url as its source id: {pk[-90:]!r}")
check(json.loads(out)["source"] == URL, "the event's source is the url")
for flags in (["--title", "T"], ["--body", "B"], []):
    code, out, err = run([URL] + flags + BASE + ["--force"])
    check(code != 0 and "needs both --title and --body" in err,
          f"★ a missing flag is a refusal, never a silently empty packet: {flags} -> {err!r}")
check(carry.is_url(URL) and not carry.is_url("1454"),
      "a url is recognized as a url and a v4 id is not")
try:
    carry.carried_slot(URL)
    check(False, "carried_slot must refuse a url rather than glob the ledger for it")
except carry.Refusal as e:
    check("is a url, not a v4 record id" in str(e), f"★ …by name: {e}")
check(NET[0] == 0, "★ zero network calls: the live gh fetch is owed (AC9), never guessed")
check(all(c[1] in ("dispatch", "append") for c in CALLS), f"only the two doit calls ran: {CALLS}")

# ══ A9 · the PyYAML-less fallback refuses rather than mis-parses ═══════════════
p = TMP / "ledger" / "1490-folded.yml"
p.write_text("spec_id: 1490-folded\nintent: 'line one\n  and its fold'\nstatus: registered\n"
             "handed_over_at: '2026-09-15T12:52:02Z'\nspec_file: /nowhere\n")
check(carry._regex_record(p, p.read_text().replace("'line one", "plain")) ["intent"] == "plain",
      "the fallback reads a plain scalar")
try:
    carry._regex_record(p, p.read_text())
    check(False, "a folded intent must not be silently truncated by the fallback")
except carry.Refusal as e:
    check("folded or block scalar" in str(e), f"★ A9: the fallback refuses a fold, never guesses: {e}")

# ══ the dispatcher's three edits, read from the file (AC6) ═════════════════════
DOIT = (pathlib.Path(__file__).resolve().parent.parent / "doit").read_text()
check("\n  carry)   shift; exec python3 \"$SRC/carry.py\" \"$@\" ;;\n" in DOIT,
      "doit carries one new case line")
tc = DOIT.split("\n  test)")[1].split(";;")[0]
check("&&" not in tc, f"★ the test) case is no longer an && chain: {tc!r}")
check("test_*.py" in tc and "test_*.mjs" in tc and tc.index("test_*.py") < tc.index("test_*.mjs"),
      "★ it globs every test_*.py then every test_*.mjs — a new test file needs no edit here")
check("PASS" in tc and "FAIL" in tc and 'exit "$rc"' in tc,
      "one PASS/FAIL line per file, and a non-zero exit if any failed")
check(DOIT.count("doit carry") == 1, "exactly one help line names `doit carry`")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0185 · carry-both-ledgers — new ACs, this spec's own new numbering below
# ══════════════════════════════════════════════════════════════════════════════
FOOTPRINT_0185 = ["api/app/routers/billing.py", "docs/notes.md"]
VALID_STATUS_0185 = frozenset({
    "registered", "planned", "building", "gating", "ready", "merged", "shipped",
    "accepted", "awaiting-data", "awaiting-verify", "held", "bounced", "rework",
    "superseded", "retired", "unknown",
})
SPEC_ID_RE = __import__("re").compile(r"L-spec-\d{4}")


def fresh_root(name):
    """A new $DOIT_ROOT with its own empty events/content — never the shared
    `TMP/"ledger"`/`TMP/"inbox"`/`TMP/"staging"`, which stay put so `resolve_record`/
    `staged_material` still resolve the real "1454" fixture against them."""
    r = TMP / name
    (r / "content").mkdir(parents=True, exist_ok=True)
    (r / "events").mkdir(parents=True, exist_ok=True)
    os.environ["DOIT_ROOT"] = str(r)
    return r


def record_at(dirpath, num, stem, intent="carry me", spec_file=None,
             at="'2026-09-15T12:52:02Z'", status="registered"):
    """`record()`'s own shape, parametrized on directory — for the AC8-10 fresh
    ledger dir, which the module-level `record()` helper cannot target."""
    sf = spec_file if spec_file is not None else str(dirpath / f"{stem}-spec.md")
    body = (f"spec_id: {num}-{stem}\ntitle: fixture {num}\nintent: '{intent}'\n"
            f"status: {status}\nhanded_over_at: {at}\nspec_file: {sf}\nsource_brief: null\n")
    p = dirpath / f"{num}-{stem}.yml"
    p.write_text(body)
    return p


def inbox_at(dirpath, num, stem, text):
    p = dirpath / f"{num}-{stem}-spec.md"
    p.write_text(text)
    return p


# ══ AC1 · _do_carry / carry() / main() build on the same private callable ═════
fresh_root("acA")
STUB.dispatch = writer(list(FOOTPRINT_0185))
fold.check_append = lambda event, actor: None

ALLOCS[0], CALLS[:] = 0, []
res1 = carry._do_carry("1454", repo=REPO, project="albert-scott")
check(set(res1) == {"spec", "source", "tier", "rule", "packet", "path", "project"},
      f"the seven-field dict: {res1}")
check(bool(SPEC_ID_RE.fullmatch(res1["spec"])), f"freshly allocated id: {res1['spec']}")
check(res1["source"] == "1454", f"res1 source: {res1}")
check(len(CALLS) == 2 and CALLS[0][1] == "dispatch" and CALLS[1][1] == "append",
      f"★ _do_carry returns the seven-field dict; carry() and main() both build on it, "
      f"force-carrying past the idempotency guard between the three calls: {CALLS}")

ALLOCS[0], CALLS[:] = 0, []
res2_id = carry.carry("1454", repo=REPO, project="albert-scott", force=True)
check(isinstance(res2_id, str) and bool(SPEC_ID_RE.fullmatch(res2_id)) and res2_id != res1["spec"],
      f"★ _do_carry returns the seven-field dict; carry() and main() both build on it, "
      f"force-carrying past the idempotency guard between the three calls: {res2_id}")
check(len(CALLS) == 2 and CALLS[0][1] == "dispatch" and CALLS[1][1] == "append",
      f"carry() fires the same two calls: {CALLS}")

ALLOCS[0], CALLS[:] = 0, []
code, out, err = run(["1454", "--force", "--repo", str(REPO), "--project", "albert-scott"])
check(code == 0, f"main() forced carry exits 0: {err!r}")
res3 = json.loads(out)
check(set(res3) == {"spec", "source", "tier", "rule", "packet", "path", "project"},
      f"★ _do_carry returns the seven-field dict; carry() and main() both build on it, "
      f"force-carrying past the idempotency guard between the three calls: {res3}")
check(len(CALLS) == 2 and CALLS[0][1] == "dispatch" and CALLS[1][1] == "append",
      f"main() fires the same two calls: {CALLS}")

# ══ AC4 · footprint rides the append argv as a JSON list, order preserved ═════
ap4 = next(c for c in CALLS if c[1] == "append")
fp_elem = next(x for x in ap4[4:] if x.startswith("footprint="))
check(fp_elem == f"footprint={json.dumps(FOOTPRINT_0185)}",
      f"★ spec-carried carries footprint as a JSON list, order preserved: {fp_elem!r}")
check(json.loads(fp_elem.split("=", 1)[1]) == FOOTPRINT_0185,
      f"★ spec-carried carries footprint as a JSON list, order preserved: {fp_elem!r}")

# ══ AC2 · fold.check_append is consulted, event shape and literal actor ═══════
fresh_root("acB")
CHECK_APPEND_CALLS_0185 = []


def _record_check_append(event, actor):
    CHECK_APPEND_CALLS_0185.append((dict(event), actor))
    return None


fold.check_append = _record_check_append
STUB.dispatch = writer(list(FOOTPRINT_0185))
ALLOCS[0], CALLS[:] = 0, []
res_ac2 = carry._do_carry("1454", repo=REPO, project="albert-scott")
check(len(CHECK_APPEND_CALLS_0185) == 1,
      f"★ check_append is consulted with the constructed event and the literal actor "
      f"\"operator\": {CHECK_APPEND_CALLS_0185}")
ev2, actor2 = CHECK_APPEND_CALLS_0185[0]
check(ev2["type"] == "spec-carried" and ev2["subject"] == res_ac2["spec"]
      and ev2["footprint"] == FOOTPRINT_0185 and actor2 == "operator",
      f"★ check_append is consulted with the constructed event and the literal actor "
      f"\"operator\": {ev2!r} actor={actor2!r}")

# ══ AC3 · a fold-refused append raises CarryFailed, stamps nothing, ═══════════
# ══        does not burn the source, never touches sync_v4 ═══════════════════
fresh_root("acC")
fold.check_append = lambda event, actor: "actor not permitted"
STUB.dispatch = writer(list(FOOTPRINT_0185))
ALLOCS[0], CALLS[:] = 0, []
_orig_sync_v4_0185 = carry.sync_v4


def _sync_v4_boom(*a, **k):
    raise AssertionError("★ a fold-refused append is CarryFailed, stamps nothing, does not burn "
                         "the source, and never touches sync_v4 — sync_v4 was called")


carry.sync_v4 = _sync_v4_boom
try:
    try:
        carry.carry("1454", repo=REPO, project="albert-scott")
        check(False, "★ a fold-refused append is CarryFailed, stamps nothing, does not burn the "
                     "source, and never touches sync_v4 — no exception was raised")
    except carry.CarryFailed as e:
        check("would not be honoured by the fold" in str(e) and "actor not permitted" in str(e),
              f"★ a fold-refused append is CarryFailed, stamps nothing, does not burn the source, "
              f"and never touches sync_v4: {e}")
    check([c for c in CALLS if c[1] == "dispatch"] and not [c for c in CALLS if c[1] == "append"],
          f"★ a fold-refused append is CarryFailed, stamps nothing, does not burn the source, and "
          f"never touches sync_v4: {CALLS}")
    check(carry.already_carried("1454") is None,
          f"★ a fold-refused append is CarryFailed, stamps nothing, does not burn the source, and "
          f"never touches sync_v4: {carry.already_carried('1454')}")
    fold.check_append = lambda event, actor: None
    ALLOCS[0], CALLS[:] = 0, []
    retry_id = carry.carry("1454", repo=REPO, project="albert-scott")
    check(bool(SPEC_ID_RE.fullmatch(retry_id)),
          f"★ a fold-refused append is CarryFailed, stamps nothing, does not burn the source, and "
          f"never touches sync_v4: the refused source retries unforced: {retry_id}")
finally:
    carry.sync_v4 = _orig_sync_v4_0185

# ══ AC5 · sync_v4 flips registered → superseded + superseded_by ═══════════════
LEDGER_0185 = TMP / "ledger"
P1454 = LEDGER_0185 / "1454-feed-health-probes-fail-per-client-on-an-absent-date-column.yml"
before_1454 = P1454.read_text()
check("status: registered" in before_1454, f"the shared 1454 fixture is still registered: "
      f"{before_1454!r}")
n5 = carry.sync_v4([{"type": "spec-carried", "subject": "L-spec-0900", "source": "1454"}],
                   ledger_dir=LEDGER_0185)
check(n5 == 1, f"sync_v4 returns 1: {n5}")
after_1454 = P1454.read_text()
check("\nstatus: superseded\n" in after_1454,
      f"★ sync_v4 flips registered to superseded and stamps superseded_by, checked against the "
      f"v4 schema's own closed status enum, byte-identical otherwise: {after_1454!r}")
check("\nsuperseded_by: L-spec-0900\n" in after_1454,
      f"★ sync_v4 flips registered to superseded and stamps superseded_by, checked against the "
      f"v4 schema's own closed status enum, byte-identical otherwise: {after_1454!r}")
check("superseded" in VALID_STATUS_0185,
      "★ sync_v4 flips registered to superseded and stamps superseded_by, checked against the v4 "
      "schema's own closed status enum, byte-identical otherwise: membership, not bare equality")
reverted = after_1454.replace("status: superseded\nsuperseded_by: L-spec-0900\n",
                              "status: registered\n")
check(reverted == before_1454,
      f"★ sync_v4 flips registered to superseded and stamps superseded_by, checked against the "
      f"v4 schema's own closed status enum, byte-identical otherwise: every other byte unchanged")

# ══ AC6 · sync_v4 is idempotent, never regresses a non-registered record ══════
record(1495, "shipped-fixture", status="shipped")
P1495 = LEDGER_0185 / "1495-shipped-fixture.yml"
before_1495 = P1495.read_text()
n6 = carry.sync_v4([{"type": "spec-carried", "subject": "L-spec-0900", "source": "1454"},
                    {"type": "spec-carried", "subject": "L-spec-0777", "source": "1495"}],
                   ledger_dir=LEDGER_0185)
check(n6 == 0, f"★ sync_v4 is idempotent and never regresses a non-registered record: n={n6}")
check(P1454.read_text() == after_1454,
      "★ sync_v4 is idempotent and never regresses a non-registered record: 1454 re-touched")
check(P1495.read_text() == before_1495,
      "★ sync_v4 is idempotent and never regresses a non-registered record: 1495 was touched")

# ══ AC7 · PR/zero-match/ambiguous sources are skipped, never raise ════════════
P1472 = LEDGER_0185 / "1472-stale-spec-file-but-inbox-present.yml"
before_1472 = P1472.read_text()
check("status: registered" in before_1472, "1472 is still registered before AC7")
events7 = [
    {"type": "spec-carried", "subject": "L-spec-0001", "source": "https://github.com/o/r/pull/42"},
    {"type": "spec-carried", "subject": "L-spec-0002", "source": "9999"},
    {"type": "spec-carried", "subject": "L-spec-0003", "source": "1499"},
    {"type": "spec-carried", "subject": "L-spec-0004", "source": "1472"},
]
n7 = carry.sync_v4(events7, ledger_dir=LEDGER_0185)
check(n7 == 1, f"★ sync_v4 skips PR/zero-match/ambiguous sources without raising and counts only "
              f"real updates: n={n7}")
after_1472 = P1472.read_text()
check("\nstatus: superseded\n" in after_1472 and "\nsuperseded_by: L-spec-0004\n" in after_1472,
      f"★ sync_v4 skips PR/zero-match/ambiguous sources without raising and counts only real "
      f"updates: {after_1472!r}")
check((LEDGER_0185 / "1499-ambiguous-a.yml").read_text().find("status: registered") != -1
      and (LEDGER_0185 / "1499-ambiguous-b.yml").read_text().find("status: registered") != -1,
      "★ sync_v4 skips PR/zero-match/ambiguous sources without raising and counts only real "
      "updates: an ambiguous source was touched")

# ══ AC8-10 · uncarried(), a fresh isolated ledger_dir/inbox pair ══════════════
FRESH = TMP / "fresh-810"
FL, FI = FRESH / "ledger", FRESH / "inbox"
FL.mkdir(parents=True, exist_ok=True)
FI.mkdir(parents=True, exist_ok=True)
record_at(FL, 1471, "no-staged-spec-anywhere", spec_file=str(FRESH / "nowhere" / "1471-spec.md"))
record_at(FL, 1472, "inbox-match", spec_file=str(FRESH / "nowhere" / "1472-spec.md"))
inbox_at(FI, 1472, "inbox-match", "# 1472 fresh inbox\n")
record_at(FL, 1454, "fresh-carried", spec_file=str(FRESH / "nowhere" / "1454-spec.md"))
inbox_at(FI, 1454, "fresh-carried", "# 1454 fresh inbox\n")
record_at(FL, 1490, "shipped-fresh", status="shipped")

events8 = [{"type": "spec-carried", "subject": "L-spec-0500", "source": "1454"}]
u8 = carry.uncarried(events8, inbox=FI, ledger_dir=FL)
check(len(u8) == 1 and u8[0]["source"] == "1472" and u8[0]["kind"] == "v4",
      f"★ uncarried() reports only registered, stageable, not-yet-carried v4 sources — proven "
      f"where the already-carried exclusion is the ONLY reason 1454 drops out: {u8}")
check(u8[0]["registered_at"] == "2026-09-15T12:52:02Z", f"registered_at is handed_over_at: {u8}")
check(carry.load_record(FL / "1454-fresh-carried.yml")["status"] == "registered",
      f"★ uncarried() reports only registered, stageable, not-yet-carried v4 sources — proven "
      f"where the already-carried exclusion is the ONLY reason 1454 drops out: on-disk 1454 status "
      f"already flipped, which would mask the events-based exclusion")

events9 = events8 + [
    {"type": "inbound-registered", "source": "https://github.com/o/r/pull/9",
     "ts": "2026-09-10T00:00:00Z"},
    {"type": "inbound-registered", "source": "https://github.com/o/r/pull/URL2",
     "ts": "2026-09-11T00:00:00Z"},
    {"type": "spec-carried", "subject": "L-spec-0501", "source": "https://github.com/o/r/pull/URL2"},
]
u9 = carry.uncarried(events9, inbox=FI, ledger_dir=FL)
check(len(u9) == 2, f"★ uncarried() merges v4 and pr kinds, sorted by source: {u9}")
check([e["source"] for e in u9] == sorted(e["source"] for e in u9),
      f"★ uncarried() merges v4 and pr kinds, sorted by source: {u9}")
check(any(e["kind"] == "pr" and e["source"] == "https://github.com/o/r/pull/9"
         and e["registered_at"] == "2026-09-10T00:00:00Z" for e in u9),
      f"★ uncarried() merges v4 and pr kinds, sorted by source: {u9}")
check(not any(e["source"] == "https://github.com/o/r/pull/URL2" for e in u9),
      f"★ uncarried() merges v4 and pr kinds, sorted by source: already-carried pr url leaked: {u9}")

events10 = events9 + [
    {"type": "carry-failed", "source": "1472", "attempt": 1, "error": "no staged spec"},
    {"type": "carry-failed", "source": "1472", "attempt": 2, "error": "still none"},
]
u10 = carry.uncarried(events10, inbox=FI, ledger_dir=FL)
entry10 = next(e for e in u10 if e["source"] == "1472")
check(entry10["attempts"] == 2 and entry10["last_error"] == "still none",
      f"★ attempts/last_error are read from carry-failed, last entry wins: {entry10}")

# ══ AC12 · a non-digit source is never glob-matched outside ledger_dir ════════
decoy = TMP / "decoy-1.yml"
decoy.write_bytes(b"status: registered\n")
before_listing = sorted(p.name for p in TMP.iterdir())
events12 = [
    {"type": "spec-carried", "subject": "L-spec-0777", "source": "../decoy"},
    {"type": "spec-carried", "subject": "L-spec-0778", "source": "https://x/y"},
]
n12 = carry.sync_v4(events12, ledger_dir=LEDGER_0185)
check(n12 == 0,
      f"★ a non-digit source, including one shaped for path traversal, is never glob-matched "
      f"outside ledger_dir: n={n12}")
check(decoy.read_bytes() == b"status: registered\n",
      f"★ a non-digit source, including one shaped for path traversal, is never glob-matched "
      f"outside ledger_dir: decoy was mutated")
after_listing = sorted(p.name for p in TMP.iterdir())
check(before_listing == after_listing,
      f"★ a non-digit source, including one shaped for path traversal, is never glob-matched "
      f"outside ledger_dir: {before_listing} -> {after_listing}")

# ══════════════════════════════════════════════════════════════════════════════
# L-spec-0198 · trusted-author-inbound-charter (L-charter-0028)
# ══════════════════════════════════════════════════════════════════════════════
TRUST_DIR = TMP / "trust"
TRUST_DIR.mkdir(parents=True, exist_ok=True)


def fm_v4_spec(author_line, charter_line=None, body="# fixture body\n"):
    """A staged v4 spec's frontmatter block in the live spec-inbox shape (AC2/AC3):
    `intent:`/`author:`/`base_sha:`[/`charter:`]."""
    lines = ["---", "intent: 'carry me'", f"author: {author_line}", "base_sha: 'deadbeef'"]
    if charter_line is not None:
        lines.append(f"charter: {charter_line}")
    lines += ["---", "", body]
    return "\n".join(lines)


# ══ AC1 · trusted_authors() ═════════════════════════════════════════════════════
GOOD_TOML = TRUST_DIR / "good.toml"
GOOD_TOML.write_text('[[author]]\nlogin = "yitzchak-eg"\nemail = "yitzchak@ephraimgreenblatt.com"\n\n'
                     '[[author]]\nlogin = "second-row"\nemail = "second@example.com"\n')
check(carry.trusted_authors(GOOD_TOML) == [
    {"login": "yitzchak-eg", "email": "yitzchak@ephraimgreenblatt.com"},
    {"login": "second-row", "email": "second@example.com"}],
    f"AC1: a well-formed TOML file returns one dict per table, both rows: "
    f"{carry.trusted_authors(GOOD_TOML)}")

MALFORMED_TOML = TRUST_DIR / "malformed.toml"
MALFORMED_TOML.write_text("[[author]\nlogin = oops\n")  # invalid TOML syntax
check(carry.trusted_authors(MALFORMED_TOML) == [],
      "★ AC1: an invalid TOML file degrades to [], never raises")

WRONGSHAPE_TOML = TRUST_DIR / "wrongshape.toml"
WRONGSHAPE_TOML.write_text('author = "not-a-list-of-tables"\n')
check(carry.trusted_authors(WRONGSHAPE_TOML) == [],
      "★ AC1: a non-list `author` key degrades to [], never raises")

check(carry.trusted_authors(TRUST_DIR / "does-not-exist.toml") == [],
      "AC1: a non-existent path returns []")

INCOMPLETE_TOML = TRUST_DIR / "incomplete.toml"
INCOMPLETE_TOML.write_text('[[author]]\nlogin = "only-login"\n\n[[author]]\nemail = "only@email.com"\n')
check(carry.trusted_authors(INCOMPLETE_TOML) == [],
      "AC1: an incomplete table (one of login/email missing) is dropped, not an error")

# path=None default, over an otherwise-empty fixture root: no live allowlist file
# exists yet under $DOIT_ROOT at all.
LIVE_ALLOWLIST = pathlib.Path(os.environ["DOIT_ROOT"]) / "trusted_inbound_authors.toml"
check(not LIVE_ALLOWLIST.exists(), "AC1 precondition: no live allowlist file under this fixture root")
check(carry.trusted_authors(None) == [], "AC1: path=None with no file present returns []")

# ══ AC2 · frontmatter_charter() ═════════════════════════════════════════════════
FM_HAPPY = "---\nintent: 'x'\nauthor: 'a <a@b.com>'\nbase_sha: 'deadbeef'\ncharter: L-charter-0028\n---\n\n# body\n"
check(carry.frontmatter_charter(FM_HAPPY) == "L-charter-0028",
      f"AC2: the trimmed charter: value from a leading frontmatter block: "
      f"{carry.frontmatter_charter(FM_HAPPY)!r}")
FM_NO_BLOCK = "# no leading frontmatter block at all\ncharter: L-charter-0028\n"
check(carry.frontmatter_charter(FM_NO_BLOCK) is None, "AC2: no leading --- block -> None")
FM_NO_KEY = "---\nintent: 'x'\nauthor: 'a <a@b.com>'\n---\n\n# body\n"
check(carry.frontmatter_charter(FM_NO_KEY) is None, "AC2: a block with no charter: key -> None")
for blank_val in ("null", "~", ""):
    fm = f"---\nintent: 'x'\ncharter: {blank_val}\n---\n\n# body\n"
    check(carry.frontmatter_charter(fm) is None, f"AC2: charter: {blank_val!r} -> None")

# ══ the live allowlist: one trusted row, used by AC3/AC4/AC6/AC7 below ═════════
ALLOWLIST_TEXT = '[[author]]\nlogin = "trusted-eg"\nemail = "trusted@example.com"\n'
LIVE_ALLOWLIST.write_text(ALLOWLIST_TEXT)

# ══ AC3 · trust matches, charter validity project-unfiltered, charter honored ══
CHARTER_AC3 = "L-charter-2198"
write_event("L-thinker-2198", {"type": "charter-filed", "subject": CHARTER_AC3,
                               "project": "yet-another-project-for-ac3"})
record(1600, "trusted-with-open-charter")
inbox(1600, "trusted-with-open-charter",
      fm_v4_spec("Trusted Person <trusted@example.com>", CHARTER_AC3, "# 1600 fixture spec\n"))

slot3, spec_id3, source_id3, charter3, charter_reason3 = carry.carried_slot("1600")
check(charter3 == CHARTER_AC3 and charter_reason3 is None,
      f"AC3: carried_slot returns the honored charter and no charter_reason: "
      f"{charter3!r} {charter_reason3!r}")
check(b"charter: null" not in slot3.read_bytes(), "AC3: the packet does not contain 'charter: null'")

ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac3.py"])
code, out, err = run(["1600"] + BASE)
check(code == 0, f"AC3: the full carry succeeds through main(): {err!r}")
res_ac3 = json.loads(out)
d_ac3 = next(c for c in CALLS if c[1] == "dispatch")
check(d_ac3[3] == res_ac3["spec"] and d_ac3[4:6] == ["--charter", CHARTER_AC3],
      f"AC3: the dispatch argv contains --charter <id> right after the id: {d_ac3}")
ap_ac3 = next(c for c in CALLS if c[1] == "append")
kv_ac3 = dict(x.split("=", 1) for x in ap_ac3[4:])
check(kv_ac3.get("charter") == CHARTER_AC3, f"AC3: charter present on the append: {kv_ac3}")
check("charter_reason" not in kv_ac3, f"★ AC3: charter_reason KEY ABSENT (finding 3): {kv_ac3}")
check(b"charter: null" not in pathlib.Path(res_ac3["packet"]).read_bytes(),
      "AC3: no 'charter: null' anywhere in the final packet")

# ══ AC4 · the four-way charter_reason split, first three ALLOWLISTED-author ════
def assert_not_honored(record_num, want_reason):
    ALLOCS[0], CALLS[:] = 0, []
    STUB.dispatch = writer(["src/ac4.py"])
    code, out, err = run([str(record_num)] + BASE)
    check(code == 0, f"AC4({want_reason}): fixture {record_num} carries cleanly: {err!r}")
    res = json.loads(out)
    d = next(c for c in CALLS if c[1] == "dispatch")
    check("--charter" not in d, f"AC4({want_reason}): no --charter anywhere: {d}")
    ap = next(c for c in CALLS if c[1] == "append")
    kv = dict(x.split("=", 1) for x in ap[4:])
    check("charter" not in kv, f"AC4({want_reason}): charter KEY ABSENT: {kv}")
    check(kv.get("charter_reason") == want_reason, f"AC4({want_reason}): {kv}")
    pkt = pathlib.Path(res["packet"]).read_bytes()
    want_trailer = carry.TRAILER.format(charter_clause=FREE_STANDING_CLAUSE,
                                        source_id=str(record_num)).encode()
    check(pkt.endswith(want_trailer),
          f"AC4({want_reason}): the unchanged free-standing trailer, byte-identical to "
          f"TRAILER_LITERAL's own literal: {pkt[-140:]!r}")
    return res


record(1601, "trusted-no-charter")
inbox(1601, "trusted-no-charter",
      fm_v4_spec("Trusted Person <trusted@example.com>", None, "# 1601 fixture\n"))
assert_not_honored(1601, "absent")

CHARTER_AC4B = "L-charter-2199"  # never named by any event anywhere on this ledger
record(1602, "trusted-unknown-charter")
inbox(1602, "trusted-unknown-charter",
      fm_v4_spec("Trusted Person <trusted@example.com>", CHARTER_AC4B, "# 1602 fixture\n"))
assert_not_honored(1602, "unknown")

CHARTER_AC4C = "L-charter-2200"
write_event("L-operator-2200", {"type": "charter-filed", "subject": CHARTER_AC4C, "covers": "none"})
write_event("L-executor-2200", {"type": "sweep-fixpoint", "subject": CHARTER_AC4C})
_, charters_ac4c, _, _ = fold.fold(carry.scan_events(lambda e: True))
check(charters_ac4c[CHARTER_AC4C]["state"] == "L2-complete",
      f"AC4(c) precondition: the fixture charter really is L2-complete: {charters_ac4c[CHARTER_AC4C]}")
record(1603, "trusted-closed-charter")
inbox(1603, "trusted-closed-charter",
      fm_v4_spec("Trusted Person <trusted@example.com>", CHARTER_AC4C, "# 1603 fixture\n"))
assert_not_honored(1603, "closed")

record(1604, "untrusted-with-open-charter")
inbox(1604, "untrusted-with-open-charter",
      fm_v4_spec("Random Person <nobody@example.com>", CHARTER_AC3, "# 1604 fixture\n"))
assert_not_honored(1604, "untrusted")

# ══ AC5 · regression guard — structural, true regardless of trust ══════════════
record(1605, "trusted-plain")
inbox(1605, "trusted-plain",
      fm_v4_spec("Trusted Person <trusted@example.com>", None, "# 1605 fixture\n"))
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac5.py"])
code, out, err = run(["1605"] + BASE)
check(code == 0, f"AC5: fixture carries cleanly: {err!r}")
res_ac5 = json.loads(out)
ap_ac5 = next(c for c in CALLS if c[1] == "append")
kv_ac5 = dict(x.split("=", 1) for x in ap_ac5[4:])
check(kv_ac5.get("trusted_author") == "trusted-eg",
      f"AC5: trusted_author is the matched row's login: {kv_ac5}")
check([c[1] for c in CALLS] == ["dispatch", "append"],
      f"★ AC5: the CALLS list contains only dispatch/append — no spec-auditor anywhere: {CALLS}")
_grep = __import__("subprocess").run(
    ["grep", "-c", "spec-auditor", str(pathlib.Path(__file__).resolve().parent / "carry.py")],
    capture_output=True, text=True)
check(_grep.stdout.strip() == "0",
      f"★ AC5: grep -c 'spec-auditor' src/carry.py reads 0 — D1 is structural: {_grep.stdout!r}")
_all_ev_ac5 = carry.scan_events(lambda e: True)
_specs_ac5, _, _, _ = fold.fold(_all_ev_ac5)
check(_specs_ac5[res_ac5["spec"]]["state"] == "written",
      f"AC5: fold.fold() reports the carried spec's state as 'written': {_specs_ac5[res_ac5['spec']]}")

# ══ AC6 · the three not-trusted-by-default shapes ══════════════════════════════
# (a) an untrusted author naming no charter at all (disjoint from AC4(d), which
# names a charter): trusted_author=None, charter_reason="absent", tier/rule as always.
record(1606, "untrusted-no-charter")
inbox(1606, "untrusted-no-charter",
      fm_v4_spec("Random Person <nobody@example.com>", None, "# 1606 fixture\n"))
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac6a.py"])
code, out, err = run(["1606"] + BASE)
check(code == 0, f"AC6(a): fixture carries cleanly: {err!r}")
res_ac6a = json.loads(out)
ap_ac6a = next(c for c in CALLS if c[1] == "append")
kv_ac6a = dict(x.split("=", 1) for x in ap_ac6a[4:])
check("trusted_author" not in kv_ac6a, f"AC6(a): trusted_author key absent: {kv_ac6a}")
check("charter" not in kv_ac6a and kv_ac6a.get("charter_reason") == "absent",
      f"AC6(a): charter_reason=absent, charter key absent: {kv_ac6a}")
check(res_ac6a["tier"] == "gates-only" and res_ac6a["rule"] == "none",
      f"AC6(a): tier/rule unaffected by trust: {res_ac6a}")

# (b) no live allowlist file at all: absent file trusts nobody, even an email
# that would otherwise match a real row.
LIVE_ALLOWLIST_BACKUP = LIVE_ALLOWLIST.read_bytes()
LIVE_ALLOWLIST.unlink()
check(carry.trusted_authors(None) == [], "AC6(b): trusted_authors(None) returns [] with no file")
record(1607, "would-have-matched")
TEXT_1607 = fm_v4_spec("Trusted Person <trusted@example.com>", None, "# 1607 fixture\n")
inbox(1607, "would-have-matched", TEXT_1607)
check(carry.trusted_author_for(TEXT_1607) is None,
      "★ AC6(b): an absent allowlist file trusts nobody, even a would-have-matched email")
slot6b, _, _, charter6b, reason6b = carry.carried_slot("1607")
check(charter6b is None and reason6b == "absent", f"AC6(b): {charter6b!r} {reason6b!r}")
LIVE_ALLOWLIST.write_bytes(LIVE_ALLOWLIST_BACKUP)

# (c) a PR-sourced carry always stamps trusted_author=None and
# (charter, charter_reason) = (None, "absent"), regardless of --body.
PR_URL_AC6C = "https://github.com/o/r/pull/606"
slot6c, _, _, charter6c, reason6c = carry.pr_slot(
    PR_URL_AC6C, "T", "please add trusted@example.com — looks like a match, is not", str(REPO))
check(charter6c is None and reason6c == "absent", f"AC6(c): {charter6c!r} {reason6c!r}")
check(carry.trusted_author_for(slot6c.read_text()) is None,
      "★ AC6(c): a PR-sourced author frontmatter (the operator's own username) never matches, "
      "regardless of what --body contains")

# ══ AC7 · the falsifiable trust assertion ═══════════════════════════════════════
TEMPLATE_AC7 = fm_v4_spec("AC7 Person <ac7@example.com>", CHARTER_AC3, "# ac7 template\n")
record(1700, "ac7-template-a")
inbox(1700, "ac7-template-a", TEMPLATE_AC7)
record(1701, "ac7-template-b")
inbox(1701, "ac7-template-b", TEMPLATE_AC7)

LIVE_ALLOWLIST.write_text(LIVE_ALLOWLIST.read_text()
                          + '\n[[author]]\nlogin = "ac7-eg"\nemail = "ac7@example.com"\n')
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac7.py"])
code, out, err = run(["1700"] + BASE)
check(code == 0, f"AC7 (trusted run): {err!r}")
ap7t = next(c for c in CALLS if c[1] == "append")
kv7t = dict(x.split("=", 1) for x in ap7t[4:])

LIVE_ALLOWLIST.write_bytes(LIVE_ALLOWLIST_BACKUP)  # back to just trusted-eg — ac7@ untrusted again
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac7.py"])
code, out, err = run(["1701"] + BASE)
check(code == 0, f"AC7 (untrusted run): {err!r}")
ap7u = next(c for c in CALLS if c[1] == "append")
kv7u = dict(x.split("=", 1) for x in ap7u[4:])

check(kv7t["source"] != kv7u["source"],
      "AC7: source differs only by the distinct ids used to dodge idempotency")
_shared7 = (set(kv7t) | set(kv7u)) - {"source"}
_diffs7 = {k for k in _shared7 if kv7t.get(k) != kv7u.get(k)}
check(_diffs7 == {"trusted_author", "charter", "charter_reason"},
      f"★ AC7: the diff's key set is exactly trusted_author/charter/charter_reason: {_diffs7}")
check(kv7t["tier"] == kv7u["tier"] and kv7t["footprint"] == kv7u["footprint"],
      f"AC7: tier/footprint are byte-identical: {kv7t} vs {kv7u}")
check("trusted_author" in kv7t and "trusted_author" not in kv7u,
      f"★ AC7: trusted_author present vs absent: {kv7t} vs {kv7u}")
check("charter" in kv7t and kv7t["charter"] == CHARTER_AC3 and "charter" not in kv7u,
      f"★ AC7: charter present (honored) vs absent: {kv7t} vs {kv7u}")
check("charter_reason" not in kv7t and kv7u.get("charter_reason") == "untrusted",
      f"★ AC7: charter_reason absent vs 'untrusted': {kv7t} vs {kv7u}")

# ══ AC14 (SD13) · --from-ledger resolves title/body in-process, never a shell ═
PR_URL_AC14 = "https://github.com/o/r/pull/1414"
write_event("L-intake-local", {"type": "inbound-registered", "subject": PR_URL_AC14,
                               "source": PR_URL_AC14, "project": "albert-scott",
                               "title": "AC14 fixture title", "author_login": "trusted-eg",
                               "auto": True, "body": "AC14 fixture body"})
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac14.py"])
code, out, err = run([PR_URL_AC14, "--from-ledger"] + BASE)
check(code == 0, f"AC14: a --from-ledger carry succeeds through main(): {err!r}")
res14 = json.loads(out)
packet14 = pathlib.Path(res14["packet"]).read_text()
check(carry._frontmatter_value(packet14, "intent") == "AC14 fixture body",
      f"AC14: the packet's intent matches the ledger event's body byte-for-byte: {packet14!r}")
check("# AC14 fixture title" in packet14, f"AC14: the packet's title heading matches: {packet14!r}")
check(carry.frontmatter_charter(packet14) is None,
      f"AC14: charter absent/null exactly as a manual --title/--body carry of the same body would "
      f"produce (charter/author are `pr_slot`'s own, untouched — Boundaries): {packet14!r}")

# combining --from-ledger with --title/--body refuses BEFORE any alloc
allocs_before14 = ALLOCS[0]
try:
    carry._do_carry(PR_URL_AC14 + "-combo", repo=REPO, project="albert-scott",
                    from_ledger=True, title="x", body="y")
    raise AssertionError("AC14: combining --from-ledger with --title/--body must refuse")
except carry.Refusal:
    pass
check(ALLOCS[0] == allocs_before14, "AC14: the combined-flags refusal fires before any alloc")

# --from-ledger naming a url with no inbound-registered event refuses by name
try:
    carry._do_carry("https://github.com/o/r/pull/9999-no-such-registration", repo=REPO,
                    project="albert-scott", from_ledger=True)
    raise AssertionError("AC14: --from-ledger with no matching event must refuse")
except carry.Refusal:
    pass

# --title/--body keep working exactly as today for a source not using --from-ledger
ALLOCS[0], CALLS[:] = 0, []
STUB.dispatch = writer(["src/ac14b.py"])
code, out, err = run(["https://github.com/o/r/pull/1415", "--title", "manual t",
                     "--body", "manual b"] + BASE)
check(code == 0, f"AC14: a plain --title/--body carry (no --from-ledger) is unaffected: {err!r}")

print(f"carry: {N} checks pass")
