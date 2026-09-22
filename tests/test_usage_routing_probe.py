import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import usage_logging

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('usage_routing_probe',
                                              ROOT / 'scripts/probe_usage_routing.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

PLAN_SPEC = importlib.util.spec_from_file_location('usage_routing_plan',
                                                   ROOT / 'scripts/plan_usage_routing.py')
routing = importlib.util.module_from_spec(PLAN_SPEC)
PLAN_SPEC.loader.exec_module(routing)

FIXTURE = {'canonical': {'schema_version': '1.0', 'event_name': 'analytics_request_completed',
                         'interaction_id': 'seed', 'event_time': 'seed', 'status': 'success'},
           'summary': {'schema_version': '1.0', 'event_name': 'analytics_request_summary',
                       'interaction_id': 'seed', 'event_time': 'seed',
                       'request_summary': 'synthetic'}}


class ProbeContractTests(unittest.TestCase):
    def test_probe_write_matches_the_runtime_writer_apart_from_the_synthetic_marker(self):
        """Drift here would measure a shape the service never sends."""
        record = probe.build_records(FIXTURE, 1)[0]
        writer = usage_logging.LoggingWriter(probe.PROJECT)
        writer.session = Mock()
        writer.session.post.return_value = Mock(**{'raise_for_status.return_value': None})
        writer(record)
        runtime_body = writer.session.post.call_args.kwargs['json']
        probe_body = probe.write_body(record)
        self.assertEqual(probe_body['entries'], runtime_body['entries'])
        self.assertEqual(probe_body['logName'], runtime_body['logName'])
        self.assertEqual(probe_body['resource'], runtime_body['resource'])
        self.assertEqual(len(probe_body['entries']), 1)
        self.assertEqual(probe_body['labels'],
                         dict(runtime_body['labels'], usage_validation='true'))
        self.assertNotIn('usage_validation', runtime_body['labels'])
        run = '0f8fad5b-d9cb-469f-a165-70867728950e'
        self.assertEqual(probe.write_body(record, run)['labels']['usage_probe_run'], run)
        with self.assertRaises(ValueError):
            probe.write_body(record, 'NOT-A-UUID')

    def test_probe_constants_track_the_reviewed_resource_plan(self):
        plan = routing.plan('user:owner@example.test')
        self.assertEqual(probe.PROJECT, routing.PROJECT)
        self.assertEqual(probe.LOCATION, routing.LOCATION)
        self.assertEqual(sorted(probe.SINKS), sorted(sink['name'] for sink in plan['sinks']))
        for sink in plan['sinks']:
            log = probe.SUMMARY_LOG if 'summary' in sink['name'] else probe.CANONICAL_LOG
            self.assertIn(f'logName="projects/{probe.PROJECT}/logs/{log}"', sink['filter'])

    def test_synthetic_probes_are_excluded_from_the_deduplicated_kpi_views(self):
        fields = json.loads((ROOT / 'telemetry/canonical.bigquery-payload.v1.json').read_text())
        self.assertIn('usage_validation', routing.dedup_sql('events', fields))
        self.assertEqual(probe.write_body(probe.build_records(FIXTURE, 1)[0])
                         ['labels']['usage_validation'], 'true')

    def test_each_probe_is_one_interaction_with_a_distinct_stable_insert_id(self):
        records = probe.build_records(FIXTURE, 3)
        self.assertEqual(len(records), 6)
        self.assertEqual(len({record['interaction_id'] for record in records}), 3)
        self.assertEqual(len({probe.insert_id_for(record) for record in records}), 6)
        for record in records:
            self.assertEqual(probe.insert_id_for(record),
                             f"{record['interaction_id']}:{record['event_name']}")
        for count in (0, probe.MAX_PROBES + 1):
            with self.assertRaises(ValueError):
                probe.build_records(FIXTURE, count)


class ReconciliationTests(unittest.TestCase):
    def test_expected_keys_split_canonical_and_attachment_and_keep_default_empty(self):
        records = probe.build_records(FIXTURE, 2)
        expected = probe.expected_keys(records)
        self.assertEqual(sum(expected['ga4_mcp_test_events'].values()), 2)
        self.assertEqual(sum(expected['ga4_mcp_test_summary'].values()), 2)
        self.assertEqual(sum(expected['_Default'].values()), 0)
        for key in expected['ga4_mcp_test_events']:
            self.assertEqual(key[1], probe.CANONICAL_EVENT)
            self.assertTrue(key[2].endswith(probe.CANONICAL_LOG))
        for key in expected['ga4_mcp_test_summary']:
            self.assertTrue(key[2].endswith(probe.SUMMARY_LOG))

    def test_reconcile_reports_missing_and_unexpected_by_exact_key(self):
        wanted = [('a', 'e', 'log', 'a:e'), ('b', 'e', 'log', 'b:e')]
        exact = probe.reconcile(wanted, wanted)
        self.assertEqual(exact['delivered'], 2)
        self.assertEqual(exact['loss_rate'], 0.0)
        self.assertTrue(exact['isolation_holds'])
        partial = probe.reconcile(wanted, wanted[:1])
        self.assertEqual(partial['missing'], [('b', 'e', 'log', 'b:e')])
        self.assertEqual(partial['loss_rate'], 0.5)
        # A right interaction id with the wrong insert id is a miss, not a delivery.
        wrong = probe.reconcile(wanted, [('a', 'e', 'log', 'a:e'), ('b', 'e', 'log', 'other')])
        self.assertEqual(wrong['delivered'], 1)
        self.assertEqual(wrong['unexpected'], [('b', 'e', 'log', 'other')])
        self.assertFalse(wrong['isolation_holds'])

    def test_default_bucket_contributes_isolation_not_loss(self):
        leaked = {'ga4_mcp_test_events': probe.reconcile([('a', 'e', 'log', 'a:e')],
                                                         [('a', 'e', 'log', 'a:e')]),
                  '_Default': probe.reconcile([], [('a', 'e', 'log', 'a:e')])}
        summary = probe.summarize(leaked)
        self.assertEqual(summary['expected'], 1)
        self.assertEqual(summary['loss_rate'], 0.0)
        self.assertFalse(summary['isolation_holds'])
        self.assertEqual(summary['verdict'], 'ISOLATION_FAILED')

    def test_incomplete_search_never_reads_as_zero_loss(self):
        clean = {'ga4_mcp_test_events': probe.reconcile([('a', 'e', 'log', 'a:e')], [])}
        self.assertEqual(probe.summarize(clean, searches_complete=False)['verdict'], 'INCOMPLETE')
        self.assertEqual(probe.summarize(clean, searches_complete=True)['verdict'], 'MEASURED')

    def test_measured_loss_is_reported_with_a_confidence_bound(self):
        wanted = [(str(index), 'e', 'log', f'{index}:e') for index in range(100)]
        summary = probe.summarize({'b': probe.reconcile(wanted, wanted)})
        self.assertEqual(summary['loss_rate'], 0.0)
        # Zero observed losses still bound the true rate away from an absolute claim.
        low, high = summary['loss_rate_95ci']
        self.assertAlmostEqual(low, 0.0)
        self.assertGreater(high, 0.0)
        self.assertLess(high, 0.05)
        self.assertEqual(probe.wilson_interval(0, 0), (None, None))
        half = probe.wilson_interval(50, 100)
        self.assertLess(half[0], 0.5)
        self.assertGreater(half[1], 0.5)


class SearchTests(unittest.TestCase):
    def test_collect_pages_follows_every_page_then_fails_closed_at_the_cap(self):
        pages = [{'entries': [1], 'nextPageToken': 'x'}, {'entries': [2], 'nextPageToken': 'y'},
                 {'entries': [3]}]
        seen = []

        def fetch(request):
            seen.append(request.get('pageToken'))
            return pages[len(seen) - 1]

        entries, complete = probe.collect_pages(fetch, {'filter': 'f'})
        self.assertEqual((entries, complete), ([1, 2, 3], True))
        self.assertEqual(seen, [None, 'x', 'y'])
        endless = lambda request: {'entries': [1], 'nextPageToken': 'more'}  # noqa: E731
        entries, complete = probe.collect_pages(endless, {'filter': 'f'}, max_pages=3)
        self.assertEqual((len(entries), complete), (3, False))

    def test_delivery_filter_is_bounded_and_rejects_non_literal_input(self):
        run = '0f8fad5b-d9cb-469f-a165-70867728950e'
        built = probe.delivery_filter(probe.CANONICAL_LOG, run,
                                      '2026-09-22T02:00:00Z', '2026-09-22T02:30:00Z')
        self.assertIn(f'labels.usage_probe_run="{run}"', built)
        self.assertIn('labels.usage_validation="true"', built)
        self.assertIn('timestamp>="2026-09-22T02:00:00Z"', built)
        self.assertIn(f'logName="projects/{probe.PROJECT}/logs/{probe.CANONICAL_LOG}"', built)
        with self.assertRaises(ValueError):
            probe.delivery_filter('other_log', run, '2026-09-22T02:00:00Z', '2026-09-22T02:30:00Z')
        for bad in ('" OR "a"="a', 'NOT-A-UUID', run.upper(), None, ''):
            with self.assertRaises(ValueError):
                probe.delivery_filter(probe.CANONICAL_LOG, bad,
                                      '2026-09-22T02:00:00Z', '2026-09-22T02:30:00Z')
        with self.assertRaises(ValueError):
            probe.delivery_filter(probe.CANONICAL_LOG, run, 'yesterday', '2026-09-22T02:30:00Z')

    def test_filter_length_does_not_grow_with_the_sample(self):
        """Enumerating ids would near the Logging filter limit at a few hundred probes."""
        run = '0f8fad5b-d9cb-469f-a165-70867728950e'
        built = probe.delivery_filter(probe.CANONICAL_LOG, run,
                                      '2026-09-22T02:00:00Z', '2026-09-22T02:30:00Z')
        self.assertLess(len(built), 400)
        sql = probe.bigquery_check_sql(probe.BQ_TABLES[0][0], run,
                                       '2026-09-22T02:00:00Z', '2026-09-22T02:30:00Z')
        self.assertLess(len(sql), 800)

    def test_observed_key_tolerates_entries_without_a_payload(self):
        self.assertEqual(probe.observed_key({'logName': 'l', 'insertId': 'i'}),
                         (None, None, 'l', 'i'))


class BigQueryReconciliationTests(unittest.TestCase):
    """Sinks export independently, so the bucket copy proves nothing about BigQuery."""

    IDENT = '0f8fad5b-d9cb-469f-a165-70867728950e'

    def test_check_sql_filters_the_partition_and_only_accepts_known_tables(self):
        table = probe.BQ_TABLES[0][0]
        sql = probe.bigquery_check_sql(table, self.IDENT,
                                       '2026-09-22T03:00:00Z', '2026-09-22T04:00:00Z')
        self.assertIn(f'FROM `{table}`', sql)
        self.assertIn("timestamp >= TIMESTAMP('2026-09-22T03:00:00Z')", sql)
        self.assertIn(f"labels.usage_probe_run = '{self.IDENT}'", sql)
        self.assertNotIn('JSON_VALUE(TO_JSON(labels)', sql)
        for bad_table in ('ga4-reports-dev.other.table', 'ga4_mcp_test_events.ga4_mcp_test_v1'):
            with self.assertRaises(ValueError):
                probe.bigquery_check_sql(bad_table, self.IDENT,
                                         '2026-09-22T03:00:00Z', '2026-09-22T04:00:00Z')
        for bad_id in ("x' OR '1'='1", 'NOT-A-UUID', None, ''):
            with self.assertRaises(ValueError):
                probe.bigquery_check_sql(table, bad_id,
                                         '2026-09-22T03:00:00Z', '2026-09-22T04:00:00Z')
        with self.assertRaises(ValueError):
            probe.bigquery_check_sql(table, self.IDENT, 'today', '2026-09-22T04:00:00Z')

    def test_expected_bq_keys_match_the_bucket_keys_for_the_same_records(self):
        records = probe.build_records(FIXTURE, 3)
        by_table = probe.expected_bq_keys(records)
        by_bucket = probe.expected_keys(records)
        self.assertEqual(by_table[probe.BQ_TABLES[0][0]], by_bucket['ga4_mcp_test_events'])
        self.assertEqual(by_table[probe.BQ_TABLES[1][0]], by_bucket['ga4_mcp_test_summary'])

    def test_bigquery_rows_parse_into_the_same_key_shape(self):
        rows = [{'f': [{'v': self.IDENT}, {'v': probe.CANONICAL_EVENT},
                       {'v': 'projects/p/logs/l'}, {'v': 'insert'}]}]
        self.assertEqual(probe.bigquery_keys(rows),
                         [(self.IDENT, probe.CANONICAL_EVENT, 'projects/p/logs/l', 'insert')])
        with self.assertRaises(ValueError):
            probe.bigquery_keys([{'f': [{'v': 'only-one'}]}])

    def test_rejected_writes_never_inflate_the_measured_loss_rate(self):
        """A 401 or quota rejection is not routing loss; counting it as such misreports."""
        records = probe.build_records(FIXTURE, 4)
        acks = [{'interaction_id': record['interaction_id'], 'event_name': record['event_name'],
                 'ok': index < 4} for index, record in enumerate(records)]
        scope = probe.records_from_acks(acks)
        self.assertEqual(len(scope), 4)
        # Everything that was accepted arrived: the run is clean, not 50% lossy.
        expected = probe.expected_keys(scope)
        observed = {bucket: list(keys.elements()) for bucket, keys in expected.items()}
        reconciliations = {bucket: probe.reconcile(expected[bucket], observed[bucket])
                           for bucket in expected}
        summary = probe.summarize(reconciliations)
        self.assertEqual(summary['expected'], 4)
        self.assertEqual(summary['loss_rate'], 0.0)
        # Using every attempted write instead would have invented four losses.
        naive = probe.summarize({bucket: probe.reconcile(keys, observed[bucket])
                                 for bucket, keys in probe.expected_keys(records).items()})
        self.assertEqual(naive['lost'], 4)

    def test_acks_rebuild_expected_keys_and_drop_rejected_writes(self):
        acks = [{'interaction_id': self.IDENT, 'event_name': probe.CANONICAL_EVENT, 'ok': True},
                {'interaction_id': self.IDENT, 'event_name': probe.SUMMARY_EVENT, 'ok': False}]
        rebuilt = probe.records_from_acks(acks)
        self.assertEqual(len(rebuilt), 1)
        expected = probe.expected_bq_keys(rebuilt)
        self.assertEqual(sum(expected[probe.BQ_TABLES[0][0]].values()), 1)
        self.assertEqual(sum(expected[probe.BQ_TABLES[1][0]].values()), 0)


class ExportCrossCheckTests(unittest.TestCase):
    def test_export_totals_count_only_the_poc_sinks(self):
        series = {'timeSeries': [
            {'resource': {'labels': {'name': 'ga4-mcp-test-events-bq-v1'}},
             'points': [{'value': {'int64Value': '2'}}, {'value': {'int64Value': '3'}}]},
            {'resource': {'labels': {'name': '_Default'}},
             'points': [{'value': {'int64Value': '99'}}]}]}
        totals = probe.sum_export_points(series)
        self.assertEqual(totals['ga4-mcp-test-events-bq-v1'], 5)
        self.assertNotIn('_Default', totals)

    def test_a_sink_with_no_series_is_unavailable_not_zero(self):
        """Monitoring lags the writes; absence must never be read as no export."""
        totals = probe.sum_export_points({'timeSeries': []})
        self.assertEqual(set(totals), set(probe.SINKS))
        self.assertTrue(all(value is None for value in totals.values()))
        checked = probe.export_cross_check(totals, probe.expected_exports(
            probe.build_records(FIXTURE, 1)))
        self.assertTrue(all(entry['status'] == 'unavailable' for entry in checked.values()))
        self.assertTrue(all(entry['observed'] is None for entry in checked.values()))

    def test_export_cross_check_labels_match_and_shortfall(self):
        records = probe.build_records(FIXTURE, 2)
        expected = probe.expected_exports(records)
        observed = dict.fromkeys(probe.SINKS, 2)
        observed['ga4-mcp-test-summary-bucket-v1'] = 1
        checked = probe.export_cross_check(observed, expected)
        self.assertEqual(checked['ga4-mcp-test-events-bq-v1']['status'], 'match')
        self.assertEqual(checked['ga4-mcp-test-summary-bucket-v1']['status'], 'short')


class ReadinessGateTests(unittest.TestCase):
    """A fixed settle time is not proof; entries written mid-propagation vanish."""

    def test_readiness_requires_every_routed_destination_to_hold_exact_keys(self):
        full = {'ga4_mcp_test_events': probe.reconcile([('a', 'e', 'l', 'a:e')],
                                                       [('a', 'e', 'l', 'a:e')]),
                'ga4_mcp_test_summary': probe.reconcile([('a', 's', 'l', 'a:s')],
                                                        [('a', 's', 'l', 'a:s')]),
                '_Default': probe.reconcile([], [])}
        self.assertTrue(probe.readiness_reached(full))
        partial = dict(full, ga4_mcp_test_summary=probe.reconcile([('a', 's', 'l', 'a:s')], []))
        self.assertFalse(probe.readiness_reached(partial))
        leaked = dict(full, _Default=probe.reconcile([], [('a', 'e', 'l', 'a:e')]))
        self.assertFalse(probe.readiness_reached(leaked))

    def test_readiness_is_never_reached_on_an_empty_reconciliation(self):
        self.assertFalse(probe.readiness_reached({}))
        self.assertFalse(probe.readiness_reached({'_Default': probe.reconcile([], [])}))

    def test_readiness_canaries_belong_in_the_expected_export_counter(self):
        """Canary exports land inside the metric window; omitting them fakes a shortfall."""
        measured = probe.build_records(FIXTURE, 3)
        canaries = probe.build_records(FIXTURE, 1)
        without = probe.expected_exports(measured)
        with_canary = probe.expected_exports(measured + canaries)
        self.assertEqual(without['ga4-mcp-test-events-bq-v1'], 3)
        self.assertEqual(with_canary['ga4-mcp-test-events-bq-v1'], 4)
        observed = dict.fromkeys(probe.SINKS, 4)
        self.assertTrue(all(entry['status'] == 'match' for entry in
                            probe.export_cross_check(observed, with_canary).values()))
        self.assertTrue(all(entry['status'] == 'excess' for entry in
                            probe.export_cross_check(observed, without).values()))

    def test_expected_exports_count_each_entry_once_per_matching_sink(self):
        records = probe.build_records(FIXTURE, 4)
        self.assertEqual(probe.expected_exports(records),
                         {'ga4-mcp-test-events-bq-v1': 4, 'ga4-mcp-test-events-bucket-v1': 4,
                          'ga4-mcp-test-summary-bq-v1': 4, 'ga4-mcp-test-summary-bucket-v1': 4})


class ProbeCliTests(unittest.TestCase):
    def test_probe_module_cli_is_offline_and_bounds_the_sample(self):
        with tempfile.TemporaryDirectory() as path, \
             patch('sys.argv', ['probe', '--output-dir', path, '--probes', '25']), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            probe.main()
            document = json.loads((Path(path) / 'usage-routing-probe-plan.json').read_text())
        self.assertEqual(document['mode'], 'PLAN_ONLY')
        self.assertEqual((document['probes'], document['entries']), (25, 50))
        self.assertIn('IAM reads or writes', document['never'])
        with self.assertRaises(ValueError):
            probe.plan_document(probe.MAX_PROBES + 1)


class RolloutGateTests(unittest.TestCase):
    """The agreed gate is canonical delivery to BigQuery >= 95%, judged conservatively."""

    def table(self, delivered, expected):
        keys = [(str(index), probe.CANONICAL_EVENT, 'log', f'{index}:c')
                for index in range(expected)]
        return {probe.CANONICAL_BQ_TABLE: probe.reconcile(keys, keys[:delivered])}

    def test_gate_uses_the_lower_confidence_bound_not_the_point_estimate(self):
        # 75/75 looks perfect but only bounds delivery at ~95.1% — barely over the line.
        small = probe.gate_check(self.table(74, 75))
        self.assertAlmostEqual(small['delivery_rate'], 74 / 75)
        self.assertLess(small['delivery_rate_95_lower'], small['delivery_rate'])
        self.assertEqual(small['status'], 'FAIL')
        # The same rate over a larger sample clears it.
        large = probe.gate_check(self.table(296, 300))
        self.assertEqual(large['status'], 'PASS')
        self.assertGreaterEqual(large['delivery_rate_95_lower'], 0.95)

    def test_gate_reports_not_measured_rather_than_passing_on_no_data(self):
        self.assertEqual(probe.gate_check({})['status'], 'NOT_MEASURED')
        self.assertEqual(probe.gate_check(self.table(0, 0))['status'], 'NOT_MEASURED')

    def test_gate_ignores_the_summary_attachment(self):
        tables = self.table(300, 300)
        tables[probe.BQ_TABLES[1][0]] = probe.reconcile(
            [(str(i), probe.SUMMARY_EVENT, 'log', f'{i}:s') for i in range(300)], [])
        self.assertEqual(probe.gate_check(tables)['status'], 'PASS')


class ExportSettlingTests(unittest.TestCase):
    """Sink counters trail the writes by minutes, so a low count is not yet a shortfall."""

    def test_unsettled_shortfall_is_inconclusive_not_short(self):
        expected = {'ga4-mcp-test-events-bq-v1': 301}
        observed = {'ga4-mcp-test-events-bq-v1': 224}
        self.assertEqual(probe.export_cross_check(observed, expected, settled=False)
                         ['ga4-mcp-test-events-bq-v1']['status'], 'inconclusive')
        self.assertEqual(probe.export_cross_check(observed, expected, settled=True)
                         ['ga4-mcp-test-events-bq-v1']['status'], 'short')

    def test_match_and_excess_stay_meaningful_before_settling(self):
        expected = {'ga4-mcp-test-events-bq-v1': 301}
        for observed, wanted in (({'ga4-mcp-test-events-bq-v1': 301}, 'match'),
                                 ({'ga4-mcp-test-events-bq-v1': 302}, 'excess')):
            self.assertEqual(probe.export_cross_check(observed, expected, settled=False)
                             ['ga4-mcp-test-events-bq-v1']['status'], wanted)


class ExportWindowTests(unittest.TestCase):
    """Every write counted as expected must lie inside the metric window."""

    def test_window_reaches_back_past_a_retried_readiness_phase(self):
        canaries = [{'event_time': '2026-09-22T05:00:00Z'},
                    {'event_time': '2026-09-22T05:04:00Z'}]
        lower, upper = probe.export_metric_window(
            '2026-09-22T05:10:00Z', canaries,
            now=__import__('datetime').datetime(2026, 9, 22, 5, 30,
                                                tzinfo=__import__('datetime').timezone.utc))
        self.assertEqual(lower, '2026-09-22T04:58:00Z')
        self.assertEqual(upper, '2026-09-22T05:31:00Z')
        # Without canaries the window starts from the measurement instead.
        bare, _ = probe.export_metric_window('2026-09-22T05:10:00Z')
        self.assertEqual(bare, '2026-09-22T05:08:00Z')

    def test_window_rejects_non_rfc3339_input(self):
        with self.assertRaises(ValueError):
            probe.export_metric_window('yesterday')
        with self.assertRaises(ValueError):
            probe.export_metric_window('2026-09-22T05:10:00Z', [{'event_time': 'soon'}])
