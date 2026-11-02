"""Charter 0049 R3: capability gaps cannot become product failures or guessed expiry."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

_IMPORT_ROOT = tempfile.TemporaryDirectory(prefix='owed-boundary-import-')
os.environ['DOIT_ROOT'] = _IMPORT_ROOT.name
os.environ['DOIT_NO_POKE'] = '1'
import dispatch
import fold
import grading_env
import owed
import owed_routes
import sweep_owed

SID = 'L-spec-9001'
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class CapabilityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='owed-boundary-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for obj, key, value in [(fold,'ROOT',self.root),(fold,'EVENTS',self.root/'events')]:
            p=patch.object(obj,key,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,DOIT_ROOT=str(self.root),DOIT_NO_POKE='1')
        p.start();self.addCleanup(p.stop)
        p=patch('dispatch.now',return_value=NOW.isoformat());p.start();self.addCleanup(p.stop)
        self.root.joinpath('events').mkdir()
        self.seed = [self.event('owed-ac',-49,wake_at=(NOW-timedelta(hours=48)).isoformat()),
                     self.event('shipped',-48)]

    def event(self, event_type, hours, **fields):
        return dict(type=event_type,subject=SID,project='fixture',criterion='AC1',
                    ts=(NOW+timedelta(hours=hours)).isoformat(),**fields)

    def output(self, **changes):
        row=dict(spec=SID,criterion='AC1',verdict='failed',kind='unmet',
                 evidence='AssertionError: expected 3 rows, observed 2')
        row.update(changes)
        return dispatch.events_for('owed-sweeper',dict(results=[row]),
                                   SimpleNamespace(subject='L-sweep-fixture'),{})

    def test_true_mismatch_and_observed_outage_stay_product_failures(self):
        for evidence in ('AssertionError: expected 3 rows, observed 2',
                         'Production health returned HTTP 503 instead of 200',
                         'AssertionError: expected permission denied for relation public.fixture; observed SELECT succeeded',
                         'Expected unavailable browser warning; got an unsafe unrestricted session'):
            result=self.output(evidence=evidence)
            self.assertEqual(result[0][0],'owed-failed')
            self.assertEqual(result[0][1]['evidence'],evidence)

    def test_missing_select_is_recorded_losslessly_and_routes_to_an_owner(self):
        evidence='permission denied for relation public.report_fixture'
        result=self.output(evidence=evidence)
        self.assertEqual(result[0][0],'owed-unobservable')
        fields=result[0][1]
        self.assertEqual(fields['capability'],'ro-dsn')
        self.assertEqual(fields['missing_access'],['SELECT on public.report_fixture'])
        self.assertEqual(fields['next_owner'],'thinker')
        self.assertIn('public.report_fixture',fields['ask'])
        self.assertEqual(fields['evidence'],evidence)
        self.assertEqual(fields['original_verdict'],'failed')
        for actor,events in [('spec-writer',[self.seed[0]]),('executor',[self.seed[1]])]:
            path=self.root/'events'/('L-'+actor+'-fixture.jsonl')
            path.write_text(''.join(json.dumps(e)+'\n' for e in events))
        fields=dict(fields);fields.pop('subject')
        reason=dispatch.emit(self.root/'events/L-owed-sweeper-fixture.jsonl',
                             dict(subject=SID,project='fixture'),result[0][0],strict=True,**fields)
        self.assertIsNone(reason)
        row=owed.checks(fold.read_events(),NOW)[0]
        self.assertEqual((row['status'],row['failures']),('blocked',0))
        self.assertIn('public.report_fixture',row['ask'])
        self.assertEqual(row['next_owner'],'thinker')

    def test_positive_observation_gaps_are_not_product_failures(self):
        for evidence,capability in [('No browser is available on this seat','browser'),
                                    ('No deployed service is available yet','not-live'),
                                    ('Cannot read evidence artifact /fixture/proof.json','repo')]:
            with self.subTest(capability=capability):
                kind,fields=self.output(evidence=evidence)[0]
                self.assertEqual((kind,fields['capability']),('owed-unobservable',capability))
                self.assertTrue(fields['ask'])
                self.assertEqual(fields['next_owner'],'thinker')

    def test_access_asks_keep_qualified_identifiers_and_unknown_permission_gaps(self):
        cases=[('missing SELECT privilege on public.report_fixture',['SELECT on public.report_fixture']),
               ('Expected 3 reporting rows; observed permission denied for relation public.report_fixture',['SELECT on public.report_fixture']),
               ('permission denied for relation "public"."report_fixture"',['SELECT on "public"."report_fixture"']),
               ('permission denied for schema analytics',['USAGE on analytics'])]
        for evidence,access in cases:
            with self.subTest(evidence=evidence):
                kind,fields=self.output(evidence=evidence)[0]
                self.assertEqual((kind,fields['capability']),('owed-unobservable','ro-dsn'))
                self.assertEqual(fields['missing_access'],access)
                self.assertIn(access[0],fields['ask'])
        kind,fields=self.output(evidence='Permission denied by the observation tool; exact access is unknown')[0]
        self.assertEqual((kind,fields['capability']),('owed-unobservable','unknown'))
        self.assertEqual(fields['next_owner'],'thinker')

    def test_unknown_and_ambiguous_legacy_labels_do_not_guess_repo_or_expire(self):
        for capability in ('unknown','legacy-sensor','other'):
            with self.subTest(capability=capability):
                self.assertEqual(owed_routes.from_grader(capability),capability)
                self.assertIsNone(owed_routes.route_for(capability))
                events=[*self.seed,self.event('owed-unobservable',-10,capability=capability,why='route not established'),
                        self.event('owed-routed',-9,route='repo',epoch='one'),
                        self.event('owed-routed',-8,route='repo',epoch='two')]
                row=owed.checks(events,NOW+timedelta(days=365))[0]
                self.assertEqual((row['status'],row['capability']),('blocked',capability))
                self.assertEqual(row['route_attempts'],{'repo':2})
                self.assertEqual(row['routing_state'],'undetermined')
                self.assertEqual(row['next_owner'],'thinker')
                self.assertTrue(row['ask'])
                with patch.object(sweep_owed,'resolvable',return_value=(True,None)):
                    self.assertEqual(sweep_owed.readmitted({SID:dict(evs=events)},NOW,lambda p:{}),[])

    def test_unestablished_relabel_is_held_before_any_guessed_dispatch(self):
        events=[*self.seed,self.event('owed-routed',-10,route='relabel',capability='legacy-sensor')]
        row=owed.checks(events,NOW)[0]
        self.assertEqual((row['status'],row['routing_state']),('blocked','undetermined'))
        self.assertEqual(row['next_owner'],'thinker')
        self.assertIn('legacy-sensor',row['ask'])
        self.assertEqual(row['route_attempts'],{})

    def test_exhausted_known_route_keeps_all_attempts_and_an_actionable_hold(self):
        events=[*self.seed,self.event('owed-unobservable',-10,capability='browser',why='No browser available'),
                self.event('owed-routed',-9,route='browser',epoch='one'),
                self.event('owed-routed',-8,route='browser',epoch='two')]
        row=owed.checks(events,NOW)[0]
        self.assertEqual(row['status'],'blocked')
        self.assertEqual(row['routing_state'],'exhausted')
        self.assertEqual(row['route_attempts'],{'browser':2})
        self.assertEqual(row['next_owner'],'thinker')
        self.assertTrue(row['ask'])

    def test_legacy_capability_failure_is_projected_without_editing_history(self):
        event=self.event('owed-failed',-10,kind='unmet',
                         evidence='permission denied for relation public.report_fixture',_src='fixture:3')
        before=dict(event)
        row=owed.checks([*self.seed,event],NOW)[0]
        self.assertEqual((row['status'],row['failures'],row['capability']),('blocked',0,'ro-dsn'))
        self.assertEqual(event,before)
        self.assertIn('public.report_fixture',row['ask'])

    def test_uncertain_result_is_undetermined_and_unknown_schema_is_lossless(self):
        kind,fields=self.output(reason_code='undetermined',evidence='No usable evidence to classify this result')[0]
        self.assertEqual((kind,fields['capability']),('owed-unobservable','unknown'))
        import jsonschema
        schema=json.loads((Path(dispatch.__file__).resolve().parent.parent/'agents/owed-sweeper.schema.json').read_text())
        output=dict(batch='L-sweep-fixture',results=[dict(spec=SID,criterion='AC1',
            verdict='cannot-observe',capability='legacy-sensor',why='tool absent',
            ask='Thinker: name the observation tool',next_owner='thinker')],
            contamination=False,escalations=[],declarations=[])
        jsonschema.validate(output,schema)

    def test_existing_approved_access_is_not_a_new_operator_approval(self):
        classified=owed_routes.classify_ask('Use an approved read-only route for this observation','ro-dsn')
        self.assertEqual(classified['owner'],'thinker')
        kind,fields=self.output(verdict='cannot-observe',capability=None,why='repository tests')[0]
        self.assertEqual(kind,'owed-unobservable')
        self.assertIsNone(fields['original_capability'])
        self.assertEqual(fields['capability'],'unknown')

    def test_checkout_reader_boilerplate_does_not_request_browser(self):
        text='## Acceptance criteria\nAC1 [backend]: directory notification.\nreview_path: log in as a local checkout reader (no credentials); go to the Verification result; do read its assertion output.\n'
        with patch.object(grading_env,'_look_toml',return_value={'prod':[dict(project='fixture')]}):
            self.assertNotIn('browser',grading_env.criterion_caps(text,'','fixture').get('AC1',[]))
            self.assertNotIn('AC1',grading_env.route(text,'','fixture'))
            live=text.replace('a local checkout reader (no credentials)','analyst at https://example.invalid/')
            self.assertEqual(grading_env.route(live,'','fixture')['AC1'],('browser','reviewer'))


if __name__=='__main__':unittest.main()
