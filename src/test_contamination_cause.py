#!/usr/bin/env python3
"""Pure contamination causes and the grader's optional output contract."""
import hashlib
import json
from pathlib import Path
import unittest

import dispatch


ROOT = Path(__file__).resolve().parent.parent


def signed(criterion, body):
    raw = json.dumps(body, sort_keys=True).encode()
    print(json.dumps(dict(url="cli://contamination-cause/" + criterion, status=0,
                         body_sha256=hashlib.sha256(raw).hexdigest(), body_excerpt=body)))


class ContaminationCauseTests(unittest.TestCase):
    def test_reason_cases(self):
        cases = [
            ({"contamination": True, "contamination_cause": "packet-leak"},
             "contamination:packet-leak"),
            ({"contamination": True, "contamination_cause": "self-inflicted"},
             "contamination:self-inflicted"),
            ({"contamination": True}, "contamination:unstated"),
            ({"contamination": True, "contamination_cause": "unknown"},
             "contamination:unstated"),
        ]
        cases.extend((dict(contamination=True, contamination_cause=cause), "contamination:unstated")
                     for cause in (None, 1, True, [], {}, ["packet-leak"]))
        rows = []
        for output, expected in cases:
            with self.subTest(output=output):
                actual = dispatch.contamination_reason(output)
                self.assertEqual(actual, expected)
                rows.append(dict(output=output, reason=actual))
        self.assertEqual(len(rows), len(cases))
        signed("AC1", rows)

    def test_optional_grader_contract(self):
        schema = json.loads((ROOT / "agents/grader.schema.json").read_text())
        cause = schema["properties"]["contamination_cause"]
        self.assertEqual(cause["type"], "string")
        self.assertCountEqual(cause["enum"], ["packet-leak", "self-inflicted"])
        self.assertNotIn("contamination_cause", schema["required"])
        self.assertIs(schema["additionalProperties"], False)
        contract = (ROOT / "agents/grader.md").read_text()
        self.assertRegex(contract, r"`contamination: true`[\s\S]{0,500}`contamination_cause`")
        self.assertIn("`packet-leak`", contract)
        self.assertIn("`self-inflicted`", contract)
        signed("AC2", dict(property=cause, optional=True, additionalProperties=False,
                           contract_mentions_cause=True))


if __name__ == "__main__":
    unittest.main()
