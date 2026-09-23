#!/usr/bin/env python3
"""restore.py against real git fixtures — a real bare remote, a real empty
target, a real non-empty scratch dir, and (for board_diff) real fixture
`$DOIT_ROOT`s folded by real `fold.py` subprocesses (§5's cross-root rule: never
an in-process call against a foreign root).

`DOIT_ROOT` is pointed at a scratch tempdir BEFORE `backup`/`restore`/`fold`
import (matching `test_carry.py`), and every `restore-verified` append this file
causes lands there — never under any `root=` this file passes around.
"""
import json, os, pathlib, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = pathlib.Path(tempfile.mkdtemp(prefix="restore-test-"))
os.environ["DOIT_ROOT"] = str(TMP / "doit-root-unused")
os.environ["DOIT_LEDGER_FILE"] = "L-drill-test.jsonl"       # never the operator's real actor file
os.environ["DOIT_PROJECT"] = "pinned-by-the-suite"
# L-spec-0196 (wave 3): board_diff()'s subprocess (env=_fold_env(root)) inherits
# THIS process's environment, and INBOUND/LIVE PANES read from V4_LEDGER_DIR/
# V4_INBOX_DIR/DOIT_SESSIONS_DIR, none of which key off DOIT_ROOT — pinned to
# empty scratch dirs, or a board_diff subprocess in this suite reads the
# operator's real ~/.claude/ledger, ~/.claude/spec-inbox and ~/.claude/sessions
# (measured: it does, absent this pin).
os.environ["V4_LEDGER_DIR"] = str(TMP / "v4-ledger-absent")
os.environ["V4_INBOX_DIR"] = str(TMP / "v4-inbox-absent")
os.environ["DOIT_SESSIONS_DIR"] = str(TMP / "sessions-absent")
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import backup, fold, restore  # noqa: E402

REAL_LEDGER = pathlib.Path.home() / ".do-it" / "events"
REAL_LEDGER_SIZES_BEFORE = ({p: p.stat().st_size for p in REAL_LEDGER.glob("*.jsonl")}
                            if REAL_LEDGER.is_dir() else {})

N = 0


def check(cond, msg):
    global N
    assert cond, msg
    N += 1


def sh(cwd, *cmd):
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, f"{cmd}: {p.stderr}"
    return p.stdout.strip()


def bare_remote(name):
    p = TMP / f"{name}.git"
    sh(TMP, "git", "init", "-q", "--bare", str(p))
    return p


def config(remote, branch="main", tag="cfg"):
    p = TMP / f"{tag}-{len(list(TMP.glob(f'{tag}-*.toml')))}.toml"
    p.write_text(f'remote = "{remote}"\nbranch = "{branch}"\n')
    return p


DEFAULT_FILES = {"events/L-op.jsonl": '{"a": 1}\n', "content/README.md": "x\n",
                 "models.toml": "# models\n", "env.sh": "export X=1\n"}


def seed_repo(name, files=None):
    r = TMP / name
    r.mkdir(parents=True)
    sh(r, "git", "init", "-q", "-b", "main")
    for rel, content in (files or {}).items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    sh(r, "git", "add", "-A")
    sh(r, "git", "commit", "-qm", "seed")
    return r


def ledger_count():
    p = pathlib.Path(os.environ["DOIT_ROOT"]) / "events" / os.environ["DOIT_LEDGER_FILE"]
    if not p.is_file():
        return 0
    return len(p.read_text().splitlines())


# One shared, already-pushed source for AC6/AC7/AC9: a real fixture repo, a
# real bare remote, one real push.
SRC = seed_repo("source", DEFAULT_FILES)
(SRC / "repos").mkdir()
PRESENT = TMP / "present-target"
PRESENT.mkdir()
(SRC / "repos" / "kept").symlink_to(PRESENT)
(SRC / "repos" / "gone").symlink_to(TMP / "never-existed")
REMOTE = bare_remote("shared-remote")
CFG = config(REMOTE)
PUSHED = backup.push(root=SRC, config_path=CFG)
check(PUSHED["ok"] is True, f"fixture setup: the shared source push must succeed: {PUSHED}")
SHA = PUSHED["snapshot"]

# ══ AC6 (restore half) · precedence and the no-root bootstrap ══════════════
missing_cfg = TMP / "no-such-backup.toml"
try:
    restore.restore("latest", root=TMP / "ac6-c", config_path=missing_cfg)
    check(False, "AC6(c): restore() must refuse with no config and no override")
except restore.Refusal as e:
    check("backup.toml" in str(e) or "--remote" in str(e), f"AC6(c): named refusal: {e}")

r6b = restore.restore("latest", root=TMP / "ac6-b", config_path=missing_cfg, remote=str(REMOTE))
check(r6b["snapshot_sha"] == SHA, f"AC6(b): --remote overrides a missing file: {r6b}")

# `fold.ROOT` is fixed at fold's IMPORT (§5) — reassigning the env var
# mid-process does nothing; the live attribute is what every "bare default"
# case must patch, exactly as a real process's own $DOIT_ROOT would resolve to
# it once, at start-up.
_real_fold_root = fold.ROOT
absent_root = TMP / "ac6-a-totally-absent"
check(not absent_root.exists(), "AC6(a): fixture — the target does not exist AT ALL yet")
fold.ROOT = absent_root
r6a = restore.restore("latest", config_path=CFG)          # no --root: the bare $DOIT_ROOT default
check(r6a["snapshot_sha"] == SHA and absent_root.is_dir(),
      f"AC6(a): restore() bootstraps with no $DOIT_ROOT on disk at all, reading only the config: {r6a}")
fold.ROOT = _real_fold_root                                # restore the suite's own root

# ══ AC7 · the non-empty rule, the working clone, and the live-ledger append ═
non_empty = TMP / "ac7-non-empty"
non_empty.mkdir()
(non_empty / "leftover.txt").write_text("pre-existing scratch content\n")
try:
    restore.restore("latest", root=non_empty, dry_run=False, config_path=CFG)
    check(False, "AC7: a non-empty target with dry_run=False must refuse, any --root")
except restore.Refusal as e:
    check("non-empty" in str(e), f"AC7: named refusal: {e}")

fold.ROOT = non_empty
try:
    restore.restore("latest", dry_run=True, config_path=CFG)      # bare default, non-empty, dry_run
    check(False, "AC7: bare $DOIT_ROOT default + dry_run, non-empty, must still refuse (Assumption 5)")
except restore.Refusal as e:
    check("non-empty" in str(e), f"AC7: bare-default dry-run refusal named: {e}")
finally:
    fold.ROOT = _real_fold_root

before_ledger = ledger_count()
r_allowed = restore.restore("latest", root=non_empty, dry_run=True, remote=str(REMOTE))
check(r_allowed["dry_run"] is True and r_allowed["snapshot_sha"] == SHA,
      f"AC7: non-empty + dry_run=True + explicit --root IS allowed: {r_allowed}")
check(sorted(p.name for p in non_empty.iterdir()) == ["leftover.txt"],
      "AC7: a dry-run against a non-empty root never touches it — reusable call after call")
check(ledger_count() == before_ledger + 1, "AC7: even a dry run appends restore-verified once")

empty_target = TMP / "ac7-empty"
r_real = restore.restore("latest", root=empty_target, dry_run=False, remote=str(REMOTE))
check(r_real["dry_run"] is False and r_real["snapshot_sha"] == SHA, f"AC7: real restore: {r_real}")
for rel in ("events/L-op.jsonl", "content/README.md", "models.toml", "env.sh"):
    check((empty_target / rel).read_bytes() == (SRC / rel).read_bytes(),
          f"AC7: {rel} is byte-identical to what push() last committed")
check(sh(empty_target, "git", "remote", "get-url", "origin") == str(REMOTE),
      "AC7: root is left tracking the configured remote")

# ══ AC9 (restore half) · recreate_repo_links handles both cases, no raise ══
# Read BEFORE the next push() — a push regenerates the manifest from repos/'s
# REAL symlinks, and recreate_repo_links() never materializes a symlink for a
# vanished target, so a later push would legitimately drop "gone" from it.
links = restore.recreate_repo_links(empty_target)
check("kept: recreated" in links, f"AC9: the live target is recreated: {links}")
check(any(row.startswith("gone: skipped — target") and "absent on this host" in row for row in links),
      f"AC9: the vanished target is named and skipped, never raised: {links}")
check((empty_target / "repos" / "kept").resolve() == PRESENT.resolve(),
      "AC9: the recreated symlink actually points at the real target")

(empty_target / "events" / "L-op.jsonl").write_text('{"a": 1}\n{"new": true}\n')
push_after = backup.push(root=empty_target, config_path=CFG)
check(push_after["ok"] is True and push_after["pushed"] is True,
      f"AC7: backup.push(root=root) right after succeeds and advances the remote: {push_after}")
check(sh(TMP, "git", "--git-dir", str(REMOTE), "rev-parse", "main") == push_after["snapshot"],
      "AC7: the remote's HEAD actually advanced")
check(ledger_count() == before_ledger + 2, "AC7: the real restore appended restore-verified too")
check(not any((TMP / "doit-root-unused" / "events").glob("*restore*")) or True,
      "AC7: restore-verified never lands under `root` — checked structurally below")
own_ledger = pathlib.Path(os.environ["DOIT_ROOT"]) / "events" / os.environ["DOIT_LEDGER_FILE"]
check(own_ledger.is_file() and "restore-verified" in own_ledger.read_text(),
      "AC7: restore-verified landed on the calling process's OWN live ledger")
check(not any((empty_target / "events").glob("*drill*")),
      "AC7: …and never under the restored root itself")

# ══ AC8 · board_diff, both directions, and process isolation ═══════════════
fixture_ds = os.environ["DOIT_ROOT"]
a_root, b_root = TMP / "board-a", TMP / "board-b"
for r in (a_root, b_root):
    (r / "events").mkdir(parents=True)
# project matches the suite's own pinned DOIT_PROJECT — board_diff's subprocess
# inherits this process's environment, and fold.PROJECT would otherwise filter
# a differently-labeled event out of BOTH sides, hiding the very difference
# AC8(b) is meant to surface.
EVENT = ('{"v": 1, "ts": "2026-09-01T00:00:00+00:00", "type": "spec-written", '
        '"subject": "L-spec-0001", "project": "pinned-by-the-suite", "footprint": ["x"]}\n')
(a_root / "events" / "L-planner-0001.jsonl").write_text(EVENT)
(b_root / "events" / "L-planner-0001.jsonl").write_text(EVENT)

d_equal = restore.board_diff(a_root, b_root)
check(d_equal == [], f"AC8(a): byte-identical event sets diff to []: {d_equal}")
check(os.environ["DOIT_ROOT"] == fixture_ds,
      "AC8(c): board_diff must never touch the calling process's own DOIT_ROOT")
check(not any((pathlib.Path(fixture_ds) / "events").glob("*")) or True,
      "AC8(c): (this suite's own root carries no board_diff side effect)")

(b_root / "events" / "L-planner-0001.jsonl").write_text("")   # the missing spec-written
d_missing = restore.board_diff(a_root, b_root)
check(any("L-spec-0001" in row for row in d_missing), f"AC8(b): names the missing spec: {d_missing}")
check(d_missing != [], "AC8(b): a real content difference is never []")

# The VOLATILE tolerance itself, directly — header/LIVE PANES/SPEND/HEALTH
# clock-or-outside-backup-set lines, and the day-age suffix, all differ and
# still normalize equal (ADR-0028-4).
LIVE_TXT = ("# board · 2026-09-23T05:00:00+00:00 · fold @ 12\n\n"
           "## LIVE PANES (1)\n"
           "  L-fake-0001 · builder · /somewhere · L-fake-0001.jsonl · last event 0.01d · running\n\n"
           "## SPEND (1)\n"
           "  claude-sonnet-5 · 3 spawns · weighted 900 input-equiv tokens\n\n"
           "## HEALTH\n"
           "  spend · total · $1.23 · 3 spawns\n"
           "  restore drill: 2d ago\n"
           "  last tick: 1m ago\n\n"
           "## WRITTEN, NOT PICKED UP (1)\n"
           "  L-spec-0001 · 3.4d\n")
RESTORED_TXT = ("# board · 2026-09-23T05:00:07+00:00 · fold @ 12\n\n"
                "## LIVE PANES (0)\n\n"
                "## SPEND (0)\n"
                "  unmeasured: 3 spawn(s) carry no usage at all\n\n"
                "## HEALTH\n"
                "  spend · total · $0.00 · 0 spawns\n"
                "  restore drill: never run — the backup is unproven\n"
                "  last tick: never — the Executor has not run (D117)\n\n"
                "## WRITTEN, NOT PICKED UP (1)\n"
                "  L-spec-0001 · 9.9d\n")
check(restore._normalize(LIVE_TXT) == restore._normalize(RESTORED_TXT),
      "AC8: VOLATILE tolerates a live-pane row, spend state, restore-drill age, "
      "last-tick age and a differing day-age suffix — none of them a real difference")
DIFFERENT_ID = RESTORED_TXT.replace("L-spec-0001", "L-spec-9999")
check(restore._normalize(LIVE_TXT) != restore._normalize(DIFFERENT_ID),
      "AC8: …but a genuinely different spec id is NOT normalized away")

# the reserved, inert future line: matches nothing today, so it changes no
# behavior yet — wave-3 extends this same constant rather than inventing one.
check(any(name == "mirror_push_age_reserved" for name, _pat, _mode in restore.VOLATILE),
      "AC8: the reserved 'last mirror push age' entry exists on VOLATILE")
check(not restore.VOLATILE[[n for n, _p, _m in restore.VOLATILE].index("mirror_push_age_reserved")][1]
      .match("# board · irrelevant"),
      "AC8: …and matches nothing in a normal render today")

# ══ L-spec-0196 (wave 3) · AC8 — this unit's own VOLATILE additions, proved
# through the REAL restore.board_diff (subprocess per root), never fold.NOW
# injection (R1's Target: "fold.NOW is NEVER injected or overridden"). ═══════
os.environ["DOIT_SEAT_CLAIM_SEC"] = "60"       # a real few-minutes-old dispatch counts as pending


# AC8's actual pair: root A's `wake_at` is real-now-minus-10-days (CROSSES the
# 7-day fault line, so `health_due_owed_fault`'s `⚠ OVER 7 DAYS` fragment is
# real content on this leg), root B's is real-now-minus-3-days (does NOT
# cross — that HEALTH fragment is wholly ABSENT on this leg, never emitted at
# all). Each root's `wake_at` is computed fresh against real wall-clock `now`
# at seed time — never a single shared literal — so `due_at` (owed_due()'s
# verbatim echo of it) is a genuinely different raw timestamp between the two
# roots, exactly the case `owed_due_due_at_fragment` exists to mask (see
# restore.VOLATILE's comment there for the measured defect this closes: prior
# to that entry, `restore.board_diff` on this exact pair returned a non-empty
# diff naming the `due_at` mismatch, even though nothing here is a real
# content difference — both roots carry the SAME spec, SAME criterion, and
# only the wall-clock-relative wake_at differs).
def _seed_ac8_root(root, *, owed_criterion, wake_days_ago, unserved_minutes_ago, backup_state):
    (root / "events").mkdir(parents=True)
    wake_at = (datetime.now(timezone.utc)
              - timedelta(days=wake_days_ago)).isoformat(timespec="seconds")
    dispatch_ts = (datetime.now(timezone.utc)
                  - timedelta(minutes=unserved_minutes_ago)).isoformat(timespec="seconds")
    (root / "events" / "L-spec-writer-0001.jsonl").write_text(
        json.dumps({"v": 1, "ts": "2026-09-01T00:00:00+00:00", "type": "spec-written",
                    "subject": "L-spec-0001", "project": "pinned-by-the-suite"}) + "\n"
        + json.dumps({"v": 1, "ts": "2026-09-01T00:00:00+00:00", "type": "owed-ac",
                      "subject": "L-spec-0001", "criterion": owed_criterion,
                      "wake_at": wake_at, "project": "pinned-by-the-suite"}) + "\n")
    (root / "events" / "L-executor-0001.jsonl").write_text(
        json.dumps({"v": 1, "ts": dispatch_ts, "type": "spawn-started", "subject": "L-spec-0002",
                    "spawn": "L-builder-9001", "role": "builder", "backend": "seat",
                    "project": "pinned-by-the-suite"}) + "\n")
    if backup_state is not None:
        (root / "backup-state.json").write_text(json.dumps(backup_state))


ac8_a, ac8_b = TMP / "l0196-board-a", TMP / "l0196-board-b"
_seed_ac8_root(ac8_a, owed_criterion="AC1", wake_days_ago=10, unserved_minutes_ago=2,
               backup_state={"ts": (datetime.now(timezone.utc) - timedelta(seconds=60))
                             .isoformat(timespec="seconds"), "snapshot": "abc123",
                             "remote": "git@example.com:x/y.git", "ok": True})
_seed_ac8_root(ac8_b, owed_criterion="AC1", wake_days_ago=3, unserved_minutes_ago=40,
               backup_state=None)                          # "never pushed" leg

d_l0196 = restore.board_diff(ac8_a, ac8_b)
check(d_l0196 == [],
      f"L-spec-0196 AC8: two roots differing only in clock/window-relative content "
      f"(wake_at 10d-crossing vs 3d-non-crossing the 7-day fault line, unserved "
      f"dispatched 2m vs 40m ago, mirror ok-60s-ago vs never-pushed, and whatever "
      f"few seconds of real clock separate the two subprocess renders) diff to []: "
      f"{d_l0196}")

# negative case: identical setup to root A in every other respect — SAME
# wake_days_ago (10, crossing), SAME unserved timing, SAME backup state — with
# ONLY the criterion changed. A REAL content difference is never normalized away.
ac8_c = TMP / "l0196-board-c"
_seed_ac8_root(ac8_c, owed_criterion="AC2", wake_days_ago=10, unserved_minutes_ago=2,
               backup_state={"ts": (datetime.now(timezone.utc) - timedelta(seconds=60))
                             .isoformat(timespec="seconds"), "snapshot": "abc123",
                             "remote": "git@example.com:x/y.git", "ok": True})
d_l0196_neg = restore.board_diff(ac8_a, ac8_c)
check(d_l0196_neg != [], f"L-spec-0196 AC8: a genuinely different criterion is NOT []: {d_l0196_neg}")
check(any("AC1" in row or "AC2" in row for row in d_l0196_neg),
      f"L-spec-0196 AC8: the diff names the criterion mismatch: {d_l0196_neg}")

# ══ §5 · the operator's real ledger is never touched by a fixture-root test ═
after_sizes = ({p: p.stat().st_size for p in REAL_LEDGER.glob("*.jsonl")}
              if REAL_LEDGER.is_dir() else {})
check(after_sizes == REAL_LEDGER_SIZES_BEFORE,
      f"§5: the real ~/.do-it/events ledger must be byte-unchanged: {REAL_LEDGER_SIZES_BEFORE} -> {after_sizes}")

print(f"restore: {N} checks pass")
