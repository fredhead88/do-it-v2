#!/usr/bin/env python3
"""One runnable check on `intake`. Run: python3 test_intake.py

Everything is a fixture — `DOIT_ROOT` points under a fresh temp dir, and
`intake.subprocess` is replaced BEFORE any `intake` call fires (AC11), so no
`gh` process and no network connection is ever reached from this file.
"""
import base64, json, os, pathlib, sys, tempfile, types

TMP = pathlib.Path(tempfile.mkdtemp(prefix="intake-test-"))
os.environ["DOIT_ROOT"] = str(TMP)
os.environ.pop("DOIT_PROJECT", None)
for d in ("events", "content"):
    (TMP / d).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import carry, dispatch, fold, intake  # noqa: E402

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


# ── network guard (AC11) ────────────────────────────────────────────────────
NET = [0]
import urllib.request  # noqa: E402


def no_network(*a, **k):
    NET[0] += 1
    raise AssertionError("intake must never open a network connection")


urllib.request.urlopen = no_network

# ── the stub: every subprocess call `intake` makes, recorded and canned ────
CALLS = []


def _ok(out=""):
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


def _fail(err="boom"):
    return types.SimpleNamespace(returncode=1, stdout="", stderr=err)


class Stub:
    """Stands in for `intake.subprocess`. Records argv, returns a canned
    result, never runs anything real — mirrors `carry.py`'s own `Stub`/`CALLS`
    precedent (`test_carry.py:78-104`)."""
    def __init__(self):
        self.rate_remaining = 5000
        self.rate_fail = False
        self.listings = {"inbound-spec": [], "inbound-note": []}
        self.listing_fail = set()
        self.contents = {}          # path -> text
        self.comments = {}          # url -> [body, ...]
        self.states = {}            # url -> "OPEN"/"CLOSED"/"MERGED"
        self.comment_fail = False
        self.close_fail = False

    def run(self, argv, capture_output=True, text=True, **kw):
        CALLS.append(list(argv))
        if argv[:3] == ["gh", "api", "rate_limit"]:
            if self.rate_fail:
                return _fail()
            return _ok(json.dumps({"resources": {"core": {"remaining": self.rate_remaining}}}))
        if argv[:3] == ["gh", "pr", "list"]:
            label = argv[argv.index("--label") + 1]
            if label in self.listing_fail:
                return _fail()
            return _ok(json.dumps(self.listings.get(label, [])))
        if argv[:2] == ["gh", "api"] and argv[2].startswith(f"repos/{intake.REPO}/contents/"):
            path = argv[2][len(f"repos/{intake.REPO}/contents/"):]
            txt = self.contents.get(path)
            if txt is None:
                return _fail()
            return _ok(base64.b64encode(txt.encode()).decode())
        if argv[:3] == ["gh", "pr", "view"] and "comments" in argv:
            url = argv[3]
            return _ok(json.dumps({"comments": [{"body": b} for b in self.comments.get(url, [])]}))
        if argv[:3] == ["gh", "pr", "view"] and "state" in argv:
            url = argv[3]
            return _ok(json.dumps({"state": self.states.get(url, "OPEN")}))
        if argv[:3] == ["gh", "pr", "comment"]:
            if self.comment_fail:
                return _fail()
            url, body = argv[3], argv[argv.index("--body") + 1]
            self.comments.setdefault(url, []).append(body)
            return _ok("")
        if argv[:3] == ["gh", "pr", "close"]:
            if self.close_fail:
                return _fail()
            self.states[argv[3]] = "CLOSED"
            return _ok("")
        raise AssertionError(f"unexpected gh call: {argv}")


STUB = Stub()
intake.subprocess = STUB
# `fold.check_append` is not what `intake` calls (it uses `dispatch.emit`, gated
# by `fold.required_reason` alone) — nothing to stub here.

TRUST_TOML = TMP / "trusted_inbound_authors.toml"


def set_trusted(*rows):
    TRUST_TOML.write_text("".join(f'[[author]]\nlogin = "{l}"\nemail = "{e}"\n' for l, e in rows))


def clear_trusted():
    TRUST_TOML.unlink(missing_ok=True)


def pr(url, number, author, title="a title", body="a body", created="2026-09-01T00:00:00Z",
      files=(), head_sha="deadbeef"):
    return {"url": url, "number": number, "author": {"login": author}, "title": title,
            "body": body, "createdAt": created,
            "files": [{"path": f} for f in files], "headRefOid": head_sha}


def events_in(actor):
    p = TMP / "events" / f"L-{actor}-local.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.is_file() else []


def reset():
    """A fresh ledger + gh state between fixtures that must not see each
    other's history (the AC-scoped fixtures below), without tearing down the
    whole temp tree."""
    for p in (TMP / "events").glob("*.jsonl"):
        p.unlink()
    STUB.__init__()
    CALLS.clear()


# ══ AC1 · the rate guard: below the floor, zero listing calls, empty return ═══
reset()
STUB.rate_remaining = 199
out1 = intake.run([])
check(out1 == {"spec_prs": [], "note_prs": []}, f"AC1: empty return under the rate floor: {out1}")
check([c for c in CALLS if c[:3] == ["gh", "api", "rate_limit"]] and
     len([c for c in CALLS if c[:3] == ["gh", "api", "rate_limit"]]) == 1,
     f"AC1: exactly one rate-limit read: {CALLS}")
check(not [c for c in CALLS if c[:3] == ["gh", "pr", "list"]],
     f"AC1: zero listing calls under the floor: {CALLS}")

# ══ AC2 · a healthy rate: exactly two listing calls, canned rows assembled ═══
reset()
STUB.listings = {"inbound-spec": [pr("https://github.com/x/y/pull/1", 1, "someone")],
                "inbound-note": [pr("https://github.com/x/y/pull/2", 2, "someone-else")]}
out2 = intake.run([])
listing_calls2 = [c for c in CALLS if c[:3] == ["gh", "pr", "list"]]
check(len(listing_calls2) == 2, f"AC2: exactly two gh pr list calls, one per label: {listing_calls2}")
check(len(out2["spec_prs"]) == 1 and out2["spec_prs"][0]["url"] == "https://github.com/x/y/pull/1",
     f"AC2: spec_prs field-for-field: {out2['spec_prs']}")
check(len(out2["note_prs"]) == 1 and out2["note_prs"][0]["author_login"] == "someone-else",
     f"AC2: note_prs field-for-field: {out2['note_prs']}")

# ══ AC3 · a gh failure on ONE label empties only that label, never raises ════
reset()
STUB.listings = {"inbound-spec": [pr("https://github.com/x/y/pull/3", 3, "someone")]}
STUB.listing_fail = {"inbound-note"}
out3 = intake.run([])
check(out3["note_prs"] == [], f"AC3: the failing label comes back empty: {out3}")
check(len(out3["spec_prs"]) == 1, f"AC3: the OTHER label's data is unaffected: {out3}")

# ══ AC4 (R1) · a trusted author registers, even over a standing inbound-awaiting ══
reset()
set_trusted(("trusted-eg", "trusted@example.com"))
url4 = "https://github.com/fredhead88/albert-scott-platform/pull/4004"
STUB.listings = {"inbound-spec": [pr(url4, 4004, "trusted-eg", title="fixture 4004",
                                    body="the intent",
                                    files=("docs/do-it/inbound/spec.md", "README.md"))],
                "inbound-note": []}
STUB.contents["docs/do-it/inbound/spec.md"] = "# staged content\n"
intake.run([])
reg4 = [e for e in events_in("intake") if e["type"] == "inbound-registered"]
check(len(reg4) == 1, f"AC4: exactly one inbound-registered lands: {reg4}")
e4 = reg4[0]
check(e4["source"] == url4 and e4["subject"] == url4 and e4["project"] == "albert-scott"
     and e4["title"] == "fixture 4004" and e4["author_login"] == "trusted-eg" and e4["auto"] is True,
     f"AC4: the required fields: {e4}")
check("## docs/do-it/inbound/spec.md" in e4["body"] and "# staged content" in e4["body"]
     and "README.md" not in e4["body"], f"AC4: only the docs/do-it/inbound/-prefixed file rides: {e4['body']}")

# same url, now with a standing inbound-awaiting seeded FIRST — registration
# still lands (§5(a): a bare awaiting never blocks it).
reset()
set_trusted(("trusted-eg", "trusted@example.com"))
url4b = "https://github.com/fredhead88/albert-scott-platform/pull/4005"
p = TMP / "events" / "L-intake-local.jsonl"
p.write_text(json.dumps({"v": 1, "ts": "2026-09-01T00:00:00Z", "type": "inbound-awaiting",
                        "subject": url4b, "source": url4b, "project": "albert-scott",
                        "author_login": "trusted-eg", "title": "t", "opened_at": "2026-09-01T00:00:00Z"}) + "\n")
STUB.listings = {"inbound-spec": [pr(url4b, 4005, "trusted-eg")], "inbound-note": []}
intake.run([])
reg4b = [e for e in events_in("intake") if e["type"] == "inbound-registered"]
awaiting4b = [e for e in events_in("intake") if e["type"] == "inbound-awaiting"]
check(len(reg4b) == 1, f"AC4 (§5a): a standing inbound-awaiting never blocks registration: {reg4b}")
check(len(awaiting4b) == 1, f"AC4 (§5a): no duplicate inbound-awaiting is appended: {awaiting4b}")
clear_trusted()

# ══ AC6 (R2) · an untrusted author awaits, never registers; trust added later ═
reset()
url6 = "https://github.com/fredhead88/albert-scott-platform/pull/6006"
STUB.listings = {"inbound-spec": [pr(url6, 6006, "rando", title="untrusted title")], "inbound-note": []}
intake.run([])
await6 = [e for e in events_in("intake") if e["type"] == "inbound-awaiting"]
reg6 = [e for e in events_in("intake") if e["type"] == "inbound-registered"]
check(len(await6) == 1 and reg6 == [], f"AC6: exactly one inbound-awaiting, zero inbound-registered: {await6} {reg6}")
check(await6[0]["source"] == url6 and await6[0]["author_login"] == "rando"
     and await6[0]["title"] == "untrusted title", await6[0])
# the author is added to the trust list; the PR is still open on the next run —
# it registers, with no further inbound-awaiting.
set_trusted(("rando", "rando@example.com"))
intake.run([])
await6b = [e for e in events_in("intake") if e["type"] == "inbound-awaiting"]
reg6b = [e for e in events_in("intake") if e["type"] == "inbound-registered"]
check(len(reg6b) == 1, f"AC6: newly-trusted author registers on the next sighting: {reg6b}")
check(len(await6b) == 1, f"AC6: no new inbound-awaiting on the registering run: {await6b}")
clear_trusted()

# ══ AC7 (SD15) · the awaiting/gone/re-awaited cycle ══════════════════════════
reset()
url7 = "https://github.com/fredhead88/albert-scott-platform/pull/7007"
STUB.listings = {"inbound-spec": [pr(url7, 7007, "rando7")], "inbound-note": []}
intake.run([])                                    # tick 1: awaits
STUB.listings["inbound-spec"] = []                # tick 2: the PR is gone
intake.run([])
gone7 = [e for e in events_in("intake") if e["type"] == "inbound-awaiting-gone"]
check(len(gone7) == 1, f"AC7: exactly one inbound-awaiting-gone once it leaves the listing: {gone7}")
intake.run([])                                    # tick 3: still absent — no second "gone"
gone7b = [e for e in events_in("intake") if e["type"] == "inbound-awaiting-gone"]
check(len(gone7b) == 1, f"AC7: a third run with it still absent appends no second one: {gone7b}")
STUB.listings["inbound-spec"] = [pr(url7, 7007, "rando7")]   # tick 4: reappears, still untrusted
intake.run([])
await7 = [e for e in events_in("intake") if e["type"] == "inbound-awaiting"]
check(len(await7) == 2, f"AC7: a fresh inbound-awaiting once it reappears: {await7}")

# ══ AC9 (R5) · idempotent under a repeated identical listing, and unscoped by ═
# a DIFFERENT $DOIT_PROJECT between the two calls (§5's own dedup read).
for project_between in (None, "a-different-project"):
    reset()
    url9 = "https://github.com/fredhead88/albert-scott-platform/pull/9009"
    STUB.listings = {"inbound-spec": [pr(url9, 9009, "rando9")], "inbound-note": []}
    intake.run([])
    before9 = len(events_in("intake"))
    if project_between is None:
        os.environ.pop("DOIT_PROJECT", None)
    else:
        os.environ["DOIT_PROJECT"] = project_between
    intake.run([])
    after9 = len(events_in("intake"))
    os.environ.pop("DOIT_PROJECT", None)
    check(before9 == after9, f"AC9 ({project_between}): a rerun over the identical listing appends nothing further: {before9} {after9}")

# ══ AC10 · comment()/close() idempotency and forbidden flags ═════════════════
reset()
url10 = "https://github.com/fredhead88/albert-scott-platform/pull/10010"
n_before = len([c for c in CALLS if c[:3] == ["gh", "pr", "comment"]])
check(intake.comment(url10, "hello <!--marker-->", "<!--marker-->") is True, "AC10: comment() posts and returns True")
check(intake.comment(url10, "hello <!--marker-->", "<!--marker-->") is True, "AC10: a second call is still True")
n_after = len([c for c in CALLS if c[:3] == ["gh", "pr", "comment"]])
check(n_after - n_before == 1, f"AC10: exactly one posting call across two comment() calls: {n_after - n_before}")

close_calls_before = len([c for c in CALLS if c[:3] == ["gh", "pr", "close"]])
check(intake.close(url10) is True, "AC10: close() returns True")
close_argv = [c for c in CALLS if c[:3] == ["gh", "pr", "close"]][-1]
check("--delete-branch" not in close_argv and "--merge" not in close_argv,
     f"AC10: close()'s argv never carries --delete-branch/--merge: {close_argv}")
close_calls_mid = len([c for c in CALLS if c[:3] == ["gh", "pr", "close"]])
check(close_calls_mid - close_calls_before == 1, "AC10: exactly one gh pr close call on the first close()")
check(intake.close(url10) is True, "AC10: a second close() on an already-closed PR returns True")
close_calls_after = len([c for c in CALLS if c[:3] == ["gh", "pr", "close"]])
check(close_calls_after == close_calls_mid, f"AC10: no NEW gh pr close call on the second close(): {close_calls_after} {close_calls_mid}")

# ══ AC11/AC12 · every call is stubbed, and ingest_inbound_spec never appears ══
check(intake.subprocess is STUB, "AC11: intake.subprocess is the recording stub")
argv_dump = json.dumps(CALLS)
check("ingest_inbound_spec" not in argv_dump, f"AC12: no recorded argv names the v4 script: {argv_dump[:200]}")
check(NET[0] == 0, "AC11: no network call was ever attempted")

print(f"intake: {N} checks pass")
