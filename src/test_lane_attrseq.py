"""Offline attribution regressions: temporary outfiles and a shell stub only."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "lane/attrseq.sh"
SHARED = "tests/test_shared.py::test_shared"
SEED = ("BASE fails: tests/test_base.py::test_base\n"
        + "".join(f"{spec} NEW: {SHARED}\n" for spec in (2129, 1771, 2040, 1999))
        + "3000 NEW: tests/test_other.py::test_other\n4000 CONFLICT\n")


def evidence(ac, body):
    print(json.dumps({"url": Path(__file__).as_uri() + "#" + ac, "status": 200,
                      "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                      "body_excerpt": body}), flush=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-attrseq-")
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "attr.txt"
        self.log = Path(self.tmp.name) / "calls.txt"

    def shell(self, body):
        script = (f"set -euo pipefail\nsource {shlex.quote(str(HELPER))}\n"
                  f"OUT={shlex.quote(str(self.out))}\n"
                  f"LOG={shlex.quote(str(self.log))}\n" + body)
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def resolve(self):
        self.shell('''
one() {
    # A stale result must be removed before starting its replacement.
    if grep -q "^$1 " "$OUT"; then return 90; fi
    mkdir "${LOG}.active"
    echo "start $1" >> "$LOG"
    sleep 0.01
    if [ "$1" = 1771 ]; then
        echo "$1 NEW: tests/test_shared.py::test_shared" >> "$OUT"
    else
        echo "$1 NEW: none" >> "$OUT"
    fi
    echo "end $1" >> "$LOG"
    rmdir "${LOG}.active"
}
attr_resolve "$OUT" one
''')
        return self.log.read_text().splitlines() if self.log.exists() else []


class SharedTests(Fixture):
    def test_four_sharing_specs_only(self):
        self.out.write_text(SEED)
        result = self.shell('attr_shared "$OUT"')
        self.assertEqual(result.splitlines(), ["1771", "1999", "2040", "2129"])
        self.assertEqual(self.out.read_text(), SEED)
        evidence("AC1", result)

    def test_no_shared_ids(self):
        for seed in ("", "1 NEW: none\n2 CONFLICT\n",
                     f"1 NEW: {SHARED}\n2 NEW: none\n3 CONFLICT\n",
                     "1 NEW: a a\n2 NEW: b\n3 CONFLICT\n",
                     "1 NEW: a\n2 NEW: aa\n3 CONFLICT a\n"):
            with self.subTest(seed=seed):
                self.out.write_text(seed)
                self.assertEqual(self.shell('attr_shared "$OUT"'), "")

    def test_any_id_can_overlap_without_duplicate_specs(self):
        self.out.write_text("10 NEW: a b b\n2 NEW: b c\n3 NEW: c\n4 NEW: d\n")
        self.assertEqual(self.shell('attr_shared "$OUT"').splitlines(), ["2", "3", "10"])


class ResolveTests(Fixture):
    def test_sequential_replacements_preserve_unrelated_lines(self):
        self.out.write_text(SEED)
        log = self.resolve()
        expected = [f"{event} {spec}" for spec in (1771, 1999, 2040, 2129)
                    for event in ("start", "end")]
        self.assertEqual(log, expected)
        lines = self.out.read_text().splitlines()
        self.assertEqual(len(lines), len(SEED.splitlines()))
        for spec in (1999, 2040, 2129):
            self.assertIn(f"{spec} NEW: none", lines)
        self.assertIn(f"1771 NEW: {SHARED}", lines)
        for line in SEED.splitlines():
            if not any(line.startswith(f"{spec} ") for spec in (1771, 1999, 2040, 2129)):
                self.assertIn(line, lines)
        self.assertCountEqual(Path(self.tmp.name).iterdir(), [self.out, self.log])
        evidence("AC2", "\n".join(log) + "\n" + self.out.read_text())

    def test_disjoint_and_lone_failures_are_unchanged(self):
        for seed in (f"1771 NEW: {SHARED}\n2 CONFLICT\n",
                     "1 NEW: a\n2 NEW: b\n3 NEW: none\nDONE\n"):
            with self.subTest(seed=seed):
                self.out.write_text(seed)
                self.assertEqual(self.resolve(), [])
                self.assertEqual(self.out.read_text(), seed)


class CapTests(Fixture):
    def test_six_lowest_numbers_and_unchecked_seventh(self):
        self.out.write_text("".join(f"{spec} NEW: {SHARED}\n"
                                    for spec in (1000, 90, 8, 120, 2, 11, 7)))
        log = self.resolve()
        self.assertEqual(log, [f"{event} {spec}" for spec in (2, 7, 8, 11, 90, 120)
                               for event in ("start", "end")])
        self.assertIn(f"1000 NEW: {SHARED} UNCHECKED", self.out.read_text().splitlines())
        self.assertEqual(len(self.out.read_text().splitlines()), 7)
        evidence("AC3-cap", "\n".join(log) + "\n" + self.out.read_text())


class WiringTests(unittest.TestCase):
    def test_syntax_source_and_call_order(self):
        for script in ("attr.sh", "attrseq.sh"):
            subprocess.run(["bash", "-n", str(ROOT / "lane" / script)], check=True)
        staged = subprocess.run(["git", "ls-files", "-s", "lane/attrseq.sh"], cwd=ROOT,
                                check=True, capture_output=True, text=True).stdout
        self.assertTrue(staged.startswith("100755 "), staged)
        script = (ROOT / "lane/attr.sh").read_text()
        source = 'source "$(dirname "${BASH_SOURCE[0]}")/attrseq.sh"'
        self.assertIn(source, script)
        self.assertLess(script.index(source), script.index('attr_resolve "$OUT" one'))
        self.assertLess(script.index("done; wait"), script.index('attr_resolve "$OUT" one'))
        self.assertLess(script.index('attr_resolve "$OUT" one'), script.index("echo DONE"))

    def test_rloop_unchanged_from_base(self):
        base = subprocess.check_output(["git", "merge-base", "HEAD", "main"],
                                       cwd=ROOT, text=True).strip()
        original = subprocess.check_output(["git", "show", f"{base}:lane/rloop.sh"], cwd=ROOT)
        self.assertEqual((ROOT / "lane/rloop.sh").read_bytes(), original)
        evidence("AC3-wiring", "bash -n passed; source < wait < resolve < DONE; "
                 "attrseq mode 100755; rloop.sh byte-identical to base")


if __name__ == "__main__":
    unittest.main(verbosity=2)
