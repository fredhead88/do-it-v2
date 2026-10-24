#!/usr/bin/env python3
"""Hermetic regression checks for the bounded recorded-reaper tally."""
import hashlib
import json
import unittest

import resources
import tick


class ReapSummaryTests(unittest.TestCase):
    def summary(self, rows, **kw):
        return tick._reap_summary(rows, not_reapable=kw.get("not_reapable", {}),
                                  manifest=kw.get("manifest", {"legacy": 0, "declared": 0}))

    def test_large_summary(self):
        reasons = ["undetermined: malformed ref", "undetermined: owner"] + [
            "probe " + chr(97 + i) + " /fixture/item123 pid 456" for i in range(18)]
        rows = [resources.ReapResult("/fixture/" + str(i),
                    ("worktree", "scratch-db", "tmpdir")[i % 3],
                    ("refused", "reaped", "would-reap")[i % 3], reasons[i % 20])
                for i in range(1500)]
        result = self.summary(rows)
        self.assertEqual(result["n"], 1500)
        self.assertEqual(result["by_action"], {a: 500 for a in ("refused", "reaped", "would-reap")})
        self.assertEqual(set(result["by_kind"]), {"worktree", "scratch-db", "tmpdir"})
        self.assertEqual(len(result["by_reason"]), 16)
        self.assertIn("other", result["by_reason"])
        self.assertEqual(sum(result["by_reason"].values()), 1500)
        self.assertLess(len(json.dumps(result).encode()), 4096)
        body = json.dumps(result, sort_keys=True)
        print(json.dumps(dict(url="cli://recorded-reap-summary/AC1", status=0,
              body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))

    def test_reason_normalization(self):
        reasons = ["undetermined: malformed ref", "undetermined: owner",
                   "probe ('/one/path123'), pid 123;", 'probe "/two/path456": pid 456;',
                   "x" * 60 + "a", "x" * 60 + "b"]
        result = self.summary([resources.ReapResult("ref", "worktree", "refused", r) for r in reasons])
        self.assertEqual(result["by_reason"], {
            "worktree:undetermined malformed ref": 1, "worktree:undetermined owner": 1,
            "worktree:probe <path> pid #": 2, "worktree:" + "x" * 60: 2})

    def test_kind_caps_and_collisions(self):
        kinds = ["k" + chr(97 + i) for i in range(20)] + ["other", "x" * 500, "x" * 40 + "y", 123]
        rows = [resources.ReapResult("ref", k, a, "hold")
                for k in kinds for a in ("refused", "would-reap")]
        result = self.summary(rows, not_reapable={k: 2 for k in kinds})
        for name in ("by_kind", "not_reapable", "by_reason"):
            self.assertEqual(len(result[name]), 16)
            self.assertIn("other", result[name])
        self.assertEqual(sum(sum(v.values()) for v in result["by_kind"].values()), len(rows))
        self.assertEqual(sum(result["not_reapable"].values()), len(rows))
        # Reason prefixes retain the coerced kind, independent of by_kind's cap.
        single = self.summary([resources.ReapResult("ref", "x" * 500, "refused", "a" * 500)])
        self.assertEqual(list(single["by_reason"]), ["x" * 40 + ":" + "a" * 60])
        self.assertIn("other(kind)", self.summary([resources.ReapResult("r", "other", "refused", "x")])["by_kind"])
        self.assertEqual(result["by_kind"]["x" * 40], {"refused": 2, "would-reap": 2})
        tied = self.summary([resources.ReapResult("r", k, "refused", "x") for k in reversed(kinds[:20])])
        self.assertEqual(set(tied["by_kind"]), set(kinds[:15]) | {"other"})

    def test_empty_and_optional_components(self):
        self.assertIsNone(self.summary([]))
        self.assertIsNone(self.summary([], not_reapable={"upload-copy": 0}))
        self.assertEqual(self.summary([], not_reapable={"upload-copy": 3})["not_reapable"], {"upload-copy": 3})
        self.assertEqual(self.summary([], manifest={"legacy": 0, "declared": 0, "unreadable": True})["manifest"]["unreadable"], True)
        result = self.summary([], not_reapable=None, manifest={"legacy": 3, "declared": 0})
        self.assertNotIn("not_reapable", result)
        self.assertEqual(result["manifest"]["legacy"], 3)


if __name__ == "__main__":
    unittest.main()
