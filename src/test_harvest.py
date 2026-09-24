#!/usr/bin/env python3
"""harvest checks. Run: python3 test_harvest.py

★ AC9 deviation (minor, declared on the output card): the spec's own review_path
block hardcodes `cd /home/albert/.do-it/repos/do-it-v2` — a fixed, shared
checkout, not this file's own worktree. Hardcoding that path HERE would make
AC9 depend on `harvest.py` already being merged into that other checkout,
which is exactly backwards for a builder proving its OWN new module against
the real, live ledger before merge. This resolves the review script's cwd to
this file's own repo root (`pathlib.Path(__file__).resolve().parent.parent`)
instead — the identical `python3 -c "..."` script, run the identical way
(`sys.path.insert(0, 'src')` relative to that cwd), so it exercises whichever
checkout `test_harvest.py` actually lives in: this worktree today, the shared
checkout once this merges. `env={**os.environ, "DOIT_ROOT": ...}` and the
script text are otherwise byte-for-byte the spec's own.
"""
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import harvest  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + name)


def slugs(events, problems=(), toml_path=None):
    return [o["slug"] for o in harvest.occurrences(events, list(problems), toml_path)]


# ── AC1 — spawn-failed reason table, input order ────────────────────────────
events = [
    {"type": "spawn-failed", "reason": "unserved"},
    {"type": "spawn-failed", "reason": "spec-shape"},
]
check("AC1: reason=unserved -> seat-unserved, reason=spec-shape -> spec-shape-invalid, in order",
      slugs(events, []) == ["seat-unserved", "spec-shape-invalid"])
print("AC1 ok")

# ── AC2 — spawn-failed why-pattern table, first match, in order ─────────────
events = [
    {"type": "spawn-failed", "why": "timeout after 30 min"},
    {"type": "spawn-failed", "why": "is_error: boom"},
    {"type": "spawn-failed", "why": "nothing to see here"},
]
check("AC2: why-pattern table classifies in order, unmatched -> unclassified",
      slugs(events, []) == ["role-timeout", "spawn-api-error", "unclassified-spawn-failed"])
print("AC2 ok")

# ── AC3 — token wins outright over a high-similarity decoy ──────────────────
events = [{"type": "build-deviation", "what": "problem:seat-unserved — dispatch waited again"}]
problems = [{"slug": "totally-different", "statement": "dispatch waited again"}]
check("AC3: a well-formed problem: token wins outright, even against a decoy join",
      slugs(events, problems) == ["seat-unserved"])
print("AC3 ok")

# ── AC4 — no token, high similarity -> join, not mint ────────────────────────
events = [{"type": "build-deviation", "what": "the retry loop double-charged the batch API"}]
problems = [{"slug": "batch-double-charge",
             "statement": "the retry loop double-charged the batch API on failure"}]
check("AC4: similarity >= 0.6 joins the existing problem",
      slugs(events, problems) == ["batch-double-charge"])
print("AC4 ok")

# ── AC5 — no token, low similarity -> mint, hyphen kept as a word boundary ──
events = [{"type": "build-deviation", "what": "the retry loop double-charged the batch API"}]
problems = [{"slug": "unrelated",
             "statement": "completely unconnected text about a different subsystem entirely"}]
check("AC5: similarity < 0.6 mints, and 'double-charged' stays two words, not fused",
      slugs(events, problems) == ["build-deviation-the-retry-loop-double-charged"])
print("AC5 ok")

# ── AC6 — empty free text mints nothing; unclassified, not dropped ──────────
events = [
    {"type": "rejected-criterion", "criterion": "AC1", "why": ""},
    {"type": "correction"},
]
check("AC6: empty/absent free text -> unclassified-<type>, never a mint of nothing",
      slugs(events, []) == ["unclassified-rejected-criterion", "unclassified-correction"])
print("AC6 ok")

# ── AC7 — cost_min / cost_tokens extraction ──────────────────────────────────
events = [
    {"type": "spawn-failed", "duration_ms": 90000, "input_tokens": 120, "output_tokens": 340},
    {"type": "spawn-failed", "input_tokens": 50, "subagent_tokens": 200},
    {"type": "spawn-failed", "usage": {"total_tokens": 75}},
    {"type": "spawn-failed"},
]
rows = harvest.occurrences(events, [])
check("AC7a: duration_ms present -> cost_min, top-level tokens summed",
      rows[0]["cost_min"] == 1.5 and rows[0]["cost_tokens"] == 460)
check("AC7b: no duration_ms, absent token fields count as 0 once one is present",
      rows[1]["cost_min"] is None and rows[1]["cost_tokens"] == 250)
check("AC7c: no top-level token field, usage fallback sums numeric leaves",
      rows[2]["cost_min"] is None and rows[2]["cost_tokens"] == 75)
check("AC7d: nothing measured -> both None, never 0",
      rows[3]["cost_min"] is None and rows[3]["cost_tokens"] is None)
print("AC7 ok")

# ── AC8 — a mixed batch, every classification path, paired to a pasted expectation ──
problems8 = [
    {"slug": "queue-stall", "statement": "the queue stalled twice before retrying successfully"},
    {"slug": "criterion-flake", "statement": "the flaky criterion rejected valid output again"},
    {"slug": "correction-typo", "statement": "an operator correction fixed a typo in the ledger"},
]
events8 = [
    # 3 spawn-failed: reason hit, why-pattern hit, unclassified
    {"type": "spawn-failed", "reason": "unserved"},
    {"type": "spawn-failed", "why": "contamination detected in output"},
    {"type": "spawn-failed", "why": "nothing special happened"},
    # 3 build-deviation: token, join, mint
    {"type": "build-deviation", "what": "problem:custom-slug-one — details here"},
    {"type": "build-deviation", "what": "the queue stalled twice before retrying"},
    {"type": "build-deviation", "what": "a wholly new failure mode nobody has seen"},
    # 3 rejected-criterion: token, join, unclassified-empty
    {"type": "rejected-criterion", "why": "problem:custom-slug-two — the reviewer rejected AC4 again"},
    {"type": "rejected-criterion", "why": "the flaky criterion rejected valid output again and again"},
    {"type": "rejected-criterion", "why": ""},
    # 3 correction: token, join, mint
    {"type": "correction", "why": "problem:custom-slug-three — the operator corrected the actor field"},
    {"type": "correction", "why": "an operator correction fixed a typo in the ledger entry"},
    {"type": "correction", "why": "a brand new kind of misattributed event entirely"},
]
# Pasted, not summarized — computed once against the fixed step-5 rule and
# checked into this file as the expectation, exactly as the spec requires.
expected8 = [
    "seat-unserved",
    "spawn-contamination",
    "unclassified-spawn-failed",
    "custom-slug-one",
    "queue-stall",
    "build-deviation-a-wholly-new-failure-mode",
    "custom-slug-two",
    "criterion-flake",
    "unclassified-rejected-criterion",
    "custom-slug-three",
    "correction-typo",
    "correction-a-brand-new-kind-of",
]
out8 = harvest.occurrences(events8, problems8)
check("AC8: 12 in, 12 out, input order, each row matches its paired expectation",
      len(out8) == 12 and [o["slug"] for o in out8] == expected8)
print("AC8 ok")

# ── AC9 — read-only, real-ledger, own-subprocess check ──────────────────────
ac9_script = """
import sys, re
sys.path.insert(0, 'src')
import fold, harvest
events = fold.read_events()
harvested = [e for e in events if e['type'] in ('spawn-failed','build-deviation','rejected-criterion','correction')]
assert len(harvested) >= 480, f'floor breached: {len(harvested)} < 480'
out = harvest.occurrences(harvested, [])
assert len(out) == len(harvested), f'{len(out)} != {len(harvested)}'
assert all(re.match(r'^[a-z0-9-]{3,60}$', o['slug']) for o in out)
unclassified = sum(1 for o in out if o['slug'].startswith('unclassified-'))
print(f'AC9 ok count={len(out)} unclassified={unclassified}')
"""
ac9 = subprocess.run(
    ["python3", "-c", ac9_script],
    cwd=str(REPO_ROOT),
    capture_output=True, text=True,
    env={**os.environ, "DOIT_ROOT": os.path.expanduser("~/.do-it")},
)
print(ac9.stdout, end="")
if ac9.returncode != 0:
    print(ac9.stderr, end="")
check("AC9: subprocess against the real ledger exits 0 and prints its own marker",
      ac9.returncode == 0 and "AC9 ok" in ac9.stdout)

# ── AC10 — only the four named types are ever harvested ────────────────────
events = [
    {"type": "lesson", "statement": "not harvested"},
    {"type": "verdict"},
    {"type": "spawn-failed", "reason": "unserved"},
]
check("AC10: lesson/verdict pass through untouched; exactly the spawn-failed row comes back",
      slugs(events, []) == ["seat-unserved"])
print("AC10 ok")

# ── AC11 — toml_path wholesale replacement, and the no-file default branch ──
import tempfile
tmpdir = pathlib.Path(tempfile.mkdtemp())
toml_file = tmpdir / "problems.toml"
toml_file.write_text(
    '[harvest.spawn_failed_reason]\n'
    'unserved = "custom-unserved-slug"\n\n'
    '[[harvest.spawn_failed_why_pattern]]\n'
    'pattern = "banana"\n'
    'slug = "fruit-failure"\n'
)
events = [
    {"type": "spawn-failed", "reason": "unserved"},
    {"type": "spawn-failed", "reason": "spec-shape"},
    {"type": "spawn-failed", "why": "banana bread failed"},
]
check("AC11a: a custom table replaces wholesale — spec-shape falls to why-pattern "
      "(no match here, so unclassified) and NEVER back to the built-in "
      "spec-shape-invalid default; a separate why=banana event hits the custom pattern",
      slugs(events, [], str(toml_file)) ==
      ["custom-unserved-slug", "unclassified-spawn-failed", "fruit-failure"])

_orig_default = harvest.DEFAULT_TOML_PATH
harvest.DEFAULT_TOML_PATH = tmpdir / "no-such-problems.toml"
try:
    check("AC11b: toml_path=None with a monkeypatched absent DEFAULT_TOML_PATH -> "
          "built-in defaults, no exception",
          slugs([{"type": "spawn-failed", "reason": "unserved"}], [], None) == ["seat-unserved"])
finally:
    harvest.DEFAULT_TOML_PATH = _orig_default
print("AC11 ok")

# ── AC12 — a malformed token (no whitespace around the separator) never matches ──
events = [{"type": "build-deviation", "what": "problem:foo-bar baz interesting text"}]
check("AC12: a malformed token falls through to mint over the WHOLE raw text",
      slugs(events, []) == ["build-deviation-problem-foo-bar-baz-interesting"])
print("AC12 ok")

print(f"harvest: {sum(ok)}/{len(ok)} checks pass")
sys.exit(0 if all(ok) else 1)
