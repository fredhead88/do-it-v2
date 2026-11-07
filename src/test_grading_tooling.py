#!/usr/bin/env python3
"""Regressions for complete grader criteria, shell chains and tooling ownership."""
import argparse
import copy
import evidence_revision
import runner_evidence
from datetime import datetime, timedelta, timezone
import itertools
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_runner
import checks
import dispatch
import fold
import grader_packet
import packet
import verification
import test_build_runner as runner_fixture

SOURCE = pathlib.Path(__file__).resolve().parent.parent


def criterion_body(acceptance=False):
    prefix = "## Acceptance\nIntroductory text.\n" if acceptance else "## Requirements\nR1: fixture.\n"
    return (prefix + "AC1 [backend]: first.\ncheck: scratch\nreview_path: first\n\n"
            "AC2 [backend]: words " + " between ".join(
                ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
            + " words.\ncheck: scratch\nreview_path: second\n```text\n"
            "# a fenced heading\nAC99 [backend]: fenced example.\n```\n\n"
            "AC3 [backend]: last.\ncheck: scratch\nreview_path: third\n\n"
            "## Verification\n```bash\nprintf 'done\\n'\n```\n")


def fixture_chain():
    return ('cd "$(git rev-parse --show-toplevel)" &&\n'
            'a() { printf "X AC1 ok\\nX AC2 ok\\n"; } &&\n'
            'b() { :; } &&\n# AC1\nsweep_out="$(a && b)" &&\n'
            'printf \'%s\\n\' "$sweep_out" | grep -qx \'X AC1 ok\' &&\n'
            '# AC2\nprintf \'%s\\n\' "$sweep_out" | grep -qx \'X AC2 ok\' &&\n'
            'printf \'%s\\n\' "a && b" | grep -q \'a && b\'\n')


def chain_spec():
    return ('## Acceptance\nAC1 [backend]: first.\n'
            'check: data_source=scratch customer_data=false profile=repo-venv '
            'timing=pre-merge answered_by=script\n'
            'AC2 [backend]: second.\n'
            'check: data_source=scratch customer_data=false profile=repo-venv '
            'timing=pre-merge answered_by=script\n'
            'AC3 [backend]: review command.\nreview_path: run `bash -c true`\n'
            'AC4 [backend]: judgment.\nreview_path: internal-surface\n'
            '## Verification\n```bash\n' + fixture_chain() + '```\n')


def verdict(ac="AC1", reason_code="tooling-defect", **extra):
    return {"ac": ac, "verdict": "cannot-assess", "reason": "fixture", "reason_code": reason_code, **extra}


class GradingToolingTests(unittest.TestCase):
    def test_ac1_criteria_full_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            for acceptance in (False, True):
                path = pathlib.Path(temporary) / f"spec-{acceptance}.md"
                path.write_text(criterion_body(acceptance))
                body = path.read_text()
                lines = packet.criteria(body)
                joined = "\n".join(lines)
                for ac, end in (("AC1", "AC2"), ("AC2", "AC3"), ("AC3", "## Verification")):
                    expected = body[body.index(ac + " ["):body.index(end + (" [" if end.startswith("AC") else ""))]
                    start = lines.index(next(line for line in lines if line.startswith(ac + " [")))
                    stop = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(end + " [")), len(lines))
                    self.assertEqual("\n".join(lines[start:stop]) + "\n", expected)
                self.assertNotIn("## Verification", joined)
                self.assertEqual("Introductory text." in joined, acceptance)
        plain = "## Requirements\nplain\n## Verification\n```bash\nprintf 'done\\n'\n```\n## Notes\nend\n"
        self.assertEqual(packet.section(plain, "Verification"), "```bash\nprintf 'done\\n'\n```")
        disguised = "## Requirements\nwords\u2028## Verification\nstill requirements\n## Verification\nreal\n"
        self.assertEqual(packet.section(disguised, "Verification"), "real")
        bulleted = "## Acceptance\n- AC1 [backend]: legacy bullet.\nreview_path: retained\n## Verification\nunused\n"
        self.assertEqual(packet.criteria(bulleted), ["- AC1 [backend]: legacy bullet.", "review_path: retained"])
        with self.assertRaises(SystemExit):
            packet.criteria(bulleted.replace("## Acceptance\n", ""))

    def test_ac2_packet_refuses_incomplete_criterion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "content").mkdir()
            spec = root / "content" / "L-spec-9000.md"
            spec.write_text(criterion_body())
            card = root / "content" / "L-card-9000.md"
            card.write_text("verify exit 0\n")
            events = [{"type": "spec-written", "subject": "L-spec-9000", "path": str(spec)}]
            with patch.object(fold, "read_events", return_value=events), patch.object(packet, "CONTENT", root / "content"), \
                    patch.object(packet.Ctx, "card_file", return_value=card), \
                    patch.object(packet.Ctx, "worktree", return_value=str(root)), \
                    patch.object(packet.Ctx, "charter_id", return_value=None), \
                    patch.object(packet.models, "resolve", return_value={"backend": "codex"}), \
                    patch.object(grader_packet, "packet_items", side_effect=lambda c, items: items):
                ctx = packet.Ctx(argparse.Namespace(subject="L-spec-9000", role="grader", charter=None, project="demo"))
                result = "\n".join(packet.p_grader(ctx))
                body = spec.read_text()
                self.assertIn(body[body.index("AC2 ["):body.index("AC3 [")], result)
                shortened = packet.criteria(body)
                start = shortened.index(next(line for line in shortened if line.startswith("AC2 [")))
                end = shortened.index(next(line for line in shortened if line.startswith("AC3 [")))
                shortened[start:end] = shortened[start:start + 1]
                with patch.object(packet, "criteria", return_value=shortened):
                    with self.assertRaises(SystemExit) as caught:
                        packet.p_grader(ctx)
                self.assertIn("AC2", str(caught.exception))
                self.assertIn(str(spec), str(caught.exception))
                self.assertFalse((root / "packets").exists())
                with patch.object(ctx, "evs", ctx.evs + [{"type": "owed-ac", "subject": ctx.a.subject,
                                                       "actor": "spec-writer", "criterion": "AC2"}]):
                    omitted = "\n".join(packet.p_grader(ctx))
                self.assertNotIn("review_path: second", omitted)
                self.assertNotIn("AC99 [", omitted)
                self.assertIn("review_path: third", omitted)

    def test_ac3_chain_is_one_command(self):
        text = chain_spec().replace('review_path: run `bash -c true`', 'review_path: internal-surface')
        commands, _ = build_runner._commands(text, "demo", None)
        self.assertEqual(commands, [(None, verification.block(text))])
        self.assertEqual(build_runner._commands(text, "demo", {"AC2"})[0], commands)
        self.assertEqual(build_runner._commands(chain_spec(), "demo", {"AC3"})[0], [("AC3", "bash -c true")])
        selected, judgment = build_runner._commands(chain_spec(), "demo", {"AC2", "AC4"})
        self.assertEqual(selected, commands)
        self.assertEqual(judgment, ["AC4"])
        fixture = runner_fixture.BuildRunnerTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.spec(text=chain_spec())
        bundle, _ = fixture.bundle()
        chain = next(row for row in bundle["commands"] if row["criterion"] is None)
        self.assertEqual(chain["covers"], ["AC1", "AC2"])
        review = next(row for row in bundle["commands"] if row["criterion"] == "AC3")
        self.assertNotIn("covers", review)
        self.assertTrue(all(row["exit"] == 0 for row in bundle["commands"]))

    def test_ac4_shared_variables_and_quoted_and_survive(self):
        text = chain_spec().replace('review_path: run `bash -c true`', 'review_path: internal-surface')
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["git", "init", "-q", temporary], check=True)
            def run(command):
                return subprocess.run(["/bin/bash", "-c", command], cwd=temporary, capture_output=True).returncode
            self.assertTrue(all(run(command) == 0 for _, command in build_runner._commands(text, "demo", None)[0]))
            self.assertTrue(any(run(command) != 0 for _, command in checks._verification_statements(text)))

    def test_ac5_validate_output_covers(self):
        bundle = {"bundle": {"commands": [{"criterion": None, "covers": ["AC1", "AC2"]}]}}
        for row in (verdict(reason_code="tool-failed"), verdict("AC2", "evidence-missing", missing_capability="unknown")):
            self.assertIn("bundle-covered", grader_packet.validate_output({"verdicts": [row]}, bundle, criteria=[row["ac"]]))
        self.assertIsNone(grader_packet.validate_output({"verdicts": [verdict("AC2", "evidence-missing")]}, bundle, criteria=["AC2"]))
        self.assertIsNone(grader_packet.validate_output({"verdicts": [verdict("AC9", "tool-failed")]}, bundle, criteria=["AC9"]))

    def test_2046_ac1_invocations_and_latest(self):
        sid, sha = "L-spec-9000", "fixture-sha"
        def row(kind, stamp, invocation=None, **fields):
            event = dict(subject=sid, sha=sha, type=kind, ts=stamp, **fields)
            return evidence_revision.stamp(event, invocation) if invocation else event
        first_id, second_id = (evidence_revision.new_invocation_id() for _ in range(2))
        self.assertTrue(first_id and second_id)
        self.assertNotEqual(first_id, second_id)
        first = row("evidence-bundle", "2026-11-04T00:00:00Z", first_id)
        second = row("evidence-bundle-failed", "2026-11-04T00:01:00Z", second_id)
        self.assertEqual(evidence_revision.latest([second, first], sid, sha), second)
        legacy = row("evidence-bundle", second["ts"])
        for rows in ([second, legacy], [legacy, second]):
            self.assertEqual(evidence_revision.latest(rows, sid, sha), second)
        bundled = row("evidence-bundle", second["ts"], bundle={"invocation_id": first_id})
        self.assertEqual(evidence_revision.latest([second, bundled], sid, sha)["invocation_id"], first_id)
        later = row("evidence-bundle", "2026-11-04T00:02:00Z")
        result = evidence_revision.latest([first, later], sid, sha)
        self.assertIsNone(result["invocation_id"])
        self.assertNotIn("invocation_id", later)
        self.assertEqual(evidence_revision.stamp(legacy, first_id)["invocation_id"], first_id)
        self.assertNotIn("invocation_id", legacy)
        self.assertIsNone(evidence_revision.latest([first], "other", sha))
        self.assertIsNone(evidence_revision.latest([first], sid, "other"))
        unrelated = {**first, "type": "build-done", "ts": later["ts"]}
        self.assertEqual(evidence_revision.latest([first, unrelated], sid, sha), first)

    def test_2046_ac1_compact_is_additive(self):
        tip = [{"failing_ids": ["bad"], "segments": [{"results": {"bad": 1, "good": 2},
                 "collected": {"bad": "a.py", "good": "a.py"}}]}]
        base = copy.deepcopy(tip)
        plain = runner_evidence.compact(copy.deepcopy(tip), copy.deepcopy(base))
        stamped = runner_evidence.compact(copy.deepcopy(tip), copy.deepcopy(base), invocation_id="X")
        self.assertEqual(stamped.pop("invocation_id"), "X")
        self.assertEqual(stamped, plain)
        self.assertNotIn("invocation_id", plain)
        self.assertEqual(plain["commands"][0]["segments"][0],
                         {"results": {"bad": 1}, "collected": {"bad": "a.py"}, "n_collected": 2})

    def test_2046_ac3_verdict_ids(self):
        ids = [f"AC{i}" for i in range(1, 10)]
        text = "## Acceptance\n" + "\n".join(f"**{ac}** [backend]: works." for ac in ids)
        self.assertEqual(grader_packet.spec_criteria_ids(text), ids)
        self.assertEqual(grader_packet.spec_criteria_ids(criterion_body(True)), ["AC1", "AC2", "AC3"])
        def validate(rows, **kwargs):
            return grader_packet.validate_output({"verdicts": [verdict(ac) for ac in rows]}, None, **kwargs)
        error = validate(ids[:7], criteria=ids)
        self.assertTrue(error.startswith("verdicts: missing AC8, AC9 (7 of 9)"), error)
        self.assertIsNone(validate(ids, criteria=ids))
        self.assertIn("duplicate AC1", validate(ids + ["AC1"], criteria=ids))
        self.assertIn("extra AC10", validate(ids + ["AC10"], criteria=ids))
        self.assertIn("criteria", validate(["AC1"]))
        self.assertIn("criteria", grader_packet.validate_output({"verdicts": []}, {"bundle": {"spec": None}}))
        self.assertIn("criteria", validate([], criteria=[]))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"DOIT_ROOT": temporary}):
            path = pathlib.Path(temporary) / "content" / "L-spec-9000.md"
            path.parent.mkdir()
            bundle = {"bundle": {"spec": "L-spec-9000"}}
            output = {"verdicts": [verdict(ac) for ac in ids]}
            self.assertIn("criteria", grader_packet.validate_output(output, bundle))
            path.write_text(text)
            self.assertIsNone(grader_packet.validate_output(output, bundle))
            path.write_text("No criteria here.")
            self.assertIn("criteria", grader_packet.validate_output(output, bundle))

    def test_ac6_instructions_name_the_cases(self):
        for path, words in (("agents/grader.md", ["tooling-defect", "covers", "whole-chain"]),
                            ("agents/executor.md", ["grading-tooling", "re-grade once after the tooling fix"])):
            text = (SOURCE / path).read_text()
            for word in words:
                self.assertIn(word, text)

    def test_ac7_tooling_defect_opens_executor_escalation(self):
        schema = json.loads((SOURCE / "agents/grader.schema.json").read_text())
        self.assertIn("tooling-defect", schema["properties"]["verdicts"]["items"]["properties"]["reason_code"]["enum"])
        mixed = {"verdicts": [verdict(missing_capability="unknown")]}
        self.assertEqual(grader_packet.validate_output(mixed, None, criteria=["AC1"]),
                         "verdicts: tooling-defect row cannot carry missing_capability")
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with patch.object(fold, "ROOT", root), patch.object(fold, "EVENTS", root / "events"), patch.dict(os.environ, {
                    "DOIT_ROOT": str(root), "DOIT_LEDGER_FILE": "L-grader-9000.jsonl", "DOIT_PROJECT": "demo", "DOIT_NO_POKE": "1"}):
                args = argparse.Namespace(subject="L-spec-9000")
                ticks = itertools.count()
                def append(kind, subject, fields):
                    # Causal order across actor files must not depend on tied timestamps.
                    at = datetime(2026, 10, 25, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))
                    with patch.object(fold, "NOW", at):
                        fold.append([kind, subject] + [f"{key}:={json.dumps(value)}" for key, value in fields.items()])
                def drive(row):
                    out = {"verdicts": [row], "matches_intent": "yes", "card_ok": "yes", "could_not_run": False,
                           "contamination": False, "declarations": [], "checkers": []}
                    for kind, fields in dispatch.events_for("grader", out, args, {}):
                        fields = dict(fields)
                        subject = fields.pop("subject", args.subject)
                        append(kind, subject, fields)
                    return [json.loads(line) for line in (root / "events" / "L-grader-9000.jsonl").read_text().split("\n") if line]
                events = drive(verdict())
                self.assertFalse(any(event["type"] == "capability-hold" for event in events))
                self.assertEqual(next(e for e in events if e["type"] == "verdict")["missing_capability"], [])
                escalation = next(e for e in events if e["type"] == "escalation-blocking")
                for key, expected in {"subject": "grading-tooling:L-spec-9000", "kind": "grading-tooling",
                                      "owner": "executor", "default": "re-grade once after the tooling fix", "criteria": ["AC1"]}.items():
                    self.assertEqual(escalation[key], expected)
                self.assertTrue(escalation["deadline"])
                self.assertEqual(escalation["revert"], "doit append unblocked grading-tooling:L-spec-9000")
                events = drive(verdict())
                self.assertEqual(sum(e["type"] == "escalation-blocking" for e in events), 1)
                for resolution in ("decision", "unblocked"):
                    with patch.dict(os.environ, {"DOIT_LEDGER_FILE": "L-executor-9000.jsonl"}):
                        append(resolution, "grading-tooling:L-spec-9000", {"why": "fixture", "revert": "fixture"})
                    events = drive(verdict())
                self.assertEqual(sum(e["type"] == "escalation-blocking" for e in events), 3)
                args.subject = "L-spec-9001"
                events = drive(verdict(reason_code="tool-failed"))
                self.assertEqual(next(e for e in events if e["type"] == "capability-hold")["subject"],
                                 "capability:unknown:L-spec-9001")


if __name__ == "__main__":
    unittest.main()
