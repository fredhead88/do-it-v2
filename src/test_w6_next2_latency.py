"""A current-cache scan validates segment indexes once, not once per file."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_ISOLATION = tempfile.TemporaryDirectory(prefix='w6-next2-latency-')
atexit.register(_ISOLATION.cleanup)
os.environ['DOIT_ROOT'] = _ISOLATION.name
import fold
import ledger_cache
import ledger_files


class MetadataScanTests(unittest.TestCase):
    def test_text_sharing_traverses_containers_without_recursing_into_scalar_leaves(self):
        value = [{'future_audit': {'numbers': list(range(4096)),
                                  'flags': [None, True, False],
                                  'text': ['shared evidence', 'shared evidence']}}]
        with patch.object(ledger_cache, '_shared_text', wraps=ledger_cache._shared_text) as visits:
            result = ledger_cache._shared_text(value)
        self.assertEqual(result, value)
        self.assertLessEqual(visits.call_count, 6,
                            'numeric audit arrays need container visits, not one call per scalar')
        self.assertIs(result[0]['future_audit']['text'][0], result[0]['future_audit']['text'][1])

    def test_current_read_scans_indexes_once_for_live_and_sealed_files(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                events = root / 'events'
                events.mkdir()
                data, members = b'', []
                for index in range(48):
                    name = f'L-operator-{index:04}.jsonl'
                    row = dict(ts='2026-11-01T00:00:00Z', type='observed',
                               subject=f'L-spec-{index:04}', future_field={'audit': [index, 'é']})
                    content = (json.dumps(row) + '\n').encode()
                    if sealed:
                        members.append(dict(name=name, offset=len(data), length=len(content),
                            lines=1, sha256=hashlib.sha256(content).hexdigest(), ends_nl=True))
                        data += content
                    else:
                        (events / name).write_bytes(content)
                if sealed:
                    segments = events / ledger_files.SEGMENT_DIR
                    segments.mkdir()
                    (segments / 'fixture.jsonl').write_bytes(data)
                    (segments / 'fixture.idx.json').write_text(json.dumps(dict(version=1,
                        segment='fixture.jsonl', bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(), members=members)))
                with patch.multiple(fold, ROOT=root, EVENTS=events, PROJECT=None):
                    reference = fold.read_events(use_cache=False)
                    self.assertEqual(fold.read_events(), reference)
                    with patch.object(ledger_files, '_indexes', wraps=ledger_files._indexes) as scans:
                        self.assertEqual(fold.read_events(), reference)
                    self.assertLessEqual(scans.call_count, 1,
                        'current metadata must not enumerate/validate all indexes per member')
                    # A recreated live suffix still invalidates the generation and
                    # retains every old sealed event plus the new audit field.
                    with (events / 'L-operator-0000.jsonl').open('ab') as out:
                        out.write(b'{"type":"observed","subject":"tail","new_field":[1,2]}\n')
                    self.assertEqual(fold.read_events(), fold.read_events(use_cache=False))


if __name__ == '__main__':
    unittest.main()
