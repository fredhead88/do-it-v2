"""Bound repeated ledger text and normalization allocations without losing rows."""
import atexit
import copy
import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

ISOLATION = tempfile.TemporaryDirectory(prefix='ledger-memory-')
atexit.register(ISOLATION.cleanup)
os.environ['HOME'] = str(Path(ISOLATION.name) / 'home')
os.environ['DOIT_ROOT'] = str(Path(ISOLATION.name) / 'root')
import fold
import ledger_cache


def unpack():
    # Each chunk's pickle memo is independent, just as with separate spawn files.
    text = ('repeated evidence ' * 32).encode().decode()
    source = [{'type': 'observed', 'spawn': 'L-research-9901',
               'reading': {'description': text, 'rows': [text]}}]
    return ledger_cache._unpack(*ledger_cache._pack(source))


class ReaderMemoryTests(unittest.TestCase):
    def test_repeated_text_shared_across_chunks_including_nested_evidence(self):
        first, second = unpack(), unpack()
        self.assertEqual(first, second)
        self.assertIs(first[0]['spawn'], second[0]['spawn'])
        self.assertIs(first[0]['reading']['description'], second[0]['reading']['description'])
        self.assertIs(first[0]['reading']['description'], second[0]['reading']['rows'][0])

    def test_mutable_values_remain_private_to_each_read(self):
        first, second = unpack(), unpack()
        first[0]['reading']['rows'].append('caller edit')
        first[0]['reading']['description'] = 'replacement'
        self.assertEqual(second, unpack())
        self.assertNotEqual(first, second)

    def test_parser_read_does_not_allocate_another_event_dictionary_per_row(self):
        count = 8192
        raw = [dict(ts='2026-11-01T00:00:00Z', type='observed', subject='fixture',
                    actor='operator', _src=f'L-operator-fixture.jsonl:{i+1}')
               for i in range(count)]
        # Warm lazy imports outside the measured operation.
        fold.finish_events([])
        with patch.object(fold, '_read_events_uncached', return_value=raw), \
             patch.object(fold, 'PROJECT', None):
            tracemalloc.start()
            try:
                result = fold.read_events(use_cache=False)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(result, raw)
        # Lists of references fit comfortably; a second dict per row does not.
        self.assertLess(peak, count * 160, f'{peak} bytes for {count} events')

    def test_external_normalization_still_copies_and_restores_file_line_order(self):
        rows = [dict(ts='2026-11-01T00:00:00Z', type='observed', subject='z',
                     actor='operator', _src='L-operator-fixture.jsonl:10'),
                dict(ts='2026-11-01T00:00:00Z', type='observed', subject='a',
                     actor='operator', _src='L-operator-fixture.jsonl:9')]
        before = copy.deepcopy(rows)
        result = fold.finish_events(rows)
        self.assertEqual(rows, before)
        self.assertEqual([r['_src'] for r in result], [rows[1]['_src'], rows[0]['_src']])
        result[0]['subject'] = 'caller edit'
        self.assertEqual(rows, before)

    def test_cached_and_reference_read_are_equal_and_caller_mutations_do_not_leak(self):
        with tempfile.TemporaryDirectory(prefix='ledger-memory-cache-') as name:
            root = Path(name)
            events = root / 'events'
            events.mkdir()
            for n in range(2):
                row = dict(ts='2026-11-01T00:00:00Z', type='observed', subject='fixture',
                           reading={'rows': ['same evidence']})
                (events / f'L-operator-{n}.jsonl').write_text(json.dumps(row) + '\n')
            with patch.multiple(fold, ROOT=root, EVENTS=events, PROJECT=None):
                reference = fold.read_events(use_cache=False)
                cached = fold.read_events()
                self.assertEqual(cached, reference)
                cached[0]['reading']['rows'].append('caller edit')
                self.assertEqual(fold.read_events(), reference)


if __name__ == '__main__':
    unittest.main()
