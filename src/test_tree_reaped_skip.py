"""L-spec-2137: dead project `tree-reaped` records are skipped before JSON decoding.

Every test runs on private scratch; the live ledger is only read (AC5/AC6), through a
prefix-verified byte copy. No test writes a source file, a cache or the board outside
its own scratch directory. Reports print source ids, counts and hashes, never payloads.
"""
import ast
import contextlib
import datetime
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# Set before importing fold; the operator's root and cache are never touched.
SCRATCH = Path(tempfile.mkdtemp(prefix='tree-reaped-skip-'))
os.environ['DOIT_ROOT'] = str(SCRATCH / 'root')
os.environ['V4_LEDGER_DIR'] = str(SCRATCH / 'v4-ledger')
os.environ['V4_INBOX_DIR'] = str(SCRATCH / 'v4-inbox')
sys.path.insert(0, str(HERE))
import fold
import ledger_cache
import ledger_files
import ledger_compact
import scorecard

LIVE_ROOT = Path('/home/albert/.do-it')   # AC5/AC6 read this literal source root
NOW = datetime.datetime(2026, 11, 6, 12, 0, tzinfo=datetime.timezone.utc)
MARK = '"type": "tree-reaped"'
REPORTS = SCRATCH / 'reports'
REPORTS.mkdir()


def reap(subject='proj-a', project='proj-a', ts='2026-11-05T10:00:00+00:00', **extra):
    row = dict(project=project, reaped=['l-spec-0001'], repo='/r', retained=[], ts=ts,
               type='tree-reaped', v=1, **extra)
    if subject is not None:
        row['subject'] = subject
    return json.dumps(row, sort_keys=True)


def ev(kind, subject, ts, **extra):
    return json.dumps(dict(type=kind, subject=subject, ts=ts, v=1, **extra), sort_keys=True)


class Scratch(unittest.TestCase):
    """A private ROOT/events with fold pointed at it and a decode counter."""
    project = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        p = patch.multiple(fold, ROOT=self.root, EVENTS=self.events, PROJECT=self.project, NOW=NOW)
        p.start()
        self.addCleanup(p.stop)

    def put(self, name, lines, mode='w', end='\n'):
        with (self.events / name).open(mode, newline='') as fh:
            fh.write(''.join(line + end for line in lines))

    def cache_files(self):
        return sorted((self.root / 'cache').glob('*.sqlite3')) if (self.root / 'cache').exists() else []

    @contextlib.contextmanager
    def decoded(self):
        """Every line text that reaches a JSON decoder (cached and reference parsers)."""
        seen, real = [], json.loads

        def spy(text, *a, **k):
            seen.append(text if isinstance(text, str) else text.decode())
            return real(text, *a, **k)
        with patch.object(json, 'loads', spy):
            yield seen

    def full(self):
        return fold.read_events(use_cache=False, with_reaped=True)

    def minus(self, events, srcs):
        return [e for e in events if e['_src'] not in srcs]

    def expected(self):
        ref = self.full()
        return self.minus(ref, eligible_sources(ref))

    def assertSameEvents(self, a, b):
        self.assertEqual(len(a), len(b), 'event counts differ')
        for i, (actual, expected) in enumerate(zip(a, b)):
            if actual != expected:
                self.fail(f'event mismatch at index {i}: sources '
                          f'{actual.get("_src")} / {expected.get("_src")}')


class TestPredicate(Scratch):
    def test_tied_survivor_order(self):
        stamp = '2026-11-05T10:00:00+00:00'
        self.put('L-executor-0001.jsonl', [reap(subject='z-project', ts=stamp),
                                         ev('observed', 'a-subject', stamp)])
        self.put('L-operator-0001.jsonl', [ev('observed', 'm-subject', stamp)])
        full = self.full()
        expected = self.minus(full, {'L-executor-0001.jsonl:1'})
        self.assertEqual([e['subject'] for e in expected], ['m-subject', 'a-subject'])
        for use_cache in (False, True):
            with self.subTest(use_cache=use_cache):
                self.assertSameEvents(fold.read_events(use_cache=use_cache), expected)

    def test_order_keys_and_refresh(self):
        stamp = '2026-11-05T10:00:00+00:00'
        lines = [reap(subject='same', ts=stamp),
                 reap(subject='same', ts=stamp, retained_count=5),
                 reap(subject=None, ts=stamp),
                 reap(subject='café', ts=stamp)]
        for line in lines:
            if ledger_cache.reap_candidate(line, 'executor') is not None:
                parsed = ledger_compact.compact(json.loads(line))
                self.assertEqual(ledger_cache.reap_order_key(line, 'executor'), fold.event_content_key(parsed))
        for malformed in (reap()[:-1] + ',}', reap().replace('"v": 1', '"v": 01'), '\v' + reap()):
            self.assertIsNone(ledger_cache.reap_candidate(malformed, 'executor'))
        self.put('L-executor-0001.jsonl', [lines[0], ev('observed', 'a', stamp)])
        # This unauthorized reap collides on subject/type; canonical content is
        # required to position its queue relative to the skipped executor head.
        self.put('L-builder-0001.jsonl', [reap(subject='same', ts=stamp, retained_count=3), ev('observed', 'b', stamp)])
        for phase in ('cold', 'warm', 'append'):
            if phase == 'append':
                self.put('L-executor-0001.jsonl', [ev('observed', 'c', stamp)], 'a')
            full = self.full()
            expected = self.minus(full, {'L-executor-0001.jsonl:1'})
            for cache in (True, False):
                self.assertSameEvents(fold.read_events(use_cache=cache), expected)

    def cases(self):
        """name -> (file, line, eligible)"""
        ex, bl = 'L-executor-0001.jsonl', 'L-builder-0001.jsonl'
        nested = json.dumps(dict(type='note', subject='n1', ts='2026-11-05T10:03:00+00:00',
                                 payload={'type': 'tree-reaped'}, v=1), sort_keys=True)
        return {
            'canonical project reap': (ex, reap(), True),
            'charter reap': (ex, reap(subject='L-charter-0007'), False),
            'absent subject': (ex, reap(subject=None), True),
            'alternate spacing without exact marker': (ex, reap().replace(MARK, '"type":"tree-reaped"'), False),
            'other type mentioning tree-reaped': (ex, ev('note', 'n0', '2026-11-05T10:02:00+00:00',
                                                         text='see tree-reaped rows'), False),
            'nested marker': (ex, nested, False),
            'escaped marker': (ex, ev('note', 'n2', '2026-11-05T10:04:00+00:00', text=MARK), False),
            'unauthorized builder reap': (bl, reap(ts='2026-11-05T10:05:00+00:00'), False),
            'forged actor field': (bl, reap(actor='executor', ts='2026-11-05T10:06:00+00:00'), False),
            'escaped discarded field': (ex, reap(retained_reason='quoted "reason"\nline'), True),
            'operator authored reap': ('L-operator-0001.jsonl', reap(ts='2026-11-05T10:07:00+00:00'), True),
            'blank line': (ex, '', False),
            'malformed line': (ex, '{"type": "tree-reaped", ', False),
        }

    def test_exact_bytes_actor_and_correction(self):
        cases = self.cases()
        print('AC1 case inventory:')
        for name, (f, line, want) in cases.items():
            got = ledger_cache.reap_candidate(line, fold.actor_of(f)) is not None
            print(f'  {name:42} file={f} eligible={got}')
            self.assertEqual(got, want, name)
        # CRLF and unterminated final record, end to end through both parsers.
        by_file = {}
        for name, (f, line, _) in cases.items():
            by_file.setdefault(f, []).append(line)
        for f, lines in by_file.items():
            self.put(f, lines)
        self.put('L-executor-0002.jsonl', [reap(ts='2026-11-05T11:00:00+00:00'),
                                           reap(ts='2026-11-05T11:01:00+00:00')], end='\r\n')
        self.put('L-executor-0003.jsonl', [reap(ts='2026-11-05T12:00:00+00:00'), reap(ts='2026-11-05T12:01:00+00:00')])
        with (self.events / 'L-executor-0003.jsonl').open('rb+') as fh:   # drop the final newline
            fh.truncate(fh.seek(0, 2) - 1)
        ref = self.full()
        eligible = {e['_src'] for e in ref if e['type'] == 'tree-reaped' and e['actor'] in ('executor', 'operator')
                    and not str(e.get('subject', '')).startswith('L-charter-')
                    and MARK in json.dumps(e, sort_keys=True)}
        # Alternate spacing parses to the same dict, but its bytes carry no exact marker.
        eligible -= {e['_src'] for e in ref if e['_src'].endswith(':4') and e['_src'].startswith('L-executor-0001')}
        # The unterminated final record is not a committed line: it is parsed.
        unterminated = 'L-executor-0003.jsonl:2'
        eligible.discard(unterminated)
        self.assertGreaterEqual(len(eligible), 5)
        for use_cache in (False, True):
            with self.decoded() as seen:
                got = fold.read_events(use_cache=use_cache)
            eligible_text = {line for _, (f, line, want) in cases.items() if want} | {
                reap(ts='2026-11-05T11:00:00+00:00'), reap(ts='2026-11-05T11:01:00+00:00'), reap(ts='2026-11-05T12:00:00+00:00')}
            self.assertEqual(eligible_text & set(seen), set(), f'eligible candidate reached a decoder (use_cache={use_cache})')
            self.assertTrue(set(seen))
            self.assertSameEvents(got, fold.finish_events(self.minus(ref, eligible), owned=True))
        # Unauthorized builder reaps stay parsed and keep the same ignored count.
        _, _, ignored_full, _ = fold.fold(ref)
        default = fold.read_events()
        _, _, ignored_default, _ = fold.fold(default)
        self.assertEqual(len(ignored_full), len(ignored_default))
        self.assertGreaterEqual(len(ignored_default), 2)

        # Operator corrections: one changes a candidate's subject to a charter, one voids another.
        base = fold.read_events()
        src_a, src_b = 'L-executor-0002.jsonl:1', 'L-executor-0002.jsonl:2'
        self.assertNotIn(src_a, {e['_src'] for e in base})
        for ref_src, setting in ((src_a, {'subject': 'L-charter-0099'}), (src_b, {'type': 'voided'})):
            self.put('L-operator-0009.jsonl', [json.dumps(dict(type='correction', subject='x', ts='2026-11-06T00:00:00+00:00',
                                                               ref=ref_src, set=setting, v=1))], 'a')
        for use_cache in (True, False):
            got = {e['_src']: e for e in fold.read_events(use_cache=use_cache)}
            self.assertEqual(got[src_a]['subject'], 'L-charter-0099')
            self.assertEqual(got[src_b]['type'], 'voided')
            full_now = self.full()
            not_skippable = {'L-executor-0003.jsonl:2'} | {e['_src'] for e in ref if e['_src'] == 'L-executor-0001.jsonl:4'}
            self.assertSameEvents(fold.read_events(use_cache=use_cache),
                                  self.minus(full_now, eligible_sources(full_now) - not_skippable))
        self.assertEqual(len(fold.APPLIED), 2)
        # A non-operator correction cannot reach a skipped line: it stays skipped.
        other = 'L-executor-0002.jsonl:2'
        (self.events / 'L-operator-0009.jsonl').unlink()
        self.put('L-planner-0001.jsonl', [json.dumps(dict(type='correction', subject='x', ts='2026-11-06T00:00:00+00:00',
                                                          ref=other, set={'type': 'voided'}, v=1))])
        self.assertNotIn(other, {e['_src'] for e in fold.read_events()})
        self.assertNotIn(other, {e['_src'] for e in fold.read_events(use_cache=False)})

    def test_public_signature(self):
        params = inspect.signature(fold.read_events).parameters
        self.assertEqual(list(params), ['use_cache', 'snapshot', 'with_reaped'])
        self.assertIs(params['use_cache'].default, True)
        self.assertEqual(params['use_cache'].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name in ('snapshot', 'with_reaped'):
            self.assertEqual(params[name].kind, inspect.Parameter.KEYWORD_ONLY, name)
        self.assertIs(params['snapshot'].default, None)
        self.assertIs(params['with_reaped'].default, False)
        with self.assertRaises(TypeError):
            fold.read_events(True, None, True)
        self.put('L-executor-0001.jsonl', [reap(), ev('spec-written', 'L-spec-1', '2026-11-05T10:01:00+00:00')])
        with patch.object(ledger_cache, 'read', side_effect=AssertionError('cache read')), \
                patch.object(ledger_cache, 'read_conditional', side_effect=AssertionError('cache read')):
            full = fold.read_events(True, with_reaped=True)
            self.assertEqual([e['type'] for e in full], ['tree-reaped', 'spec-written'])
            self.assertEqual(self.cache_files(), [])
        self.assertEqual([e['type'] for e in fold.read_events()], ['spec-written'])
        self.assertEqual(len(self.cache_files()), 1)
        self.assertEqual([e['type'] for e in fold.read_events(with_reaped=True)], ['tree-reaped', 'spec-written'])


def fresh_read(root, code):
    env = dict(os.environ, DOIT_ROOT=str(root))
    env.pop('DOIT_PROJECT', None)
    out = subprocess.run([sys.executable, '-B', '-c', f'import sys; sys.path.insert(0, {str(HERE)!r}); import fold, json\n{code}'],
                         capture_output=True, text=True, env=env, check=True)
    return json.loads(out.stdout.splitlines()[-1])


class TestCache(Scratch):
    def stored(self):
        """Committed state read through a separate connection: (version, events, skip blobs)."""
        (db_path,) = self.cache_files()
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            events = []
            for data, sha in db.execute('SELECT data,sha FROM chunks ORDER BY name,seq'):
                events.extend(ledger_cache._unpack(data, sha))
            for tail, sha in db.execute('SELECT tail,tail_sha FROM files'):
                events.extend(ledger_cache._unpack(tail, sha))
            skips = [ledger_cache._unpack_skips(s, h) for s, h in db.execute('SELECT skip,skip_sha FROM files')]
            raw = b''.join(r[0] for r in db.execute('SELECT skip FROM files'))
        return version, events, skips, raw

    def test_committed_round_trip_and_version(self):
        self.assertEqual(ledger_cache.VERSION, 5)
        keep = ev('spec-written', 'L-spec-1', '2026-11-05T09:00:00+00:00')
        charter_reap = reap(subject='L-charter-0007', ts='2026-11-05T09:30:00+00:00')
        self.put('L-executor-0001.jsonl', [keep, reap(ts='2026-11-05T10:00:00+00:00'), charter_reap,
                                           reap(project='proj-b', ts='2026-11-05T10:01:00+00:00')])
        # cold
        got = fold.read_events()
        version, rows, skips, raw = self.stored()
        self.assertEqual(version, 5)
        self.assertEqual([e['type'] for e in rows], ['spec-written', 'tree-reaped'])
        self.assertEqual([e['subject'] for e in rows if e['type'] == 'tree-reaped'], ['L-charter-0007'])
        self.assertEqual(skips[0][0], [2, 4])
        self.assertEqual(sum(skips[0][1].values()), 2)
        self.assertNotIn(b'l-spec-0001', raw)          # no skipped payload persists
        self.assertSameEvents(got, self.minus(self.full(), {'L-executor-0001.jsonl:2', 'L-executor-0001.jsonl:4'}))
        # warm, and a fresh reader process on the committed cache
        self.assertSameEvents(fold.read_events(), got)
        fresh = fresh_read(self.root, 'print(json.dumps(fold.read_events(), sort_keys=True))')
        self.assertEqual(json.dumps(fresh, sort_keys=True), json.dumps(list(got), sort_keys=True))
        # a version-4 cache is rejected and rebuilt at 5
        (db_path,) = self.cache_files()
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('PRAGMA user_version=4')
        self.assertSameEvents(fold.read_events(), got)
        self.assertEqual(self.stored()[0], 5)
        # dead and live appends refresh the tail only
        self.put('L-executor-0001.jsonl', [reap(ts='2026-11-05T11:00:00+00:00'),
                                           ev('spec-written', 'L-spec-2', '2026-11-05T11:01:00+00:00')], 'a')
        after = fold.read_events()
        _, rows, skips, raw = self.stored()
        self.assertEqual(skips[0][0], [2, 4, 5])
        self.assertEqual([e['subject'] for e in after if e['type'] == 'spec-written'], ['L-spec-1', 'L-spec-2'])
        self.assertEqual(sum(1 for e in rows if e['type'] == 'tree-reaped'), 1)
        # torn tail: the unterminated record is parsed, then skipped once completed
        with (self.events / 'L-executor-0001.jsonl').open('a') as fh:
            fh.write(reap(ts='2026-11-05T12:00:00+00:00'))
        torn = fold.read_events()
        self.assertEqual(sum(e['type'] == 'tree-reaped' for e in torn), 2)
        self.assertSameEvents(torn, fold.read_events(use_cache=False))
        with (self.events / 'L-executor-0001.jsonl').open('a') as fh:
            fh.write('\n')
        done = fold.read_events()
        self.assertEqual(sum(e['type'] == 'tree-reaped' for e in done), 1)
        self.assertEqual(self.stored()[2][0][0], [2, 4, 5, 7])
        # a correction appended in ANOTHER file after the read skipped its target
        self.put('L-operator-0001.jsonl', [json.dumps(dict(type='correction', subject='c', ts='2026-11-06T00:00:00+00:00',
                                                           ref='L-executor-0001.jsonl:2', set={'type': 'voided'}, v=1))])
        corrected = fold.read_events()
        self.assertEqual({e['_src']: e['type'] for e in corrected}['L-executor-0001.jsonl:2'], 'voided')
        self.assertSameEvents(corrected, self.expected())
        self.assertEqual(len(fold.APPLIED), 1)
        _, stored, _, _ = self.stored()
        self.assertIn('L-executor-0001.jsonl:2', {e['_src'] for e in stored})
        self.assertNotIn('L-executor-0001.jsonl:4', {e['_src'] for e in stored})
        self.assertEqual(fresh_read(self.root, 'print(json.dumps(fold.read_events(), sort_keys=True))'),
                         list(corrected))
        # historical correction edit (rewrite in place): the target is released again
        self.put('L-operator-0001.jsonl', [json.dumps(dict(type='correction', subject='c', ts='2026-11-06T00:00:00+00:00',
                                                           ref='L-executor-0001.jsonl:4', set={'subject': 'L-charter-0042'}, v=1))])
        edited = fold.read_events()
        by_src = {e['_src']: e for e in edited}
        self.assertNotIn('L-executor-0001.jsonl:2', by_src)
        self.assertEqual(by_src['L-executor-0001.jsonl:4']['subject'], 'L-charter-0042')
        self.assertSameEvents(edited, self.expected())
        self.assertEqual(self.stored()[0], 5)
        _, stored, _, _ = self.stored()
        self.assertNotIn('L-executor-0001.jsonl:2', {e['_src'] for e in stored})
        self.assertIn('L-executor-0001.jsonl:4', {e['_src'] for e in stored})
        with patch.object(fold, '_read_events_uncached', side_effect=AssertionError('target cache bypass')):
            self.assertSameEvents(fold.read_events(), edited)
        # project scope change: cache holds every project, the read is scoped
        (self.events / 'L-operator-0001.jsonl').unlink()
        with patch.object(fold, 'PROJECT', 'proj-b'):
            scoped = fold.read_events()
            self.assertSameEvents(scoped, [e for e in fold.read_events(use_cache=False, with_reaped=True)
                                           if e.get('project') == 'proj-b' and e['_src'] not in
                                           {'L-executor-0001.jsonl:4', 'L-executor-0001.jsonl:5', 'L-executor-0001.jsonl:7', 'L-executor-0001.jsonl:2'}])
            self.assertEqual(sum(fold.skipped_counts(scoped).values()), 1)
        self.assertEqual(sum(fold.skipped_counts(fold.read_events()).values()), 4)


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def frozen_board(events):
    # notes.board_rows ages rows off the wall clock; freeze it so two renders agree.
    import notes
    with patch.object(notes, 'datetime', _FrozenDatetime):
        return fold.render(events, *fold.fold(events))


class TestConsumers(Scratch):
    def fixture(self):
        self.put('L-executor-0001.jsonl', [
            ev('spec-written', 'L-spec-1', '2026-11-05T09:00:00+00:00', project='proj-a'),
            reap(project='proj-a', ts='2026-11-05T10:00:00+00:00'),
            reap(project='proj-b', ts='not-a-timestamp'),
            reap(project='proj-a', ts='2026-11-05T10:01:00+00:00', subject='proj-a'),
            reap(subject='L-charter-0007', project='proj-a', ts='2026-11-05T10:02:00+00:00'),
            reap(project='proj-b', ts='2026-11-05T10:03:00+00:00'),
        ])
        self.put('L-operator-0001.jsonl', [ev('spec-written', 'L-spec-2', '2026-11-05T09:30:00+00:00', project='proj-b')])

    def test_events_board_up_scorecard_look(self):
        self.fixture()
        modes = []
        real_uncached = fold._read_events_uncached
        real_read = fold.read_events

        def spy_read(*a, **k):
            modes.append(('read_events', k.get('with_reaped', False)))
            self.assertFalse(k.get('with_reaped', False), 'frequent reader requested full history')
            return real_read(*a, **k)

        def spy_uncached(with_reaped=True):
            modes.append(('uncached', with_reaped))
            self.fail('frequent reader bypassed the cache')
        import notes
        import up
        import pace
        import stall_sample
        contracts = self.root / 'agents'
        contracts.mkdir()
        (contracts / 'executor.md').write_text('fixture executor contract')
        for project in (None, 'proj-a', 'proj-b'):
            with patch.object(fold, 'PROJECT', project):
                # board: direct render, then through up.py's executor loop and the fold CLI
                default, full = fold.read_events(), fold.read_events(with_reaped=True)
                expected = frozen_board(full)
                self.assertEqual(frozen_board(default), expected, f'board project={project}')
                self.assertEqual(frozen_board(default), expected)       # repeated read
                self.assertEqual(frozen_board(fold.read_events(use_cache=False)), expected)
                print(f'AC3 board project={project} fold@ line:', expected.splitlines()[0].split(' · ')[-2:])
                # scorecard rows and text
                for events in (default, fold.read_events(use_cache=False),
                               fold.read_events(snapshot=fold.ReaderSnapshot())):
                    for rows_a, rows_b in [(scorecard.day_rows(events, NOW, days=2), scorecard.day_rows(full, NOW, days=2))]:
                        self.assertEqual(rows_a, rows_b)
                        self.assertEqual(rows_a[-1]['skipped_events'], rows_b[-1]['skipped_events'])
                        self.assertEqual(scorecard.copy_text(rows_a[-1]), scorecard.copy_text(rows_b[-1]))
                with patch.object(fold, 'read_events', spy_read), \
                        patch.object(fold, '_read_events_uncached', spy_uncached), \
                        patch.object(ledger_files, 'iter_sources', side_effect=AssertionError('whole-ledger parse')), \
                        patch.object(notes, 'datetime', _FrozenDatetime):
                    # Execute the executor's board path up to its existing launch boundary.
                    with patch.object(up.dispatch, 'AGENTS', contracts), \
                            patch.object(up.models, 'load', return_value=None), \
                            patch.object(up, '_check_guard'), patch.object(up, 'install'), \
                            patch.object(up.launch, 'model_for', return_value=None), \
                            patch.object(up, '_child_env', return_value={}), \
                            patch.object(up.launch, 'child_env', side_effect=lambda role, base: base), \
                            patch.object(up, '_pane_argv', side_effect=lambda name, board, **k: ['fixture-pane', board]), \
                            patch.object(up.subprocess, 'Popen', side_effect=AssertionError('pane launch')), \
                            contextlib.redirect_stdout(io.StringIO()):
                        command, _ = up.executor_loop(self.root, 5, print_only=True, max_cycles=1)
                    self.assertEqual(command[-1], expected, f'up board project={project}')
                    # Run the real CLI body at a frozen clock, comparing every output byte.
                    output = io.StringIO()
                    with patch.dict(os.environ, DOIT_ROOT=str(self.root), DOIT_PROJECT=project or ''), \
                            patch.object(datetime, 'datetime', _FrozenDatetime), \
                            patch.object(sys, 'argv', [str(HERE / 'fold.py'), 'board']), \
                            patch.object(sys, 'excepthook', sys.excepthook), contextlib.redirect_stdout(output):
                        runpy.run_path(str(HERE / 'fold.py'), run_name='__main__')
                    self.assertEqual(output.getvalue(), expected + '\n', f'CLI board project={project}')
                    # main's JSON and text use the same accounting as its direct API.
                    output = io.StringIO()
                    with patch.object(pace, 'samples', return_value=[]), \
                            patch.object(stall_sample, 'samples', return_value=[]), contextlib.redirect_stdout(output):
                        self.assertEqual(scorecard.main(['2026-11-05']), 0)
                    row = scorecard.day_rows(full, NOW.replace(hour=0), days=2)[0]
                    self.assertEqual(output.getvalue(), json.dumps(row) + '\n' + scorecard.copy_text(row) + '\n')
        # the plain-list path keeps ordinary semantics: no accounting, no mutation
        plain = list(fold.read_events(with_reaped=True))
        before = json.dumps(plain, sort_keys=True)
        self.assertEqual(sum(scorecard.day_rows(plain, NOW, days=1)[0]['skipped_events'] for _ in [0]),
                         scorecard.day_rows(plain, NOW, days=1)[0]['skipped_events'])
        frozen_board(plain)
        self.assertEqual(json.dumps(plain, sort_keys=True), before)
        self.assertEqual(fold.skipped_counts(plain), {})
        # the invalid timestamp is the only skipped event scorecard reports
        self.assertEqual(scorecard.day_rows(fold.read_events(), NOW, days=1)[0]['skipped_events'], 1)
        self.assertNotIn(('read_events', True), modes)
        self.assertNotIn(('uncached', True), modes)
        # fold CLI: board equals the full-history board; `events` lists requested cleanup history
        cli = lambda *a: subprocess.run([sys.executable, '-B', str(HERE / 'fold.py'), *a], capture_output=True, text=True,
                                        env=dict(os.environ, DOIT_ROOT=str(self.root), DOIT_PROJECT=''), check=True).stdout
        listing = cli('events', 'proj-a')
        self.assertEqual(sum(1 for line in listing.splitlines() if json.loads(line)['type'] == 'tree-reaped'), 4)
        self.assertIn('fold @ 7', cli('board').splitlines()[0])
        # look: the per-file raw-record answer, ledger and transcript inputs
        import look
        with patch.object(ledger_cache, 'read', side_effect=AssertionError('routine look read the cache')), \
                patch.object(fold, '_read_events_uncached', side_effect=AssertionError('whole-ledger parse')):
            rows = look._read_jsonl(self.events / 'L-executor-0001.jsonl')
        raw = [json.loads(l) for l in (self.events / 'L-executor-0001.jsonl').read_text().splitlines()]
        self.assertEqual(rows, raw)
        self.assertTrue(all('actor' not in r and '_src' not in r for r in rows))
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('{"type": "assistant"}\n{broken\n[1]\n')
        self.assertEqual(look._read_jsonl(transcript), [{'type': 'assistant'}])
        # post-append refresh
        self.put('L-executor-0001.jsonl', [reap(project='proj-a', ts='2026-11-05T13:00:00+00:00')], 'a')
        self.assertEqual(frozen_board(fold.read_events()), frozen_board(fold.read_events(with_reaped=True)))
        # bundle_wait's length is a memo freshness guard, not a history count.
        # The same caller snapshot reuses mains; a new list or an append cannot.
        import bundle_wait
        for events in (fold.read_events(), fold.read_events(with_reaped=True),
                       fold.read_events(snapshot=fold.ReaderSnapshot()), list(fold.read_events())):
            with patch.object(bundle_wait, '_main_snapshot', None), \
                    patch.object(bundle_wait, 'project_main', side_effect=lambda p, **k: 'main-' + p) as resolve:
                mains = bundle_wait.project_mains(events, ['L-spec-1', 'L-spec-2'], root=self.root)
                self.assertEqual(mains, {'proj-a': 'main-proj-a', 'proj-b': 'main-proj-b'})
                self.assertIs(bundle_wait.cached_mains(events), mains)
                self.assertEqual(bundle_wait.cached_mains(list(events)), {})
                events.append({'type': 'observed', 'subject': 'new'})
                self.assertEqual(bundle_wait.cached_mains(events), {})
                self.assertEqual(resolve.call_count, 2)

    def test_reader_inventory_and_tick_reaped(self):
        rows, findings = [], []
        classes = {
            'fold_emits.py': 'declares the emitter set; reads nothing',
            'ledger_compact.py': 'projection of the stored shape',
            'ledger_cache.py': 'the skip itself',
            'fold.py': 'owner: board render, health and CLI take the skipped accounting',
            'brief.py': 'charter-scoped lookup (evs of an L-charter-* subject)',
            'proving.py': 'charter-scoped lookup (evs of a charter)',
            'tree_cleanup.py': 'writer of the record; its tests request with_reaped=True',
            'tick.py': 'assigns a local set that nothing loads (checked below)',
        }
        for path in sorted(HERE.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            tree = ast.parse(path.read_text())
            hits = sorted({n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                           and 'tree-reaped' in n.value and not (isinstance(n.value, str) and n.value.startswith('`'))})
            hits = [h for h in hits if 'tree-reaped' in path.read_text().splitlines()[h - 1] and not path.read_text().splitlines()[h - 1].lstrip().startswith('#')]
            if not hits:
                continue
            verdict = classes.get(path.name)
            rows.append((path.name, f'lines {hits}', verdict or 'UNCLASSIFIED'))
            if verdict is None:
                findings.append(path.name)
        # whole-ledger readers: default mode only, never with_reaped=True outside the owner and its cleanup helper
        for path in sorted(HERE.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and any(k.arg == 'with_reaped' for k in node.keywords):
                    rows.append((path.name, f'line {node.lineno}', 'explicit full-history request'))
                    if path.name != 'fold.py':
                        findings.append(f'{path.name}:{node.lineno} requests full history')
        count_classes = {
            'fold.py': 'owner: count accounting and admission-prefix cache',
            'scorecard.py': 'owner: timestamp-sensitive skipped count',
            'carry.py': 'reverse index search for spec-written, not a displayed count',
            'grader_packet.py': 'index search for failure and bundle boundaries',
            'merge_queue.py': 'reverse index search for queue anchor',
            'pass_memo.py': 'memo freshness by list identity and current length',
            'bundle_wait.py': 'memo freshness by list identity and current length (consumer fixture checked)',
            'fold_stalls.py': 'index boundary within subject history',
        }
        # Re-enumerate entry points and count consumers, rather than trusting a
        # dated list. Unknown raw-count consumers are explicit footprint findings.
        for path in sorted(HERE.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                symbol = ast.unparse(node.func)
                if symbol.endswith('read_events') or symbol in ('ledger_cache.read', 'ledger_cache.read_conditional', 'ledger_cache.tail_refresh', 'ledger_cache.read_default', 'ledger_cache.read_uncached'):
                    rows.append((path.name, f'{symbol}:{node.lineno}', 'ledger entry point: default filtered history'))
                if (symbol == 'len' and node.args and isinstance(node.args[0], ast.Name)
                        and node.args[0].id in ('events', 'ev', 'evs')):
                    classification = count_classes.get(path.name)
                    rows.append((path.name, f'len({node.args[0].id}):{node.lineno}', classification or 'UNCLASSIFIED COUNT'))
                    if classification is None:
                        findings.append(f'{path.name}:{node.lineno} ungranted count consumer')
        (REPORTS / 'AC3-reader-inventory.json').write_text(json.dumps(rows, indent=2))
        print('AC3 inventory report:', REPORTS / 'AC3-reader-inventory.json')
        for name, where, kind in rows:
            print(f'AC3 inventory {name:20} {where:16} {kind}')
        self.assertEqual(findings, [], f'affected reader not handled: {findings}')
        # tick: the `reaped` set has no executable loads
        tree = ast.parse((HERE / 'tick.py').read_text())
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            stores = [n for n in ast.walk(func) if isinstance(n, ast.Name) and n.id == 'reaped' and isinstance(n.ctx, ast.Store)]
            loads = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Name) and n.id == 'reaped' and isinstance(n.ctx, ast.Load)]
            if stores:
                print(f'AC3 tick {func.name}: reaped stores={len(stores)} loads={loads}')
                self.assertEqual(loads, [], f'tick.{func.name} consumes `reaped`: a project match would be a finding')
        # mixed charter/project fixtures: the set a default read yields is exactly the charter ids
        self.put('L-executor-0001.jsonl', [reap(subject='L-charter-0001'), reap(subject='proj-a'), reap(subject='L-charter-0002'),
                                           reap(subject=None)])
        names = lambda events: {e.get('subject') for e in events if e['type'] == 'tree-reaped'}
        self.assertEqual(names(fold.read_events()), {'L-charter-0001', 'L-charter-0002'})
        self.assertEqual({s for s in names(fold.read_events(with_reaped=True)) if str(s).startswith('L-charter-')},
                         names(fold.read_events()))


class TestCompatibility(Scratch):
    def test_modes_failures_and_snapshot(self):
        self.put('L-executor-0001.jsonl', [
            ev('spec-written', 'L-spec-1', '2026-11-05T09:00:00+00:00', project='proj-a', title='t'),
            reap(project='proj-a'), reap(project='proj-b', ts='2026-11-05T10:01:00+00:00'),
            reap(subject='L-charter-0007', project='proj-a', ts='2026-11-05T10:02:00+00:00'),
        ])
        self.put('L-operator-0001.jsonl', [json.dumps(dict(type='correction', subject='c', ts='2026-11-05T11:00:00+00:00',
                                                           ref='L-executor-0001.jsonl:1', set={'title': 'fixed'}, v=1))])
        want = fold.read_events(use_cache=False)
        full = fold.read_events(use_cache=False, with_reaped=True)
        applied = json.dumps(fold.APPLIED, sort_keys=True)
        self.assertEqual(len(want), len(full) - 2)
        matrix = {}
        matrix['cold'] = fold.read_events()
        matrix['warm'] = fold.read_events()
        snap = fold.ReaderSnapshot()
        matrix['conditional'] = fold.read_events(snapshot=snap)
        matrix['conditional-unchanged'] = fold.read_events(snapshot=snap)
        with patch.dict(os.environ, DOIT_READ_CACHE='0'):
            matrix['env-off'] = fold.read_events()
        for name, got in matrix.items():
            self.assertSameEvents(got, want)
            self.assertEqual(got[0]['title'], 'fixed', name)
            self.assertEqual(sum(fold.skipped_counts(got).values()), 2, name)
        print('AC4 mode matrix:', sorted(matrix))
        # alternating full/default calls with ONE snapshot: no contamination, APPLIED restored
        for _ in range(2):
            self.assertSameEvents(fold.read_events(snapshot=snap, with_reaped=True), full)
            self.assertSameEvents(fold.read_events(snapshot=snap), want)
            self.assertEqual(json.dumps(fold.APPLIED, sort_keys=True), applied)
        with patch.object(ledger_cache, 'read_conditional', side_effect=AssertionError('cached')), \
                patch.object(ledger_cache, 'read', side_effect=AssertionError('cached')):
            self.assertSameEvents(fold.read_events(snapshot=snap, with_reaped=True), full)
        # admission-prefix reuse still works for the default snapshot list
        again = fold.read_events(snapshot=snap)
        self.assertIsNotNone(getattr(again, '_snapshot', None))
        first = fold.fold(again)
        self.assertEqual(list(first[0]), list(fold.fold(want)[0]))
        # returned-object isolation
        again[0]['title'] = 'mutated'
        self.assertEqual(fold.read_events(snapshot=snap)[0]['title'], 'fixed')
        self.assertEqual(fold.read_events()[0]['title'], 'fixed')
        # append and project changes
        self.put('L-executor-0001.jsonl', [reap(project='proj-b', ts='2026-11-05T14:00:00+00:00'),
                                           ev('spec-written', 'L-spec-3', '2026-11-05T14:01:00+00:00', project='proj-b')], 'a')
        grown = fold.read_events(snapshot=snap)
        self.assertSameEvents(grown, fold.read_events(use_cache=False))
        self.assertEqual(sum(fold.skipped_counts(grown).values()), 3)
        with patch.object(fold, 'PROJECT', 'proj-b'):
            scoped = fold.read_events(snapshot=snap)
            self.assertEqual({e['project'] for e in scoped}, {'proj-b'})
            self.assertEqual(sum(fold.skipped_counts(scoped).values()), 2)
            self.assertSameEvents(scoped, fold.read_events(use_cache=False))
        self.assertSameEvents(fold.read_events(snapshot=snap), grown)
        # failures: corruption and recoverable OSError fall back to the requested mode
        for db in self.cache_files():
            db.write_bytes(b'not a database' * 100)
        corrupt = fold.read_events()
        self.assertSameEvents(corrupt, grown)
        self.assertNotEqual(list(corrupt), [])
        calls = []
        real = fold._read_events_uncached
        with patch.object(ledger_cache, 'read', side_effect=OSError('read-only root')), \
                patch.object(fold, '_read_events_uncached', lambda w=True: calls.append(w) or real(w)):
            self.assertSameEvents(fold.read_events(), grown)
        self.assertEqual(calls, [False])
        for error in (OSError('x'), ValueError('x'), EOFError(), KeyError('x'), TypeError('x')):
            with patch.object(ledger_cache, 'read_conditional', side_effect=error):
                self.assertSameEvents(fold.read_events(snapshot=snap), grown)
        # LockTimeout still propagates, with or without a snapshot
        with patch.object(ledger_cache, 'read', side_effect=ledger_cache.LockTimeout('busy')):
            with self.assertRaises(ledger_cache.LockTimeout):
                fold.read_events()
        with patch.object(ledger_cache, 'read_conditional', side_effect=ledger_cache.LockTimeout('busy')):
            with self.assertRaises(ledger_cache.LockTimeout):
                fold.read_events(snapshot=snap)
        # no new error type for a clean read after the failures
        self.assertSameEvents(fold.read_events(), grown)


def capture(dest):
    """Copy the live ledger's logical sources into scratch as a verified prefix snapshot.

    Appends are expected (the ledger is live); a rewrite, move or delete of any captured
    byte is churn and fails. Returns {relative name: (size, sha256 of the captured bytes)}.
    """
    src = LIVE_ROOT / 'events'
    if not src.is_dir():
        raise AssertionError('live ledger source unreadable: capture undetermined')
    manifest = {}
    for path in sorted(p for p in src.rglob('*') if p.is_file() and not p.name.endswith('.tmp')):
        rel = path.relative_to(src)
        size = path.stat().st_size
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, left = hashlib.sha256(), size
        with path.open('rb') as fin, target.open('wb') as fout:
            while left:
                chunk = fin.read(min(left, 1 << 22))
                if not chunk:
                    raise AssertionError(f'source shrank during capture: {rel}')
                digest.update(chunk)
                fout.write(chunk)
                left -= len(chunk)
        manifest[str(rel)] = (size, digest.hexdigest())
    if not manifest:
        raise AssertionError('empty capture')
    return manifest


def reverify(manifest):
    """The captured prefix of every live source is byte-identical after the test."""
    src = LIVE_ROOT / 'events'
    bad = []
    for rel, (size, digest) in manifest.items():
        path = src / rel
        try:
            h, left = hashlib.sha256(), size
            with path.open('rb') as fh:
                while left:
                    chunk = fh.read(min(left, 1 << 22))
                    if not chunk:
                        break
                    h.update(chunk)
                    left -= len(chunk)
            if left or h.hexdigest() != digest:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    return bad


def reference_enumeration(events_dir):
    """Independent reference: iter_sources + json.loads + compact + normalization; no skip, no cache."""
    raw = []
    for name, lines in ledger_files.iter_sources(events_dir):
        actor = fold.actor_of(name)
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                e = ledger_compact.compact(json.loads(line))
            except json.JSONDecodeError:
                continue
            e['actor'], e['_src'] = actor, f'{name}:{n}'
            raw.append(e)
    return fold.finish_events(raw, owned=True)


def eligible_sources(events):
    """Independent classification from parsed events (type, actor, subject, corrections)."""
    targeted = {e.get('ref') for e in events if e.get('type') == 'correction' and e['actor'] == 'operator'}
    return {e['_src'] for e in events if e.get('type') == 'tree-reaped' and e['actor'] in fold.EMITS['tree-reaped']
            and not str(e.get('subject', '')).startswith('L-charter-') and e['_src'] not in targeted}


def state_view(events):
    specs, charters, ignored, by_subject = fold.fold(events)
    strip = lambda table: {k: {f: v for f, v in s.items() if f != 'evs'} for k, s in table.items()}
    return strip(specs), strip(charters), ignored, by_subject


def same_modulo_ties(got, expected):
    """Require the reference survivor order, including cross-file timestamp ties."""
    got, expected = list(got), list(expected)
    if got == expected:
        return 0
    assert len(got) == len(expected), (len(got), len(expected))
    assert {e['_src']: e for e in got} == {e['_src']: e for e in expected}, 'survivor dictionaries differ'
    assert [str(e.get('ts', '')) for e in got] == [str(e.get('ts', '')) for e in expected], 'order differs across ts'
    files = {}
    for label, seq in (('got', got), ('expected', expected)):
        for e in seq:
            name, n = e['_src'].rsplit(':', 1)
            files.setdefault((label, name), []).append(int(n))
    for (label, name), lines in list(files.items()):
        if label == 'got':
            other = files[('expected', name)]
            assert lines == other, f'same-file order changed: {name}'
    moved = sum(1 for a, b in zip(got, expected) if a['_src'] != b['_src'])
    assert moved == 0, f'{moved} survivor positions differ from the full-history order'
    return moved


class LiveEquality(Scratch):
    """Shared AC5/AC6 comparison on a frozen scratch copy."""

    def prepare(self):
        self.manifest = capture(self.events)
        self.addCleanup(lambda: self.assertEqual(reverify(self.manifest), [], 'source bytes changed'))

    def compare(self, label, expect_members=False):
        events_dir = self.events
        oracle = fold.read_events(use_cache=False, with_reaped=True)
        enum = reference_enumeration(events_dir)
        self.assertSameEvents(oracle, enum)
        self.assertGreater(len(oracle), 0)
        eligible = eligible_sources(oracle)
        self.assertGreater(len(eligible), 0, 'no skip candidate in the capture: empty coverage')
        others = [e for e in oracle if MARK in json.dumps(e, sort_keys=True) and e['type'] != 'tree-reaped']
        charters = [e for e in oracle if e['type'] == 'tree-reaped' and str(e.get('subject', '')).startswith('L-charter-')]
        expected = self.minus(oracle, eligible)
        subjects = {e.get('subject') for e in oracle}
        print(f'{label} sources={len(self.manifest)} events={len(oracle)} subjects={len(subjects)} '
              f'eligible={len(eligible)} charter-reaps={len(charters)} other-type-mentions={len(others)}')
        inventory = dict(exact_marker=0, charset_escaped=0, alternate_spacing=0,
                         absent_subject=0, charter_reaps=0, project_reaps=0, other_text=0)
        for source_name, lines in ledger_files.iter_sources(self.events):
            for line in lines:
                if 'tree-reaped' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get('type') != 'tree-reaped':
                    inventory['other_text'] += 1
                    continue
                inventory['exact_marker' if MARK in line else 'alternate_spacing'] += 1
                inventory['charset_escaped'] += int('\\u' in line)
                inventory['absent_subject'] += int('subject' not in row)
                inventory['charter_reaps' if str(row.get('subject', '')).startswith('L-charter-') else 'project_reaps'] += 1
        report = dict(label=label, manifest=self.manifest, inventory=inventory,
                      oracle_sources=[e['_src'] for e in oracle], eligible_sources=sorted(eligible),
                      survivor_sources=[e['_src'] for e in expected],
                      subjects={json.dumps(subject): dict(compared=False) for subject in sorted(subjects, key=str)},
                      modes={})
        self.assertEqual(len(report['subjects']), len(subjects), 'subject report keys collide')
        report_path = REPORTS / (label + '-equality.json')
        report_path.write_text(json.dumps(report, indent=2))
        print(f'{label} report: {report_path}', flush=True)
        print(f'{label} raw inventory: {inventory}', flush=True)
        applied = json.dumps(fold.APPLIED, sort_keys=True)
        decoded = []
        real = json.loads
        with patch.object(json, 'loads', lambda t, *a, **k: decoded.append(t) or real(t, *a, **k)):
            runs = {'cold': fold.read_events(), 'warm': fold.read_events(),
                    'uncached': fold.read_events(use_cache=False)}
        decoded_eligible = [t for t in decoded if ledger_cache.reap_candidate(t, 'executor') is not None]
        self.assertEqual(decoded_eligible, [], 'an eligible candidate reached a decoder')
        # Reject a survivor/order discrepancy before the expensive whole-ledger
        # state and board comparisons. An unequal input cannot prove equality.
        for name, got in runs.items():
            same_modulo_ties(got, expected)
        ref_state = state_view(oracle)
        moved = {}
        for name, got in runs.items():
            moved[name] = same_modulo_ties(got, expected)
            self.assertEqual(list(runs['cold']), list(got), f'{name} differs from cold')
            if name == 'warm':
                continue
            self.assertEqual(json.dumps(fold.APPLIED, sort_keys=True), applied, name)
            self.assertEqual(len(got) + sum(fold.skipped_counts(got).values()), len(oracle), name)
            state = state_view(got)
            # Complete board equality is exercised by AC3's frozen fixtures.
            # AC5/AC6 require complete subject states and HEALTH admission:
            # compare the ignored records themselves, including provenance.
            self.assertEqual(state[2], ref_state[2], f'{name}: ignored records / HEALTH')
            for table, expected_table in zip(state[:2], ref_state[:2]):
                self.assertEqual(set(table), set(expected_table), f'{name}: missing subjects')
                for subject, expected_row in expected_table.items():
                    fields = set(table[subject]) | set(expected_row)
                    mismatches = [field for field in fields if table[subject].get(field) != expected_row.get(field)]
                    self.assertEqual(mismatches, [], f'{name}: subject {subject} state fields')
            # every subject, absent entries explicit; retained histories == reference minus eligible
            for subject in sorted(subjects, key=str):
                mine = [e['_src'] for e in state[3].get(subject, [])]
                theirs = [e['_src'] for e in ref_state[3].get(subject, []) if e['_src'] not in eligible]
                self.assertEqual(mine, theirs, f'{name}: subject {subject}')
                report['subjects'][json.dumps(subject)] = dict(compared=True,
                    spec_present=subject in state[0], charter_present=subject in state[1],
                    retained_history=len(mine))
            report['modes'][name] = dict(survivors=len(got), skipped=sum(fold.skipped_counts(got).values()), equal=True)
            report_path.write_text(json.dumps(report, indent=2))
        print(f'{label} tie-run positions reordered by removal (cold/warm/uncached): {moved}')
        # the classification is exactly the exact-marker rule: each eligible id decodes as a project reap
        self.assertEqual(len(others), len([e for e in expected if MARK in json.dumps(e, sort_keys=True) and e['type'] != 'tree-reaped']))
        self.assertEqual(len(charters), len([e for e in expected if e['type'] == 'tree-reaped'
                                             and str(e.get('subject', '')).startswith('L-charter-')]))
        # a committed cache write after a scratch append, read back through a separate connection
        executor = sorted(self.events.glob('L-executor-0*.jsonl'))[0] if not expect_members else self.events / 'L-executor-0001.jsonl'
        with executor.open('a') as fh:
            fh.write(reap(project='appended', ts='2026-11-06T00:00:00+00:00') + '\n')
            fh.write(ev('note', 'appended-note', '2026-11-06T00:00:01+00:00') + '\n')
        after = fold.read_events()
        full_after = self.full()
        same_modulo_ties(after, self.minus(full_after, eligible_sources(full_after)))
        (db_path,) = self.cache_files()
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
            persisted = []
            for data, sha in db.execute('SELECT data,sha FROM chunks'):
                persisted.extend(ledger_cache._unpack(data, sha))
            for tail, sha in db.execute('SELECT tail,tail_sha FROM files'):
                persisted.extend(ledger_cache._unpack(tail, sha))
        persisted_eligible = eligible_sources(persisted)
        self.assertFalse([e for e in persisted if e['_src'] in persisted_eligible
                          and e.get('type') == 'tree-reaped' and e['actor'] in ('executor', 'operator')
                          and not str(e.get('subject', '')).startswith('L-charter-')])
        self.assertGreater(len(persisted), 0)
        report['source_manifest_failures'] = reverify(self.manifest)
        self.assertEqual(report['source_manifest_failures'], [])
        report['append_committed_rows'] = len(persisted)
        report['append_survivors_equal'] = True
        report_path.write_text(json.dumps(report, indent=2))


class TestLiveCopy(LiveEquality):
    def test_live_subject_state_equality(self):
        self.prepare()
        self.compare('AC5')


class TestRolledCopy(LiveEquality):
    def test_live_rolled_member_equality(self):
        self.prepare()
        # Executor history as a virtual rolled member plus a live suffix, split mid-record.
        live = self.events / 'L-executor-0001.jsonl'
        data = live.read_bytes()
        cut = data.index(MARK.encode(), len(data) // 2)       # inside a project reap record
        cut += 20                                              # straddling the record
        member, suffix = data[:cut], data[cut:]
        segments = self.events / ledger_files.SEGMENT_DIR
        segments.mkdir(exist_ok=True)
        segment = segments / 'rolled-test.jsonl'
        segment.write_bytes(member)
        row = dict(name=live.name, offset=0, length=len(member), lines=len(member.splitlines()),
                   sha256=hashlib.sha256(member).hexdigest(), ends_nl=member.endswith(b'\n'))
        (segments / 'rolled-test.idx.json').write_text(json.dumps(dict(
            version=1, segment=segment.name, bytes=len(member), sha256=hashlib.sha256(member).hexdigest(), members=[row])))
        live.write_bytes(suffix)
        self.assertEqual(ledger_files.problems(self.events), [])
        self.assertTrue(ledger_files._members(self.events, live.name))
        self.assertFalse(member.endswith(b'\n'))
        self.compare('AC6', expect_members=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
