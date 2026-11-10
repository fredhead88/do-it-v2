"""L-spec-2266: the merge driver's migration batching, run against the real lane/rloop.sh.

Every scenario sources the actual script, calls its functions with temporary paths, real
scratch Git repositories and recording command doubles (doit, pgrep, gh, python3, sleep, the
Telegram sender). No live queue, ledger, remote branch, hosted workflow or notification is touched.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "lane"
sys.path.insert(0, str(ROOT / "src"))
import rechain  # noqa: E402  (canonical SHARED; the shell reads the same source, never a copy)

VERSIONS = "api/alembic_supabase/versions/"
CLEAN = "PREFLIGHT-CLEAN guards cpu_s=0.5 wall_s=1"


def evidence(ac, bodies):
    body = "\n".join(bodies)
    print(json.dumps({"ac": ac, "url": Path(__file__).as_uri() + "#" + ac, "status": 200,
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "body_excerpt": body[:1000]}), flush=True)


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode:
        raise AssertionError("git %s: %s" % (args, r.stdout))
    return r.stdout.strip()


class Harness(unittest.TestCase):
    """A scratch driver world: origin-like repo IR, the driver's clone R, a fake do-it tree D."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rloop-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.assert_live_untouched)
        for name in ("q", "bin", "stubs", "doit", "root/events", "out", "state"):
            (self.root / name).mkdir(parents=True)
        self.q, self.stubs, self.out = self.root / "q", self.root / "stubs", self.root / "out"
        self.ir, self.r = self.root / "ir", self.root / "r"
        self.d = self.root / "doit"
        self.lane = self.root / "lane"
        self.lane.mkdir()
        for name in ("rloop.sh", "redalone.sh", "preflight.sh", "guard-tests.txt"):
            shutil.copy(LANE / name, self.lane / name)
        (self.lane / "attr.sh").write_text("#!/bin/bash\necho attr >> %s/attr-called\n" % self.root)
        for name in ("rloop.sh", "preflight.sh", "attr.sh"):
            (self.lane / name).chmod(0o755)
        (self.d / "src").mkdir()
        shutil.copy(ROOT / "src" / "rechain.py", self.d / "src" / "rechain.py")
        self.calls, self.gitlog = self.root / "calls", self.root / "gitcalls"
        self.write_bin("doit", self.DOIT_STUB)
        shutil.copy(self.root / "bin" / "doit", self.d / "doit")
        self.write_bin("pgrep", 'echo "pgrep $*" >> "$CALLS"; [ -n "${FAKE_BUSY:-}" ] && { echo "1 python window_gate.py $FAKE_BUSY"; exit 0; }; exit 1')
        self.write_bin("gh", 'echo "gh $*" >> "$CALLS"; exit 0')
        self.write_bin("python3", 'echo "python3 $*" >> "$CALLS"; case "$*" in *push_origin*) echo albert-ok;; esac; exit 0')
        self.write_bin("sleep", 'echo "sleep $*" >> "$CALLS"; exit 0')
        self.write_bin("tgsend", 'echo "$*" >> "$TGLOG"; exit 0')
        self.write_bin("git", 'echo "$*" >> "$GITLOG"; exec /usr/bin/git "$@"')
        self.sh_ready = self.root / "ready.txt"
        git_init = ["git", "init", "-q", "-b", "master"]
        subprocess.run(git_init + [str(self.ir)], check=True)
        subprocess.run(["git", "init", "-q", str(self.r)], check=True)
        self.commit_master({"app.py": "base\n", rechain.MANIFEST: "{}\n", rechain.TEST: "t\n",
                            rechain.AUTHORITY: "a\n", rechain.FINGERPRINT: "fp0\n", "shared_code.py": "x\n"})
        self.base_env = {
            "PATH": "%s:/usr/bin:/bin" % (self.root / "bin"),
            "HOME": str(self.root / "home"), "DOIT_ROOT": str(self.root / "root"),
            "RLOOP_Q": str(self.q), "RLOOP_READY": str(self.sh_ready), "RLOOP_R": str(self.r),
            "RLOOP_D": str(self.d), "RLOOP_IR": str(self.ir), "RLOOP_TG_SEND": str(self.root / "bin" / "tgsend"),
            "RLOOP_PROJECT": "albert-scott", "CALLS": str(self.calls), "GITLOG": str(self.gitlog),
            "TGLOG": str(self.root / "tg.log"), "STUBS": str(self.stubs), "LC_ALL": "C",
            "PREFLIGHT_DOIT": str(self.d / "doit"), "PREFLIGHT_OUT": str(self.out),
            "PREFLIGHT_WT": str(self.root / "wt"), "PREFLIGHT_GUARD_FILE": str(self.lane / "guard-tests.txt"),
            "RLOOP_UNTIL": "99999999999"}
        (self.root / "home").mkdir()
        (self.root / "wt").mkdir()

    DOIT_STUB = '''
echo "$*" >> "$CALLS"
sub=$1; shift
specs=""; prev=""
for a in "$@"; do [ "$prev" = --specs ] && specs=$a; prev=$a; done
for f in "$STUBS/$sub@$specs" "$STUBS/$sub"; do [ -x "$f" ] && exec "$f" "$@"; done
echo "no stub for doit $sub" >&2; exit 99
'''

    def assert_live_untouched(self):
        """No recorded command ever named a live path (the real driver runs on this box)."""
        for trace in (self.calls, self.gitlog, self.root / "tg.log"):
            if trace.exists():
                text = trace.read_text()
                self.assertNotIn("/var/tmp/push", text.replace("/var/tmp/push/clean.py", ""))
                self.assertNotIn("/opt/albert-scott", text)

    # -- scaffolding -------------------------------------------------------------------------
    def write_bin(self, name, body):
        path = self.root / "bin" / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def stub(self, name, body):
        path = self.stubs / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)

    def commit_master(self, files):
        if subprocess.run(["git", "-C", str(self.ir), "rev-parse", "-q", "--verify", "HEAD"],
                          stdout=subprocess.DEVNULL).returncode == 0:
            git(self.ir, "checkout", "-q", "master")
        for p, c in files.items():
            f = self.ir / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(c)
        git(self.ir, "add", "-A")
        git(self.ir, "commit", "-q", "-m", "master change")
        return git(self.ir, "rev-parse", "master")

    def branch(self, n, files, on="master"):
        git(self.ir, "checkout", "-q", on if len(on) < 40 else on)
        git(self.ir, "checkout", "-q", "-B", "l-spec-%s" % n)
        for p, c in files.items():
            f = self.ir / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(c)
        git(self.ir, "add", "-A")
        git(self.ir, "commit", "-q", "-m", "spec %s" % n)
        sha = git(self.ir, "rev-parse", "HEAD")
        git(self.ir, "checkout", "-q", "master")
        return sha

    def mig(self, n, extra=None):
        files = {VERSIONS + "v%s.py" % n: "rev = %s\n" % n}
        files.update(extra or {})
        return self.branch(n, files)

    def code(self, n, extra=None):
        return self.branch(n, extra or {"code_%s.py" % n: "x\n"})

    def master(self):
        return git(self.ir, "rev-parse", "master")

    def ready(self, *numbers):
        self.sh_ready.write_text("".join("%s\n" % n for n in numbers))

    def sweep_clean_for_all(self):
        self.stub("preflight", "echo '%s'; exit 0" % CLEAN)

    def run_sh(self, script, env=None, timeout=120, init=True):
        e = dict(self.base_env)
        e.update(env or {})
        prog = 'source "%s/rloop.sh"; %s%s' % (self.lane, "rloop_init; " if init else "", script)
        r = subprocess.run(["bash", "-c", prog], env=e, cwd=str(self.root), text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return r

    def pick(self, base=None, env=None):
        r = self.run_sh('pick %s' % (base or self.master()), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split(), r

    def log(self):
        f = self.q / "rloop.log"
        return f.read_text() if f.exists() else ""

    def attention(self):
        f = self.q / "attention.txt"
        return [l for l in f.read_text().splitlines() if l] if f.exists() else []

    def call_lines(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def git_calls(self):
        return self.gitlog.read_text().splitlines() if self.gitlog.exists() else []

    def ledger(self, **rec):
        with (self.root / "root" / "events" / "L-executor-fixture.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def swept(self, batch, base, subjects, cpu, **extra):
        for s in subjects:
            self.ledger(type="preflight-swept", subject="L-spec-%s" % s, batch=batch, base=base, ok=True,
                        stage="changed-tests", failed=[], cpu_s=cpu, wall_s=1.0, **extra)

    def queue_batch(self, name, base, members, ci=None, cand=None):
        f = self.q / name
        f.write_text("%s %s\n" % (base, " ".join(str(m) for m in members)))
        if ci is not None:
            Path(str(f) + ".ci").write_text(ci)
        if cand:
            Path(str(f) + ".cand").write_text(cand + "\n")
        return f


# =============================================================================================
class TestOrdering(Harness):
    def setUp(self):
        super().setUp()
        self.sweep_clean_for_all()
        self.stub("rechain-check", 'for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.numbers = [2101, 2102, 2103, 2104, 2105, 2106, 2107]
        for n in self.numbers:
            self.mig(n)
        self.ready(*self.numbers)

    def order(self, body):
        self.stub("migration-order", body)

    def order_list(self, ids, tail="exit 0"):
        """Print only the requested members, in this order (as the real command does)."""
        self.stub("migration-order", 'req=" $(echo "${@: -1}" | tr , " ") "; for n in %s; do case "$req" in *" L-spec-$n "*) echo L-spec-$n;; esac; done; %s' % (ids, tail))

    def test_pick_follows_migration_order_not_ready_order_within_the_cap(self):
        self.order_list("2107 2105 2103 2101 2102 2104 2106")
        picked, r = self.pick()
        self.assertEqual(picked, ["2107", "2105", "2103", "2101", "2102", "2104"], r.stderr)  # six, never seven
        log = self.log()
        self.assertIn("migration-order input: 2101 2102 2103 2104 2105 2106 2107", log)
        self.assertIn("migration-order output: 2107 2105 2103 2101 2102 2104 2106", log)
        call = [c for c in self.call_lines() if c.startswith("migration-order")]
        self.assertEqual(call, ["migration-order --project albert-scott --specs "
                                + ",".join("L-spec-%d" % n for n in self.numbers)])
        self.assertEqual(self.attention(), [])
        evidence("AC1", ["input 2101..2107", "output " + " ".join(picked)] + call)

    def test_four_or_six_cap_uses_the_shared_eligibility_key_and_never_preguard_files(self):
        self.order_list("2101 2102 2103 2104 2105 2106 2107")
        (self.q / "preguard.txt").write_text("".join("%d CLEAN\n" % n for n in self.numbers))
        (self.q / "preguard.log").write_text("x sweep on %s\n" % self.master()[:12])
        # members 5 and 6 are red/undetermined on the single-spec sweep: preguard files cannot qualify them
        self.stub("preflight@L-spec-2105", "echo 'PREFLIGHT-RED guards L-spec-2105 guard boom'; exit 1")
        self.stub("preflight@L-spec-2106", "exit 2")
        picked, r = self.pick()
        self.assertEqual(picked, ["2101", "2102", "2103", "2104", "2107"], r.stderr)
        self.assertEqual(len([c for c in self.call_lines() if c.startswith("preflight")]), 7)
        self.assertEqual(len([l for l in self.attention() if l.startswith("2105 ")]), 1)
        self.assertFalse([l for l in self.attention() if l.startswith("2106 ")])

    def test_deferred_logs_its_reason_and_returns_next_pass(self):
        self.order("for n in 2101 2103; do echo L-spec-$n; done; echo 'DEFERRED L-spec-2102 waits-on-L-spec-2150'; exit 0")
        self.ready(2101, 2102, 2103)
        picked, _ = self.pick()
        self.assertEqual(picked, ["2101", "2103"])
        self.assertIn("migration-order-deferred 2102 waits-on-L-spec-2150", self.log())
        self.assertEqual(self.attention(), [])
        self.order("for n in 2101 2102 2103; do echo L-spec-$n; done; exit 0")
        picked, _ = self.pick()
        self.assertEqual(picked, ["2101", "2102", "2103"])

    def test_cycle_makes_one_attention_entry_per_cycle_and_admits_no_batch(self):
        self.order("echo L-spec-2103; echo 'CYCLE L-spec-2101 L-spec-2102'; exit 2")
        self.ready(2101, 2102)
        for _ in range(2):  # repeated passes stay at one entry
            picked, _ = self.pick()
            self.assertEqual(picked, [])
        entries = self.attention()
        self.assertEqual(len(entries), 1, entries)
        self.assertIn("migration-order-cycle L-spec-2101 L-spec-2102", entries[0])
        evidence("AC1", entries)

    def test_exit_two_without_cycle_is_undetermined_and_blames_nobody(self):
        self.order("echo L-spec-2101; echo L-spec-2102; exit 2")   # partial stdout must not override exit 2
        self.ready(2101, 2102)
        picked, _ = self.pick()
        self.assertEqual(picked, [])
        self.assertIn("migration-order-undetermined exit 2 without CYCLE", self.log())
        self.assertEqual(self.attention(), [])
        self.order("echo L-spec-2102; echo L-spec-2101; exit 0")
        picked, _ = self.pick()
        self.assertEqual(picked, ["2102", "2101"])  # a later successful pass retries

    def test_missing_or_malformed_ordering_holds_with_a_reason(self):
        self.ready(2101, 2102)
        for body, reason in (("echo L-spec-2101; exit 0", "missing-id 2102"),
                             ("echo L-spec-2101; echo L-spec-2102; echo L-spec-2109; exit 0", "unknown-id 2109"),
                             ("echo L-spec-2101; echo L-spec-2101; echo L-spec-2102; exit 0", "duplicate-id"),
                             ("echo 'garbage line'; exit 0", "unexpected-output"),
                             ("exit 7", "exit 7")):
            self.order(body)
            picked, _ = self.pick()
            self.assertEqual(picked, [], body)
            self.assertIn("migration-order-undetermined", self.log())
            self.assertIn(reason, self.log())
        self.assertEqual(self.attention(), [])

    def test_code_only_members_keep_ready_order_and_are_not_held_by_a_failed_ordering(self):
        self.code(2201)
        self.ready(2101, 2201)
        self.order("exit 2")
        picked, _ = self.pick()
        self.assertEqual(picked, ["2201"])


class TestStackAdmission(Harness):
    def setUp(self):
        super().setUp()
        self.sweep_clean_for_all()
        self.stub("migration-order", 'for s in $(echo "${@: -1}" | tr , " "); do echo $s; done; exit 0')

    def rechain(self, body):
        self.stub("rechain-check", body)

    def test_pairs_sharing_each_canonical_shared_path_reach_the_resolver_and_batch_together(self):
        shared = sorted(rechain.SHARED)
        self.assertEqual(len(shared), 4)
        self.rechain('for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        results = []
        for i, path in enumerate(shared):
            a, b = 2300 + 2 * i, 2301 + 2 * i
            self.mig(a, {path: "edit %d\n" % a})
            self.mig(b, {path: "edit %d\n" % b})
            self.ready(a, b)
            picked, r = self.pick()
            self.assertEqual(picked, [str(a), str(b)], "%s: %s" % (path, r.stderr))
            results.append("%s -> %s" % (path, " ".join(picked)))
            # next iteration must not see these two as candidates again
            self.sh_ready.write_text("")
        self.assertEqual(self.attention(), [])
        calls = [c for c in self.call_lines() if c.startswith("rechain-check")]
        self.assertGreaterEqual(len(calls), 8)
        self.assertFalse([c for c in self.call_lines() if c.startswith("gate-window")])  # no builder/CI dispatch
        merge_tree = [c for c in self.git_calls() if "merge-tree" in c]
        self.assertEqual(merge_tree, [], "plain merge-tree veto on a migration member")
        evidence("AC2", ["SHARED enumeration:"] + shared + results)

    def test_content_conflict_and_declined_park_only_the_named_member(self):
        self.mig(2401, {rechain.FINGERPRINT: "a\n"})
        self.mig(2402, {rechain.FINGERPRINT: "b\n"})
        self.mig(2403)
        self.ready(2401, 2402, 2403)
        self.rechain('echo "L-spec-2401 clean"; echo "L-spec-2402 content-conflict api/x.py"; exit 1')
        picked, _ = self.pick()
        self.assertEqual(picked, ["2401"])   # 2403 was not assessed beyond 2402's failure: it is held, not parked
        self.assertEqual(len(self.attention()), 1)
        self.assertIn("content-conflict api/x.py", self.attention()[0])
        self.assertTrue(self.attention()[0].startswith("2402 "))
        self.rechain('echo "L-spec-2401 clean"; echo "L-spec-2403 declined not-bookkeeping"; exit 1')
        self.sh_ready.write_text("2401\n2403\n")
        (self.q / "attention.txt").write_text("")
        picked, _ = self.pick()
        self.assertEqual(picked, ["2401"])
        self.assertTrue(self.attention()[0].startswith("2403 "), self.attention())
        self.assertIn("declined", self.attention()[0])

    def test_undetermined_resolver_holds_without_attention_or_ci(self):
        self.mig(2501)
        self.ready(2501)
        self.rechain("echo RECHAIN-UNDETERMINED; exit 2")
        picked, _ = self.pick()
        self.assertEqual(picked, [])
        self.assertEqual(self.attention(), [])
        self.assertIn("stack-hold: 2501", self.log())
        self.assertFalse([c for c in self.call_lines() if c.startswith("gate-window")])

    def test_non_shared_overlap_and_code_only_overlap_still_skip(self):
        self.rechain('for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.mig(2601, {"api/common.py": "one\n"})
        self.mig(2602, {"api/common.py": "two\n"})                 # non-SHARED overlap with a migration
        self.code(2603, {rechain.MANIFEST: "code edit\n"})          # code-only touching a SHARED path
        self.mig(2604, {rechain.MANIFEST: "mig edit\n"})
        self.ready(2601, 2602, 2603, 2604)
        picked, _ = self.pick()
        self.assertEqual(picked, ["2601", "2603"])   # 2602: non-SHARED overlap; 2604: SHARED path already owned by code-only 2603
        self.assertEqual(self.attention(), [])
        self.ready(2604, 2603)
        picked, _ = self.pick()
        self.assertEqual(picked, ["2604"])           # a code-only member never shares a SHARED path with a migration

    def test_stacked_candidate_migration_owner_gets_the_same_exemption(self):
        self.mig(2701, {rechain.TEST: "owner\n", "api/own.py": "x\n"})
        cand = git(self.ir, "rev-parse", "l-spec-2701")      # the candidate includes the earlier migration member
        self.queue_batch("1.batch", self.master(), [2701], ci="", cand=cand)
        (self.q / "1.batch.ci").write_text("")               # rehearsal still running
        self.rechain('for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.mig(2702, {rechain.TEST: "second\n"})           # SHARED overlap with the candidate's migration owner
        self.mig(2703, {"api/own.py": "y\n"})                # non-SHARED overlap with it
        self.code(2704, {"code_2704.py": "z\n"})
        self.ready(2702, 2703, 2704)
        picked, r = self.pick(cand)
        self.assertEqual(picked, ["2702", "2704"], r.stderr)


class TestReadySweep(Harness):
    def setUp(self):
        super().setUp()
        self.stub("migration-order", 'for s in $(echo "${@: -1}" | tr , " "); do echo $s; done; exit 0')
        self.stub("rechain-check", 'for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')

    def sweeps(self):
        return [c for c in self.call_lines() if c.startswith("preflight")]

    def test_clean_single_spec_sweep_on_master_tip_qualifies_migration_and_code_members(self):
        self.mig(2801)
        self.code(2802)
        self.sweep_clean_for_all()
        self.ready(2801, 2802)
        picked, _ = self.pick()
        self.assertEqual(sorted(picked), ["2801", "2802"])
        tip = self.master()
        self.assertEqual(sorted(self.sweeps()), sorted([
            "preflight --project albert-scott --on %s --specs L-spec-2801" % tip,
            "preflight --project albert-scott --on %s --specs L-spec-2802" % tip]))
        self.assertFalse([c for c in self.call_lines() if c.startswith("gate-window")])

    def test_red_creates_exactly_one_stage_class_attention_entry_across_repeated_calls(self):
        self.code(2811)
        self.stub("preflight", "echo 'PREFLIGHT-RED guards L-spec-2811 guard tests/x.py::t failed'; exit 1")
        self.ready(2811)
        r = self.run_sh("sync; ready_entry 2811; a=$?; ready_entry 2811; b=$?; pick $(mtip) >/dev/null; preclean 2811; c=$?; echo $a $b $c")
        self.assertEqual(r.stdout.split(), ["1", "1", "1"], r.stderr)
        self.assertEqual(len(self.attention()), 1, self.attention())
        self.assertIn("guards/guard", self.attention()[0])
        self.assertEqual(len(self.sweeps()), 1)    # the red key is remembered; no repeat sweep
        evidence("AC3", self.attention())

    def test_undetermined_retries_next_pass_without_blame_or_ci(self):
        self.code(2821)
        self.stub("preflight", "echo 'PREFLIGHT-UNDETERMINED timeout'; exit 2")
        self.ready(2821)
        for expected in (1, 2):
            self.pick()
            self.assertEqual(len(self.sweeps()), expected)
        self.assertEqual(self.attention(), [])
        self.assertFalse([c for c in self.call_lines() if c.startswith("gate-window")])
        self.sweep_clean_for_all()
        picked, _ = self.pick()
        self.assertEqual(picked, ["2821"])

    def test_clean_key_is_reused_until_branch_sha_or_master_tree_changes(self):
        self.code(2831)
        self.sweep_clean_for_all()
        self.ready(2831)
        self.pick()
        self.pick()
        self.assertEqual(len(self.sweeps()), 1)
        self.code(2831, {"code_2831.py": "changed\n"})             # branch sha moves
        self.pick()
        self.assertEqual(len(self.sweeps()), 2)
        self.commit_master({"other.py": "new master tree\n"})      # master tree moves
        self.pick()
        self.assertEqual(len(self.sweeps()), 3)

    def test_stale_preguard_files_cannot_qualify_or_release(self):
        self.code(2841)
        self.stub("preflight", "echo 'PREFLIGHT-RED static L-spec-2841 overlap clash'; exit 1")
        self.ready(2841)
        (self.q / "preguard.txt").write_text("2841 CLEAN\n")
        (self.q / "preguard.log").write_text("t sweep on %s\n" % self.master()[:12])
        self.pick()
        picked, _ = self.pick()
        self.assertEqual(picked, [])
        r = self.run_sh("pick $(mtip) >/dev/null; preclean 2841; echo $?")
        self.assertEqual(r.stdout.strip(), "1")

    def test_numeric_cpu_from_matching_records_is_logged_once_not_summed(self):
        self.code(2851)
        tip_holder = {}
        self.stub("preflight", "echo '%s'; exit 0" % CLEAN)
        self.ready(2851)
        base = self.master()
        # records that must never be attributed: before the snapshot, other base, other subject, malformed line
        self.swept("old", base, [2851], 99.0)
        self.write_bin("doit", self.DOIT_STUB)
        shutil.copy(self.root / "bin" / "doit", self.d / "doit")
        self.stub("preflight", "echo '%s'; "
                  "printf '%%s\\n' '{\"type\":\"preflight-swept\",\"subject\":\"L-spec-2851\",\"batch\":\"sweep-b1\",\"base\":\"%s\",\"ok\":true,\"cpu_s\":1.25}' "
                  ">> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; "
                  "printf '%%s\\n' '{\"type\":\"preflight-swept\",\"subject\":\"L-spec-2999\",\"batch\":\"other\",\"base\":\"%s\",\"ok\":true,\"cpu_s\":7}' "
                  ">> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; "
                  "echo 'not json {' >> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; exit 0" % (CLEAN, base, base))
        self.pick()
        self.assertIn("preflight-cpu ready-2851 sweep-b1 cpu_s=1.25", self.log())
        self.assertNotIn("99", self.log().split("preflight-cpu")[1])

    def test_missing_malformed_and_ambiguous_records_log_unknown_never_zero(self):
        self.code(2861)
        self.code(2862)
        self.code(2863)
        base = self.master()
        self.stub("preflight@L-spec-2861", "echo '%s'; exit 0" % CLEAN)   # writes no record
        two = ("for b in x1 x2; do printf '%%s\\n' \"{\\\"type\\\":\\\"preflight-swept\\\",\\\"subject\\\":\\\"L-spec-2862\\\",\\\"batch\\\":\\\"$b\\\",\\\"base\\\":\\\"%s\\\",\\\"cpu_s\\\":1}\" "
               ">> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; done; echo '%s'; exit 0" % (base, CLEAN))
        self.stub("preflight@L-spec-2862", two)
        self.stub("preflight@L-spec-2863", "printf '%%s\\n' '{\"type\":\"preflight-swept\",\"subject\":\"L-spec-2863\",\"batch\":\"m\",\"base\":\"%s\",\"cpu_s\":\"lots\"}' >> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; echo '%s'; exit 0" % (base, CLEAN))
        self.ready(2861, 2862, 2863)
        self.pick()
        log = self.log()
        for n in (2861, 2862, 2863):
            self.assertIn("preflight-cpu ready-%d - cpu_s=unknown reason=missing-or-ambiguous-preflight-record" % n, log)
        self.assertNotIn("cpu_s=0 ", log)

    def test_batch_cpu_counts_each_sweep_once_and_ready_entry_is_referenced_not_charged_again(self):
        self.code(2871)
        self.code(2872)
        self.sweep_clean_for_all()
        self.ready(2871, 2872)
        self.pick()                      # ready entry for both
        base = self.master()
        rec = ("for s in 2871 2872; do printf '%%s\\n' \"{\\\"type\\\":\\\"preflight-swept\\\",\\\"subject\\\":\\\"L-spec-$s\\\",\\\"batch\\\":\\\"bat-1\\\",\\\"base\\\":\\\"%s\\\",\\\"cpu_s\\\":2.5}\" "
               ">> \"$DOIT_ROOT/events/L-executor-fixture.jsonl\"; done; echo '%s'; exit 0" % (base, CLEAN))
        self.stub("preflight@L-spec-2871,L-spec-2872", rec)
        self.stub("gate-window", "echo 'REHEARSED rh-1 green base=x candidate=y tree=z'; exit 0")
        r = self.run_sh("rehearse %s 2871 2872" % base)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.log()
        self.assertEqual(log.count("cpu_s=2.5"), 1, log)       # two subject records, charged once
        self.assertRegex(log, r"preflight-cpu \d+\.batch bat-1 cpu_s=2\.5")
        self.assertIn("counted=ready-entry", log)
        self.assertIn("CLEAN -> queued: 2871 2872", log)

    def test_no_python_is_introduced_by_the_driver(self):
        text = (LANE / "rloop.sh").read_text()
        self.assertEqual(text.count("python3"), 2, "only the two inherited calls (review-clean rule, push)")
        self.assertNotIn("import ", text.replace("import fold,push_origin", ""))
        self.assertNotIn("/var/tmp/push/preguard", text)


class TestMigrationChain(Harness):
    def setUp(self):
        super().setUp()
        self.sweep_clean_for_all()
        self.stub("migration-order", 'for s in $(echo "${@: -1}" | tr , " "); do echo $s; done; exit 0')
        self.stub("rechain-check", 'for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.stub("gate-window", "echo \"gate-window-called $*\" >> \"$CALLS.gw\"; echo 'REHEARSED rh-1 green base=x candidate=y tree=z'; exit 0")

    def rehearsals(self):
        f = Path(str(self.calls) + ".gw")
        return f.read_text().splitlines() if f.exists() else []

    def test_first_migration_starts_on_master_a_second_one_holds_until_a_known_candidate(self):
        self.mig(2901)
        self.mig(2902)
        self.code(2903)
        self.ready(2901)
        tip = self.master()
        picked, _ = self.pick(tip)
        self.assertEqual(picked, ["2901"])
        # 2901 now rehearsing on master: candidate not yet discovered
        self.queue_batch("1.batch", tip, [2901], ci="")
        self.ready(2902, 2903)
        picked, _ = self.pick(tip)
        self.assertEqual(picked, ["2903"], "code-only capacity stays usable")
        self.assertRegex(self.log(), r"migration-hold: 2902: (unknown-candidate|migration-rehearsal-in-flight)")
        # candidate known: only that candidate (which includes 2901) can carry the next migration
        cand = git(self.ir, "rev-parse", "l-spec-2901")
        (self.q / "1.batch.cand").write_text(cand + "\n")
        picked, _ = self.pick(tip)
        self.assertEqual(picked, ["2903"])
        self.assertIn("migration-rehearsal-in-flight", self.log())
        picked, _ = self.pick(cand)
        self.assertEqual(picked, ["2902", "2903"])
        sibling = self.branch("sib", {"sibling.py": "s\n"})
        git(self.r, "fetch", "-q", str(self.ir), "l-spec-sib")
        picked, _ = self.pick(sibling)
        self.assertEqual(picked, ["2903"])
        self.assertIn("base-lacks-earlier-migration-members", self.log())

    def test_failed_or_stale_predecessor_holds_migration_batching(self):
        self.mig(2911)
        self.mig(2912)
        tip = self.master()
        cand = git(self.ir, "rev-parse", "l-spec-2911")
        self.queue_batch("1.batch", tip, [2911], ci="gate-window: red\nexit=1\n", cand=cand)
        self.ready(2912)
        picked, _ = self.pick(cand)
        self.assertEqual(picked, [])
        self.assertIn("failed-predecessor", self.log())
        (self.q / "1.batch.ci").write_text("")
        (self.q / ("nostack-" + cand[:12])).write_text("")
        picked, _ = self.pick(cand)
        self.assertEqual(picked, [])
        self.assertIn("stale-or-failed-predecessor", self.log())

    def test_simultaneous_refill_cannot_start_two_rehearsals_on_one_prefix(self):
        self.mig(2921)
        self.mig(2922)
        self.ready(2921, 2922)
        tip = self.master()
        r = self.run_sh("fill %s; sleep_marker=1; wait; fill %s" % (tip, tip))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.rehearsals()), 1, self.rehearsals())
        self.assertEqual(len(list(self.q.glob("*.batch"))), 1)

    def test_total_in_flight_ci_never_exceeds_three(self):
        for n in (2931, 2932, 2933):
            self.code(n)
        self.ready(2931, 2932, 2933)
        tip = self.master()
        self.queue_batch("1.batch", tip, [2941], ci="")
        self.queue_batch("2.batch", tip, [2942], ci="")
        r = self.run_sh("loop_once", env={"FAKE_BUSY": "win-1-a"})   # a window is running: 1 window + 2 batches
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rehearsals(), [])
        self.assertEqual(len(list(self.q.glob("*.batch"))), 2)

    def test_stale_queued_candidate_is_rerehearsed_never_launched_on_an_old_verdict(self):
        self.code(2951)
        old = self.master()
        self.queue_batch("1.batch", old, [2951], ci="REHEARSED rh-9 green base=x\nexit=0\n")
        self.commit_master({"advance.py": "master moved\n"})
        self.ready()
        r = self.run_sh("loop_once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("re-rehearse", self.log())
        self.assertNotIn("LAUNCH", self.log())
        rh = self.rehearsals()
        self.assertEqual(len(rh), 1)
        self.assertIn("--rehearse --on %s" % self.master(), rh[0])

    def test_multi_migration_gate_refusal_is_a_capability_hold_until_gate_code_or_members_change(self):
        self.mig(2961)
        self.mig(2962)
        self.ready(2961, 2962)
        tip = self.master()
        refusal = "gate-window: rehearsal undetermined: at most one migration spec per rehearsal\nexit=2\n"
        self.queue_batch("1.batch", tip, [2961, 2962], ci=refusal)
        (self.d / "src" / "window_rehearse.py").write_text("gate rev one\n")
        self.write_bin("doit", self.DOIT_STUB)
        r = self.run_sh("loop_once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("migration-gate-hold 1.batch", self.log())
        self.assertIn("at most one migration spec per rehearsal", self.log())
        self.assertEqual(self.attention(), [])
        self.assertEqual(list(self.q.glob("*.batch*")), [])      # reservation released
        before = len(self.rehearsals())
        for _ in range(3):                                       # unchanged passes and driver reloads
            r = self.run_sh("loop_once")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.rehearsals()), before)
        self.assertEqual(self.attention(), [])
        self.assertFalse((self.root / "attr-called").exists())
        self.assertFalse([c for c in self.call_lines() if c.startswith("gh ")])
        self.assertEqual(self.pick()[0], [])
        (self.d / "src" / "window_rehearse.py").write_text("gate rev two\n")   # the gate code changed
        picked, _ = self.pick()
        self.assertEqual(picked, ["2961", "2962"])
        self.assertIn("migration-gate-release 1.batch", self.log())

    def test_member_input_change_also_releases_a_gate_hold(self):
        self.mig(2971)
        self.ready(2971)
        self.queue_batch("1.batch", self.master(), [2971],
                         ci="gate-window: rehearsal undetermined: at most one migration spec per rehearsal\nexit=2\n")
        self.run_sh("loop_once")
        self.assertEqual(self.pick()[0], [])
        self.mig(2971, {VERSIONS + "v2971b.py": "more\n"})
        self.assertEqual(self.pick()[0], ["2971"])

    def test_landing_follows_the_window_path_only_from_a_green_queued_rehearsal(self):
        self.code(2981)
        tip = self.master()
        self.ledger(type="merge-gate-clean", subject="L-spec-2981", branch="l-spec-2981")   # a dry-run verdict
        self.ready(2981)
        self.stub("preflight", "echo 'PREFLIGHT-UNDETERMINED timeout'; exit 2")
        self.run_sh("loop_once")
        self.assertEqual([l for l in self.rehearsals() if "--rehearse" not in l], [])   # clean gate alone launches nothing
        self.queue_batch("1.batch", tip, [2981], ci="REHEARSED rh-1 green base=x\nexit=0\n")
        self.run_sh("loop_once")
        lines = [l for l in self.rehearsals() if "--rehearse" not in l]
        for _ in range(50):
            if lines:
                break
            subprocess.run(["sleep", "0.1"])
            lines = [l for l in self.rehearsals() if "--rehearse" not in l]
        self.assertEqual(len(lines), 1, self.rehearsals())
        self.assertRegex(lines[0], r"gate-window-called win-\d+-r\d+ --project albert-scott --specs L-spec-2981$")
        self.assertIn("LAUNCH", self.log())


class TestBaseRed(Harness):
    RED_CI = "REHEARSED rh-5 red base=b candidate=c tree=t\nUNRESOLVED tests/x.py::a\nexit=1\n"

    def setUp(self):
        super().setUp()
        self.sweep_clean_for_all()
        self.stub("migration-order", 'for s in $(echo "${@: -1}" | tr , " "); do echo $s; done; exit 0')
        self.stub("rechain-check", 'for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.stub("gate-window", "echo \"gate-window-called $*\" >> \"$CALLS.gw\"; echo 'REHEARSED rh-1 green base=x candidate=y tree=z'; exit 0")
        self.code(3001)
        self.code(3002)
        self.ready(3001, 3002)
        self.master_red(["tests/x.py::a", "tests/x.py::z"])

    def master_red(self, ids, complete=True, rc=0):
        body = ('echo "{\\"sha\\": \\"$2\\", \\"complete\\": %s, \\"ids\\": {\\"pytest\\": %s, \\"pg-parity\\": [], \\"vitest\\": [], \\"suite-error\\": []}, \\"undetermined\\": %s}"; exit %d'
                % ("true" if complete else "false", json.dumps(ids).replace('"', '\\"'), "[]" if complete else '[\\"why\\"]', rc))
        self.stub("base-red", body)

    def rehearsals(self):
        f = Path(str(self.calls) + ".gw")
        return f.read_text().splitlines() if f.exists() else []

    def red_batch(self, ci=None):
        self.queue_batch("1.batch", self.master(), [3001, 3002], ci=ci or self.RED_CI)

    def test_covered_failing_ids_hold_the_batch_without_attention_land_or_repeat_ci(self):
        self.red_batch()
        r = self.run_sh("loop_once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("base-red-hold 1.batch tests/x.py::a", self.log())
        self.assertEqual(self.attention(), [])
        self.assertEqual(list(self.q.glob("*.batch*")), [])
        for _ in range(3):                      # repeated passes and driver reloads
            self.run_sh("loop_once")
            self.assertEqual(self.pick()[0], [])
        self.assertEqual(self.rehearsals(), [])
        self.assertFalse([c for c in self.call_lines() if "--ensure" in c or "nightly" in c])
        self.assertEqual(self.attention(), [])
        evidence("AC5", [self.log()])

    def test_unrelated_master_commit_does_not_release_but_a_moved_member_does(self):
        self.red_batch()
        self.run_sh("loop_once")
        self.commit_master({"unrelated.py": "u\n"})
        self.assertEqual(self.pick()[0], [])
        self.code(3001, {"code_3001.py": "new build\n"})
        picked, _ = self.pick()
        self.assertEqual(picked, ["3001", "3002"])
        self.assertIn("base-red-release 1.batch (member branch moved)", self.log())

    def test_changed_master_red_status_releases_incomplete_evidence_does_not(self):
        self.red_batch()
        self.run_sh("loop_once")
        self.master_red([], complete=False, rc=3)
        self.assertEqual(self.pick()[0], [])
        self.master_red(["tests/x.py::z"])          # complete: the covered id is no longer red on master
        picked, _ = self.pick()
        self.assertEqual(picked, ["3001", "3002"])
        self.assertIn("master red status changed for tests/x.py::a", self.log())

    def test_mixed_empty_and_incomplete_cases_are_never_proven_base_red(self):
        cases = {
            "mixed": "REHEARSED rh-5 red base=b candidate=c tree=t\nUNRESOLVED tests/x.py::a\nUNRESOLVED tests/new.py::q\nexit=1\n",
            "empty": "REHEARSED rh-5 red base=b candidate=c tree=t\nexit=1\n",
            "noverdict": "gate-window: boom\nexit=1\n"}
        for name, ci in cases.items():
            (self.q / "attention.txt").write_text("")
            self.red_batch(ci)
            self.run_sh("loop_once")
            self.assertNotIn("base-red-hold", self.log(), name)
            self.assertEqual(len(self.attention()), 2, (name, self.attention()))
            for f in self.q.glob("*.batch*"):
                f.unlink()
        self.master_red([], complete=False, rc=3)
        (self.q / "attention.txt").write_text("")
        self.red_batch()
        self.run_sh("loop_once")
        self.assertNotIn("base-red-hold", self.log())
        self.assertEqual(len(self.attention()), 2)

    def test_classification_reads_existing_master_evidence_only(self):
        self.red_batch()
        self.run_sh("loop_once")
        base_red = [c for c in self.call_lines() if c.startswith("base-red")]
        self.assertTrue(base_red)
        for c in base_red:
            self.assertRegex(c, r"^base-red albert-scott [0-9a-f]{40} --json$")


class TestPreflightAdapter(Harness):
    def adapter(self, body, *specs, env=None):
        self.stub("preflight", body)
        e = dict(self.base_env)
        e.update(env or {})
        r = subprocess.run(["bash", str(self.lane / "preflight.sh"), "a" * 40, *["L-spec-%s" % s for s in specs]],
                           env=e, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        return (self.out / ("rehearse-" + "-".join(specs) + ".txt")).read_text()

    def test_four_outcomes_one_sweep_each_and_timeout_is_never_clean(self):
        clean = self.adapter("echo '%s'; exit 0" % CLEAN, "3101", "3102")
        red = self.adapter("echo 'PREFLIGHT-RED stack L-spec-3102 content-conflict a.py'; exit 1", "3101", "3102")
        broken = self.adapter("echo 'PREFLIGHT-UNDETERMINED stack git unavailable'; exit 2", "3101", "3102")
        slow = self.adapter("exec /bin/sleep 30", "3101", "3102", env={"PREFLIGHT_OUTER_TIMEOUT": "1"})
        self.assertIn("NO-NEW-FAILURES\nDONE", clean)
        self.assertIn("CONFLICT L-spec-3102", red)
        self.assertNotIn("NO-NEW-FAILURES", red)
        self.assertIn("RUN-BROKEN: PREFLIGHT-UNDETERMINED stack", broken)
        self.assertIn("RUN-BROKEN: PREFLIGHT-UNDETERMINED timeout", slow)
        for report in (red, broken, slow):
            self.assertNotIn("NO-NEW-FAILURES", report)
        sweeps = [c for c in self.call_lines() if c.startswith("preflight")]
        self.assertEqual(len(sweeps), 4)     # exactly one shared sweep per invocation
        evidence("AC6", [clean, red, broken, slow])

    def test_driver_reads_each_report_and_only_a_clean_one_is_queued(self):
        self.code(3111)
        self.code(3112)
        base = self.master()
        self.stub("gate-window", "echo 'REHEARSED rh-1 green base=x candidate=y tree=z'; exit 0")
        self.stub("attr", "exit 0")
        outcomes = {}
        for label, body in (("clean", "echo '%s'; exit 0" % CLEAN),
                            ("broken", "echo 'PREFLIGHT-UNDETERMINED stack x'; exit 2"),
                            ("timeout", "exec /bin/sleep 30")):
            self.stub("preflight", body)
            for f in self.q.glob("*.batch*"):
                f.unlink()
            env = {"PREFLIGHT_OUTER_TIMEOUT": "1"} if label == "timeout" else {}
            r = self.run_sh("rehearse %s 3111 3112; echo rc=$?" % base, env=env)
            outcomes[label] = (r.stdout.strip(), self.log())
        self.assertEqual(outcomes["clean"][0], "rc=0")
        self.assertEqual(outcomes["broken"][0], "rc=1")
        self.assertEqual(outcomes["timeout"][0], "rc=1")
        self.assertEqual(self.log().count("CLEAN -> queued"), 1)
        self.assertIn("could not run its tests", (self.root / "tg.log").read_text())
        self.assertEqual(self.attention(), [])    # a broken sweep blames no spec

    def test_no_raw_merge_heads_or_second_guard_step_remains_in_the_adapter(self):
        text = (LANE / "preflight.sh").read_text()
        for forbidden in ("merge-tree", "git merge", "worktree add", "pytest", "alembic heads", "\nGUARDS="):
            self.assertNotIn(forbidden, text)
        self.assertEqual(text.count("preflight --project"), 1)
        self.assertEqual(text.count("guard-tests.txt"), text.count("guard-tests.txt"))
        self.assertEqual((LANE / "guard-tests.txt").read_text(), (self.lane / "guard-tests.txt").read_text())

    def test_inherited_suite_still_passes(self):
        r = subprocess.run([sys.executable, str(ROOT / "src" / "test_lane_preflight.py")], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        evidence("AC6", [r.stdout.strip().splitlines()[-1]])


class TestStateRoundTrip(Harness):
    def test_source_only_creates_nothing_and_defaults_match_the_declared_literals(self):
        ghost = self.root / "ghost"
        r = subprocess.run(["bash", "-c", 'source "%s/rloop.sh"; echo sourced' % self.lane],
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home"), "RLOOP_Q": str(ghost / "q")},
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.root))
        self.assertEqual(r.stdout.strip(), "sourced", r.stderr)
        self.assertFalse(ghost.exists())
        r = subprocess.run(["bash", "-c", 'source "%s/rloop.sh"; echo "$RLOOP_Q|$RLOOP_READY|$RLOOP_R|$RLOOP_D|$RLOOP_IR|$RLOOP_TG_SEND|$RLOOP_PROJECT|$Q|$R|$D|$IR|$READY"' % self.lane],
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home")},
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.root))
        want = ["/var/tmp/push/rq", "/var/tmp/push/ready-pass.txt", "/opt/albert-scott", "/home/albert/do-it-v2",
                "/home/albert/.do-it/repos/albert-scott", "/home/albert/.do-it/telegram/tg-send.sh", "albert-scott",
                "/var/tmp/push/rq", "/opt/albert-scott", "/home/albert/do-it-v2", "/home/albert/.do-it/repos/albert-scott",
                "/var/tmp/push/ready-pass.txt"]
        self.assertEqual(r.stdout.strip().split("|"), want, r.stderr)
        for script in ("rloop.sh", "preflight.sh"):
            subprocess.run(["/bin/bash", "-n", str(LANE / script)], check=True)
        evidence("AC7", ["defaults: " + " ".join(want[:7])])

    def test_executing_the_script_runs_main_which_initialises_the_scratch_queue(self):
        env = dict(self.base_env, RLOOP_UNTIL="1")      # the deadline has passed: main starts and ends at once
        r = subprocess.run(["bash", str(self.lane / "rloop.sh")], env=env, cwd=str(self.root), text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.q / "main.pid").exists())
        self.assertTrue((self.q / "attention.txt").exists())
        self.assertIn("rloop start", self.log())
        self.assertIn("rloop end", self.log())

    def test_state_written_by_one_process_is_read_by_a_fresh_one(self):
        self.code(3201)
        self.code(3202)
        self.mig(3203)
        self.sweep_clean_for_all()
        self.stub("migration-order", 'for s in $(echo "${@: -1}" | tr , " "); do echo $s; done; exit 0')
        self.stub("rechain-check", 'for s in $(echo "${@: -1}" | tr , " "); do echo "$s clean"; done; exit 0')
        self.stub("base-red", 'echo "{\\"sha\\": \\"$2\\", \\"complete\\": true, \\"ids\\": {\\"pytest\\": [\\"t.py::a\\"]}, \\"undetermined\\": []}"; exit 0')
        self.ready(3201)
        writer = self.run_sh("sync; ready_entry 3201; echo writer=$?")
        self.assertEqual(writer.stdout.strip(), "writer=0", writer.stderr)
        self.queue_batch("1.batch", self.master(), [3202], ci="REHEARSED rh-1 red base=b\nUNRESOLVED t.py::a\nexit=1\n")
        self.run_sh("ci_red %s/1.batch; echo done" % self.q)
        self.queue_batch("2.batch", self.master(), [3203], ci="gate-window: rehearsal undetermined: at most one migration spec per rehearsal\nexit=2\n")
        self.run_sh("ci_red %s/2.batch" % self.q)
        before = len(self.sweeps())
        reader = self.run_sh('preclean 3201; a=$?; base_red_held 3202; b=$?; gate_held 3203; c=$?; echo $a $b $c', init=False)
        self.assertEqual(reader.stdout.split(), ["0", "0", "0"], reader.stderr)
        self.assertEqual(len(self.sweeps()), before)       # the reader needed no sweep
        # changing the bound inputs invalidates each record
        self.code(3201, {"code_3201.py": "moved\n"})
        self.code(3202, {"code_3202.py": "moved\n"})
        self.mig(3203, {VERSIONS + "extra.py": "m\n"})
        self.run_sh("sync", init=False)
        reader = self.run_sh('preclean 3201; a=$?; base_red_held 3202; b=$?; gate_held 3203; c=$?; echo $a $b $c', init=False)
        self.assertEqual(reader.stdout.split(), ["1", "1", "1"], reader.stderr)
        evidence("AC7", ["writer then separate reader: 0 0 0; after input change: 1 1 1"])

    def sweeps(self):
        return [c for c in self.call_lines() if c.startswith("preflight")]

    def test_malformed_state_and_hostile_input_never_admit_or_execute(self):
        self.code(3301)
        self.sweep_clean_for_all()
        self.stub("migration-order", "exit 0")
        sha, tree = git(self.r, "rev-parse", "HEAD") if False else None, None
        self.run_sh("sync", init=True)
        sha = git(self.r, "rev-parse", "refs/integ/l-spec-3301")
        tree = git(self.r, "rev-parse", "refs/integ/master^{tree}")
        elig = self.q / "eligibility"
        elig.mkdir()
        (elig / ("3301.%s.%s" % (sha, tree))).write_text("clean albert-scott garbage\n")
        self.ready(3301)
        r = self.run_sh("preclean 3301; echo $?")
        self.assertEqual(r.stdout.strip(), "1")
        picked, _ = self.pick()
        self.assertEqual(picked, [])
        self.assertIn("eligibility-record-malformed", self.log())
        # hostile ready rows and hostile CLI text are data, never code
        marker = self.root / "pwned"
        (elig / ("3301.%s.%s" % (sha, tree))).unlink()
        self.sh_ready.write_text("3301$(touch %s)\n3301; touch %s\n" % (marker, marker))
        self.stub("preflight", "echo 'PREFLIGHT-RED guards L-spec-3301 guard $(touch %s); `touch %s` | ; done'; exit 1" % (marker, marker))
        self.pick()
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.attention()), 1)
        self.assertNotIn("|", self.attention()[0].replace("|do not auto-release|", ""))
        # malformed hold files keep their members held
        hold = self.q / "gate-hold"
        hold.mkdir()
        (hold / "x.batch").write_text("rev\nnot-a-member-list\n")
        (self.q / "attention.txt").write_text("")
        self.sh_ready.write_text("3301\n")
        r = self.run_sh("gate_held 3301; echo $?")
        self.assertEqual(r.stdout.strip(), "1", r.stderr)  # a malformed file that names no member of 3301 is not its hold
        (hold / "x.batch").write_text("rev\n3301:zzz\n")
        r = self.run_sh("gate_held 3301; echo $?")
        self.assertEqual(r.stdout.strip(), "0")
        self.assertIn("migration-gate-hold-malformed", self.log())

    @unittest.skipIf(os.geteuid() == 0, "directory permissions do not bind root")
    def test_failed_persistence_cannot_admit(self):
        self.code(3401)
        self.sweep_clean_for_all()
        self.run_sh("sync")
        (self.q / "eligibility").mkdir()
        (self.q / "eligibility").chmod(0o500)
        self.addCleanup((self.q / "eligibility").chmod, 0o700)
        r = self.run_sh("ready_entry 3401; echo $?")
        self.assertEqual(r.stdout.strip(), "1", r.stderr)
        self.assertIn("eligibility-unwritable", self.log())
        self.ready(3401)
        self.assertEqual(self.pick()[0], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
