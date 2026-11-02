"""Hermetic M2 audit: read-only reconciliation, never an automatic fix generator."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory(prefix='failure-snapshot-import-')
os.environ['DOIT_ROOT'] = _TMP.name
import owed_failure_snapshot as audit

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SID = 'L-spec-9001'
FIX = 'L-spec-9002'


def event(event_type, hours, **fields):
    actor = {'owed-ac':'spec-writer', 'spec-written':'spec-writer', 'shipped':'executor',
             'owed-failed':'owed-sweeper', 'owed-met':'owed-sweeper',
             'owed-routed':'thinker', 'owed-corrective':'thinker', 'owed-waived':'operator'}.get(event_type,'executor')
    return dict(type=event_type, subject=SID, actor=actor, criterion='AC1',
                ts=(NOW+timedelta(hours=hours)).isoformat(),
                _src=f'L-{actor}-fixture.jsonl:{event_type}:{hours}', **fields)


def failed(hours=-48, **fields):
    return event('owed-failed',hours,kind='unmet',evidence='expected 2, got 1',**fields)


def seed():
    return [event('spec-written',-100), event('owed-ac',-99,wake_at=(NOW-timedelta(hours=90)).isoformat()),
            event('shipped',-90)]


class FailureSnapshotTests(unittest.TestCase):
    def report(self, events):
        before = copy.deepcopy(events)
        out = audit.snapshot(events, NOW)
        self.assertEqual(events,before)
        return out

    def test_repeat_and_recheck_never_reset_clock(self):
        events=seed()+[failed(),event('owed-routed',-3,route='recheck'),failed(-1)]
        row=self.report(events)['rows'][0]
        self.assertEqual(row['age_h'],48)
        self.assertEqual(row['first_failure']['ts'],events[3]['ts'])
        self.assertEqual(row['latest_verdict']['ts'],events[-1]['ts'])
        self.assertTrue(row['overdue_unowned'])
        self.assertEqual(row['next_owner'],'thinker')
        self.assertNotIn('evidence',json.dumps(row))

    def test_existing_corrective_remains_owned_after_repeat_no_duplicate(self):
        events=seed()+[failed(),event('owed-corrective',-40,corrective=FIX),failed(-1)]
        events.append(dict(event('spec-written',-80),subject=FIX))
        row=self.report(events)['rows'][0]
        self.assertTrue(row['owned_by_corrective'])
        self.assertFalse(row['overdue_unowned'])
        self.assertEqual(len(row['corrective_links']),1)

    def test_missing_dead_and_unauthorized_links_do_not_own_failure(self):
        for target, extra in [(FIX,[]),(FIX,[dict(event('spec-killed',-2),subject=FIX)]),
                              (FIX,[dict(event('observed',-80),subject=FIX)]),
                              (FIX,[dict(event('spec-written',-80),subject=FIX)])]:
            events=seed()+[failed()]+extra
            events.append(dict(event('owed-corrective',-1,corrective=target),
                               actor='builder' if extra and extra[0]['type']=='spec-written' else 'thinker'))
            row=self.report(events)['rows'][0]
            self.assertTrue(row['overdue_unowned'])

    def test_valid_waiver_and_met_resolve_but_later_failure_starts_new_episode(self):
        for resolution in [event('owed-waived',-25,reason='Reasoned, specific fixture waiver'),
                           event('owed-met',-25,evidence='observed 2')]:
            events=seed()+[failed(),resolution]
            self.assertFalse(self.report(events)['rows'][0]['open_product_failure'])
            row=self.report(events+[failed(-24)])['rows'][0]
            self.assertTrue(row['overdue_unowned'])
            self.assertEqual(row['age_h'],24)
            self.assertEqual(row['first_product_failure']['ts'],events[3]['ts'])
            if resolution['type']=='owed-waived':
                self.assertTrue(row['waiver']['reason_present'])
                self.assertNotIn('Reasoned, specific',json.dumps(row))

    def test_reasonless_or_unauthorized_waiver_cannot_close(self):
        for waiver in [event('owed-waived',-1,reason='  '),
                       dict(event('owed-waived',-1,reason='not mine'),actor='builder')]:
            self.assertTrue(self.report(seed()+[failed(),waiver])['rows'][0]['overdue_unowned'])

    def test_capability_split_orphan_and_future_unauthorized_failures(self):
        rows=seed()+[dict(failed(),evidence='permission denied for relation public.fixture')]
        report=self.report(rows)
        self.assertEqual(report['counts']['capability_misclassifications'],1)
        self.assertFalse(report['rows'][0]['open_product_failure'])
        orphan=self.report([failed()])['rows'][0]
        self.assertEqual(orphan['status'],'undeclared')
        self.assertTrue(orphan['overdue_unowned'])
        self.assertEqual(self.report(seed()+[failed(1)])['rows'],[])
        out=self.report(seed()+[dict(failed(),actor='builder')])
        self.assertEqual(out['rows'],[])
        self.assertEqual(out['counts']['ignored_unauthorized_failures'],1)

    def test_real_reader_cli_changes_no_ledger_state_or_cache_bytes(self):
        with tempfile.TemporaryDirectory(prefix='failure-snapshot-cli-') as temp:
            root=Path(temp);(root/'events').mkdir()
            (root/'state').mkdir();(root/'state'/'keep').write_text('unchanged')
            (root/'cache').mkdir();(root/'cache'/'keep').write_text('unchanged')
            events=seed()+[failed()]
            for row in events:
                path=root/'events'/('L-'+row['actor']+'-fixture.jsonl')
                with path.open('a') as f:
                    f.write(json.dumps({k:v for k,v in row.items() if k not in ('actor','_src')})+'\n')
            def files():
                return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            before=files()
            env=dict(os.environ,DOIT_ROOT=str(root),DOIT_READ_CACHE='1')
            result=subprocess.run([sys.executable,str(Path(audit.__file__)), '--at',NOW.isoformat()],
                                  env=env,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(result.stdout)['counts']['overdue_unowned'],1)
            self.assertEqual(before,files())


if __name__=='__main__':
    unittest.main()
