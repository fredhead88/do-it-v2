#!/usr/bin/env python3
"""Scratch-ledger regression checks for dispatch's invocation-local view."""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import inspect
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


class ReadMeter:
    """Count outer acquisitions, retaining every nested and exempt door."""

    def __init__(self, stack, events):
        import fold, ledger_cache, ledger_files
        self.events = events.resolve()
        self.rows, self.active = [], []
        self.phase = "pre"
        for module, name in ((fold, "read_events"), (fold, "_read_events_uncached"),
                             (ledger_cache, "read"), (ledger_cache, "read_conditional"),
                             (ledger_files, "iter_sources")):
            original = getattr(module, name)
            door = module.__name__ + "." + name
            stack.enter_context(patch.object(module, name, self.wrap(original, door)))

    def wrap(self, original, door):
        def measured(*args, **kwargs):
            import fold
            target = args[0] if door.startswith(("ledger_cache.", "ledger_files.")) else fold.EVENTS
            if Path(target).resolve() != self.events:
                return original(*args, **kwargs)
            frames = inspect.stack()
            origin = [f"{Path(f.filename).name}:{f.function}:{f.lineno}"
                      for f in frames[1:] if f.filename != __file__]
            exempt = any(
                (Path(f.filename).name, f.function) in {
                    ("branch_refresh.py", "dispatch_preflight"),
                    ("branch_refresh.py", "before_refusal"),
                    ("grading_env.py", "release_in_progress"),
                    ("window_policy.py", "apply_default"), ("fold.py", "append")}
                for f in frames)
            outer = not self.active
            row = dict(door=door, phase=self.phase, origin=origin,
                       disposition="excluded" if exempt else "counted" if outer else "nested")
            self.rows.append(row)
            self.active.append(row)
            try:
                return original(*args, **kwargs)
            finally:
                self.active.pop()
        return measured


def extract_oracle(destination):
    """Only Git objects from the specified revision supply baseline imports."""
    repo = Path(__file__).resolve().parent.parent
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args])
    revision = git("rev-parse", "--verify", "f2459fd^{commit}").decode().strip()
    paths = git("ls-tree", "-r", "--name-only", revision).decode().splitlines()
    digests = {}
    for path in paths:
        if not (path.startswith("src/") and path.endswith(".py") or
                path.startswith("agents/") or path.endswith(".toml")):
            continue
        # Role contracts are inputs, not code under test: they evolve (2277, 2279), so the oracle
        # reads the same agents/ bytes as the candidate; only src/ and config come from the oracle.
        data = ((repo / path).read_bytes() if path.startswith("agents/") and (repo / path).is_file()
                else git("show", revision + ":" + path))
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[path] = hashlib.sha256(data).hexdigest()
    return revision, digests


def budget_child(config):
    """Run one real invocation in a process isolated before product imports."""
    root = Path(os.environ["DOIT_ROOT"])
    source = Path(config["source"])
    sys.path.insert(0, str(source / "src"))
    with contextlib.ExitStack() as stack:
        real_run = subprocess.run
        def local_run(cmd, *args, **kwargs):
            if cmd[:2] == ["claude", "--version"]:
                return subprocess.CompletedProcess(cmd, 0, "fixture-cli", "")
            if cmd[0] == "git":
                return real_run(cmd, *args, **kwargs)
            raise AssertionError("uncaptured external command: " + repr(cmd))
        stack.enter_context(patch.object(subprocess, "run", side_effect=local_run))
        import dispatch as d
        import fold, ledger_cache, ledger_files, relay, packet, audit
        # This assertion also detects accidental candidate imports in the oracle.
        for module in (d, fold, ledger_cache, ledger_files):
            assert Path(module.__file__).resolve().is_relative_to(source.resolve())
        repo = root / "repos" / "fixture"
        repo.mkdir(parents=True, exist_ok=True)
        def git(*args):
            return real_run(["git", "-C", str(repo), *args], check=True,
                            text=True, capture_output=True).stdout.strip()
        if not config.get("resume"):
            git("init", "-q", "-b", "fixture-main")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-qm", "fixture")
        pin = git("rev-parse", "HEAD")
        subject = "L-spec-7933"
        tree = root / "worktrees" / "fixture" / subject.lower()
        if not config.get("resume"):
            git("worktree", "add", "-qb", subject.lower(), str(tree))
        d.EVENTS.mkdir(parents=True, exist_ok=True)
        d.CONTENT.mkdir(exist_ok=True)
        role, case, route = config["role"], config["case"], config["route"]
        if not config.get("resume"):
            seed = dict(v=1, ts="2026-11-06T00:00:00+00:00", type="spec-carried",
                        subject=subject, project="fixture")
            (d.EVENTS / "L-operator-7933.jsonl").write_text(json.dumps(seed) + "\n")
        if role in ("owed-sweeper", "owed-verifier"):
            row = dict(spec=subject, spec_path=str(d.spec_path(subject)),
                       shipped_sha=pin, base_sha=pin, clone=str(repo))
            d.spec_path(subject).write_text("project: fixture\ncwd: " + str(repo) +
                "\nkind: repo\n\n## Rows\n\n```json\n" + json.dumps([row]) + "\n```\n")
        text = "fixture\nPINNED_BASE_SHA: " + pin + "\n" + audit.HEADER
        if role == "grader":
            bundle = dict(spec=subject, sha=pin, base_sha=pin, project="fixture",
                          commands=[], comparison={})
            directory = d.CONTENT / "evidence" / subject
            directory.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(bundle)
            bundle_path = directory / "bundle.json"
            bundle_path.write_text(raw)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            text += "\nbundle_sha256: `" + digest + "`\n"
            if not config.get("resume"):
                rows = [dict(v=1, subject=subject, project="fixture", type="build-done",
                             ts="2026-11-06T00:01:00+00:00", ready_sha=pin, base_sha=pin),
                        dict(v=1, subject=subject, project="fixture", type="evidence-bundle",
                             ts="2026-11-06T00:02:00+00:00", sha=pin, path=str(bundle_path),
                             bundle_sha256=digest)]
                with (d.EVENTS / "L-operator-7933.jsonl").open("a") as stream:
                    stream.writelines(json.dumps(row) + "\n" for row in rows)
        packet_path = root / "packet.md"
        packet_path.write_text(text)
        backend = "codex" if route == "fallback" else route
        mapping = dict(backend=backend, model="fixture", source="fixture")
        if route == "fallback":
            mapping["fallback"] = dict(backend="seat", model="fixture")
        stack.enter_context(patch.object(d.models, "resolve", return_value=mapping))
        # The resolver and disk probe are external infrastructure boundaries;
        # admission predicates, locks and ledger readers run unchanged.
        stack.enter_context(patch.object(packet, "_effective_base", return_value=pin))
        stack.enter_context(patch.object(d, "ensure_room", return_value=True))
        stack.enter_context(patch.object(d, "now", return_value="2026-11-07T00:00:00+00:00"))
        stack.enter_context(patch.object(relay, "serving", return_value=True, create=True))
        stack.enter_context(patch.object(d.atexit, "register"))
        if case == "cache-failure":
            stack.enter_context(patch.object(ledger_cache, "read", side_effect=OSError("fixture cache failure")))
        meter = ReadMeter(stack, d.EVENTS)
        launches, publications = [], []
        detached_children = []
        real_popen = subprocess.Popen
        def detached(command, **kwargs):
            assert command[1] == str(source / "src/dispatch.py"), command
            # Preserve the real CLI's parent/child boundary and inherited root;
            # the child adds measurement before executing the parsed entry.
            child_config = dict(config, entry="cli", resume=True)
            child = real_popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                "--budget-child", json.dumps(child_config)], **kwargs)
            detached_children.append(child)
            return child
        if config.get("entry") == "detached":
            stack.enter_context(patch.object(subprocess, "Popen", side_effect=detached))
        real_write = Path.write_text
        def write(path, data, *args, **kwargs):
            if path.suffixes[-2:] == [".packet", ".md"]:
                command = path.with_name(path.name.replace(".packet.md", ".cmd.json"))
                if hasattr(d, "dispatch_view"):
                    assert command.is_file(), "packet visible before command metadata"
                publications.append(command.name)
            return real_write(path, data, *args, **kwargs)
        stack.enter_context(patch.object(Path, "write_text", write))
        def launch(name):
            launches.append(dict(route=name, pid=os.getpid(),
                                 reads=sum(r["disposition"] == "counted" for r in meter.rows)))
            meter.phase = "backend"
            if case == "timeout":
                meter.phase = "post"
                raise subprocess.TimeoutExpired("fixture", 1)
            if case == "malformed":
                body = "not JSON"
            elif route == "fallback" and name == "codex":
                body = json.dumps(dict(is_error=True, result="at capacity"))
            elif case == "success":
                body = json.dumps(dict(is_error=False, structured_output=dict(
                    findings=[], bad_cut=False, rejected=[], advisory=[], contamination=False)))
            else:
                body = json.dumps(dict(is_error=False, structured_output=None,
                                      modelUsage={} if case == "no-model" else {"fixture": {}}))
            meter.phase = "post"
            return subprocess.CompletedProcess([], 0, body, "")
        stack.enter_context(patch.object(d, "run_codex", side_effect=lambda *a, **k: launch("codex")))
        stack.enter_context(patch.object(d, "run_claude", side_effect=lambda *a, **k: launch("claude-p")))
        # Exercise the seat's actual wait/return path with a synchronous worker.
        real_seat = d.run_seat
        def seat(spawn, cmd, packet_text, cwd, timeout, **kwargs):
            launch("seat")
            meter.phase = "backend"
            clock = [0.0]
            def sleep(seconds):
                clock[0] += seconds
                if not (d.SEAT / f"{spawn}.result.json").exists():
                    (d.SEAT / f"{spawn}.result.json").write_text(json.dumps(dict(
                        is_error=False, structured_output=None, modelUsage={"fixture": {}})))
            try:
                return real_seat(spawn, cmd, packet_text, cwd, timeout, **kwargs,
                                 clock=lambda: clock[0], sleep=sleep)
            finally:
                meter.phase = "post"
        stack.enter_context(patch.object(d, "run_seat", side_effect=seat))
        args = argparse.Namespace(role=role, subject=subject, packet=str(packet_path),
            cwd=str(tree if role == "builder" else repo), project="fixture", charter=None,
            path=str(d.CONTENT / (subject + ".md")) if role == "spec-writer" else str(root / "output"),
            mcp_config=None, timeout=None, max_usd=None, seat=False, augment_history=False)
        if case == "invalid-packet":
            args.packet = str(root / "missing")
        if config.get("default_cwd"):
            args.cwd = None
        if config.get("stdin"):
            args.packet = "-"
        stack.enter_context(patch.object(sys, "stdin", io.StringIO(text)))
        equality = equality_setup(d, stack, args, config, pin, meter, real_seat) if config.get("equality") else None
        if equality is not None:
            meter.rows.clear()
        outcome = 0
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            try:
                if config.get("entry") in ("cli", "detached"):
                    argv = [str(source / "src/dispatch.py"), role, subject, "--packet", args.packet,
                            "--project", "fixture", "--path", args.path]
                    if args.cwd is not None:
                        argv += ["--cwd", args.cwd]
                    if config.get("entry") == "detached":
                        argv += ["--detach"]
                    stack.enter_context(patch.object(sys, "argv", argv))
                    # Compile the unmodified CLI entry from this source revision
                    # into its already imported module, retaining all patches
                    # at external boundaries and the exact production parser.
                    path = str(source / "src/dispatch.py")
                    module = ast.parse(Path(path).read_text(), filename=path)
                    entry = module.body[-1]
                    assert isinstance(entry, ast.If)
                    exec(compile(ast.Module(body=entry.body, type_ignores=[]), path, "exec"), d.__dict__)
                else:
                    if equality is not None and hasattr(d, "dispatch_view"):
                        with d.dispatch_view(None):
                            d._main(args)
                    else:
                        (d._main if config.get("entry") == "_main" else d.main)(args)
            except PreBackendLaunch:
                outcome = "launch"
            except SystemExit as exc:
                outcome = exc.code
        if detached_children:
            child, = detached_children
            assert child.wait(timeout=30) == 0
            launch_row = json.loads(captured.getvalue())
            lines = Path(launch_row["log"]).read_text().splitlines()
            child_result = json.loads(lines[-1])
            assert child_result["pid"] == launch_row["detached"] != os.getpid()
            assert not meter.rows, "detached parent performed a ledger acquisition"
            child_result.update(detached_parent_pid=os.getpid(), parent_reads=0)
            print(json.dumps(child_result))
            return
        result = dict(role=role, pid=os.getpid(), parent_pid=os.getppid(), route=route,
                      case=case, entry=config.get("entry", "main"), rows=meter.rows,
                      count=sum(r["disposition"] == "counted" for r in meter.rows),
                      excluded=sum(r["disposition"] == "excluded" for r in meter.rows),
                      launches=launches, outcome=outcome, publications=publications,
                      diagnostic=captured.getvalue()[-1000:])
        if equality is not None:
            if config.get("post"):
                post_backend_finish(d, config, equality)
            result["equality"] = equality
        print(json.dumps(result))


class TestReadBudget(unittest.TestCase):
    def measure(self, source, role, case, route="codex", **kwargs):
        with tempfile.TemporaryDirectory(prefix="dispatch-budget-") as tmp, \
                tempfile.TemporaryDirectory(prefix="dispatch-budget-scratch-") as scratch:
            root = Path(kwargs.pop("equality_root", tmp))
            if kwargs.get("equality"):
                root.mkdir(parents=True, exist_ok=True)
                scratch = str(root.parent / "scratch")
            config = dict(source=str(source), role=role, case=case, route=route, **kwargs)
            env = dict(isolated_env(root), DOIT_SCRATCH=scratch, DOIT_SUPERVISED="1")
            child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                    "--budget-child", json.dumps(config)],
                                   env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(child.returncode, 0, child.stderr + child.stdout)
            result = json.loads(child.stdout)
            signed(self.id(), result)
            return result

    def test_phase_budget_and_no_bypass(self):
        source = Path(__file__).resolve().parent.parent
        # Inventory the production declaration without importing dispatch in
        # this parent process; only isolated measurement children import it.
        module = ast.parse((source / "src/dispatch.py").read_text())
        roles = next(ast.literal_eval(node.value) for node in module.body
                     if isinstance(node, ast.Assign) and any(
                         isinstance(target, ast.Name) and target.id == "ROLES"
                         for target in node.targets))
        executed = []
        for role in roles:
            cases = [dict(case="invalid-packet")]
            cases += [dict(case="no-model"), dict(case="model")]
            for fixture in cases:
                with self.subTest(role=role, **fixture):
                    result = self.measure(source, role, **fixture)
                    self.assert_budget(result)
                    if fixture["case"] != "invalid-packet":
                        self.assertEqual(len(result["launches"]), 1, result)
                    executed.append((role, fixture["case"]))
        for fixture in (
            dict(case="success", role="spec-auditor"),
            dict(case="cache-failure", role="builder"),
            dict(case="timeout", role="builder"),
            dict(case="malformed", role="builder"),
            dict(case="model", role="builder", entry="_main", stdin=True),
            dict(case="model", role="builder", default_cwd=True),
            dict(case="model", role="builder", entry="cli", stdin=True),
            dict(case="model", role="spec-auditor", entry="detached"),
            dict(case="model", role="spec-auditor", route="claude-p"),
            dict(case="model", role="spec-auditor", route="seat"),
            dict(case="model", role="spec-auditor", route="fallback"),
        ):
            with self.subTest(**fixture):
                result = self.measure(source, **fixture)
                self.assert_budget(result)
                self.assertTrue(result["launches"], result)
                if fixture["case"] == "success":
                    self.assertEqual(result["outcome"], 0, result)
                if fixture.get("route") in ("seat", "fallback"):
                    self.assertEqual(len(result["publications"]), 1)
                if fixture.get("route") == "fallback":
                    self.assertEqual([r["route"] for r in result["launches"]], ["codex", "seat"])
        signed(self.id(), dict(role_inventory=list(roles), executed=executed))

    def assert_budget(self, result):
        counted = [r for r in result["rows"] if r["disposition"] == "counted"]
        self.assertTrue(all(r["door"] == "fold.read_events" for r in counted), result)
        self.assertLessEqual(len(counted), 2, result)
        self.assertLessEqual(sum(r["phase"] == "pre" for r in counted), 1, result)
        self.assertFalse(any(r["phase"] == "backend" for r in counted), result)
        if result["case"] == "invalid-packet":
            self.assertEqual(counted, [])
            self.assertEqual(result["launches"], [])
        elif result["case"] == "no-model":
            self.assertEqual(len(counted), 1, result)
        for launch in result["launches"][:1]:
            self.assertEqual(launch["reads"], 1, result)

    def test_base_exceeds_budget(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-oracle-") as tmp:
            source = Path(tmp)
            revision, digests = extract_oracle(source)
            result = self.measure(source, "builder", "model")
            self.assertTrue(result["launches"], result)
            count = sum(r["disposition"] == "counted" for r in result["rows"])
            self.assertGreater(count, 2, result)
            signed(self.id(), dict(base_revision=revision, source_digests=digests, count=count))


def isolated_env(root):
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in
                      ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "DSN", "SUPABASE"))
           and not k.startswith("DOIT_")}
    env.update(DOIT_ROOT=str(root), HOME=str(root / "home"),
               XDG_CONFIG_HOME=str(root / "xdg"), DOIT_NO_POKE="1",
               GIT_AUTHOR_DATE="2026-11-06T00:00:00+00:00",
               GIT_COMMITTER_DATE="2026-11-06T00:00:00+00:00")
    return env


class PreBackendLaunch(BaseException):
    """Capture the infrastructure boundary after all admission gates ran."""


def equality_setup(d, stack, args, config, pin, meter, real_seat):
    """Seed real files; observe original predicates without replacing verdicts."""
    import fold, spawns, dispatch_builder, grading_env, routing, precheck, grader_packet, look
    source = Path(config["source"]).resolve()
    for module in (d, fold, spawns, dispatch_builder, grading_env, routing, precheck,
                   grader_packet, look):
        assert Path(module.__file__).resolve().is_relative_to(source), module.__file__
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 11, 7, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    real_datetime = datetime
    for module in list(sys.modules.values()):
        if getattr(module, "datetime", None) is real_datetime:
            stack.enter_context(patch.object(module, "datetime", Clock))
    stack.enter_context(patch("datetime.datetime", Clock))
    stack.enter_context(patch.object(fold, "NOW", Clock.now(timezone.utc)))
    stack.enter_context(patch.object(d.os, "getpid", return_value=424242))
    stack.enter_context(patch.object(d.panes, "proc_start", return_value="fixture-process"))
    fixture = config["case"]
    subject = args.subject
    counter = [0]
    observed = dict(root=str(d.ROOT), gates=[], writes=[], launches=[], collectors=0)
    def seed(actor, type_, **kv):
        counter[0] += 1
        row = dict(v=1, ts=f"2026-11-06T23:{counter[0]:02d}:00+00:00",
                   subject=subject, project="fixture", type=type_)
        row.update(kv)
        with (d.EVENTS / f"L-{actor}-7934.jsonl").open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    if fixture == "killed":
        seed("spec-writer", "spec-killed", check=1)
    elif fixture in ("open-writer", "duplicate", "conflicting"):
        role = "spec-writer" if fixture == "open-writer" else "builder"
        seed(role, "spawn-started", role=role, spawn=f"L-{role}-7934",
             worktree=args.cwd, window_min=240)
    elif fixture == "wrong-project":
        # A later declaration is authoritative to subject_project.
        seed("spec-writer", "spec-written", **{"project": "other"})
    elif fixture == "unaudited":
        (d.EVENTS / "L-operator-7933.jsonl").write_text("")
        seed("spec-writer", "spec-written")
    elif fixture in ("wave-hold", "wave-waiver"):
        charter = "L-charter-7933"
        (d.CONTENT / f"plan-{charter}.md").write_text(
            "## first\nFootprint:\n- first.py\nWave: 1\n"
            "## second\nFootprint:\n- second.py\nWave: 2\n")
        seed("spec-writer", "spec-written", charter=charter, footprint=["second.py"])
        seed("spec-writer", "spec-written", **dict(subject="L-spec-7932", charter=charter,
                                                   footprint=["first.py"]))
        if fixture == "wave-waiver":
            seed("thinker", "decision", wave_waive="yes")
    elif fixture == "stale-branch":
        # Exercise branch admission using a real detached scratch checkout.
        subprocess.run(["git", "-C", args.cwd, "checkout", "--detach", "-q"], check=True)
    elif fixture in ("budget-hold", "budget-release"):
        for n in range(2):
            seed("grader", "rejected-criterion", criterion="AC1", why="fixture", spawn=f"prior-{n}")
        if fixture == "budget-release":
            seed("thinker", "decision", regrade="yes")
    elif fixture == "commit-shape":
        seed("builder", "rejected-criterion", criterion="COMMIT-SHAPE", why="fixture")
    elif fixture == "precheck-red":
        seed("executor", "precheck-pregrade", branch_sha=pin, master=pin, verdict="red", tests=["fixture"])
        stack.enter_context(patch.object(precheck, "grader_master", return_value=pin))
    elif fixture == "packet-refusal":
        args._packet = "no bundle manifest"
        # Captured launch of the evidence collector, never execute its command.
        def collector(*a, **kw):
            observed["collectors"] += 1
        stack.enter_context(patch.object(subprocess, "Popen", side_effect=collector))
    elif fixture in ("explicit-owed", "routed"):
        d.spec_path(subject).write_text("## Requirements\nAC1 [ui]: inspect the fixture\n"
                                      "review_path: log in as fixture at https://fixture.invalid; go to /\n")
        if fixture == "explicit-owed":
            seed("spec-writer", "owed-ac", criterion="AC1", wake_at="2026-11-08T00:00:00Z")
    elif fixture in ("held-capability", "released-capability"):
        args.mcp_config = str(d.ROOT / "mcp.json")
        Path(args.mcp_config).write_text('{"mcpServers": {}}')
        hold = f"capability:browser:{subject}"
        seed("reviewer", "capability-hold", **dict(subject=hold, capability="browser", spec=subject))
        if fixture == "released-capability":
            seed("thinker", "unblocked", **dict(subject=hold))
        stack.enter_context(patch.object(grading_env, "preflight", return_value=[]))
    elif fixture in ("disk-restored", "disk-unavailable"):
        seed("look", "escalation-blocking", **dict(subject="disk-room:builder", kind="disk-room"))
        stack.enter_context(patch.object(d, "ensure_room", return_value=fixture == "disk-restored"))
        stack.enter_context(patch.object(d.shutil, "disk_usage", return_value=type(
            "Usage", (), dict(free=2**30, total=10*2**30, used=9*2**30))()))
    elif fixture == "prior-failure":
        seed("builder", "spawn-failed", spawn="L-builder-7934", why="is_error: deterministic",
             packet_sha256=d.sha(Path(args.packet).read_text()),
             contract_sha256=d.sha((d.AGENTS / "builder.md").read_bytes() +
                                   (d.AGENTS / "builder.schema.json").read_bytes()))
    elif fixture == "window-override":
        d.spec_path(subject).write_text("window_min: 180\n\nfixture\n")
    elif fixture in ("retry-wait", "retry-ready"):
        seed("builder", "infra-failure", role="builder", backend="codex", attempt="prior",
             **{"class": "runner-or-network"})
        if fixture == "retry-wait":
            stack.enter_context(patch.object(d, "now", return_value="2026-11-06T23:02:00+00:00"))
    elif fixture == "unknown-machine":
        seed("builder", "escalation-blocking", role="builder", kind="failure-unknown", attempt="prior")

    # Everything below observes actual _main calls and production writes.
    def observe(module, name, event_arg=None):
        original = getattr(module, name)
        def call(*a, **kw):
            row = dict(gate=module.__name__ + "." + name)
            if event_arg is not None:
                row["events"] = json.loads(json.dumps(a[event_arg]))
            observed["gates"].append(row)
            try:
                value = original(*a, **kw)
            except BaseException as exc:
                row["raised"] = type(exc).__name__
                raise
            if isinstance(value, (set, frozenset)):
                row["result"] = sorted(value)
            elif module is spawns and name == "gate" and value is not None:
                row["result"] = "lock"
            else:
                assert isinstance(value, (str, int, bool, list, dict, type(None))), type(value)
                row["result"] = value
            return value
        stack.enter_context(patch.object(module, name, side_effect=call))
    for module, name, index in ((d, "infra_gate", 0), (d, "_machine_hold", 0),
            (spawns, "gate", 4), (d, "unaudited", 0), (d, "grading_budget", 0),
            (d, "held_capability", 0), (d, "window_min", 2),
            (routing, "explicit_owed_ids", 0), (precheck, "pregrade_refusal", 0)):
        observe(module, name, index)
    if config.get("post"):
        return post_backend_setup(d, stack, args, config, pin, observed, seed, meter, real_seat)
    original_emit = d.emit
    def emit(*a, **kw):
        result = original_emit(*a, **kw)
        events = d.current_events() if hasattr(d, "current_events") else fold.read_events()
        observed["writes"].append(dict(type=a[2], result=result, events=events.copy()))
        return result
    stack.enter_context(patch.object(d, "emit", side_effect=emit))
    def launch(*a, **kw):
        observed["launches"].append(dict(timeout=a[6], window=kw.get("window")))
        raise PreBackendLaunch()
    stack.enter_context(patch.object(d, "run_codex", side_effect=launch))
    observed["seed_sha256"] = hashlib.sha256(b"".join(
        p.read_bytes() for p in sorted(d.EVENTS.glob("*.jsonl")))).hexdigest()
    return observed


class TestPreBackendEquality(unittest.TestCase):
    # L-spec-2114 added only this builder-failure metadata key and this exact
    # suffix to preflight refusal text. Its omitted-cwd policy belongs to AC5;
    # AC2 supplies equivalent explicit project/cwd fixtures to both revisions.
    CWD_METADATA_KEYS = ("cwd",)
    CWD_REFUSAL_FRAGMENT = " (cwd={cwd})"

    def comparison_value(self, value, root):
        cwd = str(Path(root) / "worktrees" / "fixture" / "l-spec-7933")
        fragment = self.CWD_REFUSAL_FRAGMENT.format(cwd=cwd)

        def substitute(item):
            if isinstance(item, list):
                return [substitute(child) for child in item]
            if not isinstance(item, dict):
                return item
            result = {key: substitute(child) for key, child in item.items()}
            if result.get("type") == "spawn-failed" and result.get("actor") == "builder":
                for key in self.CWD_METADATA_KEYS:
                    if key in result:
                        self.assertEqual(result[key], cwd)
                        del result[key]
                if result.get("reason", "").startswith(
                        ("preflight:", "stale-base", "branch-not-descendant")):
                    why = result.get("why", "")
                    if why.endswith(fragment):
                        result["why"] = why[:-len(fragment)]
            return result

        # No other fields or text are discarded, including reason, actor,
        # _src, multiplicity and verdict. Apply both substitutions symmetrically.
        return json.loads(json.dumps(substitute(value)).replace(
            str(Path(root).parent), "$SCRATCH"))

    def test_pinned_base_gate_pairs(self):
        fixtures = {
            "builder": ["admitted", "killed", "open-writer", "wrong-project", "wave-hold",
                        "wave-waiver", "unaudited", "duplicate", "stale-branch", "disk-restored",
                        "disk-unavailable", "prior-failure", "window-override", "retry-wait",
                        "retry-ready", "unknown-machine"],
            "grader": ["admitted", "conflicting", "budget-hold", "budget-release", "commit-shape",
                       "precheck-red", "packet-refusal", "explicit-owed", "routed"],
            "reviewer": ["held-capability", "released-capability"],
        }
        signed(self.id(), dict(fixture_inventory=fixtures))
        with tempfile.TemporaryDirectory(prefix="dispatch-equality-oracle-") as tmp:
            baseline = Path(tmp)
            revision, digests = extract_oracle(baseline)
            signed(self.id(), dict(base_revision=revision, source_digests=digests))
            candidate = Path(__file__).resolve().parent.parent
            for role, cases in fixtures.items():
                for case in cases:
                    with self.subTest(role=role, fixture=case), tempfile.TemporaryDirectory(
                            prefix="dispatch-equality-pair-") as pair:
                        pairs, raw_pairs = [], []
                        for source in (baseline, candidate):
                            result = TestReadBudget.measure(self, source, role, case,
                                equality=True, entry="_main", equality_root=str(Path(pair) / "root"))
                            root = result["equality"].pop("root")
                            value = dict(outcome=result["outcome"], **result["equality"])
                            raw_pairs.append(value)
                            pairs.append(self.comparison_value(value, root))
                            shutil.rmtree(root)
                        signed(self.id(), dict(fixture=case, role=role,
                            comparison_substitutions=dict(
                                scratch_prefix=str(Path(root).parent),
                                builder_failure_metadata_keys=list(self.CWD_METADATA_KEYS),
                                builder_failure_refusal_fragment=self.CWD_REFUSAL_FRAGMENT.format(
                                    cwd=str(Path(root) / "worktrees" / "fixture" / "l-spec-7933"))),
                            raw_baseline=raw_pairs[0], raw_candidate=raw_pairs[1],
                            baseline=pairs[0], candidate=pairs[1]))
                        if role == "grader" and case == "packet-refusal":
                            # Acquisition moved to tick; the receiver still
                            # makes exactly the same refusal and gate decisions.
                            self.assertEqual([p["collectors"] for p in pairs], [1, 0])
                            pairs[0]["collectors"] = 0
                        self.assertEqual(pairs[0], pairs[1])
                        if case in ("held-capability", "released-capability"):
                            gate = next(g for g in pairs[0]["gates"]
                                        if g["gate"] == "dispatch.held_capability")
                            self.assertEqual(gate["result"], "capability:browser:L-spec-7933"
                                             if case == "held-capability" else None)
                            self.assertEqual(bool(pairs[0]["launches"]), case == "released-capability")
                        if case in ("explicit-owed", "routed"):
                            gate = next(g for g in pairs[0]["gates"]
                                        if g["gate"] == "routing.explicit_owed_ids")
                            self.assertEqual(gate["result"], ["AC1"] if case == "explicit-owed" else [])
                            routes = [w for w in pairs[0]["writes"] if w["type"] == "criterion-routed"]
                            self.assertEqual(len(routes), int(case == "routed"))
                            if routes:
                                self.assertTrue(any(g["gate"] == "dispatch.window_min" and any(
                                    e.get("type") == "criterion-routed" and e.get("actor") == "grader"
                                    and e.get("_src", "").endswith(":1")
                                    for e in g["events"]) for g in pairs[0]["gates"]))


def independent_read(source):
    """The observer opens closed files in its own process, without a view."""
    script = ("import json,os,sys; sys.path.insert(0,sys.argv[1]); import fold; "
              "print(json.dumps(dict(pid=os.getpid(),events="
              "fold.read_events(use_cache=False)),sort_keys=True))")
    with subprocess.Popen([sys.executable, "-B", "-c", script, str(Path(source) / "src")],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
    return json.loads(stdout)


def post_backend_setup(d, stack, args, config, pin, observed, seed, meter, real_seat):
    """Capture launches only; execute all post-backend gates and real emits."""
    import fold, spawns, validate
    fixture, role = config["case"], config["role"]
    source = config["source"]
    observed.update(observations=[], post_gates=[], late=[], seed_sha256=hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(d.EVENTS.glob("*.jsonl")))).hexdigest())
    out = dict(findings=[], bad_cut=False, rejected=[], advisory=[], contamination=False)
    if role == "builder":
        out = dict(status="DONE", identity=dict(spec_id=args.subject, built_by="L-builder-7000",
            branch=args.subject.lower(), base_sha=pin, ready_sha=pin),
            acs=[dict(id="AC1", criterion_type="backend", evidence="fixture", evidence_type="signed-response",
                      check="fixture", disposition="done")],
            verify=dict(command="fixture", exit_code=0, result="fixture"), coupled=[],
            stubs=[], deviations=[], tests=dict(added=True), not_built=[], unknowns=[],
            built_against=[], escalations=[], declarations=[])
    elif role == "grader":
        # Current grader validation requires the complete criterion inventory.
        # Give both revisions the same valid spec instead of bypassing that gate.
        d.spec_path(args.subject).write_text(
            "# L-spec-7933\n## Acceptance\nAC1 [backend]: inspect the fixture.\n" +
            ("review_path: run git diff HEAD\n" if fixture == "routed-selection" else ""))
        verdict = dict(ac="AC1", verdict="met", reason="fixture")
        if fixture in ("late-capability", "late-tooling", "late-owed"):
            verdict.update(verdict="cannot-assess", reason_code="tooling-defect" if
                           fixture == "late-tooling" else "tool-failed", missing_capability="browser")
            if fixture == "late-tooling":
                del verdict["missing_capability"]
        out = dict(verdicts=[verdict], matches_intent="yes", card_ok="yes",
                   checkers=[dict(id="fixture", version="1", coverage_note="fixture coverage", result="pass")],
                   could_not_run=False, contamination=False, declarations=[])
    elif role == "spec-writer":
        d.spec_path(args.subject).write_text(
            "# L-spec-7933\nbase_sha: HEAD\n## Verification\n```\n/usr/bin/python3 t.py\n```\n"
            "## Acceptance\nAC1 [backend]: fixture.\nreview_path: run t.py.\nWrites: src/a.py\n")
        assert validate.spec_shape_warnings(d.spec_path(args.subject).read_text())
        out = dict(status="written", spec_id=args.subject, ac_count=1, ac_types=["backend"],
                   footprint=["src/a.py"], requirement_ids=["R1"], owed=0, unknowns=0,
                   split=[], weak_dimensions=[], escalations=[], declarations=[])

    def snapshot(label):
        observer = independent_read(source)
        view = d._active_view.get()
        active = json.loads(json.dumps(list(view)))
        assert active == observer["events"], (label, active, observer)
        assert observer["pid"] != 424242
        observed["observations"].append(dict(label=label, **observer, active=active))

    if hasattr(d, "LedgerView"):
        original_current = d.LedgerView.current
        def current(view):
            before = view.refreshed
            answer = original_current(view)
            if view.refreshed and not before:
                snapshot("immediately-after-refresh")
            return answer
        stack.enter_context(patch.object(d.LedgerView, "current", current))

    original_emit = d.emit
    def emit(*a, **kw):
        answer = original_emit(*a, **kw)
        observed["writes"].append(dict(type=a[2], result=answer))
        if answer is None and hasattr(d, "_active_view") and d._active_view.get().refreshed:
            snapshot("after-write:" + a[2])
        return answer
    stack.enter_context(patch.object(d, "emit", side_effect=emit))
    for module, name in ((d.models, "first_on_model"), (d, "commit_shape_events"),
                         (spawns, "clear_events"), (d, "coverage_changes"), (d, "events_for")):
        original = getattr(module, name)
        def call(*a, _original=original, _name=name, **kw):
            value = _original(*a, **kw)
            observed["post_gates"].append(dict(gate=_name, result=json.loads(json.dumps(value))))
            return value
        stack.enter_context(patch.object(module, name, side_effect=call))

    def during_backend():
        before = independent_read(source)["events"]
        observed["late"] = []
        if fixture == "late-model":
            contract = d.sha((d.AGENTS / (role + ".md")).read_bytes() +
                             (d.AGENTS / (role + ".schema.json")).read_bytes())
            seed("spec-auditor", "spawn-done", contract_sha256=contract, model_used="fixture")
        elif fixture == "late-build":
            seed("builder", "build-done", ready_sha=pin, base_sha=pin, status="DONE")
        elif fixture == "late-rejection":
            seed("grader", "rejected-criterion", criterion="AC1", why="late rejection")
            seed("grader", "checker-coverage-change", checker="fixture", version="1",
                 coverage_note="fixture coverage")
        elif fixture == "late-capability":
            seed("reviewer", "capability-hold", **dict(subject="capability:browser:" + args.subject,
                                                       capability="browser", spec=args.subject))
        elif fixture == "late-tooling":
            seed("thinker", "escalation-blocking", **dict(subject="grading-tooling:" + args.subject,
                                                          kind="grading-tooling"))
        elif fixture == "late-owed":
            seed("spec-writer", "owed-ac", criterion="AC1", wake_at="2026-11-08T00:00:00Z")
        elif fixture == "late-lint":
            for finding in validate.spec_shape_warnings(d.spec_path(args.subject).read_text()):
                seed("spec-writer", "spec-lint-warning", finding=finding)
        elif fixture in ("retry-refresh", "unknown-failure"):
            # Committed in this process before refresh, alongside spawn-started.
            # The subsequent machine-failure gate must not replay either row.
            ledger = next(p for p in d.EVENTS.glob(f"L-{role}-*.jsonl"))
            base = dict(subject=args.subject, project="fixture", spawn=ledger.stem)
            d.emit(ledger, base, "infra-failure", role=role, backend=config["route"],
                   attempt=ledger.stem, **{"class": "runner-or-network"})
            seed(role, "infra-failure", role=role, backend=config["route"],
                 attempt="other-actor", **{"class": "runner-or-network"})
        after = independent_read(source)["events"]
        sources = {e["_src"] for e in before}
        observed["late"] = [e for e in after if e["_src"] not in sources]
        if fixture.startswith("late-") or fixture in ("retry-refresh", "unknown-failure"):
            assert observed["late"], fixture

    def envelope():
        if fixture in ("retry-refresh", "unknown-failure"):
            return dict(is_error=True, result="Connection reset by peer" if fixture == "retry-refresh"
                        else "unclassified fixture failure", modelUsage={})
        return dict(is_error=False, structured_output=out,
                    modelUsage={} if fixture in ("no-refresh", "late-lint") else {"fixture": {}})

    def launch(*a, **kw):
        observed["launches"].append(dict(route="codex", timeout=a[6]))
        meter.phase = "backend"
        if config["route"] == "fallback":
            meter.phase = "post"
            return subprocess.CompletedProcess([], 0, json.dumps(dict(is_error=True, result="at capacity")), "")
        during_backend()
        meter.phase = "post"
        return subprocess.CompletedProcess([], 0, json.dumps(envelope()), "")
    stack.enter_context(patch.object(d, "run_codex", side_effect=launch))
    def seat(*a, **kw):
        observed["launches"].append(dict(route="seat", timeout=a[4]))
        meter.phase = "backend"
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            if clock[0] == 306:
                during_backend()
                (d.SEAT / (a[0] + ".result.json")).write_text(json.dumps(envelope()))
        try:
            return real_seat(*a, **kw, clock=lambda: clock[0], sleep=sleep)
        finally:
            meter.phase = "post"
    stack.enter_context(patch.object(d, "run_seat", side_effect=seat))
    return observed


def post_backend_finish(d, config, observed):
    import spawns
    reader = independent_read(config["source"])
    events = reader["events"]
    role = config["role"]
    observed["final"] = events
    observed["retry_length"] = len(d._retry_events(events, "L-spec-7933", role))
    observed["retry_at"] = d.retry_at(events, "L-spec-7933", role, "2026-11-07T00:00:00+00:00")
    observed["open_spawns"] = spawns.open_spawns(events, datetime(2026, 11, 7, tzinfo=timezone.utc))
    observed["grading_budget"] = d.grading_budget(events, "L-spec-7933")


def handin_aware(value):
    """Strip only the additive hand-in outcome of a checked DONE builder: the
    handin_check / handin_reason fields and the handin-checked row. Everything
    else must still equal the baseline."""
    if isinstance(value, list):
        return [handin_aware(v) for v in value
                if not (isinstance(v, dict) and v.get("type") == "handin-checked")]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k == "handin_check" and v in ("clean", "undetermined"):
            continue
        if k == "handin_reason" and v == "handin-check:undetermined":
            continue
        out[k] = handin_aware(v)
    return out


class TestPostBackendFreshness(unittest.TestCase):
    def pairs(self, fixtures):
        candidate = Path(__file__).resolve().parent.parent
        completed = []
        with tempfile.TemporaryDirectory(prefix="dispatch-post-oracle-") as tmp:
            baseline = Path(tmp)
            revision, digests = extract_oracle(baseline)
            signed(self.id(), dict(base_revision=revision, source_digests=digests, fixture_inventory=fixtures))
            for role, case, route in fixtures:
                with self.subTest(role=role, case=case, route=route), tempfile.TemporaryDirectory(
                        prefix="dispatch-post-pair-") as pair:
                    results, values = [], []
                    for source in (baseline, candidate):
                        result = TestReadBudget.measure(self, source, role, case, route=route,
                            equality=True, post=True, entry="_main", equality_root=str(Path(pair) / "root"))
                        value = dict(outcome=result["outcome"], **result["equality"])
                        root = value.pop("root")
                        observations = value.pop("observations")
                        if source == candidate:
                            counted = [r for r in result["rows"] if r["disposition"] == "counted"]
                            self.assertTrue(all(r["door"] == "fold.read_events" for r in counted))
                            self.assertEqual(len(counted), 1 if case == "no-refresh" else 2)
                            self.assertTrue(all(r["phase"] == "pre" for r in counted[:1]))
                            self.assertTrue(all(r["phase"] == "post" for r in counted[1:]))
                            # Independent observations are not meter acquisitions.
                            self.assertEqual(bool(observations), case != "no-refresh")
                            if observations:
                                self.assertEqual(observations[0]["label"], "immediately-after-refresh")
                        results.append(result)
                        values.append(json.loads(json.dumps(value).replace(str(Path(root).parent), "$SCRATCH")))
                        shutil.rmtree(root)
                    signed(self.id(), dict(fixture=case, role=role, route=route,
                                           baseline=values[0], candidate=values[1]))
                    self.assertEqual(values[0], handin_aware(values[1]) if role == "builder" else values[1])
                    self.assertEqual(len(values[1]["launches"]), 2 if route == "fallback" else 1)
                    completed.append((case, values[1], results[1]))
        return completed

    def test_late_gate_pairs(self):
        fixtures = [("spec-auditor", "late-model", "codex"), ("spec-auditor", "first-model", "codex"),
                    ("builder", "late-build", "codex"), ("grader", "late-rejection", "codex"),
                    ("grader", "late-capability", "codex"), ("grader", "late-tooling", "codex"),
                    ("grader", "late-owed", "codex"), ("spec-writer", "late-lint", "codex"),
                    ("spec-auditor", "unknown-failure", "codex")]
        for case, value, result in self.pairs(fixtures):
            gates = {r["gate"]: r["result"] for r in value["post_gates"]}
            types = [r["type"] for r in value["writes"]]
            if case in ("late-model", "first-model"):
                self.assertEqual(gates["first_on_model"], case == "first-model")
            elif case == "late-build":
                self.assertEqual(gates["commit_shape_events"][0][0], "rejected-criterion")
            elif case == "late-rejection":
                self.assertEqual(gates["clear_events"][0][1]["criterion"], "AC1")
                self.assertEqual(gates["coverage_changes"], [])
            elif case in ("late-capability", "late-tooling", "late-owed"):
                self.assertNotIn("capability-hold", types)
                self.assertNotIn("escalation-blocking", types)
            elif case == "late-lint":
                self.assertNotIn("spec-lint-warning", types)
            else:
                self.assertIn("escalation-blocking", types)

    def test_exactly_once_refresh(self):
        fixtures = [("builder" if case == "retry-refresh" else "spec-auditor", case, route) for case, route in
                    (("retry-refresh", "codex"), ("seat-refresh", "seat"),
                     ("seat-refresh", "fallback"), ("no-refresh", "codex"))]
        for case, value, result in self.pairs(fixtures):
            events = value["final"]
            self.assertEqual(len(events), len({e["_src"] for e in events}))
            self.assertFalse(value["open_spawns"])
            self.assertIsNone(value["grading_budget"])
            if case == "retry-refresh":
                self.assertEqual(value["retry_length"], 3)
                self.assertEqual(value["retry_at"], "2026-11-07T00:45:00+00:00")
            if case == "seat-refresh":
                for kind in ("spawn-started", "seat-queued", "seat-stale"):
                    self.assertEqual(sum(e["type"] == kind for e in events), 1)
                self.assertTrue(any(e["type"] == "seat-stale" for e in
                                    result["equality"]["observations"][0]["active"]))


def signed(name, body):
    encoded = json.dumps(body, sort_keys=True)
    print(json.dumps(dict(url="cli://dispatch-read-once/" + name, status=0,
                         body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                         body_excerpt=body)))


class ScratchLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-view-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / "events"
        self.events.mkdir()
        self.env = isolated_env(self.root)
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        import dispatch
        import fold
        self.dispatch, self.fold = dispatch, fold
        self.enterContext(patch.multiple(dispatch, ROOT=self.root, EVENTS=self.events,
                                        CONTENT=self.root / "content"))
        self.enterContext(patch.multiple(fold, ROOT=self.root, EVENTS=self.events, PROJECT=None))
        self.enterContext(patch.object(dispatch, "now", return_value="2026-11-07T00:00:00+00:00"))

    def read_back(self):
        # Observation has a separate process and no candidate view reference.
        script = ("import json,sys; sys.path.insert(0,sys.argv[1]); import fold; "
                  "print(json.dumps(fold.read_events(use_cache=False),sort_keys=True))")
        result = subprocess.run([sys.executable, "-B", "-c", script,
                                 str(Path(__file__).resolve().parent)],
                                env=self.env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def assert_round_trip(self, view):
        observed = self.read_back()
        self.assertEqual(list(view), observed)
        signed(self.id(), dict(observation="fresh-process uncached reader", rows=observed))


class TestEmitRoundTrip(ScratchLedger):
    def test_committed_view_matches_uncached_reader(self):
        d = self.dispatch
        dst = self.events / "L-operator-0001.jsonl"
        dst.write_text("\nmalformed\n")
        other = self.events / "L-builder-0002.jsonl"
        third = self.events / "L-thinker-0003.jsonl"
        base = dict(subject="L-spec-7001", project="fixture")
        with d.dispatch_view(self.fold.read_events(use_cache=False)) as view:
            for target, kind, fields in (
                (dst, "decision", dict(decision="fixture", actor="forged", _src="forged")),
                (other, "build-started", dict(spawn="L-builder-0002", worktree="fixture")),
                (dst, "decision", dict(decision="second")),
                (dst, "tree-reaped", dict(retained=["fixture"], retained_reason="fixture")),
                (dst, "correction", dict(ref="L-operator-0001.jsonl:3",
                                         set={"project": "corrected", "actor": "forged"})),
            ):
                self.assertIsNone(d.emit(target, base, kind, **fields))
                self.assert_round_trip(view)
            # Cross-file ties and subject overrides use storage-derived order;
            # repeated identical payloads still have distinct physical sources.
            for target, subject in ((third, "L-spec-6999"), (other, "L-spec-7999"),
                                    (dst, "L-spec-7001"), (dst, "L-spec-7001")):
                self.assertIsNone(d.emit(target, {**base, "subject": subject}, "decision",
                                         decision="same second", payload={"unicode": "λ"}))
                self.assert_round_trip(view)
            self.assertTrue(any(e["_src"] == "L-operator-0001.jsonl:3" for e in view))
            self.assertEqual(len(view), len({e["_src"] for e in view}))
            # A projected ruling is read back in its normalized form.
            with patch.object(d, "now", return_value="2026-11-07T00:01:00+00:00"):
                self.assertIsNone(d.emit(third, {**base, "subject": "L-question-7001"}, "decision",
                    ruling=dict(kind="regrade", spec="L-spec-7001")))
            self.assert_round_trip(view)
            self.assertTrue(any(e.get("regrade") == "yes" and e["subject"] == "L-spec-7001" for e in view))
            # A recorded-and-ignored event is still returned by the reader.
            self.assertIsNone(d.emit(other, base, "correction", ref="fixture:1", set={"project": "ignored"}))
            self.assert_round_trip(view)
            self.assertTrue(any(e["type"] == "correction" and e["actor"] == "builder"
                                for e in view))
            before = list(view)
            disk_before = other.read_bytes()
            self.assertTrue(d.emit(other, base, "correction", strict=True, ref="fixture:1", set={"project": "refused"}))
            self.assertEqual(view, before)
            self.assertEqual(other.read_bytes(), disk_before)
        raw = [json.loads(line) for path in self.events.glob("*.jsonl")
               for line in path.read_text().splitlines() if line.startswith("{")]
        self.assertFalse(any(set(e) & {"events", "view", "_active_view", "_dispatch_view"}
                             for e in raw))
        # The negative control invokes base emit, rather than failing because
        # the baseline lacks the new API. Its committed row never extends the
        # caller's existing list; an independent read observes it on disk.
        with tempfile.TemporaryDirectory(prefix="dispatch-emit-oracle-") as tmp:
            source = Path(tmp)
            revision, digests = extract_oracle(source)
            script = '''import json, pathlib, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
with patch("subprocess.run", return_value=type("Version", (), {"stdout": "fixture-cli"})()):
    import dispatch as d
import fold
view = fold.read_events(use_cache=False)
before = len(view)
assert d.emit(d.EVENTS / "L-operator-9000.jsonl", {"subject": "L-spec-7001"}, "decision", decision="base control") is None
print(json.dumps(dict(before=before, active=view)))
'''
            child = subprocess.run([sys.executable, "-B", "-c", script, str(source / "src")],
                                   env=self.env, text=True, capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)
            value = json.loads(child.stdout)
            disk = independent_read(source)["events"]
            self.assertEqual(len(value["active"]), value["before"])
            self.assertEqual(len(disk), value["before"] + 1)
            self.assertNotEqual(value["active"], disk)
            signed(self.id(), dict(base_revision=revision, source_digests=digests,
                                   negative_control="committed row absent from active list",
                                   active=value["active"], read_back=disk))

    def test_view_lifetime_and_refusals(self):
        d = self.dispatch
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with d.dispatch_view([]) as first:
                self.assertIs(d.current_events(), first)
                self.assertIsNone(d.emit(self.events / "L-operator-0001.jsonl",
                                        {"subject": "L-spec-7001"}, "decision", decision="first view"))
                self.assertEqual(len(first), 1)
                raise RuntimeError("fixture")
        self.assertIsNone(d._active_view.get())
        with d.dispatch_view([]) as second:
            dst = self.events / "L-operator-0001.jsonl"
            self.assertTrue(d.emit(dst, {"subject": "L-spec-7001"}, "escalation-blocking"))
            self.assertEqual(len(dst.read_text().splitlines()), 1)
            self.assertEqual(second, [])
        self.assertIsNone(d._active_view.get())
        for entry in (d.main, d._main):
            with patch.object(self.fold, "read_events", side_effect=AssertionError("unexpected read")):
                with self.assertRaises(SystemExit):
                    entry(argparse.Namespace(role="builder", packet="missing"))
            self.assertIsNone(d._active_view.get())
        # An open operator ask suppresses expiry using the supplied active
        # events. Neither the guard nor emit may acquire the ledger again.
        with d.dispatch_view(self.fold.read_events(use_cache=False)) as view:
            base = dict(subject="L-spec-7001", project="fixture")
            for actor, ts, kind, fields in (
                ("spec-writer", "2026-11-01T00:00:00+00:00", "owed-ac",
                 dict(criterion="AC1", wake_at="2026-11-02T00:00:00Z", line="fixture")),
                ("executor", "2026-11-02T00:00:00+00:00", "shipped", {}),
                ("owed-sweeper", "2026-11-06T00:00:00+00:00", "owed-unobservable",
                 dict(criterion="AC1", capability="operator-action", why="approve fixture")),
            ):
                with patch.object(d, "now", return_value=ts):
                    self.assertIsNone(d.emit(self.events / f"L-{actor}-7001.jsonl", base, kind, **fields))
                self.assert_round_trip(view)
            before = list(view)
            disk_before = dst.read_bytes()
            with patch.object(self.fold, "read_events", side_effect=AssertionError("expiry guard re-read")):
                reason = d.emit(dst, base, "escalation-blocking", kind="owed-expired",
                                criteria='["AC1"]', evidence='["fixture"]')
            self.assertIn("open operator ask", reason)
            self.assertEqual(list(view), before)
            self.assertEqual(dst.read_bytes(), disk_before)
            self.assert_round_trip(view)
        signed(self.id(), dict(sequential_views=True, exception_cleanup=True,
                               invalid_packet_reads=0, refused_write_absent=True))


class TestSharedRefresh(ScratchLedger):
    def test_refresh_replaces_instead_of_replaying_self_writes(self):
        d = self.dispatch
        dst = self.events / "L-builder-0001.jsonl"
        base = dict(subject="L-spec-7001", project="fixture", spawn="L-builder-0001")
        with d.dispatch_view(None) as view:
            with patch.object(self.fold, "read_events", wraps=self.fold.read_events) as reads:
                self.assertIs(d.current_events(), view)
                d.emit(dst, base, "build-started", worktree="fixture")
                d.emit(dst, base, "infra-failure", role="builder", **{"class": "runner-or-network"})
                # A different actor commits while the backend is running.
                external = self.events / "L-operator-0001.jsonl"
                external.write_text(json.dumps(dict(v=1, type="decision", subject="L-spec-7001",
                                                     ts=d.now(), decision="late")) + "\n")
                self.assertFalse(any(e.get("decision") == "late" for e in view))
                view.after_backend = True
                self.assertEqual(reads.call_count, 1)
                self.assertIs(d.current_events(), view)
                self.assertEqual(reads.call_count, 2)
                self.assert_round_trip(view)
                d.emit(dst, base, "spawn-failed", why="fixture")
                self.assert_round_trip(view)
                self.assertIs(d.current_events(), view)
                self.assertEqual(reads.call_count, 2)
                self.assertEqual(len({e["_src"] for e in view}), len(view))
                self.assertEqual(len(d._retry_events(view, "L-spec-7001", "builder")), 1)


class TestCompatibility(ScratchLedger):
    def test_fallback_seat_command_publication(self):
        """A worker may inspect an existing packet throughout codex fallback."""
        import builtins
        d = self.dispatch
        seat = self.root / "seat"
        seat.mkdir()
        self.enterContext(patch.object(d, "SEAT", seat))
        spawn = "L-research-7001"
        command = seat / (spawn + ".cmd.json")
        packet = seat / (spawn + ".packet.md")
        command.write_text(json.dumps(dict(cmd=["codex", "exec"], cwd="fixture", path=None)))
        packet.write_text("existing codex packet")
        (seat / (spawn + ".result.json")).write_text('{"is_error": false}')
        observed = []
        original_open = Path.open

        def opened(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if "w" in mode and path.parent == seat and packet.exists():
                try:
                    # Observe at the truncation boundary, before the writer can
                    # fill its file. The published command must remain readable.
                    with builtins.open(command) as stream:
                        observed.append(json.load(stream))
                except BaseException:
                    handle.close()
                    raise
            return handle

        with patch.object(Path, "open", opened):
            result = d.run_seat(spawn, ["claude", "-p"], "seat packet", "fixture", 1,
                                sleep=lambda seconds: None)
        self.assertFalse(json.loads(result.stdout)["is_error"])
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual(json.loads(command.read_text())["cmd"], ["claude", "-p"])
        self.assertEqual(packet.read_text(), "seat packet")
        signed(self.id(), dict(published_commands=observed, fallback_command_complete=True))

    def test_supplied_rows_without_provenance(self):
        seed = dict(type="owed-ac", subject="L-spec-7001", actor="spec-writer", criterion="AC1")
        d = self.dispatch
        with d.dispatch_view([seed]) as view:
            d.emit(self.events / "L-operator-0001.jsonl",
                   dict(subject="L-spec-7001", project="fixture"), "decision", decision="fixture")
            self.assertEqual(view[0], seed)
            self.assertEqual(list(view)[1:], self.read_back())
            self.assertNotIn("_src", seed)
            self.assertNotIn("ts", seed)

    def test_grader_selection_snapshot(self):
        result = TestReadBudget.measure(self, Path(__file__).resolve().parent.parent,
            "grader", "routed-selection", equality=True, post=True, entry="_main")
        TestReadBudget.assert_budget(self, result)
        events = result["equality"]["final"]
        self.assertFalse(any(e["type"] == "criterion-routed" and e["criterion"] == "AC1"
                            and e["to"] == "owed" for e in events))
        self.assertEqual(result["outcome"], 0, result["diagnostic"])
        verdict = next(e for e in events if e["type"] == "verdict")
        self.assertEqual(verdict["n"], 1)
        self.assertTrue(verdict["confirmed"])
        self.assertEqual(verdict["cannot_assess"], [])
        signed(self.id(), dict(outcome=result["outcome"], verdict=verdict))

    def test_existing_regressions(self):
        names = ("test_dispatch.py", "test_dispatch_cwd.py", "test_dispatch_failure_detail.py",
                 "test_dispatch_item_isolation.py", "test_dispatch_manual_gate.py", "test_spawns.py")
        for name in names:
            with self.subTest(module=name):
                result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name(name))],
                                        env=self.env, text=True, capture_output=True)
                signed(name, dict(exit_code=result.returncode,
                                  stdout_tail=result.stdout[-1500:], stderr_tail=result.stderr[-1500:]))
                self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])

    def test_cache_failure_and_direct_helpers(self):
        import ledger_cache
        import spawns
        d = self.dispatch
        dst = self.events / "L-operator-0001.jsonl"
        d.emit(dst, dict(subject="L-spec-7001", project="fixture"), "decision", decision="fixture")
        reference = self.read_back()
        with patch.object(ledger_cache, "read", side_effect=OSError("fixture cache failure")):
            self.assertEqual(d.current_events(), reference)
            with d.dispatch_view(None):
                self.assertEqual(d.current_events(), reference)
                self.assertEqual(d.subject_ids("L-spec-"), [7001])
        with patch.object(self.fold, "read_events", side_effect=AssertionError("supplied input re-read")):
            self.assertEqual(d.coverage_changes([], events=reference), [])
            self.assertEqual(d.commit_shape_events("L-spec-7001", "DONE", "pin", "ready", ".",
                                                   events=reference), [])
            self.assertEqual(spawns.clear_events([], {}, "L-spec-7001", False, "fixture",
                                                 events=reference), [])
        signed(self.id(), dict(cache_fallback_equal=True, supplied_helpers_read_count=0))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--budget-child"]:
        budget_child(json.loads(sys.argv[2]))
    else:
        unittest.main()
