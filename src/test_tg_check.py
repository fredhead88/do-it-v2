"""Synthetic caller/evidence inventories. Never contact Telegram, gh or ssh."""
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tg_check as tg

REPO = Path(__file__).resolve().parents[1]
COLLECT = tg.collect
OUTBOUND = 'curl "https://api.telegram.org/bot${token}/sendMessage"\n'
POLL = '''import requests
def api(tok, method):
    return requests.get(f'https://api.telegram.org/bot{tok}/{method}')
api(tok, 'getUpdates')
api(tok, 'getFile')
requests.get(f'https://api.telegram.org/file/bot{tok}/path')
'''


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tg-check-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.doit, self.albert, self.root, self.push = [self.base / p for p in ('doit', 'albert', 'root', 'push')]
        for directory in (self.doit, self.albert, self.root / 'bin', self.root / 'telegram', self.push):
            directory.mkdir(parents=True)
        self.put(self.doit / 'scripts/tg-send.sh', OUTBOUND)
        (self.root / 'bin/tg-send.sh').symlink_to(self.doit / 'scripts/tg-send.sh')
        self.addCleanup(patch.stopall)
        patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')).start()
        self.proc = patch.object(tg, 'collect', side_effect=AssertionError('external query forbidden')).start()

    def put(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def scan(self, **kwargs):
        scan = tg.Scan(self.doit, self.albert, self.root, self.push, **kwargs)
        ok = scan.run()
        return ok, '\n'.join(sorted(scan.rows))

    def check(self, name, path, text, expected):
        print(type(self).__name__, name, path.relative_to(self.base), 'expected', expected)
        self.put(path, text)
        ok, report = self.scan()
        self.assertEqual(ok, expected, report)
        if not expected:
            self.assertIn(str(path), report)
        path.unlink()

    def args(self):
        return ['--doit-repo', str(self.doit), '--albert-repo', str(self.albert),
                '--root', str(self.root), '--push-dir', str(self.push)]

    def event(self, **changes):
        row = dict(type='decision', subject='L-charter-0085', ts='2026-01-01T00:00:00Z',
                   why=dict(resource='telegram-alerter', state='not-deployed',
                            observed_at='2025-12-31T00:00:00Z', evidence_ref='https://example.org/evidence'))
        row.update(changes)
        return row

    def ledger(self, rows, name='L-thinker-0001.jsonl'):
        return self.put(self.root / 'events' / name, ''.join(json.dumps(row) + '\n' for row in rows))

    def cli(self, *extra):
        absent_caches = [r / 'cache' for r in (self.root, self.base / 'decoy')
                         if not (r / 'cache').exists()]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tg.main(self.args() + list(extra))
        for cache in absent_caches:
            self.assertFalse(cache.exists(), str(cache))
        return code, out.getvalue()


class AC1(Fixture):
    def test_diagnostic_literals_are_not_sending_calls(self):
        self.check('diagnostic literal', self.doit / 'src/diagnostic.py',
                   "MESSAGE = 'https://api.telegram.org/botx/sendMessage'\nprint('sendMessage is forbidden')", True)
        self.check('URL bound to actual HTTP call', self.albert / 'caller.py',
                   "import requests\nURL = 'https://api.telegram.org/botx/sendMessage'\nrequests.post(URL)", False)
        self.check('checker inspects its own source', self.doit / 'src/tg_check.py',
                   (REPO / 'src/tg_check.py').read_text(), True)

    def test_seven_source_paths_and_poller_scope(self):
        archive = self.put(self.doit / 'archive/old-sender.backup', OUTBOUND)
        archive.chmod(0o700)
        self.assertFalse(self.scan()[0])
        archive.unlink()
        print('AC1 archived executable with nonstandard extension inspected')
        for name in ('scripts/invariant_check.py', 'scripts/pooler_health.py',
                     'scripts/amazon/preflight.py', 'src/amazon_intel/db.py',
                     '.github/workflows/oasdiff.yml', 'infra/lambda/telegram-alerter/handler.py',
                     'archive/soda_scan.py'):
            path = self.albert / name
            self.check('seven-source inventory', path,
                       OUTBOUND if path.suffix == '.yml' else
                       "import requests\nrequests.post('https://api.telegram.org/botx/sendMessage')",
                       name in tg.OPEN)
        for extra in ("\nrequests.get(f'https://api.telegram.org/bot{tok}/{other}')",
                      "\nrequests.get(f'https://api.telegram.org/bot{tok}/getMe')"):
            self.check('poller extra unresolved endpoint', self.root / 'telegram/tg-poll.py', POLL + extra, False)
        self.check('uninvoked poller helper', self.root / 'telegram/tg-poll.py',
                   POLL.split("api(tok, 'getUpdates')")[0], False)

    def test_direct_surface_inventory(self):
        for method in ('sendMessage', 'sendDocument'):
            for directory in (self.doit, self.albert):
                for sub, suffix in (('archive/sender', '.sh'), ('scripts/contest-helper', '.py'),
                                    ('.github/workflows/new-alert', '.yml')):
                    path = directory / (sub + suffix)
                    body = ('import urllib.request\nurllib.request.urlopen("https://api.telegram.org/botx/' + method + '")'
                            if suffix == '.py' else OUTBOUND.replace('sendMessage', method))
                    self.check(method, path, body, False)
        self.check('second sender basename', self.doit / 'archive/tg-send.sh', OUTBOUND, False)
        for sub in ('docs/readme.md', 'tests/fixture.py', 'src/test_example.py'):
            self.check('excluded source-shaped text', self.doit / sub, OUTBOUND, True)
        for sub in tg.OPEN:
            path = self.albert / sub
            body = 'import requests\nrequests.post("https://api.telegram.org/botx/sendMessage")' if path.suffix == '.py' else OUTBOUND
            self.check('exact pending off-box route', path, body, True)
            self.check('pending path extra method', path, body.replace('sendMessage', 'sendDocument'), False)
        self.assertIn('OPEN', self.scan()[1])

    def test_poller_identity_and_method_inventory(self):
        for path in (self.root / 'telegram/tg-poll.py', self.doit / 'scripts/tg-poll.py'):
            self.check('literal inbound and file download', path, POLL, True)
            for method in ("'sendMessage'", "'getChat'", "'getMe'", 'method', "'get' + suffix", "['getUpdates']"):
                self.check('forbidden or unresolved ' + method, path, POLL + '\napi(tok, ' + method + ')', False)
        versioned = self.put(self.doit / 'scripts/tg-poll.py', POLL)
        live = self.root / 'telegram/tg-poll.py'
        live.symlink_to(versioned)
        self.assertTrue(self.scan()[0])
        live.unlink(); versioned.unlink()
        for path in (self.albert / 'scripts/tg-poll.py', self.root / 'bin/tg-poll.py', self.doit / 'archive/tg-poll.py'):
            self.check('wrong poller identity', path, POLL, False)


class AC2(Fixture):
    def test_wrappers_and_assignment_order(self):
        sender = str(self.root / 'bin/tg-send.sh')
        wrapper = self.put(self.root / 'bin/document.sh',
                           'TG_SENDER="$TG_SENDER" ' + sender + ' --document "$1" "$2"\n')
        for name, prefix, expected in (('forwarded document', 'TG_SENDER=health ', True),
                                       ('missing upstream identity', '', False)):
            self.check(name, self.push / 'caller.sh', prefix + str(wrapper) + ' file text', expected)
        wrapper.write_text('TG_SENDER="$TG_SENDER" TG_CLASS=reply TG_REPLY_TO="$TG_REPLY_TO" ' + sender + ' "$@"\n')
        for prefix, expected in (('TG_SENDER=poll TG_CLASS=reply TG_REPLY_TO=42 ', True),
                                 ('TG_SENDER=poll TG_CLASS=reply ', False)):
            self.check('reply wrapper forwarding', self.push / 'caller.sh', prefix + str(wrapper) + ' text', expected)
        wrapper.unlink()
        for text in ("from tg_say import deliver\ndeliver(sender, 'text', label='x')\nsender='late'",
                     "from tg_say import deliver\nsender='first'\ndeliver(sender, 'text', label='x')\nsender=dynamic\ndeliver(sender, 'text', label='x')"):
            self.check('assignment order cannot bless another call', self.root / 'caller.py', text, False)
        self.check('conditional export cannot bless call', self.push / 'caller.sh',
                   'if false; then\nexport TG_SENDER=wrong\nfi\n' + sender + ' text', False)
        self.check('parameter shadowing cannot bless call', self.root / 'caller.py',
                   "from tg_say import deliver\nsender='unrelated'\ndef wrapper(sender):\n    deliver(sender, 'text', label='x')", False)
        self.check('aliased subprocess missing sender', self.root / 'caller.py',
                   'from subprocess import run as execute\nexecute([' + repr(sender) + ", 'text'])", False)

    def test_call_specific_sender_inventory(self):
        sender = str(self.root / 'bin/tg-send.sh')
        cases = [
            ('shell named', 'TG_SENDER=health ' + sender + ' text', True),
            ('shell missing', sender + ' text', False),
            ('unrelated literal', 'echo TG_SENDER=health\n' + sender + ' text', False),
            ('mixed calls', 'TG_SENDER=health ' + sender + ' text\n' + sender + ' text', False),
            ('dynamic sender', 'TG_SENDER="$runtime" ' + sender + ' text', False),
            ('reply metadata', 'TG_SENDER=poll TG_CLASS=reply TG_REPLY_TO=12 ' + sender + ' text', True),
            ('reply missing id', 'TG_SENDER=poll TG_CLASS=reply ' + sender + ' text', False),
            ('named CLI', 'doit tg-say --sender health --label label', True),
            ('missing CLI', 'doit tg-say --label label', False),
            ('document', 'TG_SENDER=health ' + sender + ' --document result.pdf text', True)]
        for name, body, ok in cases:
            self.check(name, self.push / 'caller.sh', body, ok)
        for env, ok in (("{'TG_SENDER':'health'}", True), ('{}', False)):
            body = 'import subprocess\nsubprocess.run([' + repr(sender) + ", 'text'], env=" + env + ')'
            self.check('subprocess explicit environment ' + str(ok), self.root / 'caller.py', body, ok)
        for args, ok in (("'health', 'text'", True), ("sender, 'text'", False), ("text='text'", False)):
            self.check('Python deliver ' + str(ok), self.root / 'caller.py',
                       'from tg_say import deliver\ndeliver(' + args + ", label='label')", ok)


class AC3(Fixture):
    def test_cycles_and_real_default_hop_boundary(self):
        first = self.put(self.root / 'bin/first.sh', str(self.root / 'bin/second.sh'))
        self.put(self.root / 'bin/second.sh', str(first))
        self.assertFalse(self.scan()[0])
        print('AC3 mutually recursive wrapper routes fail')
        target = self.doit / 'scripts/tg-send.sh'
        for i in range(17):
            link = self.root / 'bin' / ('hop-%02d.sh' % i)
            link.symlink_to(target)
            target = link
            scan = tg.Scan(self.doit, self.albert, self.root, self.push)
            if i < 16:
                self.assertEqual(scan.target(link), self.doit / 'scripts/tg-send.sh')
            else:
                with self.assertRaises(tg.Incomplete):
                    scan.target(link)
        print('AC3 default symlink budget exactly 16 and over at 17')

    def test_live_and_push_inventory(self):
        names = ['rloop.sh', 'rloop.run.sh', 'restart-driver.sh', 'restart-main.sh', 'restart-prewarm.sh',
                 'restart-safe.sh', 'restart-when-idle.sh', 'release-loop-1450.sh', 'release-now.sh',
                 'release-prewarm.sh', 'release-retry.sh', 'rehearse.sh', 'rehearse.run.sh',
                 'rehearse-with-pg.sh', 'rehearse-pg.py', 'rehearse-pg-reset.sh',
                 'foreman/catcher/catcher.py', 'hetzner/watch.py', 'new-tree/newly-named.sh']
        for name in names:
            body = "import requests\nrequests.post('https://api.telegram.org/botx/sendMessage')" if name.endswith('.py') else OUTBOUND
            self.check('push discovery', self.push / name, body, False)
        for root in (self.root / 'bin', self.root / 'telegram', self.root):
            self.check('live discovery', root / 'unversioned.py',
                       "import requests\nrequests.post('https://api.telegram.org/botx/sendMessage')", False)
        for i, name in enumerate(('dv2', 'wt-dv2', 'wt-main', 'wt1663', 'r1663b/tree')):
            checkout = self.push / name
            self.put(checkout / 'tg-send.sh', OUTBOUND)
            if i % 2:
                self.put(checkout / '.git', 'gitdir: example')
            else:
                (checkout / '.git').mkdir()
            ok, report = self.scan()
            self.assertTrue(ok, report)
            self.assertIn(str(checkout), report)
            self.assertIn('nested Git checkout', report)
            print('AC3 pruned root', checkout.relative_to(self.base), 'nested Git checkout; expected local pass')
        self.check('active invocation into pruned checkout', self.root / 'bin/caller.sh',
                   'TG_SENDER=test ' + str(self.push / 'dv2/tg-send.sh') + ' text', False)
        self.check('similarly named unmarked tree', self.push / 'dv2-other/tg-send.sh', OUTBOUND, False)
        self.check('byte-identical regular sender', self.root / 'bin/copy.sh', OUTBOUND, False)
        self.check('modified independent sender', self.root / 'bin/changed.sh', OUTBOUND + '# changed', False)

    def test_links_permissions_and_missing_roots(self):
        for name, target in (('broken.sh', self.base / 'missing'), ('alias.sh', self.root / 'bin/bad.sh'),
                             ('cycle.sh', self.root / 'bin/cycle.sh')):
            self.put(self.root / 'bin/bad.sh', OUTBOUND)
            link = self.root / 'bin' / name
            link.symlink_to(target)
            self.assertFalse(self.scan()[0], name)
            link.unlink(); (self.root / 'bin/bad.sh').unlink()
            print('AC3 link', name)
        path = self.put(self.push / 'unreadable.sh', OUTBOUND)
        path.chmod(0)
        self.assertFalse(self.scan()[0])
        path.chmod(0o600); path.unlink()
        self.push.rmdir()
        self.assertFalse(self.scan()[0])

    def test_all_six_budget_boundaries(self):
        # Exercise the actual traversal with small equivalent budgets; the
        # boundary logic is independent of production-sized default constants.
        area = self.base / 'budget'; area.mkdir()
        file = self.put(area / 'one.sh', '# 12345\n')
        def result(**limits):
            scan = tg.Scan(area, self.albert, self.root, self.push, tg.Limits(**limits))
            scan.discover(area)
            return scan.failed, '\n'.join(scan.rows)
        for key, boundary in (('entries', 1), ('sources', 1), ('depth', 1),
                              ('file_bytes', file.stat().st_size), ('total_bytes', file.stat().st_size)):
            print('AC3 budget', key, 'at', boundary, 'over', boundary - 1)
            self.assertFalse(result(**{key: boundary})[0])
            failed, report = result(**{key: boundary - 1})
            self.assertTrue(failed, report)
            self.assertIn('INCOMPLETE', report)
            self.assertIn(str(area), report)
        first = area / 'first.sh'; first.symlink_to(file)
        scan = tg.Scan(area, self.albert, self.root, self.push, tg.Limits(hops=1))
        self.assertEqual(scan.resolve(first), file)
        second = area / 'second.sh'; second.symlink_to(first)
        with self.assertRaises(tg.Incomplete):
            scan.resolve(second)
        print('AC3 budget symlink hops at 1, over 2')
        self.assertEqual(tg.Limits(), tg.Limits(100000, 25000, 32, 4194304, 134217728, 16))


class AC4(Fixture):
    def test_resource_decision_matrix(self):
        ref = 'L-thinker-0001.jsonl:1'
        self.proc.side_effect = lambda argv: '[]'
        valid = self.event()
        cases = [
            ('unrelated subject', self.event(subject='other', why='telegram-alerter unknown'), True),
            ('unrelated resource', self.event(why={'resource': 'other', 'state': 'deployed'}), True),
            ('unrelated prose', self.event(why='Plan approval'), True),
            ('later resource prose', self.event(why='telegram-alerter has no answer'), False),
            ('JSON-string resource prose', self.event(why=json.dumps('telegram-alerter unknown')), False),
            ('resource wrong event timestamp', self.event(ts='invalid'), False),
            ('resource future event timestamp', self.event(ts='2030-01-01T00:00:00Z'), False),
            ('later repetition', self.event(), True)]
        for name, row, expected in cases:
            if row['ts'] == valid['ts']:
                row['ts'] = '2026-02-01T00:00:00Z'
            self.ledger([valid, row])
            code, report = self.cli('--offbox', '--lambda-decision-ref', ref)
            self.assertEqual(code == 0, expected, (name, report))
            self.assertFalse((self.root / 'cache').exists())
            print('AC4 decision', name, 'expected silence', expected)
        self.ledger([valid])
        self.ledger([self.event(actor='operator', why={'resource': 'telegram-alerter', 'state': 'deployed'})],
                    'L-builder-0002.jsonl')
        self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', ref)[0], 0)
        print('AC4 unauthorized contradictory actor claim ignored')
        row = self.event(why=json.dumps(valid['why']))
        self.ledger([row])
        self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', ref)[0], 0)
        for missing in ('can_send',):
            row = self.event()
            row['why']['state'] = 'disabled'
            self.ledger([row])
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', ref)[0], 1)
            print('AC4 disabled missing', missing)
        for prose in ('Plan approved; off-box remains open', 'Lambda unknown', 'Secret removal needs operator approval'):
            self.ledger([self.event(why=prose)])
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', ref)[0], 1)
            print('AC4 planning prose is not silence evidence')

    def test_external_key_source_inventory(self):
        self.ledger([self.event()])
        for where in ('gh', 'ssh'):
            for key in sorted(tg.KEYS):
                print('AC4 key present, including empty-valued key', where, key)
                self.proc.side_effect = lambda argv, w=where, k=key: json.dumps(
                    ([{'name': k}] if w == 'gh' else [k]) if argv[0] == w else [])
                self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 1)
        self.proc.side_effect = lambda argv: '[]'
        self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 0)
        argv = [call.args[0] for call in self.proc.call_args_list]
        ssh = next(a for a in argv if a[0] == 'ssh')
        self.assertIn('python3 -c ', ssh[-1])
        self.assertIn('print(json.dumps(sorted(set(keys))))', ssh[-1])
        self.assertNotIn('cat ', ssh[-1])
        for bad in ('', '{}', 'not-json', '[null]'):
            print('AC4 collection malformed', repr(bad))
            self.proc.side_effect = None; self.proc.return_value = bad
            self.assertEqual(self.cli('--offbox')[0], 1)
        for failure in (PermissionError(), tg.Incomplete('timeout'), tg.Incomplete('output limit')):
            print('AC4 unavailable source', type(failure).__name__)
            self.proc.side_effect = failure
            self.assertEqual(self.cli('--offbox')[0], 1)
        self.assertFalse((self.root / 'cache').exists())

    def test_lambda_decision_inventory(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        ref = 'L-thinker-0001.jsonl:1'
        def accepts(rows, name='L-thinker-0001.jsonl', selected=ref):
            for path in (self.root / 'events').glob('*.jsonl') if (self.root / 'events').exists() else []:
                path.unlink()
            self.ledger(rows, name)
            return tg.lambda_silent(self.root, selected, now)
        for state in ('not-deployed', 'disabled'):
            row = self.event()
            row['why'].update(state=state, can_send=False)
            self.assertTrue(accepts([row]), state)
            print('AC4 accepted state', state)
        for field, value in (('state', 'deployed'), ('state', 'unknown'), ('observed_at', 'future'),
                             ('observed_at', '2030-01-01T00:00:00Z'), ('evidence_ref', ''),
                             ('resource', 'other'), ('can_send', True)):
            row = self.event()
            row['why'].update(state='disabled', can_send=False)
            row['why'][field] = value
            self.assertFalse(accepts([row]), field)
            print('AC4 rejected silence field', field, value)
        for changes in ({'type': 'note'}, {'subject': 'other'}, {'ts': 'bad'},
                        {'ts': '2030-01-01T00:00:00Z'}, {'why': 'Plan approved'}, {'why': '{bad'}):
            self.assertFalse(accepts([self.event(**changes)]), changes)
            print('AC4 rejected event fields', sorted(changes))
        self.assertFalse(accepts([self.event(actor='operator')], 'L-builder-0001.jsonl', 'L-builder-0001.jsonl:1'))
        self.assertTrue(accepts([self.event(actor='builder')]))
        self.assertTrue(accepts([self.event()], 'L-operator-0001.jsonl', 'L-operator-0001.jsonl:1'))
        for bad in (None, '../other.jsonl:1', '/source.jsonl:1', 'missing.jsonl:1', ref[:-1] + '2'):
            self.assertFalse(accepts([self.event()], selected=bad))
            print('AC4 invalid or dangling ref', bad)
        for later in (self.event(why=dict(resource='telegram-alerter', state='deployed')),
                      self.event(why=dict(resource='telegram-alerter', state='disabled', can_send=True)),
                      self.event(why=dict(resource='telegram-alerter', state='unknown')),
                      self.event(why='{"resource":"telegram-alerter" broken'),
                      self.event(why=dict(resource='telegram-alerter', state='not-deployed'))):
            later['ts'] = '2026-02-01T00:00:00Z'
            self.assertFalse(accepts([self.event(), later]))
            print('AC4 contradictory or incomplete later resource decision')
        earlier = self.event(why=dict(resource='telegram-alerter', state='deployed'), ts='2025-01-01T00:00:00Z')
        self.assertTrue(accepts([self.event(), earlier]))
        self.assertTrue(accepts([self.event(), self.event(ts='2026-02-01T00:00:00Z')]))
        equal = self.event(why=dict(resource='telegram-alerter', state='disabled', can_send=False,
                                   observed_at='2025-12-31T00:00:00Z', evidence_ref='https://example.org/proof'))
        self.assertFalse(accepts([self.event(), equal]))
        self.assertTrue(accepts([self.event()]))
        self.ledger([equal], 'L-operator-0001.jsonl')
        self.assertFalse(tg.lambda_silent(self.root, ref, now))
        print('AC4 earlier conflict, same-state repeat, equal-time later and cross-source conflict')

    def test_segmented_sources_and_runtime_root(self):
        self.ledger([self.event()])
        events = self.root / 'events'
        name = 'L-thinker-0001.jsonl'
        data = (events / name).read_bytes()
        (events / name).unlink()
        directory = events / 'segments'; directory.mkdir()
        (directory / 'a.jsonl').write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.put(directory / 'a.idx.json', json.dumps(dict(version=1, segment='a.jsonl', bytes=len(data),
            sha256=digest, members=[dict(name=name, offset=0, length=len(data), lines=1, sha256=digest, ends_nl=True)])))
        decoy = self.base / 'decoy'; (decoy / 'events').mkdir(parents=True)
        self.put(decoy / 'events' / name, json.dumps(self.event(why='unknown')) + '\n')
        self.proc.side_effect = lambda argv: '[]'
        with patch.dict(os.environ, {'DOIT_ROOT': str(decoy)}):
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', name + ':1')[0], 0)
            conflict = self.ledger([self.event(ts='2026-02-01T00:00:00Z',
                why={'resource': 'telegram-alerter', 'state': 'deployed'})], 'L-operator-0002.jsonl')
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', name + ':1')[0], 1)
            conflict.unlink()
        for root in (self.root, decoy):
            self.assertFalse((root / 'cache').exists())
        (directory / 'a.jsonl').write_bytes(data.replace(b'not-deployed', b'was-deployed'))
        self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', name + ':1')[0], 1)
        print('AC4 segmented SOURCE:ROW, explicit root override, damaged segment')


class AC5(Fixture):
    def test_runtime_root_no_writes_or_imports(self):
        import fold
        import ledger_cache
        self.ledger([self.event()])
        self.put(self.push / 'never_import.py', "raise RuntimeError('PRIVATE_SENTINEL_7421')\n")
        self.put(self.root / 'bin/config.json', 'PRIVATE_SENTINEL_7421')
        self.put(self.root / 'bin/.env', 'PRIVATE_SENTINEL_7421')
        self.put(self.root / 'bin/data/client.py', 'PRIVATE_SENTINEL_7421')
        decoy = self.base / 'decoy'
        self.put(decoy / 'events/L-operator-0001.jsonl', json.dumps(self.event(why='telegram-alerter unknown')))
        self.proc.side_effect = lambda argv: '[]'
        def snapshot():
            return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*')
                    if p.is_file() and not p.is_symlink()}
        before = snapshot()
        with patch.dict(os.environ, {'DOIT_ROOT': str(decoy)}), \
             patch.object(fold, 'read_events', side_effect=AssertionError('cached reader')), \
             patch.object(ledger_cache, 'read', side_effect=AssertionError('cached reader')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('unexpected process')):
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 0)
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'missing.jsonl:1')[0], 1)
        self.assertEqual(snapshot(), before)
        for root in (self.root, decoy):
            self.assertFalse((root / 'cache').exists())
        print('AC5 successful and failing runtime-root checks: no import, network, process, ledger/cache/store writes')

    def test_source_is_data_and_output_is_sanitized(self):
        sentinel = 'PRIVATE_SENTINEL_7421'
        sender = '123456789' + ':' + sentinel
        self.put(self.push / 'sender-name.sh', 'TG_SENDER=' + sender + ' ' + str(self.root / 'bin/tg-send.sh') + ' text')
        self.assertNotIn(sentinel, self.cli()[1])
        path = self.put(self.push / '$(touch SHOULD_NOT_EXIST).sh', OUTBOUND + '# ' + sentinel)
        before = path.read_bytes()
        code, report = self.cli()
        self.assertEqual(code, 1)
        self.assertNotIn(sentinel, report)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.base / 'SHOULD_NOT_EXIST').exists())
        secret = self.put(self.base / '.env', sentinel)
        (self.root / 'bin/escape.py').symlink_to(secret)
        original = Path.open
        def guarded(p, *a, **kw):
            if p == secret:
                raise AssertionError('secret file opened')
            return original(p, *a, **kw)
        with patch.object(Path, 'open', guarded):
            self.assertEqual(self.cli()[0], 1)
            self.ledger([self.event()])
            (self.root / 'events/escape.jsonl').symlink_to(secret)
            self.proc.side_effect = lambda argv: '[]'
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 1)
        print('AC5 shell-syntax filename, private source comment, symlink credential escape')

    def test_no_network_mutations_or_cache_reads(self):
        import fold
        import ledger_cache
        self.ledger([self.event()])
        self.proc.side_effect = lambda argv: '[]'
        cache = self.put(self.root / 'cache/existing', 'unchanged')
        store = self.put(self.root / 'store/existing', 'unchanged-store')
        snapshot = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}
        with patch.object(fold, 'read_events', side_effect=AssertionError('cached fold forbidden')), \
             patch.object(ledger_cache, 'read', side_effect=AssertionError('cached ledger forbidden')):
            self.assertEqual(self.cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 0)
            self.proc.side_effect = OSError('injected PRIVATE_SENTINEL_7421')
            code, report = self.cli('--offbox')
            self.assertEqual(code, 1)
            self.assertNotIn('PRIVATE_SENTINEL', report)
        for p, data in snapshot.items():
            self.assertEqual(p.read_bytes(), data)
        self.assertEqual(cache.read_text(), 'unchanged')
        self.assertEqual(store.read_text(), 'unchanged-store')
        print('AC5 cached readers trapped, ledger/cache/store unchanged, remote errors sanitized')

    def test_bounded_external_collector(self):
        for code, kwargs in (("print('[]')", {}), ("print('x'*100)", {'cap': 10}),
                             ('import time;time.sleep(5)', {'timeout': 0.01}),
                             ("import sys;sys.stderr.write('PRIVATE');sys.exit(1)", {})):
            print('AC5 collector bounds', sorted(kwargs))
            if kwargs or 'stderr' in code:
                with self.assertRaises(tg.Incomplete):
                    COLLECT([sys.executable, '-c', code], **kwargs)
            else:
                self.assertEqual(COLLECT([sys.executable, '-c', code]).strip(), '[]')


class AC6(Fixture):
    def test_offbox_dispatch_uses_stub_collectors(self):
        self.ledger([self.event()])
        tools = self.base / 'tools'
        for name in ('gh', 'ssh'):
            path = self.put(tools / name, '#!/usr/bin/python3\nimport json,sys\n' +
                            ('assert sys.argv[1:] == ["secret","list","--repo","fredhead88/albert-scott-platform","--json","name"]\n'
                             if name == 'gh' else 'assert sys.argv[1:3] == ["-o","BatchMode=yes"]\nassert sys.argv[-1].startswith("python3 -c ")\n') +
                            'print(json.dumps([]))\n')
            path.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(tools) + ':/usr/bin:/bin'}):
            result = self.run_cli('--offbox', '--lambda-decision-ref', 'L-thinker-0001.jsonl:1')
            self.assertEqual(result[0], 0, result)
            self.assertIn('off-box evidence confirmed', result[1])
            self.assertEqual(self.run_cli('--offbox', '--lambda-decision-ref', 'missing.jsonl:1')[0], 1)
            self.assertEqual(self.run_cli('--lambda-decision-ref', 'L-thinker-0001.jsonl:1')[0], 2)
        self.assertFalse((self.root / 'cache').exists())
        print('AC6 actual --offbox and --lambda-decision-ref forwarding; stub gh/ssh; status 0/1/2')

    def run_cli(self, *extra):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([str(REPO / 'doit'), 'tg-check', *self.args(), *extra],
                                env=env, capture_output=True, text=True, timeout=15)
        return result.returncode, result.stdout, result.stderr

    def test_dispatch_status_and_determinism(self):
        clean = self.run_cli()
        self.assertEqual(clean[0], 0, clean)
        self.assertEqual(self.run_cli(), clean)
        self.assertIn('Off-box silence was not checked', clean[1])
        path = self.put(self.push / 'caller.sh', OUTBOUND)
        result = self.run_cli()
        self.assertEqual(result[0], 1)
        self.assertIn(str(path), result[1])
        path.write_text(str(self.root / 'bin/tg-send.sh') + ' text')
        self.assertEqual(self.run_cli()[0], 1)
        path.unlink(); self.push.rmdir()
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.run_cli('--unknown')[0], 2)
        help_result = self.run_cli('--help')
        for flag in ('--offbox', '--lambda-decision-ref', '--doit-repo', '--albert-repo', '--root', '--push-dir'):
            self.assertIn(flag, help_result[1])
        print('AC6 real dispatch clean, deterministic repeat, bypass, missing sender, missing root, invalid CLI, help')


if __name__ == '__main__':
    unittest.main()
