"""Offline tests for lane/redalone.sh and its use in lane/rloop.sh; temporary directories only."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE = ROOT / "lane"
RLOOP = (LANE / "rloop.sh").read_text()


def evidence(ac, body):
    print(json.dumps({"ac": ac, "url": Path(__file__).as_uri() + "#" + ac, "status": 200,
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "body_excerpt": body[:1000]}), flush=True)


class RedAloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-redalone-")
        self.addCleanup(self.tmp.cleanup)
        self.q = Path(self.tmp.name) / "rq"
        self.q.mkdir()

    def sh(self, script, **env):
        e = dict(os.environ, Q=str(self.q), HOME=self.tmp.name, **env)
        return subprocess.run(["bash", "-c", script], env=e, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def src(self):
        return f'. "{LANE}/redalone.sh"; '

    def done_or_queued_fn(self):
        start = RLOOP.index("done_or_queued(){")
        end = RLOOP.index("is_mig(){")
        return RLOOP[start:end]

    def test_ac1_add_blocked(self):
        s = self.src()
        self.assertNotEqual(self.sh(s + "redalone_blocked 1762 aaa111").returncode, 0)  # missing file
        self.assertEqual(self.sh(s + 'redalone_add 1762 aaa111 "tests/x.py::t"').returncode, 0)
        self.assertEqual(self.sh(s + "redalone_blocked 1762 aaa111").returncode, 0)
        self.assertNotEqual(self.sh(s + "redalone_blocked 1762 bbb222").returncode, 0)
        self.assertNotEqual(self.sh(s + "redalone_blocked 1642 aaa111").returncode, 0)
        line = (self.q / "red-alone.txt").read_text().split()
        self.assertEqual((line[0], line[1], line[3]), ("1762", "aaa111", "tests/x.py::t"))
        evidence("AC1", "add/blocked: same sha blocked; other sha, other spec, missing file not")

    def stub(self, tip):
        return (f'Q="{self.q}"; R=/nonexistent; TIP={tip}; git(){{ echo "$TIP"; }}; '
                'log(){ echo "LOG $*"; }; ' + self.src() + self.done_or_queued_fn())

    def test_ac2_done_or_queued_skip_and_new_build(self):
        (self.q / "attention.txt").touch()
        self.sh(self.src() + "redalone_add 99999 tip1 t::a")
        r = self.sh(self.stub("tip1") + "done_or_queued 99999")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sh(self.stub("tip2") + "done_or_queued 99999")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        evidence("AC2", "skipped at tip1; not skipped at tip2")

    def test_ac3_record_branch(self):
        s = self.src()
        txt, log = self.q / "red-alone.txt", self.q / "red-alone.log"
        self.sh(s + 'redalone_note 1 L-spec-1762 abcdef1234567890 M M "tests/x.py::t"')
        self.assertEqual(len(txt.read_text().splitlines()), 1)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertIn("RED-ALONE L-spec-1762 abcdef123456 tests/x.py::t (clear: lane/redalone.sh clear L-spec-1762)",
                      log.read_text())
        before = (txt.read_text(), log.read_text())
        for args in ('1 L-spec-2 abc M M "RUN-BROKEN: no pytest"', '2 L-spec-3 abc M M "tests/x.py::t"',
                     '1 L-spec-4 abc M M ""', '1 L-spec-5 abc M M "tests/y.py::u"'.replace("M M", "S M")):
            self.sh(s + "redalone_note " + args)
        self.assertEqual((txt.read_text(), log.read_text()), before)
        self.assertIn("redalone_note 1", RLOOP)
        self.assertIn('. "$L/redalone.sh"', RLOOP)
        evidence("AC3", "one-spec red recorded once; broken, two-spec, no-ids, stacked record nothing")

    def test_ac5_stdout_once_clear(self):
        (self.q / "attention.txt").touch()
        self.sh(self.src() + "redalone_add 99999 tip1 t::a")
        fn = self.stub("tip1")
        r1 = self.sh(fn + "done_or_queued 99999; done_or_queued 99999")
        self.assertEqual(r1.stdout, "", r1.stdout)
        self.assertEqual(r1.stderr.count("skip L-spec-99999: red alone at tip1; waits for a new build"), 1, r1.stderr)
        self.sh(self.src() + "redalone_add 1762 aaa t::a; redalone_clear 1762")
        self.assertNotEqual(self.sh(self.src() + "redalone_blocked 1762 aaa").returncode, 0)
        self.assertEqual((self.q / "red-alone.log").read_text().count("CLEARED L-spec-1762"), 1)
        cli = subprocess.run([str(LANE / "redalone.sh"), "clear", "L-spec-99999"],
                             env=dict(os.environ, Q=str(self.q)), text=True, capture_output=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertNotEqual(self.sh(self.src() + "redalone_blocked 99999 tip1").returncode, 0)
        evidence("AC5", "skip line on stderr once; stacked records nothing; clear unblocks")


if __name__ == "__main__":
    unittest.main()
