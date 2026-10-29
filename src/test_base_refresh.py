#!/usr/bin/env python3
"""Scratch-only regression checks for builder base refreshes (L-spec-1784)."""
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import base_refresh
import packet


class Context:
    def __init__(self, repository, events, **args):
        self.evs = events
        self._spec = pathlib.Path(repository) / "refresh-spec.md"
        self.a = SimpleNamespace(role="builder", base_sha=None, repo=str(repository),
                                 subject="L-spec-1784")
        for key, value in args.items():
            setattr(self.a, key, value)

    def spec_file(self):
        spec = self._spec
        if not spec.exists():
            spec.write_text("# scratch\n")
        return spec

    def project(self):
        return "scratch"

    def all_of(self, kind):
        return [event for event in self.evs if event.get("type") == kind]

    def last(self, kind):
        return next(iter(reversed(self.all_of(kind))), None)


class BaseRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="l1784-refresh-")
        self.addCleanup(temporary.cleanup)
        self.repo = pathlib.Path(temporary.name)
        self.git("init", "-q", "--initial-branch=release/main")
        self.git("config", "user.name", "Scratch test")
        self.git("config", "user.email", "scratch@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "earliest")
        self.earliest = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "--allow-empty", "-m", "current main")
        self.tip = self.git("rev-parse", "HEAD")
        config = patch("look.load_toml", return_value={
            "push": {"main": {"scratch": "release/main"}}})
        config.start()
        self.addCleanup(config.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def context(self, *kinds, **args):
        # Identical timestamps deliberately exercise list ordering, not time.
        events = [{"type": kind, "ts": "2026-10-28T00:00:00Z"} for kind in kinds]
        for event in events:
            if event["type"] == "build-done":
                event["base_sha"] = self.earliest
                event["status"] = "DONE"
        return Context(self.repo, events, **args)

    def test_ac1_conflict_and_memoization(self):
        context = self.context("build-done", "conflict-rework")
        with patch("base_refresh.subprocess.run", wraps=subprocess.run) as run:
            self.assertEqual(base_refresh.refresh_tip(context), self.tip)
            self.assertEqual(packet.verify_base_sha(context), self.tip)
            self.assertEqual(base_refresh.refresh_tip(context), self.tip)
            self.assertEqual(run.call_count, 2)
        self.assertEqual(base_refresh.refresh_tip(self.context("conflict-rework")), self.tip)
        self.assertIsNone(base_refresh.refresh_tip(
            self.context("conflict-rework", "build-done")))
        print("L-spec-1784 AC1 ok")

    def test_ac2_dependency_release_order(self):
        context = self.context("dependency-hold", "build-done", "unblocked")
        self.assertEqual(base_refresh.refresh_tip(context), self.tip)
        self.assertEqual(packet.verify_base_sha(context), self.tip)
        self.assertEqual(base_refresh.refresh_tip(
            self.context("dependency-hold", "unblocked")), self.tip)
        for kinds in (("build-done", "unblocked"),
                      ("dependency-hold", "unblocked", "build-done"),
                      ("build-done", "unblocked", "dependency-hold")):
            self.assertIsNone(base_refresh.refresh_tip(self.context(*kinds)))
        print("L-spec-1784 AC2 ok")

    def test_ac3_must_fix_keeps_earliest_base(self):
        for kind in ("must-fix", "rejected-criterion"):
            context = self.context("build-done", "build-done", kind)
            context.evs[1]["base_sha"] = self.tip
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertEqual(packet.verify_base_sha(context), self.tip)
            context.evs[1]["base_sha_explicit"] = True
            self.assertEqual(packet.verify_base_sha(context), self.tip)
        print("L-spec-1784 AC3 ok")

    def test_ac4_explicit_pin_and_other_roles(self):
        context = self.context("build-done", "conflict-rework", base_sha=self.earliest)
        self.assertEqual(packet.verify_base_sha(context), self.tip)
        for role in ("grader", None):
            context = self.context("build-done", "conflict-rework", role=role)
            if role is None:
                del context.a.role
            with patch("base_refresh.subprocess.run") as run:
                self.assertIsNone(base_refresh.refresh_tip(context))
                self.assertEqual(packet.verify_base_sha(context), self.earliest)
                run.assert_not_called()
        context = self.context()
        del context.evs
        self.assertIsNone(base_refresh.refresh_tip(context))
        context = self.context()
        context.evs = None
        self.assertIsNone(base_refresh.refresh_tip(context))
        print("L-spec-1784 AC4 ok")

    def test_ac5_git_failure_and_invalid_results(self):
        context = self.context("build-done", "conflict-rework")
        context.a.repo = str(self.repo / "not-a-repository")
        with patch("base_refresh.subprocess.run", wraps=subprocess.run) as run:
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertEqual(run.call_count, 2)
            with self.assertRaisesRegex(SystemExit, "stale-base"):
                packet.verify_base_sha(context)
        for main in ("bad ref", "main;exit", "", None):
            with patch("look.load_toml", return_value={"push": {"main": {"scratch": main}}}), \
                    patch("base_refresh.subprocess.run") as run:
                context = self.context("conflict-rework")
                self.assertIsNone(base_refresh.refresh_tip(context))
                with self.assertRaisesRegex(SystemExit, "stale-base"):
                    packet.verify_base_sha(context)
                run.assert_not_called()
        with patch("base_refresh.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="not-a-commit\n")):
            context = self.context("conflict-rework")
            self.assertIsNone(base_refresh.refresh_tip(context))
            with self.assertRaisesRegex(SystemExit, "stale-base"):
                packet.verify_base_sha(context)
        with patch("base_refresh.subprocess.run", side_effect=OSError("git unavailable")):
            context = self.context("conflict-rework")
            self.assertIsNone(base_refresh.refresh_tip(context))
            with self.assertRaisesRegex(SystemExit, "stale-base"):
                packet.verify_base_sha(context)
        with patch("base_refresh.ROOT", self.repo.parent), \
                patch("look.load_toml", return_value={"push": {"main": {}}}), \
                patch("base_refresh.subprocess.run") as run:
            context = self.context("conflict-rework", repo=None)
            self.assertIsNone(base_refresh.refresh_tip(context))
            with self.assertRaisesRegex(SystemExit, "stale-base"):
                packet.verify_base_sha(context)
            run.assert_not_called()
        print("L-spec-1784 AC5 ok")

    def test_l1798_ac1_wave_order_and_rebase_ruling(self):
        def pre_change_refresh(events):
            held, due = False, False
            for event in events:
                kind = event.get("type")
                if kind == "build-done":
                    due = False
                elif kind == "dependency-hold":
                    held = True
                elif kind == "conflict-rework" or (kind == "unblocked" and held):
                    due = True
            return self.tip if due else None

        wave = {"type": "spawn-failed", "reason": "wave-order: earlier wave pending"}
        ruling = {"type": "decision", "rebase": "yes"}
        done = {"type": "build-done", "base_sha": self.earliest, "status": "DONE"}
        blocked = {"type": "build-blocked"}
        cases = (
            ("wave-order", [wave], self.tip),
            ("bare wave-order", [{"type": "spawn-failed", "reason": "wave-order"}],
             self.tip),
            ("completed wave-order", [wave, done], None),
            ("rebase after build", [done, ruling], self.tip),
            ("rework alone", [{"type": "decision", "rework": "yes"}], None),
            ("completed rebase", [ruling, done], None),
            ("blocked wave-order", [wave, blocked], self.tip),
            ("blocked rebase", [done, ruling, blocked], self.tip),
        )
        for name, events, expected in cases:
            with self.subTest(case=name):
                events = [{"type": "spec-written", "base_sha": self.earliest}, *events]
                self.assertEqual(base_refresh.refresh_tip(Context(self.repo, events)), expected)
                self.assertIsNone(pre_change_refresh(events))
        print("L-spec-1798 AC1 ok")

    def test_l1798_ac2_unrelated_failures_and_roles(self):
        for reason in ("bundle-pending", "duplicate-dispatch", "timeout", "",
                       None, 1798, "prefix wave-order"):
            with self.subTest(reason=reason), patch("base_refresh.subprocess.run") as run:
                events = [{"type": "spawn-failed", "reason": reason}]
                self.assertIsNone(base_refresh.refresh_tip(Context(self.repo, events)))
                run.assert_not_called()
        for event in ({"type": "spawn-failed"},
                      {"type": "decision", "rebase": "no"},
                      {"type": "decision", "rebase": True},
                      {"type": "decision", "rebase": "YES"},
                      {"type": "unblocked", "rebase": "yes"}):
            with self.subTest(event=event), patch("base_refresh.subprocess.run") as run:
                self.assertIsNone(base_refresh.refresh_tip(Context(self.repo, [event])))
                run.assert_not_called()
        for role in ("grader", "reviewer", "planner", None):
            for event in ({"type": "spawn-failed", "reason": "wave-order"},
                          {"type": "decision", "rebase": "yes"}):
                with self.subTest(role=role, event=event):
                    context = Context(self.repo, [event], role=role)
                    if role is None:
                        del context.a.role
                    with patch("base_refresh.subprocess.run") as run:
                        self.assertIsNone(base_refresh.refresh_tip(context))
                        run.assert_not_called()
        print("L-spec-1798 AC2 ok")


class R7Fixture(unittest.TestCase):
    """Shared scratch topology; no production repository, ledger or backend."""
    def setUp(self):
        import dispatch, fold, json, os
        tmp = tempfile.TemporaryDirectory(prefix="r7-current-tip-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.repo = self.root / "repos" / "scratch"
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Scratch")
        self.git("config", "user.email", "scratch@example.invalid")
        self.git("commit", "-q", "--allow-empty", "-m", "old")
        self.old = self.git("rev-parse", "HEAD")
        self.git("branch", "realm")
        self.git("update-ref", "refs/remotes/origin/main", self.old)
        self.git("commit", "-q", "--allow-empty", "-m", "new")
        self.tip = self.git("rev-parse", "HEAD")
        self.sid = "L-spec-9850"
        self.wt = self.root / "worktrees" / "scratch" / self.sid.lower()
        self.git("worktree", "add", "-q", "-b", self.sid.lower(), str(self.wt), self.tip)
        self.content = self.root / "content"
        self.content.mkdir()
        self.spec = self.content / (self.sid + ".md")
        self.spec.write_text("# scratch\n## Interfaces\nWrites: src/scratch.py\n"
                             "## Verification\n```\n/usr/bin/true && echo $BASE\n```\n")
        (self.root / "events").mkdir()
        self.rows = [dict(v=1, type="spec-written", subject=self.sid, actor="operator",
                          ts="2026-10-29T00:00:00Z", project="scratch", path=str(self.spec),
                          footprint=["src/scratch.py"], base_sha=self.old)]
        self.store()
        for module, attrs in (
            (packet, dict(ROOT=self.root, CONTENT=self.content, PACKETS=self.root / "packets")),
            (dispatch, dict(ROOT=self.root, CONTENT=self.content, EVENTS=self.root / "events")),
            (fold, dict(ROOT=self.root, EVENTS=self.root / "events")),
            (base_refresh, dict(ROOT=self.root)),
        ):
            swap = patch.multiple(module, **attrs)
            swap.start()
            self.addCleanup(swap.stop)
        swap = patch("look.load_toml", return_value={"push": {"main": {"scratch": "main"}}})
        swap.start()
        self.addCleanup(swap.stop)
        swap = patch.dict(os.environ, {"DOIT_ROOT": str(self.root), "DOIT_NO_POKE": "1"})
        swap.start()
        self.addCleanup(swap.stop)
        swap = patch("packet.preflight_lines", return_value=["preflight undetermined: scratch"])
        swap.start()
        self.addCleanup(swap.stop)

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def store(self):
        import fold, json
        from datetime import datetime, timedelta, timezone
        for path in getattr(self, "_seed_files", []):
            path.unlink(missing_ok=True)
        grouped = {}
        for index, row in enumerate(self.rows):
            allowed = fold.EMITS.get(row["type"], {"operator"})
            actor = "operator" if "operator" in allowed else sorted(allowed)[0]
            value = dict(row, actor=actor, ts=(datetime(2026, 10, 29, tzinfo=timezone.utc)
                                             + timedelta(seconds=index)).isoformat())
            grouped.setdefault(actor, []).append(value)
        self._seed_files = []
        for actor, rows in grouped.items():
            path = self.root / "events" / ("L-" + actor + "-fixture.jsonl")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self._seed_files.append(path)

    def context(self, **kw):
        args = dict(role="builder", subject=self.sid, repo=str(self.repo),
                    worktree=str(self.wt), project="scratch", base_sha=None,
                    hint=None, charter=None, conventions=None, produces=None)
        args.update(kw)
        return packet.Ctx(SimpleNamespace(**args))

    def render(self, **kw):
        return "\n".join(packet.p_builder(self.context(**kw)))

    def signed(self, ac, body):
        import hashlib, json
        payload = json.dumps(body, sort_keys=True)
        print(json.dumps(dict(url="cli://r7/" + ac, status=0,
              body_sha256=hashlib.sha256(payload.encode()).hexdigest(), body_excerpt=payload)))


class R7PacketTests(R7Fixture):
    def wrapper(self, context, block, declaration=""):
        self.spec.write_text(declaration + "\n# scratch\n## Verification\n```bash\n" + block + "```\n")
        path = packet.write_verify_script(context)
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_bytes().endswith(block.encode()))
        return path

    def execute(self, path):
        # Reopen the artifact in an independent shell process.
        return subprocess.run(["bash", str(path)], cwd=self.wt, capture_output=True, text=True)

    def test_current_tip(self):
        for branch in ("main", "master"):
            if branch == "master":
                self.git("branch", "master", self.tip)
            with patch("look.load_toml", return_value={"push": {"main": {"scratch": branch}}}):
                self.git("checkout", "-q", "realm")
                self.assertEqual(packet.current_tip(self.repo, "scratch"), self.tip)
        with patch("look.load_toml", return_value={"push": {"main": {}}}):
            with self.assertRaises(ValueError):
                packet.current_tip(self.repo, "scratch")
        for project in (None, "..", "bad project"):
            with self.assertRaises(ValueError):
                packet.current_tip(self.repo, project)
        for ref in ("missing", "realm~1", "", None, "--help", "a..b"):
            with patch("look.load_toml", return_value={"push": {"main": {"scratch": ref}}}):
                with self.assertRaises(ValueError):
                    packet.current_tip(self.repo, "scratch")
        for failure in (OSError("git failed"), subprocess.TimeoutExpired("git", 10)):
            with patch("packet.subprocess.run", side_effect=failure):
                with self.assertRaises(ValueError):
                    packet.current_tip(self.repo, "scratch")
        for result in (SimpleNamespace(returncode=1, stdout=self.old),
                       SimpleNamespace(returncode=0, stdout="not-a-commit")):
            with patch("packet.subprocess.run", return_value=result):
                with self.assertRaises(ValueError):
                    packet.current_tip(self.repo, "scratch")
        # A real ref pointing to a blob cannot become a commit pin.
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input="blob", capture_output=True, text=True, check=True).stdout.strip()
        (self.repo / ".git" / "refs" / "heads" / "blob").write_text(blob + "\n")
        with patch("look.load_toml", return_value={"push": {"main": {"scratch": "blob"}}}):
            with self.assertRaises(ValueError):
                packet.current_tip(self.repo, "scratch")
        c = self.context()
        self.assertEqual(packet.verify_base_sha(c), self.tip)
        self.git("commit", "-q", "--allow-empty", "-m", "advance", repo=self.wt)
        newer = self.git("rev-parse", "HEAD", repo=self.wt)
        self.git("update-ref", "refs/heads/main", newer)
        text = "\n".join(packet.p_builder(c))
        self.assertIn("PINNED_BASE_SHA: " + self.tip, text)
        self.assertIn("`base_sha` = " + self.tip, text)
        self.assertIn("BASE=" + self.tip, (self.content / ("verify-" + self.sid + "-builder.sh")).read_text())
        self.assertEqual(packet.verify_base_sha(self.context()), newer)

        # All roles see a captured pin for exactly four tuples; other tuples
        # must preserve native stdout, stderr and return code, including failure.
        self.git("branch", "feature-a", self.old)
        self.git("branch", "feature-b", self.tip)
        proofs = []
        for branch, declaration in (("main", ""), ("master", ""), ("realm", "Base: realm")):
            self.git("update-ref", "refs/heads/" + branch, self.tip)
            self.git("update-ref", "refs/remotes/origin/" + branch, self.old)
            config = "main" if declaration else branch
            with patch("look.load_toml", return_value={"push": {"main": {"scratch": config}}}):
                contexts = []
                for role in ("builder", "grader", "reviewer"):
                    context = self.context(role=role, base_sha=self.tip if role != "builder" else None)
                    forms = [("HEAD", branch), (branch, "HEAD"),
                             ("HEAD", "origin/" + branch), ("origin/" + branch, "HEAD")]
                    block = " && ".join(["echo $BASE"] + [
                        'echo "$(git merge-base ' + " ".join(form) + ')"' for form in forms]) + "\n"
                    path = self.wrapper(context, block, declaration)
                    contexts.append((context, path, path.read_bytes()))
                self.git("update-ref", "refs/heads/" + branch, newer)
                for context, path, captured in contexts:
                    self.assertEqual(path.read_bytes(), captured)
                    result = self.execute(path)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [self.tip] * 5)
                    for form in forms:
                        direct = self.execute(self.wrapper(context, "git merge-base " + " ".join(form) + "\n", declaration))
                        self.assertEqual((direct.returncode, direct.stdout), (0, self.tip + "\n"))
                    unsupported = [("--is-ancestor", self.tip, self.old),
                                   ("--fork-point", branch, "HEAD"),
                                   ("--all", "HEAD", branch),
                                   ("--octopus", "HEAD", branch, "feature-a"),
                                   ("feature-a", "feature-b"), ("missing-ref", "HEAD")]
                    for args in unsupported:
                        native = subprocess.run(["git", "merge-base", *args], cwd=self.wt,
                                                capture_output=True, text=True)
                        block = "BASE=$(git merge-base HEAD " + branch + ") && git merge-base " + " ".join(args) + "\n"
                        actual = self.execute(self.wrapper(context, block, declaration))
                        self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                         (native.returncode, native.stdout, native.stderr), args)
                        proofs.append(dict(branch=branch, role=context.a.role, args=args,
                                           status=actual.returncode, stdout=actual.stdout, stderr=actual.stderr))
                    native = subprocess.run(["git", "merge-base", "HEAD", "origin/" + branch],
                                            cwd=self.wt, capture_output=True, text=True)
                    actual = self.execute(self.wrapper(context,
                        "BASE=$(git merge-base HEAD " + branch + ") && command git merge-base HEAD origin/" + branch + "\n", declaration))
                    self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                     (native.returncode, native.stdout, native.stderr))
                    proofs.append(dict(branch=branch, role=context.a.role, supported=forms,
                                       pin=self.tip, bare_BASE=self.tip, advanced=newer, stale_origin=self.old))
        self.signed("AC1", dict(refs=["main", "master", "realm"], arbitrary_HEAD="realm",
                    pin=self.tip, new_context=newer, single_context=True, fail_closed=True, wrappers=proofs))

    def test_packet_paths(self):
        import dispatch, fold
        events = [dict(type="build-started", base_sha=self.old),
                  dict(type="build-done", base_sha=self.old, status="DONE"),
                  dict(type="build-done", base_sha=self.old, base_sha_explicit=True, status="DONE")]
        for event in events:
            self.rows.append(dict(self.rows[0], **event))
        self.store()
        cases = []
        triggers = [[], [dict(type="must-fix", criterion="AC1", reverify="scratch")],
                    [dict(type="conflict-rework")],
                    [dict(type="dependency-hold"), dict(type="unblocked")],
                    [dict(type="spawn-failed", reason="wave-order")],
                    [dict(type="decision", rebase="yes")]]
        baseline = list(self.rows)
        for index, trigger in enumerate(triggers):
            self.rows = baseline + [dict(self.rows[0], **event) for event in trigger]
            self.store()
            for flag in (None, self.old):
                for hint in (None, "Correct the scratch behavior"):
                    text = self.render(base_sha=flag, hint=hint)
                    self.assertEqual(dispatch._pinned_base_in(text), self.tip)
                    cases.append(dict(mode=("initial", "generic-rework", "conflict-rework", "dependency-release",
                                            "wave-order-release", "rebase-decision")[index],
                                      trigger=index, flag=bool(flag), hint=bool(hint), pin=self.tip))
        for declaration, expected in (("Base: realm", self.old),
                                      ("Base:  realm", self.tip),
                                      ("Base: realm~1", self.tip),
                                      ("Base: missing", self.tip),
                                      ("PINNED_BASE_SHA_EXPLICIT: true", self.tip)):
            original = self.spec.read_text()
            self.spec.write_text(declaration + "\n" + original)
            self.assertEqual(dispatch._pinned_base_in(self.render(base_sha=self.old)), expected)
            self.assertEqual(packet._effective_base(self.repo, "scratch", self.spec, self.sid), expected)
            self.spec.write_text(original)
        for role in ("grader", "reviewer", "spec-auditor"):
            self.assertEqual(packet.verify_base_sha(self.context(role=role)), self.old)
        with patch.object(fold, "subject_project", return_value="scratch"):
            path = packet.packet_builder_rework(self.sid, "Correct the scratch behavior")
        self.assertEqual(dispatch.pinned_base(path), self.tip)
        manual = packet.main(["builder", self.sid, "--project", "scratch", "--repo", str(self.repo),
                              "--worktree", str(self.wt), "--base-sha", self.old])
        self.assertEqual(dispatch.pinned_base(manual), self.tip)
        cases.append(dict(mode="hint-rework", pin=dispatch.pinned_base(path)))
        # Plain round two: no pending trigger or CLI flag, then consume exactly
        # the pin/marker fields the unchanged dispatch readers persist.
        persisted = []
        for kind in ("must-fix", "rejected-criterion"):
            for explicit_old in (False, True):
                self.rows = [baseline[0], dict(baseline[0], type="build-done", status="DONE",
                                              base_sha=self.old, base_sha_explicit=explicit_old),
                             dict(baseline[0], type=kind, criterion="AC1", reverify="scratch")]
                self.store()
                context = self.context()
                self.assertIsNone(base_refresh.refresh_tip(context))
                text = "\n".join(packet.p_builder(context))
                pin = dispatch._pinned_base_in(text)
                marker = dispatch._pinned_base_explicit_in(text)
                self.assertEqual(pin, self.tip)
                self.assertTrue(marker)
                self.assertEqual(self.git("merge-base", "HEAD", self.tip, repo=self.wt), self.tip)
                self.rows.append(dict(baseline[0], type="build-done", status="DONE",
                                      base_sha=pin, base_sha_explicit=marker))
                self.store()
                grading = [self.context(role=role) for role in ("grader", "reviewer")]
                outputs = []
                for grading_context in grading:
                    self.assertEqual(packet.verify_base_sha(grading_context), pin)
                    wrapper = self.wrapper(grading_context,
                        'echo $BASE && echo "$(git merge-base HEAD main)" && '
                        'echo "$(git merge-base main HEAD)" && '
                        'echo "$(git merge-base HEAD origin/main)" && '
                        'echo "$(git merge-base origin/main HEAD)"\n')
                    outputs.append((wrapper, wrapper.read_bytes()))
                self.git("update-ref", "refs/heads/main", self.old)
                self.git("update-ref", "refs/remotes/origin/main", self.old)
                for wrapper, body in outputs:
                    self.assertEqual(wrapper.read_bytes(), body)
                    result = self.execute(wrapper)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [pin] * 5)
                self.git("update-ref", "refs/heads/main", self.tip)
                persisted.append(dict(kind=kind, latest_explicit_old=explicit_old,
                                      builder=pin, marker=marker, grader=pin, reviewer=pin))
        refused = []
        for source in ("missing-project", "invalid-ref", "missing-ref", "missing-repository"):
            context = self.context(base_sha=self.old)
            config = {"push": {"main": {"scratch": "main"}}}
            if source == "missing-project":
                config["push"]["main"] = {}
            elif source in ("invalid-ref", "missing-ref"):
                config["push"]["main"]["scratch"] = "bad ref" if source == "invalid-ref" else "missing"
            else:
                context.a.repo = str(self.root / "absent")
            artifacts = {p: p.read_bytes() for p in self.content.glob("verify-*.sh")}
            with patch("look.load_toml", return_value=config):
                with self.assertRaisesRegex(SystemExit, "stale-base"):
                    packet.p_builder(context)
            self.assertEqual({p: p.read_bytes() for p in self.content.glob("verify-*.sh")}, artifacts)
            refused.append(source)
        # The pre-change precedence took the flag or explicit historical completion.
        self.assertNotEqual(self.old, self.tip)
        self.signed("AC2", dict(cases=cases, old_precedence=self.old, corrected=self.tip,
                               strict_Base=True, other_roles="historical", round_two=persisted,
                               stale_base_sources=refused))


class R7RefreshTests(R7Fixture):
    def test_pending_refresh(self):
        import dispatch
        initial = list(self.rows)
        triggers = {"conflict-rework": [dict(type="conflict-rework")],
                    "dependency": [dict(type="dependency-hold"), dict(type="unblocked")],
                    "wave-order": [dict(type="spawn-failed", reason="wave-order")],
                    "rebase": [dict(type="decision", rebase="yes")]}
        observed = []
        for name, trigger in triggers.items():
            for status in (None, "BLOCKED", "ERROR", "DONE", "DONE-new-trigger"):
                history = list(trigger)
                if status:
                    history += [dict(type="build-done", status=status.split("-")[0], base_sha=self.old)]
                if status == "DONE-new-trigger":
                    history += trigger
                self.rows = initial + [dict(initial[0], **row) for row in history]
                self.store()
                for flag in (None, self.old):
                    text = self.render(base_sha=flag)
                    marker = dispatch._pinned_base_explicit_in(text)
                    instruction = "The base moved" in text or "rebase it onto" in text
                    self.assertEqual(dispatch._pinned_base_in(text), self.tip)
                    self.assertTrue(marker, (name, status, flag, self.context().evs))
                    context = self.context(base_sha=flag)
                    pending = base_refresh.refresh_tip(context)
                    self.assertEqual(pending, None if status == "DONE" else self.tip)
                    self.assertEqual(base_refresh.refresh_tip(context), pending)
                    self.assertEqual(packet.verify_base_sha(context), self.tip)
                    self.assertFalse(instruction)
                    observed.append(dict(trigger=name, status=status, flag=bool(flag),
                                         pin=self.tip, marker=marker, pending_refresh=pending, instruction=instruction))
        self.rows = initial
        self.store()
        controls = []
        for done_base in (None, self.tip, self.old):
            self.rows = initial + ([dict(initial[0], type="build-done", status="DONE", base_sha=done_base)]
                                   if done_base else [])
            self.store()
            context = self.context()
            text = "\n".join(packet.p_builder(context))
            marker = dispatch._pinned_base_explicit_in(text)
            self.assertEqual(marker, done_base == self.old)
            self.assertEqual(dispatch._pinned_base_in(text), self.tip)
            self.assertIsNone(base_refresh.refresh_tip(context))
            self.assertNotIn("The base moved", text)
            self.assertNotIn("rebase it onto", text)
            controls.append(dict(control="no-history" if done_base is None else
                                 ("DONE-at-N" if done_base == self.tip else "DONE-at-O"),
                                 pin=self.tip, marker=marker, pending_refresh=None, instruction=False))
        for role in ("grader", "reviewer", "spec-auditor"):
            self.assertIsNone(base_refresh.refresh_tip(self.context(role=role)))
        self.signed("AC3", dict(matrix=observed, controls=controls))


if __name__ == "__main__":
    unittest.main()
