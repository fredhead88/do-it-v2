#!/usr/bin/env python3
"""L-spec-2138/carried-close: scratch-ledger checks for the carried-close capability.

    python3 src/test_charter_carry.py            the whole scratch suite
    python3 src/test_charter_carry.py --ac AC2   exactly one merge criterion
                                                 (AC1-AC6 or AC8); anything else, or a
                                                 selector that runs nothing, exits nonzero.

Every case writes through the production writers (`dispatch.emit`, `fold.append`)
on an isolated root and reads back through the production readers. The only
thing replaced is the paid-launch boundary (the sweep's Runner) and the tick's
unrelated services. No network, no backend, no production ledger.
"""
import atexit
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Bind disposable paths before any product module is imported.
_BOOT = tempfile.TemporaryDirectory(prefix='charter-carry-import-')
atexit.register(_BOOT.cleanup)
os.environ.update(DOIT_ROOT=_BOOT.name, DOIT_NO_POKE='1', HOME=str(Path(_BOOT.name) / 'home'),
                  DOIT_SCRATCH=tempfile.mkdtemp(prefix='charter-carry-scratch-'),
                  V4_LEDGER_DIR=str(Path(_BOOT.name) / 'v4-ledger'),
                  V4_INBOX_DIR=str(Path(_BOOT.name) / 'v4-inbox'),
                  V4_STAGING_DIR=str(Path(_BOOT.name) / 'v4-staging'))
for _k in ('DOIT_PROJECT', 'DOIT_LEDGER_FILE'):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brief  # noqa: E402
import charter_carry  # noqa: E402
import dispatch  # noqa: E402
import fold  # noqa: E402
import owed  # noqa: E402
import packet  # noqa: E402
import plain  # noqa: E402
import relay  # noqa: E402
import sweep_owed  # noqa: E402
import tick  # noqa: E402

T0 = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SRC, TGT, MID, OTHER = 'L-charter-9001', 'L-charter-9002', 'L-charter-9003', 'L-charter-9004'
SPEC, SPEC2 = 'L-spec-9101', 'L-spec-9102'
GOAL = 'L-goal-9001'
THINKER, OPERATOR = 'thinker', 'operator'


class Scratch(unittest.TestCase):
    """One disposable root per case; the clock is `self.at`."""

    observations = []

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='charter-carry-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('events', 'content', 'seat', 'state', 'repos'):
            (self.root / name).mkdir()
        self.at = T0
        for module, attrs in ((fold, ('ROOT', 'EVENTS')),
                              (dispatch, ('ROOT', 'EVENTS', 'CONTENT', 'SEAT'))):
            for attr in attrs:
                self.patch(module, attr, self.root if attr == 'ROOT' else self.root / attr.lower())
        for module in (sweep_owed, packet):
            for attr in ('ROOT', 'CONTENT', 'EVENTS', 'PACKETS'):
                if hasattr(module, attr):
                    self.patch(module, attr, self.root if attr == 'ROOT' else self.root / attr.lower())
        self.patch(fold, 'PROJECT', None)
        self.patch(dispatch, 'now', side_effect=lambda: self.at.isoformat(timespec='seconds'))
        env = patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_NO_POKE='1')
        env.start()
        self.addCleanup(env.stop)
        self.clock(0)

    def tearDown(self):
        stored = self.bytes_on_disk()
        digest = hashlib.sha256()
        for name, data in sorted(stored.items()):
            digest.update(name.encode() + b'\0' + data)
        specs, charters, _, _ = self.folded()
        self.observations.append({
            'case': self.id(),
            'scratch_bytes': {name: len(data) for name, data in stored.items()},
            'ledger_sha256': digest.hexdigest(),
            'states': {sid: s['state'] for sid, s in {**specs, **charters}.items()},
        })

    def patch(self, module, attr, value=None, **kwargs):
        p = patch.object(module, attr, **kwargs) if kwargs else patch.object(module, attr, value)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def clock(self, seconds):
        self.at = T0 + timedelta(seconds=seconds)
        self.patch_now = patch.object(fold, 'NOW', self.at)
        self.patch_now.start()
        self.addCleanup(self.patch_now.stop)

    # ── writers: production append only ─────────────────────────────────────
    def path(self, actor):
        return self.root / 'events' / f'L-{actor}-local.jsonl'

    def emit(self, actor, type_, subject, seconds=None, strict=False, **fields):
        if seconds is not None:
            self.at = T0 + timedelta(seconds=seconds)
        return dispatch.emit(self.path(actor), dict(subject=subject, project='demo'), type_,
                             strict=strict, **fields)

    def ok(self, *args, **kwargs):
        refusal = self.emit(*args, **kwargs)
        self.assertIsNone(refusal, refusal)

    def events(self):
        return fold.read_events(use_cache=False)

    def folded(self):
        return fold.fold(self.events())

    def state(self, cid):
        return self.folded()[1][cid]['state']

    def bytes_on_disk(self):
        return {p.name: p.read_bytes() for p in sorted((self.root / 'events').glob('*.jsonl'))}

    def of(self, kind, subject=None):
        return [e for e in self.events() if e['type'] == kind and (subject is None or e['subject'] == subject)]

    # ── fixtures ────────────────────────────────────────────────────────────
    def charter(self, cid, seconds=-864000, l1=True, goal=None, covers='R1'):
        extra = dict(goal=goal) if goal else {}
        self.ok(THINKER, 'charter-filed', cid, seconds, title=cid, covers=covers, **extra)
        if l1:
            self.ok('operator', 'l1-complete', cid, seconds + 5)

    def spec(self, sid, cid, owed_ac=True, shipped=True, wake='AC7', data=None, seconds=-259200):
        self.ok('spec-writer', 'spec-written', sid, seconds - 600, charter=cid,
                path=str(self.root / 'content' / f'{sid}.md'))
        (self.root / 'content' / f'{sid}.md').write_text(
            f'# {sid}\n\n## Acceptance criteria\n\nAC7 [observed-data]: fixture\n')
        if owed_ac:
            self.ok('spec-writer', 'owed-ac', sid, seconds - 60, criterion=wake,
                    wake_at=(T0 + timedelta(seconds=seconds + 3600)).isoformat(timespec='seconds'))
        if shipped:
            self.ok('executor', 'shipped', sid, seconds, sha='a' * 40)

    def failed_check(self, sid, seconds, criterion='AC7', kind='unmet'):
        self.ok('owed-sweeper', 'owed-failed', sid, seconds, criterion=criterion, kind=kind,
                evidence='fixture observation')
        return [e for e in self.events() if e['type'] == 'owed-failed' and e['subject'] == sid][-1]['_src']

    def meet(self, sid, seconds, criterion='AC7'):
        self.ok('owed-sweeper', 'owed-met', sid, seconds, criterion=criterion, evidence='fixture ok')

    def carry(self, source=SRC, target=TGT, actor=THINKER, seconds=-600, why='moved to the target'):
        self.ok(actor, 'charter-carried', source, seconds, carried_to=target, why=why)

    def isolate(self):
        """Only unrelated services and transport are replaced; the carry path is real."""
        services = {
            'intake.run': {}, 'notes.run': [], 'pr_closeout.run': [],
            'install_sync.run': [], 'push_origin.run': [], 'carry.uncarried': [],
            'carry.sync_v4': [], 'pane_resume.run': [], 'crons.check': [],
            'look._read_state': {'last_pass': T0.isoformat()}, 'look.heartbeat_missed': None,
            'up.ensure_relay': 'healthy', 'resources.manifest_counts': {},
            'resources.not_reapable_counts': {}, 'reap_tmp._reap_recorded': [],
            'tree_cleanup.reap_worktrees': ([], []), 'loadgate.begin': None,
            'autodispatch.redispatch_dead': [], 'merge_queue.enqueue_due': [],
            'tick.launch_precheck': None,
        }
        for name, result in services.items():
            module, attr = name.rsplit('.', 1)
            self.patch(importlib.import_module(module), attr, return_value=result)
        self.patch(importlib.import_module('autodispatch'), '_real_runner',
                   side_effect=AssertionError('external dispatch forbidden'))
        self.patch(tick, 'launch', return_value=None)

    def tick(self, seconds=None):
        if seconds is not None:
            self.clock(seconds)
        else:
            self.clock((self.at - T0).total_seconds())
        self.assertIsNotNone(tick._record())

    def tick_events(self):
        return [e for e in self.events() if e['actor'] == 'tick']

    def closed(self, cid):
        return [e for e in self.of('charter-closed', cid)]

    def briefs(self, condition):
        """Ledger brief events: look announces a condition once a day."""
        return [e for e in self.events() if e['type'] == 'brief' and e.get('condition') == condition]

    def digest(self, condition):
        """Every keyed item look recorded for the condition today (its linked JSON)."""
        out = []
        for p in sorted((self.root / 'look' / 'digests').glob('*.json')):
            out.extend(json.loads(p.read_text())['conditions'].get(condition, {}).get('items', {}).values())
        return out

    def seed_pair(self, with_spec=True):
        """A source with a shipped owed check, and a filed L1-complete target."""
        self.charter(SRC)
        self.charter(TGT)
        if with_spec:
            self.spec(SPEC, SRC)

    def complete(self, cid, seconds=-100000):
        """Take a charter with no specs to L2-complete the honest way."""
        self.ok('executor', 'sweep-fixpoint', cid, seconds)
        self.ok('charter-reviewer', 'charter-review-complete', cid, seconds + 1)
        self.assertEqual(self.state(cid), 'L2-complete')


# ═════════════════════════════════════════════════════════════════════════════
class AC1(Scratch):
    """R1: authorized carries validate at both append paths; nothing appends on a refusal."""

    def append(self, actor, source, target, why='because', strict=False, via='emit', **extra):
        fields = {k: v for k, v in dict(carried_to=target, why=why).items() if v is not None}
        fields.update(extra)
        if via == 'emit':
            return dispatch.emit(self.path(actor), dict(subject=source, project='demo'), 'charter-carried',
                                 strict=strict, **fields)
        if via == 'keyword':
            return dispatch.emit(self.path(actor), dict(project='demo'), 'charter-carried',
                                 strict=strict, subject=source, **fields)
        argv = ['charter-carried', source] + [f'{k}={v}' for k, v in fields.items()]
        with patch.dict(os.environ, DOIT_LEDGER_FILE=self.path(actor).name):
            try:
                with redirect_stderr(io.StringIO()):
                    fold.append(argv, strict=strict)
            except SystemExit as exit_:
                return str(exit_)
            except Exception as exc:               # raised before anything was written
                return f'{type(exc).__name__}: {exc}'
        return None

    def refused(self, *args, **kwargs):
        before = self.bytes_on_disk()
        results = []
        for strict in (True, False):
            for via in ('emit', 'keyword', 'cli'):
                results.append(self.append(*args, strict=strict, via=via, **kwargs))
        self.assertTrue(all(isinstance(r, str) and r for r in results), results)
        self.assertEqual(self.bytes_on_disk(), before, 'a refusal appended bytes')

    def test_valid_requests_persist_through_both_append_paths(self):
        for actor in (THINKER, OPERATOR):
            for via in ('emit', 'keyword', 'cli'):
                for strict in (True, False):
                    self.setUp()
                    self.seed_pair()
                    before = self.state(SRC)
                    self.assertIsNone(self.append(actor, SRC, TGT, via=via, strict=strict), (actor, via, strict))
                    rows = self.of('charter-carried', SRC)
                    self.assertEqual([(r['carried_to'], r['actor']) for r in rows], [(TGT, actor)])
                    self.assertEqual(self.state(SRC), before)             # a request alone changes nothing

    def test_invalid_targets_and_sources_are_refused_without_a_byte(self):
        self.charter(SRC)
        self.charter(TGT)
        self.charter(OTHER, l1=False)
        self.ok(THINKER, 'dependency-hold', 'L-spec-9999', waits_for='L-spec-9100', why='noise')
        self.ok(THINKER, 'note', 'L-charter-9099', text='an id with events but no charter-filed')
        self.refused(THINKER, SRC, SRC)                       # self
        self.refused(THINKER, SRC, 'L-charter-9098')          # unknown
        self.refused(THINKER, SRC, 'L-charter-9099')          # unfiled
        self.refused(THINKER, SRC, 'not-a-charter')
        self.refused(THINKER, 'L-charter-9097', TGT)          # source not filed
        self.refused(THINKER, SRC, None)                      # missing carried_to
        self.refused(THINKER, SRC, '  ')                      # blank carried_to
        self.refused(THINKER, SRC, TGT, why=None)             # missing why
        self.refused(THINKER, SRC, TGT, why='   ')            # blank why
        # An actor that may not ask is refused, never recorded-and-ignored.
        for actor in ('builder', 'tick', 'executor'):
            self.refused(actor, SRC, TGT)

    def test_terminal_targets_and_sources_are_refused(self):
        self.charter(SRC)
        self.charter(TGT)
        self.charter(OTHER, l1=False)
        self.charter(MID, l1=False)
        self.ok(OPERATOR, 'charter-retracted', OTHER, -50, why='withdrawn')
        self.assertEqual(self.state(OTHER), 'retracted')
        self.refused(THINKER, SRC, OTHER)                     # retracted target
        self.refused(THINKER, OTHER, TGT)                     # retracted source
        self.complete(MID)
        self.refused(THINKER, SRC, MID)                       # closed (L2-complete) target
        self.refused(THINKER, MID, TGT)                       # closed source
        # carried target / carried source
        self.isolate()
        self.carry(SRC, TGT)
        self.tick(0)
        self.assertEqual(self.state(SRC), 'carried')
        self.charter('L-charter-9005')
        self.refused(THINKER, 'L-charter-9005', SRC)          # carried target
        self.refused(THINKER, SRC, 'L-charter-9005')          # carried source

    def test_cycles_are_refused(self):
        self.charter(SRC)
        self.charter(TGT)
        self.charter(MID)
        self.carry(SRC, TGT, seconds=-500)                    # SRC -> TGT is pending
        self.refused(THINKER, TGT, SRC)                       # a chain returning to its source
        self.carry(TGT, MID, seconds=-400)                    # TGT -> MID pending as well
        self.refused(THINKER, MID, SRC)                       # a longer one
        self.assertIsNone(self.append(THINKER, OTHER_FILED(self), MID))   # a non-cycle still passes

    def test_a_ledger_that_cannot_be_read_refuses(self):
        self.seed_pair()
        before = self.bytes_on_disk()
        with patch.object(fold, 'read_events', side_effect=OSError('disk gone')):
            for strict in (True, False):
                for via in ('emit', 'cli'):
                    self.assertTrue(self.append(THINKER, SRC, TGT, strict=strict, via=via))
        self.assertEqual(self.bytes_on_disk(), before)
        with patch.object(fold, 'read_events', side_effect=OSError('disk gone')):
            _, refusal = charter_carry.guard(SRC, dict(carried_to=TGT, why='x'))
        self.assertIn('could not establish', refusal)

    def test_non_carry_appends_keep_their_contract(self):
        self.seed_pair()
        self.assertIsNone(self.emit(THINKER, 'note', SRC, text='hello'))
        self.assertIn('actor mismatch', self.emit('builder', 'charter-closed', SRC, strict=True, reason='r'))

    def test_guard_reuses_the_current_dispatch_view(self):
        self.seed_pair()
        events = self.events()
        fields = dict(carried_to=TGT, why='work moved')
        with dispatch.dispatch_view(events), patch.object(fold, 'read_events',
                                                          side_effect=AssertionError('extra read')):
            self.assertEqual(charter_carry.guard(SRC, fields), (fields, None))

    def test_newest_admitted_request_governs_and_an_unauthorized_one_cannot(self):
        self.charter(SRC)
        self.charter(TGT)
        self.charter(MID)
        # Two admitted requests with the same timestamp: the later line wins.
        self.carry(SRC, TGT, seconds=-300)
        self.carry(SRC, MID, seconds=-300)
        self.assertEqual(charter_carry.request(self.folded()[1][SRC]['evs'])['carried_to'], MID)
        # A raw, unauthorized request appended later cannot govern.
        raw = {'v': 1, 'ts': (T0 - timedelta(seconds=10)).isoformat(timespec='seconds'), 'type': 'charter-carried',
               'subject': SRC, 'project': 'demo', 'carried_to': TGT, 'why': 'forged'}
        with open(self.path('builder'), 'a') as fh:
            fh.write(json.dumps(raw) + '\n')
        self.assertEqual(charter_carry.request(self.folded()[1][SRC]['evs'])['carried_to'], MID)

    def test_target_problem_and_final_target_are_total(self):
        self.charter(SRC)
        self.charter(TGT)
        _, charters, _, _ = self.folded()
        self.assertIsNone(charter_carry.target_problem(SRC, TGT, charters))
        self.assertTrue(charter_carry.target_problem(SRC, SRC, charters))
        self.assertEqual(charter_carry.final_target(TGT, charters), TGT)
        self.assertEqual(charter_carry.final_target('L-charter-0000', charters), 'L-charter-0000')
        loop = {'a': {'state': 'carried', 'carried_to': 'b'}, 'b': {'state': 'carried', 'carried_to': 'a'}}
        self.assertEqual(charter_carry.final_target('a', loop), 'b')      # stops before a repeated id
        self.assertIn('does not end', charter_carry.target_problem(
            SRC, TGT, {SRC: charters[SRC], TGT: {**charters[TGT], 'evs': charters[TGT]['evs'] + [
                {'type': 'charter-carried', 'carried_to': MID}]},
                MID: {**charters[TGT], 'evs': charters[TGT]['evs'] + [{'type': 'charter-carried', 'carried_to': TGT}]}}))


def OTHER_FILED(case):
    case.charter(OTHER)
    return OTHER


# ═════════════════════════════════════════════════════════════════════════════
class AC2(Scratch):
    """R2: only the tick closes, the newest admitted request is revalidated, one close per carry."""

    def test_request_alone_is_nonterminal_and_the_tick_closes_each_live_state(self):
        for start in ('open', 'L1-complete', 'proving', 'reopened'):
            with self.subTest(start=start):
                self.setUp()
                self.isolate()
                self.charter(SRC, l1=(start != 'open'))
                self.charter(TGT)
                if start == 'L1-complete':
                    self.spec(SPEC, SRC, owed_ac=False, shipped=False)      # an unbuilt spec keeps it at L1
                if start in ('proving', 'reopened'):
                    self.ok('tick', 'charter-proving', SRC, -2000, reason='r', deadline=(T0 + timedelta(days=3)).isoformat())
                    if start == 'reopened':
                        self.ok('tick', 'charter-reopened', SRC, -1500, spec=SPEC, criterion='AC7', failed_src='x:1')
                self.assertEqual(self.state(SRC), start)
                self.carry(SRC, TGT, seconds=-600, why='moved')
                self.assertEqual(self.state(SRC), start)               # request alone changes nothing
                self.tick(0)
                self.assertEqual(self.state(SRC), 'carried')
                c = self.folded()[1][SRC]
                self.assertEqual(c['carried_to'], TGT)
                closed = self.closed(SRC)
                self.assertEqual(len(closed), 1)
                self.assertEqual((closed[0]['label'], closed[0]['actor'], closed[0]['carried_to']),
                                 ('carried', 'tick', TGT))
                self.assertIn(TGT, closed[0]['reason'])
                self.tick(60)
                self.assertEqual(len(self.closed(SRC)), 1, 'a second tick emitted a second close')
                self.assertFalse(self.of('charter-reopened', SRC) and start != 'reopened')

    def test_hand_close_is_not_admitted_and_retraction_wins(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        self.carry()
        for actor in ('operator', 'thinker', 'executor'):
            self.emit(actor, 'charter-closed', SRC, 5, label='carried', carried_to=TGT, reason=f'carried into {TGT}')
        self.assertNotEqual(self.state(SRC), 'carried')
        # Even a tick-named file without a request naming that target is not a carry.
        self.emit('tick', 'charter-closed', TGT, 6, label='carried', carried_to=SRC, reason=f'carried into {SRC}')
        self.assertNotEqual(self.state(TGT), 'carried')
        self.tick(10)
        self.assertEqual(self.state(SRC), 'carried')
        self.ok(OPERATOR, 'charter-retracted', SRC, 20, why='withdrawn after all')
        self.assertEqual(self.state(SRC), 'retracted')            # retracted > carried

    def test_stale_invalid_target_does_not_close_and_alarms_once(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        self.charter(MID)
        self.carry(SRC, TGT, seconds=-900)                        # an older valid request
        self.carry(SRC, MID, seconds=-600)                        # the newest names MID
        self.complete(MID)                                        # ... which closes before the tick
        self.tick(0)
        self.tick(60)
        self.tick(120)
        self.assertEqual(self.closed(SRC), [])                    # no fallback to the older target
        self.assertNotEqual(self.state(SRC), 'carried')
        self.assertEqual(len(self.briefs('carry-target-invalid')), 1)
        self.assertEqual(len(self.digest('carry-target-invalid')), 1)
        self.assertEqual(self.briefs('carry-target-invalid')[0]['owner'], 'thinker')

    def test_request_inserted_past_the_guard_is_revalidated(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        raw = {'v': 1, 'ts': (T0 - timedelta(seconds=30)).isoformat(timespec='seconds'), 'type': 'charter-carried',
               'subject': SRC, 'project': 'demo', 'carried_to': SRC, 'why': 'self, via a raw append'}
        with open(self.path(THINKER), 'a') as fh:
            fh.write(json.dumps(raw) + '\n')
        self.tick(0)
        self.assertEqual(self.closed(SRC), [])
        self.assertEqual(len(self.briefs('carry-target-invalid')), 1)

    def test_normal_close_path_is_unchanged(self):
        self.isolate()
        self.charter(TGT, l1=True)
        before = fold.close_label(self.events(), TGT)
        self.assertEqual(before, 'accepted')
        self.assertIsNone(fold._close_label([{'blocks': True}]))
        self.assertEqual(fold._close_label([]), 'accepted')
        self.assertEqual(fold._close_label([{'blocks': False}]), 'closed-unproven')

    def test_real_store_round_trip_in_a_fresh_process(self):
        self.isolate()
        self.seed_pair()
        self.carry()
        self.tick(0)
        code = ('import json, fold\n'
                'ev = fold.read_events(use_cache=False)\n'
                'specs, charters, ignored, by = fold.fold(ev)\n'
                f'print(json.dumps({{"state": charters["{SRC}"]["state"], "to": charters["{SRC}"].get("carried_to"),'
                f' "spec_charter": specs["{SPEC}"]["charter"], "spec_state": specs["{SPEC}"]["state"],'
                f' "types": [(e["type"], e["actor"]) for e in by["{SRC}"]]}}))\n')
        env = {**os.environ, 'DOIT_ROOT': str(self.root), 'PYTHONPATH': str(Path(__file__).resolve().parent)}
        env.pop('DOIT_LEDGER_FILE', None)
        out = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, cwd=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        seen = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((seen['state'], seen['to']), ('carried', TGT))
        self.assertIn(['charter-carried', 'thinker'], seen['types'])
        self.assertIn(['charter-closed', 'tick'], seen['types'])
        self.assertEqual(seen['spec_charter'], SRC)               # the spec keeps its charter ...
        self.assertTrue(seen['spec_state'].startswith('shipped'))  # ... and its lane state
        on_disk = [json.loads(l) for l in (self.path('tick')).read_text().splitlines()]
        self.assertIn(('charter-closed', 'carried'), [(e['type'], e.get('label')) for e in on_disk])

    def test_control_text_in_why_cannot_make_a_second_event_or_run_anything(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        nasty = 'ok"\n{"type":"charter-closed","subject":"%s"}\n$(touch /tmp/pwned) \x07' % SRC
        self.carry(why=nasty)
        self.tick(0)
        self.assertEqual(len(self.closed(SRC)), 1)
        self.assertEqual(self.closed(SRC)[0]['actor'], 'tick')
        self.assertNotIn('\n', self.closed(SRC)[0]['reason'])
        for p in (self.root / 'events').glob('*.jsonl'):
            for line in p.read_text().splitlines():
                json.loads(line)
        self.assertEqual(len(self.of('charter-carried', SRC)), 1)


# ═════════════════════════════════════════════════════════════════════════════
class AC3(Scratch):
    """R3: an eligible due owed check on a carried source is still selected, dispatched and counted."""

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv):
            self.calls.append(list(argv))
            out = 'packet-path.md\n' if len(argv) > 1 and argv[1] == 'packet' else ''
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')

    def sweep(self):
        runner = self.Runner()
        ev = self.events()
        with patch.object(sweep_owed.loadgate, 'readings', return_value=dict(
                mem_avail_gb=20, swap_free_gb=8, load1_per_core=1, cpu_some10=0, mem_some10=0, mem_full10=0)), \
                patch.object(sweep_owed.verifier, 'run', return_value=[]), \
                redirect_stdout(io.StringIO()):
            sweep_owed.run(dry_run=False, runner=runner, now=T0 + timedelta(seconds=self.tick_clock))
        return runner

    tick_clock = 0

    def due_rows(self, sid=SPEC):
        specs = self.folded()[0]
        now = T0
        return [r for r in sweep_owed.due_candidates(specs, now) if r['spec'] == sid]

    def test_due_check_survives_the_carry(self):
        self.isolate()
        self.seed_pair()
        before = self.due_rows()
        self.assertEqual([(r['spec'], r['criterion']) for r in before], [(SPEC, 'AC7')])
        specs, charters, _, _ = self.folded()
        owed_before = (charters[SRC]['owed'], [(r['spec'], r['criterion']) for r in fold.owed_due(specs)])
        self.carry()
        self.tick(0)
        self.assertEqual(self.state(SRC), 'carried')
        specs, charters, _, _ = self.folded()
        self.assertEqual((charters[SRC]['owed'], [(r['spec'], r['criterion']) for r in fold.owed_due(specs)]),
                         owed_before)
        self.assertEqual([(r['spec'], r['criterion']) for r in self.due_rows()], [(SPEC, 'AC7')])
        self.assertNotEqual(specs[SPEC]['state'], 'dropped')
        self.assertTrue(specs[SPEC]['state'].startswith('shipped-owed'))   # not read as retracted
        runner = self.sweep()
        launched = [c for c in runner.calls if len(c) > 2 and c[1] == 'dispatch' and 'owed-sweeper' in c]
        self.assertTrue(launched, runner.calls)                       # dispatched, at the paid-launch boundary
        manifests = sorted((self.root / 'content').glob('owed-sweep-*.md')) or \
            [p for p in (self.root / 'content').glob('*.md') if 'sweep' in p.name]
        self.assertTrue(any(SPEC in p.read_text() for p in manifests), manifests)

    def test_existing_exclusions_still_suppress(self):
        self.isolate()
        self.seed_pair()
        self.ok('owed-sweeper', 'owed-unobservable', SPEC, -3000, criterion='AC7', capability='ro-dsn',
                evidence='no dsn')
        self.assertEqual(self.due_rows(), [])                          # capability exclusion, before ...
        self.carry()
        self.tick(0)
        self.assertEqual(self.due_rows(), [])                          # ... and after the carry
        self.assertEqual(self.state(SRC), 'carried')
        # In-flight retry exclusion: a recent failure suppresses, carried or not.
        self.setUp()
        self.isolate()
        self.seed_pair()
        self.failed_check(SPEC, -300)
        self.assertEqual(self.due_rows(), [])
        self.carry()
        self.tick(0)
        self.assertEqual(self.due_rows(), [])

    def test_spec_state_does_not_treat_carried_like_retracted(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        self.spec(SPEC, SRC, shipped=False)
        before = self.folded()[0][SPEC]['state']
        self.carry()
        self.tick(0)
        self.assertEqual(self.state(SRC), 'carried')
        self.assertEqual(self.folded()[0][SPEC]['state'], before)
        self.ok(OPERATOR, 'charter-retracted', OTHER_FILED(self), 50, why='x')


# ═════════════════════════════════════════════════════════════════════════════
class AC4(Scratch):
    """R4: blocking unmet failures survive a carry as deduplicated records on the final target."""

    def records(self, target=TGT):
        return self.of('carried-check-failed', target)

    def test_standing_and_fresh_failures_each_get_one_record_and_one_brief(self):
        for entered in (False, True):
            with self.subTest(entered_proving=entered):
                self.setUp()
                self.isolate()
                self.seed_pair()
                standing = self.failed_check(SPEC, -2000)                       # before the request
                if entered:
                    self.ok('tick', 'charter-proving', SRC, -1900, reason='r',
                            deadline=(T0 + timedelta(days=3)).isoformat())
                self.carry(seconds=-600)
                fresh_spec = SPEC2
                self.spec(fresh_spec, SRC, seconds=-250000)
                fresh = self.failed_check(fresh_spec, -300)                     # after the request
                self.tick(0)
                self.assertEqual(self.state(SRC), 'carried')
                recs = self.records()
                self.assertEqual(sorted((r['spec'], r['failed_src']) for r in recs),
                                 sorted([(SPEC, standing), (fresh_spec, fresh)]))
                self.assertTrue(all(r['source'] == SRC and r['actor'] == 'tick' for r in recs))
                briefs = self.digest('proving-check-failed')           # look keeps one item per record
                self.assertEqual(len(briefs), 2)
                self.assertEqual(len(self.briefs('proving-check-failed')), 1)
                self.assertTrue(all(b['owner'] == 'thinker' and 'requirement' not in b['reading'] for b in briefs))
                self.assertTrue(all(b not in () and 'requirement' not in b for b in self.briefs('proving-check-failed')))
                self.assertFalse(self.of('charter-reopened', SRC) and not entered)
                before = len(self.events())
                self.tick(60)
                self.tick(120)
                self.assertEqual(len(self.records()), 2)
                self.assertEqual(len(self.digest('proving-check-failed')), 2)
                new = [e for e in self.events()[before:] if e['type'] in ('carried-check-failed', 'brief')]
                self.assertEqual(new, [], 'later ticks repeated a record or a brief')

    def test_nonblocking_non_unmet_superseded_and_repeated_do_not_record_or_repeat(self):
        self.isolate()
        self.seed_pair()
        self.spec(SPEC2, SRC, seconds=-250000)
        # non-unmet kind on SPEC; two failures on SPEC2, only the governing (newest) one counts
        self.failed_check(SPEC, -2000, kind='unobservable')
        first = self.failed_check(SPEC2, -2500)
        second = self.failed_check(SPEC2, -2000)
        self.carry(seconds=-600)
        self.tick(0)
        self.assertEqual([r['failed_src'] for r in self.records()], [second])
        self.assertNotEqual(first, second)
        # A nonblocking check (declared scratch, no customer data) records nothing.
        self.setUp()
        self.isolate()
        self.seed_pair()
        self.ok('executor', 'check-declared', SPEC, -3000, criterion='AC7', spec=SPEC, argv=['true'], sha='a' * 12,
                check_id=f'{SPEC}:AC7', data_source='scratch', customer_data=False, profile='repo-venv',
                timing='pre-merge', answered_by='script')
        self.failed_check(SPEC, -2000)
        self.carry(seconds=-600)
        self.tick(0)
        self.assertEqual(self.records(), [])

    def test_a_second_fresh_failure_for_the_same_criterion_records_again_after_a_met(self):
        self.isolate()
        self.seed_pair()
        first = self.failed_check(SPEC, -2000)
        self.carry(seconds=-600)
        self.tick(0)
        self.assertEqual([r['failed_src'] for r in self.records()], [first])
        self.meet(SPEC, 100)
        self.ok('executor', 'owed-ac', SPEC, 200, criterion='AC7', wake_at=(T0 + timedelta(days=4)).isoformat())
        second = self.failed_check(SPEC, 300)
        self.tick(400)
        self.assertEqual([r['failed_src'] for r in self.records()], [first, second])
        keys = [b['key'] for b in self.digest('proving-check-failed')]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 2)

    def test_failure_after_the_target_closes_is_recorded_without_reopening(self):
        self.isolate()
        self.charter(SRC)
        self.charter(TGT, l1=False)
        self.spec(SPEC, SRC)
        self.carry(seconds=-600)
        self.tick(0)
        self.complete(TGT, seconds=100)
        self.assertEqual(self.state(TGT), 'L2-complete')
        late = self.failed_check(SPEC, 500)
        self.tick(600)
        self.assertEqual([r['failed_src'] for r in self.records()], [late])
        self.assertEqual(len(self.digest('proving-check-failed')), 1)
        self.assertEqual((self.state(SRC), self.state(TGT)), ('carried', 'L2-complete'))
        self.assertEqual(self.of('charter-reopened'), [])

    def test_multi_hop_records_go_to_the_final_target_and_a_loop_terminates(self):
        self.isolate()
        self.charter(SRC)
        self.charter(MID)
        self.charter(TGT)
        self.spec(SPEC, SRC)
        failed = self.failed_check(SPEC, -2000)
        self.carry(MID, TGT, seconds=-700)
        self.carry(SRC, MID, seconds=-600)
        self.tick(0)
        self.tick(60)
        _, charters, _, _ = self.folded()
        self.assertEqual({SRC: charters[SRC]['state'], MID: charters[MID]['state']},
                         {SRC: 'carried', MID: 'carried'})
        self.assertEqual(charter_carry.final_target(SRC, charters), TGT)
        recs = self.records(TGT)
        self.assertEqual([(r['source'], r['failed_src']) for r in recs], [(SRC, failed)])
        self.assertEqual(self.records(MID), [])

    def test_valid_pending_carry_never_emits_proving_or_reopened_and_an_invalid_one_still_reopens(self):
        for start in ('proving', 'reopened'):
            self.setUp()
            self.isolate()
            self.charter(SRC)
            self.charter(TGT)
            self.spec(SPEC, SRC, owed_ac=True)
            self.ok('tick', 'charter-proving', SRC, -2000, reason='r', deadline=(T0 + timedelta(days=3)).isoformat())
            self.failed_check(SPEC, -1500)
            self.carry(seconds=-600)
            fresh = self.failed_check(SPEC, -300)                                  # between request and close
            self.tick(0)
            self.assertEqual(self.of('charter-reopened', SRC), [], start)
            self.assertEqual(self.state(SRC), 'carried')
            self.assertIn(fresh, [r['failed_src'] for r in self.records()])
        # Negative control: an invalid target leaves the normal branch alone.
        self.setUp()
        self.isolate()
        self.charter(SRC)
        self.charter(TGT, l1=False)
        self.spec(SPEC, SRC)
        self.ok('tick', 'charter-proving', SRC, -2000, reason='r', deadline=(T0 + timedelta(days=3)).isoformat())
        self.carry(seconds=-900)
        self.complete(TGT, seconds=-800)                                   # the target closes first
        self.failed_check(SPEC, -300)
        self.tick(0)
        self.assertEqual(self.closed(SRC), [])
        self.assertEqual(len(self.of('charter-reopened', SRC)), 1)         # the existing reopen is retained
        self.assertEqual(self.records(TGT), [])

    def test_no_reopen_key_reuse_and_no_close_blocker(self):
        self.isolate()
        self.charter(SRC)
        self.charter(TGT)
        self.spec(SPEC, SRC)
        self.failed_check(SPEC, -2000)
        specs, charters, _, _ = self.folded()
        label_before = (charters[TGT]['close_label'], dict(charters[TGT]['closable'].__dict__) if hasattr(
            charters[TGT]['closable'], '__dict__') else tuple(charters[TGT]['closable']))
        self.carry(seconds=-600)
        self.tick(0)
        key = self.digest('proving-check-failed')[0]['key']
        self.assertEqual(key.count('|'), 4)                               # target|source|spec|criterion|failed_src
        self.assertTrue(key.startswith(f'{TGT}|{SRC}|'))
        _, charters, _, _ = self.folded()
        after = (charters[TGT]['close_label'], dict(charters[TGT]['closable'].__dict__) if hasattr(
            charters[TGT]['closable'], '__dict__') else tuple(charters[TGT]['closable']))
        self.assertEqual(after, label_before)                             # records and briefs add no blocker
        self.assertEqual(self.folded()[1][TGT]['briefs'], 0)


# ═════════════════════════════════════════════════════════════════════════════
class AC5(Scratch):
    """R5: source and target briefs, goal views and phrases describe carried work honestly."""

    def dossier(self, item):
        return brief.dossier(item, self.events(), self.root, T0 + timedelta(seconds=0)) \
            if 'root' in brief.dossier.__code__.co_varnames else brief.dossier(item, self.events())

    def setup_world(self, goal=True):
        self.isolate()
        if goal:
            self.ok(THINKER, 'goal-filed', GOAL, -900000, title='the goal')
        self.charter(SRC, goal=GOAL if goal else None)
        self.charter(TGT, goal=GOAL if goal else None)
        self.spec(SPEC, SRC)
        failed = self.failed_check(SPEC, -2000)
        self.carry(seconds=-600, why='the remaining work moved')
        self.tick(0)
        return failed

    def test_source_phrase_catch_me_up_next_and_expected_done(self):
        self.setup_world(goal=False)
        d = self.dossier(SRC)
        self.assertEqual(plain.phrase('charter', 'carried'), 'closed, carried into another charter')
        self.assertEqual(d['status']['state'], 'carried')
        self.assertIn(f'closed, carried into {TGT}', d['status']['phrase'])
        self.assertIn(f'closed, carried into {TGT}', '\n'.join(d['catch_me_up']))
        self.assertEqual(d['next']['owner'], 'nobody')
        self.assertTrue(d['next']['text'].startswith('nothing'))
        close_ts = self.closed(SRC)[0]['ts']
        self.assertEqual(d['timeline']['expected_done'], close_ts)
        self.assertNotIn('proven', d['status']['phrase'])
        self.assertNotIn(SRC, brief._CHARTER_PROVEN)
        self.assertEqual(brief._CHARTER_PROVEN, {'L2-complete'})
        self.assertIn('carried', fold.CHARTER_STATES)
        self.assertEqual([c['id'] for c in d['children']], [SPEC])          # spec lanes stay visible
        self.assertIn(f'closed, carried into {TGT}', brief.render(d))

    def test_target_lists_carried_sources_why_and_failure_records(self):
        failed = self.setup_world(goal=False)
        d = self.dossier(TGT)
        self.assertEqual([c['id'] for c in d['carries']], [SRC])
        self.assertEqual(d['carries'][0]['why'], 'the remaining work moved')
        rec = d['carries'][0]['records']
        self.assertEqual([(r['spec'], r['failed_src']) for r in rec], [(SPEC, failed)])
        self.assertEqual(rec[0]['status'], 'still failed')
        text = brief.render(d)
        self.assertIn(SRC, text)
        self.assertIn('the remaining work moved', text)
        self.assertIn(failed, text)
        json.dumps(d)                                                       # structured dossier is JSON-clean

    def test_multiple_sources_and_a_chained_target(self):
        self.isolate()
        for cid in (SRC, MID, TGT, OTHER):
            self.charter(cid)
        self.carry(OTHER, MID, seconds=-800, why='other moved')
        self.carry(MID, TGT, seconds=-700, why='mid moved')
        self.carry(SRC, MID, seconds=-600, why='src moved')
        self.tick(0)
        self.tick(60)
        self.assertEqual({c: self.state(c) for c in (SRC, MID, OTHER)},
                         {SRC: 'carried', MID: 'carried', OTHER: 'carried'})
        mid = self.dossier(MID)
        self.assertEqual(sorted(c['id'] for c in mid['carries']), sorted([SRC, OTHER]))
        tgt = self.dossier(TGT)
        self.assertEqual(sorted(c['id'] for c in tgt['carries']), sorted([MID, SRC, OTHER]))
        self.assertIn(f'closed, carried into {MID}', self.dossier(SRC)['status']['phrase'])

    def test_goal_reads_done_only_when_the_final_target_is_complete(self):
        self.setup_world()
        goal = self.dossier(GOAL)
        self.assertNotEqual(goal['status']['state'], 'done')
        self.assertEqual(goal['status']['state'], 'being proven')       # it follows the target's state
        self.assertNotEqual(goal['next']['owner'], 'nobody')
        self.assertEqual({g['id']: g['state'] for g in brief.goals(self.events(), self.root)}[GOAL], 'being proven')
        # The source's spec is still unaccepted; the target completes -> done.
        self.complete(TGT, seconds=100)
        goal = self.dossier(GOAL)
        self.assertEqual(goal['status']['state'], 'done')
        self.assertEqual({g['id']: g['state'] for g in brief.goals(self.events(), self.root)}[GOAL], 'done')

    def test_goal_with_a_retracted_final_target_never_reads_done(self):
        self.setup_world()
        self.ok(OPERATOR, 'charter-retracted', TGT, 200, why='not going ahead')
        self.assertEqual(self.state(TGT), 'retracted')
        goal = self.dossier(GOAL)
        self.assertEqual(goal['status']['state'], 'being built')
        self.assertNotEqual(goal['status']['state'], 'done')

    def test_goal_state_substitutes_the_target_state(self):
        self.setup_world()
        _, charters, _, _ = self.folded()
        self.assertEqual(brief._goal_charter_state(SRC, charters), charters[TGT]['state'])
        self.assertEqual(plain.goal_state(['carried']), 'being built')       # live and never done


# ═════════════════════════════════════════════════════════════════════════════
class AC6(Scratch):
    """R5: counts distinguish carried from accepted, and relay follows the work."""

    def counts(self):
        out = io.StringIO()
        with patch.object(sys, 'argv', ['fold.py', 'states', '--counts']), redirect_stdout(out):
            with self.assertRaises(SystemExit) as stop:
                runpy_main()
        self.assertEqual(stop.exception.code, 0)
        return {k: int(v) for k, v in (line.split('\t') for line in out.getvalue().splitlines())}

    def test_count_partition_and_before_after_carry(self):
        self.isolate()
        for cid in ('L-charter-9010', 'L-charter-9011', 'L-charter-9012', 'L-charter-9013'):
            self.charter(cid)                                       # open-ish (L1-complete)
        self.charter('L-charter-9014', l1=False)                    # open
        self.charter('L-charter-9015')
        self.ok(OPERATOR, 'charter-retracted', 'L-charter-9015', 5, why='gone')
        for cid, label in (('L-charter-9016', 'accepted'), ('L-charter-9017', None), ('L-charter-9018', 'closed-unproven')):
            self.charter(cid, l1=False)
            self.complete(cid)
            fields = dict(reason='all checks discharged')
            if label:
                fields['label'] = label
            self.ok('tick', 'charter-closed', cid, 10, **fields)
        self.charter(SRC)
        self.charter(TGT)
        before = self.counts()
        self.assertEqual(before, {'open': 7, 'carried': 0, 'retracted': 1, 'accepted': 2, 'closed-other': 1})
        self.assertEqual(sum(before.values()), len(self.folded()[1]))
        self.carry(SRC, TGT)
        self.assertEqual(self.counts(), before)                     # a request alone moves nothing
        self.tick(0)
        after = self.counts()
        self.assertEqual(after['open'], before['open'] - 1)
        self.assertEqual(after['carried'], before['carried'] + 1)
        self.assertEqual(after['accepted'], before['accepted'])
        self.assertEqual(sum(after.values()), len(self.folded()[1]))
        # Ordinary `states` output keeps its shape and shows the new state.
        out = io.StringIO()
        with patch.object(sys, 'argv', ['fold.py', 'states']), redirect_stdout(out):
            try:
                runpy_main()
            except SystemExit:
                pass
        self.assertIn(f'{SRC}\tcarried', out.getvalue().splitlines())

    def test_board_charter_close_block_omits_carried(self):
        self.isolate()
        self.seed_pair(with_spec=False)
        self.carry()
        self.tick(0)
        specs, charters, ignored, by = self.folded()
        board = fold.render(self.events(), specs, charters, ignored, by)
        close_block = board.split('CHARTER CLOSE')[1].split('\n\n')[0] if 'CHARTER CLOSE' in board else ''
        self.assertNotIn(SRC, close_block)

    def test_relay_follows_the_work(self):
        self.isolate()
        self.charter(SRC)
        self.charter(TGT)
        self.spec(SPEC, SRC, owed_ac=False)
        specs, charters, _, _ = self.folded()
        self.carry()
        self.tick(0)
        specs, charters, _, _ = self.folded()
        self.assertEqual(charters[SRC]['state'], 'carried')
        self.assertFalse(relay._landed(SRC, specs, charters))               # target has not landed
        # Source specs cannot answer for it, even all accepted.
        accepted = {sid: {**s, 'state': 'accepted'} for sid, s in specs.items()}
        self.assertFalse(relay._landed(SRC, accepted, charters))
        self.assertFalse(relay.landed(SRC, self.events()))
        self.complete(TGT, seconds=100)
        specs, charters, _, _ = self.folded()
        self.assertTrue(relay._landed(SRC, specs, charters))
        # A looping carried pair terminates and never lands.
        loop = {'a': {'state': 'carried', 'carried_to': 'b', 'evs': []}, 'b': {'state': 'carried', 'carried_to': 'a', 'evs': []}}
        self.assertFalse(relay._landed('a', {}, loop))

    def test_open_charters_excludes_a_pending_request_and_planner_is_not_started(self):
        self.isolate()
        (self.root / 'repos' / 'demo').mkdir()
        with patch('project_scope.registered', return_value=True):
            self.charter(SRC, l1=False)
            self.charter(TGT, l1=False)
            self.assertIn(SRC, relay.open_charters(self.events(), root=self.root))
            self.carry()
            self.assertNotIn(SRC, relay.open_charters(self.events(), root=self.root))   # before the tick closes it
            self.assertIn(TGT, relay.open_charters(self.events(), root=self.root))
            self.tick(0)
            self.assertNotIn(SRC, relay.open_charters(self.events(), root=self.root))

    def test_pending_replan_does_not_override_a_carry_request(self):
        self.isolate()
        (self.root / 'repos' / 'demo').mkdir()
        with patch('project_scope.registered', return_value=True):
            self.seed_pair(with_spec=False)
            self.ok('tick', 'charter-replan', SRC, -1000,
                    verdict_src='L-charter-reviewer-9001.jsonl:1',
                    findings=[{'requirement': 'R1', 'finding': 'remaining work'}], uncovered=['R1'])
            self.assertIn(SRC, relay.open_charters(self.events(), root=self.root))
            self.carry()
            self.assertNotIn(SRC, relay.open_charters(self.events(), root=self.root))
            self.assertEqual(relay.waiting_lines(self.events(), root=self.root), [relay.DRY])


def runpy_main():
    import runpy
    runpy.run_path(str(Path(fold.__file__)), run_name='__main__')


# ═════════════════════════════════════════════════════════════════════════════
class AC8(Scratch):
    """R4 / SD9a: the target's close review or close-time brief lists carried failures; it never blocks."""

    def finish_target(self, cid):
        """The honest road: enter proving, sweep, review, then the tick's own close."""
        self.tick(20)                                                   # the tick enters proving
        self.ok('executor', 'sweep-fixpoint', cid, 30)
        self.ok('charter-reviewer', 'charter-review-complete', cid, 40)
        self.tick(100)
        self.tick(200)
        self.tick(300)

    def seed_failures(self):
        self.charter(SRC)
        self.charter(MID)
        self.spec(SPEC, SRC)
        self.spec(SPEC2, MID, seconds=-250000)
        still = self.failed_check(SPEC, -2000)
        resolved = self.failed_check(SPEC2, -2500)
        self.carry(SRC, TGT, seconds=-700)
        self.carry(MID, TGT, seconds=-650)
        return still, resolved

    def test_review_packet_lists_each_record_and_its_current_status(self):
        self.isolate()
        self.charter(TGT)
        still, resolved = self.seed_failures()
        self.tick(0)
        # SPEC2's check is later met: the record stays, its status changes.
        self.meet(SPEC2, 100)
        self.assertEqual(len(self.of('carried-check-failed', TGT)), 2)
        self.patch(packet.Ctx, 'charter_file', return_value=self.root / 'content' / f'{TGT}.md')
        self.patch(packet, 'review_account', return_value='read-only account')
        self.clock(200)
        ctx = packet.Ctx(type('A', (), {'subject': TGT, 'role': 'charter-reviewer'})())
        lines = packet.p_charter_reviewer(ctx)
        text = '\n'.join(lines)
        for needle in (SRC, MID, SPEC, SPEC2, 'AC7', still, resolved):
            self.assertIn(needle, text)
        row = {l.split(' · ')[1]: l for l in lines if ' · ' in l and l.startswith('   ')}
        self.assertTrue(row[SPEC].endswith('still failed'), row[SPEC])
        self.assertIn('no longer failed', row[SPEC2])

    def test_zero_records_leave_the_packet_unchanged(self):
        self.isolate()
        self.charter(TGT)
        self.patch(packet, 'review_account', return_value='read-only account')
        self.patch(packet.Ctx, 'charter_file', return_value=self.root / 'content' / f'{TGT}.md')
        ctx = packet.Ctx(type('A', (), {'subject': TGT, 'role': 'charter-reviewer'})())
        lines = packet.p_charter_reviewer(ctx)
        self.assertEqual(len(lines), 7)                     # the five items and their two lists, nothing added
        self.assertFalse(any(l.startswith('6.') for l in lines))
        self.assertEqual(charter_carry.record_lines(ctx.evs, ctx.specs, T0), [])

    def test_a_no_review_target_closes_then_opens_exactly_one_brief(self):
        self.isolate()
        self.charter(TGT, l1=True, covers='none')
        still, resolved = self.seed_failures()
        self.tick(0)                                                    # carries both sources, records, nothing else
        self.assertEqual(self.briefs('carried-failures-at-close'), [])
        label_before = fold.close_label(self.events(), TGT)
        closable_before = list(self.folded()[1][TGT]['closable'])
        # The target finishes normally (no review ever happens), then the tick closes it.
        self.finish_target(TGT)
        self.assertEqual(self.state(TGT), 'L2-complete')
        closes = [e for e in self.closed(TGT)]
        self.assertEqual([c['label'] for c in closes], ['accepted'] if closes[0]['label'] == 'accepted' else
                         [closes[0]['label']])
        self.assertEqual(len(closes), 1)
        self.assertEqual(len(self.briefs('carried-failures-at-close')), 1)
        items = self.digest('carried-failures-at-close')
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['owner'], items[0]['key']), ('thinker', TGT))
        self.assertNotIn('requirement', items[0]['reading'])
        self.assertNotIn('requirement', self.briefs('carried-failures-at-close')[0])
        blob = json.dumps(items[0])
        for needle in (SRC, MID, SPEC, SPEC2, still, resolved):
            self.assertIn(needle, blob)
        self.assertIn('still failed', blob)
        self.assertEqual(self.folded()[1][TGT]['briefs'], 0)             # informational: holds nothing
        self.assertEqual(label_before, 'accepted')
        self.assertTrue(all(isinstance(x, bool) for x in closable_before))

    def test_a_reviewed_target_gets_the_packet_not_the_brief(self):
        self.isolate()
        self.charter(TGT, l1=True, covers='R1')
        self.seed_failures()
        self.tick(0)
        self.finish_target(TGT)
        self.assertEqual(self.state(TGT), 'L2-complete')
        self.assertEqual(self.briefs('carried-failures-at-close'), [])
        self.assertEqual(len(self.of('carried-check-failed', TGT)), 2)    # the packet lists these

    def test_zero_record_target_closes_with_no_failure_brief(self):
        self.isolate()
        self.charter(TGT, l1=True, covers='none')
        self.finish_target(TGT)
        self.assertEqual(self.state(TGT), 'L2-complete')
        self.assertEqual(self.briefs('carried-failures-at-close'), [])

    def test_a_failure_after_close_follows_the_per_record_path(self):
        self.isolate()
        self.charter(SRC)
        self.charter(TGT, l1=True, covers='none')
        self.spec(SPEC, SRC)
        self.carry(SRC, TGT, seconds=-700)
        self.tick(0)
        self.finish_target(TGT)
        self.assertEqual(self.state(TGT), 'L2-complete')
        self.assertEqual(self.briefs('carried-failures-at-close'), [])
        self.failed_check(SPEC, 400)
        self.tick(500)
        self.assertEqual(len(self.of('carried-check-failed', TGT)), 1)
        self.assertEqual(len(self.digest('proving-check-failed')), 1)
        self.assertEqual(self.state(TGT), 'L2-complete')


# ═════════════════════════════════════════════════════════════════════════════
SELECTORS = {name: cls for name, cls in globals().items()
             if name.startswith('AC') and name[2:].isdigit() and isinstance(cls, type)}


def main(argv):
    if '--ac' not in argv:
        unittest.main(argv=[argv[0]] + [a for a in argv[1:]], verbosity=1)
        return 0
    i = argv.index('--ac')
    name = argv[i + 1] if i + 1 < len(argv) else ''
    if name not in SELECTORS:
        print(f'unsupported or empty selection {name!r}; supported: {", ".join(sorted(SELECTORS))}', file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SELECTORS[name])
    if suite.countTestCases() == 0:
        print(f'{name} selected no assertions', file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    body = json.dumps({'criterion': name, 'cases': Scratch.observations,
                       'tests_run': result.testsRun, 'failures': len(result.failures),
                       'errors': len(result.errors)}, sort_keys=True)
    print(json.dumps({'url': f'scratch://charter-carry/{name}',
                      'status': 0 if result.wasSuccessful() else 1,
                      'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                      'body_excerpt': body}, sort_keys=True))
    if not result.wasSuccessful() or result.testsRun == 0:
        return 1
    print(f'{name}: {result.testsRun} case(s) passed')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
