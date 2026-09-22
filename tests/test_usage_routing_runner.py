"""Offline fault injection for the cloud probe orchestration; no credentials or API calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('usage_routing_runner',
                                             ROOT / 'scripts/run_usage_routing_probe.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
probe = runner.probe
FIXTURE = {'canonical': {'event_name': probe.CANONICAL_EVENT},
           'summary': {'event_name': probe.SUMMARY_EVENT}}
RUN_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.cloud = Mock()
        self.cloud.write_probe.return_value = {'ok': True}
        self.args = SimpleNamespace(readiness_attempts=1, readiness_polls=2,
                                    readiness_seconds=0)

    @staticmethod
    def buckets(cloud, records, *args):
        return ({name: probe.reconcile(keys, keys)
                 for name, keys in probe.expected_keys(records).items()}, True)

    def query_results(self, missing_event=None, complete=True, page_token=None):
        def query(sql, label):
            records = [call.args[0] for call in self.cloud.write_probe.call_args_list]
            event = probe.SUMMARY_EVENT if 'summary' in label else probe.CANONICAL_EVENT
            keys = [probe.entry_key(r) for r in records if r['event_name'] == event]
            if event == missing_event:
                keys = []
            raw = {'jobComplete': complete}
            if page_token:
                raw['pageToken'] = page_token
            return keys, raw
        return query

    def run_readiness(self):
        with patch.object(runner, 'read_buckets', side_effect=self.buckets), \
                patch.object(runner.time, 'sleep'):
            return runner.await_routing_ready(self.cloud, self.args, FIXTURE, RUN_ID)

    def test_bucket_success_does_not_hide_either_missing_bigquery_canary(self):
        for event in (probe.CANONICAL_EVENT, probe.SUMMARY_EVENT):
            with self.subTest(event=event):
                self.cloud.reset_mock()
                self.cloud.query_bigquery.side_effect = self.query_results(event)
                with self.assertRaisesRegex(RuntimeError, 'never confirmed live'):
                    self.run_readiness()
                self.assertEqual(self.cloud.query_bigquery.call_count, 4)

    def test_waits_for_both_bigquery_destinations_before_readiness(self):
        real_query = self.query_results()
        calls = 0

        def delayed_query(sql, label):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return [], {'jobComplete': True}
            return real_query(sql, label)

        self.cloud.query_bigquery.side_effect = delayed_query
        attempts, written = self.run_readiness()
        self.assertTrue(attempts[-1]['ready'])
        self.assertEqual(calls, 4)
        self.assertEqual(len(written), 2)

    def test_incomplete_bigquery_results_cannot_establish_readiness(self):
        for complete, page in ((False, None), (True, 'next-page')):
            with self.subTest(complete=complete, page=page):
                self.cloud.reset_mock()
                self.cloud.query_bigquery.side_effect = self.query_results(
                    complete=complete, page_token=page)
                with self.assertRaisesRegex(RuntimeError, 'never confirmed live'):
                    self.run_readiness()

    def test_default_bucket_leak_prevents_readiness_even_when_bigquery_arrives(self):
        self.cloud.query_bigquery.side_effect = self.query_results()

        def leaked(cloud, records, *args):
            results, complete = self.buckets(cloud, records)
            results['_Default'] = probe.reconcile([], [probe.entry_key(records[0])])
            return results, complete

        with patch.object(runner, 'read_buckets', side_effect=leaked), \
                patch.object(runner.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'never confirmed live'):
                runner.await_routing_ready(self.cloud, self.args, FIXTURE, RUN_ID)


class CleanupTests(unittest.TestCase):
    def run_main(self, cloud):
        result = {'summary': {'verdict': 'MEASURED'},
                  'bigquery': {'summary': {'verdict': 'MEASURED'},
                               'gate': {'status': 'PASS'}}, 'export_metrics': {}}
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.json'
            fixture.write_text(json.dumps(FIXTURE))
            argv = ['probe', '--apply', '--enable-sinks', '--output-dir', directory,
                    '--fixture', str(fixture), '--settle-seconds', '0']
            with patch.object(runner.sys, 'argv', argv), \
                    patch.object(runner, 'Cloud', return_value=cloud), \
                    patch.object(runner, 'await_routing_ready', return_value=([], [])), \
                    patch.object(runner, 'measure', return_value=result) as measure, \
                    patch.object(runner.time, 'sleep'), patch('builtins.print'):
                return runner.main(), measure.call_count

    def test_partial_enable_failure_still_disables_all_sinks(self):
        cloud = Mock()
        # Use the real four-PATCH loop; the second enable fails after the first succeeds.
        enabled = set()
        disabled = set()

        def call(method, url, body, **kwargs):
            name = url.rsplit('/', 1)[-1]
            if not body['disabled']:
                if enabled:
                    raise RuntimeError('second enable failed')
                enabled.add(name)
            else:
                disabled.add(name)
            return body

        cloud.call.side_effect = call
        # Capture the real method before run_main patches the Cloud constructor.
        toggle = runner.Cloud.toggle_sinks
        cloud.toggle_sinks.side_effect = lambda enabled: toggle(cloud, enabled)
        with self.assertRaisesRegex(RuntimeError, 'second enable failed'):
            self.run_main(cloud)
        self.assertEqual(len(enabled), 1)
        self.assertEqual(disabled, set(probe.SINKS))

    def test_disable_exhaustion_returns_failure_despite_passing_delivery(self):
        cloud = Mock()
        cloud.toggle_sinks.side_effect = [{}, RuntimeError('offline'),
                                         RuntimeError('offline'), RuntimeError('offline')]
        status, measurements = self.run_main(cloud)
        self.assertEqual(status, 3)
        self.assertEqual(measurements, 1)
        self.assertEqual(cloud._refresh_owner_token.call_count, 3)

    def test_incomplete_or_false_disable_state_is_not_success(self):
        for state in ({}, {probe.SINKS[0]: True}, dict.fromkeys(probe.SINKS, False)):
            with self.subTest(state=state):
                cloud = Mock()
                cloud.toggle_sinks.side_effect = [{}, state, state, state]
                self.assertEqual(self.run_main(cloud)[0], 3)

    def test_transient_cleanup_failure_recovers_and_returns_success(self):
        cloud = Mock()
        cloud.toggle_sinks.side_effect = [{}, RuntimeError('offline'),
                                         dict.fromkeys(probe.SINKS, True)]
        self.assertEqual(self.run_main(cloud)[0], 0)
        self.assertEqual(cloud._refresh_owner_token.call_count, 2)


if __name__ == '__main__':
    unittest.main()
