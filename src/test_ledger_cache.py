"""Cached/reference byte equivalence, incremental parsing, and shared cache safety."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Set before importing fold; tests never inspect the operator's root.
ROOT = Path(tempfile.mkdtemp(prefix='ledger-cache-test-'))
os.environ['DOIT_ROOT'] = str(ROOT)
import fold
import ledger_cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = self.root / 'events'
        self.events.mkdir()
        self.patch = patch.multiple(fold, ROOT=self.root, EVENTS=self.events, PROJECT=None)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, rows, mode='w'):
        with (self.events / name).open(mode) as stream:
            stream.write(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in rows))

    def same(self):
        expected = fold.read_events(use_cache=False)
        applied = json.dumps(fold.APPLIED)
        actual = fold.read_events()
        self.assertEqual(json.dumps(actual, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(json.dumps(fold.APPLIED), applied)
        return actual

    def test_reference_equivalence_through_repairs_and_tails(self):
        a, b = 'L-builder-01.jsonl', 'L-executor-01.jsonl'
        def event(ty, **kw):
            return dict(ts='2026-01-01', type=ty, subject='L-spec-01', project='wrong', **kw)
        self.write(a, [event('build-started'), event('build-done', detail={'x': ['é', 1, None]})])
        self.write(b, [event('deploy-refused'), event('deploy-started')])
        self.write('L-operator-01.jsonl', [dict(ts='2026-01-02', type='correction', subject='fix',
                    ref=a + ':1', set={'project': 'right', 'actor': 'forged', '_src': 'forged'})])
        self.same(); self.same()
        with patch.object(fold, 'PROJECT', 'right'):
            self.assertEqual([e['actor'] for e in self.same()], ['builder'])
        # An old timestamp append still goes through the original global merge.
        self.write(a, [dict(event('build-blocked'), ts='2025-12-31')], 'a')
        self.same()
        # Rewrite and GROW the same inode: a metadata-only suffix cache misses this.
        self.write(a, [event('different', data='changed prefix' * 30)])
        self.same()
        self.write(a, [event('short')])  # truncate
        self.same()
        replacement = self.events / 'replacement'
        replacement.write_text((self.events / a).read_text().replace('short', 'other'))
        replacement.replace(self.events / a)
        self.same()
        (self.events / b).unlink()
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('\ninvalid\r\n' + json.dumps(event('valid-tail')))
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('garbage')  # previously valid unterminated row becomes invalid
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('\n{"ts":"broken')
        self.same()
        with (self.events / a).open('a') as fh:
            fh.write('","type":"completed","subject":"tail"}\n')
        self.same()
        cache = next((self.root / 'cache').glob('*.pickle'))
        cache.write_bytes(b'corrupt cache')
        self.same()
        for cache in (self.root / 'cache').glob('*.pickle'):
            data = bytearray(cache.read_bytes()); data[-5] ^= 1; cache.write_bytes(data)
        self.same()

    def test_only_suffix_parsed_and_caller_mutation_not_saved(self):
        name = 'L-operator-01.jsonl'
        self.write(name, [dict(ts='1', type='one', subject='test')])
        first = self.same()
        first[0]['type'] = 'caller mutation'
        with patch.object(ledger_cache, '_parse', wraps=ledger_cache._parse) as parse:
            self.same()
            self.assertEqual(parse.call_count, 0)
            self.write(name, [dict(ts='2', type='two', subject='test')], 'a')
            self.same()
            self.assertEqual(parse.call_count, 2)  # complete suffix + empty tail
            self.assertNotIn(b'"one"', b''.join(c.args[0] for c in parse.call_args_list))

    def test_blank_crlf_and_unicode_line_numbers(self):
        (self.events / 'L-operator-01.jsonl').write_bytes(
            b'\r\n\n' + json.dumps(dict(type='first', subject='x', ts='1')).encode() + b'\r\n'
            + '\u2028\r'.encode() + json.dumps(dict(type='last', subject='x', ts='2')).encode())
        self.same()
        with (self.events / 'L-operator-01.jsonl').open('ab') as fh:
            fh.write(b'\r\n\n')
        self.same()

    def test_unwritable_cache_falls_back(self):
        self.write('L-operator-01.jsonl', [dict(ts='1', type='test')])
        with patch.object(ledger_cache, '_save', side_effect=PermissionError('read-only')):
            self.same()

    def test_fresh_processes_and_concurrent_readers(self):
        self.write('L-operator-01.jsonl', [dict(ts='1', type='test')])
        env = {**os.environ, 'DOIT_ROOT': str(self.root), 'PYTHONPATH': str(Path(__file__).parent)}
        script = 'import fold,json; print(json.dumps(fold.read_events()))'
        procs = [subprocess.Popen([sys.executable, '-c', script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(4)]
        results = [p.communicate(timeout=30) for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), results)
        self.assertTrue(all(out == results[0][0] for out, err in results))
        self.assertEqual(json.loads(results[0][0]), self.same())


if __name__ == '__main__':
    unittest.main()
