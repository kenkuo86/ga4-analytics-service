import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('usage_routing_plan',ROOT/'scripts/plan_usage_routing.py')
routing=importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


class RoutingPlanTests(unittest.TestCase):
    def test_all_data_destinations_are_in_requested_project_and_region(self):
        plan=routing.plan('user:owner@example.test')
        for dataset in plan['datasets']:
            self.assertEqual(dataset['datasetReference']['projectId'],'ga4-reports-dev')
            self.assertEqual(dataset['location'],'asia-east1')
            self.assertEqual(dataset['access'],[{'role':'OWNER','userByEmail':'owner@example.test'}])
            self.assertEqual(dataset['maxTimeTravelHours'],'48')
        for sink in plan['sinks']:
            self.assertIn('/projects/ga4-reports-dev/',sink['destination'])
            self.assertTrue(sink['disabled'])
        self.assertFalse(plan['enablement']['deploy'])
        self.assertFalse(plan['enablement']['app_emission'])
        self.assertFalse(plan['enablement']['summary_emission'])

    def test_summary_is_separate_short_lived_storage_including_default_exclusion(self):
        plan=routing.plan('user:owner@example.test')
        self.assertEqual([d['defaultPartitionExpirationMs'] for d in plan['datasets']],
                         [str(180*86400000),str(30*86400000)])
        self.assertEqual([b['retentionDays'] for b in plan['logging_buckets']],[180,30])
        for sink in plan['sinks']:
            self.assertIn('labels.usage_schema="1.0"',sink['filter'])
            self.assertIn('labels.usage_environment="pilot"',sink['filter'])
            if 'summary' in sink['name']:
                self.assertIn('/ga4_mcp_test_summary',sink['destination'])
                self.assertIn('analytics_request_summary',sink['filter'])
                self.assertNotIn('analytics_request_completed',sink['filter'])
        self.assertIn('ga4_mcp_test_summary_v1',plan['default_sink_exclusion']['filter'])
        self.assertFalse(plan['default_sink_exclusion']['disabled'])

    def test_runtime_never_gets_bigquery_data_writer_or_reader(self):
        plan=routing.plan('user:owner@example.test')
        runtime=[binding for binding in plan['iam'] if binding.get('member','').endswith(routing.RUNTIME_SA)]
        self.assertEqual({b['role'] for b in runtime},{'roles/logging.logWriter','roles/secretmanager.secretAccessor'})
        writers=[binding for binding in plan['iam'] if binding['role']=='roles/bigquery.dataEditor']
        self.assertEqual(len(writers),2)
        self.assertTrue(all('member_from_sink_writerIdentity' in b for b in writers))
        self.assertTrue(plan['secret']['reuse_existing'])
        self.assertFalse(plan['secret']['rotate_automatically'])

    def test_dedup_functions_bound_time_and_do_not_copy_summary_to_long_term(self):
        for kind,model in (('events','canonical'),('summary','summary')):
            fields=json.loads((ROOT/f'telemetry/{model}.bigquery-payload.v1.json').read_text())
            sql=routing.dedup_sql(kind,fields)
            self.assertIn('PARTITION BY schema_version, interaction_id, event_name',sql)
            self.assertIn('timestamp >= TIMESTAMP(range_start',sql)
            self.assertIn('usage_validation',sql)
            self.assertIn('SAFE_CAST',sql)
            self.assertIn('Asia/Taipei',sql)
            self.assertNotIn('CREATE TABLE ',sql)
            if kind=='events':
                self.assertIn('CAST(NULL AS STRING) AS request_summary',sql)
                self.assertNotIn("JSON_VALUE(payload, '$.request_summary')",sql)
                self.assertIn("SAFE_CAST(JSON_VALUE(payload, '$.requested_days') AS BIGNUMERIC)",sql)
                self.assertIn(' = TRUNC(',sql)
                self.assertIn("JSON_VALUE(payload, '$.tenant_id') AS tenant_id",sql)
                self.assertIn('BETWEEN 0 AND 179',sql)
            else:
                self.assertIn('INTERVAL 30 DAY',sql)
                self.assertIn('BETWEEN 0 AND 29',sql)

    def test_plan_cli_is_offline_and_rejects_unbounded_principals(self):
        for owner in ('allUsers','group:all@example.test','user:owner@example.test\nmalicious'):
            with self.assertRaises(ValueError):routing.plan(owner)
        with tempfile.TemporaryDirectory() as path, patch('sys.argv',['plan','--owner-principal','user:owner@example.test','--output-dir',path]), patch('socket.socket',side_effect=AssertionError('network forbidden')):
            routing.main()
            self.assertEqual(len(list(Path(path).iterdir())),3)
            self.assertEqual(json.loads((Path(path)/'usage-routing-plan.json').read_text())['mode'],'PLAN_ONLY')
