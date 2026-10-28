#!/usr/bin/env python3
"""Direct, stdlib-only regressions for review-command deduplication."""
import json
import unittest

import build_runner
import checks
import grading_env
import verification


def pre_change_commands(text, project, criteria):
    """Frozen pre-change _commands for the unchanged-path comparisons."""
    chain = verification.block(text) or ""
    tagged = build_runner._chain_covers(chain)
    blocks = grading_env._criterion_blocks(text)
    commands, judgment = [], []
    for record in checks.records(text, project):
        criterion = record["criterion"]
        if criteria is not None and criterion not in criteria:
            continue
        if criterion in tagged:
            review = checks._review_command(blocks.get(criterion, ""))
            if review:
                commands.append((criterion, review))
        elif record["runner"] != "build":
            judgment.append(criterion)
        else:
            commands.extend((criterion, command) for command in record["commands"])
    if chain and (criteria is None or set(tagged).intersection(criteria)):
        commands.append((None, chain))
    if not commands:
        raise build_runner.Undetermined("no build command or Verification block")
    return commands, judgment


def spec(chain, review="python3 tests/foo.py", tagged=True, backticks=True):
    review_path = f"run `{review}`" if backticks else f"run {review}"
    return ("## Acceptance criteria\n"
            "AC1 [backend]: command fixture.\n"
            f"review_path: {review_path}\n"
            "AC2 [backend]: human judgment.\n"
            "review_path: inspect the result\n"
            "## Verification\n```bash\n"
            + ("# AC1\n" if tagged else "") + chain + "\n```\n")


class ReviewDedupeTests(unittest.TestCase):
    def test_ac1_commands_already_in_chain(self):
        cases = [
            ("exact", "python3 tests/foo.py", "python3 tests/foo.py"),
            ("continuation", "python3 \\\n    tests/foo.py", "python3 tests/foo.py"),
            ("chain spacing", "  python3   \ttests/foo.py  ", "python3 tests/foo.py"),
            ("review spacing", "python3 tests/foo.py", " python3   \ttests/foo.py "),
        ]
        for operator in ("\n", "&&", "||", ";", "|"):
            cases.append((f"operator {operator!r}",
                          f"true {operator} python3 tests/foo.py {operator} true",
                          "python3 tests/foo.py"))
        for label, chain, review in cases:
            for backticks in (True, False):
                text = spec(chain, review, backticks=backticks)
                for criteria in (None, {"AC1"}):
                    with self.subTest(label=label, backticks=backticks, criteria=criteria):
                        actual = build_runner._commands(text, "demo", criteria)
                        expected = ([(None, verification.block(text))],
                                    ["AC2"] if criteria is None else [])
                        self.assertEqual(actual, expected)
                print("AC1", label, json.dumps(actual))
                print("L-spec-1789 AC1 ok")

    def test_ac2_other_commands_match_pre_change(self):
        cases = [
            ("absent", "python3 tests/bar.py", True),
            ("longer command", "python3 tests/foo.py::test_a", True),
            ("comment only", "  # python3 tests/foo.py\ntrue", True),
            ("comment after operator", "# note && python3 tests/foo.py\ntrue", True),
            ("echo argument", "echo python3 tests/foo.py", True),
            ("quoted echo argument", 'echo "python3 tests/foo.py"', True),
            ("untagged duplicate", "python3 tests/foo.py", False),
            ("untagged separate", "python3 tests/bar.py", False),
        ]
        for label, chain, tagged in cases:
            text = spec(chain, tagged=tagged)
            for criteria in (None, {"AC1"}):
                with self.subTest(label=label, criteria=criteria):
                    actual = build_runner._commands(text, "demo", criteria)
                    before = pre_change_commands(text, "demo", criteria)
                    self.assertEqual(actual, before)
                    self.assertIn(("AC1", "python3 tests/foo.py"), actual[0])
                print("AC2", label, "pre-change == current", json.dumps(actual))
                print("L-spec-1789 AC2 ok")

    def test_empty_selection_remains_undetermined(self):
        text = spec("python3 tests/foo.py")
        for commands in (pre_change_commands, build_runner._commands):
            with self.assertRaisesRegex(build_runner.Undetermined,
                                        "no build command or Verification block"):
                commands(text, "demo", {"AC9"})
        print("L-spec-1789 AC2 ok")


if __name__ == "__main__":
    unittest.main()
