#!/usr/bin/env python3
"""Pin the relay's literal-path deletion rule in both serving instructions."""

from pathlib import Path
import re
import tempfile
import unittest


RELAY = Path(__file__).resolve().parent.parent / "agents" / "relay.md"
MARKERS = (
    "literal path", "rm", "git rm", "command substitution", "$(...)",
    "backticks", "variable", "glob", "xargs rm", "never",
)


def check_rule(path):
    """Return the locations missing a complete rule, including absent sections."""
    body = path.read_text(encoding="utf-8")
    step = re.search(r"^4\. (.*?)(?=^5\. |\Z)", body, re.M | re.S)
    never = re.search(r"^## Never\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    candidates = {
        "step 4": step.group(1).splitlines() if step else [],
        "Never bullet": re.findall(r"^- .*?(?=^- |\Z)", never.group(1), re.M | re.S)
        if never else [],
    }
    return [
        location for location, passages in candidates.items()
        if not any(all(marker in passage for marker in MARKERS) for passage in passages)
    ]


class RelayDeleteRuleTests(unittest.TestCase):
    def test_serving_prompt_and_never_bullet_have_rule(self):
        self.assertEqual(check_rule(RELAY), [])

    def test_removed_rule_lines_report_missing(self):
        body = RELAY.read_text(encoding="utf-8")
        rules = [line for line in body.splitlines(keepends=True) if "literal path" in line]
        self.assertEqual(len(rules), 2)
        with tempfile.TemporaryDirectory(prefix="relay-delete-rule-") as tmp:
            copy = Path(tmp) / "relay.md"
            for removed, expected in (
                (rules[:1], ["step 4"]),
                (rules[1:], ["Never bullet"]),
                (rules, ["step 4", "Never bullet"]),
            ):
                with self.subTest(missing=expected):
                    mutated = body
                    for line in removed:
                        mutated = mutated.replace(line, "")
                    copy.write_text(mutated, encoding="utf-8")
                    self.assertEqual(check_rule(copy), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
