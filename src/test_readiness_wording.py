"""Build readiness stops treating ordinary wording as missing inputs (L-spec-2287)."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_readiness as readiness

SID = 'L-spec-9000'
FILES = {
    'data/seed.csv': 'a,b\n',
    'dashboard/src/components/sales/sales-view.tsx':
        'export function SalesView(props) { return null }\nexport const DailyPulseHeader = () => null\n',
    'services/data_access/catalog.py': 'def check_against_schema(catalog, conn):\n    return []\n',
    'dashboard/src/lib/profit-v2/hooks/use-period-store.ts':
        'export function setStoredPeriodAndNotify(p) {}\n',
    'src/other.py': 'def other(): pass\n',
}


def spec(consumes, produces='- output', requirements=''):
    return (requirements + '\n## Interfaces\n\nConsumes:\n' + consumes + '\n\nProduces:\n' + produces
            + '\n\nWrites:\n- src/other.py\n\n## Verification\n\n```bash\ntrue\n```\n')


class WordingTests(unittest.TestCase):
    def scratch(self, text, others=()):
        allocation = tempfile.TemporaryDirectory(prefix='wording-test-')
        self.addCleanup(allocation.cleanup)
        root = Path(allocation.name)
        for name in ('do-it-v2', 'albert-scott', 'demo'):
            (root / 'repos' / name).mkdir(parents=True)
        repo = root / 'repos' / 'demo'

        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        for path, content in FILES.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content)
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        tree = git('ls-tree', '-r', '--name-only', base).splitlines()
        (root / 'content').mkdir()
        (root / 'content' / (SID + '.md')).write_text(text)
        events = [dict(type='spec-written', subject=SID, project='demo')]
        for sid, body in others:
            (root / 'content' / (sid + '.md')).write_text(body)
            events.append(dict(type='spec-written', subject=sid, project='demo'))
        return root, repo, base, tree, events

    def missing(self, text):
        _, repo, base, tree, _ = self.scratch(text)
        return readiness.missing_inputs(text, repo, base, tree)

    def check(self, text, others=()):
        root, _, base, _, events = self.scratch(text, others)
        with patch.object(readiness.grading_env, '_project_row', return_value={}):
            return readiness.check(SID, 'demo', base, events, root=root, dry_run=False)

    def test_ac1_existing_phrase_only_counts_under_input_headings(self):
        sentence = 'The existing-function test_edit and existing-file plan_batches stay unchanged.'.replace(
            'existing-function', 'existing function').replace('existing-file', 'existing file')
        for heading in ('## Requirements[]', '## Acceptance criteria', '## Constraints'):
            text = heading + '\n\n- ' + sentence + '\n\n## Interfaces\n\nConsumes:\n- `src/other.py`\n'
            self.assertNotIn('- ' + sentence, readiness.inputs(text), heading)
        text = '## Interfaces\n\nConsumes:\n- ' + sentence + '\n'
        self.assertIn('- ' + sentence, readiness.inputs(text))
        self.assertIn('- ' + sentence, readiness.inputs('## Reads\n- ' + sentence))
        self.assertEqual(readiness.inputs('## Requirements[]\n- Already exists: `ghost.py`\n'), [])

    def test_ac1_builtin_and_prose_slash_are_not_inputs(self):
        number = spec('- The existing table\'s partial check is `Number(...) < 1`.')
        self.assertEqual(self.missing(number), [])
        prose = spec('- Existing `usePromosCostSplitQuery`, `SalesGrid` and `SalesView` data/props '
                     'signatures stay unchanged.')
        self.assertEqual(self.missing(prose), [])
        self.assertEqual(self.missing(spec('- `data/props`')), [('path', 'data/props')])
        self.assertEqual(self.missing(spec('- data/props.py')), [('path', 'data/props.py')])
        self.assertEqual(self.missing(spec('- data/a/b')), [('path', 'data/a/b')])

    def test_ac2_symbols_resolve_tree_wide(self):
        lines = [
            '- `daysBetweenInclusive`; `SalesView({ clientName, portalParams }: SalesViewProps)` uses it and '
            '`DailyPulseHeader()` renders in `dashboard/src/lib/format.ts`.',
            '- `check_against_schema(catalog, conn) -> list[Problem]` from `services/other/wrong.py`.',
            '- `setStoredPeriodAndNotify(p: StoredPeriod): void` (`use-period-store.ts`).',
        ]
        for line in lines:
            got = self.missing(spec(line))
            self.assertNotIn(('symbol', 'SalesView'), got, line)
            self.assertNotIn(('symbol', 'DailyPulseHeader'), got, line)
            self.assertNotIn(('symbol', 'check_against_schema'), got, line)
            self.assertNotIn(('symbol', 'setStoredPeriodAndNotify'), got, line)
        self.assertEqual(self.missing(spec(lines[2])), [])

    def test_ac2_produced_symbol_is_never_missing(self):
        text = spec('- `new_helper(x)` is called by `mod.other_new(y)`.', produces='- `new_helper(x)`\n- `mod.other_new(y)`')
        self.assertEqual(self.missing(text), [])

    def test_ac2_explicit_existing_path_stays_strict(self):
        text = spec('- `services/data_access/catalog.py::SalesView`')
        self.assertEqual(self.missing(text), [('symbol', 'SalesView')])

    def test_ac3_real_misses_still_fail(self):
        text = spec('- `ghost_helper(x)`')
        result = self.check(text)
        self.assertEqual(result['status'], 'bounced')
        self.assertTrue(result['findings'][0].startswith('missing inputs without a unique producer'))
        other = '## Interfaces\n\nProduces:\n- `ghost_helper(x)`\n\nWrites:\n- src/ghost.py\n'
        result = self.check(text, others=[('L-spec-9001', other)])
        self.assertEqual((result['status'], result['waits_for']), ('held', 'L-spec-9001'))
        self.assertEqual(self.missing(spec('- `nowhere/absent.py`')), [('path', 'nowhere/absent.py')])


if __name__ == '__main__':
    unittest.main()
