#!/usr/bin/env python3
"""Revision and authority regression matrix; all inputs are fixture snapshots."""
import copy
import unittest
from unittest import mock

import effective_writes as ew
import merge_gate


class EffectiveWritesTests(unittest.TestCase):
    sid = "L-spec-9001"
    text = "Writes: src/a.py src/test_a.py src/a.py\n"

    def written(self, src="writer:1", sid=None):
        return dict(type="spec-written", subject=sid or self.sid, _src=src)

    def grant(self, paths, rev="writer:1", sid=None):
        return dict(type="writes-granted", subject=sid or self.sid,
                    spec_written_src=rev, tests=[dict(test=p, source="coupled",
                    kind="backend", ac_bound=["AC1"]) for p in paths])

    def test_revision_subject_order_metadata_and_no_mutation(self):
        events = [self.written("old"), self.grant(["src/old.py"], "old"),
                  self.written(), self.grant(["src/first.py"]),
                  self.grant(["src/test_a.py", "src/test_b.py", "src/test_b.py"]),
                  self.grant(["src/stale.py"], "old"),
                  self.written("other", "L-spec-9002"),
                  self.grant(["src/other.py"], "other", "L-spec-9002")]
        before = copy.deepcopy(events)
        self.assertEqual(ew.effective_grant(self.sid, self.text, events),
                         ["src/a.py", "src/test_a.py", "src/test_b.py"])
        rows = ew.granted_tests(self.sid, events)
        self.assertEqual(rows, events[4]["tests"])
        rows[0]["ac_bound"].append("changed")
        self.assertEqual(events, before)
        self.assertEqual(ew.effective_grant("L-spec-9002", self.text, events),
                         ["src/a.py", "src/test_a.py", "src/other.py"])

    def test_no_matching_revision_or_identity_and_empty_tests(self):
        for events in ([], [self.grant(["src/unbound.py"])],
                       [self.written(), self.grant(["src/stale.py"], "old")],
                       [self.written(), self.grant([])],
                       [self.written(), self.grant(["src/old.py"]), self.written("new")],
                       [dict(type="spec-written", subject=self.sid), self.grant([])]):
            with self.subTest(events=events):
                self.assertEqual(ew.effective_grant(self.sid, self.text, events),
                                 ["src/a.py", "src/test_a.py"])
                self.assertEqual(ew.granted_tests(self.sid, events), [])

    def test_malformed_recorded_files_fail_closed(self):
        for path in ("/etc/passwd", "../x.py", "src/../x.py", "src/**", "*/**",
                     "src/test_?.py", "src/[x].py", 17, None, "", "src/", "."):
            with self.subTest(path=path), self.assertRaises(merge_gate.Undetermined):
                ew.effective_grant(self.sid, self.text,
                                   [self.written(), self.grant([path])])
        for rows in (None, {}, [17], [{}], [dict(test="src/a.py"), 17]):
            grant = self.grant([])
            grant["tests"] = rows
            with self.subTest(rows=rows), self.assertRaises(merge_gate.Undetermined):
                ew.effective_grant(self.sid, self.text, [self.written(), grant])

    def test_existing_parser_and_recorded_validator_are_used(self):
        events = [self.written(), self.grant(["src/test_valid.py"])]
        with mock.patch.object(merge_gate, "check_grant", wraps=merge_gate.check_grant) as check:
            ew.effective_grant(self.sid, self.text, events)
        check.assert_any_call(["src/test_valid.py"], "recorded tests for " + self.sid)
        original = merge_gate.check_grant

        def refuse(paths, whose):
            if whose.startswith("recorded tests"):
                raise merge_gate.Undetermined("validator refusal")
            return original(paths, whose)

        with mock.patch.object(merge_gate, "check_grant", side_effect=refuse), \
                self.assertRaisesRegex(merge_gate.Undetermined, "validator refusal"):
            ew.effective_grant(self.sid, self.text, events)
        with self.assertRaises(merge_gate.Undetermined):
            ew.effective_grant(self.sid, "Writes: prose only\n", events)


if __name__ == "__main__":
    unittest.main()
