#!/usr/bin/env python3
"""Need grants through the production manifest, packet and dispatch paths."""
import argparse
import atexit
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_sandbox = tempfile.TemporaryDirectory(prefix="sweep-need-")
atexit.register(_sandbox.cleanup)
os.environ["DOIT_ROOT"] = _sandbox.name
os.environ["DOIT_NO_POKE"] = "1"
for _key in list(os.environ):
    if _key.startswith("SUPABASE_DB_URL") or _key in ("DOIT_LEDGER_FILE", "DOIT_SEAT"):
        os.environ.pop(_key)

import dispatch
import fold
import models
import packet
import sweep_owed


class BackendStopped(BaseException):
    """Stop at the mocked backend after dispatch has recorded its grant."""


class NeedGrants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_sandbox.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("content", "events", "cwd"):
            (self.root / name).mkdir()
        for module in (dispatch, fold, packet):
            for attr in ("ROOT", "EVENTS", "CONTENT"):
                if hasattr(module, attr):
                    value = self.root if attr == "ROOT" else self.root / attr.lower()
                    self.enterContext(patch.object(module, attr, value))
        self.enterContext(patch.dict(os.environ, DOIT_ROOT=str(self.root)))
        self.spec = self.root / "content/L-spec-9901.md"
        self.spec.write_text("# Fixture\n\n## Acceptance criteria\nAC1 [backend]: Read the fixture.\n")
        self.row = dict(spec=self.spec.stem, criterion="AC1", declared_src="scratch",
                        line="fixture", shipped_sha="fixture-shipped", base_sha="fixture-base")
        # Test data only. Never print it or any env content.
        self.sentinel = "fixture-private-value-" + self.root.name

    def manifest(self, subject, need=None, kind="plain", cwd=None):
        cwd = cwd or self.root / "cwd"
        text = sweep_owed._manifest_text(subject, "fixture", str(cwd), [self.row], kind=kind)
        if need is not None:
            text = text.replace("\n## Rows", f"\nneed: {need}\n\n## Rows")
        path = self.root / "content" / f"{subject}.md"
        path.write_text(text)
        return path

    def render(self, subject):
        context = argparse.Namespace(a=argparse.Namespace(subject=subject))
        with patch.object(packet, "review_account", return_value="not supplied"):
            return "\n".join(packet.p_owed_sweeper(context))

    def signed(self, criterion, body):
        encoded = json.dumps(body, sort_keys=True)
        self.assertNotIn(self.sentinel, encoded)
        print(json.dumps(dict(url=f"cli://sweep-need/{criterion}", status=0,
                             body_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                             body_excerpt=body)))

    def test_ac1_manifest_and_packet(self):
        import sweep_need
        self.assertEqual(sweep_need.NEEDS, ("ro-dsn", "repo", "droplet", "environment"))
        grants = {}
        for need in sweep_need.NEEDS:
            with self.subTest(need=need):
                kind = sweep_need.manifest_kind(need)
                self.assertEqual(kind, "repo" if need in ("repo", "environment") else "plain")
                subject = f"L-sweep-{need}"
                path = self.manifest(subject, need, kind)
                self.assertEqual(packet.parse_sweep_manifest(path)["need"], need)
                env = self.root / "cwd/.env"
                env.write_text(self.sentinel)
                text = self.render(subject)
                self.assertNotIn(self.sentinel, text)
                if need == "droplet":
                    self.assertIn("scripts/sweep/droplet-read", text)
                    self.assertIn("read-only", text)
                    self.assertNotIn("not available for this batch", text)
                    self.assertNotIn(str(env), text)
                    self.assertIn(sweep_need.grant_line(need), text)
                    env.unlink()
                    self.assertEqual(self.render(subject), text)
                elif need == "ro-dsn":
                    self.assertIn(f"set -a; . {env}; set +a", text)
                    env.unlink()
                    self.assertIn("not available for this batch (capability=ro-dsn)", self.render(subject))
                else:
                    self.assertIn("kind: repo — run each row's own Verification", text)
                    self.assertIn("Production DSNs stay unset for repo checks.", text)
                    self.assertNotIn(str(env), text)
                    env.unlink()
                grants[need] = [line for line in text.splitlines()
                                if line.startswith("3.") or "kind: repo" in line]

        path = self.manifest("L-sweep-legacy")
        self.assertIsNone(packet.parse_sweep_manifest(path)["need"])
        expected = [
            f"1. Batch `L-sweep-legacy` — project `fixture`, cwd `{self.root / 'cwd'}`.",
            "2. Every due check in this batch, each the criterion's own full block, verbatim, from its own spec — nothing else:",
            "   spec L-spec-9901 — criterion AC1 (declared: scratch @ fixture):",
            "     AC1 [backend]: Read the fixture.",
            "3. This batch's own read-only DB credential, for a `live_db` observation: not available for this batch (capability=ro-dsn).",
            "4. The review account for this app: not supplied. It may never delete and never move money.",
            "5. Cannot-observe capabilities: " + " | ".join(__import__("owed_routes").ROUTES) + ".",
        ]
        self.assertEqual(self.render("L-sweep-legacy").encode(), "\n".join(expected).encode())
        for index, kind in enumerate(("plain", "browser", "repo")):
            legacy = self.manifest(f"L-sweep-legacy-{index}", kind=kind)
            self.assertIsNone(packet.parse_sweep_manifest(legacy)["need"])
            self.assertEqual(packet.parse_sweep_manifest(legacy)["kind"], kind)
        for need in ("unknown", "", "ro-dsn trailing"):
            path = self.manifest("L-sweep-invalid", need)
            with self.assertRaisesRegex(SystemExit, "unsupported sweep need"):
                packet.parse_sweep_manifest(path)
        path = self.manifest("L-sweep-invalid-kind", kind="unknown")
        with self.assertRaisesRegex(SystemExit, "unsupported sweep kind"):
            packet.parse_sweep_manifest(path)
        self.signed("AC1", dict(grants=grants, legacy_byte_identical=True,
                                invalid_needs_rejected=True, invalid_kind_rejected=True,
                                sentinel_absent=True))

    def test_ac2_dispatch_isolation(self):
        import sweep_need
        results = []
        cases = [(need, sweep_need.manifest_kind(need)) for need in sweep_need.NEEDS]
        cases += [(None, kind) for kind in ("plain", "browser", "repo")]
        cases += [("ro-dsn", "repo")]
        for role in ("owed-sweeper", "owed-verifier"):
            for index, (need, kind) in enumerate(cases):
                with self.subTest(role=role, need=need, kind=kind):
                    cwd = self.root / f"{role}-{index}"
                    cwd.mkdir()
                    subject = f"L-sweep-{role}-{index}"
                    self.manifest(subject, need, kind, cwd)
                    packet_file = self.root / "packet.md"
                    packet_file.write_text("Isolated fixture packet\n")
                    a = argparse.Namespace(role=role, subject=subject, packet=str(packet_file),
                                           path=None, cwd=str(cwd), project="fixture", charter=None,
                                           mcp_config=None, timeout=None, max_usd=None, seat=False)

                    def provision(destination, checkout):
                        self.assertEqual(Path(destination), cwd)
                        self.assertEqual(checkout, self.root / "repos/fixture")
                        (cwd / ".env").write_text(self.sentinel)
                        return "readonly"

                    with patch.object(models, "resolve", return_value=dict(
                            backend="codex", model="fixture", source="fixture")), \
                            patch.object(fold, "read_events", return_value=[]), \
                            patch.object(dispatch, "poke"), \
                            patch.object(dispatch, "ensure_room", return_value=True), \
                            patch.object(dispatch, "readonly_dsn", side_effect=AssertionError("real credential read")), \
                            patch.object(dispatch, "provision_sweep_env", side_effect=provision) as spy, \
                            patch.object(dispatch, "run_codex", side_effect=BackendStopped) as backend:
                        with self.assertRaises(BackendStopped):
                            dispatch._main(a)
                        backend.assert_called_once()
                    wants = kind != "repo" and (need is None or need == "ro-dsn")
                    self.assertEqual(spy.call_count, int(wants))
                    self.assertEqual((cwd / ".env").exists(), wants)
                    events = [json.loads(line) for path in (self.root / "events").glob("*.jsonl")
                              for line in path.read_text().splitlines()]
                    start = next(e for e in events if e.get("subject") == subject and e["type"] == "spawn-started")
                    self.assertEqual(start["dsn_role"], "readonly" if wants else "none")
                    results.append(dict(role=role, need=need, kind=kind, provision_calls=spy.call_count,
                                        cwd_files=sorted(p.name for p in cwd.iterdir()), dsn_role=start["dsn_role"]))
        self.signed("AC2", results)


if __name__ == "__main__":
    unittest.main()
