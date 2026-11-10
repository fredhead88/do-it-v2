"""Synthetic, effect-free round vocabulary and parser regression fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import rounds


def criteria(count):
    return '\n'.join(f'AC{i} [backend]: criterion {i}' for i in range(1, count + 1))


def spec(paths=0, count=0):
    grant = 'Writes:\n' + '\n'.join(f'- src/file{i}.py' for i in range(paths)) if paths else ''
    return grant + '\n\n## Acceptance criteria\n' + criteria(count)


class VocabularyTests(unittest.TestCase):
    def test_constants_and_value_shapes(self):
        for name, value in [('CONTINUE', 'CONTINUE'), ('MAX_ROUNDS', 4),
                            ('SIZE_LINE_FILES', 15), ('SIZE_LINE_CRITERIA', 8)]:
            self.assertIs(type(getattr(rounds, name)), type(value))
            self.assertEqual(getattr(rounds, name), value)
        self.assertEqual(rounds.Measure(2, 3).writes_files, 2)
        self.assertEqual(rounds.Measure(2, 3).criteria, 3)
        self.assertEqual(rounds.Split('builder', ()).source, 'builder')
        self.assertEqual(rounds.Split('rounds-line', ('AC1',)).round_one, ('AC1',))

    def test_calls_repeat_and_preserve_inputs(self):
        text = spec(15, 9)
        events = [{'type': 'build-done', 'subject': 's', 'status': 'CONTINUE',
                   'nested': {'items': [1, 2]}}]
        original = deepcopy((text, events))
        for _ in range(2):
            self.assertEqual(rounds.measure(text), rounds.Measure(15, 9))
            self.assertEqual(rounds.planned_rounds(text), 2)
            self.assertEqual(rounds.round_split(text), rounds.Split('builder', ()))
            self.assertEqual(rounds.round_of(events, 's'), 2)
            self.assertEqual((text, events), original)

    def test_isolated_import_and_calls_without_effects(self):
        # Preload interpreter dependencies before denying application effects.
        program = r'''
import builtins, collections, io, os, pathlib, re, socket, subprocess, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
def denied(*args, **kwargs):
    raise AssertionError('application effect attempted')
before = set(sys.modules)
with patch('builtins.open', denied), patch('io.open', denied), \
     patch('os.open', denied), patch('os.getenv', denied), \
     patch.object(os, 'environ', {}), patch('socket.socket', denied), \
     patch('socket.create_connection', denied), patch('subprocess.Popen', denied), \
     patch('os.system', denied), patch('time.time', denied), \
     patch('time.monotonic', denied), patch('time.perf_counter', denied):
    import rounds
    text = 'Writes: src/a.py\n\n## Acceptance\nAC1 [backend]: [foundation] x'
    assert rounds.measure(text) == rounds.Measure(1, 1)
    assert rounds.planned_rounds(text) == 1
    assert rounds.round_split(text) == rounds.Split('foundation-label', ('AC1',))
    assert rounds.round_of([], 's') == 1
assert not ({'packet', 'fold', 'dispatch', 'merge_gate'} & (set(sys.modules) - before))
assert set(sys.modules) - before == {'rounds'}, set(sys.modules) - before
'''
        result = subprocess.run([sys.executable, '-B', '-c', program,
                                 str(Path(__file__).resolve().parent)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class MeasureTests(unittest.TestCase):
    def test_path_syntax_and_precedence(self):
        fixtures = [
            ('Writes:\n- src/a.py\n- src/b.py', 2),
            ('**Writes:**\n\n- `src/a.py`\n- src/*.py', 2),
            ('**Writes:**\n```text\nsrc/a.py\nsrc/b.py\n```', 2),
            ('Writes: src/a.py, src/b.py src/c.py', 3),
            ('---\nwrites:\n  - src/a.py\n  - src/*.py\n---\n', 2),
            ('---\nwrites: [src/a.py, src/b.py]\n---\n', 2),
            ('---\nwrites:\n  - "src/a.py"\n  - \'src/b.py\'\n---\n', 2),
            ('Footprint:\n- src/a.py', 1),
            ('Footprint: src/a.py src/b.py\nWrites: src/c.py', 1),
            ('---\nwrites:\n- src/a.py\n---\nWrites: src/b.py src/c.py\nFootprint: src/d.py', 1),
            ('---\nproject: demo\n---\nWrites: src/a.py', 1),
            ('No path declaration.', 0),
            ('```md\nWrites: src/example.py\nAC99 [backend]: false\n```', 0),
            ('Writes:\n- src/a.py\n\nprose after block', 1),
            ('Writes:\n- src/a.py\n## Next\nprose', 1),
            ('## Footprint\n- src/a.py', 1),
        ]
        for text, expected in fixtures:
            with self.subTest(text=text):
                self.assertEqual(rounds.measure(text), rounds.Measure(expected, 0))
                self.assertEqual(rounds.planned_rounds(text), 1)

    def test_malformed_selected_grants_refuse_fallback(self):
        fixtures = ['Writes:', 'Writes: see the table below', 'Writes: src/a.py prose',
                    'Writes:\n- not_a_path', 'Footprint: prose',
                    'Writes:\n\n## Next\nFootprint: src/a.py']
        for value in ['src/a.py', '{path: src/a.py}', 'null', '[]',
                      '[src/a.py, prose]', '\n- src/a.py\n- prose',
                      '\n  path: src/a.py', '\n- null']:
            fixtures.append(f'---\nwrites: {value}\n---\nWrites: src/valid.py\nFootprint: src/other.py')
        for text in fixtures:
            for function in (rounds.measure, rounds.planned_rounds):
                with self.subTest(text=text, function=function.__name__):
                    with self.assertRaises(ValueError):
                        function(text)

    def test_criterion_selection_inventory(self):
        fixtures = [
            ('## Acceptance\n' + criteria(8) + '\n## Other\nAC99 [backend]: extra', tuple(f'AC{i}' for i in range(1, 9))),
            ('## Acceptance\n' + criteria(8) + '\nAC1 [backend]: duplicate', tuple(f'AC{i}' for i in range(1, 9))),
            ('## Acceptance\n' + criteria(9), tuple(f'AC{i}' for i in range(1, 10))),
            ('## Acceptance\nNo criteria here.\n## Other\n' + criteria(8), tuple(f'AC{i}' for i in range(1, 9))),
            ('A prose mention of AC1 [backend] is not an anchor.', ()),
            ('## Acceptance\n- **AC1** [backend]: one\n2. AC2 [backend]: two\n+ AC3 [backend]: three\n```\nAC99 [backend]: example\n```', ('AC1', 'AC2', 'AC3')),
            ('- AC99 [backend]: outside Acceptance\n**AC1** [backend]: margin', ('AC1',)),
            ('~~~md\nAC99 [backend]: example\n~~~\nAC1 [backend]: real', ('AC1',)),
        ]
        for text, ids in fixtures:
            with self.subTest(ids=ids):
                print('selected IDs:', json.dumps(ids))
                self.assertEqual(rounds.measure(text), rounds.Measure(0, len(ids)))
                self.assertEqual(rounds.planned_rounds(text), 2 if len(ids) > 8 else 1)
                # Explicit references use exactly the measurement inventory.
                if ids:
                    split = 'Rounds: 1=' + ','.join(ids) + '; 2=rest\n'
                    self.assertEqual(rounds.round_split(split + text).round_one, ids)

    def test_size_matrix(self):
        for paths, count, planned in [(0, 0, 1), (14, 8, 1), (15, 8, 2),
                                      (16, 1, 2), (1, 9, 2), (15, 9, 2)]:
            with self.subTest(paths=paths, criteria=count):
                text = spec(paths, count)
                self.assertEqual(rounds.measure(text), rounds.Measure(paths, count))
                self.assertEqual(rounds.planned_rounds(text), planned)


class SplitTests(unittest.TestCase):
    def test_explicit_split_precedence_and_order(self):
        body = '## Acceptance\n' + criteria(4).replace('criterion 3', '[foundation] criterion 3')
        for line in ['Rounds: 1=AC1,AC2,AC4; 2=rest',
                     'Rounds: 1 = AC1, AC2 , AC1, AC4 ; 2 = rest']:
            self.assertEqual(rounds.round_split(line + '\n' + body),
                             rounds.Split('rounds-line', ('AC1', 'AC2', 'AC4')))

    def test_foundation_anchor_only(self):
        text = '''## Acceptance
AC1 [backend] [foundation]: one
AC2 [backend]: two
Continuation [foundation] prose does not label AC2.
AC3 [backend]: [foundation] three
AC3 [backend]: duplicate [foundation]
```
AC4 [backend]: [foundation] example
```
## Other
AC99 [backend]: [foundation] outside selection
'''
        self.assertEqual(rounds.round_split(text),
                         rounds.Split('foundation-label', ('AC1', 'AC3')))

    def test_builder_fallback_and_fenced_examples(self):
        for text in ['', criteria(2), 'Mention [foundation] in prose.\n' + criteria(1),
                     '```\nRounds: 1=AC99; 2=rest\n```\n' + criteria(1)]:
            self.assertEqual(rounds.round_split(text), rounds.Split('builder', ()))

    def test_invalid_explicit_split_does_not_fall_through(self):
        for line in ['Rounds:', 'Rounds: 1=AC99; 2=rest', 'Rounds: 1=AC1',
                     'Rounds: 1=AC1,; 2=rest', 'Rounds: 1=; 2=rest',
                     'Rounds: 2=rest; 1=AC1', 'Rounds: 1=AC1; 2=AC2',
                     'Rounds: 1=AC1; 2=rest trailing prose']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                rounds.round_split(line + '\nAC1 [backend]: [foundation] one')
        with self.assertRaises(ValueError):
            rounds.round_split('Rounds: 1=AC99; 2=rest\n## Acceptance\n' +
                               criteria(1) + '\n## Other\nAC99 [backend]: extra')


def event(kind, subject='s', **values):
    return dict(type=kind, subject=subject, **values)


class RoundOfTests(unittest.TestCase):
    def test_continue_sequence_and_continuation_starts(self):
        history = []
        self.assertEqual(rounds.round_of(history, 's'), 1)
        history.append(event('build-started'))
        self.assertEqual(rounds.round_of(history, 's'), 1)
        for expected in range(2, 6):
            history.append(event('build-done', status='CONTINUE', round=99))
            self.assertEqual(rounds.round_of(history, 's'), expected)
            history.append(event('build-started', continuation=True))
            self.assertEqual(rounds.round_of(history, 's'), expected)
        self.assertGreater(rounds.round_of(history, 's'), rounds.MAX_ROUNDS)

    def test_fresh_starts_reset_and_other_events_are_isolated(self):
        for fresh in [{}, {'continuation': False}]:
            history = [event('build-done', status='CONTINUE'),
                       event('build-started', **fresh),
                       event('build-done', subject='other', status='CONTINUE'),
                       event('build-started', subject='other'),
                       {'type': 'irrelevant'}, {}, event('note', status='CONTINUE')]
            self.assertEqual(rounds.round_of(history, 's'), 1)

    def test_legacy_stream_and_caller_order_without_mutation(self):
        history = [event('build-started', continuation=True, ts='same'),
                   event('build-done', status='CONTINUE', ts='same'),
                   event('build-done', status='CONTINUE', ts='same')]
        original = deepcopy(history)
        self.assertEqual(rounds.round_of(history, 's'), 3)
        self.assertEqual(history, original)
        reset = event('build-started', ts='same')
        self.assertEqual(rounds.round_of(history + [reset], 's'), 1)
        self.assertEqual(rounds.round_of([reset] + history, 's'), 3)

    def test_non_continue_handbacks_do_not_reset_without_fresh_start(self):
        history = [event('build-started')]
        for expected, status in enumerate(['CONTINUE', 'BLOCKED', 'DONE', 'NEEDS_CONTEXT'], 2):
            history.append(event('build-done', status=status))
            self.assertEqual(rounds.round_of(history, 's'), expected)

    def test_exhaustion_uses_build_ordinals_for_every_status(self):
        for status in ['BLOCKED', 'DONE', 'NEEDS_CONTEXT']:
            with self.subTest(status=status):
                history = [event('build-started', spawn='exhausted')]
                history.extend([
                    event('build-done', status=status, spawn='exhausted',
                          retained_commit=True),
                    event('build-declaration', spawn='exhausted', term='context-exhausted', admitted=True),
                    event('build-started', continuation=True),
                ])
                self.assertEqual(rounds.round_of(history, 's'), 2)
                for expected in range(3, 7):
                    history.append(event('build-done', status='CONTINUE'))
                    self.assertEqual(rounds.round_of(history, 's'), expected)
                    history.append(event('build-started', continuation=True))
                    self.assertEqual(rounds.round_of(history, 's'), expected)
                    # The routing consumer must re-cut when the ordinal exceeds 4.
                    self.assertEqual(expected > rounds.MAX_ROUNDS, expected >= 5)


if __name__ == '__main__':
    unittest.main()
