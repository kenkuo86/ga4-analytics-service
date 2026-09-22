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


class MeasurementTests(unittest.TestCase):
    def run_measurement(self, delayed_event=None, arrive_on=2, incomplete=False,
                        logging_complete=True):
        cloud = Mock()
        records = probe.build_records(FIXTURE, 100)
        expected = probe.expected_bq_keys(records)
        cloud.write_probe.side_effect = lambda r, run: dict(r, ok=True)
        cloud.export_metrics.return_value = {}
        calls = 0

        def query(sql, label):
            nonlocal calls
            poll = calls // 2 + 1
            calls += 1
            table = next(t for t, _, _ in probe.BQ_TABLES if t.endswith('.' + label))
            event = probe.SUMMARY_EVENT if 'summary' in label else probe.CANONICAL_EVENT
            keys = list(expected[table])
            if event == delayed_event and poll < arrive_on:
                keys = []
            return keys, {'jobComplete': not incomplete}

        cloud.query_bigquery.side_effect = query
        buckets, _ = ReadinessTests.buckets(cloud, records)
        args = SimpleNamespace(probes=100, spacing_seconds=0, polls=3, poll_seconds=0)
        with patch.object(probe, 'build_records', return_value=records), \
                patch.object(runner, 'read_buckets', return_value=(buckets, logging_complete)), \
                patch.object(runner.time, 'sleep'), patch('builtins.print'):
            result = runner.measure(cloud, args, FIXTURE, RUN_ID)
        return result, calls, cloud

    def test_polls_each_bigquery_destination_after_logging_is_complete(self):
        for event in (probe.CANONICAL_EVENT, probe.SUMMARY_EVENT):
            with self.subTest(event=event):
                result, calls, cloud = self.run_measurement(delayed_event=event)
                self.assertEqual(calls, 4)
                self.assertEqual(result['poll'], 1)
                self.assertEqual(result['bigquery']['summary']['delivered'], 200)
                self.assertEqual(runner.delivery_exit_code(result['bigquery'], result['summary']), 0)
                saved = [call.args[0] for call in cloud.save.call_args_list]
                self.assertIn('probe-delivery-poll-0', saved)
                self.assertIn('probe-delivery-poll-1', saved)

    def test_missing_canonical_stops_at_poll_limit_and_fails_gate(self):
        result, calls, _ = self.run_measurement(probe.CANONICAL_EVENT, arrive_on=99)
        self.assertEqual(calls, 6)
        self.assertEqual(result['poll'], 2)
        self.assertEqual(runner.delivery_exit_code(result['bigquery'], result['summary']), 2)

    def test_summary_delay_uses_full_budget_but_does_not_change_canonical_gate(self):
        result, calls, _ = self.run_measurement(probe.SUMMARY_EVENT, arrive_on=99)
        self.assertEqual(calls, 6)
        self.assertEqual(result['bigquery']['summary']['lost'], 100)
        self.assertEqual(runner.delivery_exit_code(result['bigquery'], result['summary']), 0)

    def test_incomplete_bigquery_never_passes_even_if_expected_keys_are_present(self):
        result, calls, _ = self.run_measurement(incomplete=True)
        self.assertEqual(calls, 6)
        self.assertEqual(result['bigquery']['gate']['status'], 'NOT_MEASURED')
        self.assertEqual(runner.delivery_exit_code(result['bigquery'], result['summary']), 2)

    def test_incomplete_logging_cannot_pass_with_a_passing_bigquery_gate(self):
        result, calls, _ = self.run_measurement(logging_complete=False)
        self.assertEqual(calls, 6)
        self.assertEqual(result['bigquery']['gate']['status'], 'PASS')
        self.assertEqual(runner.delivery_exit_code(result['bigquery'], result['summary']), 2)


class ReconcileCliTests(unittest.TestCase):
    def test_reconcile_mode_returns_actual_validation_status_without_writes(self):
        records = probe.build_records(FIXTURE, 100)
        expected = probe.expected_bq_keys(records)
        cases = [('pass', 0), ('missing', 2), ('incomplete', 2),
                 ('paginated', 2), ('unexpected', 1), ('empty', 2)]
        for scenario, status in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                cloud = Mock()

                def query(sql, label):
                    table = next(t for t, _, _ in probe.BQ_TABLES if t.endswith('.' + label))
                    keys = list(expected[table]) if scenario != 'missing' else []
                    if scenario == 'unexpected':
                        keys.append(('unexpected', 'event', 'log', 'insert'))
                    raw = {'jobComplete': scenario != 'incomplete'}
                    if scenario == 'paginated':
                        raw['pageToken'] = 'next'
                    return keys if scenario != 'empty' else [], raw

                cloud.query_bigquery.side_effect = query
                ack_file = Path(directory) / 'acks.json'
                ack_file.write_text(json.dumps([] if scenario == 'empty' else
                                               [dict(r, ok=True) for r in records]))
                argv = ['probe', '--apply', '--output-dir', directory,
                        '--reconcile-acks', str(ack_file), '--run-id', RUN_ID,
                        '--window-lower', '2026-09-22T00:00:00Z',
                        '--window-upper', '2026-09-22T01:00:00Z']
                with patch.object(runner.sys, 'argv', argv), \
                        patch.object(runner, 'Cloud', return_value=cloud), patch('builtins.print'):
                    self.assertEqual(runner.main(), status)
                cloud.write_probe.assert_not_called()
                cloud.toggle_sinks.assert_not_called()
                cloud.impersonate.assert_not_called()

    def test_isolation_failure_takes_priority_over_incomplete_evidence(self):
        outcome = {'summary': {'verdict': 'INCOMPLETE', 'isolation_holds': False},
                   'gate': {'status': 'NOT_MEASURED'}}
        self.assertEqual(runner.delivery_exit_code(outcome), 1)


if __name__ == '__main__':
    unittest.main()
