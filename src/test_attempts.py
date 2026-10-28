"""SD1 regression fixtures; --replay-live is an explicitly read-only observation."""
import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import attempts

# Set this before any import of the dispatch stack: its paths bind at import.
_REPLAY = '--replay-live' in sys.argv
_FIXTURE_ROOT = None if _REPLAY else tempfile.TemporaryDirectory(prefix='attempts-')
if _FIXTURE_ROOT is not None:
    os.environ['DOIT_ROOT'] = _FIXTURE_ROOT.name
import fold
import autodispatch
import lane_rules

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
SUBJECT = 'L-spec-fixture-attempts'
ROLES = ('grader', 'reviewer', 'builder', 'spec-auditor')
OUTPUTS = {'grader': ('verdict', 'cannot-assess'), 'reviewer': ('review',),
           'builder': ('build-done', 'build-blocked'), 'spec-auditor': ('spawn-done',)}
# Original event fields, copied from the live ledger, without fold enrichment.
REAL = json.loads(r'''
[
  {
    "backend": "seat",
    "dsn_role": "none",
    "project": "albert-scott",
    "role": "grader",
    "sandbox": "bwrap",
    "spawn": "L-grader-1477",
    "subject": "L-spec-0962",
    "ts": "2026-10-15T07:10:52+00:00",
    "type": "spawn-started",
    "v": 1,
    "waiter_host": "Albert-Scott-Cloud",
    "waiter_pid": 142134,
    "waiter_proc_start": "264551",
    "window_min": 30
  },
  {
    "cap_min": 30,
    "reason": "waiter-dead",
    "role": "grader",
    "spawn": "L-grader-1477",
    "subject": "L-spec-0962",
    "ts": "2026-10-15T07:16:23+00:00",
    "type": "spawn-stale",
    "v": 1
  },
  {
    "backend": "seat",
    "charter": "L-charter-0057",
    "issued_by": "planner",
    "project": "albert-scott",
    "role": "spec-auditor",
    "spawn": "L-spec-auditor-0376",
    "subject": "L-spec-1344",
    "ts": "2026-10-21T01:55:20+00:00",
    "type": "spawn-started",
    "v": 1,
    "waiter_host": "Albert-Scott-Cloud",
    "waiter_pid": 3634592,
    "waiter_proc_start": "49914396",
    "window_min": 15
  },
  {
    "backend": "seat",
    "bad_cut": false,
    "cache_creation": 70577,
    "cache_read": 1522414,
    "charter": "L-charter-0057",
    "cli": "2.1.305",
    "contamination": false,
    "contract_sha256": "5ffec0f32b12aee5",
    "cost_usd": null,
    "denied": [],
    "duration_ms": 247065,
    "first_on_model": false,
    "input_tokens": 66,
    "model": "claude-opus-5-5",
    "model_map": "models.toml",
    "model_match": true,
    "model_observed": true,
    "model_requested": "claude-opus-5-5",
    "model_used": "claude-opus-5-5",
    "output_tokens": 22979,
    "packet_sha256": "69b43e2b77f9866a",
    "project": "albert-scott",
    "session": "ad3293008904041bd",
    "spawn": "L-spec-auditor-0376",
    "spawn_path": "seat",
    "subagent_tokens": 77208,
    "subject": "L-spec-1344",
    "ts": "2026-10-21T02:04:03+00:00",
    "turns": 33,
    "type": "spawn-done",
    "v": 1
  },
  {
    "role": "relay",
    "spawn": "L-relay-1694",
    "subject": "L-relay-1694",
    "ts": "2026-10-24T14:02:51+00:00",
    "type": "spawn-failed",
    "v": 1,
    "why": "FileNotFoundError: [Errno 2] No such file or directory: 'claude'"
  },
  {
    "backend": "seat",
    "charter": "L-charter-0068",
    "cli": "2.1.311",
    "contract_sha256": "4e2192f6292110c0",
    "model": "claude-sonnet-5-5",
    "model_map": "models.toml",
    "model_requested": "claude-sonnet-5-5",
    "packet_sha256": "0e275b06ac605790",
    "project": "albert-scott",
    "reason": "bundle-pending",
    "spawn": "L-grader-3380",
    "spawn_path": "seat",
    "subject": "L-spec-1648",
    "tokens": null,
    "ts": "2026-10-27T00:31:50+00:00",
    "type": "spawn-failed",
    "v": 1,
    "why": "evidence bundle pending"
  },
  {
    "base_sha": "8afbd159a6e57f2b2b808b72eb4a279c996891e8",
    "bundle_sha256": "a261ab3844db06013e6592950915ed845150bed9e9e24bc88008ea8c3b9c426d",
    "n_commands": 1,
    "n_failed": 0,
    "n_new": 0,
    "path": "/home/albert/.do-it/content/evidence/L-spec-1648/50e09c3f01a5-all.json",
    "project": "albert-scott",
    "sha": "50e09c3f01a5beef6411440bd871a2537ec2b6b0",
    "subject": "L-spec-1648",
    "ts": "2026-10-27T00:36:55+00:00",
    "type": "evidence-bundle",
    "v": 1
  },
  {
    "backend": "seat",
    "dsn_role": "none",
    "issued_by": "unknown",
    "project": "albert-scott",
    "role": "grader",
    "sandbox": "bwrap",
    "spawn": "L-grader-3438",
    "subject": "L-spec-1319",
    "ts": "2026-10-27T05:49:55+00:00",
    "type": "spawn-started",
    "v": 1,
    "waiter_host": "Albert-Scott-Cloud",
    "waiter_pid": 2999460,
    "waiter_proc_start": "103154615",
    "window_min": 30
  },
  {
    "cannot_assess": [],
    "card_ok": "yes",
    "confirmed": true,
    "judgment_only": [],
    "matches_intent": "yes",
    "missing_capability": [],
    "n": 8,
    "n_judgment_only": 0,
    "pre_existing": [],
    "project": "albert-scott",
    "spawn": "L-grader-3438",
    "subject": "L-spec-1319",
    "ts": "2026-10-27T05:53:23+00:00",
    "type": "verdict",
    "v": 1
  },
  {
    "backend": "seat",
    "backend_ran": true,
    "cache_creation": 61231,
    "cache_read": 135640,
    "card_ok": "yes",
    "cli": "2.1.311",
    "contamination": false,
    "contract_sha256": "4e2192f6292110c0",
    "cost_usd": null,
    "could_not_run": false,
    "denied": [],
    "duration_ms": 25648,
    "first_on_model": false,
    "input_tokens": 6,
    "matches_intent": "yes",
    "model": "claude-sonnet-5-5",
    "model_map": "models.toml",
    "model_match": true,
    "model_observed": true,
    "model_requested": "claude-sonnet-5-5",
    "model_used": "claude-sonnet-5-5",
    "output_tokens": 3061,
    "packet_sha256": "7cc77cbf5fd7d255",
    "project": "albert-scott",
    "role": "grader",
    "session": "a705878d00659c7f7",
    "spawn": "L-grader-3438",
    "spawn_path": "seat",
    "subagent_tokens": 80604,
    "subject": "L-spec-1319",
    "tokens": {
      "cached_input": 135640,
      "input": 196877,
      "output": 3061,
      "reasoning": null,
      "vendor": "claude"
    },
    "ts": "2026-10-27T05:53:23+00:00",
    "turns": 3,
    "type": "spawn-done",
    "v": 1
  },
  {
    "backend": "codex",
    "base_sha": "31dd580e1da06ef83c4a822d6d59ac2ea175cb5d",
    "dsn_role": "readonly",
    "issued_by": "executor",
    "project": "albert-scott",
    "spawn": "L-builder-3778",
    "subject": "L-spec-1661",
    "ts": "2026-10-27T06:39:45+00:00",
    "type": "build-started",
    "v": 1,
    "waiter_host": "Albert-Scott-Cloud",
    "waiter_pid": 3500899,
    "waiter_proc_start": "103458604",
    "wave_note": "undetermined",
    "window_min": 90,
    "worktree": "/home/albert/.do-it/worktrees/albert-scott/l-spec-1661"
  },
  {
    "base_sha": "31dd580e1da06ef83c4a822d6d59ac2ea175cb5d",
    "base_sha_explicit": true,
    "branch": "l-spec-1661",
    "card": "/home/albert/.do-it/content/L-card-1661.md",
    "project": "albert-scott",
    "ready_sha": "56341c491e065b33e68b14ee568ca475396814b1",
    "spawn": "L-builder-3778",
    "status": "BLOCKED",
    "subject": "L-spec-1661",
    "tests_added": false,
    "ts": "2026-10-27T06:42:36+00:00",
    "type": "build-done",
    "v": 1,
    "verify_exit": 1
  },
  {
    "project": "albert-scott",
    "reason": "Expand the write grant to update the two conflicting assertions in tests/ci/test_known_red.py. Default: retain the single committed quarantine change and report BLOCKED.",
    "spawn": "L-builder-3778",
    "subject": "L-spec-1661",
    "ts": "2026-10-27T06:42:36+00:00",
    "type": "build-blocked",
    "v": 1
  },
  {
    "backend": "codex",
    "backend_ran": true,
    "cache_creation": 0,
    "cache_read": 472832,
    "cli": "2.1.311",
    "contract_sha256": "f5c62ad48e7c3afc",
    "cost_usd": null,
    "denied": [],
    "duration_ms": 160096,
    "first_on_model": false,
    "input_tokens": 498659,
    "model": "gpt-6.1-sol",
    "model_map": "models.toml",
    "model_match": true,
    "model_observed": false,
    "model_requested": "gpt-6.1-sol",
    "model_used": "gpt-6.1-sol",
    "output_tokens": 4067,
    "packet_sha256": "3b8bc0addf5293de",
    "project": "albert-scott",
    "role": "builder",
    "session": "01a17c16-2282-7ad2-8113-ae17c4716e40",
    "spawn": "L-builder-3778",
    "spawn_path": "codex",
    "status": "BLOCKED",
    "subagent_tokens": null,
    "subject": "L-spec-1661",
    "tokens": {
      "cached_input": 472832,
      "input": 498659,
      "output": 4067,
      "reasoning": 1352,
      "vendor": "codex"
    },
    "ts": "2026-10-27T06:42:36+00:00",
    "turns": 1,
    "type": "spawn-done",
    "v": 1
  },
  {
    "backend": "seat",
    "issued_by": "executor",
    "project": "albert-scott",
    "role": "reviewer",
    "spawn": "L-reviewer-1963",
    "subject": "L-spec-1244",
    "ts": "2026-10-27T06:42:49+00:00",
    "type": "spawn-started",
    "v": 1,
    "waiter_host": "Albert-Scott-Cloud",
    "waiter_pid": 3523301,
    "waiter_proc_start": "103475990",
    "window_min": 30
  },
  {
    "depth": "full",
    "n_blocking": 0,
    "project": "albert-scott",
    "round": 2,
    "spawn": "L-reviewer-1963",
    "subject": "L-spec-1244",
    "ts": "2026-10-27T06:47:32+00:00",
    "type": "review",
    "v": 1
  },
  {
    "backend": "seat",
    "backend_ran": true,
    "cache_creation": 42421,
    "cache_read": 233530,
    "cli": "2.1.311",
    "contract_sha256": "d15e53524cffe529",
    "cost_usd": null,
    "denied": [],
    "depth": "full",
    "duration_ms": 106073,
    "first_on_model": false,
    "input_tokens": 10,
    "model": "claude-sonnet-5-5",
    "model_map": "models.toml",
    "model_match": true,
    "model_observed": true,
    "model_requested": "claude-sonnet-5-5",
    "model_used": "claude-sonnet-5-5",
    "output_tokens": 2738,
    "packet_sha256": "b55d52ecfd2aef8b",
    "project": "albert-scott",
    "role": "reviewer",
    "round": 2,
    "session": "a2dc18c16bf096128",
    "spawn": "L-reviewer-1963",
    "spawn_path": "seat",
    "subagent_tokens": 61895,
    "subject": "L-spec-1244",
    "tokens": {
      "cached_input": 233530,
      "input": 275961,
      "output": 2738,
      "reasoning": null,
      "vendor": "claude"
    },
    "ts": "2026-10-27T06:47:32+00:00",
    "turns": 5,
    "type": "spawn-done",
    "v": 1
  }
]
''')


def event(kind, minute=0, subject=SUBJECT, **extra):
    row = dict(type=kind, subject=subject,
               ts=(NOW + timedelta(minutes=minute)).isoformat())
    row.update(extra)
    return row


def start(role='grader', minute=1, number=900001):
    extra = {} if role == 'builder' else {'role': role}
    return event('build-started' if role == 'builder' else 'spawn-started', minute,
                 spawn=f'L-{role}-{number}', **extra)


def result(evs, role='grader', trigger=None, **kwargs):
    return attempts.history(evs, event('trigger') if trigger is None else trigger,
                            role, now=kwargs.pop('now', NOW), **kwargs)


def failure(minute=1, number=900001, **extra):
    row = event('spawn-failed', minute, spawn=f'L-grader-{number}')
    row.update(extra)
    return row


class AttemptsTests(unittest.TestCase):
    def flags(self, actual, **expected):
        for key, value in expected.items():
            self.assertEqual(getattr(actual, key), value, (key, actual))

    def ok(self, n):
        print(f'L-spec-1662 AC{n} ok')

    def test_ac1_consumption(self):
        for role in ROLES:
            s = start(role)
            for kind in OUTPUTS[role]:
                output = event(kind, 2, spawn=s['spawn'])
                if role == 'spec-auditor':
                    output['role'] = role
                for evs in ([s, output], [output],
                            [s, output, dict(output, type='spawn-done', ts=event('x', 3)['ts'])],
                            [s, dict(s, type='spawn-done'), output]):
                    self.flags(result(evs, role), consumed=True, live=False,
                               exhausted=False, next_retry_at=None, not_ready=None)
                self.flags(result([s, dict(output, ts=event('x', -1)['ts'])], role), consumed=False)
                self.flags(result([dict(s, ts=event('x', -2)['ts']), output], role), consumed=False)
                self.flags(result([dict(output, subject='other')], role), consumed=False)
                print('consumption', role, kind)
            for kind in ('queue-add', 'merge-gate-clean', 'shipped', 'spec-killed', 'spec-closed'):
                self.assertTrue(result([event(kind, 1)], role).consumed)
                print('lifecycle', role, kind)
            if role != 'spec-auditor':
                self.assertFalse(result([dict(s, type='spawn-done')], role).consumed)
        self.assertFalse(result([event('verdict', 1)], 'reviewer').consumed)
        self.assertFalse(result([event('review', 1)], 'grader').consumed)
        self.ok(1)

    def test_ac2_live_dead(self):
        for role in ROLES:
            s = start(role)
            self.flags(result([s], role), consumed=False, live=True, n_failed=0,
                       reasons=(), next_retry_at=None, exhausted=False, not_ready=None)
            stale = dict(s, type='spawn-stale', ts=event('x', 3)['ts'])
            for evidence, dead, failed_at in (
                ([], frozenset({s['spawn']}), 1),
                ([stale], frozenset(), 3),
                ([event('autodispatched', 4, replaces=s['spawn'])], frozenset(), 1),
                ([event('escalation-blocking', 4, dead_spawn=s['spawn'])], frozenset(), 1),
                ([stale], frozenset({s['spawn']}), 3)):
                a = result([s, *evidence], role, dead=dead)
                self.flags(a, live=False, n_failed=1, reasons=(f"dead: {s['spawn']}",),
                           next_retry_at=NOW + timedelta(minutes=failed_at + 5))
                print('dead', role, [e['type'] for e in evidence], a)
            self.assertEqual(result([s], role), result([s], role, dead=frozenset({'missing'})))
        failures = [failure(i, i, reason=str(i)) for i in (1, 2, 3)]
        self.flags(result(failures + [start(minute=4)]), live=True, exhausted=False,
                   next_retry_at=None, n_failed=3)
        self.ok(2)

    def test_ac3_failures(self):
        rows = [
            (failure(reason='disk', why='something else'), 'disk'),
            (failure(why='timeout after 15 min'), 'timeout after 15 min'),
            (failure(), 'spawn-failed (no reason recorded)'),
            (event('autodispatch-failed', 1, role='grader', reason='literal'), 'literal'),
            (event('autodispatch-failed', 1, role='grader'), 'autodispatch-failed (no reason recorded)')]
        for e, reason in rows:
            self.flags(result([e]), consumed=False, live=False, n_failed=1, reasons=(reason,))
            print('failure', e['type'], reason)
        for role in ROLES[:-1]:
            s = start(role)
            reason = 'spawn-done without ' + '/'.join(OUTPUTS[role])
            self.flags(result([dict(s, type='spawn-done')], role), consumed=False,
                       live=False, n_failed=1, reasons=(reason,))
            print('failure', role, reason)
        derived = failure(spawn='L-grader-3380')
        self.assertEqual(result([derived]).n_failed, 1)
        self.assertEqual(result([derived], 'reviewer').n_failed, 0)
        relay = event('spawn-failed', 1, role='relay', spawn='L-relay-1694')
        for role in ROLES:
            self.assertEqual(result([relay], role).n_failed, 0)
        self.assertEqual(result([event('autodispatch-failed', 1, role='reviewer')]).n_failed, 0)
        self.assertEqual(result([failure(), dict(failure(2), type='spawn-done')]).n_failed, 1)
        self.assertEqual(result([failure(-1)]).n_failed, 0)
        mixed = [event('autodispatch-failed', 3, role='grader', reason='third'),
                 failure(2, reason='first'), dict(start(number=900002, minute=3), type='spawn-done')]
        self.flags(result(mixed), n_failed=3, reasons=('first', 'third', 'spawn-done without verdict/cannot-assess'))
        self.ok(3)

    def test_ac4_backoff(self):
        self.assertEqual(attempts.BACKOFF_MIN, (5, 15, 45))
        self.assertEqual(attempts.MAX_ATTEMPTS, 3)
        evs = [failure(i, i, reason=f'f{i}') for i in range(1, 5)]
        for n, delay in ((1, 5), (2, 15), (3, None), (4, None)):
            expected = None if delay is None else NOW + timedelta(minutes=n + delay)
            for clock in (NOW, NOW + timedelta(days=1)):
                self.flags(result(evs[:n], now=clock), n_failed=n, exhausted=n >= 3,
                           next_retry_at=expected)
            print('backoff', n, expected)
        self.flags(result(evs[:2], trigger=event('trigger', 3)), n_failed=0,
                   exhausted=False, next_retry_at=None)
        self.assertEqual(result(evs[:2]).n_failed, 2)
        self.flags(result(evs[:3] + [event('verdict', 4)]), consumed=True,
                   exhausted=False, n_failed=3, next_retry_at=None)
        self.ok(4)

    def test_ac5_not_ready(self):
        refusal = failure(2, reason='bundle-pending')
        self.flags(result([refusal]), n_failed=0, reasons=(), not_ready='bundle-pending',
                   consumed=False, live=False, exhausted=False, next_retry_at=None)
        for kind in ('evidence-bundle', 'evidence-bundle-failed'):
            self.flags(result([refusal, event(kind, 3)]), not_ready=None, n_failed=0, consumed=False)
            for minute in (1, 2):
                self.assertEqual(result([refusal, event(kind, minute)]).not_ready, 'bundle-pending')
            self.assertEqual(result([refusal, event(kind, 3, subject='other')]).not_ready, 'bundle-pending')
            print('bundle timeline', kind, 'before/equal open; after cleared')
        self.assertIsNone(result([refusal, start(minute=3)]).not_ready)
        self.flags(result([failure(i, i, reason='bundle-pending') for i in (1, 2, 3)]),
                   n_failed=0, exhausted=False)
        self.flags(result([failure(reason='disk'), refusal]), n_failed=1,
                   not_ready='bundle-pending', next_retry_at=NOW + timedelta(minutes=6))
        self.assertEqual(result([failure(reason='bundle-pending: waiting')]).not_ready, 'bundle-pending')
        self.assertEqual(result([failure(why='bundle-pending: waiting')]).not_ready, 'bundle-pending')
        self.ok(5)

    def test_ac6_purity_validation(self):
        signature = inspect.signature(attempts.history)
        self.assertEqual(list(signature.parameters), ['evs', 'trigger', 'role', 'now', 'dead'])
        for name in ('now', 'dead'):
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(signature.parameters['dead'].default, frozenset())
        self.assertEqual([f.name for f in fields(attempts.Attempts)],
                         ['consumed', 'live', 'n_failed', 'reasons', 'next_retry_at', 'exhausted', 'not_ready'])
        evs = [failure(2, 1, reason='two'), failure(1, 2, why='one'), start(minute=3)]
        trigger, dead = event('trigger'), frozenset({'missing'})
        before = deepcopy((evs, trigger, dead))
        a = result(evs, trigger=trigger, dead=dead)
        self.assertEqual(a, result(evs, trigger=trigger, dead=dead))
        self.assertEqual(a, result(evs[::-1], trigger=trigger, dead=dead))
        self.assertEqual((evs, trigger, dead), before)
        with self.assertRaises(FrozenInstanceError):
            a.live = False
        tree = ast.parse(Path(attempts.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.fail('only from dataclasses/datetime imports are allowed')
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, ('dataclasses', 'datetime'))
            if isinstance(node, ast.Call):
                self.assertNotIn(ast.unparse(node.func), ('open', 'print', 'datetime.now', 'datetime.utcnow'))
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ('os', 'socket', 'subprocess', 'time'))
        code = "import sys; before=set(sys.modules); import attempts; forbidden={'lane_rules','autodispatch','dispatch','fold','tick','panes','socket','subprocess'}; assert not (forbidden & (set(sys.modules)-before)); print('fresh import clean')"
        run = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parent,
                             text=True, capture_output=True, check=True)
        print(run.stdout.strip())
        self.assertEqual(attempts.DISPATCH_EVENTS, lane_rules.DISPATCH_EVENTS)
        self.assertEqual(attempts.TERMINAL_EVENTS, lane_rules.TERMINAL_EVENTS)
        print('DISPATCH_EVENTS equal; TERMINAL_EVENTS equal')
        with patch('builtins.open', side_effect=AssertionError), patch('socket.socket', side_effect=AssertionError), patch('subprocess.Popen', side_effect=AssertionError):
            self.assertEqual(a, result(evs, trigger=trigger, dead=dead))
        bad = [('role', dict(role='spec-writer')), ('trigger.ts', dict(trigger={'subject': SUBJECT})),
               ('trigger.ts', dict(trigger=event('trigger', ts='2026-10-27T12:00:00'))),
               ('trigger.ts', dict(trigger=event('trigger', ts='broken'))),
               ('trigger.subject', dict(trigger={'ts': NOW.isoformat()})),
               ('now', dict(now=NOW.replace(tzinfo=None))),
               ('event.ts', dict(evs=[failure(ts='2026-10-27T12:01:00')])),
               ('event.ts', dict(evs=[failure(ts='broken')]))]
        for field, kwargs in bad:
            args = dict(evs=[], trigger=trigger, role='grader', now=NOW)
            args.update(kwargs)
            with self.assertRaisesRegex(ValueError, field):
                attempts.history(**args)
            print('rejected', field, 'ValueError')
        self.assertFalse(result([event('spawn-failed', subject='other', ts='broken')]).consumed)
        self.assertEqual(result([failure(ts='2026-10-27T12:01:00Z')]).n_failed, 1)
        self.ok(6)

    def test_ac7_precedence_isolation(self):
        self.flags(result([start(), event('verdict', 2)]), consumed=True, live=False)
        b = failure(2, reason='B', subject='B')
        a = failure(1, reason='A')
        self.assertEqual(result([a, b]).reasons, ('A',))
        self.assertEqual(result([a, b], trigger=event('trigger', subject='B')).reasons, ('B',))
        self.flags(result([event('autodispatched', 1, role='grader', spawn='L-grader-1')]),
                   consumed=False, live=False, n_failed=0)
        self.assertEqual(result([start(minute=-1), failure(1)]).n_failed, 0)
        s = start('builder', 0)
        self.assertTrue(result([s], 'builder', trigger=s).live)
        self.flags(result([start(), failure(2), start(minute=3, number=900002)]), live=True, n_failed=1)
        # Reversals cover attempt ownership, output races and persisted evidence.
        rows = [[start(minute=-1), failure(2), event('verdict', 3, spawn='L-grader-900001')],
                [start(), dict(start(minute=3), type='spawn-stale')],
                [failure(2, reason='bundle-pending'), event('evidence-bundle', 3)]]
        for row in rows:
            self.assertEqual(result(row), result(row[::-1]))
            print('precedence', result(row))
        self.ok(7)

    def test_ac8_real_events(self):
        def rows(spawn):
            return [deepcopy(e) for e in REAL if e.get('spawn') == spawn]
        cases = [('L-builder-3778', 'builder'), ('L-grader-3438', 'grader'),
                 ('L-reviewer-1963', 'reviewer'), ('L-spec-auditor-0376', 'spec-auditor')]
        for spawn, role in cases:
            evs = rows(spawn)
            trigger = next(e for e in evs if e['type'] in ('spawn-started', 'build-started'))
            a = result(evs, role, trigger=trigger)
            self.assertTrue(a.consumed)
            print('real', spawn, trigger['subject'], a)
        refusal = rows('L-grader-3380')
        trigger = dict(refusal[0], ts='2026-10-27T00:30:00+00:00', type='build-done')
        self.flags(result(refusal, trigger=trigger), not_ready='bundle-pending', n_failed=0)
        bundle = next(e for e in REAL if e['type'] == 'evidence-bundle')
        self.flags(result(refusal + [bundle], trigger=trigger), not_ready=None, consumed=False)
        print('real L-grader-3380 bundle-pending; later bundle clears')
        relay = next(e for e in REAL if e.get('role') == 'relay' and e['type'] == 'spawn-failed')
        for role in ROLES:
            self.assertEqual(result([relay], role, trigger=relay).n_failed, 0)
        print('real', relay['spawn'], 'relay ignored for all four roles')
        evs = rows('L-grader-1477')
        trigger = next(e for e in evs if e['type'] == 'spawn-started')
        self.flags(result(evs, trigger=trigger), n_failed=1, reasons=('dead: L-grader-1477',))
        print('real L-grader-1477 persisted stale failed')
        self.ok(8)

    def test_ac9_dead_spawns_seam(self):
        self.assertEqual(fold.EVENTS, Path(_FIXTURE_ROOT.name) / 'events')
        fold.EVENTS.mkdir(exist_ok=True)
        print('temporary DOIT_ROOT: fold.EVENTS isolated')
        for age, stale in ((30, True), (150, True), (30, False)):
            s = start('builder', -age - 5, number=990000 + age + int(stale))
            evs = [s]
            if stale:
                evs.append(dict(s, type='spawn-stale', role='builder', ts=event('x', -age)['ts']))
            self.assertFalse((fold.EVENTS / (s['spawn'] + '.jsonl')).exists())
            rows = autodispatch.dead_spawns(evs, NOW)
            if age == 30 and stale:
                row = next(r for r in rows if r['spawn'] == s['spawn'])
                self.assertEqual(row['role'], 'builder')
                self.assertEqual(row['visible_at'], evs[1]['ts'])
            else:
                self.assertEqual(rows, [])
            a = result(evs, 'builder', trigger=s, dead=frozenset(r['spawn'] for r in rows))
            self.flags(a, n_failed=int(stale), live=not stale)
            print('dead_spawns seam', age, stale, rows, a)
        self.ok(9)


def replay_live():
    """Independent R1.d/e consumption filter; never use history to derive expected."""
    events = fold.read_events(use_cache=False)
    clock = datetime.now(timezone.utc)
    cutoff = datetime(2026, 10, 20, tzinfo=timezone.utc)
    def stamp(e):
        value = datetime.fromisoformat(e['ts'].replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('event.ts must be timezone-aware')
        return value
    def grader(e):
        role = e.get('role')
        if role:
            return role == 'grader'
        spawn = e.get('spawn', '')
        return spawn.startswith('L-grader-') and spawn[len('L-grader-'):].isdigit()
    subjects = sorted({e['subject'] for e in events if e.get('type') == 'spawn-failed'
                       and grader(e) and (e.get('reason') or e.get('why') or '').startswith('bundle-pending')
                       and stamp(e) >= cutoff})
    print('bundle-pending subjects since 2026-10-20:', ', '.join(subjects))
    n = mismatches = 0
    for subject in subjects:
        evs = [e for e in events if e.get('subject') == subject]
        builds = [e for e in evs if e.get('type') == 'build-done' and e.get('status') == 'DONE']
        if not builds:
            print(subject, 'excluded: no build-done status=DONE')
            continue
        trigger = max(builds, key=stamp)
        t = stamp(trigger)
        first = {}
        for e in evs:
            if grader(e) and e.get('spawn') and e.get('type') in ('spawn-started', 'build-started', 'spawn-failed', 'spawn-done', 'spawn-stale'):
                first[e['spawn']] = min(first.get(e['spawn'], stamp(e)), stamp(e))
        support = []
        for e in evs:
            if stamp(e) < t:
                continue
            kind = e.get('type')
            if kind in lane_rules.TERMINAL_EVENTS | {'queue-add', 'merge-gate-clean'}:
                support.append(kind)
            elif kind in ('verdict', 'cannot-assess') and first.get(e.get('spawn'), t) >= t:
                support.append(kind)
        expected = bool(support)
        actual = attempts.history(evs, trigger, 'grader', now=clock).consumed
        n += 1
        mismatches += expected != actual
        print(subject, 'trigger=' + trigger['ts'], 'expected=' + str(expected),
              'actual=' + str(actual), 'support=' + ','.join(support))
    print(f'L-spec-1662 AC10 replay: {n} subjects, {mismatches} mismatches')
    return 0 if n >= 1 and mismatches == 0 else 1


if __name__ == '__main__':
    if _REPLAY:
        raise SystemExit(replay_live())
    try:
        unittest.main(verbosity=2)
    finally:
        _FIXTURE_ROOT.cleanup()
